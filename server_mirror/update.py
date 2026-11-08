"""版本与更新：最低/最新版本、301 跳转与旧版本拒绝服务。

规则：

- 版本号来自 ``X-OffCraft-Version`` 请求头（兼容 ``?v=`` 查询串），可带 ``v`` 前缀；
- 只有配置了 ``MIRROR_UPDATE_MIN_VERSION`` 且请求明确低于它时才拒绝（426）；
- 未携带版本号的请求放行，保证 curl / 看板 / 旧客户端仍可用；
- ``GET /update`` 返回 301，跳到 GitHub Releases 页面；
- 客户端安装包由 ``/github/...`` 路由代理，慢线路时可作为镜像下载源。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

__all__ = [
    "DEFAULT_REPO",
    "UpdateInfo",
    "is_outdated",
    "request_version",
    "update_info",
    "version_gate",
    "version_key",
]

DEFAULT_REPO = "leaf-CatLin/CrMcDownload"
#: 当前发布版本（与客户端 __version__、page/package.json 保持一致）
DEFAULT_LATEST = "0.8.5"

_VERSION = re.compile(r"^[0-9]+(\.[0-9]+){0,3}$")
_PRERELEASE_RANK = {"alpha": 0, "a": 0, "beta": 1, "b": 1, "rc": 2, "pre": 2}


def version_key(text: str | None) -> tuple[tuple[int, ...], tuple[int, tuple[int, ...]]] | None:
    """把版本号归一化成可比较的键；非法版本返回 ``None``。

    预发布（``0.7.0-beta.1``）排在正式版之前；``v`` 前缀与 ``+build`` 后缀忽略。
    """
    raw = str(text or "").strip().lstrip("vV")
    if not raw:
        return None
    raw = raw.partition("+")[0][:32]
    base, _, pre = raw.partition("-")
    if not _VERSION.match(base):
        return None
    numbers = [int(part) for part in base.split(".")]
    while len(numbers) < 3:
        numbers.append(0)
    if pre:
        chunks = pre.lower().split(".")
        rank = _PRERELEASE_RANK.get(chunks[0], 2)
        tail = tuple(int(chunk) for chunk in chunks[1:] if chunk.isdigit())
        return tuple(numbers), (rank, tail)
    return tuple(numbers), (3, ())


def is_outdated(client: str | None, minimum: str | None) -> bool:
    """客户端版本是否低于最低版本；任一方无法解析时视为不过期。"""
    left, right = version_key(client), version_key(minimum)
    if left is None or right is None:
        return False
    return left < right


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
    return raw.strip().lstrip("vV")[:32]


@dataclass(slots=True)
class UpdateInfo:
    """更新配置快照（全部可由环境变量注入）。"""

    repo: str = DEFAULT_REPO
    latest: str = DEFAULT_LATEST
    minimum: str = ""
    enforce: bool = True
    release_url: str = ""
    asset: str = ""

    @property
    def tag(self) -> str:
        return f"v{self.latest.lstrip('vV')}"

    @property
    def page_url(self) -> str:
        if self.release_url:
            return self.release_url
        return f"https://github.com/{self.repo}/releases/tag/{self.tag}"

    @property
    def asset_name(self) -> str:
        if self.asset:
            try:
                return self.asset.format(version=self.latest.lstrip("vV"))
            except (KeyError, ValueError):
                return self.asset
        return f"CrMCDownload-{self.latest.lstrip('vV')}-win64-setup.exe"

    @property
    def direct_url(self) -> str:
        return f"https://github.com/{self.repo}/releases/download/{self.tag}/{self.asset_name}"

    @property
    def mirror_url(self) -> str:
        """同源镜像地址（由 ``/github/`` 代理路由提供，支持 Range 断点续传）。"""
        return f"/github/{self.repo}/releases/download/{self.tag}/{self.asset_name}"

    def outdated(self, client: str | None) -> bool:
        if not self.enforce or not self.minimum:
            return False
        return is_outdated(client, self.minimum)

    def payload(self, client: str = "") -> dict[str, Any]:
        forced = self.outdated(client)
        payload: dict[str, Any] = {
            "ok": not forced,
            "client_version": client,
            "latest_version": self.latest,
            "min_version": self.minimum,
            "force_update": forced,
            "release_url": self.page_url,
            "download_url": self.direct_url,
            "mirror_download_url": self.mirror_url,
            "asset": {"name": self.asset_name, "size": None},
        }
        if forced:
            payload["error"] = "客户端版本过低，请更新后再使用"
        return payload


def update_info(settings: Any) -> UpdateInfo:
    """从 :class:`Settings` 生成更新配置（也可直接传 :class:`UpdateInfo`）。"""
    if isinstance(settings, UpdateInfo):
        return settings
    latest = str(getattr(settings, "update_latest", "") or DEFAULT_LATEST).strip()
    return UpdateInfo(
        repo=str(getattr(settings, "update_repo", "") or DEFAULT_REPO).strip().strip("/"),
        latest=latest.lstrip("vV") or DEFAULT_LATEST,
        minimum=str(getattr(settings, "update_min", "") or "").strip().lstrip("vV"),
        enforce=bool(getattr(settings, "update_enforce", True)),
        release_url=str(getattr(settings, "update_release_url", "") or "").strip(),
        asset=str(getattr(settings, "update_asset", "") or "").strip(),
    )


def version_gate(
    settings: Any, headers: dict[str, str], query: str = ""
) -> tuple[int, dict[str, str], bytes] | None:
    """版本门槛：低于最低版本返回 426；放行时返回 ``None``。"""
    info = update_info(settings)
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
