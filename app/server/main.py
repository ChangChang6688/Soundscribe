"""声文 SoundScribe · 本地服务

FastAPI 应用。设计要点（方案 §2.2）：
  · 只绑定 127.0.0.1 —— 不触发防火墙、不被局域网扫到
  · 端口自动探测 —— 默认 8765，被占用则自动 +1
  · 任务队列持久化到内存 + 摘要落盘
  · SSE 推送细粒度进度与实时逐句
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# ★ 必须最先导入 doctor —— 它负责在引擎之前完成 CUDA DLL 路径引导
from app.doctor import capabilities  # noqa: E402
from app.doctor import env as doctor  # noqa: E402
from app.doctor import modelstore  # noqa: E402
from app.doctor import recommend  # noqa: E402
from app.store import config_store  # noqa: E402

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,  # noqa: E402
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app.pipeline import exporter, media, postprocess  # noqa: E402
from app.pipeline.engines import EngineError  # noqa: E402
from app.server.jobs import JobManager, cache_stats, cleanup_cache  # noqa: E402

APP_VERSION = "0.12.0"
WEB_DIR = ROOT / "app" / "web"
UPLOAD_DIR = ROOT / "data" / "uploads"
LOG_DIR = ROOT / "data" / "logs"
EXPORT_ROOT = ROOT / "data" / "exports"

ALLOWED_MEDIA = re.compile(
    r"\.(mp4|mkv|mov|avi|flv|webm|wmv|m4v|ts|mpg|mpeg|3gp|"
    r"mp3|wav|m4a|aac|flac|ogg|opus|wma|amr|aiff|ape)$", re.I)


def safe_stdio() -> str:
    """确保 stdout / stderr 可用。

    ★ 为什么需要：M1 实测踩过 —— 当程序由 pythonw 启动（无控制台），
      或 stdout 被管道/重定向后关闭时，任何写入都会抛
      `OSError: [Errno 22] Invalid argument`。
      第三方库（如 FunASR 的 tqdm）也会往里写，从而把整个任务搞崩。
      这里做两件事：① 不可用时重定向到日志文件 ② 装一个不崩溃的异常钩子。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / "app.log"
    usable = True
    try:
        if sys.stdout is None or sys.stderr is None:
            usable = False
        else:
            sys.stdout.write("")
            sys.stdout.flush()
    except Exception:  # noqa: BLE001
        usable = False

    if not usable:
        f = open(logfile, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = f
        sys.stderr = f
        return f"已重定向到 {logfile}"

    # 即便初始可用，也要防止后续管道被关闭导致的崩溃
    class _Guard:
        def __init__(self, stream, fallback_path: Path):
            self._s = stream
            self._p = fallback_path
            self._f = None

        def write(self, data):
            try:
                return self._s.write(data)
            except Exception:  # noqa: BLE001
                if self._f is None:
                    self._f = open(self._p, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
                return self._f.write(data)

        def flush(self):
            try:
                self._s.flush()
            except Exception:  # noqa: BLE001
                pass

        def isatty(self):
            try:
                return self._s.isatty()
            except Exception:  # noqa: BLE001
                return False

        def __getattr__(self, name):
            return getattr(self._s, name)

    sys.stdout = _Guard(sys.stdout, logfile)  # type: ignore[assignment]
    sys.stderr = _Guard(sys.stderr, logfile)  # type: ignore[assignment]
    return "正常"


def _install_excepthook() -> None:
    """未捕获异常只记日志，不让进程静默退出。"""
    import threading

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            (LOG_DIR / "app.log").open("a", encoding="utf-8").write(msg + "\n")
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)


app = FastAPI(title="声文 SoundScribe", version=APP_VERSION, docs_url=None, redoc_url=None)
manager = JobManager(max_workers=1)

# 删除模型前先问一句：这个引擎有任务在跑吗？（避免转到一半把模型删了）
modelstore.store.set_usage_checker(manager.is_engine_in_use)


# ═══════════════════════════════════════════════════════════
# 基础接口
# ═══════════════════════════════════════════════════════════


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": APP_VERSION, "time": datetime.now().isoformat(timespec="seconds")}


@app.get("/api/env")
def env_report() -> dict:
    """完整环境报告（硬件 + 引擎能力 + 模型门禁）。"""
    return doctor.full_report()


@app.get("/api/capabilities")
def api_capabilities() -> dict:
    """★ 能力矩阵 —— 「哪个设置对哪个引擎生效」的唯一事实来源。

    界面上的"生效范围"文案一律由它生成，不再手写。
    手写的那份一定会和实际行为漂移（实测漂过：界面写"仅 Whisper 生效"，
    而 Parakeet 其实已经接上了原生热词），漂了也没人知道 —— 所以取消手写。
    """
    problems = capabilities.audit()
    return {
        "capabilities": capabilities.matrix_for_ui(),
        "problems": problems,
        "ok": not problems,
        "engines": {e: capabilities.ENGINE_NAME[e] for e in capabilities.ALL_ENGINES},
    }


@app.get("/api/capabilities/{engine}")
def api_capabilities_for(engine: str) -> dict:
    """单个引擎视角：这台引擎上哪些设置生效、哪些不生效。"""
    if engine not in capabilities.ALL_ENGINES:
        raise HTTPException(status_code=404, detail=f"未知引擎：{engine}")
    sup = []
    for c in capabilities.CAPABILITIES:
        if c.scope != "engine":
            continue
        s = c.by_engine.get(engine)
        if not s:
            continue
        sup.append({"id": c.id, "label": c.label, "level": s.level,
                    "level_label": capabilities.LEVEL_LABEL[s.level], "why": s.why})
    return {"engine": engine, "name": capabilities.ENGINE_NAME[engine],
            "capabilities": sup}


@app.get("/api/models")
def models() -> dict:
    """模型清单（含三态可用性门禁 + 每档量化判定 + 个人安装/下载状态）。"""
    rep = doctor.full_report()
    dl = {d["model_id"]: d for d in modelstore.store.status()}
    for m in rep["models"]:
        m["download"] = dl.get(m["id"])

    # 推荐与排序已在 doctor.full_report() 里做掉，这里直接透传
    return {
        "models": rep["models"],
        "picks": rep.get("picks"),
        "reco_summary": rep.get("reco_summary"),
        "reco_profile": rep.get("reco_profile"),
        "tier": rep["tier"],
        "tier_reason": rep["tier_reason"],
        "gpu": rep["gpu"],
        "ram_gb": rep["ram_gb"],
        "disk_free_gb": rep["disk_free_gb"],
        "models": rep["models"],
    }


@app.get("/api/models/downloads")
def model_downloads() -> dict:
    """轻量轮询接口：下载进度 + 各模型安装状态。"""
    rep = doctor.full_report()
    dl = {d["model_id"]: d for d in modelstore.store.status()}
    return {
        "downloads": list(dl.values()),
        "installs": {m["id"]: {"state": m["install_state"], "installed": m["installed"],
                               "installed_mb": m["installed_mb"]} for m in rep["models"]},
        "disk_free_gb": rep["disk_free_gb"],
    }


@app.post("/api/models/{model_id}/download")
def model_download(model_id: str, payload: dict | None = None) -> dict:
    """开始下载。payload.source: "mirror"（默认，国内用户）| "official"（海外官方源）。

    ★ 选源只决定**优先级**，另一组仍作为回退保留 ——
      用户选了镜像但镜像挂了，程序自动换官方源并提示，
      而不是让他再点一次（那只是把可用性问题转嫁给他）。
    """
    source = str((payload or {}).get("source") or "mirror")
    try:
        d = modelstore.store.start_download(model_id, source)
    except modelstore.ModelBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "download": d.to_dict()}


@app.post("/api/models/{model_id}/download/cancel")
def model_download_cancel(model_id: str) -> dict:
    return {"ok": modelstore.store.cancel_download(model_id)}


@app.delete("/api/models/{model_id}")
def model_delete(model_id: str) -> dict:
    """删除个人下载的模型文件（只删这本机上的文件，不影响内置能力）。"""
    try:
        r = modelstore.store.delete(model_id)
    except modelstore.ModelBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except modelstore.ModelInUse as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return r


# ═══════════════════════════════════════════════════════════
# 热词库（内置 + 个人上传）
# ═══════════════════════════════════════════════════════════


@app.get("/api/hotwords")
def hotwords() -> dict:
    from app.pipeline import postprocess

    groups = postprocess.load_hotwords()
    user = config_store.load_user_hotwords()
    builtin = postprocess._load_builtin_hotwords()  # noqa: SLF001
    hidden = set(config_store.load_removed().get("hotwords", []))
    builtin_counts = {k: len(v) for k, v in builtin.items()}
    user_items = sorted(user.get("items", []), key=lambda x: x.get("added_at", ""), reverse=True)
    return {
        "groups": {k: len([t for t in v]) for k, v in groups.items()},
        "detail": groups,
        "builtin_counts": builtin_counts,
        "builtin_total": sum(builtin_counts.values()),
        "user_items": user_items,
        "user_total": len(user_items),
        "hidden_items": sorted(hidden),
        "hidden_total": len(hidden),
        "batches": user.get("batches", []),
        "prompt_zh": postprocess.build_whisper_prompt(groups, "zh"),
        "prompt_en": postprocess.build_whisper_prompt(groups, "en"),
    }


@app.post("/api/hotwords/import")
async def hotwords_import(file: UploadFile = File(...), group: str = Form("")) -> dict:
    raw = await file.read()
    if len(raw) > 4 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件超过 4MB，请拆分后再上传")
    try:
        items, meta = config_store.parse_hotword_file(file.filename or "upload.txt", raw)
    except config_store.ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    src = f"个人上传 · {Path(file.filename or 'file').name}"
    res = config_store.add_hotwords([i["term"] for i in items], group=group, source=src)
    res.update({"format": meta["format"], "parsed": meta["total"], "source": src})
    return res


@app.post("/api/hotwords")
async def hotwords_add(payload: dict) -> dict:
    """手动添加：terms 可以是数组，也可以是换行/逗号分隔的字符串。"""
    terms = payload.get("terms")
    if isinstance(terms, str):
        terms = re.split(r"[\r\n,，、;；]+", terms)
    if not isinstance(terms, list) or not terms:
        raise HTTPException(status_code=400, detail="没有提供任何词条")
    res = config_store.add_hotwords(terms, group=payload.get("group", ""),
                                    source=payload.get("source") or "手动添加")
    return res


@app.delete("/api/hotwords/{item_id}")
def hotwords_delete(item_id: str) -> dict:
    ok = config_store.delete_hotword(item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="词条不存在（内置热词不可删除）")
    return {"ok": True}


@app.delete("/api/hotwords/batch/{batch_id}")
def hotwords_delete_batch(batch_id: str) -> dict:
    n = config_store.delete_hotwords_by_batch(batch_id)
    return {"ok": True, "removed": n}


@app.post("/api/hotwords/clear-user")
def hotwords_clear() -> dict:
    return {"ok": True, "removed": config_store.clear_user_hotwords()}


# ═══════════════════════════════════════════════════════════
# 纠错规则表（内置 + 个人上传）
# ═══════════════════════════════════════════════════════════


@app.get("/api/corrections")
def corrections() -> dict:
    from app.pipeline import postprocess

    cfg = postprocess.load_corrections()
    rules = cfg.get("rules", [])
    user = config_store.load_user_corrections()
    hidden = config_store.load_removed().get("corrections", [])
    return {
        "rules": rules,
        "builtin_rules": [r for r in rules if r.get("builtin")],
        "user_rules": [r for r in rules if not r.get("builtin")],
        "builtin_total": len([r for r in rules if r.get("builtin")]),
        "user_total": len(user.get("rules", [])),
        "disabled_total": len([r for r in user.get("rules", []) if not r.get("enabled", True)]),
        "hidden_items": hidden,
        "hidden_total": len(hidden),
        "hallucination_patterns": cfg.get("hallucination_blacklist", {}).get("patterns", []),
        "batches": user.get("batches", []),
        # ★ 标定来源核对：规则是从哪台引擎的错误里总结的、现在还对不对得上。
        #   由数据算出来，不在界面上手写 —— 手写的那句一定会随默认引擎变化而过期。
        "calibration": postprocess.calibration(cfg),
    }


@app.post("/api/corrections/import")
async def corrections_import(file: UploadFile = File(...)) -> dict:
    raw = await file.read()
    if len(raw) > 4 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件超过 4MB，请拆分后再上传")
    try:
        rules, patterns, meta = config_store.parse_correction_file(
            file.filename or "upload.txt", raw)
    except config_store.ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    src = f"个人上传 · {Path(file.filename or 'file').name}"
    res = config_store.add_corrections(rules, source=src)
    if patterns:
        res["patterns_added"] = config_store.add_hallucination_patterns(patterns, source=src)
    res.update({"format": meta["format"], "parsed": meta["rules"], "source": src})
    return res


@app.post("/api/corrections")
async def corrections_add(payload: dict) -> dict:
    """新增规则：支持单条 {pattern, replacement} 或批量 {rules:[...]}。"""
    rules = payload.get("rules")
    if rules is None:
        if not payload.get("pattern"):
            raise HTTPException(status_code=400, detail="缺少 pattern（错误写法）")
        rules = [{"pattern": payload.get("pattern"),
                  "replacement": payload.get("replacement", ""),
                  "severity": payload.get("severity", "normal")}]
    res = config_store.add_corrections(rules, source="手动添加")
    return res


@app.delete("/api/corrections/{rule_id}")
def corrections_delete(rule_id: str) -> dict:
    ok = config_store.delete_correction(rule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="规则不存在（内置规则不可删除）")
    return {"ok": True}


@app.delete("/api/corrections/batch/{batch_id}")
def corrections_delete_batch(batch_id: str) -> dict:
    n = config_store.delete_corrections_by_batch(batch_id)
    return {"ok": True, "removed": n}


@app.post("/api/corrections/{rule_id}/toggle")
def corrections_toggle(rule_id: str, payload: dict) -> dict:
    ok = config_store.toggle_correction(rule_id, bool(payload.get("enabled", True)))
    if not ok:
        raise HTTPException(status_code=404, detail="规则不存在")
    return {"ok": True}


@app.post("/api/corrections/clear-user")
def corrections_clear() -> dict:
    return {"ok": True, "removed": config_store.clear_user_corrections()}


# ═══════════════════════════════════════════════════════════
# 导入模板下载
# ═══════════════════════════════════════════════════════════


@app.get("/api/template/{kind}")
def template(kind: str) -> JSONResponse:
    if kind == "hotwords":
        return JSONResponse({
            "filename": "热词库导入模板.txt",
            "content": config_store.HOTWORD_TEMPLATE,
        })
    if kind == "corrections":
        return JSONResponse({
            "filename": "纠错规则导入模板.txt",
            "content": config_store.CORRECTION_TEMPLATE,
        })
    raise HTTPException(status_code=404, detail="未知模板类型")


# ═══════════════════════════════════════════════════════════
# 内置条目的隐藏 / 恢复；个人数据清理
#
# ★ 内置项也要能删：内置纠错规则是从**本人录音的错误**里挖出来的，
#   把软件交给别人时未必适用。但直接改内置文件会破坏「升级整体覆盖」的约定，
#   所以记录成排除项，随时可恢复。
# ═══════════════════════════════════════════════════════════


@app.post("/api/builtin/hide")
async def builtin_hide(payload: dict) -> dict:
    try:
        return config_store.hide_builtin(payload.get("kind", ""), payload.get("key", ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/builtin/restore")
async def builtin_restore(payload: dict) -> dict:
    try:
        return config_store.restore_builtin(payload.get("kind", ""), payload.get("key", ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/user-data")
def user_data() -> dict:
    return config_store.user_data_summary()


@app.post("/api/user-data/clear")
def user_data_clear() -> dict:
    """清空全部个人痕迹（个人热词 + 个人纠错 + 内置隐藏记录），内置基线自动恢复完整。

    准备把软件交给别人时用这一下就干净了。
    """
    return config_store.clear_all_user_data()


# ═══════════════════════════════════════════════════════════
# 导出目录
# ═══════════════════════════════════════════════════════════


@app.get("/api/common-dirs")
def common_dirs() -> dict:
    """常用文件夹的真实路径。Windows 上桌面/文档可能被 OneDrive 重定向，所以逐个探测。"""
    home = Path.home()
    checks = {
        "desktop": [home / "Desktop", home / "OneDrive" / "Desktop", home / "OneDrive" / "桌面",
                    home / "桌面"],
        "documents": [home / "Documents", home / "OneDrive" / "Documents", home / "OneDrive" / "文档",
                      home / "文档"],
        "downloads": [home / "Downloads", home / "下载"],
        "home": [home],
    }
    out: dict[str, str] = {}
    for key, cands in checks.items():
        out[key] = next((str(p) for p in cands if p.is_dir()), "")
    return out


# ═══════════════════════════════════════════════════════════
# 文件系统浏览（用于在工作台选择导出目录）
#
# ★ 为什么不用系统的「选择文件夹」对话框：
#   实测它有两个绕不过去的问题 ——
#     1. 独立进程弹出的对话框没有 owner 窗口，会跑到浏览器窗口**后面**去，
#        用户以为"点了没反应"，其实窗口在底下
#     2. PowerShell 5.1 的 stdout 在中文系统上是 GBK，中文路径传回来可能乱码
#   改成应用内浏览：完全可控、不会跑到后面、不依赖外部进程、还能顺手显示可写性。
# ═══════════════════════════════════════════════════════════

_MAX_DIRS = 400          # 单个目录最多列出这么多子文件夹，防止卡界面

# 目录可写性判断
# ★ 刻意**不写探针文件**，两个原因：
#   1. 写一个文件再删掉本身就有副作用 —— 校验类接口不该改变系统状态。
#      之前用 mkdir 探测已经在磁盘上误建过目录，写文件是同一个毛病。
#   2. 在受管环境里，反复删除文件会被「批量删除守卫」拦下，
#      而守卫抛的是 SystemExit（不是 Exception），普通 except 捕不到，
#      会把整个请求打成 500 —— 实测踩过。
# 改用 os.access 做零副作用判断。权限的边缘情况留给真正写文件时报错，
# 那时的错误信息反而更准确。
def can_write_dir(p: Path) -> tuple[bool, str]:
    """返回 (是否可写, 原因)。不产生任何文件。"""
    try:
        if not p.is_dir():
            return False, "这个路径不是文件夹"
        if not os.access(p, os.W_OK):
            return False, "这个文件夹没有写入权限"
    except OSError as exc:
        return False, f"无法判断写入权限（{type(exc).__name__}）"
    return True, ""


def _is_hidden_dir(p: Path) -> bool:
    """隐藏目录与系统目录不列出来，减少噪音。"""
    if p.name.startswith("."):
        return True
    if sys.platform == "win32":
        try:
            import ctypes

            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(p))
            if attrs not in (-1, 0xFFFFFFFF):
                return bool(attrs & 0x2) or bool(attrs & 0x4)   # HIDDEN | SYSTEM
        except Exception:  # noqa: BLE001
            pass
    return False


def _quick_dirs() -> list[dict]:
    home = Path.home()
    cands = [
        ("用户目录", [home]),
        ("桌面", [home / "Desktop", home / "OneDrive" / "Desktop", home / "OneDrive" / "桌面"]),
        ("文档", [home / "Documents", home / "OneDrive" / "Documents", home / "OneDrive" / "文档"]),
        ("下载", [home / "Downloads", home / "下载"]),
        ("视频", [home / "Videos", home / "OneDrive" / "Videos", home / "视频"]),
    ]
    out: list[dict] = []
    for label, paths in cands:
        hit = next((p for p in paths if p.is_dir()), None)
        if hit:
            out.append({"name": label, "path": str(hit)})
    root = ROOT / "data" / "exports"
    out.append({"name": "声文默认导出目录", "path": str(root)})
    return out


@app.get("/api/fs/roots")
def fs_roots() -> dict:
    """目录浏览的入口：磁盘列表 + 常用位置。"""
    drives: list[dict] = []
    try:
        for d in os.listdrives():          # Python 3.12+
            drives.append({"name": d.rstrip("\\"), "path": d})
    except Exception:  # noqa: BLE001
        import string

        for letter in string.ascii_uppercase:
            cand = f"{letter}:\\"
            if os.path.exists(cand):
                drives.append({"name": letter, "path": cand})
    return {"drives": drives, "quick": _quick_dirs(), "home": str(Path.home())}


@app.get("/api/fs/list")
def fs_list(path: str = "") -> dict:
    """列出某个目录下的子文件夹（不列文件 —— 选的是导出目录，文件只会添乱）。"""
    if not path:
        return {"ok": False, "message": "没有指定路径"}
    p = Path(path).expanduser()
    if not p.exists():
        return {"ok": False, "path": str(p), "message": "这个路径不存在"}
    if not p.is_dir():
        return {"ok": False, "path": str(p), "message": "这个路径不是文件夹"}

    dirs: list[dict] = []
    truncated = False
    try:
        for child in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if len(dirs) >= _MAX_DIRS:
                truncated = True
                break
            try:
                if child.is_dir() and not _is_hidden_dir(child):
                    dirs.append({"name": child.name, "path": str(child)})
            except OSError:              # 某些系统目录权限不足，跳过即可
                continue
    except PermissionError:
        return {"ok": False, "path": str(p), "message": "没有权限读取这个文件夹"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "path": str(p), "message": f"读取失败：{exc}"}

    # 顺便把「能不能写」告诉前端，省得用户选完才发现不可写
    writable, why = can_write_dir(p)

    parent = str(p.parent) if p.parent != p else ""
    return {
        "ok": True, "path": str(p), "parent": parent,
        "dirs": dirs, "truncated": truncated,
        "writable": writable, "writable_message": why,
    }


@app.post("/api/check-dir")
def check_dir(payload: dict) -> dict:
    """校验导出目录。

    ★ 刻意**只校验、不创建** —— 早期版本用 mkdir(parents=True) 探测，
      结果用户只要输入时打错一个字，磁盘上就多出一串垃圾目录。
      改为：目录已存在就测可写性；不存在则向上找最近的已存在父目录测可写性，
      并把「需要新建」如实告诉用户，真正创建留给导出那一刻。
    """
    raw = str(payload.get("path") or "").strip().strip('"')
    if not raw:
        return {"ok": True, "path": str(exporter.DEFAULT_EXPORT), "is_default": True,
                "message": "将使用默认导出目录"}
    p = Path(raw).expanduser()

    if p.exists():
        if not p.is_dir():
            return {"ok": False, "path": str(p), "message": "这个路径指向的是一个文件，不是文件夹"}
        writable, why = can_write_dir(p)
        if not writable:
            return {"ok": False, "path": str(p), "message": why}
        free = 0.0
        try:
            free = round(shutil.disk_usage(str(p)).free / 1024**3, 1)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "path": str(p), "exists": True, "free_gb": free, "message": "目录可用"}

    # —— 目录还不存在：只做静态检查，不创建 ——
    if sys.platform == "win32":
        body = str(p)
        if len(body) > 1 and body[1] == ":":
            body = body[2:]                     # 去掉盘符，保留其后的冒号检查
        m = re.search(r'[<>:"|?*]', body)
        if m:
            return {"ok": False, "path": str(p),
                    "message": f"路径里有 Windows 不允许的字符「{m.group(0)}」"}

    parent = p.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not parent.exists():
        return {"ok": False, "path": str(p), "message": "找不到这个位置，请确认路径是否正确"}
    writable, why = can_write_dir(parent)
    if not writable:
        return {"ok": False, "path": str(p), "message": f"这个位置不可写：{why}"}

    free = 0.0
    try:
        free = round(shutil.disk_usage(str(parent)).free / 1024**3, 1)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "path": str(p), "exists": False, "free_gb": free,
            "message": f"目录还不存在，转写时会自动创建（在 {parent} 下，可写）"}


# ═══════════════════════════════════════════════════════════
# 环境体检与自愈
# ═══════════════════════════════════════════════════════════


@app.get("/api/health-check")
def health_check(refresh: int = 0) -> dict:
    """逐项检查运行所需的环境。每项都给出中文状态、详情与修复建议。"""
    from app.doctor import health

    if refresh:
        health.invalidate_cache()
    return health.check_all(use_cache=not refresh)


@app.post("/api/health-fix")
def health_fix(payload: dict | None = None) -> dict:
    """对可自动修复的项执行修复（装依赖 / 下模型 / 重建目录）。

    装包可能几分钟，所以后台跑、前端轮询进度，不在请求里同步等。
    """
    from app.doctor import health

    ids = (payload or {}).get("ids")
    r = health.runner.start(ids if isinstance(ids, list) and ids else None)
    if not r.get("ok"):
        raise HTTPException(status_code=409, detail=r.get("message", "无法开始修复"))
    return r


@app.get("/api/health-fix/status")
def health_fix_status() -> dict:
    from app.doctor import health

    return health.runner.status()


@app.post("/api/health-fix/stop")
def health_fix_stop() -> dict:
    from app.doctor import health

    return health.runner.stop()


# ═══════════════════════════════════════════════════════════
# 磁盘占用与临时文件
# ═══════════════════════════════════════════════════════════


def _dir_mb(p: Path) -> float:
    if not p.exists():
        return 0.0
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return round(total / 1024**2, 1)


# ──「释放空间」的可清理项 ───────────────────────────────────
# ★ 分三档，是因为这四类东西的"后悔成本"完全不同：
#     safe    删了没影响
#     caution 删了要重新下载 / 只是副本
#     danger  删了就是真的没了（用户的成果）
#   界面上 danger 默认不勾选，且必须再确认一次。
CLEAN_TARGETS: list[dict] = [
    {"id": "cache", "label": "临时文件", "kind": "safe", "default_on": True,
     "detail": "转写过程中的中间音轨。正常情况下任务结束就会自动删，这里是兜底。"},
    {"id": "logs", "label": "运行日志", "kind": "safe", "default_on": True,
     "detail": "排查问题用的日志文件。删掉不影响使用。"},
    {"id": "models", "label": "已下载的模型", "kind": "caution", "default_on": False,
     "detail": "删掉后下次转写要重新下载（走国内镜像，通常几分钟）。空间吃紧时最值得清的就是这里。"},
    {"id": "uploads", "label": "上传的原始素材", "kind": "caution", "default_on": False,
     "detail": "你拖进来的视频/音频副本。删掉不影响已经导出的转写结果。"},
    {"id": "exports", "label": "导出的转写结果", "kind": "danger", "default_on": False,
     "detail": "这是你的成果文件（TXT / SRT / VTT / Markdown）。删了就没了，程序里无法恢复。"},
]


def _target_sizes() -> dict[str, float]:
    from app.doctor import registry as _reg

    models_mb = 0.0
    for m in _reg.all_models():
        if m.get("planned"):
            continue
        try:
            models_mb += modelstore.install_state(m).get("installed_mb", 0.0)
        except Exception:  # noqa: BLE001
            pass
    return {
        "models": round(models_mb, 1),
        "uploads": _dir_mb(UPLOAD_DIR),
        "cache": cache_stats().get("size_mb", 0.0),
        "exports": _dir_mb(EXPORT_ROOT),
        "logs": _dir_mb(LOG_DIR),
    }


@app.get("/api/storage")
def storage() -> dict:
    """报告各类占用，让用户清楚磁盘被什么吃了。"""
    sz = _target_sizes()
    targets = []
    for t in CLEAN_TARGETS:
        item = dict(t)
        item["size_mb"] = sz.get(t["id"], 0.0)
        targets.append(item)

    return {
        "models_mb": sz["models"],
        "uploads_mb": sz["uploads"],
        "cache": cache_stats(),
        "exports_mb": sz["exports"],
        "logs_mb": sz["logs"],
        "export_dir": str(exporter.DEFAULT_EXPORT),
        "default_export_dir": str(exporter.DEFAULT_EXPORT),
        "disk_free_gb": doctor.disk_free_gb(),
        "targets": targets,
        "busy": _active_job_count() > 0,
    }


@app.post("/api/storage/cleanup")
def storage_cleanup(payload: dict) -> dict:
    """按选中的项释放空间。

    ★ 三条硬规则：
      1. 有任务在跑时一律拒绝 —— 清缓存会打断正在转写的任务，删模型会直接让它失败
      2. 导出结果（用户成果）必须显式选中才删，且界面上要再确认一次
      3. 逐项独立执行，某一项失败不影响其他项，最后如实汇报每一项的结果
    """
    want = payload.get("targets") or []
    if not isinstance(want, list) or not want:
        raise HTTPException(status_code=400, detail="没有选择要清理的内容")

    known = {t["id"] for t in CLEAN_TARGETS}
    unknown = [w for w in want if w not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知的清理项：{', '.join(unknown)}")

    if _active_job_count() > 0:
        raise HTTPException(
            status_code=409,
            detail="有任务正在执行，暂时不能清理（清理临时文件会打断它，删模型会让它失败）。"
                   "请等任务结束或先取消。")

    before_gb = doctor.disk_free_gb()
    results: list[dict] = []
    total_mb = 0.0

    for tid in want:
        item = next(t for t in CLEAN_TARGETS if t["id"] == tid)
        try:
            freed = _do_clean_one(tid)
            total_mb += freed
            results.append({"id": tid, "label": item["label"], "ok": True,
                            "freed_mb": round(freed, 1), "message": f"释放 {freed:.0f} MB"})
        except Exception as exc:  # noqa: BLE001
            results.append({"id": tid, "label": item["label"], "ok": False,
                            "freed_mb": 0.0, "message": f"{type(exc).__name__}: {exc}"[:160]})

    doctor.invalidate_cache() if hasattr(doctor, "invalidate_cache") else None
    return {
        "results": results,
        "freed_mb": round(total_mb, 1),
        "freed_gb": round(total_mb / 1024, 2),
        "disk_free_before_gb": before_gb,
        "disk_free_after_gb": doctor.disk_free_gb(),
        "models_removed": "models" in want,
    }


def _do_clean_one(tid: str) -> float:
    """执行单项清理，返回释放的 MB。"""
    if tid == "cache":
        r = cleanup_cache(0)
        return float(r.get("freed_mb", 0.0))

    if tid == "logs":
        freed = 0.0
        for f in LOG_DIR.glob("*"):
            try:
                if f.is_file() and f.name != "_live.log":
                    freed += f.stat().st_size
                    f.unlink()
            except OSError:
                continue
        return freed / 1024**2

    if tid == "exports":
        freed = _dir_mb(EXPORT_ROOT)
        if EXPORT_ROOT.exists():
            shutil.rmtree(EXPORT_ROOT, ignore_errors=True)
        return freed

    if tid == "uploads":
        freed = _dir_mb(UPLOAD_DIR)
        if UPLOAD_DIR.exists():
            # 只删文件，保留目录
            for f in UPLOAD_DIR.glob("*"):
                try:
                    if f.is_file():
                        f.unlink()
                    elif f.is_dir():
                        shutil.rmtree(f, ignore_errors=True)
                except OSError:
                    continue
        return freed

    if tid == "models":
        from app.doctor import registry as _reg

        freed = 0.0
        for m in _reg.all_models():
            if m.get("planned"):
                continue
            try:
                if modelstore.install_state(m).get("state") == "not_installed":
                    continue
                r = modelstore.store.delete(m["id"])
                freed += float(r.get("freed_mb", 0.0))
            except Exception:  # noqa: BLE001
                # 单个模型删不掉（例如正被占用）不应阻断其他模型
                continue
        return freed

    raise ValueError(f"没有对应的清理动作：{tid}")


def _active_job_count() -> int:
    try:
        with manager._lock:  # noqa: SLF001
            return sum(1 for j in manager.jobs.values()
                       if j.status in ("running", "paused", "queued", "pending"))
    except Exception:  # noqa: BLE001
        return 0


@app.post("/api/storage/clean-cache")
def clean_cache() -> dict:
    """清掉遗留的临时音轨（正常情况下任务结束就会自动删，这里是兜底）。"""
    return cleanup_cache(0)


# ═══════════════════════════════════════════════════════════
# 上传与任务
# ═══════════════════════════════════════════════════════════


async def _save_upload(file: UploadFile) -> dict:
    name = Path(file.filename or "audio").name
    if not ALLOWED_MEDIA.search(name):
        raise HTTPException(status_code=400, detail=f"不支持的文件类型：{name}")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dst = UPLOAD_DIR / f"{int(time.time()*1000)}_{len(name)}_{name}"
    # 同名同刻冲突时加后缀
    n = 1
    while dst.exists():
        dst = UPLOAD_DIR / f"{int(time.time()*1000)}_{len(name)}_{n}_{name}"
        n += 1
    written = 0
    with dst.open("wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
            written += len(chunk)
    try:
        info = media.probe(dst)
    except media.MediaError as exc:
        dst.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"upload_id": dst.name, "path": str(dst),
            "name": name, "size": written, "media": info.to_dict()}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict:
    return await _save_upload(file)


@app.post("/api/upload-batch")
async def upload_batch(files: list[UploadFile] = File(...)) -> dict:
    """★ 多文件批量上传：一次拖入多个文件时走这里。

    逐个处理、逐个探测，**单个文件失败不影响其余**（一个坏文件不该让整批白传）。
    """
    out: list[dict] = []
    for f in files:
        try:
            r = await _save_upload(f)
            out.append({"ok": True, **r})
        except HTTPException as exc:
            out.append({"ok": False, "name": Path(f.filename or "?").name,
                        "error": str(exc.detail)})
        except Exception as exc:  # noqa: BLE001
            out.append({"ok": False, "name": Path(f.filename or "?").name,
                        "error": f"上传失败：{exc}"})
    return {
        "files": out,
        "ok_count": sum(1 for x in out if x["ok"]),
        "fail_count": sum(1 for x in out if not x["ok"]),
    }


@app.post("/api/probe")
async def probe_existing(body: dict) -> dict:
    """对已在磁盘上的路径做探测（用于拖拽本地路径的场景）。"""
    p = Path(body.get("path", ""))
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在：{p}")
    try:
        return {"media": media.probe(p).to_dict()}
    except media.MediaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _job_options(payload: dict) -> dict:
    return {
        "engine": payload.get("engine", "auto"),
        "formats": payload.get("formats", ["txt", "srt", "vtt", "md"]),
        "export_dir": payload.get("export_dir") or str(exporter.DEFAULT_EXPORT),
        "export_layout": payload.get("export_layout", "by_date"),
        "name_template": payload.get("name_template", "{stem}_{engine}_{date}"),
        "with_timestamps": payload.get("with_timestamps", True),
        "auto_export": payload.get("auto_export", True),
        "vad": payload.get("vad", True),
        "batched": payload.get("batched", True),
        "batch_size": payload.get("batch_size", 16),
        "normalize": payload.get("normalize", False),
        "route_sample_seconds": payload.get("route_sample_seconds", 60),
        "compute_type": payload.get("compute_type") or "",
    }


def _resolve_path(src: str) -> Path:
    p = Path(src)
    if not p.is_absolute():
        p = UPLOAD_DIR / src
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在：{p.name}")
    return p


@app.post("/api/jobs")
async def create_job(payload: dict) -> dict:
    """创建任务。

    ★ 默认只创建、不执行（status = pending）—— 拖进来不会立刻开跑，
      等用户确认点「开始转写」才真正入队。这是本版刻意的行为。
      需要立即执行时传 start_immediately = true。
    """
    paths = payload.get("paths")
    if not paths:
        one = payload.get("path") or payload.get("upload_id")
        if not one:
            raise HTTPException(status_code=400, detail="缺少文件路径")
        paths = [one]
    if not isinstance(paths, list):
        raise HTTPException(status_code=400, detail="paths 必须是数组")

    opts = _job_options(payload)
    auto = bool(payload.get("start_immediately", False))

    created: list[dict] = []
    for src in paths:
        p = _resolve_path(str(src))
        job = manager.submit(p, dict(opts), auto_start=auto)
        # ★ 创建时就把媒体信息探测好（ffprobe 约 0.1s）。
        #   好处：待确认列表能直接显示时长/大小供用户判断，且刷新页面后
        #   待开始的任务仍能完整还原（否则只剩一个文件名）。
        try:
            info = media.probe(p)
            job.media = info.to_dict()
            job.duration = info.duration
        except Exception:  # noqa: BLE001
            pass
        created.append(job.summary())

    return {
        "jobs": created,
        "count": len(created),
        "job_id": created[0]["id"] if len(created) == 1 else "",
        "auto_started": auto,
    }


@app.post("/api/jobs/start")
async def start_jobs(payload: dict) -> dict:
    ids = payload.get("ids") or []
    if not ids:
        raise HTTPException(status_code=400, detail="没有指定要开始的任务")
    return manager.start_many(ids)


@app.post("/api/jobs/start-all")
def start_all_jobs() -> dict:
    return manager.start_all_pending()


@app.post("/api/jobs/remove-finished")
def remove_finished() -> dict:
    return {"ok": True, "removed": manager.remove_finished()}


@app.get("/api/jobs")
def list_jobs() -> dict:
    with manager._lock:  # noqa: SLF001
        items = [j.summary() for j in manager.jobs.values()]
    items.sort(key=lambda x: x["created_at"], reverse=True)
    pending = [x for x in items if x["status"] == "pending"]
    active = [x for x in items if x["status"] in ("queued", "running", "paused")]
    return {"jobs": items[:80], "pending_count": len(pending),
            "active_count": len(active)}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    j = manager.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"job": j.to_dict()}


@app.post("/api/jobs/{job_id}/start")
def start_job(job_id: str) -> dict:
    ok = manager.start(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="该任务当前状态无法开始（可能已在执行或已结束）")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str) -> dict:
    j = manager.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="任务不存在")
    ok = manager.pause(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="该任务当前状态无法暂停")
    # 一并返回当前阶段：抽音轨/加载模型这类阻塞步骤无法中断，
    # 前端可以据此告诉用户「等这一步跑完就停」，而不是让人干等
    return {"ok": True, "stage": j.stage, "stage_label": j.stage_label or "当前步骤"}


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str) -> dict:
    ok = manager.resume(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    ok = manager.cancel(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="该任务已结束，无法取消")
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, force: int = 0) -> dict:
    """移除任务记录。force=1 时连执行中的也摘掉 —— 用于处理
    线程意外死亡、状态永远停在 running 的僵尸任务。"""
    ok = manager.remove(job_id, force=bool(force))
    if not ok:
        if not manager.get(job_id):
            raise HTTPException(status_code=404, detail="任务不存在")
        raise HTTPException(
            status_code=409,
            detail="任务正在执行中。如果它已经卡住不动了，可以强制移除。")
    return {"ok": True, "forced": bool(force)}


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    """SSE 事件流：阶段 / 进度 / 实时逐句 / 告警 / 完成。"""
    j = manager.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="任务不存在")

    queue = manager.subscribe(job_id)

    async def gen():
        # 先补发当前快照，避免前端刷新后丢失已产出内容
        yield _sse({"type": "snapshot", "job": j.to_dict()})
        try:
            while True:
                if await request.is_disconnected():
                    break
                item = None
                if queue:
                    item = queue.pop(0)
                if item is None:
                    yield ": keepalive\n\n"
                    import asyncio

                    await asyncio.sleep(0.6)
                    continue
                yield _sse(item)
                if item.get("type") in ("done", "error", "canceled"):
                    break
        finally:
            manager.unsubscribe(job_id, queue)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ═══════════════════════════════════════════════════════════
# 导出与文件
# ═══════════════════════════════════════════════════════════


@app.post("/api/jobs/{job_id}/export")
def export_again(job_id: str, payload: dict) -> dict:
    j = manager.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not j.segments:
        raise HTTPException(status_code=400, detail="任务尚未产出转写结果")
    opts = exporter.ExportOptions(
        formats=payload.get("formats", ["txt", "srt", "vtt", "md"]),
        outdir=Path(payload.get("export_dir") or exporter.DEFAULT_EXPORT),
        layout=payload.get("export_layout", "by_date"),
        name_template=payload.get("name_template", "{stem}_{engine}_{date}"),
        with_timestamps=payload.get("with_timestamps", True),
    )
    meta = {
        "source_name": Path(j.source).name,
        "source_stem": Path(j.source).stem,
        "engine": j.engine,
        "engine_display": j.engine_display,
        "duration_display": j.media.get("duration_display", ""),
        "route_reason": j.route.get("reason", ""),
    }
    written = exporter.export_all(j.segments, meta, opts)
    j.exports.update(written)
    return {"exports": j.exports}


@app.post("/api/open-folder")
def open_folder(payload: dict) -> dict:
    """在系统文件管理器中打开导出目录。"""
    p = Path(payload.get("path", ""))
    target = p if p.is_dir() else p.parent
    if not target.exists():
        raise HTTPException(status_code=404, detail="目录不存在")
    try:
        if sys.platform == "win32":
            os.startfile(str(target))  # noqa: S606
        elif sys.platform == "darwin":
            import subprocess

            subprocess.Popen(["open", str(target)])
        else:
            import subprocess

            subprocess.Popen(["xdg-open", str(target)])
        return {"ok": True, "path": str(target)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"无法打开目录：{exc}") from exc


@app.get("/api/download")
def download(path: str) -> FileResponse:
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(p, filename=p.name)


# ═══════════════════════════════════════════════════════════
# 静态资源
# ═══════════════════════════════════════════════════════════


def _asset_hash() -> str:
    """给前端资源算一个内容指纹，作为 cache-busting 的版本号。

    ★ 为什么必须做这个（真实的坑）：
      静态资源没有 Cache-Control 时，浏览器走**启发式缓存** ——
      按「文件多久没改」推算可缓存时长（Chrome 约为其 1/10，上限 24 小时）。
      结果就是：后端代码改了、也重启了、接口返回的确实是新文件，
      **但浏览器连请求都不发，直接用旧的 app.js**，界面看起来"没更新"。
      更隐蔽的是它只在"改动后不久"复现，重启几次就自己好了，很难定位。

      用内容哈希而不是版本号，是因为改文件时不一定记得升版本号；
      哈希变了 URL 就变了，浏览器必然重新拉取 —— 不依赖任何人的自觉。
    """
    import hashlib

    h = hashlib.md5()
    for name in ("app.js", "app.css", "index.html"):
        p = WEB_DIR / name
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:10]


# 启动时算一次即固定：一次运行内的资源是同一份，中途改文件应当重启而不是靠浏览器猜
ASSET_VER = _asset_hash()


@app.middleware("http")
async def _static_no_cache(request, call_next):
    """入口页不缓存；静态资源每次都带 etag 校验（本地请求成本可忽略）。

    ★ 光靠文件名哈希还不够稳妥：手工编辑、直接从磁盘打开旧页面、
      或代理缓存都可能绕过。显式声明缓存策略才能把"看到的是不是最新"这件事
      从"靠浏览器猜"变成"由服务端说了算"。
    """
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith("/index.html"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    elif path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    f = WEB_DIR / "index.html"
    if not f.exists():
        return HTMLResponse("<h1>声文</h1><p>前端资源缺失：app/web/index.html</p>", status_code=500)
    html = f.read_text(encoding="utf-8")
    # ★ 把资源引用替换成带内容指纹的 URL —— 每次前端改动后 URL 都会变，
    #   浏览器无法复用旧缓存。
    html = html.replace('/static/app.css"', f'/static/app.css?v={ASSET_VER}"')
    html = html.replace('/static/app.js"', f'/static/app.js?v={ASSET_VER}"')
    # 方便排查：在控制台敲 window.__BUILD__ 就能看到当前页面加载的是哪一版资源
    html = html.replace(
        "<head>",
        f'<head>\n<!-- build {ASSET_VER} · v{APP_VERSION} -->\n'
        f'<script>window.__BUILD__ = "{ASSET_VER}";window.__APP_VERSION__ = "{APP_VERSION}";</script>',
        1)
    return HTMLResponse(html)


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


# ═══════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════


def find_free_port(preferred: int = 8765, tries: int = 30) -> int:
    for i in range(tries):
        port = preferred + i
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到可用端口")


def existing_instance(port: int = 8765) -> str | None:
    """默认端口上是否已经有声文在跑？返回它的版本号，没有则 None。

    ★ 为什么要查：端口被占时 find_free_port() 会**自动换到 8766** 再起一个实例，
      用户双击两次 start.bat 就会得到两个服务共用同一块 GPU —— 两个任务同时抢显存，
      反而容易 OOM。宁可提示"已经在跑了"并把浏览器指过去，也不要静默开第二个。
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as x:
            d = json.loads(x.read().decode("utf-8"))
        return str(d.get("version", "?")) if d.get("ok") else None
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    import argparse

    import uvicorn

    stdio_note = safe_stdio()
    _install_excepthook()

    # ★ 传 0（全清）：新进程刚起来时没有任何任务在跑，
    #   cache 里的音频必然是上次遗留的孤儿，没道理再等 24 小时。
    #   实测踩过：进程被杀后留下的采样切片要等到第二天才被清掉。
    #
    # ★★ 必须捕 **BaseException**，不能只捕 Exception。
    #    运行环境的「批量删除守卫」抛的是 SystemExit（不是 Exception 的子类），
    #    漏掉它 → 一个**清理动作**能把整个服务启动干掉（实测踩过，
    #    服务报 ERR_CONNECTION_REFUSED，而日志里只有一行守卫提示）。
    #    辅助功能永远不该有能力阻止主服务启动。
    try:
        c = cleanup_cache(0)
    except BaseException:  # noqa: BLE001
        c = {"removed": 0, "freed_mb": 0, "pending": 0}

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    ap.add_argument("--reload", action="store_true")
    ap.add_argument("--allow-multi", action="store_true",
                    help="即使已有实例在跑，也另起一个（默认不允许，避免两个服务抢 GPU）")
    args = ap.parse_args()

    # 单实例检查：默认端口已有声文时，直接指过去，不再起第二个
    if not args.port and not args.allow_multi:
        ver = existing_instance()
        if ver:
            url = "http://127.0.0.1:8765"
            print("=" * 58)
            print("  声文 SoundScribe —— 已经在运行")
            print("=" * 58)
            print(f"  版本       v{ver}")
            print(f"  地址       {url}")
            print()
            print("  不需要再启动一个。两个实例会共用同一块显卡，")
            print("  同时转写时反而更容易显存不足。")
            print()
            print("  确实要另起一个（比如换端口做测试）：")
            print("      python app\\server\\main.py --port 8766")
            print("=" * 58)
            if args.open:
                import webbrowser

                webbrowser.open(url)
            return

    port = args.port or find_free_port()
    url = f"http://127.0.0.1:{port}"

    print("=" * 58)
    print("  声文 SoundScribe")
    print("=" * 58)
    rep = doctor.full_report()
    print(f"  硬件档位   {rep['tier']} 档 · {rep['tier_reason']}")
    if rep["gpu"].get("available"):
        print(f"  显卡       {rep['gpu']['name']}（{rep['gpu']['vram_mib']} MiB）")
    else:
        print("  显卡       未检测到 NVIDIA 显卡，将走 CPU")
    print(f"  引擎       Whisper {'就绪' if rep['engine'].get('whisper_ok') else '不可用'}"
          f" · SenseVoice {'就绪' if rep['engine'].get('sensevoice_ok') else '不可用'}")
    print(f"  地址       {url}")
    if stdio_note != "正常":
        print(f"  输出       {stdio_note}")
    if c["removed"]:
        print(f"  缓存       已清理 {c['removed']} 个临时文件（{c['freed_mb']} MB）")
    print("=" * 58)
    print("  按 Ctrl+C 停止")
    print()

    if args.open:
        import threading
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run("app.server.main:app", host="127.0.0.1", port=port,
                reload=args.reload, log_level="warning")


if __name__ == "__main__":
    main()
