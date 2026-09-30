"""声文 · 模型存储：个人下载、删除与安装状态

设计要点：
  · 下载走 M1 验证过的路径 —— HF 镜像 + **禁用 Xet** + **local_dir 模式**
  · sherpa 类模型走 GitHub Releases（经代理镜像），见 _do_download_sherpa
      - 禁用 Xet：Xet 后端在 Windows（未开开发者模式）会留下 0 字节占位文件，
        表现为「下载成功但模型读不出来」，M1 实测踩过
      - local_dir：落成裸模型文件，正好符合打包需要（不是 HF 缓存结构）
  · 进度用**目录体积轮询**实现 —— 比接第三方进度回调稳，且第三方库换版本也不会崩
  · 下载串行化（一把锁），避免多个大文件同时抢带宽
  · 删除前检查该引擎是否正在被任务使用
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
MODELS_DIR = ROOT / "models"

from app.doctor import registry  # noqa: E402

# ★ 必须在 huggingface_hub 被导入前设置
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")          # 见上：防 0 字节占位文件

WHISPER_PATTERNS = [
    "config.json", "model.bin", "tokenizer.json",
    "preprocessor_config.json", "vocabulary.json",
]
# model.bin 至少要这么大才算完整（防止半截文件被当成已安装）
MIN_BIN_BYTES = 30 * 1024 * 1024


class ModelBusy(RuntimeError):
    pass


class ModelInUse(RuntimeError):
    pass


@dataclass
class Download:
    model_id: str
    status: str = "running"          # running | done | failed | canceled
    pct: float = 0.0
    downloaded_mb: float = 0.0
    total_mb: float = 0.0
    speed_mbps: float = 0.0
    eta_seconds: float = 0.0
    message: str = "准备中…"
    source: str = "mirror"           # mirror | official —— 用户选的下载源
    used_source: str = ""            # 实际成功的源（可能因回退而与 source 不同）
    from_fallback: bool = False      # 是否发生过回退
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ═══════════════════════════════════════════════════════════
# 安装状态探测
# ═══════════════════════════════════════════════════════════


def _dir_bytes(p: Path) -> int:
    if not p.exists():
        return 0
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total


MIRROR_LABEL = "镜像站"
OFFICIAL_LABEL = "海外官方源"


def sherpa_url_order(m: dict, source: str) -> list[str]:
    """按用户选的源优先排序 URL 列表；**另一组作为回退保留在后面**。

    ★ 为什么保留回退：让用户手选源解决的是"我想走哪条路"，
      但选的那条挂了还让他手动再试一次，就是把可用性问题转嫁给用户。
      所以顺序是「首选 → 备用」，而不是「只试首选」。
    """
    mir = [u for u in (m.get("mirror_urls") or []) if u]
    off = [m["official_url"]] if m.get("official_url") else []
    primary, backup = (off, mir) if source == "official" else (mir, off)
    return primary + backup


def hf_endpoints(source: str) -> list[str]:
    """HuggingFace 的下载源顺序（镜像 → 官方，或反之）。"""
    mir = "https://hf-mirror.com"
    off = "https://huggingface.co"
    return [off, mir] if source == "official" else [mir, off]


def install_state(model: dict) -> dict:
    """返回 {state, installed_mb, path}。state: not_installed | partial | installed。"""
    if model.get("planned"):
        return {"state": "planned", "installed_mb": 0.0, "path": ""}

    if model["kind"] == "whisper":
        d = ROOT / model["dir"]
        if not d.exists():
            return {"state": "not_installed", "installed_mb": 0.0, "path": str(d)}
        binp = d / "model.bin"
        cfg = d / "config.json"
        size = _dir_bytes(d)
        ok = binp.exists() and cfg.exists()
        if ok:
            try:
                ok = binp.stat().st_size >= MIN_BIN_BYTES
            except OSError:
                ok = False
        return {
            "state": "installed" if ok else "partial",
            "installed_mb": round(size / 1024**2, 1),
            "path": str(d),
        }

    # sherpa-onnx（单目录、裸模型文件、可能带 int8 量化）
    if model["kind"] == "sherpa":
        d = ROOT / model["dir"]
        if not d.exists():
            return {"state": "not_installed", "installed_mb": 0.0, "path": str(d)}
        size = _dir_bytes(d)
        # ★ 优先按注册表里显式声明的文件清单判断。不同模型的命名毫无规律
        #   （SenseVoice: model.int8.onnx / Parakeet: encoder+decoder+joiner /
        #     Moonshine: preprocess+encode+cached_decode+uncached_decode），
        #   用统一 glob 猜一定会误判。
        required = model.get("required") or []
        if required:
            missing = [n for n in required if not (d / n).exists()]
            if missing:
                return {"state": "partial", "installed_mb": round(size / 1024**2, 1),
                        "path": str(d)}
        else:
            has_model = next(d.glob("*.onnx"), None) is not None
            if not (has_model and (d / "tokens.txt").exists()):
                return {"state": "partial", "installed_mb": round(size / 1024**2, 1),
                        "path": str(d)}
        return {
            "state": "installed" if size > MIN_BIN_BYTES else "partial",
            "installed_mb": round(size / 1024**2, 1),
            "path": str(d),
        }

    # 目前只支持 whisper / sherpa 两种形态。
    # ★ 2026-09-30：原先还有一条 funasr(ModelScope) 分支，随 SenseVoice 迁移到
    #   sherpa-onnx 后已无任何模型使用，整条分支连同 _funasr_model_dirs 一起删除
    #   —— 留着死代码会让将来的依赖审计和打包判断出错。
    raise ValueError(f"未知的模型形态：{model.get('kind')!r}（期望 whisper 或 sherpa）")


# ═══════════════════════════════════════════════════════════
# 下载器
# ═══════════════════════════════════════════════════════════


class ModelStore:
    def __init__(self) -> None:
        self.downloads: dict[str, Download] = {}
        self._lock = threading.Lock()
        self._dl_lock = threading.Lock()      # 串行化实际下载
        self._cancel: set[str] = set()
        self._usage_checker = None

    def set_usage_checker(self, fn) -> None:
        """注入「该引擎是否正被任务占用」的检查函数，避免循环导入。"""
        self._usage_checker = fn

    # ---------- 查询 ----------
    def list_models(self) -> list[dict]:
        out: list[dict] = []
        for m in registry.all_models():
            st = install_state(m)
            d = self.downloads.get(m["id"])
            item = dict(m)
            item.update(st)
            item["vram_display"] = self._vram_display(m)
            item["download"] = d.to_dict() if d else None
            out.append(item)
        out.sort(key=lambda x: (x.get("planned", False), x.get("priority", 99)))
        return out

    @staticmethod
    def _vram_display(m: dict) -> str:
        vs = [v.get("vram_mb", 0) for v in m.get("variants", [])]
        if not vs:
            return "—"
        lo, hi = min(vs), max(vs)
        if hi == 0:
            return "无需显卡"
        if lo == 0:
            return f"0–{hi/1024:.1f}GB"
        return f"{lo/1024:.1f}–{hi/1024:.1f}GB"

    def status(self) -> list[dict]:
        with self._lock:
            return [d.to_dict() for d in self.downloads.values()]

    # ---------- 下载 ----------
    def start_download(self, model_id: str, source: str = "mirror") -> Download:
        m = registry.get(model_id)
        if not m:
            raise ValueError(f"未知模型：{model_id}")
        if m.get("planned"):
            raise ModelBusy("该模型尚未接入，将在后续版本提供")
        src = "official" if str(source).lower() in ("official", "overseas", "hf") else "mirror"
        with self._lock:
            cur = self.downloads.get(model_id)
            if cur and cur.status == "running":
                return cur
            d = Download(model_id=model_id,
                         total_mb=float(m.get("download_mb") or m.get("size_mb", 0)),
                         source=src)
            self.downloads[model_id] = d
            self._cancel.discard(model_id)
        threading.Thread(target=self._run, args=(m, src), daemon=True).start()
        return d

    def cancel_download(self, model_id: str) -> bool:
        with self._lock:
            d = self.downloads.get(model_id)
            if not d or d.status != "running":
                return False
            self._cancel.add(model_id)
            # ★ 立刻改文案给用户反馈。真正的停止发生在下载线程的下一个检查点，
            #   而它可能正卡在 urlopen 的连接阶段（最长 30 秒）——
            #   不给反馈的话用户会以为按钮没生效，然后反复点。
            d.message = "正在停止…"
        return True

    def _run(self, m: dict, source: str = "mirror") -> None:
        mid = m["id"]
        with self._dl_lock:
            d = self.downloads[mid]
            if mid in self._cancel:
                d.status, d.message = "canceled", "已取消"
                d.finished_at = time.time()
                return
            try:
                d.message = "正在连接下载源…"
                if m["kind"] == "whisper":
                    target = ROOT / m["dir"]
                elif m["kind"] == "sherpa":
                    # 监听父目录：下载的 .tar.bz2 与解压后的目录都在这里，进度能连续反映
                    target = MODELS_DIR / "sherpa"
                else:
                    raise ValueError(f"未知的模型形态：{m.get('kind')!r}")
                target.mkdir(parents=True, exist_ok=True)
                base = _dir_bytes(target)

                stop = threading.Event()
                t = threading.Thread(target=self._watch, args=(mid, m, target, base, stop),
                                     daemon=True)
                t.start()
                try:
                    self._do_download(m, source)
                finally:
                    stop.set()

                if mid in self._cancel:
                    d.status, d.pct, d.message = "canceled", d.pct, "已取消"
                else:
                    d.status, d.pct, d.message = "done", 100.0, "安装完成"
                    d.downloaded_mb = float(m.get("size_mb", 0))
            except Exception as exc:  # noqa: BLE001
                # ★ 用户点了停止 → 这是取消，不是失败。之前一律记成 failed，
                #   界面上会显示成"下载失败"并给出网络错误提示，误导用户。
                if mid in self._cancel:
                    d.status, d.message = "canceled", "已取消"
                else:
                    d.status = "failed"
                    d.message = _humanize(exc)
            finally:
                d.finished_at = time.time()

    def _do_download(self, m: dict, source: str = "mirror") -> None:
        if m["kind"] == "sherpa":
            self._do_download_sherpa(m, source)
            return
        if m["kind"] == "whisper":
            from huggingface_hub import snapshot_download

            repo = registry.whisper_repo(m["fw_name"])
            last_err: Exception | None = None
            for idx, ep in enumerate(hf_endpoints(source), 1):
                d = self.downloads.get(m["id"])
                label = OFFICIAL_LABEL if "huggingface.co" in ep else MIRROR_LABEL
                if d:
                    if idx > 1:
                        d.from_fallback = True
                        d.message = "首选源失败，已自动切换"
                    else:
                        d.message = f"{label} · 连接中…"
                try:
                    snapshot_download(
                        repo_id=repo,
                        local_dir=str(ROOT / m["dir"]),
                        allow_patterns=WHISPER_PATTERNS,
                        max_workers=8,
                        endpoint=ep,
                    )
                    if d:
                        d.used_source = "official" if "huggingface.co" in ep else "mirror"
                    last_err = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    continue
            if last_err is not None:
                raise RuntimeError(f"所有下载源都失败（最后错误：{last_err}）")
        else:
            raise ValueError(f"未知的模型形态：{m.get('kind')!r}")

    def _do_download_sherpa(self, m: dict, source: str = "mirror") -> None:
        """下载单个 tar.bz2 并解压。支持多源回退与取消。

        ★ 为什么自己写而不用现有库：
          这批模型托管在 GitHub Releases 上，而**国内直连 github.com 是不通的**
          （实测 HTTP 000）。所以源清单里优先放代理镜像（gh-proxy / ghproxy），
          直连只作为境外环境的兜底 —— 与「多源竞速 + 失败自动换源」的设计一致。
        """
        import tarfile
        import urllib.request

        mid = m["id"]
        parent = MODELS_DIR / "sherpa"
        parent.mkdir(parents=True, exist_ok=True)
        dest = parent / f"{m['tar_dir']}.tar.bz2"

        urls = sherpa_url_order(m, source)
        if not urls:
            raise RuntimeError("模型没有配置下载源")

        last_err: Exception | None = None
        for idx, u in enumerate(urls, 1):
            if mid in self._cancel:
                raise RuntimeError("已取消")
            # 注意：urlopen 的连接阶段最长会阻塞 timeout 秒，期间无法响应停止 ——
            # 这是"停止"不即时的主要原因。把 timeout 压到 30 秒是折中：
            # 足够慢源建连，又不会让用户等太久看不到反应。
            host = u.split("/")[2] if "//" in u else u
            is_official = u.startswith("https://github.com/")
            d = self.downloads.get(mid)
            if d:
                tag = OFFICIAL_LABEL if is_official else MIRROR_LABEL
                # 首选源失败后走到备用源时，明确告诉用户"已自动切换"
                if idx > 1 and (is_official != (source == "official")):
                    d.from_fallback = True
                    d.message = f"{MIRROR_LABEL if not is_official else OFFICIAL_LABEL}失败，已自动切换"
                else:
                    d.message = f"{tag} · {host}"
            try:
                req = urllib.request.Request(u, headers={"User-Agent": "SoundScribe/1.0"})
                with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
                    while True:
                        if mid in self._cancel:
                            raise RuntimeError("已取消")
                        buf = r.read(1 << 20)      # 1 MB
                        if not buf:
                            break
                        f.write(buf)
                if dest.stat().st_size < 1024 * 1024:
                    raise RuntimeError(f"下载内容异常（只有 {dest.stat().st_size} 字节）")
                last_err = None
                if d:
                    d.used_source = "official" if is_official else "mirror"
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
                if mid in self._cancel:
                    raise
                continue

        if last_err is not None:
            raise RuntimeError(f"所有下载源都失败（最后错误：{last_err}）")

        d = self.downloads.get(mid)
        if d:
            d.message = "下载完成，正在解压…"
        try:
            with tarfile.open(dest, "r:bz2") as tf:
                # filter="data" 阻止路径穿越；3.12+ 支持，旧版本回退
                try:
                    tf.extractall(parent, filter="data")
                except TypeError:
                    tf.extractall(parent)
        finally:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass

        if not (ROOT / m["dir"]).exists():
            raise RuntimeError(f"解压后找不到预期目录：{m['dir']}")

    def _watch(self, mid: str, m: dict, target: Path, base: int, stop: threading.Event) -> None:
        """轮询目录体积估算进度。

        ★ 为什么不用第三方进度回调：huggingface_hub / modelscope 的进度接口
          跨版本变化频繁，且回调里抛异常会直接中断下载。轮询体积只依赖文件系统，
          稳定且不会把下载搞崩。
        """
        d = self.downloads[mid]
        # ★ sherpa 类模型要区分「下载量」和「安装后体积」：
        #   压缩包 156 MB，解压后 230 MB。用安装后体积算进度会永远到不了 100%。
        total_mb = float(m.get("download_mb") or m.get("size_mb", 0)) or 1.0
        t0 = time.time()
        while not stop.wait(0.7):
            cur = _dir_bytes(target) - base
            mb = max(0.0, cur / 1024**2)
            elapsed = max(0.1, time.time() - t0)
            d.downloaded_mb = round(mb, 1)
            d.speed_mbps = round(mb / elapsed, 1)
            d.pct = round(min(98.0, mb / total_mb * 100), 1)
            if d.speed_mbps > 0.05:
                d.eta_seconds = round(max(0.0, (total_mb - mb) / d.speed_mbps), 0)
            d.message = f"已下载 {mb:.0f} / {total_mb:.0f} MB"
            if mid in self._cancel:
                break
        d.pct = min(99.0, d.pct)

    # ---------- 删除 ----------
    def delete(self, model_id: str) -> dict:
        m = registry.get(model_id)
        if not m:
            raise ValueError(f"未知模型：{model_id}")
        if m.get("planned"):
            raise ModelBusy("该模型尚未接入")
        with self._lock:
            d = self.downloads.get(model_id)
            if d and d.status == "running":
                raise ModelBusy("该模型正在下载中，请先取消下载再删除")
        if self._usage_checker and self._usage_checker(m["engine"]):
            raise ModelInUse(
                f"{m['name']} 正被正在运行的任务使用，请等任务结束后再删除"
            )

        st = install_state(m)
        if st["state"] == "not_installed":
            return {"deleted": False, "freed_mb": 0.0, "message": "该模型本来就没有安装"}

        freed = 0.0
        if m["kind"] == "whisper":
            p = ROOT / m["dir"]
            freed = _dir_bytes(p) / 1024**2
            shutil.rmtree(p, ignore_errors=True)
        elif m["kind"] == "sherpa":
            p = ROOT / m["dir"]
            freed = _dir_bytes(p) / 1024**2
            shutil.rmtree(p, ignore_errors=True)
        else:
            raise ValueError(f"未知的模型形态：{m.get('kind')!r}")

        with self._lock:
            self.downloads.pop(model_id, None)

        return {
            "deleted": True,
            "freed_mb": round(freed, 1),
            "message": f"已删除 {m['name']}，释放 {freed/1024:.2f} GB",
        }


def _humanize(exc: Exception) -> str:
    msg = str(exc)
    low = msg.lower()
    if "401" in msg or "unauthorized" in low:
        return "下载源拒绝访问。可能是仓库地址失效 —— 请点「重新检测」后再试。"
    if "connection" in low or "timeout" in low or "timed out" in low:
        return "网络连接失败。已尝试国内镜像，请检查网络后重试。"
    if "disk" in low or "no space" in low:
        return "磁盘空间不足，请清理后重试。"
    if "404" in msg or "not found" in low:
        return "下载源上没有这个文件（仓库地址可能已变更）。"
    return f"{type(exc).__name__}: {msg[:220]}"


store = ModelStore()
