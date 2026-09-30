"""声文 · GGUF/ONNX 免 torch 路线验证

目的：验证 sherpa-onnx（纯 ONNX Runtime、无 PyTorch）能否替代当前
      funasr + torch 的 SenseVoice 方案，做到
        ① 识别质量不下降  ② 速度不下降  ③ 依赖体积大幅缩小

用法：
  python 09_onnx_verify.py --model <模型目录> [--file <音频>] [--threads N]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build(model_dir: Path, threads: int = 8, itn: bool = True):
    import sherpa_onnx

    t0 = time.perf_counter()
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model_dir / "model.int8.onnx"),
        tokens=str(model_dir / "tokens.txt"),
        num_threads=threads,
        use_itn=itn,          # 打开逆文本规整：数字、标点会更像人写的
        debug=False,
    )
    return rec, time.perf_counter() - t0


def read_wav_pcm16(path: Path):
    """读 16-bit PCM WAV → (采样率, 单声道 float 数组)。

    ★ 刻意只用标准库（wave + array）：
      sherpa-onnx 没有 numpy 依赖，这条验证要证明的是"整条链都能不带 torch/numpy"。
      多声道会现场降混。
    """
    import array
    import sys
    import wave

    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"只支持 16-bit PCM WAV，实际 {w.getsampwidth()*8} bit")
        sr = w.getframerate()
        ch = w.getnchannels()
        n = w.getnframes()
        raw = w.readframes(n)

    a = array.array("h")
    a.frombytes(raw)
    if sys.byteorder == "big":          # WAV 是小端
        a.byteswap()
    if ch > 1:                          # 降混
        a = array.array("h", (sum(a[i:i + ch]) // ch for i in range(0, len(a) - ch + 1, ch)))
    f = array.array("f", map(lambda v: v / 32768.0, a))
    return sr, f


def run_one(rec, wav: Path) -> tuple[str, float, float]:
    """返回 (文本, 解码耗时, 读文件耗时)。"""
    t_read = time.perf_counter()
    sr, samples = read_wav_pcm16(wav)
    read_s = time.perf_counter() - t_read

    t0 = time.perf_counter()
    s = rec.create_stream()
    s.accept_waveform(sr, samples)
    rec.decode_stream(s)
    return s.result.text, time.perf_counter() - t0, read_s


def run_chunked(rec, wav: Path, chunk_s: float) -> tuple[str, float, float, int]:
    """按固定窗口切段后逐段识别。

    ★ 为什么必须切：SenseVoice 是自注意力结构，整段 8 分钟喂进去
      注意力开销近似平方增长 → 又慢又掉字。funasr 那条链本来就用
      VAD 把单段限在 30 秒内，这里为了对照也切段。
    """
    t_read = time.perf_counter()
    sr, samples = read_wav_pcm16(wav)
    read_s = time.perf_counter() - t_read

    step = int(chunk_s * sr)
    parts: list[str] = []
    n_chunk = 0
    t0 = time.perf_counter()
    for i in range(0, len(samples), step):
        piece = samples[i:i + step]
        if len(piece) < sr * 0.3:          # 尾巴太短就并入上一段
            if parts:
                continue
        n_chunk += 1
        st = rec.create_stream()
        st.accept_waveform(sr, piece)
        rec.decode_stream(st)
        if st.result.text:
            parts.append(st.result.text)
    return "".join(parts), time.perf_counter() - t0, read_s, n_chunk


def duration_of(wav: Path) -> float:
    """只读 WAV 头，用标准库即可 —— 不引第三方依赖。"""
    import wave

    with wave.open(str(wav), "rb") as w:
        return w.getnframes() / w.getframerate()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--file", default="")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--itn", type=int, default=1)
    ap.add_argument("--chunk", type=float, default=0.0,
                    help="切成多少秒一段再识别；0 = 整段一次喂（不推荐，长音频会掉字+变慢）")
    a = ap.parse_args()

    model_dir = Path(a.model)
    if not (model_dir / "model.int8.onnx").exists():
        print(f"[错误] 找不到模型：{model_dir / 'model.int8.onnx'}")
        return 1

    print("=" * 62)
    print("  sherpa-onnx（纯 ONNX，无 torch）验证")
    print("=" * 62)

    import sherpa_onnx

    print(f"  sherpa-onnx 版本 : {sherpa_onnx.__version__}")
    print(f"  线程数           : {a.threads}")
    print(f"  ITN（数字/标点规整）: {'开' if a.itn else '关'}")
    rec, load_s = build(model_dir, a.threads, bool(a.itn))
    print(f"  模型加载耗时     : {load_s:.2f} s")
    print()

    results: dict = {"sherpa_onnx_version": sherpa_onnx.__version__,
                     "threads": a.threads, "load_seconds": round(load_s, 2)}

    # ── 测试 1：短音频，核对内容 ──
    short = ROOT / "m1" / "asr_example_zh.wav"
    ref_file = ROOT / "m1" / "asr_example_zh.ref.txt"
    if short.exists():
        text, sec, read_s = run_one(rec, short)
        dur = duration_of(short)
        ref = ref_file.read_text(encoding="utf-8").strip() if ref_file.exists() else ""
        print("── 测试 1：短音频内容核对 ──")
        print(f"  音频时长 : {dur:.2f} s")
        print(f"  读文件   : {read_s:.2f} s")
        print(f"  识别耗时 : {sec:.2f} s")
        print(f"  识别结果 : {text}")
        if ref:
            same = text.replace(" ", "") == ref.replace(" ", "")
            print(f"  参考答案 : {ref}")
            print(f"  是否一致 : {'✓ 完全一致' if same else '✗ 不一致'}")
        results["short"] = {"text": text, "ref": ref, "seconds": round(sec, 2),
                            "read_seconds": round(read_s, 2),
                            "audio_seconds": round(dur, 2)}
        print()

    # ── 测试 2：指定文件（测速度）──
    target = Path(a.file) if a.file else (ROOT / "data" / "seg-40-48min.wav")
    if target.exists():
        dur = duration_of(target)
        print(f"── 测试 2：长音频测速（{target.name}）──")
        print(f"  音频时长 : {dur:.1f} s（{dur/60:.2f} 分钟）")
        if a.chunk > 0:
            text, sec, read_s, n_chunk = run_chunked(rec, target, a.chunk)
            print(f"  切段     : {a.chunk:.0f} 秒/段，共 {n_chunk} 段")
        else:
            text, sec, read_s = run_one(rec, target)
            n_chunk = 1
        speed = dur / sec if sec else 0
        print(f"  读文件   : {read_s:.2f} s")
        print(f"  识别耗时 : {sec:.2f} s")
        print(f"  实时倍速 : {speed:.1f}×")
        print(f"  1 小时折算: {3600/speed:.1f} 秒" if speed else "")
        print(f"  字符数   : {len(text)}")
        print()
        print(f"  前 300 字：{text[:300]}")
        results["long"] = {
            "file": target.name, "chunk_seconds": a.chunk, "chunks": n_chunk,
            "audio_seconds": round(dur, 2),
            "seconds": round(sec, 2), "read_seconds": round(read_s, 2),
            "speed_x": round(speed, 2),
            "chars": len(text), "text_head": text[:600], "full_text": text,
        }

    if results.get("long", {}).get("text_head"):
        pass
    if "long" in results and results["long"].get("full_text"):
        (ROOT / "m1" / f"onnx_{results['long']['file']}.txt").write_text(
            results["long"]["full_text"], encoding="utf-8")

    out = ROOT / "m1" / "onnx_verify_result.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"结果已写入：{out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
