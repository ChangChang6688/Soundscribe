"""声文 · 用户配置层（热词库 / 纠错规则表）

★ 核心设计：内置基线与个人数据**物理分离**

    config/hotwords.txt          内置热词基线（只读，程序升级时覆盖）
    config/corrections.json      内置纠错基线（只读，含实测得来的 12 条规则）
    config/user/hotwords.json    ← 用户上传/新增的词（可读可写可删）
    config/user/corrections.json ← 用户上传/新增的规则（可读可写可删）

这样做的三个理由：
  1. 用户能删掉自己导入的，但删不掉内置的 —— 避免把实测得来的宝贵词库误删空
  2. 程序升级直接覆盖内置文件，个人数据完全不受影响
  3. 每条个人数据都记录来源与批次，可整批撤销（"我上次导的那个文件删掉"）

支持的上传格式（中文用户常见格式都覆盖了）：
  热词  .txt（每行一词，支持 [组名] 分段） / .csv / .tsv / .json
  纠错  .json（本应用格式或裸数组） / .csv（错,对） / .tsv / .txt（错→对 / 错=对 / 错,对）
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG = ROOT / "config"
USER_DIR = CONFIG / "user"

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# ═══════════════════════════════════════════════════════════
# 基础读写
# ═══════════════════════════════════════════════════════════


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def decode_text(raw: bytes) -> str:
    """稳健解码上传文件。

    ★ 中文用户大量使用 Windows 记事本 / Excel 导出的文件，
      编码可能是 GBK 或带 BOM 的 UTF-8，直接按 UTF-8 解会乱码或报错。
    """
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "utf-16", "big5"):
        try:
            txt = raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
        # 解码成功但出现大量替换字符，说明选错了编码，继续试下一个
        if txt.count("\ufffd") > len(txt) * 0.02:
            continue
        return txt
    return raw.decode("utf-8", errors="replace")


def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else json.loads(json.dumps(default))
    except Exception:  # noqa: BLE001
        return json.loads(json.dumps(default))


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)          # 原子替换，避免写一半损坏


# ═══════════════════════════════════════════════════════════
# 一、热词库 · 个人层
# ═══════════════════════════════════════════════════════════

HOTWORD_FILE = USER_DIR / "hotwords.json"
_HOT_DEFAULT = {"items": [], "batches": []}

# 单次导入的上限，防止超大文件把界面拖死
MAX_IMPORT_ITEMS = 2000
MAX_TERM_LEN = 40


def load_user_hotwords() -> dict:
    return _load_json(HOTWORD_FILE, _HOT_DEFAULT)


def _save_user_hotwords(d: dict) -> None:
    _save_json(HOTWORD_FILE, d)


def guess_group(term: str) -> str:
    """按词的字符构成自动分组，保证组名前缀能被 build_whisper_prompt 选中。"""
    return "zh.custom" if CJK.search(term) else "en.custom"


def normalize_terms(terms: list[str]) -> list[str]:
    """清洗词条：去空白、去重、限长、剔除纯符号。"""
    out: list[str] = []
    seen: set[str] = set()
    for t in terms:
        t = re.sub(r"\s+", " ", str(t or "")).strip().strip(",，;；|")
        if not t or len(t) > MAX_TERM_LEN:
            continue
        if not CJK.search(t) and not re.search(r"[A-Za-z0-9]", t):
            continue                      # 纯符号，没有实际意义
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def add_hotwords(terms: list[str], group: str = "", source: str = "手动添加") -> dict:
    """把一批热词并入个人层。返回 {batch_id, added, skipped, terms}。

    ★ 去重范围包含**内置热词** —— 否则用户导入一份和内置重叠的文件后，
      「张懿霖」这类词会在界面里出现两次，看起来像有 bug。
    """
    d = load_user_hotwords()
    existing = {i["term"].lower() for i in d["items"]}
    try:
        from app.pipeline import postprocess

        for words in postprocess._load_builtin_hotwords().values():  # noqa: SLF001
            existing.update(w.lower() for w in words)
    except Exception:  # noqa: BLE001
        pass

    clean = normalize_terms(terms)[:MAX_IMPORT_ITEMS]
    batch = _new_id("imp")
    added: list[str] = []
    now = _now()
    for t in clean:
        if t.lower() in existing:
            continue
        existing.add(t.lower())
        g = group or guess_group(t)
        d["items"].append({
            "id": _new_id("w"), "group": g, "term": t,
            "source": source, "batch": batch, "added_at": now,
        })
        added.append(t)

    d["batches"].append({
        "id": batch, "kind": "hotwords", "source": source,
        "added": len(added), "at": now,
    })
    _save_user_hotwords(d)
    return {"batch_id": batch, "added": len(added),
            "skipped": len(clean) - len(added), "terms": added[:50]}


def delete_hotword(item_id: str) -> bool:
    d = load_user_hotwords()
    n = len(d["items"])
    d["items"] = [i for i in d["items"] if i["id"] != item_id]
    if len(d["items"]) == n:
        return False
    _save_user_hotwords(d)
    return True


def delete_hotwords_by_batch(batch_id: str) -> int:
    d = load_user_hotwords()
    n = len(d["items"])
    d["items"] = [i for i in d["items"] if i.get("batch") != batch_id]
    d["batches"] = [b for b in d["batches"] if b["id"] != batch_id]
    removed = n - len(d["items"])
    if removed:
        _save_user_hotwords(d)
    return removed


def clear_user_hotwords() -> int:
    d = load_user_hotwords()
    n = len(d["items"])
    _save_user_hotwords({"items": [], "batches": []})
    return n


# ═══════════════════════════════════════════════════════════
# 二、纠错表 · 个人层
# ═══════════════════════════════════════════════════════════

CORR_FILE = USER_DIR / "corrections.json"
_CORR_DEFAULT = {"rules": [], "hallucination_patterns": [], "batches": []}

MAX_RULE_LEN = 60


def load_user_corrections() -> dict:
    return _load_json(CORR_FILE, _CORR_DEFAULT)


def _save_user_corrections(d: dict) -> None:
    _save_json(CORR_FILE, d)


def add_corrections(rules: list[dict], source: str = "手动添加") -> dict:
    """rules: [{pattern, replacement, severity}]。返回统计。"""
    d = load_user_corrections()
    existing = {r["pattern"] for r in d["rules"]}
    batch = _new_id("imp")
    now = _now()
    added: list[dict] = []
    skipped = 0

    for r in rules[:MAX_IMPORT_ITEMS]:
        pat = str(r.get("pattern") or "").strip()
        rep = r.get("replacement")
        rep = "" if rep is None else str(rep).strip()
        if not pat or len(pat) > MAX_RULE_LEN or pat == rep or pat in existing:
            skipped += 1
            continue
        existing.add(pat)
        item = {
            "id": _new_id("r"), "pattern": pat, "replacement": rep,
            "severity": r.get("severity") or "normal",
            "note": str(r.get("note") or ""),
            "source": source, "batch": batch, "added_at": now,
            "enabled": True,
        }
        d["rules"].append(item)
        added.append(item)

    d["batches"].append({"id": batch, "kind": "corrections",
                         "source": source, "added": len(added), "at": now})
    _save_user_corrections(d)
    return {"batch_id": batch, "added": len(added), "skipped": skipped,
            "rules": added[:50]}


def add_hallucination_patterns(patterns: list[str], source: str = "手动添加") -> int:
    d = load_user_corrections()
    existing = set(d["hallucination_patterns"])
    batch = _new_id("imp")
    n = 0
    for p in patterns:
        p = str(p or "").strip()
        if p and len(p) <= MAX_RULE_LEN and p not in existing:
            existing.add(p)
            d["hallucination_patterns"].append(p)
            n += 1
    if n:
        d["batches"].append({"id": batch, "kind": "hallucination",
                             "source": source, "added": n, "at": _now()})
        _save_user_corrections(d)
    return n


def delete_correction(rule_id: str) -> bool:
    d = load_user_corrections()
    n = len(d["rules"])
    d["rules"] = [r for r in d["rules"] if r["id"] != rule_id]
    if len(d["rules"]) == n:
        return False
    _save_user_corrections(d)
    return True


def delete_corrections_by_batch(batch_id: str) -> int:
    d = load_user_corrections()
    n = len(d["rules"])
    d["rules"] = [r for r in d["rules"] if r.get("batch") != batch_id]
    d["batches"] = [b for b in d["batches"] if b["id"] != batch_id]
    removed = n - len(d["rules"])
    if removed:
        _save_user_corrections(d)
    return removed


def toggle_correction(rule_id: str, enabled: bool) -> bool:
    d = load_user_corrections()
    for r in d["rules"]:
        if r["id"] == rule_id:
            r["enabled"] = bool(enabled)
            _save_user_corrections(d)
            return True
    return False


def clear_user_corrections() -> int:
    d = load_user_corrections()
    n = len(d["rules"])
    _save_user_corrections({"rules": [], "hallucination_patterns": [], "batches": []})
    return n


# ═══════════════════════════════════════════════════════════
# 三、上传文件解析
# ═══════════════════════════════════════════════════════════

HOT_EXT = {".txt", ".csv", ".tsv", ".json", ".md"}
CORR_EXT = {".txt", ".csv", ".tsv", ".json", ".md"}


class ParseError(ValueError):
    """上传文件无法解析时抛出，附中文可读原因。"""


def _split_rows(text: str, sep: str | None = None) -> list[list[str]]:
    if sep:
        return [r for r in csv.reader(io.StringIO(text), delimiter=sep) if r]
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",\t;|")
        return [r for r in csv.reader(io.StringIO(text), dialect) if r]
    except Exception:  # noqa: BLE001
        return [r for r in csv.reader(io.StringIO(text)) if r]


def parse_hotword_file(filename: str, raw: bytes) -> tuple[list[dict], dict]:
    """解析热词文件 → ([{term, group}], 统计信息)。"""
    ext = Path(filename).suffix.lower()
    if ext not in HOT_EXT:
        raise ParseError(f"不支持的文件类型 {ext or '(无扩展名)'}，"
                         f"请上传 {'/'.join(sorted(HOT_EXT))} 之一")
    text = decode_text(raw)
    items: list[dict] = []
    fmt = ""

    if ext == ".json":
        fmt = "JSON"
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ParseError(f"JSON 格式错误（第 {exc.lineno} 行）：{exc.msg}") from exc
        if isinstance(data, list):
            items = [{"term": str(x), "group": ""} for x in data]
        elif isinstance(data, dict):
            if isinstance(data.get("items"), list):
                for x in data["items"]:
                    if isinstance(x, dict):
                        items.append({"term": str(x.get("term") or x.get("word") or ""),
                                      "group": str(x.get("group") or "")})
                    else:
                        items.append({"term": str(x), "group": ""})
            elif isinstance(data.get("groups"), dict):
                for g, arr in data["groups"].items():
                    for x in (arr or []):
                        items.append({"term": str(x), "group": str(g)})
            else:
                # {组名: [词...]} 形式
                for g, arr in data.items():
                    if isinstance(arr, list):
                        for x in arr:
                            items.append({"term": str(x), "group": str(g)})
                    else:
                        items.append({"term": str(g), "group": ""})
        else:
            raise ParseError("JSON 顶层必须是数组或对象")
    elif ext in (".csv", ".tsv"):
        fmt = "TSV" if ext == ".tsv" else "CSV"
        rows = _split_rows(text, "\t" if ext == ".tsv" else None)
        for r in rows:
            if not r:
                continue
            term = str(r[0]).strip()
            group = str(r[1]).strip() if len(r) > 1 else ""
            # 跳过表头
            if term.lower() in ("term", "word", "词", "热词", "关键词"):
                continue
            items.append({"term": term, "group": group})
    else:
        fmt = "文本（每行一词）"
        cur = ""
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            m = re.match(r"^\[([^\]]+)\]$", line)
            if m:
                cur = m.group(1).strip()
                continue
            if line.startswith("#"):
                continue
            # 允许「词,组名」这种偷懒写法
            if "," in line and len(line.split(",")) == 2 and len(line.split(",")[1].strip()) <= 20:
                a, b = line.split(",", 1)
                items.append({"term": a.strip(), "group": b.strip()})
            else:
                items.append({"term": line, "group": cur})

    clean: list[dict] = []
    seen: set[str] = set()
    for it in items:
        t = normalize_terms([it["term"]])
        if not t:
            continue
        term = t[0]
        if term.lower() in seen:
            continue
        seen.add(term.lower())
        clean.append({"term": term, "group": it.get("group") or ""})

    if not clean:
        raise ParseError("文件里没有解析出任何有效词条（可能全是空行、注释或纯符号）")
    return clean, {"format": fmt, "total": len(clean)}


def parse_correction_file(filename: str, raw: bytes) -> tuple[list[dict], list[str], dict]:
    """解析纠错文件 → ([{pattern,replacement,severity}], [幻觉黑名单], 统计)。"""
    ext = Path(filename).suffix.lower()
    if ext not in CORR_EXT:
        raise ParseError(f"不支持的文件类型 {ext or '(无扩展名)'}，"
                         f"请上传 {'/'.join(sorted(CORR_EXT))} 之一")
    text = decode_text(raw)
    rules: list[dict] = []
    patterns: list[str] = []
    fmt = ""

    if ext == ".json":
        fmt = "JSON"
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ParseError(f"JSON 格式错误（第 {exc.lineno} 行）：{exc.msg}") from exc
        if isinstance(data, list):
            data = {"rules": data}
        if not isinstance(data, dict):
            raise ParseError("JSON 顶层必须是数组或对象")
        for r in (data.get("rules") or []):
            if isinstance(r, dict):
                rules.append({
                    "pattern": str(r.get("pattern") or r.get("wrong") or ""),
                    "replacement": str(r.get("replacement", r.get("right", "")) or ""),
                    "severity": r.get("severity") or "normal",
                    "note": r.get("note") or "",
                })
            elif isinstance(r, (list, tuple)) and len(r) >= 2:
                rules.append({"pattern": str(r[0]), "replacement": str(r[1])})
        hb = data.get("hallucination_blacklist")
        if isinstance(hb, dict):
            patterns += [str(x) for x in (hb.get("patterns") or [])]
        elif isinstance(hb, list):
            patterns += [str(x) for x in hb]
        patterns += [str(x) for x in (data.get("hallucination_patterns") or [])]
    elif ext in (".csv", ".tsv"):
        fmt = "TSV" if ext == ".tsv" else "CSV"
        for r in _split_rows(text, "\t" if ext == ".tsv" else None):
            if len(r) < 2:
                continue
            a, b = str(r[0]).strip(), str(r[1]).strip()
            if a.lower() in ("pattern", "wrong", "错", "错误", "原文"):
                continue
            sev = str(r[2]).strip() if len(r) > 2 else "normal"
            rules.append({"pattern": a, "replacement": b, "severity": sev})
    else:
        fmt = "文本（错→对）"
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            hit = re.split(r"\s*(?:->|→|=>|=|，|,|\t)\s*", line, maxsplit=1)
            if len(hit) != 2:
                # 单列：当作幻觉黑名单模式
                patterns.append(line)
                continue
            rules.append({"pattern": hit[0].strip(), "replacement": hit[1].strip()})

    clean: list[dict] = []
    seen: set[str] = set()
    for r in rules:
        p = str(r.get("pattern") or "").strip()
        rep = str(r.get("replacement") or "").strip()
        if not p or len(p) > MAX_RULE_LEN or p == rep or p in seen:
            continue
        seen.add(p)
        clean.append({"pattern": p, "replacement": rep,
                      "severity": r.get("severity") or "normal",
                      "note": r.get("note") or ""})

    patterns = [p.strip() for p in patterns if p and p.strip()]

    if not clean and not patterns:
        raise ParseError("文件里没有解析出任何有效规则。"
                         "文本格式请写成每行「错误 → 正确」")
    return clean, patterns, {"format": fmt, "rules": len(clean), "patterns": len(patterns)}


# ═══════════════════════════════════════════════════════════
# 四、导入模板（供界面下载参考）
# ═══════════════════════════════════════════════════════════

HOTWORD_TEMPLATE = """# 声文 · 热词库导入模板
# 每行一个词。以 # 开头的行是注释，会被忽略。
# 可用 [组名] 手动分组；不写则自动按中英文归入 zh.custom / en.custom
# 支持 .txt / .csv（词,组名）/ .json

[zh.custom]
张懿霖
舒文帆
失蜡铸造
内腔公差

[en.custom]
PCBA
layout
review
"""

CORRECTION_TEMPLATE = """# 声文 · 纠错规则表导入模板
# 每行一条，格式：错误写法 → 正确写法
# 也支持 .csv（错误,正确,severity）与 .json
# replacement 留空表示「直接删除该词」

手势 → 首饰
缠道 → 禅道
飞移 → 非遗
公美 → 工美
张艺林 → 张懿霖
"""


def ensure_user_dir() -> None:
    USER_DIR.mkdir(parents=True, exist_ok=True)


ensure_user_dir()


# ═══════════════════════════════════════════════════════════
# 五、内置项的「隐藏」记录
#
# ★ 为什么内置也要能删：
#   内置纠错规则是从**本人真实录音的错误**里挖出来的（如「手势→首饰」），
#   对别人不一定适用。用户把软件交给同事时，需要能清掉这些个人痕迹。
#   但直接改内置文件会破坏「升级时整体覆盖」的约定，所以改为**记录排除项**：
#     config/user/removed.json = {"corrections": ["手势"], "hotwords": ["首饰"]}
#   用「内容」而不是 id 记录，因为内置数据的 id 可能随版本变化。
#   排除项随时可一键恢复，不会真的丢失。
# ═══════════════════════════════════════════════════════════

REMOVED_FILE = USER_DIR / "removed.json"
_REMOVED_DEFAULT = {"corrections": [], "hotwords": []}

_BUCKET = {"corrections": "corrections", "hotwords": "hotwords"}


def load_removed() -> dict:
    d = _load_json(REMOVED_FILE, _REMOVED_DEFAULT)
    return {k: list(d.get(k) or []) for k in ("corrections", "hotwords")}


def hide_builtin(kind: str, key: str) -> dict:
    """把某个内置项标记为「不用了」。可随时恢复。"""
    bucket = _BUCKET.get(kind)
    if not bucket:
        raise ValueError(f"未知类别：{kind}")
    key = str(key or "").strip()
    if not key:
        raise ValueError("缺少要移除的条目")
    d = load_removed()
    if key not in d[bucket]:
        d[bucket].append(key)
        _save_json(REMOVED_FILE, d)
    return {"ok": True, "kind": bucket, "hidden": len(d[bucket])}


def restore_builtin(kind: str, key: str = "") -> dict:
    """恢复内置项：给了 key 就恢复这一条，否则全部恢复。"""
    bucket = _BUCKET.get(kind)
    if not bucket:
        raise ValueError(f"未知类别：{kind}")
    d = load_removed()
    if key:
        d[bucket] = [x for x in d[bucket] if x != str(key).strip()]
    else:
        d[bucket] = []
    _save_json(REMOVED_FILE, d)
    return {"ok": True, "kind": bucket, "hidden": len(d[bucket])}


def clear_all_user_data() -> dict:
    """把个人痕迹一次清干净（交付给别人前用）。

    清掉：个人热词、个人纠错、内置隐藏记录；内置基线**自动恢复完整**。
    """
    hot = len(load_user_hotwords().get("items", []))
    corr = len(load_user_corrections().get("rules", []))
    rem = load_removed()
    hidden = len(rem["corrections"]) + len(rem["hotwords"])
    _save_json(HOTWORD_FILE, {"items": [], "batches": []})
    _save_json(CORR_FILE, {"rules": [], "hallucination_patterns": [], "batches": []})
    _save_json(REMOVED_FILE, dict(_REMOVED_DEFAULT))
    return {"user_hotwords": hot, "user_corrections": corr, "restored_builtin": hidden}


def user_data_summary() -> dict:
    rem = load_removed()
    return {
        "user_hotwords": len(load_user_hotwords().get("items", [])),
        "user_corrections": len(load_user_corrections().get("rules", [])),
        "hidden_builtin": len(rem["corrections"]) + len(rem["hotwords"]),
    }
