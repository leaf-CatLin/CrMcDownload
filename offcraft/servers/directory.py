"""镜像服务端的联机榜单客户端。

只与 ``download_base`` 通信：``/api/servers/*`` 是服务端自有的 JSON 接口，
不做 URL 改写（不属于代理路由），因此这里直接拼 ``{base}/api/...``。
写接口（上报服务器、举报服务器）才需要令牌，令牌按 :class:`Downloader` 的 origin 规则发送。

隐私约定：本地收藏/最近访问只是本机数据，**不会自动上传**；
只有用户在界面上手动确认「上报」或「举报」后才会调用这里的方法。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from offcraft import __version__
from offcraft.net.downloader import DownloadError, Downloader
from offcraft.servers.ping import PlayerEntry
from offcraft.servers.ssrf import DEFAULT_PORT, ServerAddressError, parse_server
from offcraft.servers.store import ServerEntry

__all__ = ["DirectoryError", "ServerListing", "ServerDirectory", "MAX_LIMIT"]

#: 单页最多拉多少条（与服务端上限一致）
MAX_LIMIT = 50


class DirectoryError(RuntimeError):
    """榜单请求失败。"""

    def __init__(self, message: str, *, offline: bool = False) -> None:
        super().__init__(message)
        #: 服务端没有该接口 / 不可达时置真：界面据此只显示本地收藏
        self.offline = offline


@dataclass(slots=True)
class ServerListing:
    """榜单条目 + 服务端最近一次探测结果。"""

    host: str
    port: int = DEFAULT_PORT
    label: str = ""
    source: str = "api"
    reports: int = 0
    ok: bool = False
    error: str = ""
    latency_ms: float | None = None
    motd: str = ""
    version_name: str = ""
    online: int = 0
    max_players: int = 0
    players: tuple[PlayerEntry, ...] = ()
    players_visible: bool = False
    updated_at: float = 0.0
    hot: float = 0.0
    #: 自己发布的服务器是否在榜（下架后本地仍可见，便于重新上架）
    visible: bool = True

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def entry(self) -> ServerEntry:
        return ServerEntry(host=self.host, port=self.port, label=self.label)

    @property
    def address(self) -> str:
        inner = f"[{self.host}]" if ":" in self.host else self.host
        return inner if self.port == DEFAULT_PORT else f"{inner}:{self.port}"

    @property
    def name(self) -> str:
        return self.label or self.address

    @property
    def latency_level(self) -> str:
        if self.latency_ms is None:
            return "unknown"
        if self.latency_ms < 100:
            return "green"
        if self.latency_ms < 300:
            return "yellow"
        return "red"

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ServerListing":
        players = tuple(
            PlayerEntry.from_json(item)
            for item in (data.get("players") or [])
            if isinstance(item, dict)
        )
        return cls(
            host=str(data.get("host") or "").lower(),
            port=int(data.get("port") or DEFAULT_PORT),
            label=str(data.get("label") or ""),
            source=str(data.get("source") or "api"),
            reports=int(data.get("reports") or 0),
            ok=bool(data.get("ok")),
            error=str(data.get("error") or ""),
            latency_ms=float(data["latency_ms"]) if data.get("latency_ms") is not None else None,
            motd=str(data.get("motd") or ""),
            version_name=str(data.get("version") or ""),
            online=int(data.get("online") or 0),
            max_players=int(data.get("max") or 0),
            players=players,
            players_visible=bool(data.get("players_visible")),
            updated_at=float(data.get("updated_at") or 0.0),
            hot=float(data.get("hot") or 0.0),
            visible=bool(data.get("enabled", True)),
        )


@dataclass(slots=True)
class ServerDirectory:
    """``/api/servers`` 的薄封装。"""

    base: str
    downloader: Downloader | None = None
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.base = str(self.base or "").strip().rstrip("/")

    @property
    def available(self) -> bool:
        return bool(self.base)

    def _url(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = f"{self.base}/api/{path.lstrip('/')}"
        if params:
            clean = {key: value for key, value in params.items() if value not in (None, "")}
            if clean:
                url += "?" + urlencode(sorted(clean.items()))
        return url

    def list(self, query: str = "", *, limit: int = 20, offset: int = 0) -> tuple[list[ServerListing], int]:
        """热度榜（``query`` 匹配地址 / 备注 / MOTD）。返回 ``(条目, 总数)``。"""
        data = self._get(
            "servers/list",
            {"q": query, "limit": max(1, min(MAX_LIMIT, int(limit))), "offset": max(0, int(offset))},
        )
        hits = [ServerListing.from_json(item) for item in (data.get("hits") or []) if isinstance(item, dict)]
        return hits, int(data.get("total") or len(hits))

    def detail(self, host: str, port: int = DEFAULT_PORT) -> dict[str, Any]:
        """单个服务器详情（含历史延迟曲线）。"""
        return self._get("servers/detail", {"host": host, "port": int(port)})

    def report(self, text: str, label: str = "", owner: str = "") -> dict[str, Any]:
        """把服务器上报到公共榜单；仅供用户手动确认后调用。

        ``owner`` 是本机生成的管理密钥：服务端只存它的哈希，之后凭它修改 /
        上下架 / 删除这条记录。返回服务端结果（含 ``owned``）。
        """
        if not self.base:
            raise DirectoryError("未配置下载源，无法上报服务器")
        try:
            host, port = parse_server(text)
        except ServerAddressError as exc:
            raise DirectoryError(str(exc)) from exc
        body: dict[str, Any] = {"host": host, "port": port, "label": label or ""}
        if owner:
            body["owner"] = owner
        return self._post("servers/report", body, "上报失败")

    def update(self, host: str, port: int, label: str, owner: str) -> dict[str, Any]:
        """修改自己上报的服务器备注名。"""
        return self._post(
            "servers/update",
            {"host": host, "port": int(port), "label": label or "", "owner": owner or ""},
            "修改失败",
        )

    def set_visible(self, host: str, port: int, visible: bool, owner: str) -> dict[str, Any]:
        """上架 / 下架自己上报的服务器。"""
        return self._post(
            "servers/visibility",
            {"host": host, "port": int(port), "visible": bool(visible), "owner": owner or ""},
            "上下架失败",
        )

    def delete(self, host: str, port: int, owner: str) -> dict[str, Any]:
        """删除自己上报的服务器。"""
        return self._post(
            "servers/delete",
            {"host": host, "port": int(port), "owner": owner or ""},
            "删除失败",
        )

    def _post(self, path: str, body: dict[str, Any], prefix: str) -> dict[str, Any]:
        if not self.base:
            raise DirectoryError("未配置下载源，无法提交请求")
        downloader = self.downloader
        if downloader is None:
            raise DirectoryError("下载器不可用")
        try:
            data = downloader.request_json(self._url(path), "POST", body=body)
        except DownloadError as exc:
            raise DirectoryError(f"{prefix}：{exc}") from exc
        return data if isinstance(data, dict) else {}

    def complain(
        self,
        host: str,
        port: int,
        reason: str,
        detail: str = "",
        contact: str = "",
    ) -> None:
        """举报一台服务器（用户手动提交，服务端人工审核，不会公开身份）。"""
        if not self.base:
            raise DirectoryError("未配置下载源，无法提交举报")
        downloader = self.downloader
        if downloader is None:
            raise DirectoryError("下载器不可用")
        try:
            downloader.request_json(
                self._url("servers/complaints"),
                "POST",
                body={
                    "host": str(host or "").strip(),
                    "port": int(port),
                    "reason": str(reason or "").strip()[:24],
                    "detail": str(detail or "").strip()[:500],
                    "contact": str(contact or "").strip()[:120],
                    "client": __version__,
                },
            )
        except DownloadError as exc:
            raise DirectoryError(f"举报提交失败：{exc}") from exc

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.base:
            raise DirectoryError("未配置下载源，无法获取联机榜单", offline=True)
        downloader = self.downloader
        if downloader is None:
            raise DirectoryError("下载器不可用", offline=True)
        url = self._url(path, params)
        try:
            data = downloader.fetch_json(url)
        except DownloadError as exc:
            text = str(exc)
            if "404" in text or "400" in text:
                raise DirectoryError(
                    "当前下载源还没有联机接口，只显示本地收藏", offline=True
                ) from exc
            raise DirectoryError(f"联机榜单获取失败：{exc}", offline=True) from exc
        if not isinstance(data, dict):
            raise DirectoryError("联机榜单返回格式异常", offline=True)
        if data.get("error"):
            raise DirectoryError(f"联机榜单：{data['error']}", offline=True)
        return data

    def status(self) -> dict[str, Any]:
        """榜单可用性（供状态栏与自检）。"""
        try:
            return self._get("servers/status", {})
        except DirectoryError:
            return {}


def dumps(value: Any) -> str:  # pragma: no cover - 调试辅助
    return json.dumps(value, ensure_ascii=False, indent=2)
