"""发布版本自动检测：直接读 GitHub Releases API，结果在服务端缓存。

设计要点：

- **不再把最新版本写进配置**：最新版本、tag、发布页、资产与更新日志全部来自
  ``https://api.github.com/repos/<repo>/releases``，发版后服务端自己就更新了，
  运维不需要改 ``.env`` 再重启。
- **两级缓存**：内存快照（进程内零成本读取）+ JSON 文件（重启后立刻可用，
  也能在 GitHub 不可达时顶住）。
- **过期不阻塞**：快照还在 TTL 内就直接返回；过期后只有第一个请求去回源
  （带 ``ETag`` 条件请求，304 也算一次成功），其余请求继续用旧快照；回源失败
  时按指数退避重试，旧快照退化为 stale 但照常服务。
- **只缓存确定结论**：GitHub 说「还没有任何正式发布」（404）会缓存一份空快照，
  避免每个请求都打一次 API；网络错误 / 5xx / 限流不覆盖已有快照。
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from server_mirror.netproxy import open_upstream
from server_mirror.versions import normalize, tag_version, version_key

__all__ = [
    "SNAPSHOT_SCHEMA",
    "GitHubReleases",
    "HttpGitHubClient",
    "ReleaseAsset",
    "ReleaseInfo",
    "ReleaseProvider",
    "ReleaseSnapshot",
    "asset_url",
    "mirror_url",
]

#: 快照文件格式版本（结构变化时递增，读到旧文件就丢弃）
SNAPSHOT_SCHEMA = 1
#: 资产文件名里认作「安装包」的标记，用于在多个资产里挑主下载项
_INSTALLER_HINTS = ("setup", "install", "-win64.exe", ".msi")
#: 列表接口里最多摘要多少条历史（够官网更新日志翻几页）
_MAX_HISTORY = 8


def _text(value: Any, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def _int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _safe_format(template: str, version: str, tag: str = "") -> str:
    """``{version}`` / ``{tag}`` 占位替换；模板非法时原样返回。"""
    try:
        return template.format(version=version, tag=tag or f"v{version}")
    except (KeyError, ValueError):
        return template


def asset_url(repo: str, tag: str, name: str) -> str:
    """GitHub 资产直连地址。"""
    return f"https://github.com/{repo}/releases/download/{tag or 'v0.0.0'}/{name or ''}".rstrip("/")


def mirror_url(repo: str, tag: str, name: str) -> str:
    """同源镜像地址（由 ``/github/`` 代理路由提供，支持 Range 断点续传）。"""
    return f"/github/{repo}/releases/download/{tag or 'v0.0.0'}/{name or ''}".rstrip("/")


@dataclass(slots=True)
class ReleaseAsset:
    """一个发布资产（安装包 / 免安装包）。"""

    name: str = ""
    size: int | None = None
    url: str = ""
    downloads: int | None = None

    @classmethod
    def from_api(cls, item: Any) -> "ReleaseAsset | None":
        if not isinstance(item, dict):
            return None
        name = _text(item.get("name"), 200)
        # 优先 browser_download_url：api.github.com 的资产地址需要
        # ``Accept: application/octet-stream`` 才能下载，客户端和浏览器都不认。
        url = _text(item.get("browser_download_url") or item.get("url"), 500)
        if not name and url:
            name = urlsplit(url).path.rsplit("/", 1)[-1]
        if not name:
            return None
        return cls(
            name=name,
            size=_int(item.get("size")),
            url=url,
            downloads=_int(item.get("download_count", item.get("downloads"))),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "size": self.size, "url": self.url, "downloads": self.downloads}

    def rank(self) -> tuple[int, int]:
        """挑主下载项：文件名像安装包优先，同为安装包时名字短的优先。"""
        lowered = self.name.lower()
        installer = 1 if any(hint in lowered for hint in _INSTALLER_HINTS) else 0
        return (installer, -len(self.name))


@dataclass(slots=True)
class ReleaseInfo:
    """一个 GitHub Release 的摘要（不含重型字段）。"""

    tag: str = ""
    version: str = ""
    name: str = ""
    url: str = ""
    body: str = ""
    published_at: str = ""
    prerelease: bool = False
    draft: bool = False
    assets: list[ReleaseAsset] = field(default_factory=list)
    #: 该发布按 tag 解析出的下载目录，如 ``https://github.com/o/r/releases/download/v0.9.1``
    download_base: str = ""

    @property
    def channel(self) -> str:
        tag = self.tag.lower()
        if self.prerelease or "beta" in tag or "rc" in tag or "alpha" in tag:
            return "beta"
        return "stable"

    def pick_asset(self, hints: tuple[str, ...] = ()) -> ReleaseAsset | None:
        """挑主下载项：优先精确 / 模糊匹配名字提示，其次按 :meth:`ReleaseAsset.rank`。"""
        if not self.assets:
            return None
        wanted = [hint.lower() for hint in hints if hint and hint.strip()]
        for hint in wanted:
            for asset in self.assets:
                if asset.name.lower() == hint:
                    return asset
        for hint in wanted:
            for asset in self.assets:
                if hint in asset.name.lower():
                    return asset
        return max(self.assets, key=ReleaseAsset.rank)

    def asset_dicts(self) -> list[dict[str, Any]]:
        return [asset.as_dict() for asset in self.assets]

    def as_summary(self, *, body_limit: int = 400) -> dict[str, Any]:
        """给 ``/api/update``、看板与官网用的轻量结构。"""
        body = self.body or ""
        if body_limit and len(body) > body_limit:
            body = body[:body_limit].rstrip() + "…"
        return {
            "tag": self.tag,
            "version": self.version,
            "name": self.name,
            "url": self.url,
            "published_at": self.published_at,
            "channel": self.channel,
            "prerelease": self.channel == "beta",
            "body": body,
            "assets": self.asset_dicts(),
        }

    def as_detail(self) -> dict[str, Any]:
        data = self.as_summary(body_limit=0)
        data["download_base"] = self.download_base
        return data

    def as_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "version": self.version,
            "name": self.name,
            "url": self.url,
            "body": self.body,
            "published_at": self.published_at,
            "prerelease": self.prerelease,
            "draft": self.draft,
            "assets": self.asset_dicts(),
            "download_base": self.download_base,
        }

    @classmethod
    def from_api(cls, data: Any, *, repo: str = "") -> "ReleaseInfo | None":
        if not isinstance(data, dict):
            return None
        tag = _text(data.get("tag_name"), 64)
        if not tag:
            return None
        info = cls(
            tag=tag,
            version=tag_version(tag),
            name=_text(data.get("name"), 160),
            url=_text(data.get("html_url"), 500),
            body=_text(data.get("body"), 200_000),
            published_at=_text(data.get("published_at") or data.get("created_at"), 40),
            prerelease=bool(data.get("prerelease")),
            draft=bool(data.get("draft")),
        )
        assets = [ReleaseAsset.from_api(item) for item in (data.get("assets") or [])]
        info.assets = [asset for asset in assets if asset is not None]
        if repo:
            info.url = info.url or f"https://github.com/{repo}/releases/tag/{tag}"
            info.download_base = info.download_base or f"https://github.com/{repo}/releases/download/{tag}"
        return info

    @classmethod
    def from_dict(cls, data: Any) -> "ReleaseInfo | None":
        if not isinstance(data, dict):
            return None
        tag = _text(data.get("tag"), 64)
        if not tag:
            return None
        assets = [ReleaseAsset.from_api(item) for item in (data.get("assets") or [])]
        return cls(
            tag=tag,
            version=_text(data.get("version"), 64) or tag_version(tag),
            name=_text(data.get("name"), 160),
            url=_text(data.get("url"), 500),
            body=_text(data.get("body"), 200_000),
            published_at=_text(data.get("published_at"), 40),
            prerelease=bool(data.get("prerelease")),
            draft=bool(data.get("draft")),
            assets=[asset for asset in assets if asset is not None],
            download_base=_text(data.get("download_base"), 500),
        )


@dataclass(slots=True)
class ReleaseSnapshot:
    """一次成功检测的结果，以及它的新鲜度。"""

    latest: ReleaseInfo | None = None
    min_version: str = ""
    releases: list[ReleaseInfo] = field(default_factory=list)
    fetched_at: float = 0.0
    last_attempt: float = 0.0
    #: 快照来自哪里：``github`` / ``file`` / ``fallback`` / ``empty``
    source: str = "empty"
    error: str = ""
    fails: int = 0
    retry_at: float = 0.0
    etag: str = ""
    last_modified: str = ""

    @property
    def version(self) -> str:
        return self.latest.version if self.latest else ""

    @property
    def tag(self) -> str:
        return self.latest.tag if self.latest else ""

    @property
    def assets(self) -> list[ReleaseAsset]:
        return list(self.latest.assets) if self.latest else []

    def age(self, now: float | None = None) -> float:
        if not self.fetched_at:
            return float("inf")
        return max(0.0, (now if now is not None else time.time()) - self.fetched_at)

    def is_fresh(self, ttl: float, now: float | None = None) -> bool:
        return bool(self.fetched_at) and self.age(now) < max(0.0, ttl)

    def as_dict(
        self, *, ttl: float = 0.0, now: float | None = None, stale: bool = False
    ) -> dict[str, Any]:
        moment = now if now is not None else time.time()
        return {
            "schema": SNAPSHOT_SCHEMA,
            "latest": self.latest.as_dict() if self.latest else None,
            "min_version": self.min_version,
            "releases": [item.as_dict() for item in self.releases],
            "fetched_at": self.fetched_at,
            "last_attempt": self.last_attempt,
            "source": self.source,
            "error": self.error,
            "fails": self.fails,
            "retry_at": self.retry_at,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "age": None if not self.fetched_at else round(self.age(moment), 3),
            "ttl": ttl,
            "stale": stale,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ReleaseSnapshot | None":
        if not isinstance(data, dict) or data.get("schema") != SNAPSHOT_SCHEMA:
            return None
        latest = ReleaseInfo.from_dict(data.get("latest"))
        releases = [r for r in (ReleaseInfo.from_dict(x) for x in (data.get("releases") or [])) if r]
        return cls(
            latest=latest,
            min_version=normalize(data.get("min_version")),
            releases=releases,
            fetched_at=float(data.get("fetched_at") or 0.0),
            last_attempt=float(data.get("last_attempt") or 0.0),
            source=_text(data.get("source") or "file", 16),
            error=_text(data.get("error"), 300),
            fails=int(data.get("fails") or 0),
            retry_at=float(data.get("retry_at") or 0.0),
            etag=_text(data.get("etag"), 200),
            last_modified=_text(data.get("last_modified"), 100),
        )


class HttpGitHubClient:
    """GitHub Releases API 客户端：编请求、跳转 / 限流 / 条件请求都在这里。"""

    def __init__(
        self,
        *,
        proxy: str = "",
        token: str = "",
        timeout: float = 8.0,
        user_agent: str = "OffCraft-Mirror/1.0",
    ) -> None:
        self.proxy = proxy or ""
        self.token = token or ""
        self.timeout = max(1.0, float(timeout))
        self.user_agent = user_agent

    def headers(self, etag: str = "", last_modified: str = "") -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": self.user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if etag:
            headers["If-None-Match"] = etag
        elif last_modified:
            headers["If-Modified-Since"] = last_modified
        return headers

    def fetch(
        self,
        url: str,
        *,
        etag: str = "",
        last_modified: str = "",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any, dict[str, str]]:
        """GET 一个地址，返回 ``(状态码, 数据, 响应头)``。

        状态码 0 表示连接失败、502 表示响应不是 JSON、304 表示内容没变；
        301/302/307/308 时「数据」是跳转目标地址。
        """
        conn = None
        try:
            conn = open_upstream(url, self.proxy, timeout=self.timeout)
            parts = urlsplit(url)
            path = parts.path or "/"
            if parts.query:
                path = f"{path}?{parts.query}"
            request_headers = self.headers(etag, last_modified)
            request_headers.update(headers or {})
            conn.request("GET", path, headers=request_headers)
            resp = conn.getresponse()
            body = resp.read(4 * 1024 * 1024)
            headers = {name.lower(): value for name, value in resp.getheaders()}
            status = resp.status
            if status == 304:
                return 304, None, headers
            if status in (301, 302, 307, 308) and headers.get("location"):
                return status, headers["location"], headers
            if status != 200:
                return status, body[:200].decode("utf-8", "replace").strip(), headers
            try:
                return 200, json.loads(body.decode("utf-8")), headers
            except (ValueError, UnicodeDecodeError) as exc:
                return 502, f"GitHub 返回了非 JSON 内容：{exc}", headers
        except (OSError, http.client.HTTPException) as exc:
            return 0, f"连接 GitHub 失败：{exc}", {}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass


class ReleaseProvider:
    """进程内单例：持有最新快照，负责刷新与落盘。

    ``fetch`` 参数用于测试注入（返回 :class:`ReleaseSnapshot`）；``clock`` 同理。
    """

    def __init__(
        self,
        *,
        repo: str,
        cache_file: Path | str = "",
        ttl: float = 600.0,
        retry_min: float = 30.0,
        retry_max: float = 1800.0,
        timeout: float = 8.0,
        proxy: str = "",
        token: str = "",
        fallback_version: str = "",
        fallback_min: str = "",
        min_mode: str = "off",
        include_prerelease: bool = False,
        asset_hint: str = "",
        release_url: str = "",
        offline: bool = False,
        upstream_base: str = "",
        lock_timeout: float = 3.0,
        client_factory: Callable[..., Any] | None = None,
        fetch: Callable[[], ReleaseSnapshot] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.repo = (repo or "").strip().strip("/")
        self.cache_file = Path(cache_file) if cache_file else None
        self.ttl = max(0.0, float(ttl))
        self.retry_min = max(1.0, float(retry_min))
        self.retry_max = max(self.retry_min, float(retry_max))
        self.timeout = max(1.0, float(timeout))
        self.proxy = proxy or ""
        self.token = token or ""
        self.fallback_version = normalize(fallback_version)
        self.fallback_min = normalize(fallback_min)
        self.min_mode = (min_mode or "off").strip().lower()
        self.include_prerelease = bool(include_prerelease)
        self.asset_hint = (asset_hint or "").strip()
        self.release_url = (release_url or "").strip()
        self.offline = bool(offline)
        self.upstream_base = (upstream_base or "").strip().rstrip("/")
        self.lock_timeout = max(0.05, float(lock_timeout))
        self.clock = clock
        self._client_factory = client_factory or HttpGitHubClient
        self._fetch_override = fetch
        self._client: Any = None
        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()
        self._warned_file = False
        # 先用文件快照 / 兜底值顶上，避免第一次请求就撞网络
        self._snapshot = self._load_file() or self._fallback_snapshot("尚无快照")

    # -------------------------------------------------------------- 快照读取
    def snapshot(self) -> ReleaseSnapshot:
        """当前快照（可能已过期）。"""
        with self._lock:
            return self._snapshot

    def current(
        self,
        *,
        ttl: float | None = None,
        refresh: bool = False,
        block: bool = True,
        timeout: float | None = None,
    ) -> ReleaseSnapshot:
        """取当前快照；过期时刷新一次（``block`` 为假则交给后台线程）。"""
        ttl = self.ttl if ttl is None else max(0.0, float(ttl))
        wait = self.lock_timeout if timeout is None else max(0.0, float(timeout))
        now = self.clock()
        snap = self.snapshot()
        if not refresh and snap.is_fresh(ttl, now):
            return snap
        if now < snap.retry_at:
            # 上次回源失败且还在退避窗口：先用旧快照，不再打网络
            return snap
        if not block:
            self.refresh_async()
            return self.snapshot()
        self.refresh(wait=wait)
        return self.snapshot()

    def refresh_async(self) -> None:
        """后台刷新（不等待）：已有线程在刷新时直接返回。"""
        if self._fetch_lock.locked():
            return

        def worker() -> None:
            try:
                self.refresh(wait=0)
            except Exception:  # noqa: BLE001 - 后台线程不能把异常抛给解释器
                pass

        threading.Thread(target=worker, name="release-refresh", daemon=True).start()

    def start(self, *, interval: float | None = None) -> None:
        """起一个守护线程定时刷新（服务端常驻时用）。"""
        period = max(30.0, float(interval if interval is not None else self.ttl or 600.0))

        def loop() -> None:
            while True:
                time.sleep(period)
                try:
                    self.refresh(wait=0)
                except Exception:  # noqa: BLE001
                    continue

        self.refresh_async()
        threading.Thread(target=loop, name="release-watch", daemon=True).start()

    # ------------------------------------------------------------------ 刷新
    def refresh(self, *, wait: float = 0.0) -> ReleaseSnapshot:
        """回源一次；``wait`` > 0 表示最多等这么久（等不到就先用旧快照）。"""
        if wait > 0:
            acquired = self._fetch_lock.acquire(timeout=wait)
        else:
            acquired = self._fetch_lock.acquire(blocking=False)
        if not acquired:
            return self.snapshot()
        try:
            snapshot = self._fetch()
            with self._lock:
                self._snapshot = snapshot
            self._save_file(snapshot)
            return snapshot
        finally:
            self._fetch_lock.release()

    def _fetch(self) -> ReleaseSnapshot:
        if self._fetch_override is not None:
            return self._fetch_override()
        previous = self.snapshot()
        now = self.clock()
        if not self.repo:
            return self._fallback_snapshot("未配置发布仓库")
        if self.offline:
            # 关闭自动检测：只用快照 / 兜底值，绝不出网
            snap = self._clone(previous)
            if snap.latest is None:
                return self._fallback_snapshot("已关闭自动检测")
            snap.source = "file" if snap.fetched_at else "fallback"
            snap.error = "已关闭自动检测（MIRROR_UPDATE_AUTO=off）"
            snap.last_attempt = now
            return snap

        # 1) 先按配置仓库问 GitHub Releases API
        status, data, headers = self._http().fetch(
            self._releases_url(),
            etag=previous.etag if previous.source == "github" else "",
            last_modified=previous.last_modified if previous.source == "github" else "",
        )
        if status in (301, 302, 307, 308) and isinstance(data, str):
            status, data, headers = self._http().fetch(data)
        built = self._build(status, data, headers, now, source="github")
        if built is not None:
            return built

        # 2) GitHub 不可达时，退一步问镜像服务自己的 /api/update（可能已检测过）
        upstream = self._fetch_upstream()
        if upstream is not None:
            return upstream
        return self._failed(
            previous,
            str(data or f"GitHub 返回 HTTP {status}")[:300],
            now,
        )

    def _build(
        self,
        status: int,
        data: Any,
        headers: dict[str, str],
        now: float,
        *,
        source: str = "github",
    ) -> ReleaseSnapshot | None:
        """把一次 API / 上游响应转成快照；无法判定时返回 ``None``（交给下一步）。"""
        if status == 304:
            snap = self._clone(self.snapshot())
            snap.fetched_at = now
            snap.last_attempt = now
            snap.error = ""
            snap.fails = 0
            snap.retry_at = 0.0
            snap.source = source
            return snap
        if status == 404:
            # GitHub 说「还没有正式发布」是确定结论：缓存空快照，避免每请求都打 API
            snap = ReleaseSnapshot(
                latest=None,
                min_version=self._min_version(""),
                fetched_at=now,
                last_attempt=now,
                source=source,
                error="仓库暂无已发布版本",
            )
            self._absorb_headers(snap, headers)
            return snap
        if status != 200:
            return None

        # 兼容三种入口：GitHub 列表 / GitHub 单条 release / 镜像 /api/update 摘要
        entries: list[dict[str, Any]] = []
        if isinstance(data, list):
            entries = [item for item in data if isinstance(item, dict)]
        elif isinstance(data, dict) and isinstance(data.get("releases"), list):
            entries = [item for item in data["releases"] if isinstance(item, dict)]
        elif isinstance(data, dict) and ("latest_version" in data or data.get("tag_name")):
            single = _snapshot_from_update_payload(data, repo=self.repo)
            if single is not None:
                single.source = source
                single.fetched_at = single.last_attempt = now
                single.min_version = self.fallback_min or single.min_version
                return single

        stable = [
            entry for entry in entries
            if not entry.get("draft") and not self._prerelease(entry)
        ]
        pool = stable or [entry for entry in entries if not entry.get("draft")]
        infos = [info for info in (ReleaseInfo.from_api(entry, repo=self.repo) for entry in pool) if info]
        infos.sort(key=lambda item: version_key(item.version) or ((-1,), (-1, ())), reverse=True)
        if not infos:
            snap = ReleaseSnapshot(
                latest=None,
                min_version=self._min_version(""),
                fetched_at=now,
                last_attempt=now,
                source=source,
                error="仓库暂无已发布版本",
            )
            self._absorb_headers(snap, headers)
            return snap
        latest = infos[0]
        if self.release_url:
            latest.url = _safe_format(self.release_url, latest.version, latest.tag)
        snap = ReleaseSnapshot(
            latest=latest,
            min_version=self._min_version(latest.version),
            releases=infos[:_MAX_HISTORY],
            fetched_at=now,
            last_attempt=now,
            source=source,
        )
        self._absorb_headers(snap, headers)
        return snap

    def _fetch_upstream(self) -> ReleaseSnapshot | None:
        """GitHub 不可达时，问镜像自己的 ``/api/update``（避免上游也跟着挂）。"""
        if not self.upstream_base:
            return None
        url = f"{self.upstream_base}/api/update"
        status, data, headers = self._http().fetch(url)
        if status != 200 or not isinstance(data, dict):
            return None
        snap = _snapshot_from_update_payload(data, repo=self.repo)
        if snap is None:
            return None
        now = self.clock()
        snap.source = "upstream"
        snap.fetched_at = now
        snap.last_attempt = now
        snap.error = ""
        snap.min_version = self.fallback_min or snap.min_version
        self._absorb_headers(snap, headers)
        return snap

    def _min_version(self, latest: str) -> str:
        """最低版本：显式配置优先；``auto`` = 当前次版本的上一档（``0.9.1`` → ``0.8.0``）。"""
        if self.fallback_min:
            return self.fallback_min
        if self.min_mode == "auto":
            return _previous_series(latest)
        return ""

    def _prerelease(self, entry: dict[str, Any]) -> bool:
        if self.include_prerelease:
            return False
        if entry.get("prerelease"):
            return True
        tag = str(entry.get("tag_name") or "").lower()
        return any(word in tag for word in ("beta", "rc", "alpha"))

    def _releases_url(self, base: str = "https://api.github.com") -> str:
        return f"{base.rstrip('/')}/repos/{self.repo}/releases?per_page=10"

    def _http(self) -> Any:
        if self._client is None:
            self._client = self._client_factory(
                proxy=self.proxy, token=self.token, timeout=self.timeout
            )
        return self._client

    @staticmethod
    def _absorb_headers(snapshot: ReleaseSnapshot, headers: dict[str, str]) -> None:
        snapshot.etag = _text(headers.get("etag"), 200)
        snapshot.last_modified = _text(headers.get("last-modified"), 100)

    def _failed(self, previous: ReleaseSnapshot, error: str, now: float) -> ReleaseSnapshot:
        snapshot = self._clone(previous)
        snapshot.fails = previous.fails + 1
        delay = min(self.retry_max, self.retry_min * (2 ** max(0, snapshot.fails - 1)))
        snapshot.retry_at = now + delay
        snapshot.last_attempt = now
        snapshot.error = error
        if not snapshot.fetched_at and snapshot.latest is None:
            fallback = self._fallback_snapshot(error)
            fallback.fails = snapshot.fails
            fallback.retry_at = snapshot.retry_at
            fallback.last_attempt = now
            return fallback
        return snapshot

    def _fallback_snapshot(self, error: str = "") -> ReleaseSnapshot:
        """没有任何可用数据时的兜底：用环境变量里的版本号顶上（stale）。"""
        version = self.fallback_version
        latest = None
        if version:
            tag = f"v{version}"
            latest = ReleaseInfo(
                tag=tag,
                version=version,
                name=f"CrMCDownload {version}",
                url=self._release_page(tag),
                download_base=f"https://github.com/{self.repo}/releases/download/{tag}",
                assets=[
                    ReleaseAsset(
                        name=f"CrMCDownload-{version}-win64-setup.exe",
                        url=asset_url(self.repo, tag, f"CrMCDownload-{version}-win64-setup.exe"),
                    )
                ],
            )
        return ReleaseSnapshot(
            latest=latest,
            min_version=self.fallback_min,
            releases=[latest] if latest else [],
            fetched_at=0.0,
            last_attempt=self.clock(),
            source="fallback" if latest else "empty",
            error=error,
        )

    def _release_page(self, tag: str) -> str:
        if self.release_url:
            return _safe_format(self.release_url, normalize(tag), tag)
        return f"https://github.com/{self.repo}/releases/tag/{tag}" if tag else f"https://github.com/{self.repo}/releases"

    @staticmethod
    def _clone(snapshot: ReleaseSnapshot) -> ReleaseSnapshot:
        return ReleaseSnapshot(
            latest=snapshot.latest,
            min_version=snapshot.min_version,
            releases=list(snapshot.releases),
            fetched_at=snapshot.fetched_at,
            last_attempt=snapshot.last_attempt,
            source=snapshot.source,
            error=snapshot.error,
            fails=snapshot.fails,
            retry_at=snapshot.retry_at,
            etag=snapshot.etag,
            last_modified=snapshot.last_modified,
        )

    # -------------------------------------------------------------- 文件快照
    def _load_file(self) -> ReleaseSnapshot | None:
        path = self.cache_file
        if path is None:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return ReleaseSnapshot.from_dict(data)

    def _save_file(self, snapshot: ReleaseSnapshot) -> None:
        path = self.cache_file
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = snapshot.as_dict()
            for key in ("stale", "age", "ttl"):
                payload.pop(key, None)
            tmp = path.with_name(f"{path.name}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            if not self._warned_file:
                self._warned_file = True
                print(f"[releases] 快照写入失败（忽略）：{exc}", flush=True)

    # ------------------------------------------------------------------ 工具
    def asset_hints(self, version: str) -> tuple[str, ...]:
        if not self.asset_hint:
            return ()
        return (_safe_format(self.asset_hint, version),)

    def release_for(self, version: str = "") -> ReleaseInfo | None:
        """按版本找发布记录；``version`` 为空时返回当前最新。"""
        snapshot = self.snapshot()
        target = normalize(version) or snapshot.version
        if not target:
            return None
        for item in snapshot.releases:
            if item.version == target:
                return item
        if snapshot.latest is not None and snapshot.latest.version == target:
            return snapshot.latest
        return None

    def asset_for(self, version: str = "") -> ReleaseAsset | None:
        """按版本挑主下载项；``version`` 为空时用当前最新版。"""
        target = normalize(version) or self.snapshot().version
        if not target:
            return None
        release = self.release_for(target)
        if release is None:
            return None
        return release.pick_asset(self.asset_hints(target))

    @property
    def cache_path(self) -> str:
        return str(self.cache_file or "")

    def close(self) -> None:
        self._client = None


class GitHubReleases(ReleaseProvider):
    """语义化别名：GitHub 发布版本提供者。"""


def _previous_series(latest: str) -> str:
    """``auto`` 最低版本：当前次版本的 ``x.N-1.0``（``0.9.1`` → ``0.8.0``）。"""
    key = version_key(latest)
    if key is None:
        return ""
    numbers = key[0]
    if len(numbers) < 2:
        return ""
    return f"{numbers[0]}.{max(0, numbers[1] - 1)}.0"


def _snapshot_from_update_payload(data: dict[str, Any], *, repo: str) -> ReleaseSnapshot | None:
    """把另一个镜像的 ``/api/update`` 摘要当作快照（仅上游兜底时用）。"""
    version = normalize(data.get("latest_version") or data.get("tag_name"))
    if not version:
        return None
    tag = _text(data.get("tag") or data.get("tag_name"), 64) or f"v{version}"
    summary = data.get("release") if isinstance(data.get("release"), dict) else {}
    url = _text(data.get("release_url") or summary.get("url"), 500)
    if not url:
        url = f"https://github.com/{repo}/releases/tag/{tag}"
    assets = [
        asset for asset in (ReleaseAsset.from_api(item) for item in (data.get("assets") or []))
        if asset is not None
    ]
    if not assets:
        asset_field = data.get("asset") if isinstance(data.get("asset"), dict) else {}
        primary = _text(asset_field.get("name"), 200)
        if primary:
            assets = [
                ReleaseAsset(
                    name=primary,
                    size=_int(asset_field.get("size")),
                    url=_text(data.get("download_url"), 500),
                )
            ]
    latest = ReleaseInfo(
        tag=tag,
        version=version,
        name=_text(data.get("name"), 160) or f"CrMCDownload {version}",
        url=url,
        body=_text(data.get("notes") or data.get("body"), 200_000),
        published_at=_text(data.get("published_at"), 40),
        prerelease=_text(data.get("channel"), 16) == "beta",
        assets=assets,
        download_base=_text(data.get("download_base"), 500),
    )
    release_entries = data.get("releases")
    history: list[ReleaseInfo] = [latest]
    if isinstance(release_entries, list):
        for entry in release_entries:
            if not isinstance(entry, dict):
                continue
            tag_of = _text(entry.get("tag") or entry.get("tag_name"), 64)
            if not tag_of:
                continue
            if tag_version(tag_of) == version:
                continue
            history.append(
                ReleaseInfo(
                    tag=tag_of,
                    version=tag_version(tag_of),
                    name=_text(entry.get("name"), 160),
                    url=_text(entry.get("html_url") or entry.get("url"), 500),
                    body=_text(entry.get("body"), 200_000),
                    published_at=_text(entry.get("published_at"), 40),
                    prerelease=bool(entry.get("prerelease")),
                )
            )
    return ReleaseSnapshot(
        latest=latest,
        min_version=normalize(data.get("min_version")),
        releases=history[:_MAX_HISTORY],
        source="upstream",
    )