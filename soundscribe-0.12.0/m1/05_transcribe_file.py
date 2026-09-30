"""M1.5 · 完整文件转写并导出交付件（TXT / SRT / VTT / JSON）

用法：
  python 05_transcribe_file.py <音频或视频> --outdir <目录> [选项]

选项：
  --device cuda|cpu         默认 cuda
  --compute-type float16    默认 float16
  --batch 16                批处理批大小（>0 时自动绑定 VAD）
  --vad                     启用 VAD（批处理时强制开启）
  --language zh|en          指定语言；不给则自动检测
  --initial-prompt "..."    热词/上下文提示（提升领域术语准确率）
  --limit 180               只处理前 N 秒（快速抽样）
  --name-prefix xxx         输出文件名前缀

产出：<outdir>/<prefix>.txt / .srt / .vtt / .json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
HERE = Path(__file__).resolve().parent

sys.path.insert(0, str(HERE))
import sb_env  # noqa: E402,F401


def fmt_ts(seconds: float, sep: str = ",") -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", sep)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", type=Path)
    ap.add_argument("--outdir", type=Path, required=True)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute-type", default="float16")
    ap.add_argument("--batch", type=int, default=0)
    ap.add_argument("--vad", action="store_true")
    ap.add_argument("--language", default=None)
    ap.add_argument("--initial-prompt", default=None)
    ap.add_argument("--limit", type=float, default=0)
    ap.add_argument("--beam-size", type=int, default=5)
    ap.add_argument("--name-prefix", default=None)
    ap.add_argument("--engine-label", default="whisper-turbo")
    args = ap.parse_args()

    src = args.audio
    if not src.is_absolute():
        src = (HERE / src).resolve()
    if not src.exists():
        raise SystemExit(f"[错误] 找不到输入：{src}")

    outdir = args.outdir if args.outdir.is_absolute() else (HERE / args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    prefix = args.name_prefix or src.stem

    # 批处理必须搭配 VAD —— 引擎硬约束，代码里绑定
    use_vad = args.vad or args.batch > 0
    if args.batch > 0 and not args.vad:
        print("[注意] 批处理必须搭配 VAD，已自动开启 vad_filter=True")

    from faster_whisper import WhisperModel, BatchedInferencePipeline

    lock = MODELS_DIR / "last_download.txt"
    model_ref = lock.read_text(encoding="utf-8").strip() if lock.exists() else "large-v3-turbo"

    print("=" * 66)
    print("声文 · 完整转写")
    print("=" * 66)
    print(f"  输入     {src.name}")
    print(f"  输出目录 {outdir}")
    print(f"  模型     {model_ref}")
    print(f"  设备     {args.device} / {args.compute_type}")
    print(f"  批处理   {args.batch if args.batch > 0 else '关闭'}")
    print(f"  VAD      {'开启' if use_vad else '关闭'}")

    t0 = time.perf_counter()
    model = WhisperModel(model_ref, device=args.device, compute_type=args.compute_type)
    t_load = time.perf_counter() - t0

    kwargs: dict = dict(
        beam_size=args.beam_size,
        vad_filter=use_vad,
        language=args.language,
        word_timestamps=False,
    )
    if args.language is None:
        kwargs.pop("language")
    if args.initial_prompt:
        kwargs["initial_prompt"] = args.initial_prompt

    audio_arg = str(src)
    tmp_cut = None
    if args.limit > 0:
        import shutil
        import subprocess
        import tempfile

        ff = shutil.which("ffmpeg")
        tmp_cut = Path(tempfile.gettempdir()) / f"sb_cut_{int(args.limit)}s.wav"
        subprocess.run(
            [ff, "-y", "-v", "error", "-t", str(args.limit), "-i", str(src),
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tmp_cut)],
            check=True, timeout=300,
        )
        audio_arg = str(tmp_cut)
        print(f"  [抽样] 仅处理前 {args.limit:.0f} 秒")

    print(f"\n[加载] {t_load:.2f}s")
    print("[转写中] ...", flush=True)

    t0 = time.perf_counter()
    if args.batch > 0:
        pipe = BatchedInferencePipeline(model=model)
        segments, info = pipe.transcribe(audio_arg, batch_size=args.batch, **kwargs)
    else:
        segments, info = model.transcribe(audio_arg, **kwargs)

    rows = []
    first_at = None
    for seg in segments:
        if first_at is None:
            first_at = time.perf_counter() - t0
        rows.append({
            "id": len(rows) + 1,
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text.strip(),
            "avg_logprob": round(getattr(seg, "avg_logprob", 0) or 0, 4),
            "no_speech_prob": round(getattr(seg, "no_speech_prob", 0) or 0, 4),
        })
        if len(rows) % 200 == 0:
            print(f"  已转写 {len(rows)} 段 ...", flush=True)
    t_total = time.perf_counter() - t0

    audio_sec = info.duration or 0
    speed = audio_sec / t_total if t_total else 0

    # ---- 导出 ----
    full_text = "".join(r["text"] for r in rows)
    (outdir / f"{prefix}.txt").write_text(full_text, encoding="utf-8")

    with (outdir / f"{prefix}.srt").open("w", encoding="utf-8") as f:
        for i, r in enumerate(rows, 1):
            f.write(f"{i}\n{fmt_ts(r['start'])} --> {fmt_ts(r['end'])}\n{r['text']}\n\n")

    with (outdir / f"{prefix}.vtt").open("w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for r in rows:
            f.write(f"{fmt_ts(r['start'], '.')} --> {fmt_ts(r['end'], '.')}\n{r['text']}\n\n")

    report = {
        "input": src.name,
        "engine": args.engine_label,
        "model": model_ref,
        "device": args.device,
        "compute_type": args.compute_type,
        "batch": args.batch,
        "vad": use_vad,
        "language_arg": args.language,
        "detected_language": getattr(info, "language", None),
        "language_probability": round(getattr(info, "language_probability", 0) or 0, 4),
        "initial_prompt": args.initial_prompt,
        "audio_seconds": round(audio_sec, 2),
        "segments": len(rows),
        "load_seconds": round(t_load, 2),
        "transcribe_seconds": round(t_total, 2),
        "first_segment_seconds": round(first_at, 3) if first_at else None,
        "speed_x": round(speed, 2),
        "minutes_per_hour_audio": round(3600 / speed / 60, 2) if speed else None,
        "chars": len(full_text),
        "segments_detail": rows,
    }
    (outdir / f"{prefix}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n[完成] 转写 {t_total:.2f}s | {len(rows)} 段 | {speed:.1f}× | 音频 {audio_sec/60:.2f} 分钟")
    print(f"[语言] {report['detected_language']}（置信 {report['language_probability']}）")
    print(f"[字符] {len(full_text)} 字")
    print(f"[产出] {prefix}.txt / .srt / .vtt / .json  →  {outdir}")

    if tmp_cut and tmp_cut.exists():
        tmp_cut.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
