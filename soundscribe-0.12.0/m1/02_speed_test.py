"""M1 · 转写测速脚本

目的：用真机实测替换方案 §9「性能预估」里所有推算数字。

用法示例：
  # GPU（A 档）
  python 02_speed_test.py long-zh-test.wav --device cuda --compute-type float16

  # CPU（D/E 档对照）
  python 02_speed_test.py long-zh-test.wav --device cpu --compute-type int8

  # 批处理加速
  python 02_speed_test.py long-zh-test.wav --device cuda --compute-type float16 --batch 16

  # 先切 60 秒快速验证
  python 02_speed_test.py long-zh-test.wav --limit 60 --device cuda --compute-type float16

输出：控制台摘要 + speed_report_<device>_<compute>.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
HERE = Path(__file__).resolve().parent

# 必须先引导 CUDA DLL 路径，再导入 faster_whisper（见 sb_env 模块说明）
sys.path.insert(0, str(HERE))
import sb_env  # noqa: E402,F401


def sh(cmd: list[str], timeout: int = 30) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()


def gpu_mem() -> dict | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = sh([exe, "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"])
        used, total = (int(x.strip()) for x in out.split(","))
        return {"used_mib": used, "total_mib": total, "free_mib": total - used}
    except Exception:  # noqa: BLE001
        return None


def duration(path: Path) -> float:
    exe = shutil.which("ffprobe")
    if not exe:
        return 0.0
    try:
        return float(
            sh(
                [
                    exe, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ]
            )
        )
    except Exception:  # noqa: BLE001
        return 0.0


def cut(src: Path, seconds: float) -> Path:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise SystemExit("[错误] 需要 ffmpeg 才能切割音频")
    dst = Path(tempfile.gettempdir()) / f"sb_cut_{int(seconds)}s_{src.stem}.wav"
    sh(
        [
            exe, "-y", "-v", "error",
            "-t", str(seconds),
            "-i", str(src),
            "-c", "copy", str(dst),
        ],
        timeout=120,
    )
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", type=Path)
    ap.add_argument("--model", default=None, help="模型名或本地目录；默认读 last_download.txt")
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu", "auto"])
    ap.add_argument("--compute-type", default="float16")
    ap.add_argument("--beam-size", type=int, default=5)
    ap.add_argument("--batch", type=int, default=0, help=">0 启用批处理推理")
    ap.add_argument("--vad", action="store_true", help="启用 VAD 静音过滤")
    ap.add_argument("--language", default=None, help="zh / en；不传则自动检测")
    ap.add_argument("--limit", type=float, default=0, help="只处理前 N 秒")
    ap.add_argument("--label", default="", help="报告后缀标记")
    args = ap.parse_args()

    src = args.audio
    if not src.is_absolute():
        src = (HERE / src).resolve()
    if not src.exists():
        raise SystemExit(f"[错误] 找不到音频：{src}")

    if args.limit > 0:
        src = cut(src, args.limit)
        print(f"[切割] 仅处理前 {args.limit:.0f} 秒 -> {src.name}")

    total_dur = duration(src)

    # 解析模型路径
    model_ref = args.model
    if model_ref is None:
        lock = MODELS_DIR / "last_download.txt"
        if lock.exists():
            model_ref = lock.read_text(encoding="utf-8").strip()
        else:
            model_ref = "large-v3-turbo"
    if model_ref and Path(model_ref).exists():
        model_ref = str(Path(model_ref))

    from faster_whisper import WhisperModel

    print("=" * 66)
    print("声文 M1 · 转写测速")
    print("=" * 66)
    print(f"  音频        {src.name}")
    print(f"  时长        {total_dur:.1f}s ({total_dur/60:.2f} 分钟)")
    print(f"  模型        {model_ref}")
    print(f"  设备        {args.device}")
    print(f"  量化        {args.compute_type}")
    print(f"  beam        {args.beam_size}")
    print(f"  批处理      {args.batch if args.batch > 0 else '关闭'}")
    print(f"  VAD         {'开启' if args.vad else '关闭'}")
    print(f"  语言        {args.language or '自动检测'}")

    mem_before = gpu_mem()
    if mem_before:
        print(f"  显存(前)    {mem_before['used_mib']} / {mem_before['total_mib']} MiB")

    # ---- 加载模型 ----
    t0 = time.perf_counter()
    model = WhisperModel(
        model_ref,
        device=args.device,
        compute_type=args.compute_type,
        download_root=str(MODELS_DIR),
    )
    t_load = time.perf_counter() - t0

    mem_after_load = gpu_mem()
    print(f"\n[加载] {t_load:.2f}s")
    if mem_before and mem_after_load:
        print(f"[显存] 加载后 {mem_after_load['used_mib']} MiB（模型占用约 {mem_after_load['used_mib']-mem_before['used_mib']} MiB）")

    # ---- 转写 ----
    kwargs = dict(
        beam_size=args.beam_size,
        vad_filter=args.vad,
        language=args.language,
        word_timestamps=False,
    )
    if args.vad:
        kwargs["vad_parameters"] = dict(min_silence_duration_ms=500)

    t0 = time.perf_counter()
    if args.batch > 0:
        from faster_whisper import BatchedInferencePipeline

        pipe = BatchedInferencePipeline(model=model)
        segments, info = pipe.transcribe(str(src), batch_size=args.batch, **kwargs)
    else:
        segments, info = model.transcribe(str(src), **kwargs)

    first_seg_at = None
    texts: list[str] = []
    n_seg = 0
    for seg in segments:
        if first_seg_at is None:
            first_seg_at = time.perf_counter() - t0
        n_seg += 1
        texts.append(seg.text.strip())
    t_total = time.perf_counter() - t0

    # ---- 结果 ----
    audio_sec = total_dur if total_dur > 0 else (info.duration or 0)
    rtf = t_total / audio_sec if audio_sec else 0
    speed = audio_sec / t_total if t_total else 0

    mem_peak = gpu_mem()

    print(f"\n[转写] 总耗时 {t_total:.2f}s")
    print(f"[转写] 段数   {n_seg}")
    print(f"[转写] 首段延迟 {first_seg_at:.2f}s" if first_seg_at else "")
    print(f"[性能] 实时倍速 {speed:.1f}×   RTF {rtf:.4f}")
    if speed:
        # 注意单位：3600/speed 得到的是「秒」，换算成分钟要再除以 60
        print(f"[性能] 折算 1 小时录音约 {3600/speed/60:.2f} 分钟（{3600/speed:.1f} 秒）")
    if mem_peak:
        print(f"[显存] 峰值 {mem_peak['used_mib']} MiB")

    print("\n[转写文本 前 3 段]")
    for line in texts[:3]:
        print(f"   {line[:78]}")
    if len(texts) > 6:
        print("   ...")
        print("[末 2 段]")
        for line in texts[-2:]:
            print(f"   {line[:78]}")

    report = {
        "audio": src.name,
        "audio_seconds": round(audio_sec, 2),
        "model": model_ref,
        "device": args.device,
        "compute_type": args.compute_type,
        "beam_size": args.beam_size,
        "batch": args.batch,
        "vad": args.vad,
        "language_arg": args.language,
        "detected_language": getattr(info, "language", None),
        "language_probability": round(getattr(info, "language_probability", 0) or 0, 4),
        "segments": n_seg,
        "load_seconds": round(t_load, 2),
        "transcribe_seconds": round(t_total, 2),
        "first_segment_seconds": round(first_seg_at, 2) if first_seg_at else None,
        "realtime_factor": round(rtf, 4),
        "speed_x": round(speed, 2),
        "seconds_per_hour_audio": round(3600 / speed, 1) if speed else None,
        "minutes_per_hour_audio": round(3600 / speed / 60, 2) if speed else None,
        "vram": {"before": mem_before, "after_load": mem_after_load, "after_run": mem_peak},
        "text_preview": texts[:5],
    }
    tag = f"{args.device}_{args.compute_type}" + (f"_b{args.batch}" if args.batch else "")
    if args.vad:
        tag += "_vad"
    if args.label:
        tag += f"_{args.label}"
    out = HERE / f"speed_report_{tag}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[落盘] {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
