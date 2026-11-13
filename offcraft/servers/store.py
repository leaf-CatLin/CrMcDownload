"""本地服务器收藏、最近访问与探测缓存。

收藏与最近访问写进 ``config.json``（跟着配置一起备份、F12 窗口可见），
探测结果单独写 ``servers.json``：里面可能含服务器返回的 MOTD 与 favicon，
体积大且会不断变化，不适合塞进主配置。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from offcraft.servers.ping import ProbeResult
from offcraft.servers.ssrf import DEFAULT_PORT, ServerAddressError, format_host_port, parse_server

__all__ = ["CACHE_TTL", "ServerEntry", "ServerStore", "key_of"]

#: 探测缓存有效期（秒）：列表首屏先渲染缓存，超过这个时间才重新探测
CACHE_TTL = 300.0
#: 最近访问保留条数
RECENT_MAX = 12


def key_of(host: str, port: int = DEFAULT_PORT) -> str:
    """缓存与去重用的键（小写主机 + 端口）。"""
    return f"{str(host or '').strip().lower()}:{int(port or DEFAULT_PORT)}"


@dataclass(frozen=True, slots=True)
class ServerEntry:
    """一条用户添加的服务器（地址 + 可选备注名）。"""

    host: str
    port: int = DEFAULT_PORT
    label: str = ""
    added_at: float = 0.0

    @property
    def key(self) -> str:
        return key_of(self.host, self.port)

    @property
    def address(self) -> str:
        return format_host_port(self.host, self.port)

    @property
    def name(self) -> str:
        return self.label or self.address

    @classmethod
    def parse(cls, text: str, label: str = "") -> "ServerEntry":
        """从 ``host[:port]`` 建条目；地址非法时抛 :class:`ServerAddressError`。"""
        host, port = parse_server(text)
        return cls(host=host, port=port, label=str(label or "").strip(), added_at=time.time())

    def to_json(self) -> dict[str, Any]:
        return {"host": self.host, "port": self.port, "label": self.label, "added_at": self.added_at}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ServerEntry":
        host = str(data.get("host") or "").strip().lower()
        text = str(data.get("address") or "") or host
        try:
            parsed_host, parsed_port = parse_server(text)
        except ServerAddressError:
            parsed_host, parsed_port = host or text, DEFAULT_PORT
        return cls(
            host=str(data.get("host") or parsed_host).strip().lower(),
            port=int(data.get("port") or parsed_port),
            label=str(data.get("label") or ""),
            added_at=float(data.get("added_at") or 0.0),
        )


@dataclass(slots=True)
class ServerStore:
    """收藏 / 最近访问 / 探测缓存的统一读写入口（线程安全、原子落盘）。"""

    data_dir: Path | str
    path: Path = field(init=False)
    entries: list[ServerEntry] = field(default_factory=list)
    recent: list[str] = field(default_factory=list)
    cache: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)
    #: 本机备注名（key → 名称），对公共榜单条目也生效；不上传服务端
    labels: dict[str, str] = field(default_factory=dict)
    #: 本机屏蔽名单（key），只在「热门」里隐藏，不影响收藏 / 最近访问
    blocked: list[str] = field(default_factory=list)
    #: 自己上报过的服务器（key → 记录），保存管理密钥用于修改 / 上下架 / 删除
    published: dict[str, dict[str, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _dirty: bool = False

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.path = self.data_dir / "servers.json"
        self.load()

    # ------------------------------------------------------------------ IO
    def load(self) -> None:
        """读盘；文件损坏时备份并回到空集合（与 ``config.py`` 同策略）。"""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            try:
                self.path.replace(self.path.with_name(f"servers.corrupt-{int(time.time())}.json"))
            except OSError:
                pass
            return
        if not isinstance(data, dict):
            return
        with self._lock:
            self.entries = [
                ServerEntry.from_json(item)
                for item in (data.get("servers") or [])
                if isinstance(item, dict)
            ]
            self.recent = [str(item) for item in (data.get("recent") or [])][:RECENT_MAX]
            self.labels = {
                str(key): str(value)[:40]
                for key, value in (data.get("labels") or {}).items()
                if isinstance(key, str) and isinstance(value, str) and value.strip()
            }
            self.blocked = [
                str(item) for item in (data.get("blocked") or []) if isinstance(item, str)
            ]
            self.published = {
                str(key): dict(value)
                for key, value in (data.get("published") or {}).items()
                if isinstance(key, str) and isinstance(value, dict)
            }
            raw_cache = data.get("cache") or {}
            self.cache = {
                key: (float(value.get("ts") or 0.0), dict(value.get("result") or {}))
                for key, value in raw_cache.items()
                if isinstance(value, dict)
            }

    def save(self) -> Path:
        """原子写（临时名 + ``os.replace``）。"""
        with self._lock:
            payload = {
                "servers": [item.to_json() for item in self.entries],
                "recent": list(self.recent),
                "labels": dict(self.labels),
                "blocked": list(self.blocked),
                "published": {key: dict(value) for key, value in self.published.items()},
                "cache": {key: {"ts": ts, "result": result} for key, (ts, result) in self.cache.items()},
            }
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name("servers.json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        return self.path

    # -------------------------------------------------------------- 收藏管理
    def list_entries(self) -> list[ServerEntry]:
        with self._lock:
            return list(self.entries)

    def find(self, text: str) -> ServerEntry | None:
        try:
            host, port = parse_server(text)
        except ServerAddressError:
            return None
        return self.get(host, port)

    def get(self, host: str, port: int = DEFAULT_PORT) -> ServerEntry | None:
        key = key_of(host, port)
        with self._lock:
            for item in self.entries:
                if item.key == key:
                    return item
        return None

    def add(self, text: str, label: str = "") -> ServerEntry:
        """添加收藏；已存在时只更新备注名。"""
        entry = ServerEntry.parse(text, label)
        with self._lock:
            for index, item in enumerate(self.entries):
                if item.key == entry.key:
                    if entry.label and entry.label != item.label:
                        self.entries[index] = ServerEntry(
                            host=item.host, port=item.port, label=entry.label, added_at=item.added_at
                        )
                    return self.entries[index]
            self.entries.append(entry)
        self.save()
        return entry

    def remove(self, host: str, port: int = DEFAULT_PORT) -> bool:
        key = key_of(host, port)
        with self._lock:
            before = len(self.entries)
            self.entries = [item for item in self.entries if item.key != key]
            self.recent = [item for item in self.recent if item != key]
            self.cache.pop(key, None)
        self.save()
        return len(self.entries) != before

    # ------------------------------------------------------------ 本机备注/屏蔽
    def set_label(self, host: str, port: int = DEFAULT_PORT, label: str = "") -> None:
        """设置本机备注名（空字符串表示清除）；不收藏、不上传。"""
        key = key_of(host, port)
        text = str(label or "").strip()[:40]
        with self._lock:
            if text:
                self.labels[key] = text
            else:
                self.labels.pop(key, None)
            for index, item in enumerate(self.entries):
                if item.key == key:
                    self.entries[index] = ServerEntry(
                        host=item.host, port=item.port, label=text, added_at=item.added_at
                    )
        self.save()

    def label_for(self, host: str, port: int = DEFAULT_PORT) -> str:
        with self._lock:
            return self.labels.get(key_of(host, port), "")

    def block(self, host: str, port: int = DEFAULT_PORT) -> None:
        """把服务器加入本机屏蔽名单（只在热门榜隐藏）。"""
        key = key_of(host, port)
        with self._lock:
            if key not in self.blocked:
                self.blocked.append(key)
        self.save()

    def unblock(self, host: str, port: int = DEFAULT_PORT) -> None:
        key = key_of(host, port)
        with self._lock:
            self.blocked = [item for item in self.blocked if item != key]
        self.save()

    def is_blocked(self, host: str, port: int = DEFAULT_PORT) -> bool:
        with self._lock:
            return key_of(host, port) in self.blocked

    def list_blocked(self) -> list[ServerEntry]:
        with self._lock:
            keys = list(self.blocked)
        out: list[ServerEntry] = []
        for key in keys:
            host, _, port_text = key.rpartition(":")
            if host:
                out.append(
                    ServerEntry(
                        host=host,
                        port=int(port_text or DEFAULT_PORT),
                        label=self.label_for(host, int(port_text or DEFAULT_PORT)),
                    )
                )
        return out

    # -------------------------------------------------------------- 我发布的
    def publish_record(
        self, host: str, port: int, label: str, owner: str, *, visible: bool = True
    ) -> dict[str, Any]:
        """记录自己上报的服务器与管理密钥（密钥只存本机，用于后续管理）。"""
        key = key_of(host, port)
        record = {
            "host": str(host).lower(),
            "port": int(port),
            "label": str(label or "").strip()[:40],
            "owner": str(owner or ""),
            "visible": bool(visible),
            "added_at": time.time(),
        }
        with self._lock:
            self.published[key] = record
        self.save()
        return record

    def published_for(self, host: str, port: int = DEFAULT_PORT) -> dict[str, Any] | None:
        with self._lock:
            item = self.published.get(key_of(host, port))
            return dict(item) if item else None

    def set_published_visible(self, host: str, port: int = DEFAULT_PORT, visible: bool = True) -> None:
        key = key_of(host, port)
        with self._lock:
            item = self.published.get(key)
            if item is not None:
                item["visible"] = bool(visible)
        self.save()

    def drop_published(self, host: str, port: int = DEFAULT_PORT) -> None:
        key = key_of(host, port)
        with self._lock:
            self.published.pop(key, None)
        self.save()

    def list_published(self) -> list[dict[str, Any]]:
        with self._lock:
            items = [dict(value) for value in self.published.values()]
        items.sort(key=lambda item: float(item.get("added_at") or 0.0), reverse=True)
        return items

    # -------------------------------------------------------------- 最近访问
    def touch_recent(self, host: str, port: int = DEFAULT_PORT) -> None:
        key = key_of(host, port)
        changed = False
        with self._lock:
            if key in self.recent:
                self.recent.remove(key)
            changed = True
            self.recent.insert(0, key)
            self.recent = self.recent[:RECENT_MAX]
        if changed:
            self.save()

    def list_recent(self) -> list[ServerEntry]:
        """最近访问条目（含已被取消收藏的：地址直接从缓存键还原）。"""
        with self._lock:
            keys = list(self.recent)
            known = {item.key: item for item in self.entries}
        out: list[ServerEntry] = []
        for key in keys:
            entry = known.get(key)
            if entry is None:
                host, _, port_text = key.rpartition(":")
                if not host:
                    continue
                entry = ServerEntry(host=host, port=int(port_text or DEFAULT_PORT))
            out.append(entry)
        return out

    # ---------------------------------------------------------------- 缓存
    def cached(self, host: str, port: int = DEFAULT_PORT, *, ttl: float = CACHE_TTL) -> ProbeResult | None:
        """未过期的缓存结果；过期或没有时返回 ``None``。"""
        key = key_of(host, port)
        with self._lock:
            item = self.cache.get(key)
        if not item:
            return None
        ts, result = item
        if ttl > 0 and time.time() - ts > ttl:
            return None
        try:
            return ProbeResult.from_json(result)
        except (TypeError, ValueError):
            return None

    def remember(self, result: ProbeResult) -> None:
        key = key_of(result.host, result.port)
        with self._lock:
            self.cache[key] = (time.time(), result.to_json())
            # 缓存最多留 64 条，按时间淘汰最旧的
            if len(self.cache) > 64:
                for old in sorted(self.cache, key=lambda k: self.cache[k][0])[: len(self.cache) - 64]:
                    self.cache.pop(old, None)
        self.save()

    def forget(self, host: str, port: int = DEFAULT_PORT) -> None:
        with self._lock:
            self.cache.pop(key_of(host, port), None)
        self.save()

    def merge(self, extra: Iterable[ServerEntry]) -> list[ServerEntry]:
        """把服务端榜单条目并进本地列表（不写收藏，只用于展示）。"""
        seen = {item.key for item in self.entries}
        out = list(self.entries)
        for item in extra:
            if item.key in seen:
                continue
            seen.add(item.key)
            out.append(item)
        return out
