#!/bin/bash
# ============================================================
#  SoundScribe (声文) · macOS 启动脚本
# ============================================================
#
#  用法：
#    方式一：双击本文件（首次可能需要先执行一次 chmod +x start.command）
#    方式二：终端里执行  ./start.command
#
#  ★ 如果双击提示「无法打开，因为它来自身份不明的开发者」：
#     右键点击 → 打开 → 再点「打开」。之后就能正常双击了。
#
#  ★★ 本文件的换行必须是 LF（Unix 格式）。
#     Windows 上编辑后若变成 CRLF，macOS 会报
#     `bad interpreter: /bin/bash^M` 而无法运行。
#     仓库根目录的 .gitattributes 已强制 *.command 用 LF —— 别去掉那一行。
#
#  与 start.bat 的分工：两者做同一件事，只是各自适配自己的平台。
#  改了一边记得同步另一边（Python 解释器查找顺序要保持一致）。

cd "$(dirname "$0")" || exit 1

echo
echo "  ============================================================"
echo "    SoundScribe  -  local video / audio to text"
echo "  ============================================================"
echo

# ── 查找 Python 解释器 ──
# 顺序与 start.bat 保持一致：便携 runtime → 项目 .venv → 用户环境 → 系统
PY=""
for cand in \
  "$(pwd)/runtime/python/bin/python3" \
  "$(pwd)/.venv/bin/python3" \
  "$HOME/.workbuddy/binaries/python/envs/soundscribe/bin/python3"
do
  if [ -x "$cand" ]; then
    PY="$cand"
    break
  fi
done
if [ -z "$PY" ] && command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
fi

if [ -z "$PY" ]; then
  echo "  [ERROR] 找不到 Python 运行环境，无法启动。"
  echo
  echo "  已尝试这些位置："
  echo "    1. ./runtime/python/bin/python3"
  echo "    2. ./.venv/bin/python3"
  echo "    3. ~/.workbuddy/binaries/python/envs/soundscribe/bin/python3"
  echo "    4. 系统 python3"
  echo
  echo "  请把这个窗口截图发给我们。"
  echo
  read -n 1 -s -r -p "  按任意键关闭…"
  exit 1
fi

echo "  runtime : $PY"
echo
echo "  正在启动，请稍等几秒。"
echo "  浏览器会自动打开： http://127.0.0.1:8765"
echo
echo "  重要：请不要关闭这个终端窗口。"
echo "        它就是这个程序本身，关掉它就等于退出程序。"
echo

"$PY" -u app/server/main.py --open
RC=$?

echo
if [ "$RC" != "0" ]; then
  echo "  [NOTE] 退出码 $RC —— 请看上面的提示信息"
  echo
fi
echo "  已停止。现在可以关闭这个窗口了。"
echo
read -n 1 -s -r -p "  按任意键关闭…"
