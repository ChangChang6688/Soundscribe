"""声文 · 模型推荐（按机器画像给模型打分）

★ 为什么需要这个模块：
  同一个模型在不同机器上"值不值得下"完全不同。举例：
    · Whisper large-v3 在 8GB 显存的 Windows 上很合适；
      但在 Mac 上只能跑 CPU（CTranslate2 没有 Metal 后端），1 小时录音要十几分钟。
    · Parakeet 在 CPU 上就有 25 倍速，不需要显卡 —— 但只认英文。
  把这些差异丢给用户自己判断是不现实的。所以由程序按**机器画像**（系统 / 芯片 /
  显存 / 内存 / 核数）算出推荐，并把理由讲出来。

★ 设计原则：
  1. **推荐要说明理由**，不能只给一个分数 —— 用户得知道"为什么是你"。
  2. **按用途分组**（中文主力 / 英文专用 / 通用备选），因为"最合适的模型"
     取决于你要转什么语言，单给一个全局冠军会误导只做中文或只做英文的人。
  3. 打分只影响**排序与徽标**，绝不隐藏任何模型 —— 用户始终可以自己选。
"""

from __future__ import annotations

# 用途分组：让用户按语言挑，而不是面对一个大列表
GROUP_CN = "中文主力"
GROUP_EN = "英文专用"
GROUP_GENERAL = "通用 / 备选"

ENGINE_GROUP = {
    "sensevoice": GROUP_CN,
    "parakeet": GROUP_EN,
    "moonshine": GROUP_EN,
    "whisper": GROUP_GENERAL,
}


def machine_profile(env: dict) -> dict:
    """从环境报告里抽出打分需要的字段。"""
    gpu = env.get("gpu") or {}
    return {
        "os": str(env.get("os", "")),
        "is_mac": "Darwin" in str(env.get("os", "")) or "macOS" in str(env.get("os", "")),
        "gpu_name": gpu.get("name") if gpu.get("available") else "",
        "vram_mib": int(gpu.get("vram_mib") or 0),
        "ram_gb": float(env.get("ram_gb") or 0),
        "cores": int(env.get("cpu_cores") or 0),
        "tier": env.get("tier", ""),
    }


# 同一体量档内的偏好。这是**我们自己模型线上的判断**，不是通用规律 ——
# "large" 这一档有四五个型号，只按体积打分它们会全部并列，
# 用户看到的就是"排序没意义"。所以把差别写清楚。
WITHIN_CLASS: dict[str, tuple[int, str]] = {
    "whisper-large-v3-turbo": (+4, "速度与质量平衡最好，日常首选"),
    "whisper-large-v3": (-3, "精度最高但最慢，只在需要极限精度时选"),
    "distil-large-v3.5": (+1, "蒸馏版，纯英文批量转写更快"),
    "whisper-medium": (-2, "介于大小之间，定位不突出"),
    "whisper-small": (0, ""),
    "whisper-base": (0, ""),
}


def _size_class(size_mb: float) -> str:
    if size_mb >= 1400:
        return "large"
    if size_mb >= 400:
        return "mid"
    return "small"


def score(m: dict, prof: dict) -> tuple[int, list[str]]:
    """给单个模型打分，返回 (分数, 理由列表)。分数仅供排序。"""
    eng = m.get("engine", "")
    size = float(m.get("size_mb") or 0)
    klass = _size_class(size)
    pts = 50
    why: list[str] = []

    # ── 1. 用途基础分 ──
    if eng == "sensevoice":
        pts += 20
        why.append("中文术语最准，且 CPU 上就有 35–43 倍速（不需要显卡）")
    elif eng in ("parakeet", "moonshine"):
        pts += 12
        why.append("英文专用：CPU 上约 23–25 倍速，比 Whisper 快约 5 倍")
        why.append("不认中文，只适合纯英文素材")
    elif eng == "whisper":
        if klass == "large":
            why.append("多语种 + 可翻译，但模型大、吃资源")
        else:
            why.append("体积小，适合配置一般的机器")

    # ── 2. 模型体量与显存是否匹配 ──
    if prof["vram_mib"] >= 6000:
        if klass == "large":
            pts += 18
            why.append(f"你的显卡显存 {round(prof['vram_mib'] / 1024, 1)} GB，跑大模型没压力")
        elif klass == "small":
            pts -= 4
    elif prof["vram_mib"] >= 4000:
        if klass == "mid":
            pts += 14
            why.append("显存 4–6 GB，中等体积模型最合适")
        elif klass == "large":
            pts -= 8
            why.append("显存偏紧，大模型可能跑不动或需要降精度")
    else:
        if klass == "large":
            pts -= 22
            why.append("没有可用显卡，大模型在 CPU 上会非常慢")
        elif klass == "small":
            pts += 10

    # ── 3. macOS 特判：Whisper 只能走 CPU ──
    if prof["is_mac"]:
        if eng == "whisper" and klass == "large":
            pts -= 18
            why.append("macOS 上 Whisper 只能用 CPU（CTranslate2 无 Metal），"
                       "大模型 1 小时录音要 10 分钟以上")
        elif eng in ("parakeet", "moonshine", "sensevoice"):
            pts += 8
            why.append("ONNX 系模型在 Apple 芯片上不受影响，CPU 就够快")

    # ── 4. 内存 ──
    if prof["ram_gb"] and prof["ram_gb"] < 16:
        if klass == "large":
            pts -= 12
            why.append(f"内存 {prof['ram_gb']:.0f} GB，跑大模型会比较吃力")
        elif klass == "small":
            pts += 6
    elif prof["ram_gb"] >= 32 and klass != "small":
        pts += 4

    # ── 5. CPU 核数 ──
    if prof["cores"] and prof["cores"] <= 4:
        if klass == "large":
            pts -= 8
            why.append(f"CPU 只有 {prof['cores']} 核，大模型会更慢")
        elif klass == "small":
            pts += 6
            why.append(f"CPU 只有 {prof['cores']} 核，建议选体积小的")

    # ── 6. 同档内偏好 ──
    bonus, note = WITHIN_CLASS.get(m.get("id", ""), (0, ""))
    if bonus:
        pts += bonus
    if note:
        why.append(note)

    # ── 7. 已安装的略加分：省一次下载 ──
    # ★ 注意字段名：`state` 是**可用性门禁状态**（ok / risky / blocked / planned），
    #   不是安装状态。安装状态在 `installed`（bool）+ `install_state`（三态字符串）。
    #   混用会导致"已装却显示待下载"这种不一致 —— 踩过一次。
    if m.get("installed") or m.get("install_state") == "installed":
        pts += 3

    return max(0, min(100, pts)), why


def rank(models: list[dict], env: dict) -> dict:
    """给全部模型打分并排序，同时给出「中文推荐 / 英文推荐」两个结论。

    返回 {"models": [...], "picks": {...}, "profile": {...}, "summary": str}
    """
    prof = machine_profile(env)
    scored: list[dict] = []
    for m in models:
        pts, why = score(m, prof)
        item = dict(m)
        item["group"] = ENGINE_GROUP.get(m.get("engine", ""), GROUP_GENERAL)
        item["score"] = pts
        item["why"] = why
        scored.append(item)

    # 排序：按用途分组，组内按分数降序。这样"中文/英文/备选"三块各自从优到劣。
    order = {GROUP_CN: 0, GROUP_EN: 1, GROUP_GENERAL: 2}
    scored.sort(key=lambda x: (order.get(x["group"], 9), -x["score"], x.get("priority", 99)))

    # 组内第一名标「为你推荐」（跳过 planned：还没接入的模型不该被推荐）
    best_per_group: dict[str, str] = {}
    for it in scored:
        g = it["group"]
        if g not in best_per_group and it.get("state") != "planned":
            best_per_group[g] = it["id"]
    for it in scored:
        it["recommended"] = best_per_group.get(it["group"]) == it["id"]

    # 两个明确结论：中文用哪个、英文用哪个
    def _pick(group: str) -> dict | None:
        for it in scored:
            if it["group"] == group and it.get("state") != "planned":
                return {"id": it["id"], "name": it["name"], "why": it["why"][:1],
                        # ★ 用 installed 字段，不是 state
                        "installed": bool(it.get("installed")
                                          or it.get("install_state") == "installed")}
        return None

    picks = {"中文素材": _pick(GROUP_CN), "英文素材": _pick(GROUP_EN)}

    bits = []
    if prof["tier"]:
        bits.append(f"{prof['tier']} 档")
    if prof["gpu_name"]:
        bits.append(f"{prof['gpu_name']} {round(prof['vram_mib'] / 1024)}GB")
    elif prof["is_mac"]:
        bits.append("Apple 芯片")
    else:
        bits.append("无独立显卡")
    if prof["cores"]:
        bits.append(f"{prof['cores']} 线程")
    summary = "检测到：" + " · ".join(bits)

    return {"models": scored, "picks": picks, "profile": prof, "summary": summary}
