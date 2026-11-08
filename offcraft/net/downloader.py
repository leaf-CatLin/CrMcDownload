"""并发下载器：断点续传、SHA-1 校验、重试、取消与进度聚合。"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import ssl
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from offcraft import DEFAULT_USER_AGENT, __version__
from offcraft.net.proxy import ProxyConfig, open_tunnel, parse_proxy

__all__ = [
    "Cancelled",
    "DownloadError",
    "DownloadReport",
    "DownloadTask",
    "Downloader",
    "ProgressTracker",
    "SourceStalled",
    "origin_of",
    "parse_origin",
    "sha1_file",
    "verify_file",
]

_CHUNK = 256 * 1024

#: 单文件下载速率低于该值（字节/秒）即视为「过慢」
SLOW_THRESHOLD = 256 * 1024
#: 慢速判定的观察窗口（秒）
SLOW_WINDOW = 6.0
#: 开始慢速检测前的预热（秒）：避开建连与系统缓冲区填充
SLOW_WARMUP = 4.0
#: 剩余字节少于此值时不判慢（小文件很快下完，不值得切源）
SLOW_MIN_BYTES = 8 * 1024 * 1024
#: 单个文件最多因过慢切源次数
STALL_SWITCHES = 2


def origin_of(url: str) -> tuple[str, str, int] | None:
    """URL 的 ``(scheme, host, port)``；非 http(s) 或主机非法时返回 None。"""
    try:
        parts = urlsplit(str(url or ""))
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        if scheme not in ("http", "https") or not host:
            return None
        port = parts.port
    except ValueError:
        return None
    return scheme, host, port or (443 if scheme == "https" else 80)


def parse_origin(value: str | None) -> tuple[str, str, int] | None:
    """把 ``https://mc.example.com`` / ``mc.example.com`` 归一化成 origin。"""
    text = str(value or "").strip()
    if not text:
        return None
    if "//" not in text:
        text = "https://" + text
    return origin_of(text)


class DownloadError(RuntimeError):
    """下载或校验失败。"""


class Cancelled(RuntimeError):
    """用户取消。"""


class SourceStalled(DownloadError):
    """下载速率持续过低：保留 ``.part``，交给调用方换源后续传。"""


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK * 4), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path: Path, sha1: str | None, size: int | None) -> bool:
    """文件存在、大小与 SHA-1 均匹配时返回 True。"""
    try:
        stat = path.stat()
    except OSError:
        return False
    if not path.is_file():
        return False
    if size is not None and stat.st_size != size:
        return False
    if sha1 and sha1_file(path).lower() != sha1.lower():
        return False
    return True


@dataclass(frozen=True, slots=True)
class DownloadTask:
    url: str
    dest: Path
    sha1: str | None = None
    size: int | None = None
    label: str = ""

    @property
    def name(self) -> str:
        return self.label or self.dest.name


@dataclass(slots=True)
class ProgressTracker:
    """线程安全的进度聚合。"""

    total_bytes: int = 0
    done_bytes: int = 0
    files_total: int = 0
    files_done: int = 0
    current: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _samples: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=16), repr=False)

    def start(self, files_total: int, total_bytes: int) -> None:
        with self._lock:
            self.files_total = files_total
            self.total_bytes = max(total_bytes, 0)
            self.done_bytes = 0
            self.files_done = 0
            self._samples.clear()
            self._samples.append((time.monotonic(), 0))

    def add_bytes(self, count: int) -> None:
        with self._lock:
            self.done_bytes += count
            now = time.monotonic()
            self._samples.append((now, self.done_bytes))
            while len(self._samples) > 2 and now - self._samples[0][0] > 3.0:
                self._samples.popleft()

    def file_started(self, name: str) -> None:
        with self._lock:
            self.current = name

    def file_finished(self) -> None:
        with self._lock:
            self.files_done += 1

    @property
    def speed(self) -> float:
        with self._lock:
            if len(self._samples) < 2:
                return 0.0
            (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
            if t1 <= t0:
                return 0.0
            return (b1 - b0) / (t1 - t0)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            done, total = self.done_bytes, self.total_bytes
            files_done, files_total = self.files_done, self.files_total
            current = self.current
        speed = self.speed
        remaining = max(total - done, 0) if total else None
        eta = remaining / speed if remaining is not None and speed > 0 else None
        return {
            "done_bytes": done,
            "total_bytes": total,
            "files_done": files_done,
            "files_total": files_total,
            "current": current,
            "speed": speed,
            "eta": eta,
            "percent": (done / total * 100.0) if total else None,
        }


@dataclass(slots=True)
class DownloadReport:
    succeeded: list[DownloadTask] = field(default_factory=list)
    skipped: list[DownloadTask] = field(default_factory=list)
    failed: list[tuple[DownloadTask, str]] = field(default_factory=list)
    bytes_written: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed


class Downloader:
    """基于标准库的并发下载器。

    ``progress`` 每完成一块数据会被调用（默认每 256 KiB）。
    """

    def __init__(
        self,
        *,
        proxy: str | ProxyConfig | None = None,
        token: str | None = None,
        token_origin: str | None = None,
        workers: int = 8,
        timeout: float = 30.0,
        retries: int = 3,
        user_agent: str = DEFAULT_USER_AGENT,
        progress_callback: Callable[[ProgressTracker], None] | None = None,
        progress_interval: float = 0.2,
        slow_threshold: float = SLOW_THRESHOLD,
        slow_window: float = SLOW_WINDOW,
        slow_warmup: float = SLOW_WARMUP,
        slow_min_bytes: int = SLOW_MIN_BYTES,
        stall_switches: int = STALL_SWITCHES,
        url_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        self.proxy = parse_proxy(proxy) if isinstance(proxy, str) else proxy
        self.token = token or None
        #: 只有目标 origin 与它完全一致时才向请求头里加镜像令牌
        self.token_origin = parse_origin(token_origin) if self.token else None
        self.workers = max(1, int(workers))
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.user_agent = user_agent
        self.progress_callback = progress_callback
        self.progress_interval = progress_interval
        self.slow_threshold = max(0.0, float(slow_threshold))
        self.slow_window = max(0.1, float(slow_window))
        self.slow_warmup = max(0.0, float(slow_warmup))
        self.slow_min_bytes = max(0, int(slow_min_bytes))
        self.stall_switches = max(0, int(stall_switches))
        #: 下载过慢时的换源回调：输入当前下载地址，返回新地址或 None
        self.url_resolver = url_resolver
        self._ssl = ssl.create_default_context()
        self._last_progress = 0.0
        self._pool = threading.local()

    # ------------------------------------------------------------------ HTTP
    def token_scope(self) -> str:
        """便于日志/界面展示的令牌作用域（未配置时为空）。"""
        if self.token_origin is None:
            return ""
        scheme, host, port = self.token_origin
        return f"{scheme}://{host}:{port}"

    def sends_token(self, url: str) -> bool:
        """这次请求会不会带上镜像令牌：仅当 scheme/host/port 与镜像源完全一致。"""
        if not self.token or self.token_origin is None:
            return False
        return origin_of(url) == self.token_origin

    def _headers(self, url: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            # 服务端据此判断客户端是否低于最低版本（旧版本会被 426 拒绝）
            "X-OffCraft-Version": __version__,
        }
        # 令牌只能发给配置的镜像源：官方源、第三方 CDN、明文外链一律不带
        if self.sends_token(url):
            headers["X-OffCraft-Token"] = self.token or ""
        if extra:
            headers.update(extra)
        return headers

    def _pooled(self) -> dict[tuple[str, str, int], http.client.HTTPConnection]:
        pool = getattr(self._pool, "conns", None)
        if pool is None:
            pool = {}
            self._pool.conns = pool
        return pool

    def _new_connection(
        self, scheme: str, host: str, port: int, timeout: float | None = None
    ) -> http.client.HTTPConnection:
        raw = open_tunnel(host, port, self.proxy, timeout or self.timeout)
        if scheme == "https":
            raw = self._ssl.wrap_socket(raw, server_hostname=host)
        conn = http.client.HTTPConnection(host, port, timeout=timeout or self.timeout)
        conn.sock = raw
        return conn

    def probe(self, url: str, *, first_chunk: int = 2048, timeout: float | None = None) -> float:
        """探测首字节耗时（秒），不进入连接池。

        只读取首个数据块（附带 ``Range`` 限制），用于比较镜像与直连的时延；
        失败时抛 :class:`DownloadError`。
        """
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise DownloadError(f"非法 URL：{url}")
        scheme = parts.scheme
        host = parts.hostname
        port = parts.port or (443 if scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        headers = self._headers(url, {"Range": f"bytes=0-{max(first_chunk - 1, 0)}"})
        started = time.monotonic()
        conn = self._new_connection(scheme, host, port, timeout)
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            if resp.status not in (200, 206):
                raise DownloadError(f"HTTP {resp.status}：{url}")
            resp.read(first_chunk)
            return time.monotonic() - started
        except (OSError, http.client.HTTPException) as exc:
            raise DownloadError(f"探测失败：{url}（{exc}）") from exc
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def probe_sample(
        self, url: str, *, sample_bytes: int = 256 * 1024, timeout: float | None = None
    ) -> dict[str, float | int | None]:
        """探测「首字节耗时 + 下载速率」，不进入连接池。

        只读取前 ``sample_bytes`` 字节（附带 ``Range`` 限制），返回
        ``{"ttfb": 秒, "speed": 字节/秒, "bytes": 实读字节数}``。
        服务端不支持 ``Range`` 时按整包首段处理；失败抛 :class:`DownloadError`。
        """
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise DownloadError(f"非法 URL：{url}")
        scheme = parts.scheme
        host = parts.hostname
        port = parts.port or (443 if scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        want = max(1, int(sample_bytes))
        headers = self._headers(url, {"Range": f"bytes=0-{want - 1}"})
        started = time.monotonic()
        conn = self._new_connection(scheme, host, port, timeout)
        first_at: float | None = None
        received = 0
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            if resp.status not in (200, 206):
                raise DownloadError(f"HTTP {resp.status}：{url}")
            while received < want:
                chunk = resp.read(min(16 * 1024, want - received))
                if not chunk:
                    break
                if first_at is None:
                    first_at = time.monotonic()
                received += len(chunk)
            ended = time.monotonic()
            ttfb = None if first_at is None else first_at - started
            elapsed = (ended - first_at) if first_at is not None else 0.0
            speed = (received / elapsed) if elapsed > 0 and received else None
            if ttfb is None:
                raise DownloadError(f"空响应：{url}")
            return {"ttfb": ttfb, "speed": speed, "bytes": received}
        except (OSError, http.client.HTTPException) as exc:
            raise DownloadError(f"探测失败：{url}（{exc}）") from exc
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _open(
        self,
        url: str,
        extra_headers: dict[str, str] | None = None,
        method: str = "GET",
        body: bytes | None = None,
    ):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise DownloadError(f"非法 URL：{url}")
        scheme = parts.scheme
        host = parts.hostname
        port = parts.port or (443 if scheme == "https" else 80)
        key = (scheme, host, port)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        headers = self._headers(url, extra_headers)
        if body is not None and "Content-Length" not in headers:
            headers["Content-Length"] = str(len(body))

        pool = self._pooled()
        conn = pool.pop(key, None)
        reused = conn is not None
        if conn is None:
            conn = self._new_connection(scheme, host, port)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException):
            try:
                conn.close()
            except OSError:
                pass
            if reused:
                conn = self._new_connection(scheme, host, port)
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
            else:
                raise
        return conn, resp, key

    def _finish(self, conn, resp, key: tuple[str, str, int]) -> None:
        """响应读完后：可复用则放回连接池，否则关闭。"""
        if resp.will_close:
            try:
                conn.close()
            except OSError:
                pass
            return
        self._pooled()[key] = conn

    def close(self) -> None:
        pool = getattr(self._pool, "conns", None) or {}
        for conn in pool.values():
            try:
                conn.close()
            except OSError:
                pass
        pool.clear()

    def _request_with_retry(
        self,
        url: str,
        extra_headers: dict[str, str] | None = None,
        method: str = "GET",
        body: bytes | None = None,
        *,
        attempts: int | None = None,
    ):
        attempts = self.retries + 1 if attempts is None else attempts
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return self._open(url, extra_headers, method, body)
            except (OSError, http.client.HTTPException) as exc:
                last = exc
                if attempt + 1 < attempts:
                    time.sleep(min(2 ** attempt * 0.5, 4.0))
        raise DownloadError(f"请求失败：{url}（{last}）") from last

    def fetch_bytes(self, url: str, *, max_size: int = 64 * 1024 * 1024) -> bytes:
        conn, resp, key = self._request_with_retry(url)
        reusable = False
        try:
            if resp.status != 200:
                raise DownloadError(f"HTTP {resp.status}：{url}")
            data = resp.read(max_size + 1)
            if len(data) > max_size:
                raise DownloadError(f"响应超过 {max_size} 字节：{url}")
            reusable = True
            return data
        finally:
            if reusable:
                self._finish(conn, resp, key)
            else:
                conn.close()

    def fetch_json(self, url: str) -> Any:
        raw = self.fetch_bytes(url)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DownloadError(f"JSON 解析失败：{url}（{exc}）") from exc

    def request_json(
        self,
        url: str,
        method: str = "GET",
        *,
        body: dict[str, Any] | bytes | None = None,
        headers: dict[str, str] | None = None,
        max_size: int = 4 * 1024 * 1024,
    ) -> Any:
        """任意方法的 JSON 请求（镜像 API 上报、皮肤上传等）。

        仍走 :meth:`_headers` 的令牌规则 —— 只有目标 origin 与镜像一致才带 token。
        """
        payload = body
        extra = dict(headers or {})
        if isinstance(body, dict):
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            extra.setdefault("Content-Type", "application/json; charset=utf-8")
        elif isinstance(body, bytes) and "Content-Type" not in extra:
            extra.setdefault("Content-Type", "application/octet-stream")
        conn, resp, key = self._request_with_retry(url, extra, method.upper(), payload, attempts=1)
        reusable = False
        try:
            if resp.status not in (200, 201, 202, 204):
                detail = resp.read(512).decode("utf-8", "replace")
                raise DownloadError(f"HTTP {resp.status}：{detail[:200]}")
            data = resp.read(max_size + 1)
            if len(data) > max_size:
                raise DownloadError(f"响应超过 {max_size} 字节：{url}")
            reusable = True
            if not data:
                return {}
            try:
                return json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DownloadError(f"JSON 解析失败：{url}（{exc}）") from exc
        finally:
            if reusable:
                self._finish(conn, resp, key)
            else:
                conn.close()

    # -------------------------------------------------------------- download
    def download(
        self,
        task: DownloadTask,
        tracker: ProgressTracker | None = None,
        cancel: threading.Event | None = None,
        *,
        keep_part: bool = False,
    ) -> bool:
        """下载单个文件；已存在且校验通过返回 ``False``（跳过）。

        ``keep_part=True`` 时网络类失败会保留 ``.part``，下次从断点继续
        （自更新安装包用得到；普通资源仍按原行为清理残片）。
        """
        dest = Path(task.dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if verify_file(dest, task.sha1, task.size):
            return False
        if tracker is not None:
            tracker.file_started(task.name)
        part = dest.with_name(dest.name + ".part")
        url = task.url
        last_error: Exception | None = None
        failures = 0
        switches = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            try:
                self._download_attempt(
                    task,
                    part,
                    tracker,
                    cancel,
                    url=url,
                    slow=switches < self.stall_switches,
                )
                if task.size is not None:
                    actual = part.stat().st_size
                    if actual != task.size:
                        raise DownloadError(f"大小不符：期望 {task.size}，实际 {actual}")
                if task.sha1 and sha1_file(part).lower() != task.sha1.lower():
                    raise DownloadError("SHA-1 校验失败")
                os.replace(part, dest)
                return True
            except Cancelled:
                raise
            except SourceStalled as exc:
                last_error = exc
                # 保留 .part，询问调用方换源；没有更优源时关闭慢速检测继续下载
                new_url = (
                    self.url_resolver(url) if callable(self.url_resolver) else None
                )
                if new_url and new_url != url:
                    switches += 1
                    url = new_url
                    continue
                switches = self.stall_switches
                continue
            except (OSError, http.client.HTTPException, DownloadError) as exc:
                last_error = exc
                # 网络中断保留 .part 以便续传；校验/尺寸错误才丢弃，避免坏片被复用
                if part.exists() and not (
                    keep_part and isinstance(exc, (OSError, http.client.HTTPException))
                ):
                    part.unlink(missing_ok=True)
                failures += 1
                if failures > self.retries:
                    break
                time.sleep(min(2 ** (failures - 1) * 0.5, 4.0))
        raise DownloadError(f"下载失败：{task.name}（{last_error}）") from last_error

    def _download_attempt(
        self,
        task: DownloadTask,
        part: Path,
        tracker: ProgressTracker | None,
        cancel: threading.Event | None,
        *,
        url: str | None = None,
        slow: bool = False,
    ) -> int:
        target = url or task.url
        offset = part.stat().st_size if part.exists() else 0
        headers: dict[str, str] = {}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        conn, resp, key = self._request_with_retry(target, headers)
        written = 0
        completed = False
        started = time.monotonic()
        window_start = started
        window_bytes = 0
        try:
            if resp.status == 416:
                part.unlink(missing_ok=True)
                raise DownloadError("断点位置无效，已重置")
            if resp.status == 206 and offset:
                mode = "ab"
                expected = _content_range_total(resp) or task.size
            elif resp.status == 200:
                mode = "wb"
                offset = 0
                expected = task.size
            else:
                raise DownloadError(f"HTTP {resp.status}：{target}")
            if expected and task.size and expected != task.size:
                raise DownloadError(f"服务端大小 {expected} 与预期 {task.size} 不符")
            # 直接写 .part：续传时前缀已在该文件中，不能再另起临时文件
            with part.open(mode) as fh:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
                    window_bytes += len(chunk)
                    if tracker is not None:
                        tracker.add_bytes(len(chunk))
                        self._emit_progress(tracker)
                    if slow:
                        now = time.monotonic()
                        if (
                            now - started >= self.slow_warmup
                            and now - window_start >= self.slow_window
                        ):
                            rate = window_bytes / max(now - window_start, 1e-6)
                            remaining = (
                                None if task.size is None else task.size - offset - written
                            )
                            if (
                                rate < self.slow_threshold
                                and (remaining is None or remaining > self.slow_min_bytes)
                            ):
                                raise SourceStalled(
                                    f"下载速率过低（{rate / 1024:.0f} KB/s），尝试切换下载源"
                                )
                            window_start, window_bytes = now, 0
            completed = True
            return written
        finally:
            if completed and not (cancel is not None and cancel.is_set()):
                self._finish(conn, resp, key)
            else:
                conn.close()

    def _emit_progress(self, tracker: ProgressTracker) -> None:
        if self.progress_callback is None:
            return
        now = time.monotonic()
        if now - self._last_progress >= self.progress_interval:
            self._last_progress = now
            self.progress_callback(tracker)

    # ------------------------------------------------------------------ bulk
    def run(
        self,
        tasks: Iterable[DownloadTask],
        *,
        tracker: ProgressTracker | None = None,
        cancel: threading.Event | None = None,
    ) -> DownloadReport:
        tasks = [t for t in tasks]
        report = DownloadReport()
        tracker = tracker or ProgressTracker()
        tracker.start(len(tasks), sum(t.size or 0 for t in tasks))

        pending: list[DownloadTask] = []
        for task in tasks:
            if verify_file(Path(task.dest), task.sha1, task.size):
                report.skipped.append(task)
                tracker.file_finished()
            else:
                pending.append(task)

        if not pending:
            return report

        errors: dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=min(self.workers, len(pending))) as pool:
            futures: dict[Future[bool], DownloadTask] = {}
            for task in pending:
                if cancel is not None and cancel.is_set():
                    report.failed.append((task, "已取消"))
                    continue
                future = pool.submit(self.download, task, tracker, cancel)
                futures[future] = task
            for future in as_completed(futures):
                task = futures[future]
                try:
                    future.result()
                    report.succeeded.append(task)
                    tracker.file_finished()
                except Cancelled:
                    report.failed.append((task, "已取消"))
                except Exception as exc:  # noqa: BLE001 - 汇总给 UI
                    report.failed.append((task, str(exc)))
                self._emit_progress(tracker)
        if errors:
            report.failed.extend((pending[i], msg) for i, msg in errors.items())
        report.bytes_written = tracker.done_bytes
        return report


def _content_range_total(resp) -> int | None:
    header = resp.getheader("Content-Range") or ""
    if "/" not in header:
        return None
    total = header.rsplit("/", 1)[1].strip()
    try:
        return int(total)
    except ValueError:
        return None
