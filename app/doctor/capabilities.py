"""声文 · 能力矩阵（唯一事实来源）

★★ 为什么需要这个文件

本项目反复出现同一类 bug：**设置项看起来能点，实际根本没生效**。
一天之内查出 6 个，根因不是粗心，而是**「能力信息没有单一事实来源」**：
「这个设置对哪些引擎生效」这个知识，原本散落在四个地方 ——

  ① 前端界面上的手写说明文字（比如"仅对 Whisper 生效"）
  ② jobs.py 里的 if / else 分支（决定实际传什么参数）
  ③ 引擎类的 stream() 签名（`**_` 会把不认识的参数**静默吃掉**，不报错）
  ④ 注册表与注释里的零散记录

四处只要有一处不同步，就会出现「界面说有 / 后端没传 / 引擎也不支持」
这种组合。更糟的是：**没有任何机制能发现这种不同步** ——
只有用户碰巧问"这个真的生效吗"才会暴露。

★ 收敛成一张表之后，这张表必须**可执行**，而不只是结论。
  所以每一项能力都带三样东西：

    injects    → 这个值要传成哪几个参数、传到构造期还是推理期、要不要变形
    evidence   → 这个引擎跑起来之后，从哪儿能看出它**真的用了**这个值
    always     → 与用户设置无关、该引擎恒定要带的参数

  有了它，三件事全部由表驱动，且**必然一致**（读的是同一张表）：

    · 后端传参   `build_engine_kwargs()` 走表分发，不再 if/else 硬编码
    · 前端文案   `hint()` / `matrix_for_ui()` 生成"生效范围"，不再手打
    · 自动验证   `tools/effectiveness_check.py` 三层核验：
                 ① 表自身无矛盾（`audit()`）
                 ② 表里声明的参数名，真的出现在引擎签名里（静态）
                 ③ 真实跑一遍，从 evidence 读取到"确实生效了"（运行时）

★ 维护约定（新增能力 / 新增引擎时照做，不改别处代码）
  1. 在 CAPABILITIES 里加一项，对**每个引擎**表一个态（漏写会被 audit 抓出来）
  2. 声明 supported 就**必须**给出 injects + evidence，否则 audit 直接失败 ——
     这条规则的作用是把"我接了"变成"我能证明我接了"
  3. 引擎实现里把 evidence 对应的键写进 `last_info`，运行时核验才有依据
  4. 别在前端再写死"仅 X 生效"，改成读 /api/capabilities
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 便于直接 `python app/doctor/capabilities.py` 跑自检（其余模块也这么做）
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── 支持等级 ──────────────────────────────────────────────
SUPPORTED = "supported"          # 引擎原生支持，且**已接入**
NATIVE_NOT_WIRED = "not_wired"   # 引擎支持，但我们还没接上（这是个待办，不是缺陷）
UNSUPPORTED = "unsupported"      # 引擎本身没有这个能力
N_A = "n_a"                      # 与引擎无关（后处理 / 音频预处理 / 导出层面）

LEVEL_LABEL = {
    SUPPORTED: "生效",
    NATIVE_NOT_WIRED: "引擎支持但未接入",
    UNSUPPORTED: "该引擎不支持",
    N_A: "与引擎无关",
}

ALL_ENGINES = ["sensevoice", "whisper", "parakeet", "moonshine"]
ENGINE_NAME = {
    "sensevoice": "SenseVoice", "whisper": "Whisper",
    "parakeet": "Parakeet", "moonshine": "Moonshine",
}


# ═══════════════════════════════════════════════════════════
# 描述"怎么把这个值传进去"
# ═══════════════════════════════════════════════════════════

@dataclass
class Inject:
    point: str                 # "ctor"（构造期） | "stream"（推理期）
    name: str                  # 目标参数名
    kind: str = "raw"          # 值怎么变形，见 ADAPTERS
    # ★ src 必须显式写（除 kind=const）。留空会默认取能力 id ——
    #   实测踩过：batch_size 忘记写 src，于是拿 batched=True 去 int() 得到了 1，
    #   参数名对、位置对、不报错，只是值是错的 —— 又一类"静默传错参"。
    #   audit() 会强制这一条。
    src: str = ""
    value: Any = None          # kind="const" 时用的固定值


@dataclass
class Support:
    level: str
    why: str = ""                                   # 给用户看的解释
    injects: list[Inject] = field(default_factory=list)   # 依赖用户设置值
    always: list[Inject] = field(default_factory=list)    # 该引擎恒定要带的参数
    # 证据键：引擎跑起来之后，`last_info[evidence]` 里必须能看到"这个值真的被用了"。
    #
    # ★★ 它为什么是不可省的：没有它，`supported` 只是一句口头承诺 ——
    #    参数传进去了，引擎内部却可能完全没用（实测踩过：Parakeet 的热词因为缺
    #    bpe_vocab，16 个词全被静默跳过；Python 侧零异常、last_info 也看不出问题）。
    #
    # ★ 配套约定：**值进到引擎里之后又被吃掉多少，也要写出来** ——
    #   把数量放进 `last_info[f"{evidence}_dropped"]`，被剔除的具体值放
    #   `last_info[f"{evidence}_dropped_words"]`（可选）。
    #   上层会自动据此告警，所以新增能力时只要遵守这个命名就不必再改告警代码。
    evidence: str = ""

    def __post_init__(self) -> None:
        # 允许写成 dict，读起来紧凑些；统一归一化成 Inject
        self.injects = [_as_inject(x) for x in self.injects]
        self.always = [_as_inject(x) for x in self.always]


def _as_inject(x) -> Inject:
    if isinstance(x, Inject):
        return x
    if isinstance(x, dict):
        return Inject(**{k: v for k, v in x.items() if k in Inject.__dataclass_fields__})
    raise TypeError(f"不是合法的注入规格：{x!r}")


@dataclass
class Capability:
    id: str
    label: str
    desc: str                                      # 一句话说清这个能力是什么
    scope: str                                     # engine | post | prep | export
    by_engine: dict[str, Support] = field(default_factory=dict)
    ui_note: str = ""                              # 界面上额外要说明的
    injectable: bool = True                        # False = 它本身是选择项（如"用哪个引擎"），
                                                   #   没有"用户值要注入"这回事（但可以有 always）

    # ---------- 查询辅助 ----------
    def engines_with(self, level: str) -> list[str]:
        return [e for e, s in self.by_engine.items() if s.level == level]

    def hint(self) -> str:
        """生成给界面用的"生效范围"文案 —— 前端不再手写这一类说明。"""
        if self.scope == "post":
            return "全部引擎生效（在识别之后处理，与引擎无关）"
        if self.scope in ("prep", "export"):
            return "全部引擎生效"
        ok = [ENGINE_NAME[e] for e in self.engines_with(SUPPORTED)]
        no = [ENGINE_NAME[e] for e in self.engines_with(UNSUPPORTED)]
        pend = [ENGINE_NAME[e] for e in self.engines_with(NATIVE_NOT_WIRED)]
        if not ok:
            return "暂不可用"
        t = "生效：" + "、".join(ok)
        if pend:
            t += "；" + "、".join(pend) + " 引擎支持但尚未接入"
        if no:
            t += "；" + "、".join(no) + " 不支持"
        return t

    def hint_short(self) -> str:
        """胶囊标签用的短文案。"""
        if self.scope in ("post", "prep", "export"):
            return "全部引擎生效"
        ok = [ENGINE_NAME[e] for e in self.engines_with(SUPPORTED)]
        if not ok:
            return "暂不可用"
        if len(ok) == len(ALL_ENGINES):
            return "全部引擎生效"
        if len(ok) == 1:
            return f"仅 {ok[0]} 引擎生效"
        return "生效：" + "、".join(ok)


def _all(level: str, why: str = "", **kw) -> dict[str, Support]:
    return {e: Support(level, why, **kw) for e in ALL_ENGINES}


def _mix(**kw) -> dict[str, Support]:
    """_mix(whisper=SUPPORTED, sensevoice=UNSUPPORTED) —— 未列出的按 unsupported 记。"""
    out = {}
    for e in ALL_ENGINES:
        v = kw.get(e, UNSUPPORTED)
        if isinstance(v, Support):
            out[e] = v
        elif isinstance(v, tuple):
            out[e] = Support(v[0], v[1] if len(v) > 1 else "")
        else:
            out[e] = Support(v)
    return out


# ═══════════════════════════════════════════════════════════
# 值适配器：同一个设置值，不同引擎需要不同形态
#
# ★ 这一节是"两个入口必须共享同一状态"那条教训的推广：
#   热词这份数据只有一份，Whisper 需要的是**提示词文本**，
#   Parakeet 需要的是**词表文件**。以前是各写各的、于是只有一个接上。
#   现在只在适配器里分叉一次，来源是同一个 dict。
# ═══════════════════════════════════════════════════════════

_LATIN = re.compile(r"[A-Za-z]")
HOTWORD_LIMIT = 120


def _flat(groups: Any) -> list[str]:
    if isinstance(groups, dict):
        items: list[str] = []
        for v in groups.values():
            items += [str(x) for x in (v or [])]
        return items
    if isinstance(groups, str):
        return [s.strip() for s in groups.splitlines()]
    if isinstance(groups, (list, tuple, set)):
        return [str(x) for x in groups]
    return []


def _dedup(items: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in items:
        k = t.strip()
        if not k or k.lower() in seen:
            continue
        seen.add(k.lower())
        out.append(k)
        if len(out) >= limit:
            break
    return out


def _adapt_prompt(raw: Any, ctx: dict) -> str:
    """热词 → Whisper 的 initial_prompt（整句形式比纯词表效果好）。"""
    groups = raw if isinstance(raw, dict) else {"zh.all": _flat(raw)}
    from app.pipeline import postprocess       # 延迟导入，避免 doctor ↔ pipeline 循环引用

    return postprocess.build_whisper_prompt(groups, ctx.get("lang", "zh"))


def _adapt_hotwords_file(raw: Any, ctx: dict) -> list[str]:
    """热词 → 英文专用引擎的**原生热词表**。

    ★ 必须只保留含拉丁字母的词：Parakeet / Moonshine 是英文模型，
      把中日韩词喂进去只会占位、不可能匹配（实测把中文喂给 Parakeet 会输出乱码拼音）。
      这不是"静默丢弃"——被滤掉多少会在 explain_ignored / 日志里说清楚。
    """
    groups = raw if isinstance(raw, dict) else {"en.all": _flat(raw)}
    pref = "en."
    terms = [t for k, v in groups.items() if k.startswith(pref) for t in (v or [])]
    if not terms:                                  # 用户没分组时，全量里挑拉丁词
        terms = _flat(groups)
    terms = [str(t) for t in terms if _LATIN.search(str(t))]
    return _dedup(terms, HOTWORD_LIMIT)


def _adapt_int(v: Any, ctx: dict) -> int:
    """★ 拒绝布尔：True 会被 int() 悄悄变成 1，看起来像"设进去了其实拿错了值"。"""
    if isinstance(v, bool):
        raise ValueError("int 型参数收到了布尔值 —— 多半是 Inject.src 指错了来源")
    return int(v)


ADAPTERS = {
    "raw": lambda v, ctx: v,
    "const": lambda v, ctx: v,
    "bool": lambda v, ctx: bool(v),
    "int": _adapt_int,
    "str": lambda v, ctx: str(v),
    "prompt": _adapt_prompt,
    "hotwords_file": _adapt_hotwords_file,
}

# 空值判定：这些形态视为"用户没设"，直接不传
_EMPTY = (None, "", 0, [], {}, ())


def is_set(v: Any) -> bool:
    if isinstance(v, bool):
        return True               # False 是有效值，不能当空
    return not (v is None or v in _EMPTY or v == [] or v == {})


# ═══════════════════════════════════════════════════════════
# ★★ 能力矩阵本体
#   每一行都要如实反映**实测过的真实情况**，不能写"应该可以"。
# ═══════════════════════════════════════════════════════════

CAPABILITIES: list[Capability] = [

    Capability(
        id="engine", label="识别引擎", scope="engine",
        desc="这一段素材用哪个模型来识别。逐文件选择，默认按语言自动判定。",
        # ★ 它本身就是"选哪台引擎"，没有用户值要注入；但每个引擎恒定要带
        #   语言参数（Whisper 让它自己判、sherpa 家族写 auto），所以有 always。
        by_engine={
            "whisper": Support(SUPPORTED, "faster-whisper，语言全自动判定",
                               always=[{"point": "stream", "name": "language",
                                        "kind": "const", "value": None}]),
            "sensevoice": Support(SUPPORTED, "sherpa-onnx，多语种自动",
                                  always=[{"point": "stream", "name": "language",
                                           "kind": "const", "value": "auto"}]),
            "parakeet": Support(SUPPORTED, "sherpa-onnx NeMo transducer，英文",
                                always=[{"point": "stream", "name": "language",
                                         "kind": "const", "value": "auto"}]),
            "moonshine": Support(SUPPORTED, "sherpa-onnx Moonshine，英文",
                                 always=[{"point": "stream", "name": "language",
                                          "kind": "const", "value": "auto"}]),
        },
        injectable=False,
        ui_note="自动判定会按语言路由：中文→SenseVoice、纯英文→Parakeet、"
                "中英夹杂→Whisper。也可逐文件手动指定。",
    ),

    Capability(
        id="hotwords", label="领域热词", scope="engine",
        desc="把公司名/人名/行业术语喂给引擎的识别阶段，让模型更容易听对。",
        # 同一份热词，两个引擎吃两种形态 —— 由 ADAPTERS 分叉，来源是同一个 dict
        by_engine={
            "whisper": Support(
                SUPPORTED, "拼成 initial_prompt 注入推理期",
                injects=[{"point": "stream", "name": "initial_prompt", "kind": "prompt",
                          "src": "hotwords"}],
                evidence="initial_prompt_chars",
            ),
            "parakeet": Support(
                SUPPORTED, "NeMo transducer 原生热词加权（装载期，需 beam search）",
                injects=[{"point": "ctor", "name": "hotwords", "kind": "hotwords_file",
                          "src": "hotwords"}],
                evidence="hotwords",
            ),
            # 实测：SenseVoice 没有提示词接口；hr_lexicon 单独给词表不生效（需额外 dict/FST 资源）
            "sensevoice": Support(UNSUPPORTED, "该模型没有提示词/热词接口"),
            "moonshine": Support(UNSUPPORTED, "该模型没有提示词/热词接口"),
        },
        ui_note="热词作用在识别阶段，需要引擎支持。中文默认走的 SenseVoice 不支持 → "
                "中文场景请改用「纠错规则表」，那个一定生效。",
    ),

    Capability(
        id="corrections", label="纠错规则表", scope="post",
        desc="识别完成后逐句做文字替换（同音字、专名、固定错法）。",
        by_engine={},
        ui_note="因为它作用在识别之后，所以与引擎无关，改一条就全引擎生效 —— "
                "这是「想让某个词一定对」最可靠的手段。",
    ),

    Capability(
        id="vad", label="人声检测（VAD）", scope="engine",
        desc="跳过静音段，提速并抑制静音处的幻觉。",
        by_engine={
            "whisper": Support(SUPPORTED, "faster-whisper 的 vad_filter",
                               injects=[{"point": "stream", "name": "vad", "kind": "bool",
                                         "src": "vad"}],
                               evidence="vad"),
            # sherpa 三个引擎必须按静音切段工作（单段 >30 秒会静默掉字），
            # 所以这个开关对它们没有意义 —— 不是没接，是引擎不支持
            "sensevoice": Support(UNSUPPORTED, "必须按静音切段工作（切段是正确性前提）"),
            "parakeet": Support(UNSUPPORTED, "同上"),
            "moonshine": Support(UNSUPPORTED, "同上"),
        },
        ui_note="SenseVoice / Parakeet / Moonshine 必须按静音切段，"
                "单段超过 30 秒会静默掉字，所以它们不受这个开关影响。",
    ),

    Capability(
        id="normalize", label="响度归一化", scope="prep",
        desc="抽音轨时统一响度，录音忽大忽小时能改善稳定性。",
        by_engine={},
        ui_note="在抽音轨阶段完成，对所有引擎生效。",
    ),

    Capability(
        id="timeline", label="时间轴", scope="export",
        desc="是否给每句带上起止时间，决定能否导出 SRT / VTT。",
        by_engine={},
        ui_note="段级时间戳由切段边界给出，对所有引擎生效。"
                "「词级」暂未开放：识别结果里已带字级时间戳，但导出环节还没接上。",
    ),

    Capability(
        id="batched", label="批处理", scope="engine",
        desc="一次推理多条，显著提速。需搭配 VAD。",
        by_engine={
            "whisper": Support(
                SUPPORTED, "faster-whisper 的 BatchedInferencePipeline",
                injects=[{"point": "stream", "name": "batched", "kind": "bool",
                          "src": "batched"},
                         {"point": "stream", "name": "batch_size", "kind": "int",
                          "src": "batch_size"}],
                evidence="batched",
            ),
            "sensevoice": Support(UNSUPPORTED, "sherpa-onnx 逐段解码"),
            "parakeet": Support(UNSUPPORTED, "sherpa-onnx 逐段解码"),
            "moonshine": Support(UNSUPPORTED, "sherpa-onnx 逐段解码"),
        },
    ),

    Capability(
        id="compute_type", label="量化档（computation type）", scope="engine",
        desc="FP16 / INT8 等精度选择，影响速度与显存。",
        by_engine={
            "whisper": Support(SUPPORTED, "CTranslate2 的 compute_type",
                               injects=[{"point": "ctor", "name": "compute_type", "kind": "str",
                                         "src": "compute_type"}],
                               evidence="compute_type"),
            "sensevoice": Support(UNSUPPORTED, "模型已固定为 int8 量化档"),
            "parakeet": Support(UNSUPPORTED, "同上"),
            "moonshine": Support(UNSUPPORTED, "同上"),
        },
    ),

    Capability(
        id="low_confidence", label="低置信标注", scope="post",
        desc="给引擎没把握的句子加底纹，提示重点核对。",
        by_engine={},
        ui_note="⚠ 当前所有引擎都拿不到置信度，这个标注实际不产出。"
                "（Whisper 走 CT2 有 avg_logprob，但默认的中文路径是 sherpa-onnx，"
                "其 ys_log_probs 为空、avg_logprob 恒为 0）",
    ),

    Capability(
        id="needs_review", label="待复核标注", scope="post",
        desc="识别结果出现重复退化时标出来，交人工看一眼。",
        by_engine={},
        ui_note="它是低置信标注缺位后的兜底：机器不敢替你决定的地方，标出来让你判断。",
    ),

    Capability(
        id="hallucination_filter", label="幻觉过滤", scope="post",
        desc="滤掉纯噪声段（例如整段只有标点）。",
        by_engine={},
    ),
]

BY_ID = {c.id: c for c in CAPABILITIES}


# ═══════════════════════════════════════════════════════════
# 由矩阵驱动：构建引擎参数
# ═══════════════════════════════════════════════════════════

def build_engine_kwargs(engine: str, values: dict[str, Any],
                        ctx: dict | None = None) -> tuple[dict, dict]:
    """按矩阵把设置值翻译成"构造参数"与"推理参数"。

    ★ 这是这个模块存在的核心理由：
      「界面说支持什么」由此函数实际传什么，**必然一致** —— 因为读的是同一张表。
      以前是前端手写说明 + 后端 if/else 各写一份，两边一定会漂移。

    values：能力 id → 值。只放进来的才可能被传，未知 key 一律忽略。
    ctx   ：适配器需要的上下文（如 lang）。

    返回 (ctor_kwargs, stream_kwargs)。
    """
    ctx = ctx or {}
    ctor: dict[str, Any] = {}
    stream: dict[str, Any] = {}

    def put(point: str, name: str, val: Any) -> None:
        (ctor if point == "ctor" else stream)[name] = val

    for cap in CAPABILITIES:
        if cap.scope != "engine":
            continue
        sup = cap.by_engine.get(engine)
        if sup is None or sup.level != SUPPORTED:
            continue                       # 不支持 → 明确不传，而不是让它被静默吞掉

        # 1) 恒定参数（与用户设置无关，例如语言）
        for inj in sup.always:
            put(inj.point, inj.name, inj.value)

        # 2) 用户设置值
        if not cap.injectable:
            continue
        for inj in sup.injects:
            if inj.kind == "const":
                put(inj.point, inj.name, inj.value)
                continue
            if not inj.src:
                continue                   # audit 会拦住；这里只是兜底，绝不猜来源
            if inj.src not in values:
                continue
            raw = values[inj.src]
            if not is_set(raw):
                continue                   # 空提示词/空词表没有意义，别传
            fn = ADAPTERS.get(inj.kind)
            if fn is None:                 # audit 会拦住这种情况，这里是兜底
                continue
            put(inj.point, inj.name, fn(raw, ctx))

    return ctor, stream


def explain_ignored(engine: str, values: dict[str, Any]) -> list[dict]:
    """列出"用户设了、但这个引擎不会生效"的能力 —— 用于如实告知，而不是假装生效。

    这是"不给用户无效开关"原则的落实：设了没用的东西，要主动说出来。
    """
    out: list[dict] = []
    for cap in CAPABILITIES:
        if cap.scope != "engine" or cap.id not in values or not is_set(values[cap.id]):
            continue
        sup = cap.by_engine.get(engine)
        if sup and sup.level == SUPPORTED:
            continue
        out.append({
            "id": cap.id, "label": cap.label,
            "level": sup.level if sup else UNSUPPORTED,
            "why": (sup.why if sup else "") or "该引擎不支持",
        })
    return out


def ignored_message(engine: str, values: dict[str, Any]) -> str:
    """把被忽略的设置拼成一句人话（给前端告警用）。"""
    items = explain_ignored(engine, values)
    if not items:
        return ""
    names = "、".join(i["label"] for i in items)
    return (f"当前引擎 {ENGINE_NAME.get(engine, engine)} 不支持：{names}。"
            "这些设置本次不会生效，已在下方列出原因。")


def adaptation_notes(engine: str, values: dict[str, Any]) -> list[dict]:
    """值在适配过程中"缩水"了的话，如实说出来。

    ★ 为什么必须有这个：Parakeet 是英文专用模型，中文热词喂进去只会占位、不可能匹配，
      所以适配器会滤掉。但"滤掉"如果不说，用户就会觉得"我明明加了为什么不生效" ——
      跟以前那批 bug 是同一种伤害。宁可说清楚"过滤了 61 个非拉丁词"。
    """
    notes: list[dict] = []
    for cap in CAPABILITIES:
        if cap.scope != "engine" or not cap.injectable:
            continue
        sup = cap.by_engine.get(engine)
        if sup is None or sup.level != SUPPORTED:
            continue
        for inj in sup.injects:
            if inj.kind != "hotwords_file" or inj.src not in values:
                continue
            raw = values[inj.src]
            total = len(_dedup(_flat(raw), 10 ** 6))
            kept = len(_adapt_hotwords_file(raw, {}))
            if total and kept < total:
                notes.append({
                    "id": cap.id, "label": cap.label,
                    "dropped": total - kept, "kept": kept, "total": total,
                    "message": f"{ENGINE_NAME.get(engine, engine)} 是英文专用模型，"
                               f"已滤掉 {total - kept} 个不含拉丁字母的条目"
                               f"（保留 {kept} 个）。中文词请改放纠错规则表。",
                })
    return notes


def usage_profile(engine: str) -> dict:
    """这台引擎实际上支持哪些**用户可设**的项 —— 供界面在选引擎时就提示。

    ★ 与 explain_ignored 的区别：那个是"你已经设了、但这次不生效"（事后），
      这个是"选它之前你就该知道的"（事前）。两件事都要有：
      事后告知是补救，事前告知才是设计。
    """
    ok, off = [], []
    for c in CAPABILITIES:
        if c.scope != "engine" or not c.injectable:
            continue
        s = c.by_engine.get(engine)
        if s is None:
            continue
        (ok if s.level == SUPPORTED else off).append({
            "id": c.id, "label": c.label, "level": s.level, "why": s.why,
        })
    return {"engine": engine, "name": ENGINE_NAME.get(engine, engine),
            "works": ok, "no_effect": off}


def matrix_for_ui() -> list[dict]:
    """给前端的完整矩阵（界面上的"生效范围"说明由它生成，不再手写）。"""
    return [{
        "id": c.id, "label": c.label, "desc": c.desc, "scope": c.scope,
        "hint": c.hint(), "hint_short": c.hint_short(),
        "ui_note": c.ui_note,
        "by_engine": {e: {"level": s.level, "label": LEVEL_LABEL[s.level],
                          "why": s.why, "name": ENGINE_NAME.get(e, e)}
                      for e, s in c.by_engine.items()},
    } for c in CAPABILITIES]


def audit() -> list[dict]:
    """自检：矩阵本身是否自相矛盾。

    检查项：
      1. 声明 supported 的必须给出 injects（除非是选择项）
      2. 声明 supported 的必须给出 evidence —— 否则"我接了"不可证明
      3. scope=engine 的能力必须对 ALL_ENGINES 都有结论（不能漏写引擎）
      4. scope 非 engine 的能力不应写 by_engine
      5. 不支持却写了 injects（自相矛盾）
      6. kind 必须是已注册的适配器

    ★ 第 2 条是这套机制的关键：把"我接了"变成"我能证明我接了"。
      没有它，矩阵退化回一张可以随便填的表。
    """
    problems: list[dict] = []
    for c in CAPABILITIES:
        if c.scope != "engine":
            if c.by_engine:
                problems.append({"cap": c.id, "type": "非引擎级能力不该逐引擎声明"})
            continue

        missing = [e for e in ALL_ENGINES if e not in c.by_engine]
        if missing:
            problems.append({"cap": c.id, "type": "缺引擎结论", "detail": "、".join(missing)})

        for e, s in c.by_engine.items():
            if s.level == SUPPORTED:
                if c.injectable and not s.injects:
                    problems.append({"cap": c.id, "engine": e, "type": "声明支持但没写注入方式",
                                     "detail": "等于没接上：要么补 injects，要么改成 not_wired"})
                if c.injectable and not s.evidence:
                    problems.append({"cap": c.id, "engine": e, "type": "声明支持但没写证据键",
                                     "detail": "无法验证是否真的生效，运行时核验会跳过它"})
            else:
                if s.injects:
                    problems.append({"cap": c.id, "engine": e, "type": "不支持却写了注入方式"})
            for inj in list(s.injects) + list(s.always):
                if inj.kind not in ADAPTERS:
                    problems.append({"cap": c.id, "engine": e, "type": "未知适配器",
                                     "detail": f"kind={inj.kind}"})
                if inj.point not in ("ctor", "stream"):
                    problems.append({"cap": c.id, "engine": e, "type": "未知注入阶段",
                                     "detail": f"point={inj.point}"})
                # ★ 取值来源必须显式
                if inj.kind != "const" and not inj.src:
                    problems.append({"cap": c.id, "engine": e, "type": "注入未写明取值来源",
                                     "detail": f"{inj.name} 缺 src；"
                                               "留空会拿错值且不报错（batch_size 踩过）"})
    return problems


def evidence_map(engine: str) -> dict[str, str]:
    """该引擎上"能力 id → last_info 证据键"，供运行时核验使用。"""
    out: dict[str, str] = {}
    for c in CAPABILITIES:
        if c.scope != "engine" or not c.injectable:
            continue
        s = c.by_engine.get(engine)
        if s and s.level == SUPPORTED and s.evidence:
            out[c.id] = s.evidence
    return out


if __name__ == "__main__":
    import json

    ps = audit()
    print("矩阵自检:", "✓ 无矛盾" if not ps else json.dumps(ps, ensure_ascii=False, indent=2))
    print()
    demo = {"hotwords": {"zh.all": ["首饰", "非遗"], "en.all": ["GEO", "totwoo"]},
            "vad": True, "batched": True, "batch_size": 16, "compute_type": "float16"}
    for e in ALL_ENGINES:
        ctor, stream = build_engine_kwargs(e, demo, ctx={"lang": "zh"})
        print(f"{e:11s} ctor={ {k: (v if not isinstance(v, list) else f'[{len(v)} 词]') for k, v in ctor.items()} }")
        print(f"{'':11s} stream={ {k: (f'{len(v)} 字' if k == 'initial_prompt' else v) for k, v in stream.items()} }")
        ig = explain_ignored(e, demo)
        if ig:
            print(f"{'':11s} 被忽略：{'、'.join(i['label'] for i in ig)}")
