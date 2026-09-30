"""声文 · 媒体处理：探测与抽音轨（方案 §4 管线第 1–4 步）"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm", ".wmv", ".m4v", ".ts", ".mpg", ".mpeg", ".3gp"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".amr", ".silk"}
SUPPORTED_EXT = VIDEO_EXT | AUDIO_EXT


class MediaError(RuntimeError):
    pass


@dataclass
class MediaInfo:
    path: Path
    kind: str            # video | audio
    duration: float      # 秒
    size_bytes: int
    has_audio: bool
    audio_codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    width: int = 0
    height: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.path.name,
            "kind": self.kind,
            "duration": round(self.duration, 2),
            "duration_display": fmt_duration(self.duration),
            "size_bytes": self.size_bytes,
            "size_display": fmt_size(self.size_bytes),
            "has_audio": self.has_audio,
            "audio_codec": self.audio_codec,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "resolution": f"{self.width}×{self.height}" if self.width else "",
        }


def fmt_duration(sec: float) -> str:
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_size(n: int) -> str:
    v = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if v < 1024:
            return f"{v:.1f} {u}"
        v /= 1024
    return f"{v:.1f} TB"


def probe(path: Path) -> MediaInfo:
    """读容器与音频流信息。"""
    if not path.exists():
        raise MediaError(f"文件不存在：{path}")
    if path.suffix.lower() not in SUPPORTED_EXT:
        raise MediaError(f"不支持的格式：{path.suffix}")

    raw = subprocess.run(
        [FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60,
    ).stdout
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise MediaError(f"无法解析媒体信息：{exc}") from exc

    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = 0.0
    try:
        duration = float(data.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        pass
    if duration <= 0 and a:
        try:
            duration = float(a.get("duration") or 0)
        except (TypeError, ValueError):
            pass

    info = MediaInfo(
        path=path,
        kind="video" if v else "audio",
        duration=duration,
        size_bytes=path.stat().st_size,
        has_audio=a is not None,
        audio_codec=(a or {}).get("codec_name", ""),
        sample_rate=int((a or {}).get("sample_rate") or 0),
        channels=int((a or {}).get("channels") or 0),
        width=int((v or {}).get("width") or 0),
        height=int((v or {}).get("height") or 0),
    )
    if not info.has_audio:
        raise MediaError("这个文件没有音轨")
    return info


def extract_audio(src: Path, dst: Path, *, normalize: bool = False) -> Path:
    """抽音轨为 16kHz 单声道 PCM（ASR 标准输入）。

    M1 实测：立体声 44.1kHz 素材走这条路正常；2 小时音频约 2.6 秒完成。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    filters = []
    if normalize:
        # EBU R128 响度归一化 —— 会议录音忽大忽小时有帮助
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")

    cmd = [FFMPEG, "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000"]
    if filters:
        cmd += ["-af", ",".join(filters)]
    cmd += ["-c:a", "pcm_s16le", str(dst)]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0 or not dst.exists():
        raise MediaError(f"抽音轨失败：{(r.stderr or '')[:300]}")
    return dst


def slice_audio(src: Path, dst: Path, start: float, duration: float) -> Path:
    """切一段音频（用于语言路由的前段采样与对比测试）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-ss", str(start), "-t", str(duration), "-i", str(src),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)],
        check=True, timeout=600,
    )
    return dst
