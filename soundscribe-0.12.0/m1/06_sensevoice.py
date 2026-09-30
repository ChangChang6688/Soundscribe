"""M1.5 · SenseVoice（FunASR）转写 —— 与 Whisper 做同源对比

用法：
  python 06_sensevoice.py <音频> --outdir <目录> [--limit 180] [--device cuda:0]

产出：与 05_transcribe_file.py 相同格式的 .txt / .srt / .json，便于逐段比对。

★ 实测注意事项
  - SenseVoice 输出自带 <|zh|><|NEUTRAL|><|Speech|> 等富文本标记，必须用
    funasr.utils.postprocess_utils.rich_transcription_postprocess 清洗，
    否则文本里会混入标记。
  - SenseVoice 不提供逐句时间戳（segment-level timestamps）时，
    SRT 只能按整体时长粗略均分；若需精确时间轴要用 Paraformer 或 Whisper。
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

os.environ.setdefault("MODELSCOPE_CACHE", str(MODELS_DIR / "modelscope"))
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


def fmt_ts(seconds: float, sep: str = ",") -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", sep)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", type=Path)
    ap.add_argument("--outdir", type=Path, required=True)
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--limit", type=float, default=0)
    ap.add_argument("--language", default="auto", help="auto/zh/en/yue/ja/ko")
    ap.add_argument("--use-itn", action="store_true", default=True)
    ap.add_argument("--no-itn", dest="use_itn", action="store_false")
    ap.add_argument("--name-prefix", default=None)
    ap.add_argument("--hub", default="ms", choices=["ms", "hf"], help="模型来源：ms=ModelScope")
    ap.add_argument("--batch-size-s", type=int, default=300, help="动态批处理的秒数预算")
    args = ap.parse_args()

    src = args.audio
    if not src.is_absolute():
        src = (HERE / src).resolve()
    if not src.exists():
        raise SystemExit(f"[错误] 找不到输入：{src}")

    outdir = args.outdir if args.outdir.is_absolute() else (HERE / args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    prefix = args.name_prefix or f"{src.stem}-sensevoice"

    import shutil
    import subprocess
    import tempfile

    audio_arg = str(src)
    tmp_cut = None
    if args.limit > 0:
        ff = shutil.which("ffmpeg")
        tmp_cut = Path(tempfile.gettempdir()) / f"sb_sv_cut_{int(args.limit)}s.wav"
        subprocess.run(
            [ff, "-y", "-v", "error", "-t", str(args.limit), "-i", str(src),
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tmp_cut)],
            check=True, timeout=300,
        )
        audio_arg = str(tmp_cut)

    print("=" * 66)
    print("声文 · SenseVoice 转写")
    print("=" * 66)
    print(f"  输入   {Path(audio_arg).name}")
    print(f"  设备   {args.device}")
    print(f"  语言   {args.language}")
    print(f"  ITN    {args.use_itn}")
    print(f"  缓存   {os.environ['MODELSCOPE_CACHE']}")

    from funasr import AutoModel

    t0 = time.perf_counter()
    model = AutoModel(
        model="iic/SenseVoiceSmall",
        trust_remote_code=True,
        vad_model="fsmn-vad",
        vad_kwargs={"max_single_segment_time": 30000},
        device=args.device,
        disable_update=True,
        hub=args.hub,
    )
    t_load = time.perf_counter() - t0
    print(f"\n[加载] {t_load:.2f}s")
    print("[转写中] ...", flush=True)

    t0 = time.perf_counter()
    res = model.generate(
        input=audio_arg,
        cache={},
        language=args.language,
        use_itn=args.use_itn,
        batch_size_s=args.batch_size_s,
        merge_vad=True,
        merge_length_s=15,
    )
    t_total = time.perf_counter() - t0

    raw = res[0]["text"] if res else ""
    try:
        from funasr.utils.postprocess_utils import rich_transcription_postprocess

        text = rich_transcription_postprocess(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"[警告] 富文本后处理不可用（{exc}），使用原始输出")
        text = raw

    # SenseVoice 长音频返回的是整段文本（可能带 <|...|> 切分标记）
    print(f"\n[完成] 转写 {t_total:.2f}s | {len(text)} 字")
    print(f"[预览] {text[:160]}...")

    (outdir / f"{prefix}.txt").write_text(text, encoding="utf-8")

    # 无逐句时间戳 → 按 <|...|> 切分标记拆句，时间轴粗略均分（仅作占位）
    marks = [p for p in raw.split("<|") if p.strip()]
    pieces = [p.split("|>")[-1].strip() for p in marks if p.split("|>")[-1].strip()]
    if not pieces:
        pieces = [text]

    total = args.limit if args.limit > 0 else 0
    if total <= 0:
        import shutil as _sh

        ffprobe = _sh.which("ffprobe")
        if ffprobe:
            try:
                total = float(subprocess.run(
                    [ffprobe, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
                    capture_output=True, text=True, timeout=30).stdout.strip())
            except Exception:  # noqa: BLE001
                total = 0

    span = total / len(pieces) if pieces and total else 0
    with (outdir / f"{prefix}.srt").open("w", encoding="utf-8") as f:
        for i, p in enumerate(pieces, 1):
            f.write(f"{i}\n{fmt_ts((i-1)*span)} --> {fmt_ts(i*span)}\n{p}\n\n")

    speed = (total / t_total) if t_total and total else 0
    report = {
        "input": src.name,
        "engine": "sensevoice-small",
        "device": args.device,
        "language_arg": args.language,
        "use_itn": args.use_itn,
        "audio_seconds": round(total, 2),
        "load_seconds": round(t_load, 2),
        "transcribe_seconds": round(t_total, 2),
        "speed_x": round(speed, 2),
        "minutes_per_hour_audio": round(3600 / speed / 60, 2) if speed else None,
        "chars": len(text),
        "pieces": len(pieces),
        "text": text,
        "raw_text": raw,
        "note": "SenseVoice 无逐句时间戳，SRT 时间轴为均分占位，仅供对照阅读",
    }
    (outdir / f"{prefix}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n[产出] {prefix}.txt / .srt / .json  →  {outdir}")
    if speed:
        print(f"[性能] {speed:.1f}× | 1 小时录音约 {3600/speed/60:.2f} 分钟")

    if tmp_cut and tmp_cut.exists():
        tmp_cut.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
