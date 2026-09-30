"""M1 · 环境与加速能力自检

对应方案 §5.1 检测项矩阵与 §6.7 门禁判定：
  - ctranslate2 能否看到 CUDA 设备（决定 A/B 档是否成立）
  - 各 device 支持的 compute_type（决定量化档位可选范围）
  - 显存与驱动信息（决定档位判定）

输出直接用于回填方案的「档位判定」与「量化档可选表」。
"""

import json
import platform
import shutil
import subprocess
import sys


def hw() -> dict:
    info: dict = {
        "os": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
    }
    exe = shutil.which("nvidia-smi")
    if exe:
        try:
            out = subprocess.run(
                [
                    exe,
                    "--query-gpu=name,driver_version,memory.total,memory.free",
                    "--format=csv,noheader",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            ).stdout.strip()
            info["nvidia_smi"] = out
        except Exception as exc:  # noqa: BLE001
            info["nvidia_smi"] = f"ERR {exc}"
    else:
        info["nvidia_smi"] = "not found"
    return info


def ctranslate_info() -> dict:
    import ctranslate2

    out: dict = {"version": ctranslate2.__version__}
    try:
        out["cuda_device_count"] = ctranslate2.get_cuda_device_count()
    except Exception as exc:  # noqa: BLE001
        out["cuda_device_count"] = f"ERR {type(exc).__name__}: {exc}"

    types: dict = {}
    for dev in ("cuda", "cpu", "auto"):
        try:
            types[dev] = sorted(ctranslate2.get_supported_compute_types(dev))
        except Exception as exc:  # noqa: BLE001
            types[dev] = f"ERR {type(exc).__name__}: {exc}"
    out["supported_compute_types"] = types
    return out


def main() -> int:
    print("=" * 62)
    print("声文 M1 · 环境与加速能力自检")
    print("=" * 62)

    h = hw()
    print("\n[硬件]")
    for k, v in h.items():
        print(f"  {k:<12} {v}")

    print("\n[ctranslate2]")
    ct = ctranslate_info()
    print(f"  version            {ct['version']}")
    print(f"  cuda_device_count  {ct['cuda_device_count']}")
    for dev, value in ct["supported_compute_types"].items():
        print(f"  {dev:<18} {value}")

    n = ct.get("cuda_device_count") if isinstance(ct, dict) else None
    print("\n[初步结论]")
    if isinstance(n, int) and n > 0:
        print("  CUDA 可用 → 本机应落在 A/B 档，走 GPU 加速路线")
    else:
        print("  CUDA 不可用 → 将降级到 CPU int8（对应 D/E/F 档）")
        print("  排查：确认已装 nvidia-cublas-cu12 与 nvidia-cudnn-cu12 9.x")

    (out := __import__("pathlib").Path(__file__).parent / "env_report.json").write_text(
        json.dumps({"hardware": h, "ctranslate2": ct}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[落盘] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
