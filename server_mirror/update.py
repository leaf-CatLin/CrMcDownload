"""版本与更新：最新版本自动检测、最低版本门槛、301 跳转与旧版本拒绝服务。

规则：

- **最新版本来自 GitHub Releases**（``server_mirror.releases``，服务端缓存），
  不再由 ``MIRROR_UPDATE_LATEST_VERSION`` 决定；``.env`` 里的版本号只在
  「GitHub 不可达且没有快照」时作为兜底，正常部署不需要再改它（见 ``auto``）；
- 最低版本 ``MIRROR_UPDATE_MIN_VERSION``：显式写死，或设 ``auto`` 自动取
  「当前次版本的上一档」（``0.9.1`` → ``0.8.0``）；
- 版本号来自 ``X-OffCraft-Version`` 请求头（兼容 ``?v=`` 查询串），可带 ``v`` 前缀；
- 只有配置了最低版本且请求明确低于它时才拒绝（426）；
- 未携带版本号的请求放行，保证 curl / 看板 / 旧客户端仍可用；
- ``GET /update`` 返回 301，跳到 GitHub Releases 页面（按检测到的 tag）；
- 客户端安装包由 ``/github/...`` 路由代理，慢线路时可作为镜像下载源。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

from server_mirror.releases import (
    ReleaseInfo,
    ReleaseSnapshot,
    ReleaseProvider,
    asset_url,
    mirror_url,
)
from server_mirror.versions import is_outdated, normalize, tag_version  # noqa: F401  (re-export)

__all__ = [
    "DEFAULT_REPO",
    "FALLBACK_LATEST",
    "UpdateInfo",
    "is_outdated",
    "releases_payload",
    "request_version",
    "update_info",
    "version_gate",
    "version_key",
]

from server_mirror.versions import version_key  # noqa: E402  (向后兼容的再导出)

DEFAULT_REPO = "leaf-CatLin/CrMcDownload"
#: 引导用的兜底版本：仅当 GitHub 不可达且快照文件不存在时才使用。
#: 正常部署下最新版本由 GitHub Releases 自动检测，不需要维护这个常量。
FALLBACK_LATEST = "0.9.1"
#: ``/api/update`` 里附带的更新摘要长度（完整正文走 ``/api/releases``）
_NOTES_LIMIT = 600


def request_version(headers: dict[str, str], query: str = "") -> str:
    """从请求头或查询串取版本号（头优先），截断到 32 字符。"""
    raw = ""
    for name, value in (headers or {}).items():
        if name.lower() == "x-offcraft-version":
            raw = str(value or "")
            break
    if not raw and query:
        params = parse_qs(query, keep_blank_values=False)
        raw = str((params.get("v") or params.get("version") or [""])[0])
    return normalize(raw)


# ------------------------------------------------------------------ 快照 → 配置


def _state_of(settings: Any) -> tuple[ReleaseSnapshot | None, float]:
    """取快照与其 TTL（没有 provider 时返回 ``(None, 0)``）。"""
    provider = getattr(settings, "releases_provider", None)
    if provider is None:
        return None, 0.0
    try:
        return provider.snapshot(), float(getattr(provider, "ttl", 0.0) or 0.0)
    except Exception:  # noqa: BLE001 - 快照异常不能拖垮版本接口
        return None, 0.0


def _snapshot_of(settings: Any) -> ReleaseSnapshot | None:
    return _state_of(settings)[0]


def _settings_min(settings: Any, snapshot: ReleaseSnapshot | None) -> str:
    """最低版本：快照里的结论优先，其次 ``MIRROR_UPDATE_MIN_VERSION``。"""
    if snapshot is not None and snapshot.min_version:
        return snapshot.min_version
    return normalize(getattr(settings, "update_min", ""))


def _release_of(snapshot: ReleaseSnapshot | None, version: str) -> ReleaseInfo | None:
    if snapshot is None:
        return None
    if snapshot.latest is not None and (not version or snapshot.latest.version == version):
        return snapshot.latest
    for item in snapshot.releases:
        if item.version == version:
            return item
    return snapshot.latest if not version else None


def _asset_hint(settings: Any, version: str) -> tuple[str, ...]:
    template = str(getattr(settings, "update_asset", "") or "").strip()
    if not template or not version:
        return ()
    try:
        return (template.format(version=version, tag=f"v{version}"),)
    except (KeyError, ValueError):
        return (template,)


@dataclass(slots=True)
class UpdateInfo:
    """一次版本通告的完整上下文（最新版本、资产、门槛与来源）。"""

    repo: str = DEFAULT_REPO
    latest: str = FALLBACK_LATEST
    minimum: str = ""
    enforce: bool = True
    release_url: str = ""
    asset: str = ""
    tag: str = ""
    #: 该发布的全部资产（来自 GitHub；为空时按命名规则拼一个）
    assets: list[dict[str, Any]] = field(default_factory=list)
    asset_size: int | None = None
    notes: str = ""
    published_at: str = ""
    channel: str = "stable"
    download_base: str = ""
    #: ``github``（自动检测）/ ``file``（快照文件）/ ``fallback``（环境变量兜底）
    source: str = "env"
    fetched_at: float = 0.0
    stale: bool = False

    # ------------------------------------------------------------ 派生地址
    @property
    def tag_name(self) -> str:
        return self.tag or (f"v{self.latest}" if self.latest else "")

    @property
    def page_url(self) -> str:
        if self.release_url:
            return self.release_url
        if not self.latest:
            return f"https://github.com/{self.repo}/releases"
        return f"https://github.com/{self.repo}/releases/tag/{self.tag_name}"

    @property
    def asset_name(self) -> str:
        """主下载项文件名：GitHub 资产优先，其次模板，最后按命名规则拼。"""
        for item in self.assets:
            name = str(item.get("name") or "")
            if not name:
                continue
            lowered = name.lower()
            if any(hint in lowered for hint in ("setup", "install", "-win64.exe", ".msi")):
                return name
        if self.assets:
            name = str(self.assets[0].get("name") or "")
            if name:
                return name
        if self.asset:
            try:
                return self.asset.format(version=self.latest, tag=self.tag_name)
            except (KeyError, ValueError):
                return self.asset
        return f"CrMCDownload-{self.latest}-win64-setup.exe"

    @property
    def direct_url(self) -> str:
        for item in self.assets:
            if str(item.get("name") or "") != self.asset_name:
                continue
            url = str(item.get("url") or "")
            # api.github.com 的资产地址需要特别的 Accept 头，浏览器/客户端用不了，
            # 这种情况按 tag + 文件名拼标准下载地址。
            if url.startswith("https://github.com/"):
                return url
        return asset_url(self.repo, self.tag_name, self.asset_name)

    @property
    def mirror_download_url(self) -> str:
        """同源镜像地址（由 ``/github/`` 代理路由提供，支持 Range 断点续传）。"""
        return mirror_url(self.repo, self.tag_name, self.asset_name)

    # 旧名保留（0.7.0 起叫 mirror_url），避免外部脚本引用断裂
    @property
    def mirror_url(self) -> str:
        return self.mirror_download_url

    # ------------------------------------------------------------ 判定与负载
    def outdated(self, client: str | None) -> bool:
        if not self.enforce or not self.minimum:
            return False
        return is_outdated(client, self.minimum)

    def payload(self, client: str = "") -> dict[str, Any]:
        forced = self.outdated(client)
        asset = {
            "name": self.asset_name,
            "size": self.asset_size,
            "url": self.direct_url,
            "mirror_url": self.mirror_download_url,
            "downloads": next(
                (item.get("downloads") for item in self.assets if item.get("name") == self.asset_name),
                None,
            ),
        }
        payload: dict[str, Any] = {
            "ok": not forced,
            "client_version": client,
            "latest_version": self.latest,
            "min_version": self.minimum,
            "force_update": forced,
            "release_url": self.page_url,
            "download_url": self.direct_url,
            "mirror_download_url": self.mirror_download_url,
            "asset": asset,
            # 自动检测相关（老客户端忽略未知字段）
            "tag": self.tag_name,
            "published_at": self.published_at,
            "channel": self.channel,
            "notes": self.notes,
            "assets": self.assets,
            "version_source": self.source,
            "version_checked_at": self.fetched_at,
            "version_stale": self.stale,
        }
        if forced:
            payload["error"] = "客户端版本过低，请更新后再使用"
        return payload


def update_info(settings: Any, *, snapshot: ReleaseSnapshot | None = None) -> UpdateInfo:
    """从 :class:`Settings` 生成版本通告（也可直接传 :class:`UpdateInfo`）。

    最新版本优先级：GitHub 快照 > ``MIRROR_UPDATE_LATEST_VERSION`` > 内置兜底。
    传 ``snapshot``（如 ``ReleaseProvider.current()``）时不重复读配置里的快照。
    """
    if isinstance(settings, UpdateInfo):
        return settings
    if snapshot is None:
        snapshot, ttl = _state_of(settings)
    else:
        provider = getattr(settings, "releases_provider", None)
        ttl = float(getattr(provider, "ttl", 0.0) or 0.0)

    repo = str(getattr(settings, "update_repo", "") or DEFAULT_REPO).strip().strip("/")
    env_latest = normalize(getattr(settings, "update_latest", ""))
    version = (snapshot.version if snapshot is not None else "") or env_latest or FALLBACK_LATEST
    release = _release_of(snapshot, version)
    hints = _asset_hint(settings, version)

    assets = release.asset_dicts() if release is not None else []
    asset_size = None
    primary = release.pick_asset(hints) if release is not None else None
    if primary is not None:
        asset_size = primary.size
    if primary is not None and primary.name:
        assets = [dict(item) for item in assets]
        for item in assets:
            if item.get("name") == primary.name and item.get("size") is not None:
                asset_size = item.get("size")

    tag = (release.tag if release is not None else "") or (f"v{version}" if version else "")
    notes = (release.body or "") if release is not None else ""
    if len(notes) > _NOTES_LIMIT:
        notes = notes[:_NOTES_LIMIT].rstrip() + "…"
    release_url = str(getattr(settings, "update_release_url", "") or "").strip()
    if not release_url and release is not None:
        release_url = release.url or ""

    return UpdateInfo(
        repo=repo,
        latest=version,
        minimum=_settings_min(settings, snapshot),
        enforce=bool(getattr(settings, "update_enforce", True)),
        release_url=release_url,
        asset=str(getattr(settings, "update_asset", "") or "").strip(),
        tag=tag,
        assets=assets,
        asset_size=asset_size,
        notes=notes,
        published_at=(release.published_at if release is not None else ""),
        channel=(release.channel if release is not None else "stable"),
        download_base=(release.download_base if release is not None else ""),
        source=(snapshot.source if snapshot is not None else "env") or "env",
        fetched_at=(snapshot.fetched_at if snapshot is not None else 0.0),
        stale=bool(snapshot is not None and (ttl <= 0 or not snapshot.is_fresh(ttl))),
    )


def releases_payload(
    settings: Any,
    *,
    provider: ReleaseProvider | None = None,
    limit: int = 5,
    body_limit: int = 4000,
    snapshot: ReleaseSnapshot | None = None,
) -> dict[str, Any]:
    """``/api/releases`` 的负载：更新日志 + 当前版本 + 检测状态。"""
    if provider is not None:
        if snapshot is None:
            snapshot = provider.snapshot()
        ttl = float(getattr(provider, "ttl", 0.0) or 0.0)
    else:
        snapshot, ttl = _state_of(settings)
    info = update_info(settings, snapshot=snapshot)
    release_limit = max(0, min(20, int(limit)))
    releases: list[dict[str, Any]] = []
    if snapshot is not None:
        for item in snapshot.releases[:release_limit]:
            detail = item.as_detail()
            if body_limit > 0 and len(detail["body"]) > body_limit:
                detail["body"] = detail["body"][:body_limit].rstrip() + "…"
            releases.append(detail)
    return {
        "ok": True,
        "repo": info.repo,
        "latest_version": info.latest,
        "min_version": info.minimum,
        "release_url": info.page_url,
        "download_url": info.direct_url,
        "mirror_download_url": info.mirror_download_url,
        "asset": {"name": info.asset_name, "size": info.asset_size},
        "assets": info.assets,
        "releases": releases,
        "check": None if snapshot is None else snapshot.as_dict(
            ttl=ttl, stale=bool(ttl <= 0 or not snapshot.is_fresh(ttl))
        ),
    }


def version_gate(
    settings: Any, headers: dict[str, str], query: str = "", *, snapshot: ReleaseSnapshot | None = None
) -> tuple[int, dict[str, str], bytes] | None:
    """版本门槛：低于最低版本返回 426；放行时返回 ``None``。"""
    info = update_info(settings, snapshot=snapshot)
    if not info.enforce or not info.minimum:
        return None
    client = request_version(headers, query)
    if not client or not info.outdated(client):
        return None
    body = json.dumps(info.payload(client), ensure_ascii=False).encode("utf-8")
    return 426, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        "X-OffCraft-Min-Version": info.minimum,
        "X-OffCraft-Latest-Version": info.latest,
        "X-OffCraft-Force-Update": "1",
    }, body