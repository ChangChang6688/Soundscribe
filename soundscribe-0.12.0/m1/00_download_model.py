"""M1 · 模型下载脚本

验证方案 §6.5 的两条设计：
  1. 走 hf-mirror 国内镜像（HF_ENDPOINT 覆盖）
  2. 模型落到项目自己的 models/ 目录，不用默认的 ~/.cache

用法：
  python 00_download_model.py [模型名]
默认模型：large-v3-turbo（A 档推荐）
"""

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

# —— 方案 §6.5：国内镜像优先 ——
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
# M1 实测教训：必须禁用 Xet 后端。
# Windows 未开启开发者模式时无法创建符号链接，Xet 会在 snapshots/ 下留下
# 0 字节占位文件，导致 ctranslate2 报 "model.bin is incomplete"。
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

MODEL_ID = sys.argv[1] if len(sys.argv) > 1 else "large-v3-turbo"


def resolve_repo(model_id: str) -> str:
    """从 faster-whisper 内置映射表解析真实仓库名。

    M1 实测教训：仓库名不能硬编码。
    faster-whisper 1.2.1 中 'large-v3-turbo' 实际指向
    mobiuslabsgmbh/faster-whisper-large-v3-turbo，而不是 Systran/ 前缀，
    硬编码会得到 401 RepositoryNotFound。改成读引擎自己的表，永远对得上。
    """
    from faster_whisper.utils import _MODELS

    if model_id in _MODELS:
        return _MODELS[model_id]
    if "/" in model_id:  # 用户直接给了 repo id
        return model_id
    raise SystemExit(f"[错误] 未知模型名 {model_id}，可选：{sorted(_MODELS)}")


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def dir_size(path: Path) -> int:
    total = 0
    if path.exists():
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    return total


def main() -> int:
    repo = resolve_repo(MODEL_ID)
    print(f"[镜像] HF_ENDPOINT = {os.environ['HF_ENDPOINT']}")
    print(f"[模型] {MODEL_ID}")
    print(f"[仓库] {repo}")
    print(f"[目标] {MODELS_DIR}")

    from huggingface_hub import snapshot_download

    # local_dir 模式：直接落真实文件，不建 blobs/snapshots 缓存结构、不做符号链接。
    # 这也正是方案主张的模型存放方式 —— models/ 下放裸模型文件，简单、可拷贝、可打包。
    target = MODELS_DIR / f"whisper-{MODEL_ID}"
    target.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    try:
        path = snapshot_download(
            repo_id=repo,
            local_dir=str(target),
            allow_patterns=[
                "config.json",
                "model.bin",
                "tokenizer.json",
                "preprocessor_config.json",
                "vocabulary.json",
            ],
            max_workers=8,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[失败] 镜像下载失败：{type(exc).__name__}: {exc}")
        print("[建议] 换源重试，或走离线补丁包通道")
        return 1

    elapsed = time.time() - t0
    snap = Path(path)
    size = dir_size(snap)
    print(f"[成功] 耗时 {elapsed:.1f}s，体积 {human(size)}")
    print(f"[路径] {snap}")
    for p in sorted(snap.iterdir()):
        if p.is_file():
            print(f"        {p.name:<28} {human(p.stat().st_size)}")

    # 把路径写入锁文件，供后续脚本复用
    (MODELS_DIR / "last_download.txt").write_text(str(snap), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
