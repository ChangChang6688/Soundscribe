"""声文 · 后处理层（方案 §4 管线第 7–8 步）

包含四件事：
  1. 纠错表应用（来自 config/corrections.json，全部规则都源自真实转写错误）
  2. 幻觉过滤（黑名单 + 重复退化检测）
  3. 低置信标记
  4. ★ 语言一致性校验 —— 防 SenseVoice 语言漂移的第二道防线
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG = ROOT / "config"

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
LATIN_WORD = re.compile(r"[A-Za-z]+")


# ═══════════════════════════════════════════════════════════
# 配置加载
# ═══════════════════════════════════════════════════════════


def _load_builtin_corrections() -> dict:
    p = CONFIG / "corrections.json"
    empty = {"rules": [], "hallucination_blacklist": {"patterns": []}, "repetition_guard": {}}
    if not p.exists():
        return empty
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else empty
    except Exception:  # noqa: BLE001
        return empty


def load_corrections() -> dict:
    """内置基线 + 个人上传的合并结果。

    ★ 内置规则带 `builtin: True` 标记；用户可以把内置规则也标记为「不用了」
      （记录在 config/user/removed.json，不入内置文件本身），随时可恢复。
    """
    from app.store import config_store

    try:
        removed = set(config_store.load_removed().get("corrections", []))
    except Exception:  # noqa: BLE001
        removed = set()

    builtin = _load_builtin_corrections()
    rules: list[dict] = []
    for r in builtin.get("rules", []):
        if r.get("pattern") in removed:
            continue
        rr = dict(r)
        rr["builtin"] = True
        rr["enabled"] = True
        rules.append(rr)

    patterns = list(builtin.get("hallucination_blacklist", {}).get("patterns", []))
    try:
        user = config_store.load_user_corrections()
    except Exception:  # noqa: BLE001
        user = {"rules": [], "hallucination_patterns": []}
    for r in user.get("rules", []):
        if not r.get("enabled", True):
            continue
        rr = dict(r)
        rr.setdefault("severity", "normal")
        rr["builtin"] = False
        rules.append(rr)
    for p in user.get("hallucination_patterns", []):
        if p and p not in patterns:
            patterns.append(p)

    return {
        "rules": rules,
        "hallucination_blacklist": {"patterns": patterns},
        "repetition_guard": builtin.get("repetition_guard", {}),
    }


def _load_builtin_hotwords() -> dict[str, list[str]]:
    """解析 config/hotwords.txt（内置基线），按 [组名] 归组。"""
    p = CONFIG / "hotwords.txt"
    groups: dict[str, list[str]] = {}
    if not p.exists():
        return groups
    cur = None
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[([^\]]+)\]$", line)
        if m:
            cur = m.group(1).strip()
            groups.setdefault(cur, [])
            continue
        if cur:
            groups[cur].append(line)
    return groups


def load_hotwords() -> dict[str, list[str]]:
    """内置热词 + 个人上传热词的合并结果（组内去重、保持顺序）。

    被用户标记为「不用了」的内置词会被过滤掉（见 config/user/removed.json）。
    """
    groups = _load_builtin_hotwords()
    try:
        from app.store import config_store

        removed = set(config_store.load_removed().get("hotwords", []))
        if removed:
            groups = {g: [w for w in ws if w not in removed] for g, ws in groups.items()}
            groups = {g: ws for g, ws in groups.items() if ws}

        for it in config_store.load_user_hotwords().get("items", []):
            g = it.get("group") or "zh.custom"
            groups.setdefault(g, [])
            if it.get("term") and it["term"] not in groups[g]:
                groups[g].append(it["term"])
    except Exception:  # noqa: BLE001
        pass
    return groups


def build_whisper_prompt(groups: dict[str, list[str]], lang: str = "zh") -> str:
    """把热词组拼成 Whisper 的 initial_prompt（整句形式效果好于纯词表）。

    ★ 修正：原实现排除了 people / brands 两组，理由不成立 ——
      实测中 Whisper 错得最多的恰恰是人名（张懿霖 → 张艺林/张依林）与品牌名。
      这里改为全部纳入，总额限 90 个词，避免 prompt 过长反而稀释效果。
    """
    prefix = "zh." if lang == "zh" else "en."
    terms: list[str] = []
    for k, v in groups.items():
        if k.startswith(prefix):
            terms += v
    seen: set[str] = set()
    uniq: list[str] = []
    for t in terms:
        if len(t) <= 14 and t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    uniq = uniq[:90]
    if lang == "zh":
        if not uniq:
            return "以下是普通话录音。"
        return ("以下是普通话录音，内容涉及珠宝首饰设计、品牌出海、可穿戴硬件。"
                "专有名词：" + "、".join(uniq) + "。")
    if not uniq:
        return "This is an English recording."
    return "This is an English recording. Proper nouns and terms: " + ", ".join(uniq) + "."


# ═══════════════════════════════════════════════════════════
# 规则应用
# ═══════════════════════════════════════════════════════════


@dataclass
class AppliedRule:
    rule_id: str
    pattern: str
    replacement: str
    count: int


@dataclass
class PostResult:
    text: str
    hits: list[AppliedRule] = field(default_factory=list)
    hallucinations: list[dict] = field(default_factory=list)
    low_confidence: list[int] = field(default_factory=list)
    needs_review: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "hits": [h.__dict__ for h in self.hits],
            "hallucinations": self.hallucinations,
            "low_confidence_segments": self.low_confidence,
            "needs_review_segments": self.needs_review,
            "correction_total": sum(h.count for h in self.hits),
        }


def apply_corrections(text: str, cfg: dict | None = None) -> tuple[str, list[AppliedRule]]:
    cfg = cfg or load_corrections()
    hits: list[AppliedRule] = []
    out = text
    for r in cfg.get("rules", []):
        pat, rep = r.get("pattern"), r.get("replacement")
        if not pat or rep is None:
            continue
        n = out.count(pat)
        if n:
            out = out.replace(pat, rep)
            hits.append(AppliedRule(r.get("id", "?"), pat, rep, n))
    return out, hits


def is_hallucination(text: str, cfg: dict | None = None) -> tuple[bool, str]:
    """判断该段是否应当**丢弃**。

    ★ 设计修正（实测踩过）：重复**绝不能**作为丢弃依据。
      中文口语里「非常非常」「一定一定」「极其极其」「对抗对抗对抗」是极常见的强调说法，
      实测某份 122 分钟录音里 **39.2% 的段都含重复模式** —— 按重复丢弃会删掉四成内容。
      「疑似重复退化」只做标记（见 needs_review），不丢弃。
    """
    cfg = cfg or load_corrections()
    t = text.strip()
    if not t:
        return True, "空段"
    for p in cfg.get("hallucination_blacklist", {}).get("patterns", []):
        if p and p in t:
            return True, f"命中幻觉黑名单「{p}」"
    # 纯数字/纯符号段（Whisper 在噪声段的典型输出）
    if re.fullmatch(r"[\d\s.,%]+", t):
        return True, "纯数字噪声段"
    # 完全没有中英文实义字符
    if not CJK.search(t) and not LATIN_WORD.search(t):
        return True, "无实义字符"
    # 极短且无意义
    if len(t) <= 2 and not CJK.search(t) and not LATIN_WORD.search(t):
        return True, "无意义短段"
    return False, ""


def needs_review(text: str) -> tuple[bool, str]:
    """标记「疑似解码退化」，供人工复核 —— 只标记，不丢弃。

    阈值刻意设得保守，只抓极端情况（同一字符连续 4 次以上、或同一 1–2 字短语连续 4 次以上）。
    """
    t = text.strip()
    if re.search(r"(.)\1{3,}", t):
        return True, "存在字符级重复"
    if re.search(r"(.{1,2})\1{3,}", t):
        return True, "存在短语级重复"
    return False, ""


def calibration(cfg: dict | None = None) -> dict:
    """纠错规则表的「标定来源」与「现在实际在跑的引擎」是否对得上。

    ★ 为什么必须算这个，而不是在界面上写一句"内置规则来自 Whisper"：
      规则是从**某个引擎**的实际错误里总结出来的。引擎换了，错误类型会变
      （Whisper 错在切词与英文术语，SenseVoice 错在同音字与短句合并），
      命中率自然会掉。这不是"规则没生效"，是"规则对不上现在的问题" ——
      两者的处置方式完全不同（前者去查接线，后者去补规则），所以必须能分辨。
      而且这个事实会随默认引擎变化，写死在界面上就一定会过期。
    """
    cfg = cfg or load_corrections()
    by_src: dict[str, int] = {}
    for r in cfg.get("rules", []):
        k = r.get("source_engine") or "unknown"
        by_src[k] = by_src.get(k, 0) + 1

    try:
        from app.pipeline import langroute

        zh_engine = langroute.ENGINE_FOR_ZH
    except Exception:  # noqa: BLE001
        zh_engine = ""
    try:
        from app.doctor import capabilities as _cap

        z = _cap.ENGINE_NAME.get(zh_engine, zh_engine)
    except Exception:  # noqa: BLE001
        z = zh_engine

    mismatched = sum(n for k, n in by_src.items()
                     if k not in ("unknown", "") and k != zh_engine)

    note = ""
    warn = False
    if mismatched and zh_engine:
        note = (f"内置规则有 {mismatched} 条是在其它引擎上总结的，"
                f"而中文素材现在默认走 {z} —— 引擎不同、错误类型不同，"
                f"所以这部分规则命中率会偏低。这不是接线问题，是规则需要按 {z} 的实际错误补充。")
        warn = True
    return {
        "by_source_engine": by_src,
        "zh_default_engine": zh_engine,
        "zh_default_name": z,
        "mismatched": mismatched,
        "warn": warn,
        "note": note,
    }


LOW_CONF_LOGP = -0.6


def postprocess_segments(segments: list[dict], cfg: dict | None = None) -> tuple[list[dict], PostResult]:
    """对分段结果做过滤、纠错与标记。返回 (清洗后的分段, 后处理报告)。"""
    cfg = cfg or load_corrections()
    kept: list[dict] = []
    hallucinations: list[dict] = []
    low: list[int] = []
    review: list[int] = []
    all_hits: dict[str, AppliedRule] = {}

    for s in segments:
        bad, why = is_hallucination(s.get("text", ""), cfg)
        if bad:
            hallucinations.append({"start": s.get("start"), "text": s.get("text"), "reason": why})
            continue
        txt, hits = apply_corrections(s.get("text", ""), cfg)
        for h in hits:
            if h.rule_id in all_hits:
                all_hits[h.rule_id].count += h.count
            else:
                all_hits[h.rule_id] = h
        s = dict(s)
        s["text"] = txt
        s["corrected"] = bool(hits)
        # 重复退化只标记不丢弃
        flag, flag_why = needs_review(txt)
        if flag:
            s["needs_review"] = True
            s["review_reason"] = flag_why
            review.append(s.get("index", 0))
        if s.get("avg_logprob", 0) and s["avg_logprob"] < LOW_CONF_LOGP:
            s["low_confidence"] = True
            low.append(s.get("index", 0))
        kept.append(s)

    text = "".join(s["text"] for s in kept)
    report = PostResult(text=text, hits=list(all_hits.values()),
                        hallucinations=hallucinations, low_confidence=low,
                        needs_review=review)
    return kept, report


# ═══════════════════════════════════════════════════════════
# ★ 语言一致性校验（第二道防线）
# ═══════════════════════════════════════════════════════════


def check_language_drift(text: str, used_engine: str, cjk_ratio: float,
                         audio_seconds: float = 0.0) -> dict:
    """校验转写结果的语言是否与预期一致。

    实测背景：SenseVoice 在纯英文音频上会「语言漂移」——把整段英文输出成中文
    （3 分钟英文素材上出现 236 个中文字符、20 个连续中文块）。
    漂移发生在中段（全文 30%–54% 位置），所以只靠前段采样会漏掉，必须整段复校。

    ★★ 2026-09-30 补：原来只写了 sensevoice 和 whisper 两个分支，
      **parakeet / moonshine 完全没有**。而这两个是英文专用模型、不认中文 ——
      中文素材一旦被路由到它们（实测发生过：短素材采样失败被判成"纯英文"），
      输出会静默变坏且**没有任何警告**。最危险的引擎反而没有防线，这是本末倒置。
      现在补齐，并额外加一条与语言无关的判据：输出量是否正常。
    """
    cjk = len(CJK.findall(text))
    latin = len(LATIN_WORD.findall(text))
    units = cjk + latin
    ratio = cjk / units if units else 0.0

    # 连续中文块（≥2 字）—— 用来区分「零星中文」与「整段漂移」
    blocks = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    max_block = max((len(b) for b in blocks), default=0)

    warn = False
    reason = ""
    suggestion = ""

    if used_engine == "sensevoice" and ratio < 0.35 and max_block >= 6:
        warn = True
        reason = (f"使用 SenseVoice 但结果中仅 {ratio*100:.1f}% 为中文，"
                  f"且出现最长 {max_block} 字的连续中文块，疑似语言漂移。")
        suggestion = "建议改用 Whisper 引擎重跑，或开启双引擎比对。"
    elif used_engine == "whisper" and ratio > 0.85 and cjk_ratio < 0.4:
        warn = True
        reason = f"预期英文素材但结果中 {ratio*100:.1f}% 为中文，疑似引擎语言判定异常。"
        suggestion = "建议检查音频语言，或改用 SenseVoice 重跑。"
    elif used_engine in ("parakeet", "moonshine") and ratio > 0.10:
        # ★★ 英文专用模型吐出了中文 —— 它不可能听对，这部分内容是坏的
        warn = True
        reason = (f"用的是英文专用模型，却识别出 {ratio*100:.1f}% 的中文"
                  f"（{cjk} 个汉字、最长 {max_block} 字连续块）。该模型不支持中文，"
                  "这部分内容很可能已经被丢掉或识别错了。")
        suggestion = "建议改用 Whisper 或 SenseVoice 重跑这段素材。"

    # ★ 与语言无关的兜底判据：输出量明显偏少 = 引擎没听懂（或漏了大段）。
    #   实测中文被喂给 Parakeet 时，输出会变成乱码拼音 —— 那些是拉丁字母，
    #   上面按「中文占比」的判据一个都抓不到，这条能兜住。
    #   阈值刻意压得很低（每分钟不到 15 个字词），正常语音远高于此，
    #   只有"整段没听懂"或"大段被丢弃"才会触发。
    if not warn and audio_seconds > 60 and units:
        per_min = units / (audio_seconds / 60.0)
        if per_min < 15:
            warn = True
            reason = (f"{audio_seconds/60:.1f} 分钟的素材只识别出 {units} 个字词"
                      f"（约 {per_min:.0f} 个/分钟，正常语音远高于此），"
                      "可能有整段内容没被识别出来。")
            suggestion = ("请先用音频播放器确认这段素材里确实有人声；"
                          "若有人声，建议换个引擎重跑对比（例如改用 Whisper）。")

    return {
        "ok": not warn,
        "warn": warn,
        "reason": reason,
        "suggestion": suggestion,
        "result_cjk_chars": cjk,
        "result_latin_words": latin,
        "result_cjk_ratio": round(ratio, 4),
        "expected_cjk_ratio": round(cjk_ratio, 4),
        "max_cjk_block": max_block,
        "units_per_minute": round(units / (audio_seconds / 60.0), 1) if audio_seconds else 0.0,
    }
