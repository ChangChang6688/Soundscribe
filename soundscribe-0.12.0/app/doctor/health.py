"""声文 · 环境体检与自愈

两件事：
  1. `check_all()`   —— 逐项检查运行所需的一切，每项给中文状态与详情
  2. `RepairRunner`  —— 对可自动修复的项执行修复（装包 / 下模型 / 建目录）

★ 设计原则：检查项必须是**用户看得懂、能行动**的东西，
  而不是把 import 成功与否直接抛出去。每一项都要回答三件事：
    · 这是什么（label）
    · 现在怎么样（status + detail）
    · 坏了怎么办（fixable / fix_hint）

★ 与 `env.py` 的分工：env.py 管「这台机器适合跑什么」（档位、门禁），
  本模块管「现在能不能跑起来」（依赖是否齐全、坏了能不能修）。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# 与安装时保持一致的国内源（M1 实测腾讯云最快 0.25s，清华会瞬时失败需回退）
PIP_MIRROR = "https://mirrors.cloud.tencent.com/pypi/simple/"
PIP_EXTRA = "https://pypi.tuna.tsinghua.edu.cn/simple"
TORCH_CPU = "https://download.pytorch.org/whl/cpu"

_CACHE: dict = {"at": 0.0, "data": None}
_CACHE_TTL = 25.0


def _has(mod: str) -> bool:
    """只判断能不能导入，不真的导入 —— 导入 torch/funasr 要好几秒。"""
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def _pkg_version(mod: str) -> str:
    try:
        import importlib.metadata as md

        for name in (mod, mod.replace("_", "-")):
            try:
                return md.version(name)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return ""


def _decode(raw: bytes) -> str:
    """Windows 控制台程序（pip / netstat / taskkill）的输出不一定是 UTF-8。

    ★ 不要用 subprocess 的 text=True：它按 UTF-8 解码，中文系统上直接抛
      UnicodeDecodeError，而且异常发生在 reader 线程里，stdout 会变成 None。
    """
    if not raw:
        return ""
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _pip_install_cmd(packages: list[str], index: str | None = None) -> list[str]:
    cmd = [sys.executable, "-m", "pip", "install", *packages]
    if index:
        cmd += ["--index-url", index]
    else:
        cmd += ["-i", PIP_MIRROR, "--extra-index-url", PIP_EXTRA]
    return cmd


# ═══════════════════════════════════════════════════════════
# 各项检查
# ═══════════════════════════════════════════════════════════


def _ck(id_: str, group: str, label: str, status: str, detail: str,
        required: bool = True, fixable: bool = False, fix_hint: str = "",
        fix: dict | None = None) -> dict:
    return {
        "id": id_, "group": group, "label": label, "status": status,
        "detail": detail, "required": required, "fixable": fixable,
        "fix_hint": fix_hint, "fix": fix or {},
    }


def check_python() -> dict:
    v = sys.version.split()[0]
    ok = sys.version_info >= (3, 10)
    return _ck("python", "运行时", "Python 解释器", "ok" if ok else "fail",
               f"{v}（{sys.executable}）", True, False,
               "" if ok else "需要 Python 3.10 以上，请重新安装运行环境")


def check_dirs() -> list[dict]:
    out = []
    for id_, label, rel in [("dir_data", "数据目录可写", "data"),
                            ("dir_config", "配置目录可写", "config"),
                            ("dir_export", "默认导出目录可写", "data/exports")]:
        p = ROOT / rel
        try:
            p.mkdir(parents=True, exist_ok=True)   # 这三个目录是程序自己要用的，创建是合理的
            if not os.access(p, os.W_OK):
                raise PermissionError("没有写入权限")
            out.append(_ck(id_, "运行时", label, "ok", str(p), True, True, "",
                           {"action": "mkdir", "path": str(p)}))
        except Exception as exc:  # noqa: BLE001
            out.append(_ck(id_, "运行时", label, "fail", f"{p} —— {exc}", True, True,
                           "尝试重新创建并赋予权限", {"action": "mkdir", "path": str(p)}))
    return out


def check_ffmpeg() -> list[dict]:
    out = []
    for exe, id_, label in [("ffmpeg", "ffmpeg", "FFmpeg（音视频解码）"),
                            ("ffprobe", "ffprobe", "FFprobe（读取媒体信息）")]:
        path = shutil.which(exe)
        if not path:
            out.append(_ck(id_, "音视频", label, "fail", "未找到，无法读取任何视频/音频", True, False,
                           "请安装 FFmpeg 并加入 PATH（gyan.dev 的 full build 即可），"
                           "或把 ffmpeg.exe / ffprobe.exe 放到 runtime\\bin 目录"))
            continue
        try:
            r = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=10)
            line = (r.stdout or "").splitlines()[0] if r.stdout else ""
            out.append(_ck(id_, "音视频", label, "ok", line[:70] or path, True, False))
        except Exception as exc:  # noqa: BLE001
            out.append(_ck(id_, "音视频", label, "warn", f"找到但无法运行：{exc}", True, False,
                           "FFmpeg 可能损坏，建议重新安装"))
    return out


def check_gpu() -> list[dict]:
    from app.doctor import env as doctor

    gpu = doctor.gpu_info()
    if gpu.get("available"):
        drv = gpu.get("driver", "?")
        try:
            major = int(str(drv).split(".")[0])
        except Exception:  # noqa: BLE001
            major = 0
        ok = major >= 525          # CUDA 12 需要 525+
        return [_ck("gpu", "加速", "NVIDIA 显卡与驱动",
                    "ok" if ok else "warn",
                    f"{gpu['name']} · 驱动 {drv} · 显存 {gpu['vram_mib']} MiB",
                    False, False,
                    "" if ok else "驱动偏旧，GPU 加速可能不可用（会自动回落到 CPU）")]
    return [_ck("gpu", "加速", "NVIDIA 显卡与驱动", "warn",
                "未检测到 NVIDIA 显卡，将使用 CPU 模式", False, False,
                "CPU 模式可正常使用。中文素材建议选 SenseVoice，速度比 Whisper 快约 8 倍")]


def check_cuda_libs() -> list[dict]:
    """CUDA 运行库（pip 版 nvidia-* wheel）。缺了 GPU 就静默降级，必须显式检出。"""
    from app.doctor import env as doctor

    folders = doctor.register_cuda_dlls()
    if not folders:
        return [_ck("cuda_libs", "加速", "CUDA 运行库（cuBLAS / cuDNN）", "warn",
                    "未安装 pip 版 CUDA 运行库", False, True,
                    "安装后 GPU 加速可用", {"action": "pip", "packages": ["nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"]})]

    def find(pattern: str) -> str:
        for f in folders:
            try:
                hit = next(Path(f).glob(pattern), None)
                if hit:
                    return hit.name
            except Exception:  # noqa: BLE001
                continue
        return ""

    cublas = find("cublas64_*.dll")
    cudnn = find("cudnn*64_9*.dll")
    installed = []
    if cublas:
        installed.append("cuBLAS")
    if cudnn:
        installed.append("cuDNN")
    if cublas and cudnn:
        return [_ck("cuda_libs", "加速", "CUDA 运行库（cuBLAS / cuDNN）", "ok",
                    f"{' + '.join(installed)} 就绪 · {len(folders)} 个库路径已注册")]
    missing = "cuBLAS" if not cublas else "cuDNN"
    return [_ck("cuda_libs", "加速", "CUDA 运行库（cuBLAS / cuDNN）", "fail",
                f"缺少 {missing}（GPU 会静默降级到 CPU）", False, True,
                "重新安装 CUDA 运行库",
                {"action": "pip", "packages": ["nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"]})]


def check_engines() -> list[dict]:
    out = []
    # CTranslate2 —— Whisper 的推理后端
    if _has("ctranslate2"):
        ver = _pkg_version("ctranslate2")
        n = 0
        try:
            import ctranslate2

            n = ctranslate2.get_cuda_device_count()
        except Exception as exc:  # noqa: BLE001
            out.append(_ck("ctranslate2", "引擎", "CTranslate2（Whisper 后端）", "warn",
                           f"{ver} 已安装，但加载失败：{exc}", True, False,
                           "缺少 CUDA 运行库时会这样（见上一项）"))
            return out + _check_engine_pair("faster-whisper", "faster_whisper")
        out.append(_ck("ctranslate2", "引擎", "CTranslate2（Whisper 后端）", "ok",
                       f"v{ver} · 可用 GPU {n} 个"))
    else:
        out.append(_ck("ctranslate2", "引擎", "CTranslate2（Whisper 后端）", "fail",
                       "未安装，Whisper 引擎不可用", True, True,
                       "自动安装", {"action": "pip", "packages": ["faster-whisper"]}))

    out += _check_engine_pair("faster-whisper", "faster_whisper")

    # ★ 2026-09-30 迁移：SenseVoice 从 funasr + torch 改为 sherpa-onnx（纯 ONNX Runtime）
    #   这一项同时替代了原来的三项检查（torch / funasr / modelscope），
    #   总依赖体积从 3.6 GB 降到 104 MB，且不再需要显卡。
    if _has("sherpa_onnx"):
        out.append(_ck("sherpa_onnx", "引擎", "sherpa-onnx（SenseVoice 引擎）", "ok",
                       f"v{_pkg_version('sherpa_onnx')} · 纯 ONNX，不需要 PyTorch"))
    else:
        out.append(_ck("sherpa_onnx", "引擎", "sherpa-onnx（SenseVoice 引擎）", "fail",
                       "未安装，SenseVoice 引擎不可用（中文场景的主力引擎）", True, True,
                       "自动安装（仅 104 MB，不需要 PyTorch / 不需要显卡）",
                       {"action": "pip", "packages": ["sherpa-onnx", "sherpa-onnx-bin"]}))

    # Whisper 的模型从 HuggingFace 拉取，下载器依赖它
    if _has("huggingface_hub"):
        out.append(_ck("hf_hub", "引擎", "HuggingFace 下载器", "ok",
                       f"v{_pkg_version('huggingface_hub')}", False))
    else:
        out.append(_ck("hf_hub", "引擎", "HuggingFace 下载器", "warn",
                       "未安装，无法下载 Whisper 模型", False, True,
                       "自动安装", {"action": "pip", "packages": ["huggingface_hub"]}))
    return out


def _check_engine_pair(id_: str, mod: str) -> list[dict]:
    if _has(mod):
        return [_ck(id_, "引擎", "faster-whisper（识别引擎）", "ok", f"v{_pkg_version(mod)}")]
    return [_ck(id_, "引擎", "faster-whisper（识别引擎）", "fail",
                "未安装，Whisper 引擎不可用", True, True,
                "自动安装（含依赖）", {"action": "pip", "packages": ["faster-whisper"]})]


def check_models() -> list[dict]:
    from app.doctor import modelstore, registry

    out = []
    for m in registry.all_models():
        if m.get("planned"):
            continue
        if m["engine"] == "sensevoice" and not _has("sherpa_onnx"):
            continue                     # 框架都没装，模型检查没意义
        if m["engine"] == "whisper" and not _has("faster_whisper"):
            continue
        try:
            st = modelstore.install_state(m)
        except Exception as exc:  # noqa: BLE001
            out.append(_ck(f"model_{m['id']}", "模型", m["name"], "warn", f"检查失败：{exc}",
                           False, False))
            continue
        if st["state"] == "installed":
            out.append(_ck(f"model_{m['id']}", "模型", m["name"], "ok",
                           f"已安装 {st['installed_mb']:.0f} MB", False, False))
        elif st["state"] == "partial":
            out.append(_ck(f"model_{m['id']}", "模型", m["name"], "fail",
                           "文件不完整（下载中断过）", False, True,
                           "重新下载（会覆盖残缺文件）",
                           {"action": "download", "model_id": m["id"]}))
        else:
            # ★ 没装的模型既不是错误、也不是「需要留意的问题」——
            #   它只是一个还没做的可选项（模型按需下载本来就是设计的一部分）。
            #   所以单独给一个 "optional" 状态：不计入 warn、不影响结论、
            #   也不会被「诊断修复」批量拉进下载队列。
            dl = m.get("download_mb") or m["size_mb"]
            out.append(_ck(f"model_{m['id']}", "模型", m["name"], "optional",
                           f"未安装（下载 {dl} MB / 安装后 {m['size_mb']} MB，"
                           f"{m.get('recommend') or m.get('note', '')}）",
                           False, True, "", {"action": "download", "model_id": m["id"]}))
    return out


def check_storage() -> list[dict]:
    from app.doctor import env as doctor

    free = doctor.disk_free_gb()
    if free >= 10:
        st = "ok"
    elif free >= 3:
        st = "warn"
    else:
        st = "fail"
    return [_ck("disk", "存储", "磁盘剩余空间", st, f"{free} GB",
                True, False,
                "" if st == "ok" else "空间不足会导致模型下载与中间音频写不出来，建议清理磁盘")]


# ═══════════════════════════════════════════════════════════
# 汇总
# ═══════════════════════════════════════════════════════════


def check_all(use_cache: bool = True) -> dict:
    now = time.time()
    if use_cache and _CACHE["data"] and (now - _CACHE["at"]) < _CACHE_TTL:
        return _CACHE["data"]

    items: list[dict] = [check_python()]
    items += check_dirs()
    items += check_ffmpeg()
    items += check_gpu()
    items += check_cuda_libs()
    items += check_engines()
    items += check_models()
    items += check_storage()

    def count(s: str) -> int:
        return sum(1 for i in items if i["status"] == s)

    groups: list[str] = []
    for i in items:
        if i["group"] not in groups:
            groups.append(i["group"])

    data = {
        "items": items,
        "groups": groups,
        "ok": count("ok"),
        "warn": count("warn"),
        "fail": count("fail"),
        "optional": count("optional"),
        "total": len(items),
        "blocking": sum(1 for i in items if i["status"] == "fail" and i["required"]),
        # ★ problem = 真的需要处理的问题项（warn / fail）。
        #   optional（未安装的可选模型）不算问题，所以既不计入下面的 fixable，
        #   也不会被「诊断修复」一把拉走 —— 否则点一下就会开始下十几 GB 的模型。
        "problem": count("warn") + count("fail"),
        "fixable": sum(1 for i in items if i["status"] in ("fail", "warn") and i["fixable"]),
        "installable": sum(1 for i in items if i["status"] == "optional" and i["fixable"]),
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    data["usable"] = data["blocking"] == 0
    if not data["usable"]:
        data["verdict"] = f"有 {data['blocking']} 项必需组件缺失，可能无法正常转写"
    elif data["problem"]:
        data["verdict"] = f"可以运行，但有 {data['problem']} 项建议处理"
    else:
        data["verdict"] = "环境完整，可以正常使用"
    if data["optional"]:
        data["verdict"] += f"（另有 {data['optional']} 个模型未安装，需要时再下）"
    _CACHE.update({"at": now, "data": data})
    return data


def invalidate_cache() -> None:
    _CACHE["at"] = 0.0
    _CACHE["data"] = None


# ═══════════════════════════════════════════════════════════
# 修复执行器
#
# 装包可能几分钟，所以不能在 HTTP 请求里同步跑 ——
# 后台线程执行 + 前端轮询进度（与转写任务同一套思路）。
# ═══════════════════════════════════════════════════════════


class RepairRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.steps: list[dict] = []
        self.log: list[str] = []
        self.started_at = 0.0
        self.finished_at = 0.0
        self._stop = False

    # ---------- 状态 ----------
    def status(self) -> dict:
        with self._lock:
            done = sum(1 for s in self.steps if s["status"] in ("ok", "fail", "skip"))
            return {
                "running": self.running,
                "steps": [dict(s) for s in self.steps],
                "log": self.log[-150:],
                "elapsed": round(time.time() - self.started_at, 1) if self.started_at else 0.0,
                "done": done,
                "total": len(self.steps),
                "finished_at": self.finished_at,
            }

    def start(self, ids: list[str] | None = None) -> dict:
        if self.running:
            return {"ok": False, "message": "修复正在进行中，请等它跑完"}
        report = check_all(use_cache=False)
        candidates = [i for i in report["items"] if i["status"] != "ok" and i["fixable"]]
        if ids:
            # 显式点名（点某一行的按钮）→ 未安装的模型也允许处理
            want = set(ids)
            targets = [t for t in candidates if t["id"] in want]
        else:
            # 「诊断修复」一键 → 只修真正的问题项，不主动去下载可选模型
            targets = [t for t in candidates if t["status"] != "optional"]
        if not targets:
            return {"ok": False, "message": "没有需要修复的项目"}

        with self._lock:
            self.running = True
            self.started_at = time.time()
            self.finished_at = 0.0
            self._stop = False
            self.log = []
            self.steps = [{"id": t["id"], "label": t["label"], "status": "pending",
                           "message": "", "fix_hint": t.get("fix_hint", "")}
                          for t in targets]
        threading.Thread(target=self._run, args=(targets,), daemon=True).start()
        return {"ok": True, "count": len(targets)}

    def stop(self) -> dict:
        self._stop = True
        return {"ok": True}

    # ---------- 执行 ----------
    def _run(self, targets: list[dict]) -> None:
        try:
            for idx, t in enumerate(targets):
                if self._stop:
                    self._set(idx, "skip", "已跳过")
                    continue
                self._set(idx, "running", "执行中…")
                try:
                    self._set(idx, "ok", self._apply(t))
                except Exception as exc:  # noqa: BLE001
                    self._set(idx, "fail", self._humanize(exc))
                invalidate_cache()
        finally:
            with self._lock:
                self.running = False
                self.finished_at = time.time()

    def _set(self, idx: int, status: str, message: str) -> None:
        with self._lock:
            if 0 <= idx < len(self.steps):
                self.steps[idx]["status"] = status
                self.steps[idx]["message"] = message
                self.log.append(f"[{self.steps[idx]['label']}] {status} — {message}")

    def _apply(self, item: dict) -> str:
        fix = item.get("fix") or {}
        action = fix.get("action")
        if action == "pip":
            return self._pip(fix.get("packages") or [])
        if action == "download":
            return self._download(fix.get("model_id", ""))
        if action == "mkdir":
            p = Path(fix.get("path", ""))
            p.mkdir(parents=True, exist_ok=True)
            if not os.access(p, os.W_OK):
                raise PermissionError("目录已建立但没有写入权限")
            return f"已重建并可写：{p}"
        raise RuntimeError(f"没有对应的修复动作：{action}")

    def _log(self, text: str) -> None:
        with self._lock:
            self.log.append(text)

    def _pip(self, packages: list[str]) -> str:
        if not packages:
            raise RuntimeError("没有指定要安装的包")
        cmd = _pip_install_cmd(packages)
        self._log("$ " + " ".join(cmd))
        r = subprocess.run(cmd, capture_output=True, timeout=1800)
        out_txt = _decode(r.stdout) + _decode(r.stderr)
        if r.returncode != 0:
            tail = [x for x in out_txt.strip().splitlines() if x.strip()][-3:]
            raise RuntimeError("安装失败：" + " / ".join(tail)[:220])
        return f"已安装 {' '.join(packages)}"

    def _download(self, model_id: str) -> str:
        from app.doctor import modelstore

        if not model_id:
            raise RuntimeError("没有指定模型")
        modelstore.store.start_download(model_id)
        deadline = time.time() + 3600
        while time.time() < deadline:
            time.sleep(1.0)
            st = modelstore.store.downloads.get(model_id)
            if st is None:
                raise RuntimeError("下载任务丢失")
            if st.status != "running":
                if st.status == "done":
                    return f"下载完成（{st.total_mb:.0f} MB）"
                raise RuntimeError(f"下载未完成：{st.message}")
            with self._lock:
                for s in self.steps:
                    if s["id"] == f"model_{model_id}":
                        s["message"] = (f"下载中 {st.pct:.0f}%"
                                        f"（{st.downloaded_mb:.0f}/{st.total_mb:.0f} MB）")
                        break
        raise RuntimeError("下载超时（超过 1 小时）")

    @staticmethod
    def _humanize(exc: Exception) -> str:
        msg = str(exc)
        low = msg.lower()
        if "no module named pip" in low:
            return "当前 Python 环境里没有 pip，无法自动安装。请改用完整版运行环境。"
        if "permission" in low or "denied" in low:
            return "没有权限写入。请确认程序目录不是只读，或用管理员身份重试。"
        if "timed out" in low or "timeout" in low:
            return "超时（网络可能太慢），可以稍后重试。"
        return msg[:240]


runner = RepairRunner()
