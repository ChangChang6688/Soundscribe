"""声文 · Doctor —— 环境自检、DLL 引导与模型可用性门禁

职责（对应方案 §5 与 §6.7）：
  1. ★ 在导入任何推理引擎之前，把 pip 版 CUDA 库的 bin 目录注册进 DLL 搜索路径
  2. 检测硬件（GPU/显存/驱动/CPU/内存/磁盘）并判定硬件档位
  3. 对模型做可用性门禁（三态：可用 / 勉强可用 / 已禁用）
  4. 生成诊断报告

★ 关键约束：本模块必须在 `import ctranslate2` / `import funasr` 之前被导入。
   原因见 M1 实测坑 1：pip 装的 nvidia-cublas-cu12/nvidia-cudnn-cu12 只是把 DLL
   放进 site-packages/nvidia/*/bin，Windows 的 DLL 搜索路径不会自动包含它们，
   否则 ctranslate2 会报 "Library cublas64_12.dll is not found or cannot be loaded"。
"""

from __future__ import annotations

import ctypes
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent

# ═══════════════════════════════════════════════════════════
# 一、DLL 引导 —— 必须在导入引擎之前执行
# ═══════════════════════════════════════════════════════════

_REGISTERED_DLL_DIRS: list[str] = []


def _site_packages() -> list[Path]:
    out: list[Path] = []
    for entry in sys.path:
        if not entry:
            continue
        p = Path(entry)
        if p.name == "site-packages" and p.is_dir():
            out.append(p)
    return out


def register_cuda_dlls() -> list[str]:
    """把 nvidia-* wheel 的 bin 目录注册进 DLL 搜索路径。幂等。"""
    global _REGISTERED_DLL_DIRS
    if _REGISTERED_DLL_DIRS:
        return _REGISTERED_DLL_DIRS

    added: list[str] = []
    # 1) site-packages/nvidia/*/bin （pip 安装的 CUDA 运行库）
    for sp in _site_packages():
        nv = sp / "nvidia"
        if not nv.is_dir():
            continue
        for sub in sorted(nv.iterdir()):
            b = sub / "bin"
            if not b.is_dir():
                continue
            _add_dll_dir(b)
            added.append(str(b))

    # 2) 项目自带的 dlls/ 目录（预留：离线补丁包可往里放 DLL）
    local = ROOT / "runtime" / "dlls"
    if local.is_dir():
        _add_dll_dir(local)
        added.append(str(local))

    _REGISTERED_DLL_DIRS = added
    return added


def _add_dll_dir(p: Path) -> None:
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(str(p))
        except (OSError, FileNotFoundError):
            pass
    # 兜底：PATH 前置，供底层 C 库按传统方式查找
    os.environ["PATH"] = str(p) + os.pathsep + os.environ.get("PATH", "")


# 导入本模块即生效 —— 这是全应用的硬性顺序保证
register_cuda_dlls()


# ═══════════════════════════════════════════════════════════
# 二、硬件检测
# ═══════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════
# ★★ 推理后端的**真实**可用性探测
#
#   这一段存在的理由，和 capabilities.py 是同一个：
#   「这个后端能不能用」必须是一个**可被验证的结论**，而不是一句假设。
# ═══════════════════════════════════════════════════════════

_GPU_PROBE: tuple[bool, str] | None = None


def probe_sherpa_gpu(force: bool = False) -> tuple[bool, str]:
    """探测 sherpa-onnx 能不能真的用上显卡。返回 ``(可用, 人话说明)``。

    ★★ 为什么"看机器有没有显卡"远远不够：
      sherpa-onnx 在 provider 不可用时会**静默回落 CPU** ——
      只在 stderr 打一行 `Fallback to cpu`，Python 侧拿不到任何异常，
      调用方也无从区分「我传了 cuda」和「真的跑在 cuda」。
      实测：CPU-only 编译的包传 `provider='cuda'` 就是这种表现。
      （这和"Parakeet 热词被静默丢弃"是同一类问题的不同环节。）

    ★ 为什么不用"构建个模型试试"：那要加载几百 MB 模型、秒级起步，
      而我们只需要知道 provider 本身能不能用。直接看 sherpa 自带的
      provider DLL —— **文件在不在 + 能不能加载**，毫秒级，
      而且能区分三种情形（这才是它对用户有价值的地方）：

        文件不存在      → 这一版 sherpa-onnx 是 CPU-only 编译
        存在但加载失败  → 后端有、但缺 CUDA 运行库（实测缺 cufft64_11.dll）
        加载成功        → 真的可用

      结果缓存，探测只做一次。

    ★★ 2026-10-01 按 Eli 的要求定案：**兼容性优先，不追求最后那一下。**
      这个软件要同时跑在 Windows 和 Mac 上、最后要封包上传 GitHub，
      所以"每个平台都用最稳妥的后端"比"榨出最后一点速度"重要得多。
    """
    global _GPU_PROBE
    if _GPU_PROBE is not None and not force:
        return _GPU_PROBE

    # macOS：**不猜 CoreML，统一走 CPU**。
    #   ★ 为什么不乐观尝试：CoreML 的 provider 不像 CUDA 那样有独立的库文件，
    #     "这一版编译时有没有带"**无法从文件系统判断**；而猜错的后果是
    #     sherpa 静默回落 CPU —— 那就变成了"声称用了 GPU 其实没有"，
    #     比直接不用更糟（用户以为问题已解决）。
    #   ★ 而 Apple Silicon 的 CPU 本身就很快，sherpa 在 CPU 上已有 60×+，
    #     不值得为不确定的收益去冒这个险。
    if sys.platform == "darwin":
        _GPU_PROBE = (False, "macOS 按兼容性优先策略统一走 CPU"
                             "（CoreML 无法可靠探测；Apple Silicon 的 CPU 已足够快）")
        return _GPU_PROBE

    if not hasattr(ctypes, "WinDLL"):        # 其它平台（Linux 等）
        _GPU_PROBE = (False, "当前平台未启用 GPU 加速通道，使用 CPU")
        return _GPU_PROBE

    try:
        import sherpa_onnx
    except Exception as exc:  # noqa: BLE001
        _GPU_PROBE = (False, f"sherpa-onnx 不可用：{exc}")
        return _GPU_PROBE

    lib = Path(sherpa_onnx.__file__).parent / "lib"
    dll = lib / "onnxruntime_providers_cuda.dll"
    if not dll.exists():
        _GPU_PROBE = (False, "这一版 sherpa-onnx 是 CPU-only 编译（不含 CUDA 后端）")
        return _GPU_PROBE

    try:
        ctypes.WinDLL(str(dll))              # 只加载，不初始化：不产生推理开销
    except OSError as exc:
        _GPU_PROBE = (False, "CUDA 后端存在，但加载失败（多半缺 CUDA 运行库）："
                             f"{str(exc)[:160]}")
        return _GPU_PROBE

    _GPU_PROBE = (True, "CUDA 后端可用")
    return _GPU_PROBE


def _run(cmd: list[str], timeout: int = 15) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def cpu_name() -> str:
    if sys.platform == "win32":
        try:
            import winreg

            k = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            )
            return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except Exception:  # noqa: BLE001
            pass
    return platform.processor() or "unknown"


def total_ram_gb() -> float:
    if sys.platform == "win32":
        try:

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            m = MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return round(m.ullTotalPhys / 1024**3, 1)
        except Exception:  # noqa: BLE001
            pass
    return 0.0


def gpu_info() -> dict[str, Any]:
    """读 nvidia-smi。返回 name/driver/vram_mib/free_mib，无 N 卡则 available=False。"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"available": False, "reason": "未找到 nvidia-smi"}
    out = _run([exe, "--query-gpu=name,driver_version,memory.total,memory.free",
                "--format=csv,noheader,nounits"])
    if not out:
        return {"available": False, "reason": "nvidia-smi 无输出"}
    try:
        name, driver, total, free = [x.strip() for x in out.splitlines()[0].split(",")]
        return {
            "available": True,
            "name": name,
            "driver": driver,
            "vram_mib": int(total),
            "free_mib": int(free),
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"解析失败: {exc}"}


def disk_free_gb(path: Path | None = None) -> float:
    try:
        return round(shutil.disk_usage(str(path or ROOT)).free / 1024**3, 1)
    except Exception:  # noqa: BLE001
        return 0.0


def ffmpeg_ok() -> dict[str, Any]:
    exe = shutil.which("ffmpeg")
    if not exe:
        return {"available": False}
    v = _run([exe, "-version"], timeout=10)
    m = re.search(r"ffmpeg version (\S+)", v)
    return {"available": True, "path": exe, "version": m.group(1) if m else "unknown"}


# ═══════════════════════════════════════════════════════════
# 三、引擎能力探测（必须在 DLL 引导之后）
# ═══════════════════════════════════════════════════════════

_ENGINE_CAPS: dict[str, Any] | None = None


def engine_caps() -> dict[str, Any]:
    global _ENGINE_CAPS
    if _ENGINE_CAPS is not None:
        return _ENGINE_CAPS

    caps: dict[str, Any] = {}
    try:
        import ctranslate2

        caps["ctranslate2"] = ctranslate2.__version__
        caps["cuda_devices"] = ctranslate2.get_cuda_device_count()
        caps["cuda_compute_types"] = sorted(ctranslate2.get_supported_compute_types("cuda"))
        caps["cpu_compute_types"] = sorted(ctranslate2.get_supported_compute_types("cpu"))
        caps["whisper_ok"] = True
    except Exception as exc:  # noqa: BLE001
        caps["whisper_ok"] = False
        caps["whisper_error"] = f"{type(exc).__name__}: {exc}"
        caps["cuda_devices"] = 0

    # ★ 2026-09-30 迁移：SenseVoice 从 funasr + torch 改为 sherpa-onnx（纯 ONNX Runtime）。
    #   好处不只是省 3.5 GB 依赖 —— sensevoice_ok 的判定也不再依赖 torch 是否装好，
    #   而 sherpa-onnx 是自带推理引擎的单个包，装不上几乎只有一种原因：没装。
    try:
        import sherpa_onnx

        caps["sherpa_onnx"] = getattr(sherpa_onnx, "__version__", "?")
        caps["sensevoice_ok"] = True
    except Exception as exc:  # noqa: BLE001
        caps["sensevoice_ok"] = False
        caps["sensevoice_error"] = (
            "缺少 sherpa-onnx（pip install sherpa-onnx sherpa-onnx-bin）。"
            "它只有 104 MB，且不需要 PyTorch。"
        )
        caps["sensevoice_detail"] = f"{type(exc).__name__}: {exc}"

    # 保留 torch 的存在性记录：打包前用来判断"能不能把 torch 那 3.5 GB 砍掉"
    try:
        import torch  # noqa: F401

        caps["torch"] = torch.__version__
    except Exception:  # noqa: BLE001
        caps["torch"] = ""

    _ENGINE_CAPS = caps
    return caps


# ═══════════════════════════════════════════════════════════
# 四、硬件档位判定（方案 §6.3）
# ═══════════════════════════════════════════════════════════


def hardware_tier(gpu: dict, ram: float) -> tuple[str, str]:
    """判定硬件档位（方案 §6.3）。

    ★ 实现注意：用 MiB 直接比较，不要换算成 GB 再比。
      "8GB 显存"的卡实际报告 8188 MiB，换算是 7.996 GB —— 用 >= 8 判会误落到 B 档。
    """
    if gpu.get("available"):
        vram_mib = gpu.get("vram_mib", 0)
        if vram_mib >= 7600:          # 名义 8GB 及以上
            return "A", f"高性能 GPU（显存 {vram_mib} MiB）"
        if vram_mib >= 3800:          # 名义 4GB
            return "B", f"中端 GPU（显存 {vram_mib} MiB）"
        return "C", f"低端 GPU（显存 {vram_mib} MiB）"
    cores = os.cpu_count() or 1
    if ram >= 16 and cores >= 8:
        return "D", f"现代 CPU（{cores} 线程 / {ram:.0f}GB 内存）"
    if ram >= 8 and cores >= 4:
        return "E", f"普通 CPU（{cores} 线程 / {ram:.0f}GB 内存）"
    return "F", f"弱机兜底（{cores} 线程 / {ram:.0f}GB 内存）"


# ═══════════════════════════════════════════════════════════
# 五、模型可用性门禁（方案 §6.7）
# ═══════════════════════════════════════════════════════════


def gate_models(env: dict) -> list[dict]:
    """对每个模型的**每个量化档**做三态判定：可用 / 勉强可用 / 已禁用。

    ★ 关键设计：按 variant（量化档）判定，不是按模型整体禁用。
      同一个 Whisper large-v3 在 8GB 卡上 fp16 可用，在 4GB 卡上只有 int8 可用，
      没 N 卡的机器则只能用 int8(CPU) 档 —— 用户看到的不该是「这个模型不能用」，
      而是「这一档在你机器上用不了，但那一档可以」。
    """
    from app.doctor import modelstore, registry

    gpu = env["gpu"]
    ram = env["ram_gb"]
    disk = env["disk_free_gb"]
    caps = env["engine"]
    has_cuda = bool(caps.get("cuda_devices"))
    vram = gpu.get("vram_mib", 0)

    out: list[dict] = []
    for m in registry.all_models():
        try:
            st = modelstore.install_state(m)
        except Exception:  # noqa: BLE001
            st = {"state": "not_installed", "installed_mb": 0.0}

        engine_ok = True
        engine_reason = ""
        if m["engine"] == "whisper" and not caps.get("whisper_ok"):
            engine_ok = False
            engine_reason = "Whisper 引擎未就绪：" + str(caps.get("whisper_error", ""))[:120]
        if m["engine"] == "sensevoice" and not caps.get("sensevoice_ok"):
            engine_ok = False
            engine_reason = "SenseVoice 引擎未就绪（缺少 PyTorch）"

        variant_gates: list[dict] = []
        for v in m.get("variants", []):
            state = "ok"
            reasons: list[str] = []
            device = v.get("device", "cuda")
            need = int(v.get("vram_mb", 0) or 0)

            if not engine_ok:
                state, reasons = "blocked", [engine_reason]
            elif device == "cuda":
                if not has_cuda:
                    state = "blocked"
                    reasons.append(f"此档需约 {need/1024:.1f}GB 显存，本机未检测到可用 NVIDIA 显卡")
                elif need and vram < need:
                    state = "blocked"
                    reasons.append(f"此档需约 {need/1024:.1f}GB 显存，本机 {vram/1024:.1f}GB")
                elif need and vram < need * 1.3:
                    state = "risky"
                    reasons.append("显存较紧张，转写时建议关闭其他占用显卡的程序")
            elif device == "cpu":
                req_ram = int(m.get("ram_mb", 0) or 0)
                if req_ram and ram and ram * 1024 < req_ram:
                    state = "blocked"
                    reasons.append(f"内存需求约 {req_ram/1024:.1f}GB，本机 {ram:.1f}GB")

            variant_gates.append({
                "compute": v.get("compute"),
                "label": v.get("label", ""),
                "tag": v.get("tag", ""),
                "quality": v.get("quality", ""),
                "device": device,
                "vram_mb": need,
                "vram_display": f"{need/1024:.1f}GB" if need else "无需显卡",
                "state": state,
                "reasons": reasons,
            })

        # ---- 整体状态 ----
        if m.get("planned"):
            overall = "planned"
        elif not variant_gates:
            overall = "blocked"
        else:
            usable = [v for v in variant_gates if v["state"] != "blocked"]
            if not usable:
                overall = "blocked"
            elif any(v["state"] == "risky" for v in usable):
                overall = "risky"
            else:
                overall = "ok"

        # ---- 磁盘门禁（只影响「还没装、准备下载」的情况）----
        download_warn = ""
        if st["state"] != "installed" and disk and disk * 1024 < m.get("size_mb", 0) * 1.5:
            download_warn = (f"磁盘剩余 {disk:.1f}GB 偏紧"
                             f"（下载需约 {m.get('size_mb', 0)*1.5/1024:.1f}GB）")

        # ---- 推荐档：第一个可用的，按注册表里的优先级顺序 ----
        rec = next((v for v in variant_gates if v["state"] == "ok"), None)
        if rec is None:
            rec = next((v for v in variant_gates if v["state"] == "risky"), None)

        item = {
            "id": m["id"], "name": m["name"], "engine": m["engine"],
            "kind": m.get("kind"), "source": m.get("source", ""),
            "size_mb": m.get("size_mb", 0), "languages": m.get("languages", ""),
            "note": m.get("note", ""), "recommend": m.get("recommend", ""),
            "priority": m.get("priority", 99), "planned": bool(m.get("planned")),
            "state": overall,
            "installed": st["state"] == "installed",
            "install_state": st["state"],
            "installed_mb": st.get("installed_mb", 0.0),
            "path": st.get("path", ""),
            "download_warn": download_warn,
            "variants": variant_gates,
            "recommended": rec["compute"] if rec else "",
            "recommended_label": rec["label"] if rec else "",
            "vram_display": _vram_display(variant_gates),
        }
        item["reasons"] = _collect_reasons(item, engine_ok, engine_reason, download_warn)
        out.append(item)

    out.sort(key=lambda x: (x["planned"], x["priority"]))
    return out


def _vram_display(gates: list[dict]) -> str:
    nums = [g["vram_mb"] for g in gates]
    if not nums:
        return "—"
    lo, hi = min(nums), max(nums)
    if hi == 0:
        return "无需显卡"
    if lo == 0:
        return f"0 – {hi/1024:.1f}GB"
    return f"{lo/1024:.1f} – {hi/1024:.1f}GB"


def _collect_reasons(item: dict, engine_ok: bool, engine_reason: str,
                     download_warn: str) -> list[str]:
    """整体层面的提示（档位级原因在 variants 里逐档给出）。"""
    reasons: list[str] = []
    if not engine_ok:
        reasons.append(engine_reason)
    elif item["state"] == "blocked":
        # 汇总各档被禁的原因，取第一条最有代表性的
        firsts = [v["reasons"][0] for v in item["variants"] if v["state"] == "blocked" and v["reasons"]]
        if firsts:
            reasons.append(firsts[0])
            if len(set(firsts)) > 1:
                reasons.append(f"其余 {len(firsts)-1} 个量化档同样不可用")
    elif item["state"] == "risky":
        firsts = [v["reasons"][0] for v in item["variants"] if v["state"] == "risky" and v["reasons"]]
        if firsts:
            reasons.append(firsts[0])
    if download_warn:
        reasons.append(download_warn)
    return reasons


# ═══════════════════════════════════════════════════════════
# 六、汇总报告
# ═══════════════════════════════════════════════════════════


@dataclass
class EnvReport:
    os: str = ""
    python: str = ""
    cpu: str = ""
    cpu_cores: int = 0
    ram_gb: float = 0.0
    disk_free_gb: float = 0.0
    gpu: dict = field(default_factory=dict)
    ffmpeg: dict = field(default_factory=dict)
    dll_dirs: list = field(default_factory=list)
    engine: dict = field(default_factory=dict)
    tier: str = ""
    tier_reason: str = ""
    models: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def ready_engines() -> list[dict]:
    """当前「真正可用」的引擎：既装了运行时依赖，也下载了对应模型。

    ★ 为什么不只看 whisper_ok / sensevoice_ok：
      那只是"库在不在"，与"这个引擎现在能不能真的干活"是两件事
      —— 库装了但模型没下，引擎依然用不了。
      而且 sherpa-onnx 一家撑起三个引擎（SenseVoice / Parakeet / Moonshine），
      单看 capability 标志根本区分不开它们。
    """
    try:
        from app.doctor import modelstore as _ms
        from app.doctor import registry as _reg

        caps = engine_caps()
        out: list[dict] = []
        seen: set[str] = set()
        for m in _reg.all_models():
            eng = m.get("engine")
            if eng in seen:
                continue
            runtime = caps.get("whisper_ok") if eng == "whisper" else caps.get("sensevoice_ok")
            if not runtime:
                continue
            try:
                if _ms.install_state(m).get("state") != "installed":
                    continue          # 这个尺寸没装，看同引擎的下一个
            except Exception:  # noqa: BLE001
                continue
            seen.add(eng)
            out.append({"engine": eng, "model": m["name"], "display": eng})
        return out
    except Exception:  # noqa: BLE001
        return []


def full_report() -> dict:
    caps = engine_caps()
    gpu = gpu_info()
    ram = total_ram_gb()
    tier, tier_reason = hardware_tier(gpu, ram)

    rep = EnvReport(
        os=f"{platform.system()} {platform.release()} ({platform.version()[:40]})",
        python=sys.version.split()[0],
        cpu=cpu_name(),
        cpu_cores=os.cpu_count() or 0,
        ram_gb=ram,
        disk_free_gb=disk_free_gb(),
        gpu=gpu,
        ffmpeg=ffmpeg_ok(),
        dll_dirs=_REGISTERED_DLL_DIRS,
        engine=caps,
        tier=tier,
        tier_reason=tier_reason,
    )
    rep.models = gate_models(rep.to_dict())
    d = rep.to_dict()

    # ★ 把「推理后端到底能不能用」如实报出去。
    #   Eli 就是因为界面上看不到设备信息，才怀疑"是不是全跑在 CPU 上" ——
    #   而这种怀疑本来不该靠猜：探测结果是确定的，就把它显示出来。
    #   （详见 probe_sherpa_gpu() 的说明：provider 会静默回落，不能只看有没有显卡。）
    gpu_ok, gpu_why = probe_sherpa_gpu()
    d["sherpa_gpu"] = {"available": gpu_ok, "reason": gpu_why,
                       "active": "cuda" if gpu_ok else "cpu"}

    # ★ 推荐要在这里就做掉，不能只在 /api/models 里做：
    #   模型中心的清单实际是拿 state.env（来自 /api/env）渲染的，
    #   只在 /api/models 里排好序，页面上根本看不到分组和「为你推荐」。
    from app.doctor import recommend as _reco

    rec = _reco.rank(list(d["models"]), d)
    d["models"] = rec["models"]
    d["picks"] = rec["picks"]
    d["reco_summary"] = rec["summary"]
    d["reco_profile"] = rec["profile"]
    d["engines_ready"] = ready_engines()
    return d


if __name__ == "__main__":
    import json

    print(json.dumps(full_report(), ensure_ascii=False, indent=2))
