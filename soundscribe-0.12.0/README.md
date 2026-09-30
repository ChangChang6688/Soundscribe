# 声文 SoundScribe

**本地视频 / 音频转文字。离线、不上传、打开就能用。**

把一段录音或视频拖进来，得到带时间轴的文稿，可以直接导出 txt / srt / vtt / md。
所有识别都在你自己的电脑上完成 —— 素材不出本机，不联网也能跑。

---

## 快速开始

### Windows

1. 双击 **`start.bat`**
2. 等几秒，浏览器会自动打开 <http://127.0.0.1:8765>
3. **那个黑色窗口就是程序本身**，别关；关掉就等于退出。

### macOS

1. 首次使用先在终端里执行一次：`chmod +x start.command`
2. 双击 **`start.command`**
   （若提示"来自身份不明的开发者"：右键 → 打开 → 再点"打开"，之后就能正常双击）
3. 浏览器会自动打开 <http://127.0.0.1:8765>

> 两个启动脚本做的是同一件事，各自适配自己的平台。
> 改了一个记得同步另一个 —— 里面的 Python 查找顺序要保持一致。

---

## 需要准备什么

| 东西 | 说明 |
|---|---|
| **Python 3.10+** | 启动脚本会自动去找。找不到会明确告诉你试过哪些路径 |
| **ffmpeg** | 负责抽音轨和切段。Windows 把 `ffmpeg.exe` / `ffprobe.exe` 放进 PATH；macOS `brew install ffmpeg` |
| **模型** | 首次使用在界面「模型中心」里下，按机器配置会给推荐 |

依赖安装：

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt     # Windows
.venv/bin/pip     install -r requirements.txt     # macOS / Linux
```

想给 Whisper 开显卡加速，再叠加 `pip install -r requirements-gpu.txt`（约 2 GB，仅 NVIDIA）。

---

## 四个识别引擎

界面里可以逐文件选择，默认按语言自动判定。

| 引擎 | 适合 | 备注 |
|---|---|---|
| **SenseVoice** | 中文 | 中文最准，速度最快 |
| **Parakeet** | 纯英文 | 英文专用，CPU 上比 Whisper 快约 5 倍 |
| **Moonshine** | 纯英文 · 体积最小 | 英文专用 |
| **Whisper** | 通用 / 中英夹杂 / 需要翻译 | 唯一支持翻译的引擎 |

**自动判定的规则**：中文为主 → SenseVoice；纯英文 → Parakeet；
中英夹杂或拿不准 → Whisper（通用引擎，最稳妥）。

> ⚠️ Parakeet 和 Moonshine **不认中文**。中文素材喂给它们会输出乱码，
> 所以语言阈值卡得很严。拿不准时选 Whisper。

---

## 跨平台说明

这个软件的目标是 **Windows 和 macOS 都能开箱即用**，所以有几个刻意做的取舍：

- **Windows / Linux**：会探测显卡。装了 `requirements-gpu.txt` 且显卡可用时，
  Whisper 自动走 CUDA（实测快 15 倍）；sherpa 三个引擎始终走 CPU
  （它们的 pip 包是 CPU-only 编译，传 CUDA 也只会静默回落）。
- **macOS**：**统一走 CPU**。CoreML 通道没法可靠探测，"猜错了会静默回落"
  比"不用"更糟；而 Apple Silicon 的 CPU 本身已经够快。
- **界面就是网页**：本地起服务、浏览器访问，所以两端界面完全一致。

想强制指定后端（排查/对比用）：

```bash
SOUNDSCRIBE_PROVIDER=cpu        # macOS/Linux
set SOUNDSCRIBE_PROVIDER=cpu    # Windows
```

---

## 自检工具

改完代码按这个顺序跑，出问题会直接告诉你哪里不对：

```bash
node tools/front_check.js                       # 前端静态一致性（秒级）
python tools/platform_check.py                  # 跨平台兼容（秒级）
python tools/route_check.py                     # 语言路由边界（秒级）
python tools/effectiveness_check.py --static    # 设置项是否真的生效·静态层
python tools/release_check.py                   # 接口 / 异常路径 / 记录落盘
node tools/ui_smoke_test.js                     # 真实浏览器全量交互
python tools/effectiveness_check.py             # 设置项生效·含运行时
```

其中几个是踩坑踩出来的，值得单独说：

- **`platform_check.py`** —— 跨平台问题在开发机上完全看不出来。
  它做 AST 分析找出没加守卫的 Windows 专属调用，还会检查
  `.command` 的换行符（CRLF 会让 macOS 报 `bad interpreter: /bin/bash^M`，
  **代码全对，死在换行上**）。
- **`effectiveness_check.py`** —— 治的是"界面上有这个设置、实际没生效"。
  三层核验：表自检 → 参数名是否真在引擎签名里 → 真跑一遍读运行时证据。
- **`route_check.py`** —— 只测边界。曾经 128 项界面测试全绿的同时，
  一个"素材短于 30 秒"的边界让中文素材被路由到不认中文的模型。

---

## 目录

```
app/
  server/       HTTP 服务与任务管线
  pipeline/     引擎适配 / 媒体处理 / 后处理 / 语言路由
  doctor/       环境探测、模型仓库、能力矩阵
  web/          界面（原生 JS，无构建步骤）
config/         内置热词与纠错规则（只读基线）
data/           运行时数据：任务记录、导出、缓存、日志
models/         模型文件（自行下载）
tools/          上面那些自检工具
```

---

## 已知限制

- **低置信标注不产出**：当前所有引擎都拿不到逐句置信度，界面上已如实说明。
  重复退化会用「待复核」标记兜底。
- **说话人分离未做**：技术上可行（依赖里已带），但缺少真实多人录音素材来调参。
  重叠说话本就无法可靠分离。
- **词级时间轴未开放**：识别结果里已带字级时间戳，导出环节还没接上。
- **内置纠错规则是按 Whisper 的错误模式整理的**，而中文现在默认走 SenseVoice，
  两者错误类型不同，所以这部分规则命中率偏低 —— 界面「词库」页会如实提示。
