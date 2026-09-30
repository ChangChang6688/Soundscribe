"""声文 · 语言路由判定（方案 §6.6 的可执行实现）

两道防线：
  第一道  前段采样 → 统计中文字符占比 → 定初判
  第二道  整段转写后复校语言比例（见 postprocess.check_language_drift）

判定依据（实测校准）：
  CJK 占比 > 85%     → SenseVoice   中文术语更准、CPU 快 7.8 倍
  40% – 85%          → Whisper      中英成句交替
  < 40%              → Whisper      英文明显更准

★ 第三条依据：英文术语密度
  实测发现「中文为主但英文术语密集」的素材（行业授课/技术会议），
  单跑 SenseVoice 会丢掉一批英文术语（高频术语命中 23/33），
  而 Whisper 能全部抓到（33/33）。这类素材应标记为「建议双引擎比对」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
LATIN_WORD = re.compile(r"[A-Za-z]+")

THRESHOLD_CN = 0.85
THRESHOLD_MIX = 0.40
# 每 10 分钟出现多少个不重复英文词就提示「建议双引擎比对」
EN_TERM_DENSITY_HINT = 20
# 判定"纯英文"的中文占比上限：低于此值才允许用英文专用模型
THRESHOLD_EN_ONLY = 0.03

# ★ 各语言档位默认落到哪台引擎 —— 只在这里定义一次。
#   别处（例如"纠错规则是从哪台引擎的错误里总结的、现在还对不对得上"）
#   需要知道这个对应关系时，从这里取，不要再抄一份字面量。
ENGINE_FOR_ZH = "sensevoice"
ENGINE_FOR_EN_ONLY = "parakeet"
ENGINE_FOR_MIXED = "whisper"

# ★★ 判不出语言时用哪台引擎。
#   必须选**最通用**的那台（中英都能处理），绝不能落到英文专用模型上 ——
#   见 MIN_UNITS 的说明，这里踩过一个很严重的坑。
ENGINE_FOR_UNKNOWN = "whisper"

# ★★ 样本量下限：少于这么多「字 + 词」，就不足以对语言下结论。
#
#   为什么必须设这个下限（实测踩过的真事故）：
#     路由会先切一段采样丢给 Whisper 试跑，再统计中英文构成。
#     一旦采样失败（切空、素材过短、Whisper 输出为空），统计结果就是
#     cjk=0 / latin=0 / units=0，而 cjk_ratio 会被算成 **0.0** ——
#     于是 `r <= THRESHOLD_EN_ONLY` 成立，判定成「纯英文」，路由到 Parakeet。
#     **"没有数据"被当成了"数据表明是纯英文"**，而落点偏偏是最窄、
#     不认中文的那个模型 → 中文素材输出乱码或空白，还只报一句
#     "没有识别到任何语音内容"，完全指错方向。
#
#   实测触发条件极其普通：音频短于采样起点（30 秒）时采样必空。
#   实测 5.5 秒的中文素材 100% 命中此坑。
MIN_UNITS = 20

# ★★ 判错的方向是不对称的 —— 这是整个路由里最值得记住的一条设计原则。
#
#   · 「判成中文」几乎不会错：**出现了汉字，就说明素材里有中文**。
#     这是个单向、可靠的信号，样本少也一样成立（10 个汉字就够了）。
#   · 「判成纯英文」在样本少时**极不可靠**：采样这几十个字里没中文，
#     完全不代表一小时的素材里没中文 —— 事实上有素材前 60 秒恰好是纯英文、
#     后面全是中文（实测）。
#
#   所以门槛必须分开设：
#     有足够汉字      → 可以放行中文分支（哪怕总样本很少）
#     没有汉字且样本少 → **不下结论**，用通用引擎兜底
#
#   这一条直接决定短语音的体验：Eli 的产品定义是「短按一下记录一句说话」，
#   那种素材就几秒、也就十几个字。用统一阈值会把它们全推到兜底档
#   （Whisper 只有 11×，而 SenseVoice 有 35×），明明判得出来却慢了 3 倍。
MIN_CJK_FOR_ZH = 8


@dataclass
class RouteResult:
    engine: str                 # sensevoice | parakeet | moonshine | whisper
    reason: str
    cjk_ratio: float
    cjk_chars: int
    latin_words: int
    latin_unique: int
    units: int
    suggest_dual: bool          # 是否建议双引擎比对
    suggest_reason: str = ""
    # ★ 样本量是否足以支撑这次判定。False 表示"没判出来，用的是兜底引擎"——
    #   界面上要如实说明，不能把它当成一次正常判定。
    sufficient: bool = True

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cjk_ratio_pct"] = round(self.cjk_ratio * 100, 1)
        return d


def text_stats(text: str, audio_seconds: float = 0.0) -> dict:
    """统计中英文构成。

    ★ 实现坑（已踩过）：不要先把「标点 + 空白」一起删掉再数英文词——
      空格被删会让所有英文单词粘成一整坨字母，[A-Za-z]+ 只匹配到 1 个。
      正确做法是直接在原文上分别计数。
    """
    cjk = len(CJK.findall(text))
    latin = [w.lower() for w in LATIN_WORD.findall(text)]
    units = cjk + len(latin)
    ratio = cjk / units if units else 0.0
    density = 0.0
    if audio_seconds > 0:
        density = len(set(latin)) / (audio_seconds / 600.0)  # 每 10 分钟的不重复英文词数
    return {
        "cjk_chars": cjk,
        "latin_words": len(latin),
        "latin_unique": len(set(latin)),
        "units": units,
        "cjk_ratio": ratio,
        # ★ units=0 时 ratio 会被算成 0.0，而它看起来和"真的是纯英文"一模一样。
        #   调用方必须先看 sufficient，否则就会把「没数据」当成「是英文」。
        "sufficient": units >= MIN_UNITS,
        "en_term_density_per_10min": round(density, 1),
    }


def decide(stats: dict) -> RouteResult:
    r = stats["cjk_ratio"]
    units = stats.get("units", 0)
    cjk = stats.get("cjk_chars", 0)

    # ★★ 第一道闸：样本不够就**不下结论**，直接走通用引擎。
    #   这是整个路由里最关键的一个分支 —— 少了它，采样失败会被判成"纯英文"
    #   从而路由到不认中文的模型（实测事故，见 MIN_UNITS 的说明）。
    #
    #   ★ 但"够不够"要分方向看（见 MIN_CJK_FOR_ZH）：
    #     出现了足够多汉字 = 确定是中文素材，样本少也算数；
    #     一个汉字都没有且样本很少 = **什么都没证明**，不能当"纯英文"。
    sufficient = stats.get("sufficient", units >= MIN_UNITS)
    confirmed_zh = cjk >= MIN_CJK_FOR_ZH and r > THRESHOLD_CN
    if confirmed_zh:
        sufficient = True

    if not sufficient:
        why = (f"采样只得到 {units} 个字/词且其中没有足够中文，"
               "不足以排除「素材里其实有中文」→ 使用 Whisper"
               "（通用引擎，中英文都能处理；不从窄模型下手）")
        return RouteResult(
            engine=ENGINE_FOR_UNKNOWN, reason=why,
            cjk_ratio=r, cjk_chars=cjk,
            latin_words=stats["latin_words"], latin_unique=stats["latin_unique"],
            units=units, suggest_dual=False, sufficient=False,
        )

    if r > THRESHOLD_CN:
        engine, reason = ENGINE_FOR_ZH, "中文为主 → SenseVoice（中文术语更准、CPU 快 7.8 倍）"
    elif r >= THRESHOLD_MIX:
        engine, reason = ENGINE_FOR_MIXED, "中英成句交替 → Whisper（英文术语更稳）"
    elif r <= THRESHOLD_EN_ONLY:
        # ★ 纯英文：改用英文专用小模型。实测 CPU 上 25×（1 小时 2.4 分钟），
        #   而 Whisper 在 CPU 上只有 4.7×（12.7 分钟）；质量词级 99.2% 一致。
        #   ★★ 门槛卡在"几乎没有中文"上：Parakeet/Moonshine 不认识中文，
        #      只要有一点中文就会把它整段丢掉，所以不能按"英文为主"就放行。
        engine, reason = ENGINE_FOR_EN_ONLY, (
            "纯英文 → Parakeet（英文专用，CPU 上快 5 倍；不认中文，含中文素材请改选 Whisper）")
    else:
        engine, reason = ENGINE_FOR_MIXED, "英文为主但含少量中文 → Whisper（Parakeet 会丢中文）"

    # 第三条依据：英文术语密度
    suggest_dual = False
    suggest_reason = ""
    if engine == "sensevoice" and stats.get("en_term_density_per_10min", 0) >= EN_TERM_DENSITY_HINT:
        suggest_dual = True
        suggest_reason = (
            f"检测到英文术语密度较高（每 10 分钟约 {stats['en_term_density_per_10min']:.0f} 个不重复英文词）。"
            "实测此形态下单跑 SenseVoice 会丢掉部分英文术语，建议开启双引擎比对。"
        )

    return RouteResult(
        engine=engine, reason=reason,
        cjk_ratio=r,
        cjk_chars=stats["cjk_chars"],
        latin_words=stats["latin_words"],
        latin_unique=stats["latin_unique"],
        units=stats["units"],
        suggest_dual=suggest_dual,
        suggest_reason=suggest_reason,
    )


def resolve(preferred: str, installed: set[str]) -> tuple[str, str]:
    """把「理想引擎」解析成「实际可用的引擎」，返回 (engine, 补充说明)。

    ★ 为什么要有这一步：路由是纯函数（只看统计量），它不知道哪个模型装没装。
      如果直接把 parakeet 丢给上层，而用户没下载那个模型，任务会直接失败。
      所以在这里做一次可用性回退，并把回退原因带回界面上。

    ★ 回退的优先级不是随意的：**越通用的引擎越优先**。
      多语种模型（Whisper / SenseVoice）排在英文专用模型之前 ——
      因为回退的本质是"信息不足时别赌"，赌一个只认英文的模型风险最大。
    """
    if preferred in installed:
        return preferred, ""

    # 回退链：英文专用的两个模型互相兜底，最终落到 Whisper
    if preferred == "parakeet":
        if "moonshine" in installed:
            return "moonshine", "Parakeet 模型未安装，已改用 Moonshine（同为英文专用）"
        if "whisper" in installed:
            return "whisper", "英文专用模型未安装，已回退到 Whisper（建议去模型中心下载）"
    if preferred == "moonshine":
        if "parakeet" in installed:
            return "parakeet", "Moonshine 模型未安装，已改用 Parakeet（同为英文专用）"
        if "whisper" in installed:
            return "whisper", "英文专用模型未安装，已回退到 Whisper（建议去模型中心下载）"
    if preferred == "sensevoice" and "whisper" in installed:
        return "whisper", "SenseVoice 模型未安装，已回退到 Whisper（建议去模型中心下载）"

    # ★ Whisper 是兜底档（ENGINE_FOR_UNKNOWN），它自己没装时也要能落地，
    #   否则"样本不足"这种常见情况会直接把任务顶失败。
    #   这里选 SenseVoice 而不是英文专用模型：它支持 5 种语言，
    #   在"不知道素材是什么语言"的前提下是更安全的选择。
    if preferred == "whisper":
        for alt in ("sensevoice", "parakeet", "moonshine"):
            if alt in installed:
                return alt, (f"Whisper 模型未安装，已回退到 {alt}"
                             "（建议去模型中心下载 Whisper，它是中英都能处理的通用引擎）")
    return preferred, ""
