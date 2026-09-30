"""把 engines.py 里的 SenseVoice 引擎从 funasr+torch 换成 sherpa-onnx。

★ 迁移要点：
  1. 保持 stream() 接口不变 → jobs.py / postprocess / exporter 全都不用改
  2. 保留「静音切块」方案（它提供时间戳），但**单段必须 < 30 秒**
  3. 不再需要 ffmpeg 逐块切片：直接在内存里按 chunk 边界 seek 读取
     （原来 122 分钟要调 245 次 ffmpeg，现在 0 次）
  4. avg_logprob 拿不到了（ys_log_probs 在此构建为空）→ 置 0，低置信标记自然停用
  5. 新增逐字时间戳（sherpa-onnx 提供），为将来的「词级时间轴」预留
"""
from pathlib import Path

P = Path(r'C:/Users/Eli/WorkBuddy/workbuddy/soundscribe/app/pipeline/engines.py')
src = P.read_text(encoding='utf-8')

start = src.index('# ═══════════════════════════════════════════════════════════\n# SenseVoice 引擎（FunASR）')
end = src.index('# ═══════════════════════════════════════════════════════════\n\nENGINES = {')

NEW = '''# ═══════════════════════════════════════════════════════════
# SenseVoice 引擎（sherpa-onnx · 纯 ONNX Runtime）
#
# ★ 2026-09-30 从 funasr + torch 迁移过来。实测收益：
#     · 依赖        3.6 GB → 104 MB（去掉 torch / ctranslate2 / CUDA 库 / scipy 等）
#     · 模型        901 MB → 230 MB（int8 量化）
#     · CPU 速度    25.8–27.3× → 35–43×
#     · 质量        122 分钟真实录音字符级 98.1%，术语命中几乎全同
#     · 跨平台      同一个包可在 Windows / macOS / Linux（含 arm64）—— Mac 不再要重建引擎
#
# ★★ 一条硬约束，必须写死在代码里：单段必须 < 30 秒
#     SenseVoice 是自注意力结构，整段长音频喂进去注意力开销近似平方增长。
#     实测 8 分钟：整段喂 7.2× 且**掉字**；切 30 秒 38.4×。
#     **最阴险的是它不报错、不崩溃**，只是把「老师喜欢什么」悄悄变成「老师欢什么」。
#     所以这里：目标段长 25 秒、硬上限 28 秒，并在切块后**再做一次强制兜底拆分**。
#
# ★ 能力变化（诚实记录）：
#     · 失去：avg_logprob（sherpa-onnx 此构建的 ys_log_probs 为空）→ 界面上「低置信标注」不再出现
#     · 得到：逐字时间戳 timestamps → 将来可实现「词级时间轴」（原方案里 SenseVoice 做不到）
#     · 得到：lang / emotion / event 标签（当前未使用，lang 可用于语言一致性校验）
# ═══════════════════════════════════════════════════════════

SENSEVOICE_TARGET_CHUNK = 25.0     # 目标段长（秒）
SENSEVOICE_MAX_CHUNK = 28.0        # 硬上限，必须 < 30；再大就会掉字
SENSEVOICE_MAX_THREADS = 8         # ★ 实测最优：8 线程 43× > 16 线程 35× > 24 线程 20×（过度订阅）


def default_sensevoice_dir() -> Path:
    """定位 SenseVoice 的 ONNX 模型目录。

    优先精确目录，其次扫描 models/sherpa/ 下任何含 tokens.txt + *model*.onnx 的子目录
    —— 这样换模型版本或手工放模型进来都不用改代码。
    """
    base = ROOT / "models" / "sherpa"
    exact = base / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
    if (exact / "tokens.txt").exists():
        return exact
    if base.is_dir():
        for p in sorted(base.iterdir()):
            if p.is_dir() and (p / "tokens.txt").exists() and next(p.glob("*model*.onnx"), None):
                return p
    return exact


def _pick_provider() -> str:
    """选择推理后端。

    Windows / Linux → cpu。SenseVoice 在 CPU 上已 35–43×，1 小时录音 84–103 秒，不需要显卡。
    macOS → 先试 coreml（框架层的 Metal 通道）。sherpa-onnx 在不支持时会**自动回落到 cpu**
    并在日志里写明，所以这是安全的乐观尝试 —— 前提是将来真在 Mac 上验一次。
    可用环境变量强制指定：SOUNDSCRIBE_PROVIDER=cpu|cuda|coreml|directml
    """
    import os

    forced = os.environ.get("SOUNDSCRIBE_PROVIDER", "").strip()
    if forced:
        return forced
    if sys.platform == "darwin":
        return "coreml"
    return "cpu"


def _read_frames_float(w, sr: int, ch: int, start: float, end: float):
    """按需 seek 读取一小段并转成 float 数组。

    ★ 为什么不一次性读完整个文件：
      2 小时 16k 音频约 1.15 亿样本。float32 数组本身要 447 MB；
      而 `[v/32768.0 for v in a]` 这种列表推导会额外造出**约 3.5 GB** 的中间对象
      （每个 Python float 约 32 字节），直接把内存打爆。
      这里用 map 惰性迭代，并且**始终只持有当前这一段**（28 秒 ≈ 1.8 MB）。
    """
    import array

    first = max(0, int(start * sr))
    n = max(1, int((end - start) * sr))
    w.setpos(min(first, w.getnframes()))
    raw = w.readframes(n)
    a = array.array("h")
    a.frombytes(raw)
    if sys.byteorder == "big":              # WAV 是小端
        a.byteswap()
    if ch > 1:                              # 多声道降混
        a = array.array("h",
                        (sum(a[i:i + ch]) // ch for i in range(0, len(a) - ch + 1, ch)))
    return array.array("f", map(lambda v: v / 32768.0, a))


def _enforce_max_chunk(chunks: list[tuple[float, float]], max_s: float) -> list[tuple[float, float]]:
    """兜底：把任何超过 max_s 的段再等分拆开。

    plan_chunks 在合并过短尾块时，理论上可能产出略超上限的段。
    对 SenseVoice 来说超限不是「慢一点」，而是**静默掉字**，
    所以这里宁可多一道看似冗余的保险 —— 这类错误在结果里根本看不出来。
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
            sub_s = s + k * step
            sub_e = e if k == n - 1 else s + (k + 1) * step
            out.append((sub_s, sub_e))
    return out


class SenseVoiceEngine:
    name = "sensevoice"
    display = "SenseVoice-Small（ONNX）"

    def __init__(self, device: str | None = None,
                 chunk_target: float = SENSEVOICE_TARGET_CHUNK,
                 model_dir: Path | None = None,
                 num_threads: int | None = None,
                 provider: str | None = None):
        caps = doctor.engine_caps()
        if not caps.get("sensevoice_ok"):
            raise EngineError(caps.get("sensevoice_error") or "SenseVoice 引擎不可用")

        self.model_dir = Path(model_dir) if model_dir else default_sensevoice_dir()
        # ★ 无论上层传什么，都不能超过硬上限
        self.chunk_target = max(5.0, min(float(chunk_target), SENSEVOICE_MAX_CHUNK - 3))
        self.num_threads = int(num_threads or min(SENSEVOICE_MAX_THREADS, os.cpu_count() or 4))
        self.provider = provider or _pick_provider()
        # SenseVoice 在 CPU 上已经足够快，不占用显存（把显卡留给 Whisper）
        self.device = self.provider
        self._m = None
        self.load_seconds = 0.0
        self.last_info: dict = {}

    # ---------- 加载 ----------
    def _paths(self) -> tuple[Path, Path]:
        d = self.model_dir
        model = None
        for cand in ("model.int8.onnx", "model.onnx", "model.fp16.onnx"):
            if (d / cand).exists():
                model = d / cand
                break
        if model is None:
            model = next(iter(sorted(d.glob("*model*.onnx"))), None)
        tokens = d / "tokens.txt"
        if model is None or not model.exists():
            raise EngineError(f"找不到 SenseVoice 模型文件（{d}）。请在模型中心重新下载。")
        if not tokens.exists():
            raise EngineError(f"找不到 tokens.txt（{d}）。请在模型中心重新下载。")
        return model, tokens

    def load(self) -> float:
        import sherpa_onnx

        model, tokens = self._paths()
        t0 = time.perf_counter()
        self._m = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model), tokens=str(tokens),
            num_threads=self.num_threads,
            use_itn=True,                 # 逆文本规整：数字、标点更像人写的
            provider=self.provider,
            debug=False,
        )
        self.load_seconds = time.perf_counter() - t0
        return self.load_seconds

    # ---------- 转写 ----------
    def stream(self, audio: Path, *, language: str = "auto", use_itn: bool = True,
               **_) -> Iterator[Segment]:
        if self._m is None:
            self.load()

        total = audio_duration(audio)
        silences = detect_silences(audio)
        chunks = plan_chunks(total, silences, target=self.chunk_target,
                             hard_max=SENSEVOICE_MAX_CHUNK)
        chunks = _enforce_max_chunk(chunks, SENSEVOICE_MAX_CHUNK)
        self.last_info = {
            "language": "auto",
            "duration": total,
            "device": self.provider,
            "chunks": len(chunks),
            "chunked": True,
            "chunk_max_seconds": SENSEVOICE_MAX_CHUNK,
            "num_threads": self.num_threads,
            "engine_impl": "sherpa-onnx",
        }

        import wave

        with wave.open(str(audio), "rb") as w:
            sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            if width != 2:
                raise EngineError(
                    f"音频不是 16-bit PCM（{width*8} bit）。上游抽音轨环节应产出 pcm_s16le。")
            for i, (s, e) in enumerate(chunks, 1):
                samples = _read_frames_float(w, sr, ch, s, e)
                if len(samples) < sr * 0.2:        # 短于 0.2 秒的碎片直接跳过
                    continue
                st = self._m.create_stream()
                st.accept_waveform(sr, samples)
                self._m.decode_stream(st)
                res = st.result
                text = (res.text or "").strip()
                if text:
                    yield Segment(
                        index=i, start=s, end=e, text=text,
                        # avg_logprob 恒为 0：sherpa-onnx 此构建的 ys_log_probs 为空，
                        # 拿不到置信度 → 界面的「低置信标注」自然停用（不是 bug，是能力变化）
                        avg_logprob=0.0,
                        no_speech_prob=0.0,
                        words=_tokens_to_words(res, s, e),
                    )


def _tokens_to_words(res, chunk_start: float, chunk_end: float) -> list[dict]:
    """把 sherpa-onnx 的逐字时间戳转成字级 (start, end)。

    ★ 这是迁移后新获得的能力：原来 funasr 完全不提供时间戳，
      方案里「词级时间轴」那一档在 SenseVoice 上是做不到的。
      现在可以了 —— 但先只把数据带出来，等做时间轴功能时再用。
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


'''

src = src[:start] + NEW + src[end:]

# Segment 增加 words 字段（默认空，不影响现有消费方）
src = src.replace(
    '''    text: str
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "text": self.text,
            "avg_logprob": round(self.avg_logprob, 3),
            "no_speech_prob": round(self.no_speech_prob, 3),
        }''',
    '''    text: str
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
        return d''')
src = src.replace('from dataclasses import dataclass',
                  'from dataclasses import dataclass, field')
src = src.replace('import re\nimport shutil', 'import os\nimport re\nimport shutil')

# 更新文件头的说明
src = src.replace(
    '''  SenseVoiceEngine   FunASR SenseVoiceSmall —— 原生不支持流式，
                     用「静音检测切块」实现近似逐句产出，并顺带补上时间戳

★ SenseVoice 的两个实测特性（M1 结论）：
  1. 不提供逐句时间戳 → 用静音切块后按块边界给时间，SRT 才可用
  2. 在纯英文音频上会「语言漂移」（整段输出中文）→ 上层必须做语言一致性校验''',
    '''  SenseVoiceEngine   sherpa-onnx / ONNX Runtime（2026-09-30 起）—— 原生不支持流式，
                     用「静音检测切块」实现近似逐句产出，并顺带补上时间戳

★ SenseVoice 的实测特性：
  1. 不提供**句级**时间戳 → 用静音切块后按块边界给时间，SRT 才可用
     （但 sherpa-onnx 提供**字级**时间戳 timestamps，见 _tokens_to_words）
  2. 在纯英文音频上会「语言漂移」（整段输出中文）→ 上层必须做语言一致性校验
  3. ★★ 单段必须 < 30 秒，否则又慢又掉字且不报错 —— 见 SENSEVOICE_MAX_CHUNK''')

P.write_text(src, encoding='utf-8')
print('engines.py 已重写')
print('  新文件行数:', len(src.splitlines()))
print('  含 funasr 引用:', src.count('funasr'))
print('  含 torch 引用:', src.count('torch'))
print('  含 sensevoice 相关常量:', all(k in src for k in
      ['SENSEVOICE_MAX_CHUNK', '_enforce_max_chunk', '_read_frames_float', 'default_sensevoice_dir']))
