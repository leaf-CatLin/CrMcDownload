"""Server List Ping：用标准库 socket 读取服务器状态。

协议（Java 版 Status 阶段，``protocol=-1`` 表示不关心具体版本）：

1. ``Handshake``：``varint packetId(0x00) + varint protocol(-1) +
   varint string serverID + u16 port + varint nextState(1)``
2. ``Status Request``：``varint packetId(0x00)``，**不带任何字段**
3. 服务器回 ``varint 帧长 + varint packetId(0x00) + varint strlen + JSON``

注意不能再把 ``Login Start``（``0x00 + 名字``）当状态请求发：Vanilla 会忽略多余
字节，但 Velocity / BungeeCord 等严格实现会直接断开连接（表现为「连接被对方
关闭」或一直挂到超时）。响应也必须先跳过 packetId 与字符串长度前缀再解析 JSON。

无鉴权、不加密，因此离线账户也能探测在线模式服务器。``players.sample`` 是否
存在由服务器决定（1.19+ 开启 ``enforce-secure-profile`` 或装了隐藏插件的服务器
会省略），拿不到时按「仅显示人数」降级。
"""

from __future__ import annotations

import base64
import json
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any

from offcraft.servers.ssrf import DEFAULT_PORT, ServerAddressError, parse_server, resolve_is_public

__all__ = [
    "MAX_JSON_BYTES",
    "PlayerEntry",
    "ProbeResult",
    "encode_varint",
    "ping_server",
    "read_varint",
    "slp_probe",
    "strip_color_codes",
]

#: JSON Response 上限：原版实现限制在 32767 字节附近，这里再放宽一点
MAX_JSON_BYTES = 512 * 1024
_COLOR = re.compile(r"§[0-9a-fk-orA-FK-OR]")
_HEX_COLOR = re.compile(r"&[0-9a-fk-orA-FK-OR]")


@dataclass(frozen=True, slots=True)
class PlayerEntry:
    """玩家列表条目（服务器愿意公开时才有）。"""

    name: str
    uuid: str = ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "PlayerEntry":
        return cls(
            name=str(data.get("name") or ""),
            uuid=str(data.get("id") or data.get("uuid") or ""),
        )


@dataclass(slots=True)
class ProbeResult:
    """一次探测的结果。``ok`` 为假时 ``error`` 是面向用户的中文理由。"""

    host: str
    port: int = DEFAULT_PORT
    ok: bool = False
    error: str = ""
    latency_ms: float | None = None
    motd: str = ""
    version_name: str = ""
    version_protocol: int | None = None
    online: int = 0
    max_players: int = 0
    players: tuple[PlayerEntry, ...] = ()
    favicon: bytes | None = None
    brand: str = ""
    enforce_secure_profile: bool = False
    modified: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    checked_at: float = field(default_factory=time.time)

    @property
    def address(self) -> str:
        inner = f"[{self.host}]" if ":" in self.host else self.host
        return inner if self.port == DEFAULT_PORT else f"{inner}:{self.port}"

    @property
    def players_visible(self) -> bool:
        """服务器是否公开了玩家列表（很多服出于隐私会关闭）。"""
        return bool(self.players)

    @property
    def latency_level(self) -> str:
        """延迟分档：``green``(<100) / ``yellow``(<300) / ``red`` / ``unknown``。"""
        if self.latency_ms is None:
            return "unknown"
        if self.latency_ms < 100:
            return "green"
        if self.latency_ms < 300:
            return "yellow"
        return "red"

    def to_json(self) -> dict[str, Any]:
        """可写盘的紧凑形式（不含原始响应，favicon 转 base64）。"""
        return {
            "host": self.host,
            "port": self.port,
            "ok": self.ok,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "motd": self.motd,
            "version_name": self.version_name,
            "version_protocol": self.version_protocol,
            "online": self.online,
            "max_players": self.max_players,
            "players": [{"name": item.name, "uuid": item.uuid} for item in self.players],
            "favicon": base64.b64encode(self.favicon).decode("ascii") if self.favicon else "",
            "brand": self.brand,
            "enforce_secure_profile": self.enforce_secure_profile,
            "modified": self.modified,
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ProbeResult":
        favicon = str(data.get("favicon") or "")
        raw_icon: bytes | None = None
        if favicon:
            try:
                raw_icon = base64.b64decode(favicon.encode("ascii")) or None
            except Exception:  # noqa: BLE001 - 缓存里的坏数据不影响展示
                raw_icon = None
        players = tuple(
            PlayerEntry(str(item.get("name") or ""), str(item.get("uuid") or ""))
            for item in (data.get("players") or [])
            if isinstance(item, dict)
        )
        return cls(
            host=str(data.get("host") or ""),
            port=int(data.get("port") or DEFAULT_PORT),
            ok=bool(data.get("ok")),
            error=str(data.get("error") or ""),
            latency_ms=_as_float(data.get("latency_ms")),
            motd=str(data.get("motd") or ""),
            version_name=str(data.get("version_name") or ""),
            version_protocol=_as_int(data.get("version_protocol")),
            online=int(data.get("online") or 0),
            max_players=int(data.get("max_players") or 0),
            players=players,
            favicon=raw_icon,
            brand=str(data.get("brand") or ""),
            enforce_secure_profile=bool(data.get("enforce_secure_profile")),
            modified=bool(data.get("modified")),
            checked_at=float(data.get("checked_at") or 0.0),
        )


# --------------------------------------------------------------- 协议编解码
def encode_varint(value: int) -> bytes:
    """Netty VarInt：小端 7 位一组，高位续位，最多 5 字节（负数按无符号 32 位写）。"""
    if value < 0:
        value &= 0xFFFFFFFF
    out = bytearray()
    for index in range(5):
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            break
    return bytes(out)


def read_varint(read: "int") -> tuple[int, int]:
    """从 bytes 头部读一个 VarInt，返回 ``(值, 消耗字节数)``。

    ``read`` 也可以是 ``bytearray``/``memoryview``；数据不足时抛 ``ValueError``，
    超过 5 字节视为非法（防止恶意服务器把位移撑爆）。
    """
    data = bytes(read)
    value = 0
    for index in range(5):
        if index >= len(data):
            raise ValueError("VarInt 数据不完整")
        chunk = data[index]
        value |= (chunk & 0x7F) << (7 * index)
        if not chunk & 0x80:
            return value, index + 1
    raise ValueError("VarInt 超过 5 字节")


def _varint_string(text: str) -> bytes:
    payload = text.encode("utf-8")
    return encode_varint(len(payload)) + payload


STATUS_REQUEST = b"\x01\x00"  # length=1, packetId=0x00，不带字段


def handshake_packet(host: str, port: int) -> bytes:
    """Handshake 包：nextState=1 进入 Status 阶段。"""
    body = (
        encode_varint(0x00)
        + encode_varint(-1)
        + _varint_string(host)
        + int(port).to_bytes(2, "big")
        + encode_varint(1)
    )
    return encode_varint(len(body)) + body


def build_request(host: str, port: int, name: str = "OffCraft") -> bytes:
    """Handshake + Status Request，一次发出、省一个 RTT。

    ``name`` 仅为兼容旧调用保留，不再发送：状态阶段发 Login Start 会被严格
    服务端（Velocity / BungeeCord）判定为协议错误并断开。
    """
    return handshake_packet(host, port) + STATUS_REQUEST


def _read_exact(sock: socket.socket, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        piece = sock.recv(min(count - len(chunks), 65536))
        if not piece:
            raise OSError("连接被对方关闭")
        chunks.extend(piece)
    return bytes(chunks)


def _read_varint_from_socket(sock: socket.socket) -> int:
    value = 0
    for index in range(5):
        chunk = _read_exact(sock, 1)[0]
        value |= (chunk & 0x7F) << (7 * index)
        if not chunk & 0x80:
            return value
    raise ValueError("VarInt 超过 5 字节")


def _read_status_json(sock: socket.socket) -> bytes:
    """读一帧状态响应：``帧长 → packetId(0x00) → 字符串长度 → JSON``。"""
    length = _read_varint_from_socket(sock)
    if length <= 0:
        raise ValueError("响应为空")
    if length > MAX_JSON_BYTES:
        raise ValueError(f"响应过大（{length} 字节）")
    frame = _read_exact(sock, length)
    packet_id, offset = read_varint(frame)
    if packet_id != 0x00:
        raise ValueError(f"响应包 id 异常（0x{packet_id:02x}）")
    size, step = read_varint(frame[offset:])
    offset += step
    if size <= 0 or len(frame) < offset + size:
        raise ValueError("响应 JSON 长度不完整")
    return frame[offset : offset + size]


# ------------------------------------------------------------------- 解析
def strip_color_codes(text: str) -> str:
    """去掉 ``§``/``&`` 颜色与控制码，供界面展示。"""
    return _COLOR.sub("", _HEX_COLOR.sub("", str(text or "")))


def _chat_to_text(value: Any) -> str:
    """把聊天组件（字符串 / 对象 / 数组）递归降为纯文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_chat_to_text(item) for item in value)
    if isinstance(value, dict):
        text = str(value.get("text") or "")
        for key in ("extra", "with"):
            if value.get(key) is not None:
                text += _chat_to_text(value[key])
        return text
    return str(value)


def _parse_favicon(value: Any) -> bytes | None:
    text = str(value or "")
    if not text.startswith("data:image/"):
        return None
    _, _, payload = text.partition(",")
    if not payload:
        return None
    try:
        return base64.b64decode(payload.encode("ascii"))[: 2 * 1024 * 1024]
    except Exception:  # noqa: BLE001 - 图标坏掉不影响其他信息
        return None


def _brand_from_modified(data: Any) -> tuple[str, bool]:
    """``Modified Data Attempt`` 里常见的是 ``login_start:brand:xxx`` 形式。"""
    text = str(data or "")
    if not text:
        return "", False
    for part in reversed(text.split(":")):
        if part and len(part) <= 64 and part.isascii() and part.isprintable():
            return part, True
    return "", True


def parse_response(payload: dict[str, Any]) -> dict[str, Any]:
    """把 JSON Response 摊平成 :class:`ProbeResult` 需要的字段。"""
    version = payload.get("version") or {}
    players = payload.get("players") or {}
    sample = [
        PlayerEntry.from_json(item)
        for item in (players.get("sample") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    brand, modified = _brand_from_modified(payload.get("Modified Data Attempt"))
    return {
        "motd": strip_color_codes(_chat_to_text(payload.get("description"))).strip("\n"),
        "version_name": str(version.get("name") or ""),
        "version_protocol": _as_int(version.get("protocol")),
        "online": _as_int(players.get("online")) or 0,
        "max_players": _as_int(players.get("max")) or 0,
        "players": tuple(sample),
        "favicon": _parse_favicon(payload.get("favicon")),
        "brand": brand,
        "modified": modified,
        "enforce_secure_profile": bool(payload.get("enforceSecureProfile")),
    }


# ------------------------------------------------------------------- 探测
def slp_probe(
    host: str,
    port: int = DEFAULT_PORT,
    *,
    name: str = "OffCraft",
    timeout: float = 5.0,
    sock: socket.socket | None = None,
) -> ProbeResult:
    """对已知 ``(host, port)`` 发起 SLP 探测，不做地址校验（调用方负责）。"""
    started = time.perf_counter()
    own_socket = sock is None
    infos: list = []
    if sock is None:
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            return _failed(host, port, f"无法解析服务器地址：{host}（{exc.strerror or exc}）")
        if not infos:
            return _failed(host, port, f"无法解析服务器地址：{host}")
        family, socktype, proto, _, _ = infos[0]
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout)
    try:
        if infos:
            # 用 getaddrinfo 给的 sockaddr（IPv6 需要 flowinfo/scope_id 四个字段）
            sock.connect(infos[0][4])
        else:
            sock.connect((host, port))
        sock.sendall(build_request(host, port, name))
        payload = _read_status_json(sock)
        latency_ms = (time.perf_counter() - started) * 1000.0
        try:
            data = json.loads(payload.decode("utf-8", "replace"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _failed(host, port, "服务器响应不是有效 JSON，可能不是 Minecraft 服务器", started=started)
        if not isinstance(data, dict) or "version" not in data:
            return _failed(host, port, "响应缺少 version 字段，可能不是 Minecraft 服务器", started=started)
        result = ProbeResult(host=host, port=port, ok=True, latency_ms=round(latency_ms, 1), raw=data)
        for key, value in parse_response(data).items():
            setattr(result, key, value)
        return result
    except socket.timeout:
        return _failed(host, port, "连接超时", started=started)
    except OSError as exc:
        return _failed(host, port, f"无法连接：{exc.strerror or exc}", started=started)
    except ValueError as exc:
        return _failed(host, port, f"不是 Minecraft 服务器（{exc}）", started=started)
    finally:
        if own_socket and sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def ping_server(
    text: str,
    *,
    name: str = "OffCraft",
    timeout: float = 5.0,
    allow_local: bool = False,
) -> ProbeResult:
    """面向界面的入口：解析地址 → 安全护栏 → SLP 探测。

    ``allow_local=True``（配置项 ``allow_local_servers``）时才允许内网/本机地址。
    """
    try:
        host, port = parse_server(text)
    except ServerAddressError as exc:
        return ProbeResult(host=str(text or ""), port=DEFAULT_PORT, ok=False, error=str(exc))
    if not allow_local:
        allowed, reason = resolve_is_public(host)
        if not allowed:
            return ProbeResult(host=host, port=port, ok=False, error=reason)
    return slp_probe(host, port, name=name, timeout=timeout)


def _failed(host: str, port: int, error: str, *, started: float | None = None) -> ProbeResult:
    latency = round((time.perf_counter() - started) * 1000.0, 1) if started is not None else None
    return ProbeResult(host=host, port=port, ok=False, error=error, latency_ms=latency)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
