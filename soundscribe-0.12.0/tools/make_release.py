"""声文 · 生成发布包（上 GitHub / 发给别人用）

★★ 为什么要有这个工具，而不是手动压缩

  手动压缩有两个必然会犯的错：
    ① **多带** —— 把 data/（用户上传的音视频，实测本机 6.0 GB）、
       models/（5.2 GB）一起打进去。既是体积炸弹也是隐私泄露。
    ② **漏带** —— 忘了 README、.gitattributes 或 start.command，
       别人拿到包要么不知道是什么，要么 Mac 上根本打不开。

  这个工具的做法是：**以 git 的"将被提交清单"为准**。
  那份清单已经被 `tools/platform_check.py` 的第 ⑦ 项验证过
  （敏感目录、转写产物都会被 `git check-ignore` 拦下），
  所以拿它来打包，既不漏也不多。

  产物：
    dist/soundscribe-<版本>/      ← 一个干净的文件夹（可直接拖进 GitHub 网页上传）
    dist/soundscribe-<版本>.zip   ← 同内容的压缩包（发给别人 / 备份用）

  ★ 为什么两种都给：GitHub 网页上传支持**拖文件夹**（会保留目录结构），
    但拖 zip 只会把它当成一个二进制文件上传，不会解压。
    所以要传仓库用文件夹，要发给别人用 zip。

用法：
    python tools/make_release.py
    python tools/make_release.py --no-zip      # 只要文件夹
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST_ROOT = ROOT / "dist"

# 打包时绝不能出现的路径（万一 git 清单不对，这里再兜一道）
FORBIDDEN_PREFIXES = ("data/", "models/", "output/", "dist/", "runtime/", ".git/")


def _version() -> str:
    """从 app/server/main.py 读版本号 —— 只此一处，不另存一份。"""
    import re

    src = (ROOT / "app" / "server" / "main.py").read_text(encoding="utf-8")
    m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', src)
    return m.group(1) if m else "0.0.0"


def _clean_file_list() -> list[str]:
    """从 git 拿"将被提交"的文件列表。

    ★ 这是本工具的关键：不自己猜"该带哪些文件"，
      而是问 git —— 那份清单已经过隐私与体积检查。
    """
    r = subprocess.run(["git", "add", "--dry-run", "."],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(
            f"git add --dry-run 失败（是否还没 git init？）：\n{r.stderr[:400]}")
    files = [ln[5:-1] for ln in (r.stdout or "").splitlines()
             if ln.startswith("add '") and ln.endswith("'")]
    if not files:
        raise RuntimeError("git 清单为空 —— 是不是所有文件都被 .gitignore 排除了？")

    # 兜底：再挡一道（清单异常时也不会把用户数据打进去）
    bad = [f for f in files if f.startswith(FORBIDDEN_PREFIXES)]
    if bad:
        raise RuntimeError(
            "★ 清单里出现了不该打包的路径（.gitignore 可能被改坏了）：\n  "
            + "\n  ".join(bad[:8]))
    return sorted(files)


def main() -> int:
    ap = argparse.ArgumentParser(description="声文 · 生成发布包")
    ap.add_argument("--no-zip", action="store_true", help="只生成文件夹，不打 zip")
    args = ap.parse_args()

    ver = _version()
    name = f"soundscribe-{ver}"
    target = DIST_ROOT / name
    zip_path = DIST_ROOT / f"{name}.zip"

    print("=" * 62)
    print(f"  声文 · 生成发布包  v{ver}")
    print("=" * 62)

    files = _clean_file_list()
    print(f"\n  待打包文件：{len(files)} 个（取自 git 的将被提交清单）")

    # 清掉上一次的产物（只在 dist/ 内操作，且数量可控）
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    total = 0
    for rel in files:
        src = ROOT / rel
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)          # copy2 保留时间戳；zip 不转义换行
        total += src.stat().st_size
    print(f"  已复制到：{target.relative_to(ROOT)}  （{total/1024/1024:.2f} MB）")

    if not args.no_zip:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for rel in files:
                z.write(target / rel, f"{name}/{rel}")
        print(f"  已打包：  {zip_path.relative_to(ROOT)}"
              f"  （{zip_path.stat().st_size/1024/1024:.2f} MB）")

    # ── 自检：打包结果必须干净且完整 ──
    print("\n  自检：")
    got = {str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()}
    print(f"    文件夹内文件数  {len(got)}  （应为 {len(files)}）"
          f"  {'OK' if len(got) == len(files) else '★ 不一致'}")
    leaked = [f for f in got if f.startswith(FORBIDDEN_PREFIXES)]
    print(f"    敏感路径泄漏    {len(leaked)}  {'OK' if not leaked else '★ ' + str(leaked[:3])}")

    must_have = ["README.md", "start.bat", "start.command",
                 "requirements.txt", ".gitattributes", ".gitignore"]
    missing = [m for m in must_have if m not in got]
    print(f"    必需文件齐全    {'OK' if not missing else '★ 缺 ' + '、'.join(missing)}")

    # 换行没被破坏（zip 与复制都不该改，但值得验一次）
    cmd = (target / "start.command").read_bytes()
    crlf = cmd.count(b"\r\n")
    print(f"    start.command   CRLF={crlf}  {'OK（纯 LF，macOS 可用）' if crlf == 0 else '★ 含 CRLF，Mac 打不开'}")

    print("\n" + "=" * 62)
    print("  怎么用：")
    print(f"    · 传到 GitHub：打开仓库页 → Add file → Upload files →")
    print(f"      把 **{name}** 整个文件夹拖进去（它会保留目录结构）")
    print(f"    · 发给别人：把 {name}.zip 发过去，解压后双击 start.bat / start.command")
    print("=" * 62)

    ok = (len(got) == len(files) and not leaked and not missing and crlf == 0)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
