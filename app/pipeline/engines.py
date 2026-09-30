"""声文 · 引擎适配层

统一接口：`stream(...)` 产出 Segment 迭代器，使上层管线与具体引擎解耦。

四个实现：
  WhisperEngine      faster-whisper / CTranslate2 —— 原生流式，逐段产出
  SenseVoiceEngine   sherpa-onnx / ONNX Runtime（2026-09-30 起）—— 原生不支持流式，
                     用「静音检测切块」实现近似逐句产出，并顺带补上时间戳
  ParakeetEngine     英文专用，CPU 上 25×（NeMo transducer）
  MoonshineEngine    英文专用，体积最小 274MB

★ SenseVoice 的实测特性：
  1. 不提供**句级**时间戳 → 用静音切块后按块边界给时间，SRT 才可用
     （但 sherpa-onnx 提供**字级**时间戳 timestamps，见 _tokens_to_words）
  2. 在纯英文音频上会「语言漂移」（整段输出中文）→ 上层必须做语言一致性校验
  3. ★★ 单段必须 < 30 秒，否则又慢又掉字且不报错 —— 见 SENSEVOICE_MAX_CHUNK
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.doctor import env as doctor  # noqa: E402  必须先导入以完成 DLL 引导

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


@dataclass
class Segment:
    index: int
    start: float
    end: float
    text: str
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0
    # 字级时间戳（sherpa-onnx 提供）。为空表示该引擎/该段没有 —— 消费方需容忍缺失。
    words: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "index": self.index,
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "text": self.text,
            "avg_logprob": round(self.avg_logprob, 3),
            "no_speech_prob": round(self.no_speech_prob, 3),
        }
        if self.words:
            d["words"] = self.words
        return d


class EngineError(RuntimeError):
    pass


# ═══════════════════════════════════════════════════════════
# 静音检测与切块规划（供 SenseVoice 使用）
# ═══════════════════════════════════════════════════════════


def _load_bpe_pieces(tokens_path: Path) -> set[str]:
    """读出一份 sherpa tokens.txt 里的全部子词。"""
    out: set[str] = set()
    try:
        for ln in tokens_path.read_text(encoding="utf-8").splitlines():
            p = ln.split()
            if len(p) == 2:
                out.add(p[0])
    except Exception:  # noqa: BLE001
        return set()
    return out


def bpe_split_ok(word: str, pieces: set[str], max_piece: int = 24) -> bool:
    """这个词能不能被切成词表里已有的子词？

    ★ 为什么要预先判断：模型的热词是**装载期**加载的，失败时 sherpa 只在
      stderr 打一行 "Cannot find ID for token XXX"，Python 侧没有任何异常、
      last_info 里也看不出异常 —— 参数传进去了、看着像生效、实际效果为零。
      与其事后猜，不如在写词表之前就自己判一遍，把"过滤掉了几个"如实报出来。

    词首与空格按 BPE 约定写成 ``▁``（SentencePiece 风格）；
    实测 "generative engine optimization" 这类多词短语也正因此才编码得进去。
    空词表 → 无从判断，一律放行（宁可多留一个词，也不要误删）。
    """
    if not pieces:
        return True
    cand = "▁" + word.strip().replace(" ", "▁")
    n = len(cand)
    reach = [False] * (n + 1)
    reach[0] = True
    for i in range(n):
        if not reach[i]:
            continue
        for j in range(i + 1, min(n, i + max_piece) + 1):
            if cand[i:j] in pieces:
                reach[j] = True
                if j == n:
                    return True
    return reach[n]


def _write_bpe_vocab(tokens_path: Path, cache_dir: Path) -> Path | None:
    """把模型的 tokens.txt 合成成 sherpa 需要的 BPE 词表（``piece score``）。

    ★★ 这不是可选优化，是让热词真正生效的**必需**一步。实测依据：

        只给 hotwords_file、不给 bpe_vocab 时，sherpa 拿**整词**去 tokens.txt 里查 ID。
        而 Parakeet 的 tokens.txt 是子词表（▁t / ▁th / in … 共 1025 条），整词查不到，
        于是 16 个英文热词**全被跳过**，只在 stderr 留下一行提示。
        补上 bpe_vocab + modeling_unit="bpe" 之后，同一批词（含多词短语）全部编码成功。

    分数用 ``-id``：tokens.txt 的顺序本身就是合并顺序（越靠前越常用），
    分数越高越优先合并，因此 ``-id`` 恰好还原了模型的原始优先级。
    按内容命名缓存，模型换了自动生成新的一份。
    """
    if not tokens_path.exists():
        return None
    import hashlib

    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(tokens_path.read_bytes()).hexdigest()[:12]
    out = cache_dir / f"bpe_{key}.vocab"
    if out.exists():
        return out
    lines: list[str] = []
    for ln in tokens_path.read_text(encoding="utf-8").splitlines():
        p = ln.split()
        if len(p) != 2:
            continue
        try:
            lines.append(f"{p[0]} {-int(p[1])}")
        except ValueError:
            continue
    if not lines:
        return None
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def detect_silences(audio: Path, noise_db: int = -32, min_silence: float = 0.45) -> list[tuple[float, float]]:
    """用 ffmpeg silencedetect 找静音区间。零额外模型依赖。"""
    r = subprocess.run(
        [FFMPEG, "-v", "info", "-i", str(audio),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
         "-f", "null", "-"],
        capture_output=True, text=True, timeout=1800,
    )
    text = (r.stderr or "") + (r.stdout or "")
    starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", text)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", text)]
    out: list[tuple[float, float]] = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else s + 1e9
        out.append((s, e))
    return out


def audio_duration(audio: Path) -> float:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio)],
        capture_output=True, text=True, timeout=60,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def plan_chunks(duration: float, silences: list[tuple[float, float]],
                target: float = 45.0, hard_max: float = 90.0) -> list[tuple[float, float]]:
    """按目标时长切块，切点尽量落在静音中点，避免把词切断。"""
    if duration <= 0:
        return []
    if duration <= hard_max:
        return [(0.0, duration)]

    # 静音中点作为候选切点
    cuts = [ (s + e) / 2 for s, e in silences if e - s >= 0.35 and 0 < (s + e) / 2 < duration ]

    chunks: list[tuple[float, float]] = []
    start = 0.0
    while start < duration:
        want = start + target
        if want >= duration:
            chunks.append((start, duration))
            break
        # 找离 want 最近、且不超过 hard_max 的切点
        cand = [c for c in cuts if start + 8 <= c <= start + hard_max]
        cut = min(cand, key=lambda c: abs(c - want)) if cand else min(want, start + hard_max)
        chunks.append((start, cut))
        start = cut
    # 合并过短的尾块
    if len(chunks) >= 2 and chunks[-1][1] - chunks[-1][0] < 6:
        s0, _ = chunks[-2]
        _, e1 = chunks[-1]
        chunks = chunks[:-2] + [(s0, e1)]
    return chunks


def split_audio(audio: Path, start: float, end: float, dst: Path) -> Path:
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-ss", f"{start}", "-t", f"{max(0.1, end - start)}",
         "-i", str(audio), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)],
        capture_output=True, text=True, timeout=900,
    )
    if not dst.exists():
        raise EngineError(f"切片失败 {start}-{end}")
    return dst


# ═══════════════════════════════════════════════════════════
# Whisper 引擎
# ═══════════════════════════════════════════════════════════


class WhisperEngine:
    name = "whisper"
    display = "Whisper large-v3-turbo"

    def __init__(self, model_path: Path | None = None, device: str | None = None,
                 compute_type: str | None = None):
        caps = doctor.engine_caps()
        if not caps.get("whisper_ok"):
            raise EngineError(f"Whisper 引擎不可用：{caps.get('whisper_error')}")

        self.model_path = str(model_path or (ROOT / "models" / "whisper-large-v3-turbo"))
        if device:
            self.device = device
        else:
            self.device = "cuda" if caps.get("cuda_devices") else "cpu"
        if compute_type:
            self.compute_type = compute_type
        else:
            self.compute_type = "float16" if self.device == "cuda" else "int8"
        self._m = None
        self.load_seconds = 0.0

    def load(self) -> float:
        from faster_whisper import WhisperModel

        t0 = time.perf_counter()
        self._m = WhisperModel(self.model_path, device=self.device,
                               compute_type=self.compute_type)
        self.load_seconds = time.perf_counter() - t0
        return self.load_seconds

    def stream(self, audio: Path, *, language: str | None = None, vad: bool = True,
               beam_size: int = 5, initial_prompt: str | None = None,
               batched: bool = True, batch_size: int = 16) -> Iterator[Segment]:
        if self._m is None:
            self.load()

        lang = language or None
        if lang in ("auto", ""):
            lang = None

        kwargs: dict = dict(beam_size=beam_size, vad_filter=vad)
        if lang:
            kwargs["language"] = lang
        if initial_prompt:
            kwargs["initial_prompt"] = initial_prompt

        if batched:
            # ★ 引擎硬约束：批处理必须搭配 VAD，否则报 "No clip timestamps found"
            from faster_whisper import BatchedInferencePipeline

            kwargs["vad_filter"] = True
            segments, info = BatchedInferencePipeline(model=self._m).transcribe(
                str(audio), batch_size=batch_size, **kwargs
            )
        else:
            segments, info = self._m.transcribe(str(audio), **kwargs)

        self.last_info = {
            "language": getattr(info, "language", None),
            "language_probability": round(getattr(info, "language_probability", 0) or 0, 4),
            "duration": getattr(info, "duration", 0.0),
            "device": self.device,
            "compute_type": self.compute_type,
            # ★ 以下四项是给 tools/effectiveness_check.py 用的**证据**：
            #   光看"参数传进来了"不够，得能从引擎侧读到"它确实被应用了"。
            #   没有这层，能力矩阵里的"声明支持"就只是口头承诺。
            #   注意 vad 要记**生效值**：开批处理时上面会强制打开 vad_filter，
            #   若照抄入参，用户关了 VAD、实际却开着，证据就是假的。
            "vad": bool(vad) or bool(batched),
            "batched": bool(batched),
            "batch_size": int(batch_size) if batched else 0,
            "initial_prompt_chars": len(initial_prompt or ""),
        }

        i = 0
        for seg in segments:
            i += 1
            yield Segment(
                index=i, start=seg.start, end=seg.end, text=seg.text.strip(),
                avg_logprob=getattr(seg, "avg_logprob", 0) or 0,
                no_speech_prob=getattr(seg, "no_speech_prob", 0) or 0,
            )


# ═══════════════════════════════════════════════════════════
# sherpa-onnx 引擎族（纯 ONNX Runtime，不依赖 PyTorch）
#
# 三个引擎共用同一套「静音切块 + 逐块解码」流程，只有装载方式不同：
#   SenseVoiceEngine   中文主力（from_sense_voice）
#   ParakeetEngine     英文专用，CPU 上 25×（from_transducer / NeMo）
#   MoonshineEngine    英文专用，体积最小 274MB（from_moonshine）
#
# ★ 2026-09-30 从 funasr + torch 迁到 sherpa-onnx，实测收益：
#     依赖 3.6 GB → 104 MB；SenseVoice 模型 901 → 230 MB；CPU 速度 +34%；
#     同一套代码可在 Windows / macOS / Linux(arm64) 运行
#
# ★★ 一条硬约束：单段必须 < 30 秒
#     SenseVoice 是自注意力结构，整段长音频喂进去注意力近似平方增长。
#     实测 8 分钟：整段喂 7.2× 且**掉字**；切 30 秒 38.4×。
#     **最阴险的是它不报错、不崩溃**，只是把「老师喜欢什么」悄悄变成「老师欢什么」。
#     所以：目标 25 秒、硬上限 28 秒，切块后再做一次强制兜底拆分。
#
# ★ 能力变化（诚实记录）：
#     · 失去：avg_logprob（本构建的 ys_log_probs 为空）→ 界面「低置信标注」不再出现
#     · 得到：逐字时间戳 timestamps → 将来可实现「词级时间轴」
# ═══════════════════════════════════════════════════════════

SHERPA_TARGET_CHUNK = 25.0     # 目标段长（秒）
SHERPA_MAX_CHUNK = 28.0        # 硬上限，必须 < 30
SHERPA_MAX_THREADS = 8         # ★ 实测最优：8 线程 43× > 16 线程 35× > 24 线程 20×


def sherpa_model_dir(rel: str) -> Path:
    """按注册表里声明的相对目录定位模型（不存在时回落到同级扫描）。"""
    d = ROOT / rel
    if (d / "tokens.txt").exists():
        return d
    base = d.parent
    if base.is_dir():
        for p in sorted(base.iterdir()):
            if p.is_dir() and (p / "tokens.txt").exists():
                return p
    return d


def _pick_provider() -> str:
    """选推理后端 —— **探测后决定**，而不是硬编码。

    ★★ 2026-09-30 修正（Eli 问"是不是没做设备自识别"）。

    原来的实现是一句：
        return "coreml" if sys.platform == "darwin" else "cpu"
    也就是说 **Windows/Linux 一律走 CPU，完全没有自识别** ——
    他的机器有 RTX 4070，三个 sherpa 引擎却全在 CPU 上跑。

    当时的理由是"CPU 已经 35–43×，不需要显卡"（见 jobs._build_engine 的注释）。
    现在改掉，因为那个理由站不住了：

      ① **体积代价其实很小**：那 ~2 GB CUDA 运行库本来就要为 Whisper 装，
         本机早就有了；sherpa 的 CUDA wheel 只比 CPU 版多 86 MB（200 vs 104 MB）。
         "为了省体积而不用 GPU"在装了 Whisper 的机器上根本不成立。
      ② **"够快"不该是不加速的理由**：长音频的绝对时间差是实打实的
         （实测数据见 data/cache/_gpu_bench.py 的输出）。
      ③ 打包分发时才需要考虑体积，那是**另一个决策**（可以给用户按需下载），
         不该让它悄悄决定了开发机上的默认行为。

    ★★ 但必须**真探测**，不能只看"机器有显卡"：
      sherpa-onnx 在 provider 不可用时会**静默回落 CPU**，
      只在 stderr 打一行。只看硬件会得出"用了 GPU"的错误结论 ——
      那比不用 GPU 更糟，因为它让人以为问题已经解决了。
      `probe_sherpa_gpu()` 检查真实 DLL 能否加载，毫秒级，且能给出失败原因。

    ★ 环境变量仍可强制指定，用于排查/压测对比：
        SOUNDSCRIBE_PROVIDER=cpu | cuda | coreml | directml

    ★★ 2026-10-01 按 Eli 的要求定案：**兼容性优先，不追求最后那一下。**
      这个软件要同时跑 Windows 和 Mac、要跑在网页端、最后封包上传 GitHub，
      所以"每个平台都用最稳妥的后端"远比"榨出最后一点速度"重要。
    """
    forced = os.environ.get("SOUNDSCRIBE_PROVIDER", "").strip()
    if forced:
        return forced

    # ★ macOS：直接走 CPU，**不猜 CoreML**。
    #   CoreML 的 provider 没有独立的库文件可以检查，"这一版有没有带"无法从文件系统判断；
    #   猜错的后果是静默回落（声称用 GPU 其实没有），比不用更糟。
    #   Apple Silicon 的 CPU 本身就很快，不值得冒这个险。
    if sys.platform == "darwin":
        return "cpu"

    try:
        from app.doctor import env as doctor

        ok, _why = doctor.probe_sherpa_gpu()
        return "cuda" if ok else "cpu"
    except Exception:  # noqa: BLE001
        return "cpu"                     # 探测本身失败 → 保守走 CPU，绝不冒进


def _read_frames_float(w, sr: int, ch: int, start: float, end: float):
    """按需 seek 读一小段并转 float。

    ★ 不要一次读完整个文件再转：2 小时 16k 音频约 1.15 亿样本，
      float32 数组本身要 447 MB，而 `[v/32768.0 for v in a]` 这种列表推导
      会额外造出约 3.5 GB 中间对象（每个 Python float 约 32 字节），直接爆内存。
      这里用 map 惰性迭代，并且**始终只持有当前这一段**（28 秒 ≈ 1.8 MB）。
    """
    import array

    w.setpos(min(max(0, int(start * sr)), w.getnframes()))
    raw = w.readframes(max(1, int((end - start) * sr)))
    a = array.array("h")
    a.frombytes(raw)
    if sys.byteorder == "big":
        a.byteswap()
    if ch > 1:
        a = array.array("h",
                        (sum(a[i:i + ch]) // ch for i in range(0, len(a) - ch + 1, ch)))
    return array.array("f", map(lambda v: v / 32768.0, a))


def _enforce_max_chunk(chunks: list[tuple[float, float]], max_s: float) -> list[tuple[float, float]]:
    """兜底：把超过 max_s 的段再等分。

    plan_chunks 合并过短尾块时理论上可能产出略超上限的段。
    对 SenseVoice 来说超限不是「慢一点」而是**静默掉字**，
    所以宁可多一道看似冗余的保险 —— 这类错误在结果里根本看不出来。
    """
    out: list[tuple[float, float]] = []
    for s, e in chunks:
        span = e - s
        if span <= max_s:
            out.append((s, e))
            continue
        n = int(span / max_s) + 1
        step = span / n
        for k in range(n):
            out.append((s + k * step, e if k == n - 1 else s + (k + 1) * step))
    return out


def _tokens_to_words(res, chunk_start: float, chunk_end: float) -> list[dict]:
    """把 sherpa-onnx 的逐字时间戳转成字级 (start, end)。

    ★ 这是迁移后新获得的能力：funasr 完全不提供时间戳，
      方案里「词级时间轴」那一档在 SenseVoice 上原本是做不到的。
    """
    try:
        toks = list(res.tokens or [])
        tss = list(res.timestamps or [])
    except Exception:  # noqa: BLE001
        return []
    if not toks or len(toks) != len(tss):
        return []
    out: list[dict] = []
    for i, (tk, ts) in enumerate(zip(toks, tss)):
        nxt = tss[i + 1] if i + 1 < len(tss) else (chunk_end - chunk_start)
        out.append({
            "w": tk,
            "s": round(chunk_start + float(ts), 2),
            "e": round(chunk_start + float(max(ts, nxt)), 2),
        })
    return out


class _SherpaChunkedEngine:
    """sherpa-onnx 的「静音切块 + 逐块解码」通用实现。子类只需实现 _build_recognizer。"""

    name = ""
    display = ""
    model_rel = ""          # 相对 ROOT 的模型目录（与 registry 里的 dir 一致）
    max_chunk = SHERPA_MAX_CHUNK
    use_itn = False         # 只有 SenseVoice 有逆文本规整

    def __init__(self, model_dir: Path | None = None,
                 num_threads: int | None = None,
                 provider: str | None = None,
                 chunk_target: float | None = None,
                 **_ignored):
        caps = doctor.engine_caps()
        if not caps.get("sensevoice_ok"):
            raise EngineError(caps.get("sensevoice_error") or "sherpa-onnx 不可用")

        # ★★ 不静默吞参。
        #   原来的写法是 `**_ignored` 一收到底 —— 参数名打错、或某个能力矩阵声明了
        #   却传到了这台引擎上，都会**悄无声息地消失**，不报错也没有痕迹。
        #   实测踩过：用户设了热词、界面也写着生效，实际一个字节都没进引擎。
        #   现在记下来，并在上游/核验工具里据此告警。
        #   （Parakeet 在它自己的 __init__ 里已把 hotwords 收走，不会流到这里。）
        self.ignored_kwargs: list[str] = sorted(_ignored)
        if self.ignored_kwargs:
            print(f"[engines] {type(self).__name__} 忽略了未识别的构造参数："
                  f"{self.ignored_kwargs}", flush=True)

        self.model_dir = Path(model_dir) if model_dir else sherpa_model_dir(self.model_rel)
        # ★ 无论上层传什么，都不能超过硬上限
        self.chunk_target = max(5.0, min(float(chunk_target or SHERPA_TARGET_CHUNK),
                                        self.max_chunk - 3))
        self.num_threads = int(num_threads or min(SHERPA_MAX_THREADS, os.cpu_count() or 4))
        self.provider = provider or _pick_provider()
        # ★ 「我要的」和「实际生效的」必须分开记 —— 这是两件事。
        #   sherpa 不告诉我们它最终落在哪个 provider 上，而把"我传了 cuda"
        #   当成"跑在 cuda"正是本项目一直在治的那类错误（看起来生效）。
        #   所以用探测结果反推真实后端，并如实报给上层。
        self.provider_requested = self.provider
        self.provider_effective = self._effective_provider(self.provider)
        self.device = self.provider_effective
        self._m = None
        self.load_seconds = 0.0
        self.last_info: dict = {}

    @staticmethod
    def _effective_provider(requested: str) -> str:
        """由请求的 provider 反推**实际生效**的 provider。

        ★ 只有 CUDA（Windows/Linux）是**可探测**的 —— 它有独立的 provider DLL，
          文件在不在、能不能加载都能查。
        ★ 其余后端（coreml / directml）**无法可靠探测**，所以一律如实记 cpu。
          "不知道有没有生效"就写 cpu，比写一个没验证过的值诚实 ——
          接口名对不上时静默丢掉东西、provider 不可用时静默回落，
          这类"看起来生效"的问题已经在这个项目里反复出现过太多次了。
        """
        if requested == "cpu":
            return "cpu"
        if requested == "cuda" and sys.platform != "darwin":
            try:
                from app.doctor import env as doctor

                ok, _why = doctor.probe_sherpa_gpu()
                return "cuda" if ok else "cpu"
            except Exception:  # noqa: BLE001
                return "cpu"
        return "cpu"

    # ---------- 子类实现 ----------
    def _build_recognizer(self, d: Path, **kw):
        raise NotImplementedError

    def _missing(self, d: Path) -> list[str]:
        """返回缺失的文件名列表；空列表表示齐全。"""
        from app.doctor import registry as _reg

        entry = next((m for m in _reg.all_models() if m["engine"] == self.name), None)
        need = (entry or {}).get("required") or ["tokens.txt"]
        return [n for n in need if not (d / n).exists()]

    # ---------- 公共流程 ----------
    def load(self) -> float:
        d = self.model_dir
        miss = self._missing(d)
        if miss:
            raise EngineError(
                f"{self.display} 模型文件不完整（{d}）：缺少 {', '.join(miss)}。"
                "请在「模型中心」重新下载。")
        t0 = time.perf_counter()
        self._m = self._build_recognizer(d, num_threads=self.num_threads,
                                         provider=self.provider,
                                         use_itn=self.use_itn)
        self.load_seconds = time.perf_counter() - t0
        return self.load_seconds

    def stream(self, audio: Path, *, language: str = "auto", **_kw):
        if self._m is None:
            self.load()

        # ★ 未知推理参数同样不静默吞掉（理由见 __init__）
        ignored = sorted(_kw)
        if ignored:
            print(f"[engines] {type(self).__name__}.stream 忽略了未识别参数：{ignored}",
                  flush=True)

        total = audio_duration(audio)
        silences = detect_silences(audio)
        chunks = plan_chunks(total, silences, target=self.chunk_target,
                             hard_max=self.max_chunk)
        chunks = _enforce_max_chunk(chunks, self.max_chunk)
        # ★ 合并而不是整体替换：热词等装载期证据是在 _build_recognizer 里写进 last_info 的，
        #   直接赋值会把它们冲掉 → 运行时核验读不到证据，等于白做。
        self.last_info = {
            **self.last_info,
            "language": language, "duration": total,
            # ★ device 记的是**实际生效**的后端；请求值单独留一份，便于核对
            "device": self.provider_effective,
            "provider_requested": self.provider_requested,
            "provider_effective": self.provider_effective,
            "chunks": len(chunks), "chunked": True,
            "chunk_max_seconds": self.max_chunk, "num_threads": self.num_threads,
            "engine_impl": "sherpa-onnx", "engine": self.name,
            "ignored_kwargs": self.ignored_kwargs + ignored,
        }

        import wave

        with wave.open(str(audio), "rb") as w:
            sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            if width != 2:
                raise EngineError(
                    f"音频不是 16-bit PCM（{width*8} bit）。上游抽音轨环节应产出 pcm_s16le。")
            for i, (s, e) in enumerate(chunks, 1):
                samples = _read_frames_float(w, sr, ch, s, e)
                if len(samples) < sr * 0.2:
                    continue
                st = self._m.create_stream()
                st.accept_waveform(sr, samples)
                self._m.decode_stream(st)
                res = st.result
                text = (res.text or "").strip()
                if text:
                    yield Segment(
                        index=i, start=s, end=e, text=text,
                        # 恒为 0：sherpa-onnx 此构建 ys_log_probs 为空，拿不到置信度
                        # → 界面「低置信标注」自然停用（不是 bug，是能力变化）
                        avg_logprob=0.0, no_speech_prob=0.0,
                        words=_tokens_to_words(res, s, e),
                    )


class SenseVoiceEngine(_SherpaChunkedEngine):
    name = "sensevoice"
    display = "SenseVoice-Small（ONNX）"
    model_rel = "models/sherpa/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
    use_itn = True

    def _build_recognizer(self, d: Path, **kw):
        import sherpa_onnx

        model = None
        for cand in ("model.int8.onnx", "model.onnx", "model.fp16.onnx"):
            if (d / cand).exists():
                model = d / cand
                break
        if model is None:
            model = next(iter(sorted(d.glob("*.onnx"))), None)
        if model is None:
            raise EngineError(f"找不到 SenseVoice 模型文件（{d}）")
        return sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model), tokens=str(d / "tokens.txt"),
            num_threads=kw["num_threads"], use_itn=kw["use_itn"],
            provider=kw["provider"], debug=False)


class ParakeetEngine(_SherpaChunkedEngine):
    """NVIDIA Parakeet TDT —— 英文专用。

    实测（CPU 8 线程、25 秒切段）：25.0× 实时，1 小时录音 2.4 分钟，
    词级质量与 Whisper-turbo 99.2% 一致。
    ⚠️ 只认英文：不认中文、不做翻译。

    ★ 支持**原生热词**（NeMo transducer 的 hotwords 加权）。三个硬约束都来自实测：
        · 必须换成 `modified_beam_search` 解码 —— 用 greedy 会直接抛
          "Please use --decoding-method=modified_beam_search when using --hotwords-file"
        · 热词是**装载期**参数（不是推理期），所以必须开一份新的 recognizer
        · ★★ 还必须同时给出 `bpe_vocab` + `modeling_unit="bpe"` ——
          否则整词查不到 ID，**全部热词被静默跳过**。见 _write_bpe_vocab 的说明。
    """

    name = "parakeet"
    display = "Parakeet TDT 0.6B（英文专用）"
    model_rel = "models/sherpa/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"

    def __init__(self, *a, hotwords: str | list[str] | None = None, **kw):
        # hotwords 可以是路径，也可以是词列表/多行文本
        self._hotwords_spec = hotwords
        self._hotwords_path: Path | None = None
        self._bpe_vocab_path: Path | None = None
        self._pieces: set[str] = set()
        self._kw_kept = 0
        self._kw_dropped: list[str] = []
        super().__init__(*a, **kw)

    def _prepare_hotwords(self, d: Path) -> Path | None:
        """把热词落成一个文件 —— sherpa 的 hotwords_file 只认路径。

        放在 data/cache/ 下并**按内容命名**：同一批热词复用同一个文件，
        不同批次不会互相覆盖（多任务并行时这点很重要）。

        ★ 写之前先用模型自己的子词表判一遍可达性，不可达的直接剔除并计数 ——
          剔除数量会写进 last_info，由上层如实告诉用户。
        """
        # 先把模型的子词表读出来（判可达性 + 后面合成 BPE 词表都要用）
        self._pieces = _load_bpe_pieces(d / "tokens.txt")
        self._bpe_vocab_path = _write_bpe_vocab(
            d / "tokens.txt", ROOT / "data" / "cache" / "hotwords")

        spec = self._hotwords_spec
        if not spec:
            return None
        if isinstance(spec, str) and Path(spec).is_file():
            return Path(spec)
        words = ([w.strip() for w in spec.splitlines()] if isinstance(spec, str)
                 else [str(w).strip() for w in spec])
        words = [w for w in words if w]
        if not words:
            return None

        kept, dropped = [], []
        for w in words:
            (kept if bpe_split_ok(w, self._pieces) else dropped).append(w)
        self._kw_kept = len(kept)
        self._kw_dropped = dropped
        if not kept:
            return None

        import hashlib

        cache = ROOT / "data" / "cache" / "hotwords"
        cache.mkdir(parents=True, exist_ok=True)
        body = "\n".join(kept) + "\n"
        name = hashlib.md5(body.encode("utf-8")).hexdigest()[:12]
        p = cache / f"en_{name}.txt"          # 英文热词（Parakeet 只认英文）
        if not p.exists():
            p.write_text(body, encoding="utf-8")
        return p

    def _build_recognizer(self, d: Path, **kw):
        import sherpa_onnx

        hw = self._prepare_hotwords(d)
        extra: dict = {}
        if hw:
            self._hotwords_path = hw
            extra = {
                "hotwords_file": str(hw),
                "hotwords_score": 1.5,
                # ★ 实测：greedy_search 下带 hotwords_file 会直接报错
                "decoding_method": "modified_beam_search",
            }
            if self._bpe_vocab_path:
                # ★★ 没有这两个，上面那行 hotwords_file 等于白给：
                #    整词查不到 ID → 全部被静默跳过（见 _write_bpe_vocab）
                extra["bpe_vocab"] = str(self._bpe_vocab_path)
                extra["modeling_unit"] = "bpe"

        # 证据键：告诉上层"实际用上了几个、剔除几个"，而不是"我传了几个"
        self.last_info["hotwords"] = self._kw_kept
        self.last_info["hotwords_dropped"] = len(self._kw_dropped)
        if self._kw_dropped:
            self.last_info["hotwords_dropped_words"] = self._kw_dropped[:10]
        self.last_info["bpe_vocab"] = bool(self._bpe_vocab_path)

        return sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(d / "encoder.int8.onnx"),
            decoder=str(d / "decoder.int8.onnx"),
            joiner=str(d / "joiner.int8.onnx"),
            tokens=str(d / "tokens.txt"),
            num_threads=kw["num_threads"], provider=kw["provider"],
            model_type="nemo_transducer", debug=False, **extra)


class MoonshineEngine(_SherpaChunkedEngine):
    """Moonshine base.en —— 英文专用、体积最小（274 MB）。

    实测（CPU 8 线程、25 秒切段）：23.4× 实时，词级质量与 Whisper 99.3% 一致。
    ⚠️ 只认英文；标点比 Whisper 略少（逗号 57 vs 78）。
    """

    name = "moonshine"
    display = "Moonshine base.en（英文 · 极小）"
    model_rel = "models/sherpa/sherpa-onnx-moonshine-base-en-int8"

    def _build_recognizer(self, d: Path, **kw):
        import sherpa_onnx

        # ★ Moonshine(v1) 是四段式，且文件名不是 decode* 而是 cached/uncached_decode
        return sherpa_onnx.OfflineRecognizer.from_moonshine(
            preprocessor=str(d / "preprocess.onnx"),
            encoder=str(d / "encode.int8.onnx"),
            uncached_decoder=str(d / "uncached_decode.int8.onnx"),
            cached_decoder=str(d / "cached_decode.int8.onnx"),
            tokens=str(d / "tokens.txt"),
            num_threads=kw["num_threads"], provider=kw["provider"], debug=False)


# ═══════════════════════════════════════════════════════════

ENGINES = {
    "whisper": WhisperEngine,
    "sensevoice": SenseVoiceEngine,
    "parakeet": ParakeetEngine,
    "moonshine": MoonshineEngine,
}


def build(engine: str, **kw):
    if engine not in ENGINES:
        raise EngineError(f"未知引擎：{engine}")
    return ENGINES[engine](**kw)


if __name__ == "__main__":
    import json

    print(json.dumps(doctor.engine_caps(), ensure_ascii=False, indent=2))
