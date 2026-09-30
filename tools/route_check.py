"""声文 · 语言路由边界回归

★★ 为什么单独有这个工具

2026-09-30 的发布前检查抓到一个**严重事故**，而当时的界面冒烟测试 123 项全绿：

    中文素材 → 采样切空 → 统计出 units=0 → cjk_ratio 被算成 0.0
    → decide() 判定「纯英文」→ 路由到 Parakeet（不认中文的模型）
    → 输出乱码/空白，最终只报一句「没有识别到任何语音内容」

    触发条件极其普通：音频短于采样起点（30 秒）。实测 5.5 秒的中文素材 100% 命中。
    而 Eli 的产品定义是「短按一下记录一句说话」—— 那种语音就是几秒到几十秒。

**为什么 123 项冒烟测试没抓到？** 因为它用的素材（8.2 分钟英文）恰好能过。
测试覆盖的是"能过的路径"，边界一个都没碰。

所以这个工具的定位很明确：**只测边界，不测正常流程**。
正常流程归 ui_smoke_test 和 effectiveness_check 管。

三类边界：
  ① 样本量不足（空文本 / 极短文本）→ 绝不能落到英文专用引擎
  ② 素材过短 / 采样窗口 → 绝不能切出空片，且要看得到中段与尾部
  ③ 结果与预期不符时 → 必须发出警告，不能静默

用法：
  python tools/route_check.py          # 纯函数回归（秒级，不加载模型）
  python tools/route_check.py --e2e    # 追加端到端（会用 5.5 秒素材真跑一次）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.pipeline import langroute, postprocess  # noqa: E402
from app.server.jobs import CACHE_DIR, JobManager, purge_job_cache  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, good: bool, detail: str = "") -> None:
    RESULTS.append((name, good, detail))
    print(f"  {'OK  ' if good else 'FAIL'} {name}" + (f"  — {detail}" if detail else ""),
          flush=True)


EN_ONLY = ("parakeet", "moonshine")


def section(title: str) -> None:
    print(f"\n{title}")


# ═══════════════════════════════════════════════════════════
# ① 样本量不足：绝不能落到英文专用引擎
# ═══════════════════════════════════════════════════════════

def t_insufficient() -> None:
    section("【①】样本不足时不能落到英文专用引擎（本次事故的根因）")
    cases = [
        ("空文本（采样被切空）", ""),
        ("全空白", "   \n\t "),
        ("只有标点", "。。。！？，、"),
        ("零星几个英文词", "ok yes"),
    ]
    for name, text in cases:
        st = langroute.text_stats(text)
        r = langroute.decide(st)
        good = r.engine not in EN_ONLY and r.sufficient is False
        check(name, good,
              f"units={st['units']} → 引擎 {r.engine}（sufficient={r.sufficient}）")


def t_asymmetry() -> None:
    """★ 判错方向不对称 —— 本轮补上的一条设计原则。

    出错的方向不是对称的：
      · 判定「是中文」几乎不会错（出现了汉字就是有中文），样本少也成立
      · 判定「是纯英文」在样本少时极不可靠（这几十个字没中文 ≠ 全篇没中文）
    所以门槛必须分开设。这一条直接决定短语音的体验：
    几秒的中文语音只有十几个字，用统一阈值会被推到慢 3 倍的兜底档。
    """
    section("【①b】判错方向不对称（汉字是强信号；'没汉字'在样本少时什么都证明不了）")

    r = langroute.decide(langroute.text_stats("今天我们来聊珠宝首饰的设计与制作"))
    check("18 个字全中文（样本很少）→ 判中文，不走兜底",
          r.engine == "sensevoice" and r.sufficient is True,
          f"{r.engine} / sufficient={r.sufficient}")

    r = langroute.decide(langroute.text_stats("ok thank you"))
    check("3 个英文词（样本很少）→ 走兜底，不赌纯英文",
          r.engine == "whisper" and r.sufficient is False,
          f"{r.engine} / sufficient={r.sufficient}")

    r = langroute.decide(langroute.text_stats(
        "this is a fairly long english sentence about jewelry design " * 3))
    check("英文样本充足 → 才允许判给英文专用引擎",
          r.engine == "parakeet", f"{r.engine} / units={langroute.text_stats('x' * 1)['units']}")

    n = langroute.MIN_CJK_FOR_ZH
    r = langroute.decide(langroute.text_stats("好" * n))
    check(f"恰好 {n} 个汉字 → 足以确认是中文", r.engine == "sensevoice", f"{r.engine}")
    r = langroute.decide(langroute.text_stats("好" * (n - 1)))
    check(f"仅 {n - 1} 个汉字 → 不足以确认，走兜底", r.engine == "whisper", f"{r.engine}")


# ═══════════════════════════════════════════════════════════
# ② 采样窗口：短素材不能切空，长素材要覆盖中段与尾部
# ═══════════════════════════════════════════════════════════

def t_windows() -> None:
    section("【②】采样窗口（固定 start=30 会切空短素材）")
    w = lambda d, s: JobManager._route_windows(d, s)  # noqa: SLF001

    cases = [
        ("5.5 秒素材（事故素材）", 5.5),
        ("20 秒素材", 20.0),
        ("59 秒素材", 59.0),
        ("60 秒素材（正好等于采样预算）", 60.0),
        ("61 秒素材", 61.0),
        ("8 分钟素材", 480.0),
        ("2 小时素材", 7200.0),
    ]
    for name, dur in cases:
        ws = w(dur, 60.0)
        ok_span = all(s >= 0 and e > s and e <= dur + 0.01 for s, e in ws)
        # ★ 最关键：每个窗口都必须落在素材范围内，不能越界切空
        check(f"{name} → 窗口有效", ok_span,
              "、".join(f"{s:.1f}-{e:.1f}s" for s, e in ws))

    # 短素材必须从 0 开始整段用（原来固定 start=30 就是在这里切空的）
    short = w(5.5, 60.0)
    check("短素材从 0 开始整段采样（不切空）",
          len(short) == 1 and short[0][0] == 0.0 and short[0][1] == 5.5,
          f"{short}")

    # 长素材必须覆盖中段与尾部 —— 只看开头会漏掉"中段才漂移"和"前段恰好是英文"
    long_ws = w(480.0, 60.0)
    check("长素材采样点分散到多处", len(long_ws) >= 2, f"{len(long_ws)} 处")
    spans_tail = any(s > 480 * 0.6 for s, _ in long_ws)
    spans_mid = any(480 * 0.3 < s < 480 * 0.7 for s, _ in long_ws)
    check("覆盖到中段", spans_mid, "、".join(f"{s:.0f}s" for s, _ in long_ws))
    check("覆盖到尾部（前段恰好是英文时能救回来）", spans_tail,
          "、".join(f"{s:.0f}s" for s, _ in long_ws))

    check("总采样时长不超过预算",
          sum(e - s for s, e in long_ws) <= 60.0 + 0.01,
          f"{sum(e - s for s, e in long_ws):.1f}s")


# ═══════════════════════════════════════════════════════════
# ③ 正常语言判定（防"修一个坏一个"）
# ═══════════════════════════════════════════════════════════

def t_language() -> None:
    section("【③】正常语言判定不能被改坏")
    cases = [
        ("中文长文本", "今天我们来聊一聊珠宝首饰的设计与制作工艺，重点是非遗工美方向。" * 5,
         "sensevoice"),
        ("英文长文本", "Today we are going to talk about jewelry design and craft. " * 5,
         "parakeet"),
        ("中英夹杂", "今天我们讲 SEO 和 GEO 的区别，这是 generative engine optimization 的核心。" * 3,
         "whisper"),
        ("英文为主含少量中文",
         "This is a very long English sentence about jewelry design and craft markets. "
         "今天我们讲首饰" * 3, "whisper"),
    ]
    for name, text, want in cases:
        st = langroute.text_stats(text)
        r = langroute.decide(st)
        check(name, r.engine == want,
              f"中文占比 {st['cjk_ratio']*100:.0f}% → {r.engine}（期望 {want}）")


# ═══════════════════════════════════════════════════════════
# ④ 结果异常必须被警告，不能静默
# ═══════════════════════════════════════════════════════════

def t_drift() -> None:
    section("【④】结果与预期不符时必须报警（原来 parakeet/moonshine 没有防线）")
    zh_text = "这是一段中文内容，讲的是珠宝首饰的设计与制作，还有非遗工美的东西。"

    for eng in ("parakeet", "moonshine"):
        r = postprocess.check_language_drift(zh_text, eng, 0.01, audio_seconds=300)
        check(f"{eng} 吐出中文 → 必须告警", r["warn"] is True,
              r["reason"][:56] if r["warn"] else "未告警（静默！）")

    # 正常情况不能误报
    #   ★ 注意：文本长度必须与 audio_seconds **匹配**。第一次写这段时只重复了 6 遍
    #     （66 个词）却标称 5 分钟 → 触发"输出量偏少"告警。那不是误报，
    #     是测试数据不真实：真实 5 分钟英文素材有 700+ 个词。
    #     这条例外恰好说明那个判据是有效的。
    normal = "Today we talk about jewelry design and craft markets in Europe. " * 60
    r = postprocess.check_language_drift(normal, "parakeet", 0.0, audio_seconds=300)
    check("英文素材 + 英文引擎 → 不误报", r["warn"] is False,
          r["reason"][:50] or f"无告警（{r['units_per_minute']} 词/分钟）")

    # ★ 与语言无关的兜底：输出量明显偏少 = 引擎没听懂
    #   （中文喂给 Parakeet 会输出乱码拼音，那些是拉丁字母，按"中文占比"抓不到）
    r = postprocess.check_language_drift("hello world", "parakeet", 0.0, audio_seconds=600)
    check("10 分钟素材只出 2 个词 → 必须告警", r["warn"] is True,
          r["reason"][:60] if r["warn"] else "未告警（静默！）")

    # 短素材不该触发"语速异常"（时长条件未满足时不判断）
    r = postprocess.check_language_drift("你好", "sensevoice", 1.0, audio_seconds=10)
    check("极短素材不误报语速异常", r["warn"] is False, r["reason"][:50] or "无告警")


# ═══════════════════════════════════════════════════════════
# ⑤ 临时文件：采样切片从 1 片变 3 片后，清理清单必须跟上
# ═══════════════════════════════════════════════════════════

def t_cleanup() -> None:
    section("【⑤】采样切片清理（少写一个名字不会报错，只会悄悄堆文件）")
    jid = "_routetest_purge_xyz"     # 刻意用不可能与真实任务撞车的 id
    names = [f"{jid}.wav"] + [f"{jid}_route{i}.wav" for i in range(1, 4)] \
        + [f"{jid}_route.wav"]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for n in names:
        (CACHE_DIR / n).write_bytes(b"x" * 64)
    purge_job_cache(jid)
    left = [n for n in names if (CACHE_DIR / n).exists()]
    for n in left:                    # 收尾：别把测试痕迹留在用户磁盘上
        try:
            (CACHE_DIR / n).unlink()
        except Exception:  # noqa: BLE001
            pass
    check("三片采样切片全部被清掉", not left, "、".join(left) or f"{len(names)} 个名字")


# ═══════════════════════════════════════════════════════════
# ⑥ 端到端（可选）：用事故素材真跑一次
# ═══════════════════════════════════════════════════════════

def t_e2e() -> None:
    section("【⑥】端到端：5.5 秒中文素材（本次事故的原素材）")
    clip = ROOT / "m1" / "asr_example_zh.wav"
    if not clip.exists():
        check("事故素材可用", False, f"缺 {clip}")
        return
    try:
        from app.pipeline import engines, media
    except Exception as e:  # noqa: BLE001
        check("引擎模块可导入", False, str(e))
        return

    dur = media.probe(clip).duration
    ws = JobManager._route_windows(dur, 60.0)  # noqa: SLF001
    ok_win = len(ws) == 1 and ws[0][0] == 0.0
    check(f"{dur:.1f} 秒素材采样从 0 开始", ok_win, f"{ws}")

    try:
        import subprocess
        import tempfile

        from app.pipeline.engines import FFMPEG

        tmp = Path(tempfile.gettempdir()) / "_route_e2e.wav"
        s, e = ws[0]
        subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", str(s), "-t", str(e - s),
                        "-i", str(clip), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                        str(tmp)], capture_output=True, timeout=120)
        check("采样能切出内容（不是空片）",
              tmp.exists() and tmp.stat().st_size > 1000,
              f"{tmp.stat().st_size if tmp.exists() else 0} 字节")
        try:
            tmp.unlink()
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        check("采样切片可执行", False, f"{type(exc).__name__}: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description="声文 · 语言路由边界回归")
    ap.add_argument("--e2e", action="store_true", help="追加端到端检查（会切音频）")
    args = ap.parse_args()

    print("=" * 66)
    print("  声文 · 语言路由边界回归")
    print("=" * 66)

    t_insufficient()
    t_asymmetry()
    t_windows()
    t_language()
    t_drift()
    t_cleanup()
    if args.e2e:
        t_e2e()

    n_fail = sum(1 for _, g, _ in RESULTS if not g)
    print("\n" + "=" * 66)
    print(f"  通过 {len(RESULTS) - n_fail} · 失败 {n_fail}")
    if n_fail:
        print("  失败清单：")
        for n, g, d in RESULTS:
            if not g:
                print(f"    X {n}  {d}")
    print("=" * 66)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
