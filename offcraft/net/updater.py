"""客户端自更新：直连延迟探测、清理 DNS 后重测、镜像兜底与断点续传。

流程（``plan_download``）：

1. 先测 GitHub Releases 直连的首字节耗时（只读 2 KiB）；
2. 超过阈值（默认 1.2 s）时清理本机 DNS 缓存并重测；
3. 仍然偏高或不可达时改用镜像（``/github/...`` 代理，支持 Range 断点续传）。

下载统一交给 :class:`offcraft.net.downloader.Downloader`，失败重试会保留
``.part`` 文件，因此断网、切源后都能从断点继续。
"""

from __future__ import annotations

import platform
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from offcraft import DEFAULT_USER_AGENT
from offcraft.net.downloader import DownloadError, Downloader, DownloadTask, ProgressTracker

__all__ = [
    "DEFAULT_THRESHOLD_MS",
    "GITHUB_LATEST_API",
    "ReleaseInfo",
    "UpdatePlan",
    "download_installer",
    "fetch_release",
    "flush_dns",
    "is_newer",
    "plan_download",
    "probe_first_byte",
    "scan_installer_name",
]

#: 直连首字节超过该值就视为“延迟过高”（毫秒）
DEFAULT_THRESHOLD_MS = 1200
#: 探测超时（秒）
DEFAULT_PROBE_TIMEOUT = 6.0
#: 无镜像地址时用于查询最新版本的 GitHub API
GITHUB_LATEST_API = "https://api.github.com/repos/leaf-CatLin/CrMcDownload/releases/latest"


@dataclass(slots=True)
class ReleaseInfo:
    """一次版本查询的结果。"""

    version: str
    min_version: str = ""
    force: bool = False
    release_url: str = ""
    download_url: str = ""
    mirror_url: str = ""
    asset_name: str = ""
    notes: str = ""

    @property
    def outdated(self) -> bool:
        return self.force


@dataclass(slots=True)
class UpdatePlan:
    """选源结论。"""

    source: str  # "direct" / "mirror"
    url: str
    direct_ms: float | None = None
    retest_ms: float | None = None
    dns_flushed: bool = False
    reason: str = ""

    @property
    def label(self) -> str:
        return "镜像" if self.source == "mirror" else "GitHub 直连"

    def detail(self) -> str:
        parts = [f"直连 {_fmt(self.direct_ms)}"]
        if self.retest_ms is not None:
            parts.append(f"清理 DNS 后 {_fmt(self.retest_ms)}")
        if self.dns_flushed:
            parts.append("已清理 DNS 缓存")
        parts.append(f"→ {self.label}")
        if self.reason:
            parts.append(f"（{self.reason}）")
        return " / ".join(parts)


def _fmt(value: float | None) -> str:
    return "不可达" if value is None else f"{value * 1000:.0f}ms"


def _absolute(base: str, url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    return (base or "").rstrip("/") + "/" + text.lstrip("/")


def _downloader(*, proxy=None, token: str = "", origin: str = "", timeout: float, retries: int = 1) -> Downloader:
    return Downloader(
        proxy=proxy,
        token=token or None,
        token_origin=origin or None,
        workers=1,
        timeout=timeout,
        retries=retries,
        user_agent=DEFAULT_USER_AGENT,
    )


def fetch_release(
    base_url: str = "",
    *,
    token: str = "",
    proxy: str | None = None,
    timeout: float = 8.0,
) -> ReleaseInfo:
    """查询最新版本；``base_url`` 为空时直接问 GitHub API。"""
    base = (base_url or "").strip().rstrip("/")
    dl = _downloader(proxy=proxy, token=token, origin=base, timeout=timeout, retries=1)
    if base:
        payload = dl.request_json(f"{base}/api/update", headers={"Accept": "application/json"})
    else:
        payload = dl.fetch_json(GITHUB_LATEST_API)
    if not isinstance(payload, dict):
        raise DownloadError("更新接口返回了非 JSON 对象")
    return read_release(payload, base=base)


def read_release(payload: dict, *, base: str = "") -> ReleaseInfo:
    """把镜像 ``/api/update`` 或 GitHub Releases API 的响应归一化。"""
    if "latest_version" in payload:
        notes = str(payload.get("notes") or "")
        return ReleaseInfo(
            version=str(payload.get("latest_version") or "").lstrip("vV"),
            min_version=str(payload.get("min_version") or "").lstrip("vV"),
            force=bool(payload.get("force_update")),
            release_url=str(payload.get("release_url") or ""),
            download_url=str(payload.get("download_url") or ""),
            mirror_url=_absolute(base, str(payload.get("mirror_download_url") or "")),
            asset_name=str((payload.get("asset") or {}).get("name") or ""),
            notes=notes,
        )
    tag = str(payload.get("tag_name") or payload.get("name") or "").lstrip("vV")
    assets = payload.get("assets") or []
    asset = next((item for item in assets if item.get("browser_download_url")), None)
    return ReleaseInfo(
        version=tag,
        release_url=str(payload.get("html_url") or ""),
        download_url=str((asset or {}).get("browser_download_url") or ""),
        asset_name=str((asset or {}).get("name") or ""),
        notes=str(payload.get("body") or ""),
    )


def probe_first_byte(url: str, *, timeout: float = DEFAULT_PROBE_TIMEOUT, proxy=None) -> float | None:
    """测一次首字节耗时（秒）；不可达返回 ``None``，不抛出。"""
    if not url:
        return None
    prober = _downloader(proxy=proxy, timeout=timeout, retries=0)
    try:
        return prober.probe(url, first_chunk=2048, timeout=timeout)
    except (DownloadError, OSError):
        return None
    finally:
        prober.close()


#: 提权成功后，间隔多久才允许再次弹出 UAC（避免频繁打扰）
_ELEVATION_RETRY_SEC = 30 * 60
_elevation_lock = threading.Lock()
#: "" = 尚未请求；"approved" = 用户同意过；"denied" = 用户拒绝（本次进程不再询问）
_elevation_state = ""
_elevation_at = 0.0
_elevation_busy = False


def _is_admin() -> bool:
    """当前进程是否已具备管理员权限（仅 Windows 有意义）。"""
    if platform.system() != "Windows":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - 取不到就当普通权限
        return False


def _request_elevation() -> bool:
    """通过 UAC 发起 ``ipconfig /flushdns``；成功启动返回 True。"""
    try:
        import ctypes

        # ShellExecuteW 返回值 > 32 表示已成功启动；<= 32 通常是被 UAC 拒绝
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "ipconfig", "/flushdns", None, 0
        )
        return int(result) > 32
    except Exception:  # noqa: BLE001 - 提权失败不应影响探测
        return False


def _flush_windows_elevated(*, timeout: float = 8.0) -> bool:
    """普通权限下清理 DNS 失败时，通过 UAC 请求管理员权限执行 ``ipconfig /flushdns``。

    返回 True 表示用户已同意并成功发起提权（不代表命令一定执行完成）。
    同一个进程里：

    * 用户同意后 30 分钟内不再重复弹窗；到期后会再请求一次；
    * 用户拒绝后不再询问，避免反复打扰；
    * 请求在后台线程里等待 UAC，超时后按「本轮未清理」继续探测，不会卡住下载。
    """
    global _elevation_state, _elevation_at, _elevation_busy
    if platform.system() != "Windows" or _is_admin():
        return False
    now = time.monotonic()
    with _elevation_lock:
        if _elevation_state == "denied":
            return False
        if _elevation_state == "approved" and now - _elevation_at < _ELEVATION_RETRY_SEC:
            return False
        if _elevation_busy:
            return False
        _elevation_busy = True

    outcome: dict[str, bool] = {}

    def request() -> None:
        global _elevation_state, _elevation_at, _elevation_busy
        launched = _request_elevation()
        with _elevation_lock:
            _elevation_busy = False
            _elevation_at = time.monotonic()
            _elevation_state = "approved" if launched else "denied"
        outcome["launched"] = launched

    thread = threading.Thread(target=request, name="dns-elevate", daemon=True)
    thread.start()
    thread.join(timeout)
    launched = bool(outcome.get("launched"))
    if launched:
        # 提权进程是异步的，留一小段时间让 flushdns 生效后再探测
        time.sleep(0.3)
    return launched


def flush_dns() -> bool:
    """清理本机 DNS 缓存；普通权限失败时会请求一次管理员权限（UAC）。"""
    system = platform.system()
    if system == "Windows":
        commands = [["ipconfig", "/flushdns"]]
    elif system == "Darwin":
        commands = [["dscacheutil", "-flushcache"], ["killall", "-HUP", "mDNSResponder"]]
    else:
        commands = [["resolvectl", "flush-caches"], ["systemd-resolve", "--flush-caches"]]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if system == "Windows" else 0
    for command in commands:
        try:
            result = subprocess.run(
                command, capture_output=True, timeout=5, check=False, creationflags=flags
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return True
    if system == "Windows":
        # 普通权限通常返回「需要提升权限」；改为弹出 UAC 请求管理员权限
        return _flush_windows_elevated()
    return False


def plan_download(
    release: ReleaseInfo,
    *,
    threshold_ms: int = DEFAULT_THRESHOLD_MS,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    proxy=None,
    probe: Callable[..., float | None] = probe_first_byte,
    flush: Callable[[], bool] = flush_dns,
    sleep: Callable[[float], None] = time.sleep,
) -> UpdatePlan:
    """按直连延迟决定走 GitHub 还是镜像。"""
    threshold = max(0.0, float(threshold_ms)) / 1000.0
    direct_ms = probe(release.download_url, timeout=timeout, proxy=proxy)
    has_mirror = bool(release.mirror_url)
    if direct_ms is not None and direct_ms < threshold:
        return UpdatePlan("direct", release.download_url, direct_ms=direct_ms)

    flushed = bool(flush())
    retest_ms = None
    if flushed:
        sleep(0.5)
        retest_ms = probe(release.download_url, timeout=timeout, proxy=proxy)
        if retest_ms is not None and retest_ms < threshold:
            return UpdatePlan(
                "direct",
                release.download_url,
                direct_ms=direct_ms,
                retest_ms=retest_ms,
                dns_flushed=True,
                reason="清理 DNS 缓存后恢复",
            )
    if has_mirror:
        reason = "直连不可达" if direct_ms is None else "直连延迟过高"
        return UpdatePlan(
            "mirror",
            release.mirror_url,
            direct_ms=direct_ms,
            retest_ms=retest_ms,
            dns_flushed=flushed,
            reason=reason,
        )
    return UpdatePlan(
        "direct",
        release.download_url,
        direct_ms=direct_ms,
        retest_ms=retest_ms,
        dns_flushed=flushed,
        reason="无可用镜像，继续直连",
    )


def version_key(text: str) -> tuple[tuple[int, ...], tuple[int, tuple[int, ...]]] | None:
    """版本号 → 可比较键；预发布排在正式版之前，非法输入返回 ``None``。"""
    raw = str(text or "").strip().lstrip("vV")
    if not raw:
        return None
    raw = raw.partition("+")[0][:32]
    base, _, pre = raw.partition("-")
    parts = base.split(".")
    if not parts or not all(part.isdigit() for part in parts if part) or len(parts) > 4:
        return None
    numbers = [int(part) for part in parts if part]
    while len(numbers) < 3:
        numbers.append(0)
    if pre:
        return tuple(numbers), (0, tuple(int(x) for x in pre.split(".")[1:] if x.isdigit()))
    return tuple(numbers), (3, ())


def is_newer(candidate: str, current: str) -> bool:
    """``candidate`` 是否比 ``current`` 新。"""
    left, right = version_key(candidate), version_key(current)
    if left is None or right is None:
        return False
    return left > right


def scan_installer_name(url: str) -> str:
    """从更新地址里取安装包文件名。"""
    return Path(urlsplit(str(url or "")).path).name


def download_installer(
    plan: UpdatePlan,
    dest_dir: Path | str,
    *,
    proxy: str | None = None,
    token: str = "",
    mirror_origin: str = "",
    progress: Callable[[ProgressTracker], None] | None = None,
    cancel=None,
    timeout: float = 30.0,
) -> Path:
    """下载安装包到 ``dest_dir``，支持断点续传。"""
    name = scan_installer_name(plan.url)
    if not name:
        raise DownloadError("更新地址缺少文件名")
    dest = Path(dest_dir) / name
    dl = Downloader(
        proxy=proxy,
        token=token or None,
        token_origin=mirror_origin or None,
        workers=1,
        timeout=timeout,
        retries=3,
        user_agent=DEFAULT_USER_AGENT,
        progress_callback=progress,
    )
    try:
        dl.download(
            DownloadTask(url=plan.url, dest=dest, label=name),
            tracker=ProgressTracker(),
            cancel=cancel,
            keep_part=True,
        )
    finally:
        dl.close()
    return dest
