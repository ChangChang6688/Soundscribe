"""声文 · 开发期重启脚本

为什么需要它：反复重启时，"先杀掉占用端口的旧进程"这一步很容易做错 ——

  × 用 `netstat -ano | grep :8765 | awk '{print $5}'` 取 PID 不可靠：
    TIME_WAIT 那几行的字段数不一样，取到的可能是 0 或错的行，
    结果是**旧进程仍活着**，而新进程 bind 失败报
    `WinError 10048 通常每个套接字地址只允许使用一次` 后静默退出。
    现象极具迷惑性：页面还能打开，但跑的是旧代码。

本脚本的做法：
  1. 只挑 netstat 里同时含该端口与 LISTENING 的行，取**最后一个字段**当 PID
  2. 逐个结束，然后**用真正的 bind 测试确认端口已释放**（不靠猜）
  3. 反向确认：真去 bind 一次，能绑上才算自由，立刻释放
  4. 后台启动新进程，日志写 data/logs/server.out
  5. 轮询 /api/health 直到就绪，并把 banner 打出来

用法：
  python tools/restart.py                 # 重启，默认端口 8765
  python tools/restart.py --port 9000
  python tools/restart.py --stop          # 只停不启
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "logs" / "server.out"
DEFAULT_PORT = 8765


# ─────────────────────────── 端口与进程 ───────────────────────────

def _decode(raw: bytes) -> str:
    """Windows 控制台程序（netstat / taskkill）的输出编码不一定是 UTF-8。

    ★ 不要用 subprocess 的 text=True：它会按 UTF-8 解码，中文系统上直接抛
      UnicodeDecodeError，而且异常发生在 reader 线程里，表现为 stdout 变成 None，
      报出来的错是 `'NoneType' object has no attribute 'splitlines'` —— 与真正的原因
      相距甚远。（这里只需要 ASCII 部分，所以逐级回退即可。）
    """
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def listeners_on(port: int) -> list[int]:
    """返回监听该端口的进程 PID 列表。只在 LISTENING 行里取最后一个字段。"""
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=20)
    except Exception:  # noqa: BLE001
        return []
    out = _decode(r.stdout or b"")
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[-2].upper() != "LISTENING":
            continue
        if not parts[1].endswith(f":{port}"):
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid and pid not in pids:
            pids.append(pid)
    return pids


def port_is_free(port: int) -> bool:
    """真去 bind 一次 —— 比看 netstat 可靠。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def kill(pid: int) -> bool:
    # 同 _decode 的说明：taskkill 的输出也是本地编码，不能用 text=True
    r = subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    return r.returncode == 0


def is_ours(port: int) -> bool:
    """端口上跑的是不是声文自己（避免误杀别的程序）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as x:
            d = json.load(x)
        return bool(d.get("ok")) and "version" in d
    except Exception:  # noqa: BLE001
        return False


def stop(port: int, verbose: bool = True) -> bool:
    pids = listeners_on(port)
    if not pids:
        if port_is_free(port):
            if verbose:
                print(f"  端口 {port} 本来就没有进程在监听")
            return True
        # netstat 没找到但 bind 不上：可能被别的机制占用
        if verbose:
            print(f"  ⚠ 端口 {port} 无法绑定，但 netstat 里找不到 LISTENING 进程")
        return False

    ours = is_ours(port)
    if verbose and not ours:
        print(f"  ⚠ 端口 {port} 上的进程没有响应声文的健康检查，可能是别的程序")

    killed = []
    for pid in pids:
        if kill(pid):
            killed.append(pid)
    if verbose:
        print(f"  已结束进程: {', '.join(map(str, killed)) or '（无）'}")

    for _ in range(40):                       # 最多等 4 秒
        if port_is_free(port):
            if verbose:
                print(f"  端口 {port} 已释放")
            return True
        time.sleep(0.1)
    if verbose:
        print(f"  ⚠ 端口 {port} 仍被占用（等待超时）")
    return False


# ─────────────────────────── 启动 ───────────────────────────

def start(port: int) -> subprocess.Popen:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    f = open(LOG, "w", encoding="utf-8")        # noqa: SIM115  子进程持有，不关
    creation = 0x00000008 | 0x00000200          # DETACHED_PROCESS | NEW_PROCESS_GROUP
    return subprocess.Popen(
        [sys.executable, "-u", "app/server/main.py", "--port", str(port)],
        cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=creation, close_fds=True,
    )


def wait_ready(port: int, seconds: float = 40.0) -> dict | None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health",
                                        timeout=2) as x:
                return json.load(x)
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    return None


def tail_log(n: int = 12) -> str:
    if not LOG.exists():
        return "(无日志)"
    lines = [x for x in LOG.read_text(encoding="utf-8", errors="replace").splitlines()
             if x.strip()]
    return "\n".join("  " + x for x in lines[-n:])


# ─────────────────────────── 主流程 ───────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--stop", action="store_true", help="只停服务，不启动")
    a = ap.parse_args()

    print("=" * 58)
    print(f"  声文 · {'停止服务' if a.stop else '重启服务'}（端口 {a.port}）")
    print("=" * 58)

    print("[1/3] 结束旧进程")
    stop(a.port)
    if a.stop:
        return 0

    if not port_is_free(a.port):
        print()
        print(f"  ✗ 端口 {a.port} 仍未释放，中止。")
        print("    可以换一个端口：python tools/restart.py --port 8766")
        return 1

    print()
    print("[2/3] 启动新进程")
    proc = start(a.port)
    print(f"  已拉起 pid={proc.pid}，日志：data/logs/server.out")

    print()
    print("[3/3] 等待就绪")
    info = wait_ready(a.port)
    if not info:
        print("  ✗ 超时未就绪。日志尾部：")
        print(tail_log())
        return 1

    print(f"  ✓ 就绪  v{info.get('version')}  http://127.0.0.1:{a.port}")
    print()
    print("  启动日志：")
    print(tail_log(8))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
