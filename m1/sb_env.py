"""声文 · 运行时 DLL 路径引导

★ M1 实测教训（务必保留）★
pip 安装的 nvidia-cublas-cu12 / nvidia-cudnn-cu12 只是把 DLL 放进
    site-packages/nvidia/cublas/bin/cublas64_12.dll
    site-packages/nvidia/cudnn/bin/cudnn64_9.dll
Windows 的 DLL 搜索路径**不会**自动包含这些目录，于是 ctranslate2 报：
    RuntimeError: Library cublas64_12.dll is not found or cannot be loaded

必须在 `import ctranslate2 / faster_whisper` **之前**调用本模块，
把 nvidia 各子包的 bin 目录注册进 DLL 搜索路径。

这就是方案 §5「环境自检与自动补齐」里 Doctor 模块要做的第一件事：
程序启动时先引导 DLL 路径，再导入引擎。缺了这一步，GPU 路线直接不可用。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REGISTERED: list[str] = []


def _site_packages() -> list[Path]:
    found: list[Path] = []
    for entry in sys.path:
        if not entry:
            continue
        p = Path(entry)
        if p.name == "site-packages" and p.is_dir():
            found.append(p)
    return found


def register_nvidia_dlls() -> list[str]:
    """把 nvidia-* wheel 的 bin 目录加入 DLL 搜索路径。返回已注册目录列表。"""
    added: list[str] = []
    for sp in _site_packages():
        nv = sp / "nvidia"
        if not nv.is_dir():
            continue
        for sub in sorted(nv.iterdir()):
            bin_dir = sub / "bin"
            if not bin_dir.is_dir():
                continue
            # Python 3.8+ on Windows：推荐方式
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(str(bin_dir))
                except (OSError, FileNotFoundError):
                    pass
            # 兜底：PATH 前置，供底层 C 库按传统方式查找
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            added.append(str(bin_dir))
    return added


def ensure() -> list[str]:
    """幂等调用；返回已注册的 DLL 目录。"""
    global _REGISTERED
    if not _REGISTERED:
        _REGISTERED = register_nvidia_dlls()
    return _REGISTERED


def describe() -> str:
    """人类可读的引导结果，供诊断报告使用。"""
    dirs = ensure()
    if not dirs:
        return "未发现 nvidia-* wheel 的 bin 目录 → GPU 路线不可用，将走 CPU"
    lines = [f"已注册 {len(dirs)} 个 CUDA DLL 目录："]
    lines += [f"  · {d}" for d in dirs]
    return "\n".join(lines)


# 导入即生效 —— 调用方只要 `import sb_env` 就行
ensure()


if __name__ == "__main__":
    print(describe())
    import ctranslate2

    print(f"\nctranslate2 {ctranslate2.__version__}")
    print(f"cuda devices = {ctranslate2.get_cuda_device_count()}")
    print(f"cuda 支持的量化档 = {sorted(ctranslate2.get_supported_compute_types('cuda'))}")
