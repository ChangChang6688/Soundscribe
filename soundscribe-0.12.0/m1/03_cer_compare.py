"""M1 · 字错率（CER）评测

目的：验证方案 §2.1 的核心论断——中文场景下 Whisper 与国产引擎的差距到底有多大。
但注意：M1 的公开示例音频只能做「管线正确性」验证；
     真实中文会议录音的准确率对比需要 Eli 本人提供素材。

CER = (替换 + 删除 + 插入) / 参考文本字数
标点与空白在计算前先剔除（与 FunASR 官方口径一致）。

用法：
  python 03_cer_compare.py m1/asr_example_zh.wav --ref-file m1/asr_example_zh.ref.txt --device cuda --compute-type float16
"""

from __future__ import annotations

import argparse
import json
import re
import string
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
HERE = Path(__file__).resolve().parent

# 必须先引导 CUDA DLL 路径，再导入 faster_whisper（见 sb_env 模块说明）
sys.path.insert(0, str(HERE))
import sb_env  # noqa: E402,F401

PUNCT = set(string.punctuation) | set("，。！？、；：""''（）《》〈〉【】…—～·「」『』,.!?;:\"'()[]{}<>-")


def normalize(text: str) -> str:
    """去标点、去空白、统一全角半角、英文小写 —— 对齐 CER 计算口径。"""
    text = unicodedata.normalize("NFKC", text)
    out = []
    for ch in text:
        if ch in PUNCT or ch.isspace():
            continue
        out.append(ch.lower())
    return "".join(out)


def levenshtein_ops(ref: str, hyp: str) -> tuple[int, list[dict]]:
    """返回 (编辑距离, 操作明细)。明细用于定位错在哪。"""
    n, m = len(ref), len(hyp)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            dp[i][j] = min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + cost)

    ops: list[dict] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + (0 if ref[i - 1] == hyp[j - 1] else 1):
            if ref[i - 1] != hyp[j - 1]:
                ops.append({"type": "sub", "ref": ref[i - 1], "hyp": hyp[j - 1], "pos": i - 1})
            i, j = i - 1, j - 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            ops.append({"type": "del", "ref": ref[i - 1], "hyp": "", "pos": i - 1})
            i -= 1
        else:
            ops.append({"type": "ins", "ref": "", "hyp": hyp[j - 1], "pos": i})
            j -= 1
    ops.reverse()
    return dp[n][m], ops


def transcribe(audio: Path, device: str, compute_type: str, language: str | None, vad: bool) -> tuple[str, dict]:
    from faster_whisper import WhisperModel

    lock = MODELS_DIR / "last_download.txt"
    model_ref = lock.read_text(encoding="utf-8").strip() if lock.exists() else "large-v3-turbo"

    model = WhisperModel(model_ref, device=device, compute_type=compute_type, download_root=str(MODELS_DIR))
    segments, info = model.transcribe(str(audio), language=language, vad_filter=vad, beam_size=5)
    parts = [seg.text.strip() for seg in segments]
    return "".join(parts), {
        "model": model_ref,
        "device": device,
        "compute_type": compute_type,
        "language": getattr(info, "language", None),
        "language_probability": round(getattr(info, "language_probability", 0) or 0, 4),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", type=Path)
    ap.add_argument("--ref", default=None, help="参考文本（直接给）")
    ap.add_argument("--ref-file", type=Path, default=None, help="参考文本文件")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute-type", default="float16")
    ap.add_argument("--language", default=None)
    ap.add_argument("--vad", action="store_true")
    ap.add_argument("--save-text", default=None, help="把识别结果存到文件")
    args = ap.parse_args()

    audio = args.audio if args.audio.is_absolute() else (HERE / args.audio).resolve()
    if not audio.exists():
        raise SystemExit(f"[错误] 找不到音频：{audio}")

    if args.ref_file:
        ref_raw = (args.ref_file if args.ref_file.is_absolute() else (HERE / args.ref_file)).read_text(encoding="utf-8")
    elif args.ref:
        ref_raw = args.ref
    else:
        raise SystemExit("[错误] 需要 --ref 或 --ref-file 提供参考文本")

    print("=" * 66)
    print("声文 M1 · 字错率（CER）评测")
    print("=" * 66)
    print(f"  音频   {audio.name}")
    print(f"  设备   {args.device} / {args.compute_type}")

    hyp_raw, meta = transcribe(audio, args.device, args.compute_type, args.language, args.vad)

    ref, hyp = normalize(ref_raw), normalize(hyp_raw)
    dist, ops = levenshtein_ops(ref, hyp)
    cer = dist / len(ref) if ref else 0.0
    subs = sum(1 for o in ops if o["type"] == "sub")
    dels = sum(1 for o in ops if o["type"] == "del")
    inss = sum(1 for o in ops if o["type"] == "ins")

    print(f"\n[参考] {ref_raw.strip()}")
    print(f"[识别] {hyp_raw.strip()}")
    print(f"\n[语言] 检测为 {meta['language']}（置信 {meta['language_probability']}）")
    print(f"\n[CER]  {cer*100:.2f}%   编辑距离 {dist} / 参考 {len(ref)} 字")
    print(f"       替换 {subs} · 删除 {dels} · 插入 {inss}")
    if ops:
        print("\n[错误明细]")
        for o in ops[:40]:
            if o["type"] == "sub":
                print(f"   位置{o['pos']:>4}  替换：应为「{o['ref']}」识别成「{o['hyp']}」")
            elif o["type"] == "del":
                print(f"   位置{o['pos']:>4}  漏字：丢失「{o['ref']}」")
            else:
                print(f"   位置{o['pos']:>4}  多字：多出「{o['hyp']}」")
        if len(ops) > 40:
            print(f"   ...（共 {len(ops)} 处）")

    tag = f"{args.device}_{args.compute_type}"
    report = {
        "audio": audio.name,
        "meta": meta,
        "reference": ref_raw.strip(),
        "hypothesis": hyp_raw.strip(),
        "cer": round(cer, 4),
        "edit_distance": dist,
        "ref_chars": len(ref),
        "substitutions": subs,
        "deletions": dels,
        "insertions": inss,
        "errors": ops[:200],
    }
    out = HERE / f"cer_report_{tag}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[落盘] {out.name}")

    if args.save_text:
        Path(args.save_text).write_text(hyp_raw, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
