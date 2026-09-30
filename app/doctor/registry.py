"""声文 · 模型注册表（纯数据，无副作用）

★ 一处关键设计修正：模型按「文件」粒度注册，量化档（float16 / int8_float16 / int8）
  是**运行参数**，不是独立文件。

  原方案把「large-v3-turbo int8」和「large-v3-turbo f16」列成两个可下载条目 ——
  实际它们是**同一份 CT2 权重文件**，只是运行时精度不同。照原样做会让用户
  白下 1.6GB 重复文件。这里改为：一个模型 → 多个 variants，门禁对 variant 判定。

★ 仓库名绝不硬编码（M1 实测坑 3）：
  按直觉写 "Systran/faster-whisper-large-v3-turbo" 会 401，
  看起来像「镜像要鉴权」，实际是仓库不存在 —— turbo 版由 mobiuslabsgmbh 发布。
  故一律从 faster-whisper 自带的映射表动态读取。
"""

from __future__ import annotations

from typing import Any

MODELS: list[dict[str, Any]] = [
    {
        "id": "sensevoice-small",
        "name": "SenseVoice-Small（ONNX · int8）",
        "engine": "sensevoice",
        # ★ 2026-09-30：从 funasr(ModelScope) 改为 sherpa-onnx 的 ONNX 版本
        #   901 MB → 230 MB，且不再需要 PyTorch（依赖省了 3.5 GB）
        "kind": "sherpa",
        "dir": "models/sherpa/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
        "tar_dir": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
        # ★ 下载源分两组，让用户显式选择（国内用户走镜像，海外走官方）
        #   两组都保留：选的那组失败时仍会自动回退到另一组。
        "mirror_urls": [
            "https://gh-proxy.com/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
            "https://ghproxy.net/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
        ],
        "official_url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
        # ★ 显式声明"装好"需要哪些文件：不要靠 glob 猜文件名，
        #   不同模型的命名完全不一样（Parakeet 是 encoder/decoder/joiner，
        #   Moonshine 是 preprocess/encode/cached_decode…），猜必错。
        "required": ["model.int8.onnx", "tokens.txt"],
        "size_mb": 230,          # 安装后占磁盘
        "download_mb": 156,      # 实际下载量（bz2 压缩包）
        "languages": "中文（含粤语）· 英日韩",
        "note": "中文主力 · 纯 ONNX，不需要 PyTorch · CPU 上 35–43 倍速",
        "recommend": "中文素材默认选择",
        "priority": 1,
        "variants": [
            # SenseVoice 在 CPU 上已经 35–43×（1 小时录音 84–103 秒），
            # 因此**不提供 GPU 档** —— 把显卡完整留给 Whisper。
            {"compute": "cpu", "label": "CPU 运行", "vram_mb": 0,
             "quality": "约 35–43 倍速", "device": "cpu", "tag": "无需显卡"},
        ],
    },
    {
        "id": "whisper-large-v3-turbo",
        "name": "Whisper large-v3-turbo",
        "engine": "whisper",
        "kind": "whisper",
        "fw_name": "large-v3-turbo",
        "source": "HuggingFace",
        "dir": "models/whisper-large-v3-turbo",
        "size_mb": 1600,
        "languages": "英文 / 中英夹杂 / 99 种语言",
        "note": "英文首选 · 实测 GEO 20:11、ChatGPT 4:0 领先 SenseVoice",
        "recommend": "英文与中英夹杂素材",
        "priority": 2,
        "variants": [
            {"compute": "float16", "label": "fp16 精度优先", "vram_mb": 3600,
             "quality": "最高", "device": "cuda", "tag": "GPU"},
            {"compute": "int8_float16", "label": "fp16 显存精简", "vram_mb": 2400,
             "quality": "几乎无损", "device": "cuda", "tag": "GPU"},
            {"compute": "int8", "label": "int8 CPU", "vram_mb": 0,
             "quality": "约 3 倍速", "device": "cpu", "tag": "无需显卡"},
        ],
    },
    {
        "id": "whisper-large-v3",
        "name": "Whisper large-v3",
        "engine": "whisper",
        "kind": "whisper",
        "fw_name": "large-v3",
        "source": "HuggingFace",
        "dir": "models/whisper-large-v3",
        "size_mb": 3100,
        "languages": "99 种语言 / 可翻译成英文",
        "note": "支持翻译成英文 · 比 turbo 略准但慢一倍",
        "recommend": "需要英文翻译或最高精度时",
        "priority": 4,
        "variants": [
            {"compute": "float16", "label": "fp16 精度优先", "vram_mb": 5400,
             "quality": "最高", "device": "cuda", "tag": "GPU"},
            {"compute": "int8_float16", "label": "fp16 显存精简", "vram_mb": 3400,
             "quality": "几乎无损", "device": "cuda", "tag": "GPU"},
            {"compute": "int8", "label": "int8 CPU", "vram_mb": 0,
             "quality": "很慢（1 小时约 40 分）", "device": "cpu", "tag": "无需显卡"},
        ],
    },
    {
        "id": "distil-large-v3.5",
        "name": "Distil-Whisper large-v3.5",
        "engine": "whisper",
        "kind": "whisper",
        "fw_name": "distil-large-v3.5",
        "source": "HuggingFace",
        "dir": "models/distil-large-v3.5",
        "size_mb": 1500,
        "languages": "纯英文",
        "note": "纯英文素材比 turbo 更快，但只支持英文（中文会输出乱码）",
        "recommend": "纯英文大批量场景",
        "priority": 5,
        "variants": [
            {"compute": "float16", "label": "fp16 精度优先", "vram_mb": 3400,
             "quality": "最高", "device": "cuda", "tag": "GPU"},
            {"compute": "int8_float16", "label": "fp16 显存精简", "vram_mb": 2200,
             "quality": "几乎无损", "device": "cuda", "tag": "GPU"},
        ],
    },
    {
        "id": "whisper-medium",
        "name": "Whisper medium",
        "engine": "whisper",
        "kind": "whisper",
        "fw_name": "medium",
        "source": "HuggingFace",
        "dir": "models/whisper-medium",
        "size_mb": 1530,
        "languages": "99 种语言",
        "note": "中等显存机器的折中档",
        "recommend": "4GB 显存档位",
        "priority": 6,
        "variants": [
            {"compute": "float16", "label": "fp16", "vram_mb": 2300,
             "quality": "较好", "device": "cuda", "tag": "GPU"},
            {"compute": "int8_float16", "label": "fp16 精简", "vram_mb": 1500,
             "quality": "几乎无损", "device": "cuda", "tag": "GPU"},
            {"compute": "int8", "label": "int8 CPU", "vram_mb": 0,
             "quality": "慢", "device": "cpu", "tag": "无需显卡"},
        ],
    },
    {
        "id": "whisper-small",
        "name": "Whisper small",
        "engine": "whisper",
        "kind": "whisper",
        "fw_name": "small",
        "source": "HuggingFace",
        "dir": "models/whisper-small",
        "size_mb": 490,
        "languages": "99 种语言",
        "note": "无显卡机器的英文可用档",
        "recommend": "普通 CPU 机器",
        "priority": 7,
        "variants": [
            {"compute": "int8", "label": "int8 CPU", "vram_mb": 0,
             "quality": "一般", "device": "cpu", "tag": "无需显卡"},
            {"compute": "float16", "label": "fp16", "vram_mb": 900,
             "quality": "较好", "device": "cuda", "tag": "GPU"},
        ],
    },
    {
        "id": "whisper-base",
        "name": "Whisper base",
        "engine": "whisper",
        "kind": "whisper",
        "fw_name": "base",
        "source": "HuggingFace",
        "dir": "models/whisper-base",
        "size_mb": 150,
        "languages": "99 种语言",
        "note": "弱机兜底 · 质量一般但任何现代电脑都能跑",
        "recommend": "4 核以下的老机器",
        "priority": 8,
        "variants": [
            {"compute": "int8", "label": "int8 CPU", "vram_mb": 0,
             "quality": "一般", "device": "cpu", "tag": "无需显卡"},
        ],
    },
    # ── 英文专用模型（2026-09-30 加入）────────────────────────────
    # ★ 为什么加这两个：Whisper 在 CPU 上只有 4.7×（1 小时 12.7 分钟），
    #   而 Mac 上 CTranslate2 没有 Metal 后端，只能跑 CPU → 英文体验很差。
    #   实测换用英文专用的小模型后，**CPU 上就能到 23–25×（1 小时 2.4 分钟）**，
    #   质量与 Whisper 词级 99.2–99.3% 一致。Mac 因此不再需要 GPU。
    #   ⚠️ 二者都只认英文：不认中文、不做翻译 → 中英夹杂和翻译仍归 Whisper。
    {
        "id": "parakeet-en",
        "name": "Parakeet TDT 0.6B v2（英文专用）",
        "engine": "parakeet",
        "kind": "sherpa",
        "dir": "models/sherpa/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8",
        "tar_dir": "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8",
        # ★ 下载源分两组，让用户显式选择（国内用户走镜像，海外走官方）
        #   两组都保留：选的那组失败时仍会自动回退到另一组。
        "mirror_urls": [
            "https://gh-proxy.com/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2",
            "https://ghproxy.net/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2",
        ],
        "official_url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2",
        "required": ["encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt"],
        "size_mb": 631,
        "download_mb": 460,
        "languages": "英文",
        "note": "★ 纯英文首选 · CPU 上 25 倍速（1 小时录音约 2.4 分钟）· 不认中文",
        "recommend": "英文素材（尤其是 Mac / 无显卡机器）",
        "priority": 3,
        "variants": [
            {"compute": "cpu", "label": "CPU 运行", "vram_mb": 0,
             "quality": "约 25 倍速", "device": "cpu", "tag": "无需显卡"},
        ],
    },
    {
        "id": "moonshine-en",
        "name": "Moonshine base.en（英文 · 极小）",
        "engine": "moonshine",
        "kind": "sherpa",
        "dir": "models/sherpa/sherpa-onnx-moonshine-base-en-int8",
        "tar_dir": "sherpa-onnx-moonshine-base-en-int8",
        # ★ 下载源分两组，让用户显式选择（国内用户走镜像，海外走官方）
        #   两组都保留：选的那组失败时仍会自动回退到另一组。
        "mirror_urls": [
            "https://gh-proxy.com/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-moonshine-base-en-int8.tar.bz2",
            "https://ghproxy.net/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-moonshine-base-en-int8.tar.bz2",
        ],
        "official_url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-moonshine-base-en-int8.tar.bz2",
        "required": ["preprocess.onnx", "encode.int8.onnx",
                     "uncached_decode.int8.onnx", "cached_decode.int8.onnx", "tokens.txt"],
        "size_mb": 274,
        "download_mb": 239,
        "languages": "英文",
        "note": "★ 体积最小（274 MB）· CPU 上 23.4 倍速 · 空间紧张时选它 · 不认中文",
        "recommend": "英文素材 · 磁盘紧张（Mac 尤其适用）",
        "priority": 4,
        "variants": [
            {"compute": "cpu", "label": "CPU 运行", "vram_mb": 0,
             "quality": "约 23 倍速", "device": "cpu", "tag": "无需显卡"},
        ],
    },
]



# ═══════════════════════════════════════════════════════════
# 预留位：尚未接入、且当前不打算接入的模型
#
# ★ 2026-09-18 决策：**声纹/说话人分离暂不做**（Eli 判断"太麻烦"）。
#   技术上不难（CAM++ 30MB 免授权，funasr + torch 都是现成依赖），
#   真正的成本在"聚类阈值必须用真实多人录音反复调"——没有合适素材就推不动。
#   他的素材以单人授课为主，声纹在那种场景下几乎没用（全程同一个人）。
#
#   因此这里**不登记任何条目**：界面上不该出现"后续版本"这种悬着的承诺，
#   那会让用户（尤其拿到这份软件的同事）以为有东西还没装。
#
#   将来若要启用，把下面这段注释直接恢复成一条字典即可，其余代码无需改动
#   —— 门禁（env.py）、体检（health.py）、下载（modelstore.py）、
#      模型中心（app.js 的 planned → "后续版本" 徽标）都已支持 planned 状态：
#
#   {
#       "id": "campplus-zh",
#       "name": "CAM++ 声纹（说话人分离）",
#       "engine": "speaker",
#       "kind": "funasr",
#       "source": "ModelScope · iic/speech_campplus_sv_zh-cn_16k-common",
#       "size_mb": 30,
#       "languages": "中文",
#       "note": "免授权 · 30MB · 分离后可直接命名角色（「说话人1」→「舒总」）",
#       "recommend": "多人会议",
#       "planned": True,
#       "variants": [],
#   },
#
#   前置条件（真要做时）：① 需先拿到一份真实的多人会议录音用于调阈值
#                        ② 聚类必须"全篇一次"，逐块独立聚类会让编号在时间轴上跳变
#                        ③ 重叠说话无法分离，界面上要如实标注而不是硬给答案
# ═══════════════════════════════════════════════════════════
PLANNED_MODELS: list[dict[str, Any]] = []


def all_models() -> list[dict]:
    return MODELS + PLANNED_MODELS


def get(model_id: str) -> dict | None:
    for m in all_models():
        if m["id"] == model_id:
            return m
    return None


def whisper_repo(fw_name: str) -> str:
    """从 faster-whisper 的内置映射取仓库名，绝不硬编码。

    M1 实测：turbo 版是 mobiuslabsgmbh/faster-whisper-large-v3-turbo，
    不是 Systran/faster-whisper-large-v3-turbo。硬编码会得到 401，
    而且报错信息看起来像「镜像需要鉴权」，极易误判。
    """
    try:
        from faster_whisper.utils import _MODELS

        repo = _MODELS.get(fw_name)
        if repo:
            return repo
    except Exception:  # noqa: BLE001
        pass
    return f"Systran/faster-whisper-{fw_name}"


def funasr_repo(model: dict) -> str:
    return model.get("source", "").split("·")[-1].strip() or "iic/SenseVoiceSmall"
