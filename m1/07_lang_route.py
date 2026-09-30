"""M1.5 · 语言路由判定器（方案 §6.6 的可执行实现）

作用：在整段转写之前，先判断素材该走哪个引擎，避免用错引擎白跑一遍。

做法：
  1. 从音频里取前 N 秒（默认 60s，跳过开头的静音/杂音段）
  2. 用最快配置跑一次试转写
  3. 统计识别结果里的中文字符占比
  4. 按阈值给出路由建议

阈值（来自 2026-09-18 真实素材验证）：
  CJK 占比 > 85%         → SenseVoice（中文主力，术语更准、CPU 快 7.8 倍）
  40% – 85%             → Whisper（中英夹杂，Whisper 术语更稳）
  < 40%                 → Whisper（英文，Whisper 明显更准）

用法：
  python 07_lang_route.py <音频> [--sample 60] [--engine whisper-turbo|whisper-base]

注意：判定器只做「建议」，界面上始终允许用户手动覆盖。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
HERE = Path(__file__).resolve().parent

sys.path.insert(0, str(HERE))
import sb_env  # noqa: E402,F401

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
LATIN_WORD = re.compile(r"[A-Za-z]+")

THRESHOLD_CN = 0.85
THRESHOLD_MIX = 0.40


def take_sample(src: Path, seconds: float) -> Path:
    ff = shutil.which("ffmpeg")
    if not ff:
        raise SystemExit("[错误] 需要 ffmpeg")
    dst = Path(tempfile.gettempdir()) / f"sb_route_{int(seconds)}s.wav"
    subprocess.run(
        [ff, "-y", "-v", "error", "-ss", "30", "-t", str(seconds), "-i", str(src),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)],
        check=True, timeout=180,
    )
    return dst


def transcribe_sample(audio: Path, engine: str) -> tuple[str, float, str]:
    from faster_whisper import WhisperModel

    lock = MODELS_DIR / "last_download.txt"
    if engine == "whisper-base":
        model_ref = "base"
    else:
        model_ref = lock.read_text(encoding="utf-8").strip() if lock.exists() else "large-v3-turbo"

    device, compute = ("cuda", "float16") if _cuda_ok() else ("cpu", "int8")
    t0 = time.perf_counter()
    model = WhisperModel(model_ref, device=device, compute_type=compute,
                         download_root=str(MODELS_DIR))
    segments, info = model.transcribe(str(audio), vad_filter=True, beam_size=1)
    text = "".join(s.text for s in segments)
    elapsed = time.perf_counter() - t0
    return text, elapsed, device


def _cuda_ok() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        return False


def ratio(text: str) -> dict:
    """统计中文占比。

    ★ 实现坑（已踩过）：不要先把「标点 + 空白」一起删掉再数英文词。
    那样会把所有英文单词粘成一整坨字母，re[A-Za-z]+ 只会匹配到 1 个。
    正确做法是直接在原文上分别计数——标点不会凭空造出单词。
    """
    cjk = len(CJK.findall(text))
    latin_words = LATIN_WORD.findall(text)
    latin_chars = sum(len(w) for w in latin_words)
    # 中文按「字」计、英文按「词」计，统一成可比的中文当量
    units = cjk + len(latin_words)
    r = (cjk / units) if units else 0.0
    return {
        "cjk_chars": cjk,
        "latin_words": len(latin_words),
        "latin_chars": latin_chars,
        "units": units,
        "cjk_ratio": round(r, 4),
    }


def route(r: float) -> tuple[str, str]:
    if r > THRESHOLD_CN:
        return "sensevoice", "中文为主 → SenseVoice（中文术语更准、CPU 快 7.8 倍）"
    if r >= THRESHOLD_MIX:
        return "whisper", "中英夹杂 → Whisper（英文术语更稳）"
    return "whisper", "英文为主 → Whisper（实测明显更准）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", type=Path)
    ap.add_argument("--sample", type=float, default=60.0)
    ap.add_argument("--engine", default="whisper-turbo")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    src = args.audio
    if not src.is_absolute():
        src = (HERE / src).resolve()
    if not src.exists():
        raise SystemExit(f"[错误] 找不到输入：{src}")

    sample = take_sample(src, args.sample)
    text, elapsed, device = transcribe_sample(sample, args.engine)
    stats = ratio(text)
    engine, reason = route(stats["cjk_ratio"])

    result = {
        "input": src.name,
        "sample_seconds": args.sample,
        "route_engine": engine,
        "reason": reason,
        "probe_seconds": round(elapsed, 2),
        "probe_device": device,
        "thresholds": {"sensevoice_above": THRESHOLD_CN, "whisper_below": THRESHOLD_MIX},
        **stats,
        "sample_text": text[:400],
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("=" * 62)
        print("声文 · 语言路由判定")
        print("=" * 62)
        print(f"  素材        {src.name}")
        print(f"  采样        {args.sample:.0f}s（从第 30 秒起）")
        print(f"  试转写      {elapsed:.2f}s（{device}）")
        print(f"  中文字符    {stats['cjk_chars']}")
        print(f"  英文词      {stats['latin_words']}")
        print(f"  中文占比    {stats['cjk_ratio']*100:.1f}%")
        print()
        print(f"  → 建议引擎  {engine.upper()}")
        print(f"  → 理由      {reason}")
        print()
        print(f"  [样本文本] {text[:220]}")

    (HERE / f"route_{src.stem}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    sample.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
