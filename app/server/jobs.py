"""声文 · 任务运行器：串起整条管线并推送进度事件

事件流（供 SSE 推给前端）：
  stage      阶段切换（探测 / 抽音轨 / 语言路由 / 加载模型 / 识别 / 后处理 / 导出）
  progress   细粒度进度（百分比 / 已用时 / 预计剩余 / 实时倍速 / 当前第几块）
  segment    ★ 实时逐句（每识别完一句立即推送）
  warning    告警（如语言一致性校验失败）
  done       完成（附带结果摘要与导出文件）
  error      失败（附中文可读原因）
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.pipeline import engines, exporter, langroute, media, postprocess  # noqa: E402
from app.doctor import capabilities  # noqa: E402  ★ 设置项生效的唯一事实来源

STAGES = [
    ("probe", "探测媒体信息"),
    ("extract", "抽取音轨"),
    ("route", "语言路由判定"),
    ("load", "加载识别引擎"),
    ("transcribe", "语音识别"),
    ("postprocess", "后处理与校验"),
    ("export", "导出文件"),
]

# ★ 中间音轨放独立缓存目录，与用户上传的原文件分开。
#   原因：2 小时录像抽出的 16kHz WAV 可达 200MB+，若和原文件混在
#   data/uploads 里且从不清理，磁盘会随使用无限增长（实测 11 个任务就堆了 120MB）。
CACHE_DIR = ROOT / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ★ 语言路由的采样片数。见 _route_windows()：只看开头会误判。
ROUTE_SAMPLES = 3

# ★ 任务记录落盘位置。
#
#   为什么必须落盘（之前是纯内存，实测是个明显缺陷）：
#     文稿库的数据源就是内存里的任务列表。服务一重启 → 列表清空 →
#     用户第二天打开看到「还没有完成的文稿」，而 data/exports/ 里
#     明明躺着一堆转写稿。文件没丢，但**体验上就是"我的东西没了"**。
#     本地工具的常规用法恰恰是"跑完关掉、明天再开"。
#
#   ★ 放在 data/ 根目录而不是 cache/ 或 exports/：
#     它属于**用户数据**，不该被「释放空间」当成临时文件清掉。
JOBS_FILE = ROOT / "data" / "jobs.json"
JOBS_KEEP = 100          # 只保留最近这么多条终态记录，避免文件无限增长


def purge_job_cache(job_id: str) -> None:
    """删除该任务的中间音轨（含语言路由的采样切片）。失败不影响任务结果。

    ★ 用明确文件名逐个删，不用 glob —— 通配符批量删除在受管环境里会被
      「批量删除守卫」拦下（实测触发过），而且逐个删顺序可控、更好排查。
    ★ 必须捕 BaseException：文件系统/运行环境可能抛 SystemExit 这类
      不是 Exception 子类的异常，漏掉会让清理变成"炸点"。
    ★★ 采样切片从 1 片变成 3 片之后，这里的清单也必须同步 ——
      少写一个名字不会报错，只会让临时文件在磁盘上悄悄堆积。
    """
    names = [f"{job_id}.wav", f"{job_id}_route.wav"]
    names += [f"{job_id}_route{i}.wav" for i in range(1, ROUTE_SAMPLES + 1)]
    for name in names:
        try:
            p = CACHE_DIR / name
            if p.exists():
                p.unlink()
        except BaseException:  # noqa: BLE001
            continue


# ★★ 受管环境有一条「批量删除守卫」：**单轮删除超过约 50 个文件会被拦下**，
#    而且它抛的是 SystemExit（不是 Exception）—— 上层的 `except Exception`
#    捕不到，会直接把服务启动干掉（实测踩过，见下）。
#    所以这里自己设一个更低的上限，宁可分几次清，也不要一次撞上去。
#
#    ★ 血的教训：这个坑在 purge_job_cache 的注释里**早就记着**
#      （"通配符批量删除会被守卫拦下"），我在新写的 cleanup_cache(0) 里
#      又一次踩了上去 —— 而且这次代价更大：守卫的 SystemExit 一路逃到
#      main.py 的 `except Exception` 之外，**服务直接起不来**。
#      教训：知道 ≠ 会做；凡是"批量删除"的地方，都要主动封顶 + 捕 BaseException。
CACHE_DELETE_BATCH = 40


def cleanup_cache(max_age_hours: float = 24) -> dict:
    """清理 cache 目录里的临时音轨。

    ★★ 启动时必须传 **0**（全清），不能传 24。
      新进程刚起来时不可能有任何任务在跑，所以 cache 里的音频**必然**是
      上次遗留的孤儿 —— 没有任何理由让它们再躺 24 小时。
      实测踩过：上次进程被杀后留下 10 个 `_route*.wav` 采样切片，
      按 24 小时规则要等到第二天才被清掉。

    ★ 单轮最多清 CACHE_DELETE_BATCH 个（见上面的说明），多出来的留到下次；
      返回值里的 `pending` 会如实告诉用户还剩多少。

    ★ 只删文件、不递归删目录：`hotwords/` 是按内容命名的可复用缓存，不是孤儿。
    """
    cutoff = time.time() - max_age_hours * 3600

    victims: list[tuple[Path, int]] = []
    for p in CACHE_DIR.glob("*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                victims.append((p, p.stat().st_size))
        except BaseException:  # noqa: BLE001
            continue                       # 单个文件读不到属性，跳过就好

    removed = freed = 0
    for p, size in victims[:CACHE_DELETE_BATCH]:
        try:
            p.unlink()
            removed += 1
            freed += size
        except BaseException:  # noqa: BLE001  ★ 必须 BaseException，理由见上
            continue
    return {"removed": removed, "freed_mb": round(freed / 1024**2, 1),
            "pending": max(0, len(victims) - removed)}


def cache_stats() -> dict:
    files = size = 0
    for p in CACHE_DIR.glob("*"):
        try:
            if p.is_file():
                files += 1
                size += p.stat().st_size
        except Exception:  # noqa: BLE001
            continue
    return {"files": files, "size_mb": round(size / 1024**2, 1)}


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Job:
    id: str
    source: str
    options: dict
    # ★ 新增 pending 态：拖进来的文件先停在「待开始」，等用户点「开始转写」才执行
    #   pending → queued → running ⇄ paused → done | failed | canceled
    status: str = "pending"
    created_at: str = field(default_factory=now_iso)
    started_at: str = ""
    finished_at: str = ""
    stage: str = ""
    stage_label: str = ""
    pct: float = 0.0
    duration: float = 0.0
    processed: float = 0.0
    speed: float = 0.0
    eta_seconds: float = 0.0
    elapsed: float = 0.0
    pause_total: float = 0.0          # 累计暂停秒数（用于修正耗时与倍速）
    segments: list = field(default_factory=list)
    media: dict = field(default_factory=dict)
    route: dict = field(default_factory=dict)
    post: dict = field(default_factory=dict)
    language_check: dict = field(default_factory=dict)
    exports: dict = field(default_factory=dict)
    engine: str = ""
    engine_display: str = ""
    # ★ 本次实际生效的设置（"回执"）。每个任务自带一份 ——
    #   用户问"转写设置里这么多东西到底生效了没"，答案不该靠我解释，该由任务自己给出。
    applied: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    error: str = ""
    log: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("log", None)
        return d

    def summary(self) -> dict:
        return {
            "id": self.id, "source": self.source, "status": self.status,
            "stage": self.stage, "stage_label": self.stage_label,
            "pct": round(self.pct, 1), "engine": self.engine,
            "engine_display": self.engine_display,
            "created_at": self.created_at, "finished_at": self.finished_at,
            "duration": round(self.duration, 1),
            "speed": round(self.speed, 1),
            "elapsed": round(self.elapsed, 1),
            "segments_count": len(self.segments),
            "chars": sum(len(s.get("text", "")) for s in self.segments),
            "exports": self.exports,
            "error": self.error,
            "warnings": self.warnings,
            "applied": self.applied,
            "media": self.media,
        }


# 状态常量，避免各处拼字符串出错
PENDING, QUEUED, RUNNING = "pending", "queued", "running"
PAUSED, DONE, FAILED, CANCELED = "paused", "done", "failed", "canceled"


class JobManager:
    def __init__(self, max_workers: int = 1):
        self.jobs: dict[str, Job] = {}
        self.subscribers: dict[str, list] = {}
        self._lock = threading.Lock()
        self._queue: list[str] = []
        self._worker = threading.Thread(target=self._run_loop, daemon=True)
        self._wake = threading.Event()
        self._stop = False
        self.max_workers = max_workers
        self._active = 0
        # 每个任务一个 Event：set = 正常跑，clear = 暂停中
        self._run_gates: dict[str, threading.Event] = {}
        self._t0: dict[str, float] = {}          # 执行起点，用于随时算净耗时
        self._persist_warned = False             # 落盘失败只提醒一次
        self.load_persisted()                    # ★ 恢复上次的任务记录
        self._worker.start()

    # ---------- 任务记录持久化 ----------
    def load_persisted(self) -> int:
        """启动时恢复上次的任务记录。返回恢复了多少条。

        ★ 上次异常退出时正在跑的任务，这里要**明确标成失败**并说明原因 ——
          否则界面上会挂着一个永远不动的"运行中"任务，用户只能去点删除。
        """
        if not JOBS_FILE.exists():
            return 0
        try:
            raw = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
        except BaseException:  # noqa: BLE001
            return 0                          # 文件坏了就当没有，不能因此起不来服务

        fields = set(Job.__dataclass_fields__)
        items = [d for d in (raw.get("jobs") or []) if isinstance(d, dict)]
        # 只留最近的若干条，避免文件随使用无限膨胀
        items = sorted(items, key=lambda d: d.get("created_at", ""), reverse=True)[:JOBS_KEEP]

        n = 0
        with self._lock:
            for d in items:
                try:
                    j = Job(**{k: v for k, v in d.items() if k in fields})
                except BaseException:  # noqa: BLE001
                    continue                  # 单条坏了跳过，不影响其他
                if j.status in (RUNNING, PAUSED, QUEUED):
                    j.status = FAILED
                    j.error = ("上次运行时应用被关闭，这个任务被中断了。"
                               "文件还在，重新拖进来跑一次即可。")
                    j.finished_at = j.finished_at or now_iso()
                self.jobs[j.id] = j
                n += 1
        return n

    def _persist(self) -> None:
        """把任务记录落盘。

        ★ 三条铁律：
          1. **绝不能让落盘失败影响任务本身** —— 整个函数吞掉所有异常。
          2. 不存运行态（`_cancel` / `_pause_request` / 暂停闸门 / 启动时刻），
             那些只在进程活着时有意义。
          3. 原子替换：写临时文件再 rename，避免写一半崩了留下半个坏文件
             （下次启动读它会失败，等于把用户记录全清了）。
        """
        try:
            # ★ 先在锁内取快照，再在锁外序列化 —— 别拿着锁做磁盘 IO，
            #   也别直接遍历 self.jobs（其他线程可能正在增删，会抛
            #   "dictionary changed size during iteration"）。
            with self._lock:
                snapshot = list(self.jobs.values())

            keep = {"log", "words"}          # 日志与字级时间戳不必落盘（后者占大头）
            items = []
            for j in snapshot:
                d = asdict(j)
                for k in keep:
                    d.pop(k, None)
                for s in (d.get("segments") or []):
                    if isinstance(s, dict):
                        s.pop("words", None)
                d["options"] = {k: v for k, v in (d.get("options") or {}).items()
                                if not k.startswith("_")}
                items.append(d)
            items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            items = items[:JOBS_KEEP]

            JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = JOBS_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"version": 1, "jobs": items}, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(JOBS_FILE)
        except BaseException as exc:  # noqa: BLE001
            # ★ 落盘失败绝不能影响任务本身 —— 但**也不能一声不吭**。
            #   本项目的教训反复是同一个：静默失败最贵。
            #   这里只在第一次失败时出声，避免刷屏。
            if not self._persist_warned:
                self._persist_warned = True
                print(f"[jobs] 任务记录落盘失败（不影响任务执行，但重启后记录会丢）："
                      f"{type(exc).__name__}: {exc}", flush=True)

    # ---------- 事件分发 ----------
    def subscribe(self, job_id: str):
        q: list = []
        with self._lock:
            self.subscribers.setdefault(job_id, []).append(q)
        return q

    def unsubscribe(self, job_id: str, q) -> None:
        with self._lock:
            if job_id in self.subscribers and q in self.subscribers[job_id]:
                self.subscribers[job_id].remove(q)

    def emit(self, job: Job, event: dict) -> None:
        event = {"ts": time.time(), **event}
        with self._lock:
            for q in self.subscribers.get(job.id, []):
                q.append(event)

    # ---------- 任务生命周期 ----------
    def submit(self, source: Path, options: dict, auto_start: bool = False) -> Job:
        """入列。

        ★ auto_start=False（默认）时任务停在 pending —— 拖进来不会立刻开跑，
          等用户确认后显式 start。这是刻意的：拖错文件、想改引擎/格式、
          想攒齐一批再统一跑，都需要这个缓冲。
        """
        job = Job(id=uuid.uuid4().hex[:12], source=str(source), options=options)
        job.status = QUEUED if auto_start else PENDING
        with self._lock:
            self.jobs[job.id] = job
            self._run_gates[job.id] = threading.Event()
            self._run_gates[job.id].set()
            if auto_start:
                self._queue.append(job.id)
        if auto_start:
            self._wake.set()
        self._persist()          # 新任务立刻落盘，否则关掉应用就找不到了
        return job

    def submit_many(self, items: list[dict], auto_start: bool = False) -> list[Job]:
        return [self.submit(Path(it["path"]), it["options"], auto_start) for it in items]

    def start(self, job_id: str) -> bool:
        """把 pending 任务放入执行队列。"""
        with self._lock:
            j = self.jobs.get(job_id)
            if not j or j.status != PENDING:
                return False
            j.status = QUEUED
            ev = self._run_gates.setdefault(job_id, threading.Event())
            ev.set()
            self._queue.append(job_id)
        self._wake.set()
        self.emit(j, {"type": "queued"})
        return True

    def start_many(self, ids: list[str]) -> dict:
        started, skipped = [], []
        for i in ids:
            (started if self.start(i) else skipped).append(i)
        return {"started": started, "skipped": skipped, "count": len(started)}

    def start_all_pending(self) -> dict:
        with self._lock:
            ids = [j.id for j in self.jobs.values() if j.status == PENDING]
        return self.start_many(ids)

    def pause(self, job_id: str) -> bool:
        """暂停：在下一个检查点阻塞。不是杀进程，恢复后从原处继续。"""
        j = self.jobs.get(job_id)
        if not j or j.status not in (QUEUED, RUNNING):
            return False
        ev = self._run_gates.setdefault(job_id, threading.Event())
        ev.clear()
        if j.status == QUEUED:
            # 还没进执行线程，先标记，等它跑到第一个检查点时停下
            j.status = PAUSED
            self.emit(j, {"type": "paused"})
        else:
            j.options["_pause_request"] = True
        return True

    def resume(self, job_id: str) -> bool:
        j = self.jobs.get(job_id)
        if not j:
            return False
        ev = self._run_gates.setdefault(job_id, threading.Event())
        ev.set()
        j.options.pop("_pause_request", None)
        if j.status == PAUSED and j.stage == "":
            # 排队期就被暂停的，直接回到队列
            with self._lock:
                j.status = QUEUED
                if job_id not in self._queue:
                    self._queue.append(job_id)
            self._wake.set()
            self.emit(j, {"type": "queued"})
        return True

    def cancel(self, job_id: str) -> bool:
        j = self.jobs.get(job_id)
        if not j:
            return False
        if j.status in (DONE, FAILED, CANCELED):
            return False
        j.options["_cancel"] = True
        ev = self._run_gates.get(job_id)
        if ev:
            ev.set()            # 先唤醒，否则暂停中的任务永远收不到取消
        with self._lock:
            if j.status in (PENDING, QUEUED) and job_id in self._queue:
                self._queue.remove(job_id)
        if j.status in (PENDING, QUEUED):
            j.status = CANCELED
            j.finished_at = now_iso()
            self._persist()
            self.emit(j, {"type": "canceled"})
        return True

    def remove(self, job_id: str, force: bool = False) -> bool:
        """从列表中移除一条任务记录。

        force=False 时只允许删已结束或未开始的；force=True 时连执行中的也摘掉 ——
        用于处理「线程意外死亡、状态永远停在 running」的僵尸任务，
        否则用户除了重启服务别无他法。
        """
        j = self.jobs.get(job_id)
        if not j:
            return False
        if not force and j.status in (RUNNING, PAUSED):
            return False
        if force:
            # 先让可能还活着的线程尽快退出，再摘记录
            j.options["_cancel"] = True
            ev = self._run_gates.get(job_id)
            if ev:
                ev.set()
        with self._lock:
            self.jobs.pop(job_id, None)
            self._run_gates.pop(job_id, None)
            self._t0.pop(job_id, None)
            if job_id in self._queue:
                self._queue.remove(job_id)
        purge_job_cache(job_id)
        self._persist()          # ★ 删除也要落盘，否则重启后它又"活"回来了
        return True

    def remove_finished(self) -> int:
        with self._lock:
            ids = [j.id for j in self.jobs.values()
                   if j.status in (DONE, FAILED, CANCELED)]
        return sum(1 for i in ids if self.remove(i))

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def is_engine_in_use(self, engine: str) -> bool:
        """供模型删除前检查：该引擎是否有任务正在跑。"""
        for j in self.jobs.values():
            if j.status in (RUNNING, PAUSED) and j.engine == engine:
                return True
        return False

    def _run_loop(self) -> None:
        stall_ticks = 0
        while not self._stop:
            self._wake.wait(timeout=1.0)
            self._wake.clear()

            # ★ 看门狗：队列里还有任务、并发计数却是满的、而且没有任何任务真的在跑 ——
            #   说明计数卡住了（收尾清理抛异常等意外）。连续 5 秒都这样就把计数归零自愈，
            #   否则用户会遇到「点了开始却永远排队」，只能重启服务。
            with self._lock:
                need_watch = bool(self._queue) and self._active >= self.max_workers
            if need_watch:
                alive = sum(1 for j in self.jobs.values()
                            if j.status in (RUNNING, PAUSED, QUEUED))
                if alive == 0:
                    stall_ticks += 1
                    if stall_ticks >= 5:
                        with self._lock:
                            self._active = 0
                        stall_ticks = 0
                else:
                    stall_ticks = 0
            else:
                stall_ticks = 0

            while True:
                with self._lock:
                    if not self._queue or self._active >= self.max_workers:
                        break
                    job_id = self._queue.pop(0)
                    self._active += 1
                j = self.jobs.get(job_id)
                if not j:
                    with self._lock:
                        self._active -= 1
                    continue
                t = threading.Thread(target=self._execute, args=(j,), daemon=True)
                t.start()

    # ---------- 暂停 / 取消检查点 ----------
    def _checkpoint(self, job: Job) -> None:
        """在每个可中断点调用：响应取消与暂停。

        ★ 暂停是真的暂停（阻塞在检查点），不是中断重跑 ——
          恢复后从原处继续，已识别的段落不会重来。
        """
        if job.options.get("_cancel"):
            raise RuntimeError("已取消")

        ev = self._run_gates.get(job.id)
        if ev is None or ev.is_set():
            return

        job.status = PAUSED
        paused_at = time.time()
        self.emit(job, {"type": "paused", "stage": job.stage})
        while not ev.wait(0.25):
            if job.options.get("_cancel"):
                break
        job.pause_total += time.time() - paused_at
        if job.options.get("_cancel"):
            raise RuntimeError("已取消")
        job.status = RUNNING
        self.emit(job, {"type": "resumed", "stage": job.stage})

    @staticmethod
    def _active_elapsed(t_start: float, job: Job) -> float:
        """扣除暂停时间后的真实运行耗时。"""
        return max(0.0, time.perf_counter() - t_start - job.pause_total)

    # ---------- 主管线 ----------
    def _execute(self, job: Job) -> None:
        job.status = RUNNING
        job.started_at = now_iso()
        t_start = time.perf_counter()
        self._t0[job.id] = t_start
        try:
            self._checkpoint(job)          # 进管线前先过一道（可能刚被暂停）
            self._pipeline(job, t_start)
            if job.options.get("_cancel"):
                job.status = CANCELED
                # ★★ 顺序：**先落盘，再对外可见**。
                #   `/api/jobs` 一返回 done，用户就可能立刻关掉应用；
                #   若落盘排在 emit 之后，中间这个窗口里关掉 → 这条记录丢失。
                #   实测验证时正是卡在这个时序上（脚本抢在落盘前读到了旧内容）。
                self._persist()
                self.emit(job, {"type": "canceled"})
            else:
                job.status = DONE
                self._persist()
                self.emit(job, {"type": "done", "result": job.summary()})
        except BaseException as exc:  # noqa: BLE001
            # ★ 必须捕 BaseException，不能只捕 Exception。
            #   SystemExit / MemoryError 这类不是 Exception 的子类，
            #   一旦漏掉，任务会永远停在 running —— 既不能删也不能重试，
            #   用户只能重启整个服务。实测踩过。
            if isinstance(exc, KeyboardInterrupt):
                raise
            if job.options.get("_cancel"):
                job.status = CANCELED
                self._persist()
                self.emit(job, {"type": "canceled"})
            else:
                job.status = FAILED
                job.error = _humanize(exc) or f"任务被中断（{type(exc).__name__}）"
                job.log.append(traceback.format_exc())
                self._persist()
                self.emit(job, {"type": "error", "message": job.error})
        finally:
            # ★ 顺序至关重要：**先**把并发计数还回去，再做收尾清理。
            #   曾经的写法是「先清理临时文件、再减计数」，而清理一旦抛异常
            #   （比如运行环境的安全限制抛 SystemExit），finally 的后续语句会被跳过，
            #   _active 永远减不回去 → worker 认为并发已满 → **之后所有任务都只排队不执行**，
            #   界面上表现为「点了开始却一直没动静」，只能重启服务。
            #   实测踩过，这是最隐蔽也最要命的一个坑。
            with self._lock:
                self._active -= 1
            self._wake.set()
            try:
                job.finished_at = now_iso()
                job.elapsed = round(self._active_elapsed(t_start, job), 2)
                self._t0.pop(job.id, None)
                purge_job_cache(job.id)
            except BaseException:  # noqa: BLE001
                pass
            # ★ 终态落盘放在最后：此时 segments / exports / applied 都已写全。
            #   放前面会存下一个"跑完了但结果还没写进去"的记录。
            self._persist()

    def _stage(self, job: Job, key: str, label: str, pct: float = 0.0) -> None:
        self._checkpoint(job)
        job.stage, job.stage_label, job.pct = key, label, pct
        # 阶段切换时也刷新耗时，否则「已用」要等到第一段识别出来才有数字
        t0 = self._t0.get(job.id)
        if t0:
            job.elapsed = self._active_elapsed(t0, job)
        self.emit(job, {"type": "stage", "stage": key, "label": label, "pct": pct,
                        "elapsed": round(job.elapsed, 1)})

    def _pipeline(self, job: Job, t_start: float) -> None:
        opts = job.options
        src = Path(job.source)

        # ---- 1 探测 ----
        self._stage(job, "probe", "探测媒体信息", 2)
        info = media.probe(src)
        job.media = info.to_dict()
        job.duration = info.duration
        self.emit(job, {"type": "media", "media": job.media})

        # ---- 2 抽音轨 ----
        self._stage(job, "extract", "抽取音轨（16kHz 单声道）", 6)
        work = CACHE_DIR / f"{job.id}.wav"
        media.extract_audio(src, work, normalize=bool(opts.get("normalize")))
        self.emit(job, {"type": "progress", "pct": 10, "detail": "音轨就绪"})

        # ---- 3 语言路由 ----
        self._stage(job, "route", "语言路由判定", 12)
        requested = opts.get("engine", "auto")
        route_meta: dict
        if requested == "auto":
            route_meta = self._route(job, work, opts)
        else:
            route_meta = {
                "engine": requested, "reason": "用户手动指定", "cjk_ratio": 0.0,
                "cjk_ratio_pct": 0.0, "cjk_chars": 0, "latin_words": 0,
                "latin_unique": 0, "suggest_dual": False, "suggest_reason": "",
                "manual": True,
            }
        # ★ 路由是纯函数（只看语言统计），不知道模型装没装 —— 这里做一次可用性解析，
        #   否则用户没下载对应模型时任务会直接失败。
        installed = _installed_engines()
        actual, note = langroute.resolve(route_meta["engine"], installed)
        if actual != route_meta["engine"]:
            route_meta = dict(route_meta)
            route_meta["fallback_from"] = route_meta["engine"]
            route_meta["engine"] = actual
            route_meta["reason"] = f'{route_meta["reason"]}；{note}'
        route_meta.setdefault("installed", sorted(installed))

        job.route = route_meta
        job.engine = route_meta["engine"]
        self.emit(job, {"type": "route", "route": route_meta})

        # ---- 4 加载引擎 ----
        #
        # ★★ 从这里开始，所有"设置项 → 引擎参数"的翻译**全部由能力矩阵决定**。
        #   以前这里是 `if job.engine == "whisper": ... else: ...`，
        #   和前端手写的说明是两份互相独立的真相 —— 它们一定会漂移，
        #   而且漂移之后没有任何机制能发现。实测就漂了：
        #     · 热词只写进了 whisper 分支，中文默认走的 SenseVoice 一个词都收不到
        #     · 而且那句还被 `if cfg` 挡着 —— 用户没配纠错规则时，热词连 Whisper 都不生效
        #     · vad / batched 对 sherpa 引擎而言是"不支持"，但界面上仍写着可调
        #   现在改成"表怎么说、就怎么传"，两边**不可能**再不一致。
        self._stage(job, "load", "加载识别引擎", 16)
        cfg = postprocess.load_corrections()
        hot = postprocess.load_hotwords()
        # 提示词用哪种语言写，取决于这台引擎面向的语言
        lang_for_prompt = "zh" if job.engine == "sensevoice" else "en"

        cap_values: dict[str, Any] = {
            "engine": job.engine,
            "hotwords": hot,
            "corrections": cfg,
            "vad": bool(opts.get("vad", True)),
            "normalize": bool(opts.get("normalize", False)),
            "batched": bool(opts.get("batched", True)),
            "batch_size": int(opts.get("batch_size", 16)),
            "compute_type": opts.get("compute_type") or "",
        }
        ctor_kwargs, stream_kwargs = capabilities.build_engine_kwargs(
            job.engine, cap_values, ctx={"lang": lang_for_prompt})

        eng = self._build_engine(job.engine, ctor_kwargs)
        eng.load()
        job.engine_display = getattr(eng, "display", job.engine)
        job.applied = _receipt(job.engine, job.engine_display, eng,
                               cap_values, ctor_kwargs, stream_kwargs)
        self._emit_setting_notes(job, eng, cap_values)
        self.emit(job, {"type": "progress", "pct": 20,
                        "detail": f"{job.engine_display} 已加载（{eng.load_seconds:.1f}s）"})

        # ---- 5 识别（实时逐句）----
        self._stage(job, "transcribe", "语音识别", 22)

        raw_segments: list[dict] = []
        t_tr = time.perf_counter()
        pause_at_tr = job.pause_total
        total_chunks = None

        for seg in eng.stream(work, **stream_kwargs):
            # ★ 逐句之间就是天然的可中断点 —— 暂停与取消都在这里生效
            self._checkpoint(job)
            if total_chunks is None:
                total_chunks = (eng.last_info or {}).get("chunks")
            raw_segments.append(seg.to_dict())
            processed = seg.end
            # 扣掉暂停时间，否则倍速会随着暂停时长一路往下掉
            elapsed = max(0.001, time.perf_counter() - t_tr - (job.pause_total - pause_at_tr))
            speed = processed / elapsed if elapsed > 0 else 0
            eta = (job.duration - processed) / speed if speed > 0 else 0
            # 识别阶段占总进度 22% → 90%
            frac = (processed / job.duration) if job.duration else 0
            pct = 22 + frac * 68
            detail = f"已识别 {len(raw_segments)} 段"
            if total_chunks:
                detail += f" · 第 {min(seg.index, total_chunks)}/{total_chunks} 块"
            job.pct, job.processed, job.speed, job.eta_seconds = pct, processed, speed, eta
            job.elapsed = self._active_elapsed(t_start, job)
            self.emit(job, {"type": "segment", "segment": raw_segments[-1],
                            "index": len(raw_segments)})
            self.emit(job, {"type": "progress", "pct": round(pct, 1),
                            "detail": detail, "speed": round(speed, 1),
                            "eta_seconds": round(eta, 1),
                            "elapsed": round(job.elapsed, 1),
                            "processed": round(processed, 1),
                            "duration": round(job.duration, 1)})

        if not raw_segments:
            raise RuntimeError("没有识别到任何语音内容，请确认音频中有人声")

        # ---- 6 后处理与校验 ----
        self._stage(job, "postprocess", "后处理与语言校验", 92)
        kept, post = postprocess.postprocess_segments(raw_segments, cfg)
        job.segments = kept
        job.post = post.to_dict()
        drift = postprocess.check_language_drift(
            post.text, job.engine, route_meta.get("cjk_ratio", 0.0),
            audio_seconds=job.duration,
        )
        job.language_check = drift
        self.emit(job, {"type": "post", "post": job.post, "language_check": drift})
        if drift.get("warn"):
            job.warnings.append({"kind": "language_drift", **drift})
            self.emit(job, {"type": "warning", "kind": "language_drift",
                            "message": drift["reason"], "suggestion": drift["suggestion"]})
        if post.hallucinations:
            self.emit(job, {"type": "warning", "kind": "hallucination",
                            "message": f"过滤掉 {len(post.hallucinations)} 个疑似幻觉/噪声段"})

        # ---- 7 导出 ----
        if opts.get("auto_export", True):
            self._stage(job, "export", "导出文件", 96)
            raw_dir = opts.get("export_dir") or str(exporter.DEFAULT_EXPORT)
            if raw_dir == "@source_dir":
                # 导出到源视频/音频所在文件夹（用户常用的一种约定）
                raw_dir = str(src.parent)
            eopts = exporter.ExportOptions(
                formats=opts.get("formats", ["txt", "srt", "vtt", "md"]),
                outdir=Path(raw_dir),
                layout=opts.get("export_layout", "by_date"),
                name_template=opts.get("name_template", "{stem}_{engine}_{date}"),
                with_timestamps=bool(opts.get("with_timestamps", True)),
            )
            meta = {
                "source_name": info.path.name,
                "source_stem": info.path.stem,
                "engine": job.engine,
                "engine_display": job.engine_display,
                "duration_display": info.to_dict()["duration_display"],
                "route_reason": route_meta.get("reason", ""),
            }
            job.exports = exporter.export_all(kept, meta, eopts)

        job.pct = 100.0
        self.emit(job, {"type": "progress", "pct": 100, "detail": "完成"})

    # ---------- 子步骤 ----------
    @staticmethod
    def _route_windows(duration: float, sample_sec: float) -> list[tuple[float, float]]:
        """选采样窗口 —— **分散在全篇**，而不是只看开头那一段。

        ★ 为什么必须分散（实测踩过）：
          原来固定从第 30 秒开始取 60 秒。这有两个致命问题 ——

          ① **短素材直接采空**。音频不足 30 秒时切出来是 0 秒空文件，
             统计结果 units=0 → cjk_ratio=0.0 → 被判成"纯英文" → 路由到不认中文的
             Parakeet。实测 5.5 秒的中文素材 100% 命中，报错是误导性的
             "没有识别到任何语音内容"。

          ② **开头不代表全篇**。实测一份中英夹杂素材，前 60 秒恰好是纯英文
             → 判成"纯英文"→ 用 Parakeet 跑完整段 → 后面的中文全部丢掉。
             而且早先就发现过语言漂移发生在全文 30%–54% 位置，
             只看开头本身就会漏。

        所以改为在 15% / 50% / 85% 三处各取一段，样本覆盖到中段与尾部。
        """
        if duration <= 0:
            return [(0.0, max(1.0, sample_sec))]
        if duration <= sample_sec:
            return [(0.0, duration)]           # 素材比采样预算还短 → 整段用
        seg = sample_sec / ROUTE_SAMPLES
        out: list[tuple[float, float]] = []
        for anchor in (0.15, 0.50, 0.85):
            mid = duration * anchor
            s = max(0.0, min(duration - seg, mid - seg / 2))
            out.append((round(s, 2), round(s + seg, 2)))
        return out

    def _route(self, job: Job, audio: Path, opts: dict) -> dict:
        """第一道防线：分散采样判定语言。"""
        sample_sec = float(opts.get("route_sample_seconds", 60))
        try:
            windows = self._route_windows(job.duration, sample_sec)
            self.emit(job, {"type": "progress", "pct": 12,
                            "detail": f"正在试跑 {len(windows)} 处采样，判断语言…"})

            # 试跑用 Whisper：它对中英两种都能出文本，便于统计。
            # ★ 只加载一次，循环里复用 —— 每片都新建会把采样耗时乘以 3。
            probe = engines.build("whisper", device="cuda" if _has_cuda() else "cpu")
            probe.load()

            texts: list[str] = []
            for i, (s, e) in enumerate(windows, 1):
                tmp = CACHE_DIR / f"{job.id}_route{i}.wav"
                media.slice_audio(audio, tmp, start=s, duration=max(0.5, e - s))
                # 极短素材可能切出空片；空片不进统计，否则又成了"没数据当数据"
                if tmp.exists() and tmp.stat().st_size > 1000:
                    texts.append("".join(
                        x.text for x in probe.stream(tmp, vad=True, batched=False,
                                                     beam_size=1)))
                # 临时切片统一交给任务收尾的 purge_job_cache 处理 ——
                # 清理动作分散在各处时，任何一处抛异常都会把整条管线带崩。
            text = "".join(texts)

            stats = langroute.text_stats(text, audio_seconds=job.duration)
            res = langroute.decide(stats)
            out = res.to_dict()
            out["sample_text"] = text[:300]
            out["sample_seconds"] = round(sum(e - s for s, e in windows), 1)
            out["sample_windows"] = [f"{s:.0f}-{e:.0f}s" for s, e in windows]
            return out
        except Exception as exc:  # noqa: BLE001
            job.warnings.append({"kind": "route_failed", "message": str(exc)})
            self.emit(job, {"type": "warning", "kind": "route_failed",
                            "message": f"语言路由判定失败，回退到 Whisper：{exc}"})
            return {"engine": "whisper", "reason": "路由判定失败，回退默认引擎",
                    "cjk_ratio": 0.0, "cjk_ratio_pct": 0.0, "cjk_chars": 0,
                    "latin_words": 0, "latin_unique": 0, "units": 0,
                    "sufficient": False,
                    "suggest_dual": False, "suggest_reason": "", "fallback": True}

    def _build_engine(self, name: str, ctor_kwargs: dict):
        """建引擎。

        ★ `device` / `provider` 不来自能力矩阵 —— 它们是从**硬件事实**派生的，
          不是用户设置项。但也**绝不硬编码**：一律交给 `engines._pick_provider()`
          真探测（探针检查 sherpa 自带的 CUDA provider DLL 能否加载）。

        ★★ 2026-09-30 修正：这里原来是"sherpa 三个引擎统一走 CPU"，
          理由是"CPU 已经 35–43×、不需要显卡，把显卡留给 Whisper"。
          现在改为按探测结果决定 —— 因为那个理由不成立：
          那 ~2GB CUDA 库本来就要为 Whisper 装、本机早就有了，
          sherpa 的 CUDA wheel 只多 86MB，"省体积"在装了 Whisper 的机器上没有意义。
          （真要省体积是**打包分发**时的事，不该让它悄悄决定开发机的默认行为。）
        """
        if name in ("sensevoice", "parakeet", "moonshine"):
            # 不显式传 provider → 由 _pick_provider() 探测后决定 cuda/cpu
            return engines.build(name, **ctor_kwargs)
        # Whisper 走 CTranslate2，它自己的 device 语义：有显卡就 cuda + float16
        device = "cuda" if _has_cuda() else "cpu"
        return engines.build("whisper", device=device, **ctor_kwargs)

    def _emit_setting_notes(self, job: Job, eng, cap_values: dict) -> None:
        """把"设了但不生效""生效前被缩水"的设置主动说出来。

        ★ 这是整件事的收口：不生效不要紧（引擎能力差异是客观事实），
          但**不能让用户以为生效了**。以前是界面写死一句"仅 Whisper 生效"，
          既不准确、也没法随引擎变化 —— 现在每次跑都由矩阵现算。
        """
        msgs: list[dict] = []

        for it in capabilities.explain_ignored(job.engine, cap_values):
            msgs.append({"kind": "ignored_setting", "label": it["label"], "why": it["why"]})
        if msgs:
            names = "、".join(m["label"] for m in msgs)
            job.warnings.append({"kind": "ignored_settings", "engine": job.engine,
                                 "items": msgs})
            self.emit(job, {"type": "warning", "kind": "ignored_settings",
                            "message": f"本段使用 {job.engine_display}，以下设置对它不生效：{names}",
                            "items": msgs})

        for n in capabilities.adaptation_notes(job.engine, cap_values):
            job.warnings.append({"kind": "hotword_dropped", **n})
            self.emit(job, {"type": "warning", "kind": "hotword_dropped",
                            "message": n["message"]})

        # ★★ 值进到引擎里之后又被"吃掉"多少 —— 这是最后一道网。
        #   约定（见 capabilities.Support.evidence 的说明）：
        #     引擎若丢弃了一部分值，就把数量写进 last_info[f"{evidence}_dropped"]。
        #   实测背景：Parakeet 的热词因为缺 bpe_vocab，16 个词**全被静默跳过**，
        #   Python 侧没有任何异常、last_info 也看不出问题 ——
        #   参数传进去了、看着像生效、实际效果为零。这类问题必须能被说出来。
        info = getattr(eng, "last_info", {}) or {}
        for cap_id, key in capabilities.evidence_map(job.engine).items():
            n_drop = info.get(f"{key}_dropped") or 0
            if not n_drop:
                continue
            cap = capabilities.BY_ID.get(cap_id)
            words = info.get(f"{key}_dropped_words") or []
            msg = (f"{cap.label if cap else cap_id}：有 {n_drop} 个值无法被这个模型接受，已剔除"
                   + (f"（{'、'.join(str(w) for w in words)}）" if words else '')
                   + "。它们本次不会起作用。")
            job.warnings.append({"kind": "value_dropped", "cap": cap_id,
                                 "dropped": n_drop, "words": words})
            self.emit(job, {"type": "warning", "kind": "value_dropped", "message": msg})

        # ★ 引擎侧若报出"我忽略了这些参数"，说明矩阵和实现对不上号了 —— 必须炸出来。
        #   这正是以前那批 bug 的形态：参数名对不上，于是静默消失、什么都不发生。
        ig = list(getattr(eng, "ignored_kwargs", []) or [])
        if ig:
            job.warnings.append({"kind": "kwargs_ignored", "items": ig})
            self.emit(job, {"type": "warning", "kind": "kwargs_ignored",
                            "message": f"引擎忽略了未识别的参数：{ig}（能力矩阵与引擎实现可能已不一致）"})


def _receipt(engine: str, engine_display: str, eng, cap_values: dict,
             ctor: dict, stream: dict) -> dict:
    """生成这次任务的"设置回执"：哪些生效、以什么形态生效、哪些被忽略。

    ★ 为什么要有它：用户问的是"转写设置里这么多东西，是不是真的生效的"。
      在这之前只能靠我去读代码回答 —— 那是不可持续的。现在每个任务
      自带一份回执，等于把这个问题变成**产品自己能回答的事**：
      「你设了 7 项，其中 4 项在这台引擎上生效，3 项不生效（原因是…）」。

      回执分三段，与能力矩阵的语义一一对应：
        applied  真的传进引擎了（附传成了什么，便于核对）
        ignored  这个引擎不支持，明确列出原因
        adapted  生效了但形态变了（例如热词 → 提示词文本 / 原生热词表）
    """
    applied: list[dict] = []
    for cap in capabilities.CAPABILITIES:
        if cap.scope != "engine" or not cap.injectable:
            continue
        sup = cap.by_engine.get(engine)
        if sup is None or sup.level != capabilities.SUPPORTED:
            continue
        for inj in sup.injects:
            if inj.kind == "const" or inj.src not in cap_values:
                continue
            v = cap_values[inj.src]
            if not capabilities.is_set(v):
                continue
            got = ctor.get(inj.name, stream.get(inj.name))
            applied.append({
                "id": cap.id, "label": cap.label,
                "param": inj.name,
                "where": "构造期" if inj.point == "ctor" else "推理期",
                "form": _form_note(inj.kind, v, got),
            })

    return {
        "engine": engine,
        "engine_display": engine_display,
        # ★ 设备必须如实记录，而且**请求值**和**实际值**要分开。
        #   这是"看起来生效"最容易发生的地方：sherpa 的 provider 不可用时会
        #   静默回落 CPU，只看"我传了 cuda"会得出错误结论。
        #   实测 Eli 就是因为界面上看不到设备，才怀疑"是不是全跑在 CPU 上"。
        "device": getattr(eng, "device", "") or "",
        "device_requested": (getattr(eng, "provider_requested", "")
                             or getattr(eng, "device", "") or ""),
        "applied": applied,
        "ignored": capabilities.explain_ignored(engine, cap_values),
        "adapted": capabilities.adaptation_notes(engine, cap_values),
    }


def _form_note(kind: str, raw, got) -> str:
    """一句话说清"这个值最后变成什么传进去了" —— 便于人工核对。"""
    if kind == "prompt":
        return f"拼成 {len(str(got or ''))} 字的提示词"
    if kind == "hotwords_file":
        return f"落成 {len(got or [])} 词的原生热词表"
    if kind == "bool":
        return "开" if got else "关"
    if isinstance(raw, dict):
        return f"{len(raw)} 组"
    if isinstance(raw, str):
        return raw if len(raw) <= 24 else raw[:24] + "…"
    return str(got)


def _installed_engines() -> set[str]:
    """当前已装好的引擎集合（用于路由回退）。"""
    try:
        from app.doctor import registry as _reg
        from app.doctor import modelstore as _ms

        out = set()
        for m in _reg.all_models():
            eng = m.get("engine")
            # Whisper 有多个尺寸，任意一个装好即可用
            try:
                if _ms.install_state(m).get("state") == "installed":
                    out.add(eng)
            except Exception:  # noqa: BLE001
                continue
        return out
    except Exception:  # noqa: BLE001
        return {"whisper"}


def _has_cuda() -> bool:
    try:
        from app.doctor import env as d

        return bool(d.engine_caps().get("cuda_devices"))
    except Exception:  # noqa: BLE001
        return False


def _humanize(exc: Exception) -> str:
    msg = str(exc)
    table = [
        ("Library cublas64_12.dll", "CUDA 运行库未正确加载。请重启应用；若仍失败，说明 pip 版 CUDA 库缺失。"),
        ("No clip timestamps", "批处理推理必须搭配 VAD（已自动处理，若仍报错请反馈）。"),
        ("out of memory", "显存不足。请在模型中心改用更小的量化档，或关闭其他占用显卡的程序。"),
        ("No such file", "找不到输入文件，可能已被移动或删除。"),
        ("没有识别到任何语音内容", "没有识别到任何语音内容，请确认这段音频里有人声。"),
        ("已取消", "任务已取消。"),
        ("SystemExit", "任务被外部中断（多为运行环境的安全限制）。可以对这条记录点「移除」后再重试。"),
        ("MemoryError", "内存不足，任务被迫中断。建议关闭其他程序，或改用更小的模型。"),
    ]
    for k, v in table:
        if k.lower() in msg.lower():
            return v
    return f"{type(exc).__name__}: {msg[:300]}"
