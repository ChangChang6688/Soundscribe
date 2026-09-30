"""声文 · 设置项生效性核验

★★ 这个工具解决的是本项目最痛的一类问题：
   **界面上的设置看起来生效，实际根本没传到引擎。**

   一天之内查出 6 个同类 bug，形态各不相同但根因相同 ——
   能力信息散落在四处（前端手写文案 / jobs.py 的 if-else / 引擎 `**_` 静默吞参 /
   注册表注释），四处只要有一处不同步，就会出现「界面说有、后端没传、引擎也不支持」
   这种组合，而且**没有任何机制能发现**。

   根治办法是把这四处收敛成一张表（app/doctor/capabilities.py）。
   但"收敛成一张表"只解决了「没有唯一来源」，还没解决「表本身写错了怎么办」——
   表可以声明 `supported`，实际却没接上；这正是原来那批 bug 的形态。

   所以需要这个工具：**按表逐项验证"声明支持的，是否真的接上了"**，分三层 ——

     第 1 层  矩阵自检        表自身有没有自相矛盾（漏写引擎、声明支持却没写注入方式…）
     第 2 层  静态接线核验    表里写的参数名，是否真的存在于引擎签名里
                             —— 挡住"参数名写错 → 被 **kwargs 静默吃掉"
     第 3 层  运行时证据核验  真实装载并跑一小段，从引擎侧读到"它确实用了这个值"
                             —— 挡住"参数确实传进去了，但引擎内部没用"

   第 3 层是关键：没有它，`supported` 只是一句口头承诺。
   有它，"我接了"就变成"我能证明我接了"。

用法（Windows / git bash）：
  python tools/effectiveness_check.py              # 全三层
  python tools/effectiveness_check.py --static     # 只做 1–2 层（不装载模型，秒级）
  python tools/effectiveness_check.py --engine whisper
  python tools/effectiveness_check.py --json       # 机器可读

退出码：0 全通过；1 有失败项（可直接接进 CI 或提交前自检）。
"""

from __future__ import annotations

import argparse
import inspect
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.doctor import capabilities as C  # noqa: E402
from app.pipeline import engines, postprocess  # noqa: E402

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"

# 中文素材给中文引擎、英文素材给英文引擎 —— 拿错语言跑会得到"能跑但没意义"的结果
SAMPLE_ZH = ROOT / "m1" / "asr_example_zh.wav"
SAMPLE_EN = ROOT / "data" / "en-audio-16k.wav"
SAMPLE_SECONDS = 20.0

results: list[dict] = []


def rec(layer: str, item: str, status: str, detail: str = "") -> None:
    results.append({"layer": layer, "item": item, "status": status, "detail": detail})
    icon = {PASS: "✓", WARN: "!", FAIL: "✗", SKIP: "–"}[status]
    line = f"  {icon} {item}"
    if detail:
        line += f"  — {detail}"
    print(line, flush=True)


# ═══════════════════════════════════════════════════════════
# 第 1 层 · 矩阵自检
# ═══════════════════════════════════════════════════════════

def layer1_matrix() -> None:
    print("\n【第 1 层】能力矩阵自检（表自身有没有自相矛盾）")
    problems = C.audit()
    if not problems:
        rec("matrix", "矩阵无自相矛盾", PASS,
            f"{len(C.CAPABILITIES)} 项能力 × {len(C.ALL_ENGINES)} 个引擎")
    for p in problems:
        rec("matrix", f"{p.get('cap')} · {p.get('type')}",
            FAIL, p.get("detail") or p.get("engine", ""))

    # 每个 injectable 的引擎级能力都必须至少有一个引擎说 supported，
    # 否则这个设置永远不生效 —— 用户在界面上却看得见它
    dead = [c.label for c in C.CAPABILITIES
            if c.scope == "engine" and c.injectable
            and not c.engines_with(C.SUPPORTED)]
    rec("matrix", "没有「所有引擎都不生效」的死设置",
        PASS if not dead else FAIL, "、".join(dead) or "无")

    # 被忽略的解释必须完整 —— 用户在界面上看到"不生效"时要能读到原因
    missing = []
    for c in C.CAPABILITIES:
        if c.scope != "engine":
            continue
        for e, s in c.by_engine.items():
            if s.level != C.SUPPORTED and not s.why:
                missing.append(f"{c.id}/{e}")
    rec("matrix", "所有「不支持」都写明了原因",
        PASS if not missing else FAIL, "、".join(missing) or "全部有原因")


# ═══════════════════════════════════════════════════════════
# 第 1.5 层 · 文案单源核验
# ═══════════════════════════════════════════════════════════

# ★ 这些句子曾经手写在界面上，而且**都漂移过**。
#   现在它们必须由 /api/capabilities 生成，界面文件里出现即视为回归。
BANNED_UI_PHRASES = [
    "仅对 <b>Whisper</b> 生效",          # 实测漂移：Parakeet 后来也支持了
    "仅 Whisper 引擎生效",               # 同上
    "内置规则是从 <b>Whisper</b> 的历史错误里总结的",  # 事实来源当时根本没记录在数据里
]


def _strip_comments(text: str, lang: str) -> str:
    """去掉注释再检查。

    ★ 必须去注释，否则这条规则会把"解释这段历史教训的注释"当成违规 ——
      实测第一次跑就误报了三条，全是在注释里说明"这里曾经手写过什么"。
      这跟本项目早先踩过的"提取 id 没排除注释行"是同一类错误：
      **对源码做文本检查时，先想清楚"注释算不算内容"。**
    """
    import re as _re

    if lang == "html":
        return _re.sub(r"<!--.*?-->", "", text, flags=_re.S)
    text = _re.sub(r"/\*.*?\*/", "", text, flags=_re.S)
    return _re.sub(r"^\s*//.*$", "", text, flags=_re.M)


def layer1_5_ui_single_source() -> None:
    print("\n【第 1.5 层】文案单源核验（界面不许再手写生效范围）")
    offenders = []
    for rel, lang in (("app/web/index.html", "html"), ("app/web/app.js", "js")):
        p = ROOT / rel
        if not p.exists():
            continue
        text = _strip_comments(p.read_text(encoding="utf-8"), lang)
        for phrase in BANNED_UI_PHRASES:
            if phrase in text:
                offenders.append(f"{rel}: 「{phrase}」")
    rec("ui", "界面里没有手写的生效范围文案",
        PASS if not offenders else FAIL, "；".join(offenders) or "全部来自 /api/capabilities")


# ═══════════════════════════════════════════════════════════
# 第 2 层 · 静态接线核验
# ═══════════════════════════════════════════════════════════

def _targets(engine: str) -> dict[str, object]:
    cls = engines.ENGINES[engine]
    return {"ctor": cls.__init__, "stream": cls.stream}


def _sig_params(fn) -> tuple[set[str], bool]:
    """返回 (显式参数名集合, 是否有 **kwargs 兜底)。"""
    params = inspect.signature(fn).parameters
    named = {n for n, p in params.items()
             if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    has_var_kw = any(p.kind == p.VAR_KEYWORD for p in params.values())
    return named, has_var_kw


def layer2_static(only: list[str]) -> None:
    print("\n【第 2 层】静态接线核验（表里写的参数名，引擎真的收吗）")
    checked = 0
    for cap in C.CAPABILITIES:
        if cap.scope != "engine":
            continue
        for engine in only:
            sup = cap.by_engine.get(engine)
            if sup is None:
                continue
            tg = _targets(engine)
            for inj in list(sup.injects) + list(sup.always):
                point_name = "构造期" if inj.point == "ctor" else "推理期"
                label = f"{cap.label} → {engine}.{inj.name}（{point_name}）"
                named, has_var_kw = _sig_params(tg[inj.point])
                checked += 1
                if inj.name in named:
                    rec("static", label, PASS, "参数名匹配")
                elif has_var_kw:
                    rec("static", label, WARN,
                        "只能被 **kwargs 兜住，静态无法确认；靠第 3 层运行时证据")
                else:
                    rec("static", label, FAIL,
                        f"{engine} 的 {inj.point} 签名里没有这个参数 —— "
                        "传了会直接被丢掉（正是静默吞参）")

            # 反向核验：不支持的引擎，绝不能把值传进去
            if sup.level != C.SUPPORTED:
                for inj in sup.injects:
                    rec("static", f"{cap.label} → {engine} 不该传参", FAIL,
                        f"矩阵说 {sup.level}，却写了注入方式 {inj.name}")

    # 恒定参数（language 之类）也要对得上
    for engine in only:
        tg = _targets(engine)
        named, has_var_kw = _sig_params(tg["stream"])
        sup = C.BY_ID["engine"].by_engine[engine]
        for inj in sup.always:
            if inj.name in named:
                rec("static", f"恒定参数 {engine}.{inj.name}", PASS, repr(inj.value))
            elif has_var_kw:
                rec("static", f"恒定参数 {engine}.{inj.name}", WARN, "靠 **kwargs")
            else:
                rec("static", f"恒定参数 {engine}.{inj.name}", FAIL, "签名里没有")

    rec("static", "静态核验覆盖量", PASS, f"{checked} 处注入点")


# ═══════════════════════════════════════════════════════════
# 第 3 层 · 运行时证据核验
# ═══════════════════════════════════════════════════════════

def _installed() -> set[str]:
    try:
        from app.doctor import modelstore, registry

        out = set()
        for m in registry.all_models():
            try:
                if modelstore.install_state(m).get("state") == "installed":
                    out.add(m.get("engine"))
            except Exception:  # noqa: BLE001
                continue
        return out
    except Exception:  # noqa: BLE001
        return set()


def _slice(src: Path, dst: Path, seconds: float) -> bool:
    from app.pipeline.engines import FFMPEG

    r = subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-i", str(src), "-t", str(seconds),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)],
        capture_output=True, text=True)
    return dst.exists() and dst.stat().st_size > 1000


def _has_cuda() -> bool:
    try:
        from app.doctor import env as d

        return bool(d.engine_caps().get("cuda_devices"))
    except Exception:  # noqa: BLE001
        return False


def _probe_values() -> dict:
    """构造一组"全都打开"的设置值 —— 目的是让每一项都有机会留下证据。

    ★ 刻意用**真实词库**而不是编造的一两个词：只有真实数据才能暴露
      "适配器把词滤空了""分组前缀不匹配"这类只在真实数据上出现的问题。
    """
    return {
        "hotwords": postprocess.load_hotwords(),
        "corrections": postprocess.load_corrections(),
        "vad": True,
        "normalize": True,
        "batched": True,
        "batch_size": 8,
        "compute_type": "float16" if _has_cuda() else "int8",
    }


def layer3_runtime(only: list[str]) -> None:
    print("\n【第 3 层】运行时证据核验（真跑一小段，从引擎侧读证据）")
    inst = _installed()
    if not inst:
        rec("runtime", "已安装引擎", SKIP, "一个都没装，跳过第 3 层")
        return

    vals = _probe_values()
    tmpdir = Path(tempfile.mkdtemp(prefix="sbc_eff_"))
    try:
        for engine in only:
            if engine not in inst:
                rec("runtime", f"{engine} 运行时核验", SKIP, "模型未安装")
                continue

            ev = C.evidence_map(engine)
            sup = C.BY_ID["compute_type"].by_engine.get(engine)
            _ = sup  # compute_type 只对 whisper 有效，证据表已涵盖
            ctor, stream = C.build_engine_kwargs(
                engine, vals, ctx={"lang": "zh" if engine == "sensevoice" else "en"})

            src = SAMPLE_ZH if engine == "sensevoice" else SAMPLE_EN
            if not src.exists():
                rec("runtime", f"{engine} 运行时核验", SKIP, f"缺素材 {src.name}")
                continue
            clip = tmpdir / f"{engine}.wav"
            if not _slice(src, clip, SAMPLE_SECONDS):
                rec("runtime", f"{engine} 运行时核验", FAIL, "素材切片失败")
                continue

            try:
                kw = dict(ctor)
                if engine == "whisper":
                    kw["device"] = "cuda" if _has_cuda() else "cpu"
                eng = engines.build(engine, **kw)
                eng.load()
                segs = []
                for s in eng.stream(clip, **stream):
                    segs.append(s)
                    if len(segs) >= 3:
                        break
            except Exception as exc:  # noqa: BLE001
                rec("runtime", f"{engine} 运行时核验", FAIL,
                    f"{type(exc).__name__}: {str(exc)[:120]}")
                continue

            info = getattr(eng, "last_info", {}) or {}
            rec("runtime", f"{engine} 能跑通", PASS if segs else FAIL,
                f"{len(segs)} 段")

            # ★ 引擎若报告"我忽略了这些参数"，说明有人绕过矩阵传了东西
            ig = list(getattr(eng, "ignored_kwargs", []) or []) + list(
                info.get("ignored_kwargs", []) or [])
            rec("runtime", f"{engine} 没有未被识别的参数", PASS if not ig else FAIL,
                "、".join(ig) or "无")

            # ★ 逐项读证据：这是第 3 层的核心
            for cap_id, key in ev.items():
                if key not in info:
                    rec("runtime", f"{engine} · {C.BY_ID[cap_id].label} 证据", FAIL,
                        f"last_info 里没有 {key}，无法证明生效")
                    continue
                got = info[key]
                want = None
                for inj in C.BY_ID[cap_id].by_engine[engine].injects:
                    if inj.src in vals:
                        want = vals[inj.src]
                        break
                if want is None:
                    rec("runtime", f"{engine} · {C.BY_ID[cap_id].label} 证据", SKIP, "本次未设置")
                    continue
                if inj.kind == "bool":
                    good = bool(got) == bool(want)
                elif inj.kind == "int":
                    good = int(got) == int(want)
                elif inj.kind == "str":
                    good = str(got) == str(want)
                else:                       # prompt / hotwords_file → 只看有没有真的进去
                    good = float(got or 0) > 0
                rec("runtime", f"{engine} · {C.BY_ID[cap_id].label} 证据",
                    PASS if good else FAIL, f"{key}={got!r}（设置值 {want!r}）")

                # ★★ "传进去了" ≠ "真的用上了"。
                #    引擎可能收到值之后又把一部分丢掉（子词表编不出来、格式不认…），
                #    而且往往**不报错、也不抛异常**。实测踩过：Parakeet 的 16 个英文热词
                #    因为没给 bpe_vocab，被 sherpa 全部静默跳过 ——
                #    上一版这个检查因为只看"数量 > 0"而放行了它。
                #    所以必须连带核验丢弃数（约定见 capabilities.Support.evidence）。
                #
                # ★ 只对**列表型**注入要求这项：单个字符串/布尔/数字不存在"被吃一半"，
                #   一律要求报告只会制造误报（第一次跑就给 whisper 报了一条假警告）。
                if any(i.kind == "hotwords_file" for i in
                       C.BY_ID[cap_id].by_engine[engine].injects):
                    n_drop = info.get(f"{key}_dropped")
                    if n_drop is None:
                        rec("runtime", f"{engine} · {C.BY_ID[cap_id].label} 丢弃数", WARN,
                            f"引擎没有报告 {key}_dropped —— 无法确认值是否被悄悄吃掉")
                    else:
                        rec("runtime", f"{engine} · {C.BY_ID[cap_id].label} 丢弃数",
                            PASS if not n_drop else FAIL,
                            (f"丢弃 {n_drop} 个：{info.get(key + '_dropped_words', [])[:6]}"
                             if n_drop else "0（全部被接受）"))
    finally:
        try:
            for p in tmpdir.glob("*"):
                p.unlink()
            tmpdir.rmdir()
        except Exception:  # noqa: BLE001
            pass


# ═══════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(description="声文 · 设置项生效性核验")
    ap.add_argument("--static", action="store_true", help="只做静态层（不装载模型）")
    ap.add_argument("--engine", action="append", default=None,
                    help="只核验指定引擎，可多次传入")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    only = [e for e in (args.engine or C.ALL_ENGINES) if e in C.ALL_ENGINES]
    if not only:
        print(f"未知引擎。可选：{'、'.join(C.ALL_ENGINES)}")
        return 2

    print("=" * 62)
    print("  声文 · 设置项生效性核验")
    print(f"  范围：{'、'.join(only)}")
    print("=" * 62)

    layer1_matrix()
    layer1_5_ui_single_source()
    layer2_static(only)
    if args.static:
        print("\n  （--static：跳过第 3 层运行时核验）")
    else:
        layer3_runtime(only)

    n = {k: sum(1 for r in results if r["status"] == k) for k in (PASS, WARN, FAIL, SKIP)}
    print("\n" + "=" * 62)
    print(f"  通过 {n[PASS]} · 警告 {n[WARN]} · 失败 {n[FAIL]} · 跳过 {n[SKIP]}")
    if n[FAIL]:
        print("  失败清单：")
        for r in results:
            if r["status"] == FAIL:
                print(f"    ✗ [{r['layer']}] {r['item']}  {r['detail']}")
    print("=" * 62)

    if args.json:
        print(json.dumps({"summary": n, "results": results}, ensure_ascii=False, indent=2))
    return 1 if n[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
