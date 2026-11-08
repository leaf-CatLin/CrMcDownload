"""``/api`` + ``/skin`` + ``/page`` 分流：把联机榜单、统计看板与皮肤接到 HTTP 层。

约定（与 ``proxy.py`` 协商）：本路由只在 ``_dispatch`` 里按前缀被调用，返回
``(status, headers_dict, body_bytes)``，由 handler 统一写回。读接口免令牌，
写接口（``POST /api/servers/report``、``POST /api/servers/complaints``、
``POST /skin/*``）在没有配置令牌时放行、配置了就必须带 ``X-OffCraft-Token``。
响应统一 UTF-8 JSON，读接口带 15s 短缓存。

``POST /api/servers/complaints`` 是用户手动举报服务器的唯一入口：客户端不会
自动上报用户添加的服务器，只有用户点「举报服务器」时才提交；服务端只做入库
（``server_complaints``），供管理端审核，不改变榜单状态。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote

from server_mirror.db import Database
from server_mirror.default_skin import DEFAULT_SKIN_NAME, default_skin_bytes
from server_mirror.skins import SkinError, SkinStore
from server_mirror.ssrf_guard import ServerAddressError, parse_host_port
from server_mirror.update import request_version, update_info, version_gate

__all__ = ["ApiRouter"]

_READ_CACHE = {"Cache-Control": "public, max-age=15"}


def _json(status: int, payload: Any, *, extra_headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    headers.update(extra_headers or {})
    return status, headers, body


def _err(status: int, message: str) -> tuple[int, dict[str, str], bytes]:
    return _json(status, {"error": message})


@dataclass(slots=True)
class ApiRouter:
    """聚合各子系统的只读/写入接口。"""

    settings: Any
    db: Database
    skins: SkinStore | None = None
    page: Any = None                       # StaticHandler
    p2p: Any = None                        # SignalingRouter（离线联机信令）
    metrics_snapshot: Callable[[], dict] | None = None
    cache_stats: Callable[[], dict] | None = None
    probe_status: Callable[[], dict] | None = None
    _report_buckets: dict[str, list[float]] = field(default_factory=dict)

    # -------------------------------------------------------------- 可用性
    def handles(self, path: str) -> bool:
        clean = path.split("?", 1)[0]
        return (
            clean.startswith("/api/")
            or clean.startswith("/skin/")
            or clean.startswith("/p2p/")
            or clean.startswith("/plugin/")
            or clean == "/update"
            or clean == "/"
            or clean == "/page"
            or clean.startswith("/page/")
        )

    def _authorized(self, headers: dict[str, str]) -> bool:
        token = getattr(self.settings, "token", "") or ""
        if not token:
            return True
        got = headers.get("x-offcraft-token") or headers.get("X-OffCraft-Token") or ""
        return got == token

    # -------------------------------------------------------------- 分发
    def dispatch(
        self, method: str, target: str, headers: dict[str, str], body: bytes | None = None,
        client: Any = None,
    ) -> tuple[int, dict[str, str], bytes]:
        raw_path, _, query = target.partition("?")
        path = unquote(raw_path)
        params = {k: v[0] for k, v in parse_qs(query, keep_blank_values=False).items()}
        if path == "/":
            return 302, {"Location": "/page/", "Cache-Control": "no-store"}, b""
        if path == "/page" or path.startswith("/page/"):
            return self._serve_page(path)
        if path == "/update":
            return self._serve_update()
        if path.startswith("/skin/"):
            return self._serve_skin(method, path, params, headers, body)
        if path.startswith("/p2p/"):
            return self._serve_p2p(method, path, headers, body, client)
        if path.startswith("/plugin/"):
            return self._serve_plugin(method, path)
        if path.startswith("/api/"):
            return self._serve_api(method, path, params, headers, body)
        return _err(404, "未知接口")

    def _serve_page(self, path: str):
        if self.page is None or not getattr(self.page, "available", False):
            return _err(404, "看板未构建")
        return self.page.serve(path)

    def _serve_update(self):
        """``GET /update`` → 301 跳到 GitHub Releases 页面（下载按钮统一入口）。"""
        info = update_info(self.settings)
        return 301, {
            "Location": info.page_url,
            "Cache-Control": "no-store",
            "X-OffCraft-Latest-Version": info.latest,
        }, b""

    # --------------------------------------------------------------- /p2p
    def _serve_p2p(self, method: str, path: str, headers: dict[str, str], body, client):
        """离线联机控制面：POST-only，凭证独立于全局下载令牌（靠 proof 准入）。"""
        if self.p2p is None:
            return _err(404, "离线联机未启用")
        if method != "POST":
            return _err(405, "方法不允许")
        payload = _decode_json(body)
        if payload is None:
            return _err(400, "请求体不是合法 JSON")
        status, data = self.p2p.handle(path, payload, client)
        return _json(status, data)

    # --------------------------------------------------------------- /plugin
    _PLUGIN_FILES = {
        "offline-multiplayer.manifest.json": ("application/json; charset=utf-8", "public, max-age=60"),
        "offline-multiplayer.zip": ("application/zip", "public, max-age=60"),
    }

    def _serve_plugin(self, method: str, path: str):
        """官方特权插件按需下载：GET-only，免令牌，文件来自 ``plugin_dir``。"""
        if method not in ("GET", "HEAD"):
            return _err(405, "方法不允许")
        tail = path[len("/plugin/"):]
        if ".." in tail or "/" in tail:
            return _err(400, "非法插件路径")
        meta = self._PLUGIN_FILES.get(tail)
        if meta is None:
            return _err(404, "未知插件")
        plugin_dir = getattr(self.settings, "plugin_dir", None)
        if not plugin_dir:
            return _err(404, "插件下载未配置")
        file_path = Path(plugin_dir) / tail
        if not file_path.is_file():
            return _err(404, "插件不存在")
        data = file_path.read_bytes()
        ctype, cache = meta
        return 200, {"Content-Type": ctype, "Cache-Control": cache}, data

    # --------------------------------------------------------------- /api
    def _serve_api(self, method: str, path: str, params: dict[str, str], headers: dict[str, str], body):
        if path == "/api/servers/complaints":
            if method == "POST":
                return self._complaint(body, headers)
            return _err(405, "方法不允许")
        if method not in ("GET", "HEAD"):
            if path == "/api/servers/report" and method == "POST":
                return self._report(body, headers)
            return _err(405, "方法不允许")
        if path == "/api/update":
            # 更新查询本身永远放行，老客户端靠它拿到强制更新信息
            return self._update(params, headers)
        query = "&".join(f"{k}={v}" for k, v in params.items())
        blocked = version_gate(self.settings, headers, query)
        if blocked is not None:
            return blocked
        if path == "/api/servers/list":
            return self._list(params)
        if path == "/api/servers/detail":
            return self._detail(params)
        if path == "/api/servers/status":
            return _json(200, self._servers_status(), extra_headers=_READ_CACHE)
        if path == "/api/skins/status":
            payload = self.skins.status() if self.skins else {}
            return _json(200, payload, extra_headers=_READ_CACHE)
        if path == "/api/skins/list":
            if self.skins is None:
                return _err(404, "皮肤服务未启用")
            limit = _clamp(params.get("limit"), 1, 200, 100)
            items = self.skins.list(query=str(params.get("q") or "")[:32], limit=limit)
            return _json(200, {"items": items, "count": len(items)}, extra_headers=_READ_CACHE)
        if path.startswith("/api/stats/"):
            return self._stats(path.rsplit("/", 1)[1])
        return _err(404, "未知接口")

    def _update(self, params: dict[str, str], headers: dict[str, str]):
        """``GET /api/update``：返回最新/最低版本、强制更新标记与下载地址。"""
        info = update_info(self.settings)
        client = request_version(headers) or str(params.get("v") or params.get("version") or "")
        client = client.strip().lstrip("vV")[:32]
        return _json(200, info.payload(client), extra_headers={"Cache-Control": "no-store"})

    def _list(self, params: dict[str, str]):
        q = str(params.get("q") or "")[:64]
        limit = _clamp(params.get("limit"), 1, 50, 20)
        offset = _clamp(params.get("offset"), 0, 1_000_000, 0)
        rows, total = self.db.rank(q, limit=limit, offset=offset)
        return _json(
            200,
            {"total": total, "limit": limit, "offset": offset, "hits": [_listing(r) for r in rows]},
            extra_headers=_READ_CACHE,
        )

    def _detail(self, params: dict[str, str]):
        host = str(params.get("host") or "")
        if not host:
            return _err(400, "缺少 host")
        try:
            host, port = parse_host_port(f"{host}:{params.get('port') or 25565}")
        except ServerAddressError as exc:
            return _err(400, str(exc))
        item = self.db.detail(host, port)
        if item is None:
            return _err(404, "没有该服务器的记录")
        return _json(200, _listing(item, detail=True), extra_headers=_READ_CACHE)

    def _report(self, body, headers: dict[str, str]):
        if not self._authorized(headers):
            return _err(403, "需要访问令牌")
        payload = _decode_json(body)
        if payload is None:
            return _err(400, "请求体不是合法 JSON")
        host = str(payload.get("host") or "")
        try:
            host, port = parse_host_port(f"{host}:{payload.get('port') or 25565}")
        except ServerAddressError as exc:
            return _err(400, str(exc))
        label = str(payload.get("label") or "")[:40]
        if not self._allow_report(host):
            return _err(429, "上报过于频繁，请稍后再试")
        self.db.report(host, port, label=label)
        return _json(200, {"ok": True})

    def _complaint(self, body, headers: dict[str, str]):
        """``POST /api/servers/complaints``：用户手动举报服务器，只入库待审核。"""
        if not self._authorized(headers):
            return _err(403, "需要访问令牌")
        payload = _decode_json(body)
        if payload is None:
            return _err(400, "请求体不是合法 JSON")
        host = str(payload.get("host") or "")
        try:
            host, port = parse_host_port(f"{host}:{payload.get('port') or 25565}")
        except ServerAddressError as exc:
            return _err(400, str(exc))
        reason = str(payload.get("reason") or "").strip()[:24]
        if not reason:
            return _err(400, "举报原因不能为空，请填写原因")
        detail = str(payload.get("detail") or "").strip()[:500]
        contact = str(payload.get("contact") or "").strip()[:120]
        client = str(payload.get("client") or "").strip()[:32]
        if not self._allow_report(host):
            return _err(429, "举报过于频繁，请稍后再试")
        complaint_id = self.db.add_complaint(
            host, port, reason, detail=detail, contact=contact, client=client
        )
        return _json(200, {"ok": True, "id": complaint_id})

    def _allow_report(self, host: str) -> bool:
        """按主机限流：令牌桶，每秒补 1、容量 10。"""
        import time

        now = time.time()
        bucket = self._report_buckets.get(host, [10.0, now])
        tokens = min(10.0, bucket[0] + (now - bucket[1]))
        if tokens < 1:
            self._report_buckets[host] = [tokens, now]
            return False
        self._report_buckets[host] = [tokens - 1, now]
        return True

    def _servers_status(self):
        status = {"servers": self.db.overview()}
        if self.probe_status is not None:
            status["probe"] = self.probe_status()
        return status

    def _stats(self, name: str):
        if name == "overview":
            snap = self.metrics_snapshot() if self.metrics_snapshot else {}
            return _json(200, {"metrics": snap, "servers": self.db.overview()}, extra_headers=_READ_CACHE)
        if name == "cache":
            return _json(200, self.cache_stats() if self.cache_stats else {}, extra_headers=_READ_CACHE)
        if name == "download":
            snap = self.metrics_snapshot() if self.metrics_snapshot else {}
            return _json(200, {"metrics": snap}, extra_headers=_READ_CACHE)
        if name == "servers":
            return _json(200, self.db.overview(), extra_headers=_READ_CACHE)
        return _err(404, "未知统计项")

    # -------------------------------------------------------------- /skin
    def _serve_skin(self, method: str, path: str, params: dict[str, str], headers: dict[str, str], body):
        if self.skins is None:
            return _err(404, "皮肤服务未启用")
        try:
            name = _skin_name(path)
        except SkinError as exc:
            return _err(400, str(exc))
        model = str(params.get("model") or "").lower()
        slim = model == "slim" or name.endswith("_slim")
        auto_model = model in ("", "auto")
        # 内置默认皮肤：任何账户缺图时都能拿到一张可用的贴图（?fallback=1）。
        base_name = name[:-5] if name.endswith("_slim") else name
        if base_name == DEFAULT_SKIN_NAME:
            data = default_skin_bytes("slim" if slim else "classic")
            return 200, {"Content-Type": "image/png", "Cache-Control": "public, max-age=86400"}, data
        if method in ("GET", "HEAD"):
            data = self.skins.read(name, slim=slim)
            if data is None and auto_model:
                # 没指定模型时 classic/slim 都试一次，客户端「自动判定」可直接取回。
                data = self.skins.read(name, slim=True)
            if data is None and params.get("fallback") in ("1", "true", "on"):
                data = default_skin_bytes("slim" if slim else "classic")
                return 200, {
                    "Content-Type": "image/png",
                    "Cache-Control": "public, max-age=300",
                    "X-Skin-Fallback": "default",
                }, data
            if data is None:
                return _err(404, "没有该皮肤")
            return 200, {"Content-Type": "image/png", "Cache-Control": "public, max-age=300"}, data
        if method == "POST":
            if not self._authorized(headers):
                return _err(403, "需要访问令牌")
            try:
                used, width, height = self.skins.write(name, body or b"", model=model or "auto")
            except SkinError as exc:
                return _err(400, str(exc))
            return _json(200, {"ok": True, "model": used, "width": width, "height": height})
        if method == "DELETE":
            if not self._authorized(headers):
                return _err(403, "需要访问令牌")
            removed = self.skins.delete(name)
            return _json(200, {"ok": True, "removed": removed})
        return _err(405, "方法不允许")


# ---------------------------------------------------------------- 工具
def _skin_name(path: str) -> str:
    tail = path[len("/skin/"):]
    if tail.endswith(".png"):
        tail = tail[: -len(".png")]
    if "/" in tail or ".." in tail:
        raise SkinError("非法皮肤路径")
    return tail


def _listing(item: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    players = item.get("players") or []
    out = {
        "host": item["host"],
        "port": item["port"],
        "label": item.get("label") or "",
        "source": item.get("source") or "api",
        "reports": int(item.get("reports") or 0),
        "ok": bool(item.get("ok")),
        "error": item.get("error") or "",
        "latency_ms": item.get("latency_ms"),
        "motd": item.get("motd") or "",
        "version": item.get("version") or "",
        "online": int(item.get("online") or 0),
        "max": int(item.get("players_max") or 0),
        "players": players,
        "players_visible": bool(players),
        "updated_at": float(item.get("updated_at") or 0.0),
        "hot": round(float(item.get("hot") or 0.0), 3),
    }
    if detail:
        out["history"] = item.get("history") or []
    return out


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _decode_json(body: bytes | None) -> dict[str, Any] | None:
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None
