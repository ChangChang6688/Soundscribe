"""声文 · 导出层（方案 §4 管线第 10 步、§7.7）

支持：TXT / SRT / VTT / Markdown / JSON
可配：导出目录、目录结构策略（按日期 / 平铺）、文件名模板
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_EXPORT = ROOT / "data" / "exports"


def fmt_ts(sec: float, sep: str = ",") -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", sep)


def safe_name(s: str) -> str:
    """清掉文件名里的非法字符，保留中文。"""
    return re.sub(r'[\\/:*?"<>|\r\n]+', "_", s).strip() or "untitled"


@dataclass
class ExportOptions:
    formats: list[str]                    # txt|srt|vtt|md|json
    outdir: Path
    layout: str = "by_date"               # by_date | flat
    name_template: str = "{stem}_{engine}_{date}"
    with_timestamps: bool = True


def build_outdir(opts: ExportOptions, when: datetime | None = None) -> Path:
    when = when or datetime.now()
    d = opts.outdir if opts.layout == "flat" else opts.outdir / when.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_basename(opts: ExportOptions, stem: str, engine: str,
                   when: datetime | None = None) -> str:
    when = when or datetime.now()
    name = (opts.name_template
            .replace("{stem}", safe_name(stem))
            .replace("{engine}", engine)
            .replace("{date}", when.strftime("%Y%m%d")))
    return safe_name(name)


def export_all(segments: list[dict], meta: dict, opts: ExportOptions) -> dict:
    """写出全部勾选格式，返回 {格式: 文件路径}。"""
    now = datetime.now()
    outdir = build_outdir(opts, now)
    base = build_basename(opts, meta.get("source_stem", "audio"), meta.get("engine", "asr"), now)
    written: dict[str, str] = {}

    text = "".join(s.get("text", "") for s in segments)

    if "txt" in opts.formats:
        p = outdir / f"{base}.txt"
        header = f"# {meta.get('source_name','')}\n# 引擎 {meta.get('engine_display','')} | 时长 {meta.get('duration_display','')}\n\n"
        p.write_text(header + text, encoding="utf-8")
        written["txt"] = str(p)

    if "srt" in opts.formats and opts.with_timestamps:
        p = outdir / f"{base}.srt"
        with p.open("w", encoding="utf-8") as f:
            for i, s in enumerate(segments, 1):
                f.write(f"{i}\n{fmt_ts(s['start'])} --> {fmt_ts(s['end'])}\n{s['text']}\n\n")
        written["srt"] = str(p)

    if "vtt" in opts.formats and opts.with_timestamps:
        p = outdir / f"{base}.vtt"
        with p.open("w", encoding="utf-8") as f:
            f.write("WEBVTT\n\n")
            for s in segments:
                f.write(f"{fmt_ts(s['start'], '.')} --> {fmt_ts(s['end'], '.')}\n{s['text']}\n\n")
        written["vtt"] = str(p)

    if "md" in opts.formats:
        p = outdir / f"{base}.md"
        lines = [
            f"# {meta.get('source_name','')}", "",
            f"- 引擎：**{meta.get('engine_display','')}**",
            f"- 时长：{meta.get('duration_display','')}",
            f"- 导出时间：{now.strftime('%Y-%m-%d %H:%M')}",
            f"- 段落数：{len(segments)}", "",
        ]
        if meta.get("route_reason"):
            lines += [f"- 路由判定：{meta['route_reason']}", ""]
        lines.append("---")
        lines.append("")
        for s in segments:
            ts = f"`{fmt_ts(s['start'], '.')[:8]}` " if opts.with_timestamps else ""
            lines.append(f"{ts}{s['text']}")
            lines.append("")
        p.write_text("\n".join(lines), encoding="utf-8")
        written["md"] = str(p)

    if "json" in opts.formats:
        p = outdir / f"{base}.json"
        p.write_text(json.dumps({"meta": meta, "segments": segments},
                                ensure_ascii=False, indent=2), encoding="utf-8")
        written["json"] = str(p)

    return written
