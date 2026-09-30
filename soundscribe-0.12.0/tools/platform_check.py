"""声文 · 跨平台兼容性自检

★★ 为什么需要这个工具

Eli 的要求：「多平台多设备部署，Windows 和 Mac 一定要兼容，最后要封包上传 GitHub」。
而跨平台的问题有个共同特点：**在开发机（Windows）上完全看不出来**，
只会在同事的 Mac 上炸，而且报错信息往往指不到真正的原因。

所以这个工具把"Mac 上会不会炸"变成**可在 Windows 上静态验证**的检查项。
实测抓到的三类问题：

  ① **Windows-only API 没加守卫** —— 比如 `ctypes.WinDLL` / `winreg` /
     `os.startfile`。Windows 上跑得好好的，Mac 上 ImportError / AttributeError。
  ② **换行符** —— `.command` / `.sh` 被 Windows 编辑器改成 CRLF 后，
     macOS 双击报 `bad interpreter: /bin/bash^M`。
     **代码逻辑全对，死在换行上**，而且这个错极难联想到换行。
  ③ **分发文件缺失** —— 只有 start.bat 没有 start.command，
     或没有 requirements.txt，别人拿到包根本装不起来。

用法：
    python tools/platform_check.py            # 全量
    python tools/platform_check.py --json

退出码：0 全通过；1 有失败项。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, str, str]] = []      # (分组, 项目, 说明)
PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


def rec(group: str, item: str, status: str, detail: str = "") -> None:
    RESULTS.append((group, f"[{status}] {item}", detail))
    icon = {PASS: "OK  ", WARN: "WARN", FAIL: "FAIL"}[status]
    print(f"  {icon} {item}" + (f"  — {detail}" if detail else ""), flush=True)


# ═══════════════════════════════════════════════════════════
# ① Windows-only API 的守卫检查（AST，比正则可靠）
# ═══════════════════════════════════════════════════════════

# Windows 专属：出现在 Mac/Linux 上会直接报错
WIN_API_ATTRS = {
    "WinDLL", "windll", "startfile", "add_dll_directory",
    "CREATE_NO_WINDOW", "DETACHED_PROCESS", "NEW_PROCESS_GROUP",
    "GetFileAttributesW", "GetConsoleWindow",
}
WIN_API_MODULES = {"winreg", "msvcrt", "winsound", "ctypes.wintypes"}
# 只在 Windows 有意义的命令行工具
WIN_API_COMMANDS = ("taskkill", "netstat", "where ", "start \"\"")

# 出现这些标记就认为"作者意识到了跨平台问题"
GUARD_MARKERS = (
    "sys.platform", "platform.system", "os.name",
    "hasattr(", "getattr(", "IS_WINDOWS", "IS_MAC",
)


def _func_source(node: ast.AST, src: str) -> str:
    try:
        return ast.get_source_segment(src, node) or ""
    except Exception:  # noqa: BLE001
        return ""


def _docstring_nodes(node: ast.AST) -> set[int]:
    """收集"文档字符串"节点的 id。

    ★★ 必须排除 docstring，否则会误报 —— 实测第一次跑就报了
      `health.py::_decode 用了 netstat、taskkill`，而那其实是一句
      **文档字符串**（描述"Windows 控制台程序（pip / netstat / taskkill）
      的输出不一定是 UTF-8"），函数本身完全跨平台正确。

    ★ 这个教训在本项目出现过第二次了：另一个检查工具也曾把**注释**当成违规。
      对源码做文本/语法检查时，先想清楚"注释与文档算不算内容"。
      （AST 里注释不出现，只有 docstring 会以 Constant 形式出现。）
    """
    out: set[int] = set()
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Expr) and isinstance(sub.value, ast.Constant)
                and isinstance(sub.value.value, str)):
            out.add(id(sub.value))
    return out


def check_win_api_guards() -> None:
    print("\n【①】Windows 专属 API 是否都有跨平台守卫")
    findings: list[str] = []
    scanned = 0

    for py in sorted((ROOT / "app").rglob("*.py")):
        try:
            src = py.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except Exception:  # noqa: BLE001
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.Module)):
                continue
            body = _func_source(node, src)
            if not body:
                continue
            scanned += 1
            docs = _docstring_nodes(node)

            hits: list[str] = []
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and sub.attr in WIN_API_ATTRS:
                    hits.append(sub.attr)
                elif isinstance(sub, ast.Name) and sub.id in WIN_API_MODULES:
                    hits.append(sub.id)
                elif isinstance(sub, ast.Import):
                    for a in sub.names:
                        if a.name in WIN_API_MODULES:
                            hits.append(a.name)
                elif (isinstance(sub, ast.Constant)
                      and isinstance(sub.value, str)
                      and id(sub) not in docs):          # ★ 跳过文档字符串
                    for cmd in WIN_API_COMMANDS:
                        if cmd in sub.value:
                            hits.append(cmd.strip())
            hits = sorted(set(hits))
            if not hits:
                continue

            has_guard = any(m in body for m in GUARD_MARKERS)
            name = getattr(node, "name", "<module>")
            loc = f"{py.relative_to(ROOT)}::{name}"
            if has_guard:
                rec("win-api", f"{loc}", PASS, "、".join(hits) + "（有守卫）")
            else:
                findings.append(f"{loc} 用了 {'、'.join(hits)} 但没有守卫")
                rec("win-api", f"{loc}", FAIL,
                    "、".join(hits) + " —— 在 macOS 上会直接报错")

    if not findings:
        rec("win-api", f"扫描 {scanned} 个函数/模块，未发现无守卫的 Windows 专属调用",
            PASS)


# ═══════════════════════════════════════════════════════════
# ② 换行符：跨平台最隐蔽的一类问题
# ═══════════════════════════════════════════════════════════

def check_line_endings() -> None:
    print("\n【②】换行符（.command/.sh 带 CRLF 会让 macOS 直接打不开）")

    must_lf = [p for p in ROOT.rglob("*")
               if p.is_file() and p.suffix.lower() in (".command", ".sh", ".bash")
               and ".git" not in p.parts]
    must_crlf = [p for p in ROOT.rglob("*.bat")
                 if p.is_file() and ".git" not in p.parts]

    if not must_lf:
        rec("eol", "存在 shell 脚本", WARN, "没找到 .command/.sh")
    for p in must_lf:
        raw = p.read_bytes()
        has_crlf = b"\r\n" in raw
        rec("eol", f"{p.name} 使用 LF 换行", FAIL if has_crlf else PASS,
            "★ 含 CRLF，macOS 会报 bad interpreter: /bin/bash^M" if has_crlf else "")

    for p in must_crlf:
        raw = p.read_bytes()
        # .bat 里出现裸 LF 行尾（没有配对的 CR）才算出问题
        lf_only = raw.replace(b"\r\n", b"").count(b"\n")
        rec("eol", f"{p.name} 使用 CRLF 换行", WARN if lf_only else PASS,
            f"有 {lf_only} 处裸 LF（Windows 批处理可能出错）" if lf_only else "")

    ga = ROOT / ".gitattributes"
    ok = ga.exists()
    txt = ga.read_text(encoding="utf-8") if ok else ""
    rec("eol", ".gitattributes 强制了 shell 脚本用 LF",
        PASS if (ok and "*.command" in txt and "eol=lf" in txt) else FAIL,
        "缺了它，Windows 提交后 macOS 检出就是 CRLF" if not ok else "")


# ═══════════════════════════════════════════════════════════
# ③ 分发必需文件
# ═══════════════════════════════════════════════════════════

REQUIRED_FILES = [
    ("start.bat", "Windows 启动脚本"),
    ("start.command", "macOS 启动脚本"),
    ("requirements.txt", "依赖清单（别人拿到包才装得起来）"),
    (".gitattributes", "跨平台换行约定"),
    ("app/server/main.py", "服务入口"),
]


def check_dist_files() -> None:
    print("\n【③】分发必需文件（封包上传 GitHub 的完整度）")
    for rel, desc in REQUIRED_FILES:
        p = ROOT / rel
        rec("dist", f"{rel}（{desc}）", PASS if p.exists() else FAIL,
            "" if p.exists() else "缺失")
    # 可选但有价值
    for rel, desc in [("requirements-gpu.txt", "可选 GPU 依赖"), ("README.md", "仓库首页说明")]:
        p = ROOT / rel
        rec("dist", f"{rel}（{desc}）", PASS if p.exists() else WARN,
            "" if p.exists() else "建议补上")


# ═══════════════════════════════════════════════════════════
# ④ 硬编码的平台路径
# ═══════════════════════════════════════════════════════════

HARDCODED = [
    ("C:\\", "Windows 盘符绝对路径"),
    ("C:/", "Windows 盘符绝对路径"),
    ("Windows/Fonts", "Windows 字体目录"),
    ("AppData", "Windows 用户目录"),
    ("/Users/", "macOS 用户目录"),
    ("Program Files", "Windows 程序目录"),
]


def check_hardcoded_paths() -> None:
    print("\n【④】硬编码的平台路径（换台机器就找不到）")
    bad: list[str] = []
    for py in sorted((ROOT / "app").rglob("*.py")):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            st = line.strip()
            if st.startswith("#"):                 # 注释里提到没关系
                continue
            for pat, why in HARDCODED:
                if pat in line:
                    bad.append(f"{py.relative_to(ROOT)}:{i} {why}: {st[:64]}")
    if bad:
        for b in bad:
            rec("path", b, WARN, "确认是否会影响其它平台")
    else:
        rec("path", "app/ 里没有硬编码的平台路径", PASS)


# ═══════════════════════════════════════════════════════════
# ⑤ 平台分支的完整性
# ═══════════════════════════════════════════════════════════

def check_platform_branches() -> None:
    print("\n【⑤】平台分支是否有兜底（只写 win32 分支会坑到 Mac）")

    # ★ 判据要收窄，否则全是误报（第一次跑报了 7 条，其实都有意为之）：
    #   只要 if 块里出现了「提前返回 / raise / try」，就认为作者已经处理完
    #   那条路径，不需要 else。只有"什么都不做就往下走"的分支才值得提醒。
    HANDLED = (ast.Return, ast.Raise, ast.Try)

    suspicious: list[str] = []
    for py in sorted((ROOT / "app").rglob("*.py")):
        src = py.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except Exception:  # noqa: BLE001
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            seg = ast.get_source_segment(src, node) or ""
            if "sys.platform" not in seg and "platform.system" not in seg:
                continue
            if node.orelse:                       # 有 else → 已兜底
                continue
            if any(isinstance(x, HANDLED) for x in ast.walk(node)):
                continue                          # 提前返回/try → 已处理
            head = seg.split("\n", 1)[0].strip()[:70]
            suspicious.append(f"{py.relative_to(ROOT)}: {head}")

    if suspicious:
        for s in suspicious:
            rec("branch", s, WARN, "这个分支既不返回也不兜底，确认另一边平台的行为")
    else:
        rec("branch", "所有平台分支要么提前返回、要么有 else 兜底", PASS)


# ═══════════════════════════════════════════════════════════
# ⑥ 依赖声明的跨平台可用性
# ═══════════════════════════════════════════════════════════

# 只在这些平台有 wheel 的包（写进基础 requirements 就要说明）
PLATFORM_SPECIFIC = {
    "nvidia-cublas-cu12": "仅 Windows/Linux（NVIDIA）",
    "nvidia-cudnn-cu12": "仅 Windows/Linux（NVIDIA）",
    "nvidia-cuda-nvrtc-cu12": "仅 Windows/Linux（NVIDIA）",
    "pywin32": "仅 Windows",
    "pyobjc": "仅 macOS",
}


def check_requirements() -> None:
    print("\n【⑥】依赖清单的跨平台可用性")
    base = ROOT / "requirements.txt"
    if not base.exists():
        rec("deps", "requirements.txt 存在", FAIL)
        return
    lines = [x.strip() for x in base.read_text(encoding="utf-8").splitlines()]
    pkgs = [x.split("==")[0].split(">=")[0].strip()
            for x in lines if x and not x.startswith("#") and not x.startswith("-")]
    bad = [p for p in pkgs if p.lower() in PLATFORM_SPECIFIC]
    rec("deps", "基础依赖不含平台专属包（Mac 也装得上）",
        FAIL if bad else PASS,
        "、".join(f"{b}（{PLATFORM_SPECIFIC[b.lower()]}）" for b in bad) if bad
        else f"{len(pkgs)} 个包，均为跨平台")

    # ★ detail 要跟着实际结果走 —— 第一版把它写成了固定文案，
    #   于是出现"状态 OK 但说明说有问题"的自相矛盾输出。
    pinned = [x for x in lines
              if x and not x.startswith("#") and not x.startswith("-")]
    unpinned = [x for x in pinned if "==" not in x]
    rec("deps", "依赖版本钉死（保证另一台机器装出同一套）",
        WARN if unpinned else PASS,
        "未钉版本：" + "、".join(unpinned) if unpinned
        else f"{len(pinned)} 个条目全部钉死")


def check_repo_hygiene() -> None:
    """分发前的隐私与体积检查。

    ★★ 这一项是**实际检查时才发现的**，而且差点出事：
      准备上 GitHub 时先写了 .gitignore 忽略 data/ 和 models/，
      但 `git add --dry-run` 一跑才发现 **m1/ 里有 17 个文件也是敏感内容** ——
        · m1/app_122min_after_migration.txt  ← 122 分钟课程的完整转写稿
        · m1/onnx_*.txt                      ← 真实录音的转写结果
        · m1/route_*.json                    ← 含语言路由的**采样文本片段**
      再加上 `config/user/`（自加的纠错规则与热词，含公司名/人名）。
      这些和 data/ 里的音频是同一批内容的两种形态，**推上去就等于公开录音内容**。

    ★ 所以"目录级忽略"不够，得真的验证一遍**最终会被提交哪些文件**。
      靠人眼扫清单是扫不出来的 —— 这次就是靠 `git add --dry-run` 才看见的。
    """
    print("\n【⑦】分发前的隐私与体积（★ 别把用户数据推上去）")

    gi = ROOT / ".gitignore"
    if not gi.exists():
        rec("repo", ".gitignore 存在", FAIL,
            "没有它，git add . 会把模型和用户数据一起提交")
        return
    rec("repo", ".gitignore 存在", PASS)
    txt = gi.read_text(encoding="utf-8")

    # 关键：必须被忽略的路径（含隐私或大体积）
    must_ignore = [
        ("data/", "用户上传的原始音视频 + 转写结果（实测本机 6.0 GB，含隐私）"),
        ("models/", "模型文件（实测 5.2 GB，由用户按需下载）"),
        ("config/user/", "个人词库（自加规则/热词，含公司名与人名）"),
    ]
    for pat, why in must_ignore:
        rec("repo", f".gitignore 覆盖 {pat}", PASS if pat in txt else FAIL, why)

    # m1/ 下只该留脚本 —— 那些 txt/json 是转写产物
    ok_m1 = ("m1/*" in txt and "!m1/*.py" in txt)
    rec("repo", "m1/ 只保留脚本、忽略产物（产物里有转写稿）",
        PASS if ok_m1 else FAIL,
        "评测脚本有参考价值，但同目录的 .txt/.json 是真实录音的转写结果")

    # 有 git 仓库就做一次真实演练（最可靠）
    if (ROOT / ".git").is_dir():
        try:
            import subprocess

            def git(*a):
                return subprocess.run(["git", *a], cwd=str(ROOT), capture_output=True,
                                      text=True, timeout=60)

            r = git("add", "--dry-run", ".")
            files = [ln[5:-1] for ln in (r.stdout or "").splitlines() if ln.startswith("add '")]
            if files:
                leaked = [f for f in files
                          if any(f.startswith(p.rstrip("/")) for p, _ in must_ignore)]
                rec("repo", f"演练 git add：{len(files)} 个文件将入库",
                    FAIL if leaked else PASS,
                    "★ 有隐私文件会被提交：" + "、".join(leaked[:5]) if leaked
                    else "敏感目录均未被跟踪")
                # 转写产物（哪怕放在别的目录）也要拦
                txt_leak = [f for f in files
                            if f.lower().endswith((".txt", ".json"))
                            and "config/" not in f and "tools/" not in f
                            and not f.startswith(("requirements", "使用说明"))]
                rec("repo", "没有把转写产物（.txt/.json）当代码提交",
                    WARN if txt_leak else PASS,
                    "、".join(txt_leak[:5]) if txt_leak else "")
        except Exception as exc:  # noqa: BLE001
            rec("repo", "演练 git add", WARN, f"跳过：{type(exc).__name__}")
    else:
        rec("repo", "本地 git 仓库", WARN, "尚未 git init，无法演练；不影响运行")


def main() -> int:
    ap = argparse.ArgumentParser(description="声文 · 跨平台兼容性自检")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    print("=" * 66)
    print("  声文 · 跨平台兼容性自检（Windows ⇄ macOS）")
    print("=" * 66)

    check_win_api_guards()
    check_line_endings()
    check_dist_files()
    check_hardcoded_paths()
    check_platform_branches()
    check_requirements()
    check_repo_hygiene()

    n_fail = sum(1 for _, item, _ in RESULTS if item.startswith("[FAIL]"))
    n_warn = sum(1 for _, item, _ in RESULTS if item.startswith("[WARN]"))
    n_pass = len(RESULTS) - n_fail - n_warn

    print("\n" + "=" * 66)
    print(f"  通过 {n_pass} · 提示 {n_warn} · 失败 {n_fail}")
    if n_fail:
        print("  失败清单：")
        for g, item, d in RESULTS:
            if item.startswith("[FAIL]"):
                print(f"    X {item[7:]}  {d}")
    print("=" * 66)

    if args.json:
        print(json.dumps([{"group": g, "item": i, "detail": d}
                          for g, i, d in RESULTS], ensure_ascii=False, indent=2))
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
