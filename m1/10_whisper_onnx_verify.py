"""声文 · Whisper ONNX（sherpa-onnx）验证

背景：现有方案里 Whisper 走 faster-whisper + CTranslate2。
      这条链的问题有两个：① 在 Mac 上没有 GPU（CTranslate2 不支持 Metal）
                        ② 需要 CUDA 库（2 GB）才能在这台机器上用 GPU
      而 CPU 上它只有 3.3× 实时（1 小时录音要 18 分钟），是"无显卡机器不可用"的那一半。

本次验证：换用 sherpa-onnx 的 Whisper（纯 ONNX Runtime）
  · CPU 上能到多少倍速？是否足以让"没显卡的机器"也能用？
  · 识别质量与 CTranslate2 版差多少？
  · 依赖体积省多少？

用法：
  python 10_whisper_onnx_verify.py --model <目录> --file <音频> [--threads 8] [--chunk 30]
"""

from __future__ import annotations

import argparse
import json
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_wav_pcm16(path: Path):
    import array
    import sys

    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("只支持 16-bit PCM WAV")
        sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        raw = w.readframes(n)
    a = array.array("h")
    a.frombytes(raw)
    if sys.byteorder == "big":
        a.byteswap()
    if ch > 1:
        a = array.array("h", (sum(a[i:i + ch]) // ch for i in range(0, len(a) - ch + 1, ch)))
    return sr, array.array("f", map(lambda v: v / 32768.0, a))


def duration_of(wav: Path) -> float:
    with wave.open(str(wav), "rb") as w:
        return w.getnframes() / w.getframerate()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--file", default="data/en-audio-16k.wav")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--chunk", type=float, default=30.0)
    ap.add_argument("--language", default="en")
    a = ap.parse_args()

    import sherpa_onnx

    md = Path(a.model)
    enc = next(md.glob("*encoder*.onnx"), None)
    dec = next(md.glob("*decoder*.onnx"), None)
    tok = next(md.glob("*tokens*.txt"), None)
    if not (enc and dec and tok):
        print(f"[错误] 模型目录缺少 encoder/decoder/tokens：{md}")
        return 1

    print("=" * 62)
    print("  sherpa-onnx Whisper（纯 ONNX，无 CTranslate2 / 无 torch）")
    print("=" * 62)
    print(f"  encoder : {enc.name}  ({enc.stat().st_size/1048576:.0f} MB)")
    print(f"  decoder : {dec.name}  ({dec.stat().st_size/1048576:.0f} MB)")
    print(f"  线程数  : {a.threads}   切段: {a.chunk:.0f} 秒   语言: {a.language}")

    t0 = time.perf_counter()
    rec = sherpa_onnx.OfflineRecognizer.from_whisper(
        encoder=str(enc), decoder=str(dec), tokens=str(tok),
        num_threads=a.threads, language=a.language, task="transcribe",
        decoding_method="greedy_search", provider="cpu",
    )
    load_s = time.perf_counter() - t0
    print(f"  加载耗时: {load_s:.2f} s")
    print()

    target = Path(a.file)
    if not target.is_absolute():
        target = ROOT / target
    dur = duration_of(target)
    sr, samples = read_wav_pcm16(target)

    step = int(a.chunk * sr)
    parts: list[str] = []
    n = 0
    t0 = time.perf_counter()
    for i in range(0, len(samples), step):
        piece = samples[i:i + step]
        if len(piece) < sr * 0.5 and parts:
            break
        n += 1
        st = rec.create_stream()
        st.accept_waveform(sr, piece)
        rec.decode_stream(st)
        if st.result.text.strip():
            parts.append(st.result.text.strip())
    elapsed = time.perf_counter() - t0
    text = " ".join(parts)

    speed = dur / elapsed if elapsed else 0
    print("── 结果 ──")
    print(f"  音频     : {target.name}  {dur:.1f} s（{dur/60:.2f} 分钟）")
    print(f"  切段     : {a.chunk:.0f} 秒/段，共 {n} 段")
    print(f"  识别耗时 : {elapsed:.2f} s")
    print(f"  实时倍速 : {speed:.1f}×")
    print(f"  1 小时折算: {3600/speed:.1f} 秒（{3600/speed/60:.1f} 分钟）" if speed else "")
    print(f"  词数     : {len(text.split())}")
    print()
    print(f"  前 300 字：{text[:300]}")

    out = ROOT / "m1" / "whisper_onnx_result.json"
    out.write_text(json.dumps({
        "model": md.name, "file": target.name, "threads": a.threads,
        "chunk_seconds": a.chunk, "chunks": n,
        "audio_seconds": round(dur, 2), "seconds": round(elapsed, 2),
        "speed_x": round(speed, 2), "words": len(text.split()),
        "load_seconds": round(load_s, 2), "full_text": text,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "m1" / f"onnx_{target.stem}.whisper.txt").write_text(text, encoding="utf-8")
    print()
    print(f"结果已写入：{out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
