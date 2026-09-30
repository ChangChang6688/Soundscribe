"""声文 · 英文识别模型对照（找 Mac 上跑得动的方案）

背景：Mac 上 CTranslate2 不支持 Metal，Whisper 只能跑 CPU（约 4×，1 小时要 13–15 分钟）。
      而 SenseVoice 中文最好、英文一般。所以"Mac 上的英文"需要一个**本身就在 CPU 上够快**的模型。

本脚本在同一段英文素材上依次跑多个模型，输出：
  · 装载耗时 / 识别耗时 / 实时倍速 / 模型体积
  · 完整文本（供逐词对照）

⚠️ 重要前提：本机是 Windows x86。**Mac 是 arm64，绝对速度会不同**，
   但同架构内部的**相对快慢**通常可以迁移（同一套 ONNX Runtime CPU 内核）。
   Mac 上的真机数据必须另测。

用法：
  python 11_en_model_compare.py --file data/en-audio-16k.wav [--threads 8] [--chunk 25]
"""

from __future__ import annotations

import argparse
import json
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHERPA_DIR = ROOT / "models" / "sherpa"


# ────────────── 音频读取（只依赖标准库 + sherpa-onnx 自带的波形输入）──────────────

def read_chunk(w, sr: int, ch: int, start: float, end: float):
    import array
    import sys

    w.setpos(min(max(0, int(start * sr)), w.getnframes()))
    raw = w.readframes(max(1, int((end - start) * sr)))
    a = array.array("h")
    a.frombytes(raw)
    if sys.byteorder == "big":
        a.byteswap()
    if ch > 1:
        a = array.array("h", (sum(a[i:i + ch]) // ch
                              for i in range(0, len(a) - ch + 1, ch)))
    return array.array("f", map(lambda v: v / 32768.0, a))


def duration_of(p: Path) -> float:
    with wave.open(str(p), "rb") as w:
        return w.getnframes() / w.getframerate()


# ────────────── 各类型模型的装载 ──────────────

def build(model_dir: Path, threads: int, provider: str = "cpu"):
    """按目录里存在的文件判断模型类型并装载。"""
    import sherpa_onnx

    names = {f.name for f in model_dir.glob("*.onnx")}

    if {"encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx"} <= names:
        # NeMo Parakeet 等 transducer 结构
        return sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(model_dir / "encoder.int8.onnx"),
            decoder=str(model_dir / "decoder.int8.onnx"),
            joiner=str(model_dir / "joiner.int8.onnx"),
            tokens=str(model_dir / "tokens.txt"),
            num_threads=threads, provider=provider, model_type="nemo_transducer",
        ), "transducer(parakeet)"

    if (model_dir / "preprocess.onnx").exists() or any(n.startswith("encode") for n in names):
        # Moonshine（v1）是四段式：preprocess / encode / uncached_decode / cached_decode
        # ★ 注意文件名不是统一的 "decode"，而是 cached_decode / uncached_decode ——
        #   按 "decode*" 去 glob 会一个都匹配不到。
        def pick(*pats):
            for pat in pats:
                hits = [f for f in sorted(model_dir.glob(pat)) if "int8" in f.name]
                if hits:
                    return hits[0]
            for pat in pats:
                hits = sorted(model_dir.glob(pat))
                if hits:
                    return hits[0]
            raise RuntimeError(f"缺少模型文件：{pats}")

        return sherpa_onnx.OfflineRecognizer.from_moonshine(
            preprocessor=str(pick("preprocess*.onnx")),
            encoder=str(pick("encode*.onnx", "encoder*.onnx")),
            uncached_decoder=str(pick("uncached_decode*.onnx")),
            cached_decoder=str(pick("cached_decode*.onnx")),
            tokens=str(model_dir / "tokens.txt"),
            num_threads=threads, provider=provider,
        ), "moonshine"

    if any("encoder" in n for n in names) and any("decoder" in n for n in names):
        enc = next(sorted(model_dir.glob("*encoder*.onnx")))
        dec = next(sorted(model_dir.glob("*decoder*.onnx")))
        return sherpa_onnx.OfflineRecognizer.from_whisper(
            encoder=str(enc), decoder=str(dec),
            tokens=str(model_dir / "tokens.txt"),
            num_threads=threads, language="en", task="transcribe",
            decoding_method="greedy_search", provider=provider,
        ), "whisper"

    raise RuntimeError(f"认不出模型类型：{sorted(names)}")


def dir_mb(p: Path) -> float:
    return round(sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1024**2, 1)


# ────────────── 主流程 ──────────────

CANDIDATES = [
    ("whisper-large-v3-turbo", "Whisper large-v3-turbo（现用，ONNX int8）"),
    ("sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8", "NVIDIA Parakeet TDT 0.6B v2（英文专用）"),
    ("sherpa-onnx-moonshine-base-en-int8", "Moonshine base.en（边缘设备向）"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="data/en-audio-16k.wav")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--chunk", type=float, default=25.0)
    ap.add_argument("--only", default="")
    a = ap.parse_args()

    target = Path(a.file)
    if not target.is_absolute():
        target = ROOT / target
    dur = duration_of(target)

    print("=" * 74)
    print("  英文识别模型对照（找 Mac 上跑得动的方案）")
    print("=" * 74)
    print(f"  素材 : {target.name}  {dur:.1f} s（{dur/60:.2f} 分钟）")
    print(f"  线程 : {a.threads}   切段: {a.chunk:.0f} 秒")
    print(f"  ★ 本机是 Windows x86；Mac 是 arm64，绝对速度会不同，")
    print(f"    但同架构内的相对快慢通常可迁移。Mac 真机数据需另测。")
    print()

    results = []
    for slug, label in CANDIDATES:
        if a.only and a.only not in slug:
            continue
        d = SHERPA_DIR / slug
        if not d.exists():
            print(f"  ── {label}\n     （未下载，跳过）")
            continue

        size = dir_mb(d)
        try:
            t0 = time.perf_counter()
            rec, kind = build(d, a.threads)
            load_s = time.perf_counter() - t0
        except Exception as exc:  # noqa: BLE001
            print(f"  ── {label}\n     装载失败：{type(exc).__name__}: {str(exc)[:110]}")
            continue

        parts = []
        n = 0
        with wave.open(str(target), "rb") as w:
            sr, ch = w.getframerate(), w.getnchannels()
            t0 = time.perf_counter()
            step = a.chunk
            pos = 0.0
            while pos < dur:
                end = min(pos + step, dur)
                samples = read_chunk(w, sr, ch, pos, end)
                if len(samples) > sr * 0.3:
                    st = rec.create_stream()
                    st.accept_waveform(sr, samples)
                    rec.decode_stream(st)
                    txt = (st.result.text or "").strip()
                    if txt:
                        parts.append(txt)
                        n += 1
                pos = end
            elapsed = time.perf_counter() - t0

        text = " ".join(parts)
        speed = dur / elapsed if elapsed else 0
        print(f"  ── {label}")
        print(f"     体积 {size:7.0f} MB | 类型 {kind:<18} | 装载 {load_s:5.2f} s")
        print(f"     识别 {elapsed:7.1f} s | 倍速 {speed:6.1f}× | "
              f"1 小时折算 {3600/speed:6.0f} 秒（{3600/speed/60:.1f} 分钟）")
        print(f"     词数 {len(text.split())}   片段 {n}")
        print(f"     前 200 字符：{text[:200]}")
        print()
        results.append({
            "slug": slug, "label": label, "kind": kind, "size_mb": size,
            "load_seconds": round(load_s, 2), "seconds": round(elapsed, 2),
            "speed_x": round(speed, 2), "words": len(text.split()),
            "minutes_per_hour": round(3600 / speed / 60, 1) if speed else None,
            "full_text": text,
        })
        (ROOT / "m1" / f"en_{slug}.txt").write_text(text, encoding="utf-8")

    out = ROOT / "m1" / "en_model_compare.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写入 {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
