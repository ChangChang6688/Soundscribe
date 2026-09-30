"""声文 · 发布前稳妥性检查

★ 定位：**只查"会不会出事"，不重跑功能测试**。
  功能对不对由另外几个工具管，这里负责的是"用户会不会遇到想不通的情况"。

  完整一套（改完代码按顺序跑）：
    node tools/front_check.js                      前端静态一致性（秒级）
    python tools/route_check.py                    语言路由边界（秒级）
    python tools/effectiveness_check.py --static   设置项生效·静态层（秒级）
    python tools/release_check.py                  接口与异常路径（需服务在跑）
    node tools/ui_smoke_test.js                    真实浏览器全量交互（分钟级）
    python tools/effectiveness_check.py            设置项生效·含运行时（分钟级）

★ 这个文件是怎么来的：2026-09-30 做发布前检查时，用它查出两个**严重问题**——
    ① 中文素材被路由到 Parakeet（不认中文的模型），触发条件就是"素材短于 30 秒"
    ② 重启应用后转写记录列表清空（文件还在磁盘上，但界面上什么都没有）
  而当时的界面冒烟测试 128 项全绿。**两条都没被覆盖，因为测试只走了能过的路径。**
  所以这里每条断言背后，都对应一次真实的、被漏掉的失败。

覆盖范围：
    A 接口自检          能力矩阵、未知引擎、错误码
    B 各引擎真实任务    中 / 英 / 夹杂三条路径 + 设置回执自洽
    C 失败路径          静音音频、不存在的文件、空参数
    D 接口异常输入      空 ids、不存在的 id
    E 服务存活          跑完上面一堆之后还活着
    F 任务记录落盘      记录能存能读，重启不丢（含"上次跑一半被杀"的标注）

用法：
    python tools/release_check.py            # 全量
    python tools/release_check.py --quick    # 跳过真实任务，只查接口与落盘（秒级）
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8765"
# ★ 两层：tools/ → soundscribe/。这个文件原来放在 data/cache/ 下（三层），
#   移到 tools/ 后忘了改，于是素材路径全部指错、app.* 也导不进来。
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ★ 环境里配了系统代理，localhost 会被拦成 502 —— 必须显式绕过
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(path: str, payload: dict | None = None, timeout: int = 30):
    url = BASE + path
    if payload is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {"detail": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


results: list[tuple[str, bool, str]] = []


def check(name: str, good: bool, detail: str = "") -> None:
    results.append((name, good, detail))
    print(f"  {'OK  ' if good else 'FAIL'} {name}" + (f"  — {detail}" if detail else ""),
          flush=True)


# ★★ 测试绝不能污染用户数据。
#   自 v0.11.0 起任务记录会落盘持久化，所以在检查里跑的那几个测试任务
#   会**真的留在用户的文稿库里**（几条 asr_example_zh.wav / silence3s.wav）。
#   这跟早先"冒烟测试把用户任务删了"是同一类问题的两个方向：
#   那次是越界删，这次是越界留。原则一样 —— **只碰自己创建的**。
CREATED_JOBS: list[str] = []


def submit(path: str, engine: str) -> str:
    st, body = api("/api/jobs", {"paths": [str(path)], "engine": engine,
                                 "start_immediately": True})
    if st != 200 or not body.get("jobs"):
        return ""
    jid = body["jobs"][0]["id"]
    CREATED_JOBS.append(jid)          # 记下来，收尾时删掉
    return jid


def api_delete(path: str) -> tuple[int, dict]:
    req = urllib.request.Request(BASE + path, method="DELETE")
    try:
        with _opener.open(req, timeout=15) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:  # noqa: BLE001
        return 0, {}


def cleanup_created() -> int:
    """收尾：只删本次检查自己建的任务。"""
    n = 0
    for jid in CREATED_JOBS:
        st, _ = api_delete(f"/api/jobs/{jid}")
        if st == 200:
            n += 1
    return n


def wait(job_id: str, limit: int = 900) -> dict:
    t0 = time.time()
    while time.time() - t0 < limit:
        st, body = api("/api/jobs")
        if st == 200:
            for j in body.get("jobs", []):
                if j["id"] == job_id:
                    if j["status"] in ("done", "failed", "canceled"):
                        return j
        time.sleep(1.5)
    return {"status": "timeout"}


print("=" * 66)
print("  声文 · 发布前稳妥性检查")
print("=" * 66)

print("\n【A】接口自检")
st, health = api("/api/health")
check("健康检查可用", st == 200 and health.get("ok"), f"v{health.get('version')}")
st, caps = api("/api/capabilities")
check("能力矩阵自检无矛盾", st == 200 and caps.get("ok") is True,
      f"{len(caps.get('capabilities', []))} 项 / problems={len(caps.get('problems', []))}")
st, _ = api("/api/capabilities/whisper")
check("单引擎能力查询可用", st == 200, f"HTTP {st}")
st, bad = api("/api/capabilities/not-an-engine")
check("未知引擎返回 404 而不是 500", st == 404, f"HTTP {st}")

# ★★ 推理后端必须**被探测**并如实报出，而不是靠"机器有显卡"去假设。
#    sherpa-onnx 在 provider 不可用时会静默回落 CPU ——
#    只看硬件会得出"用了 GPU"的结论，那比不用 GPU 更糟（让人以为问题已解决）。
st, env = api("/api/env")
_sg = (env or {}).get("sherpa_gpu") or {}
check("★ 环境报告里有推理后端的真实探测结果",
      st == 200 and ("available" in _sg) and bool(_sg.get("reason")),
      f"available={_sg.get('available')} active={_sg.get('active')}"
      f" — {str(_sg.get('reason'))[:56]}")
check("★ 设备探测给了「为什么」而不只是一个布尔值",
      len(str(_sg.get("reason", ""))) > 6, str(_sg.get("reason"))[:56])

QUICK = "--quick" in sys.argv

if QUICK:
    print("\n【B】（--quick：跳过真实任务，不做各引擎端到端验证）")
else:
    print("\n【B】各引擎真实任务（覆盖之前没跑过的路径）")

# 这一组素材刻意各不相同：中文短素材 / 中英夹杂 / 纯英文。
# ★ 上一版只用英文素材跑，于是"中文被路由到英文专用模型"这个严重问题
#   一次都没被覆盖到 —— 测试用了恰好能过的素材。
cases = [] if QUICK else [
    ("中文 → SenseVoice", ROOT / "m1" / "asr_example_zh.wav", "auto", {"sensevoice"}),
    ("中英夹杂 → Whisper", ROOT / "data" / "mix-audio-16k.wav", "auto", {"whisper"}),
    ("显式指定 Whisper", ROOT / "data" / "en-audio-16k.wav", "whisper", {"whisper"}),
]

for name, path, engine, want in cases:
    if not path.exists():
        check(name, False, f"素材不存在 {path.name}")
        continue
    jid = submit(path, engine)
    if not jid:
        check(name, False, "任务创建失败")
        continue
    j = wait(jid)
    ok = j.get("status") == "done" and j.get("engine") in want
    detail = (f"status={j.get('status')} engine={j.get('engine')} "
              f"段={j.get('segments_count')} 字={j.get('chars')} "
              f"倍速={j.get('speed')}× 耗时={j.get('elapsed')}s")
    if j.get("error"):
        detail += f" error={j['error'][:80]}"
    check(name, ok, detail)

    # ★ 回执必须存在且自洽。
    #   注意**不能要求"一定有生效项"** —— SenseVoice 对热词/VAD/批处理全部不支持，
    #   它的 applied 本来就该是空的、ignored 才是主体。要求 applied 非空会把
    #   正确行为当失败（第一版就犯了这个错）。
    #   正确的不变量是：生效项 + 不生效项 ≥ 1，且生效项都写明了传成什么。
    ap = j.get("applied") or {}
    if j.get("status") == "done":
        applied = ap.get("applied") or []
        ignored = ap.get("ignored") or []
        ok_r = (bool(applied) or bool(ignored)) and all(
            a.get("param") and a.get("form") is not None for a in applied)
        check(f"  └ 回执自洽（生效 {len(applied)} / 不生效 {len(ignored)}）", ok_r,
              (" / ".join(f"{a['label']}→{a['param']}" for a in applied)
               or "、".join(i["label"] for i in ignored))[:110])
        # 回执里记的引擎必须与实际一致
        check("  └ 回执引擎与实际一致", ap.get("engine") == j.get("engine"),
              f"回执={ap.get('engine')} 实际={j.get('engine')}")
        # ★★ 设备必须落在回执里，而且**请求值**与**实际值**都要有。
        #    这是 Eli 报"是不是全跑在 CPU 上"的直接产物 ——
        #    界面上以前完全没有设备信息，只能靠猜。
        #    provider 会静默回落，所以两者必须分开记录、如实显示。
        check("★ 回执写明了推理设备", bool(ap.get("device")),
              f"device={ap.get('device')} requested={ap.get('device_requested')}")
        check("★ 回执区分「请求的设备」与「实际生效的设备」",
              bool(ap.get("device_requested")), str(ap.get("device_requested")))
        # 不生效的每一项都必须带原因（不能只丢一个"不支持"）
        check("  └ 不生效项都写明了原因",
              all((i.get("why") or "").strip() for i in ignored),
              f"{len(ignored)} 项")

print("\n【C】失败路径（异常必须被兜住，而不是崩服务）")
jj = submit(ROOT / "m1" / "silence3s.wav", "auto")
if jj:
    j = wait(jj, 300)
    check("静音音频给出可读失败原因而不是崩服务",
          j.get("status") == "failed" and bool(j.get("error")),
          f"status={j.get('status')} error={str(j.get('error'))[:70]}")
else:
    check("静音音频任务可创建", False, "创建失败")

st, body = api("/api/jobs", {"paths": [str(ROOT / "no-such-file-xyz.wav")],
                             "start_immediately": True})
check("不存在的文件被拒绝且不产生脏任务", st in (404, 400),
      f"HTTP {st} detail={str(body.get('detail'))[:60]}")

st, body = api("/api/jobs", {"paths": []})
check("空 paths 被拒绝", st == 400, f"HTTP {st}")

print("\n【D】接口异常输入")
st, _ = api("/api/jobs/start", {"ids": []})
check("空 ids 启动被拒绝", st == 400, f"HTTP {st}")
st, _ = api("/api/jobs/start", {"ids": ["not-a-real-id"]})
check("不存在的任务 id 不 500", st in (200, 400) and st != 500, f"HTTP {st}")

print("\n【E】服务存活（跑完上面一堆之后）")
st, health = api("/api/health")
check("服务仍然健康（没有被打挂）", st == 200 and health.get("ok"))


# ═══════════════════════════════════════════════════════════
# F 任务记录落盘 —— 重启后转写记录不能凭空消失
# ═══════════════════════════════════════════════════════════

def t_persist() -> None:
    import threading
    import tempfile

    from app.server import jobs as J

    print("\n【F】任务记录落盘（重启后转写记录不能凭空消失）")

    # ① 真实文件的内容必须"干净"：不含运行态、不含字级时间戳、不含日志
    if not J.JOBS_FILE.exists():
        check("任务记录文件已生成", False, f"没有 {J.JOBS_FILE.name}")
    else:
        try:
            d = json.loads(J.JOBS_FILE.read_text(encoding="utf-8"))
            items = d.get("jobs") or []
        except Exception as exc:  # noqa: BLE001
            items = []
            check("任务记录文件可解析", False, f"{type(exc).__name__}: {exc}")
        if items:
            check("任务记录文件可解析", True, f"{len(items)} 条")
            bad_rt = sorted({k for x in items for k in (x.get("options") or {})
                             if str(k).startswith("_")})
            check("不落盘运行态字段（_cancel / _pause_request 等）", not bad_rt,
                  "、".join(bad_rt[:4]) or "无")
            bad_w = [x.get("id") for x in items
                     if any("words" in s for s in (x.get("segments") or []))]
            check("不落盘字级时间戳（控制体积）", not bad_w, "、".join(bad_w[:3]) or "无")
            check("不落盘日志（log 字段）", all("log" not in x for x in items))
            check("每条都有 id / status / created_at",
                  all(x.get("id") and x.get("status") and x.get("created_at")
                      for x in items))

    # ② 往返测试：存进去能原样读出来。
    #    ★ 用临时路径并改回原值 —— 绝不能拿用户真实的 records 文件做实验。
    d_tmp = Path(tempfile.mkdtemp(prefix="sbc_persist_"))
    tmp = d_tmp / "jobs.json"
    orig = J.JOBS_FILE
    try:
        J.JOBS_FILE = tmp
        m1 = J.JobManager.__new__(J.JobManager)          # 只测存读，不启动 worker
        m1.jobs, m1._lock, m1._persist_warned = {}, threading.Lock(), False

        a = J.Job(id="t1", source="a.wav", options={}, status="done", engine="sensevoice")
        a.segments = [{"index": 1, "start": 0.0, "end": 2.0, "text": "你好",
                       "words": [{"w": "你", "s": 0.0, "e": 0.5}]}]
        a.applied = {"engine": "sensevoice", "applied": [],
                     "ignored": [{"label": "领域热词", "why": "该模型没有提示词接口"}]}
        a.exports = {"txt": "x.txt"}
        b = J.Job(id="t2", source="b.wav", options={"_cancel": True}, status="running")
        m1.jobs = {"t1": a, "t2": b}
        m1._persist()
        check("落盘写出文件", tmp.exists(),
              f"{tmp.stat().st_size} 字节" if tmp.exists() else "未生成")

        m2 = J.JobManager.__new__(J.JobManager)
        m2.jobs, m2._lock, m2._persist_warned = {}, threading.Lock(), False
        n = m2.load_persisted()
        check("读回条数一致", n == 2, f"{n} 条")

        r1, r2 = m2.jobs.get("t1"), m2.jobs.get("t2")
        check("完成态原样恢复（段落 / 回执 / 导出都在）",
              bool(r1) and r1.status == "done" and len(r1.segments or []) == 1
              and (r1.segments or [{}])[0].get("text") == "你好"
              and bool((r1.applied or {}).get("ignored")) and bool(r1.exports),
              f"段={len((r1.segments if r1 else []) or [])} 导出"
              f"={len((r1.exports if r1 else {}) or {})}")
        check("字级时间戳已剔除（控制体积）",
              not any("words" in s for s in ((r1.segments if r1 else []) or [])), "已剔除")
        check("★ 上次跑一半被杀的任务 → 标成失败并说明原因",
              bool(r2) and r2.status == "failed" and "中断" in (r2.error or ""),
              f"{(r2.status if r2 else '-')} / {((r2.error if r2 else '') or '')[:34]}")
    finally:
        J.JOBS_FILE = orig
        try:
            for p in d_tmp.glob("*"):
                p.unlink()
            d_tmp.rmdir()
        except Exception:  # noqa: BLE001
            pass


t_persist()


# ═══════════════════════════════════════════════════════════
# G 批量删除守卫 —— 一个清理动作能把整个服务启动干掉
# ═══════════════════════════════════════════════════════════

def t_cleanup_batch() -> None:
    """★ 这个探针的来历（2026-09-30 实测事故）：

      把启动时的缓存清理从"只清 24 小时前的"改成"全清"之后，
      某次启动恰好有 51 个残留文件 → 一次删 51 个，越过受管环境的
      「批量删除守卫」阈值（约 50）→ 守卫抛 **SystemExit** →
      而 main.py 那层 `except Exception` 捕不到它 → **整个服务起不来**。
      现象是浏览器官网都打不开（ERR_CONNECTION_REFUSED），
      日志里只有一行 `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]`。

    所以这里守两件事：
      ① 单轮删除必须**自己封顶**（低于守卫阈值），宁可分几次清
      ② 任何删除动作都捕 **BaseException**，绝不允许抛穿
      ③ 辅助功能（清理缓存）永远不该有能力阻止主服务启动
    """
    from app.server.jobs import CACHE_DIR as _CACHE
    from app.server.jobs import CACHE_DELETE_BATCH, cleanup_cache

    print("\n【G】缓存清理的批量保护（守卫会把服务启动干掉）")
    check("清理有自行封顶，且低于守卫阈值 50",
          CACHE_DELETE_BATCH < 50, f"上限 {CACHE_DELETE_BATCH}")

    # 有任务在跑就先跳过：cleanup_cache(0) 会清掉所有临时文件，
    # 包括那个正在跑的任务的音轨。
    st, jd = api("/api/jobs")
    if st == 200 and jd.get("active_count"):
        check("批量清理保护", True, "有任务在跑，跳过（避免误删它的临时音轨）")
        return

    n = CACHE_DELETE_BATCH + 5          # 45 个：够触发封顶，总量又不碰阈值
    made = []
    try:
        for i in range(n):
            p = _CACHE / f"_releasetest_{i}.bin"
            p.write_bytes(b"x" * 64)
            made.append(p)
        try:
            r = cleanup_cache(0)
            check("超量清理不抛异常（守卫不会被触发）", True, str(r))
            # ★ pending 只要求 ≥ 造出来的余数：cache 里可能还有别的残留
            #   （上次被中断的任务留下的），cleanup_cache(0) 会一并清掉。
            #   断言写成 ==5 会误报 —— 第一次跑就是这样。
            check("多出来的留到下次，并如实报告 pending",
                  r.get("removed") == CACHE_DELETE_BATCH and r.get("pending", 0) >= 5,
                  f"removed={r.get('removed')}（应为 {CACHE_DELETE_BATCH}）"
                  f" pending={r.get('pending')}（应 ≥5）")
        except BaseException as exc:  # noqa: BLE001
            check("超量清理不抛异常（守卫不会被触发）", False,
                  f"{type(exc).__name__}: {exc}")
    finally:
        # 收尾：剩下的几个由测试自己删掉，不留痕迹在用户磁盘上
        for p in made:
            try:
                if p.exists():
                    p.unlink()
            except BaseException:  # noqa: BLE001
                pass


t_cleanup_batch()

# ★ 收尾：删掉本次检查自己建的任务。
#   任务记录现在是落盘的，不清就会在用户文稿库里留下测试痕迹。
_removed = cleanup_created()
print(f"\n  收尾：清理本次检查新建的 {_removed} 条任务记录"
      f"（开工前已有的记录一律不碰）")

n_fail = sum(1 for _, g, _ in results if not g)
print("\n" + "=" * 66)
print(f"  通过 {len(results) - n_fail} · 失败 {n_fail}")
if n_fail:
    print("  失败清单：")
    for n, g, d in results:
        if not g:
            print(f"    X {n}  {d}")
print("=" * 66)
sys.exit(1 if n_fail else 0)
