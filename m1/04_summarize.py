"""M1 · 汇总所有测速与 CER 报告，生成可直接回填方案的对比表。

用法：
  python 04_summarize.py
输出：
  控制台表格 + m1_summary.md
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_speed() -> list[dict]:
    rows = []
    for p in sorted(HERE.glob("speed_report_*.json")):
        try:
            rows.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    return rows


def load_cer() -> list[dict]:
    rows = []
    for p in sorted(HERE.glob("cer_report_*.json")):
        try:
            rows.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    return rows


def main() -> int:
    speeds = load_speed()
    cers = load_cer()

    lines: list[str] = []
    lines.append("# 声文 M1 · 实测数据汇总\n")
    lines.append("> 由 `04_summarize.py` 自动生成，数据来自 m1/ 下的 JSON 报告。")
    lines.append("> 用途：替换方案 §9「性能预估」中的推算值。\n")

    lines.append("## 一、转写速度实测\n")
    if speeds:
        lines.append("| 音频 | 时长 | 设备 | 量化 | 批 | VAD | 加载 | 转写 | 倍速 | 1 小时录音折算 | 显存峰值 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for r in sorted(speeds, key=lambda x: (x.get("device") or "", x.get("audio") or "")):
            dur = r.get("audio_seconds") or 0
            vac = "开" if r.get("vad") else "关"
            bt = r.get("batch") or "关"
            mem = (r.get("vram") or {}).get("after_run") or {}
            memtxt = f"{mem.get('used_mib')} MiB" if mem.get("used_mib") else "—"
            # 统一从 speed_x 现算，避免历史报告里字段单位不一致
            sp = r.get("speed_x") or 0
            mins = f"{3600/sp/60:.2f} 分钟" if sp else "—"
            lines.append(
                "| {audio} | {d} | {dev} | {ct} | {b} | {v} | {ld}s | {tr}s | **{sp}×** | {mp} | {mem} |".format(
                    audio=r.get("audio"),
                    d=f"{dur:.1f}s",
                    dev=r.get("device"),
                    ct=r.get("compute_type"),
                    b=bt,
                    v=vac,
                    ld=r.get("load_seconds"),
                    tr=r.get("transcribe_seconds"),
                    sp=r.get("speed_x"),
                    mp=mins,
                    mem=memtxt,
                )
            )
        lines.append("")
        # 长音频才是可信数据
        longs = [r for r in speeds if (r.get("audio_seconds") or 0) > 120]
        if longs:
            lines.append("> ⚠️ 只有时长 > 2 分钟的结果才可用于外推；")
            lines.append("> 短音频的固定开销（模型预热、解码初始化）占比过大，会严重低估速度。\n")
    else:
        lines.append("（暂无测速报告）\n")

    lines.append("## 二、字错率（CER）实测\n")
    if cers:
        lines.append("| 音频 | 设备 | 量化 | 检测语言 | 参考字数 | 编辑距离 | 替换 | 删除 | 插入 | **CER** |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for r in cers:
            m = r.get("meta") or {}
            lines.append(
                "| {a} | {d} | {c} | {l}({p}) | {n} | {e} | {s} | {dl} | {i} | **{cer:.2f}%** |".format(
                    a=r.get("audio"),
                    d=m.get("device"),
                    c=m.get("compute_type"),
                    l=m.get("language"),
                    p=m.get("language_probability"),
                    n=r.get("ref_chars"),
                    e=r.get("edit_distance"),
                    s=r.get("substitutions"),
                    dl=r.get("deletions"),
                    i=r.get("insertions"),
                    cer=(r.get("cer") or 0) * 100,
                )
            )
        lines.append("")
        for r in cers:
            lines.append(f"**{r.get('audio')} · {r.get('meta',{}).get('device')}**")
            lines.append(f"- 参考：{r.get('reference')}")
            lines.append(f"- 识别：{r.get('hypothesis')}")
            lines.append("")
    else:
        lines.append("（暂无 CER 报告）\n")

    lines.append("## 三、环境信息\n")
    env = HERE / "env_report.json"
    if env.exists():
        e = json.loads(env.read_text(encoding="utf-8"))
        for k, v in (e.get("hardware") or {}).items():
            lines.append(f"- **{k}**：{v}")
        ct = e.get("ctranslate2") or {}
        lines.append(f"- **ctranslate2**：{ct.get('version')}")
        lines.append(f"- **cuda 设备数**：{ct.get('cuda_device_count')}")
        t = (ct.get("supported_compute_types") or {})
        lines.append(f"- **cuda 量化档**：{t.get('cuda')}")
        lines.append(f"- **cpu 量化档**：{t.get('cpu')}")
    lines.append("")

    text = "\n".join(lines)
    print(text)
    (HERE / "m1_summary.md").write_text(text, encoding="utf-8")
    print(f"\n[落盘] {HERE / 'm1_summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
