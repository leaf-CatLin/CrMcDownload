"""vpnd（C# 数据面守护进程）的本地 IPC 客户端。

职责边界：Python 负责信令、房间密钥与 UI；vpnd 负责 Wintun 网卡与 UDP 数据面。
这里只做「命名管道 JSON Lines」通信：握手 token、下发配置（VIP + 每个对端的单向密钥
与观测端点）、查询状态、降级判断。

vpnd 不存在（未安装 / token 文件缺失 / 管道连不上）时 :func:`try_create` 返回 ``None``，
调用方据此回落现有 ``JoinTunnel`` 端口映射模式——虚拟局域网是增强路径，不是硬依赖。
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import io
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .crypto import KEY_BYTES, RoomKeys

__all__ = [
    "PIPE_NAME",
    "STATE_DIR",
    "VpnClient",
    "VpnClientError",
    "VpnPeerStatus",
    "VpnStatus",
    "default_state_dir",
    "default_token_path",
    "derive_peer_keys",
    "build_config",
    "try_create",
]

#: vpnd 监听的命名管道（与 packaging/vpn/Ipc.cs 保持一致）
PIPE_NAME = "\\\\.\\pipe\\offcraft-vpnd"
_TOKEN_FILE = "ipc_token.txt"
_HKDF_INFO_HOST_TO_PEER = "offcraft-vpn|host2peer"
_HKDF_INFO_PEER_TO_HOST = "offcraft-vpn|peer2host"


class VpnClientError(RuntimeError):
    """IPC 通信失败 / vpnd 返回错误 / 配置不合法。"""


def default_state_dir() -> Path:
    """vpnd 状态目录：``%ProgramData%\\CrMcDownload\\vpn``。"""
    program_data = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    return Path(program_data) / "CrMcDownload" / "vpn"


STATE_DIR = default_state_dir()


def default_token_path() -> Path:
    return default_state_dir() / _TOKEN_FILE


# ------------------------------------------------------------------ 密钥派生
def _hkdf_sha256(ikm: bytes, salt: bytes, info: str, length: int = KEY_BYTES) -> bytes:
    """HKDF-SHA256（与 packaging/vpn/Crypto.cs 的 Hkdf.Derive 逐字节一致）。"""
    if not ikm:
        raise VpnClientError("HKDF 输入密钥不能为空")
    prk = hmac.new(salt or b"\x00" * 32, ikm, hashlib.sha256).digest()
    okm = b""
    previous = b""
    counter = 1
    while len(okm) < length:
        previous = hmac.new(prk, previous + info.encode("utf-8") + bytes([counter]), hashlib.sha256).digest()
        okm += previous
        counter += 1
    return okm[:length]


def derive_peer_keys(
    keys: RoomKeys, *, is_host: bool, self_sid: str, peer_sid: str,
) -> tuple[bytes, bytes]:
    """为一对「本端 - 对端」派生单向密钥 ``(tx_key, rx_key)``。

    HKDF salt 取两端 sid **排序后**的 SHA-256，保证房主与加入者各自算出的结果一致
    （同一房间内每个对端的 salt 不同，密钥互不复用）。房主发 ``key_ab`` 收 ``key_ba``，
    加入者反之，双方 tx/rx 天然对齐。
    """
    own = str(self_sid or "")
    other = str(peer_sid or "")
    if not own or not other:
        raise VpnClientError("缺少会话 sid，无法派生密钥")
    first, second = sorted([own, other])
    salt = hashlib.sha256(f"offcraft-vpn-salt|{first}|{second}".encode("utf-8")).digest()
    host_to_peer = _hkdf_sha256(keys.key_ab, salt, _HKDF_INFO_HOST_TO_PEER)
    peer_to_host = _hkdf_sha256(keys.key_ba, salt, _HKDF_INFO_PEER_TO_HOST)
    if is_host:
        return host_to_peer, peer_to_host
    return peer_to_host, host_to_peer


def build_config(
    *,
    vip: str,
    peers: Iterable[dict[str, Any]],
    adapter: str = "CrMcDownload VPN",
    listen: int = 0,
    use_adapter: bool = True,
    edge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组装下发 vpnd 的配置字典（JSON 可直序列化）。

    ``peers`` 每项需含 ``sid`` / ``vip`` 与 ``tx_key`` / ``rx_key``（bytes）；``endpoint``
    可空——未登记 UDP 端点的对端直接走边缘中继（vpnd 见到空端点会跳过打洞）。
    密钥在这里转 hex，vpnd 端不再派生，避免两套实现漂移。
    """
    if not vip:
        raise VpnClientError("缺少本端虚拟 IP")
    items: list[dict[str, Any]] = []
    for peer in peers:
        sid = str(peer.get("sid") or "")
        endpoint = str(peer.get("endpoint") or "")
        tx_key = peer.get("tx_key") or b""
        rx_key = peer.get("rx_key") or b""
        if not sid:
            continue
        if len(tx_key) != KEY_BYTES or len(rx_key) != KEY_BYTES:
            raise VpnClientError(f"对端 {sid[:8]} 的会话密钥长度非法")
        items.append({
            "sid": sid,
            "vip": str(peer.get("vip") or ""),
            "endpoint": endpoint,
            "tx_key": tx_key.hex(),
            "rx_key": rx_key.hex(),
        })
    config: dict[str, Any] = {
        "vip": vip,
        "adapter": adapter,
        "listen": int(listen),
        "use_adapter": bool(use_adapter),
        "peers": items,
    }
    if edge:
        config["edge"] = {
            "host": str(edge.get("host") or ""),
            "port": int(edge.get("port") or 0),
            "token": str(edge.get("token") or ""),
        }
    return config


# ------------------------------------------------------------------ 状态模型
@dataclass(slots=True)
class VpnPeerStatus:
    sid: str
    vip: str = ""
    endpoint: str = ""
    path: str = "idle"          # idle / direct / relay（M3/M4 才会有 relay）
    tx: int = 0
    rx: int = 0
    last_rx: int = 0            # 距上次收到合法包的秒数（0 表示尚未收到）
    rtt_ms: int = 0             # PUNCH/KEEPALIVE 往返时延（毫秒）

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> "VpnPeerStatus":
        return cls(
            sid=str(item.get("sid") or ""),
            vip=str(item.get("vip") or ""),
            endpoint=str(item.get("endpoint") or ""),
            path=str(item.get("path") or "idle"),
            tx=int(item.get("tx") or 0),
            rx=int(item.get("rx") or 0),
            last_rx=int(item.get("last_rx") or 0),
            rtt_ms=int(item.get("rtt_ms") or 0),
        )


@dataclass(slots=True)
class VpnStatus:
    running: bool = False
    state: str = "stopped"
    vip: str = ""
    adapter: bool = False
    adapter_name: str = ""
    udp_port: int = 0
    last_error: str = ""
    uptime: int = 0
    peers: list[VpnPeerStatus] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VpnStatus":
        return cls(
            running=bool(data.get("running")),
            state=str(data.get("state") or "stopped"),
            vip=str(data.get("vip") or ""),
            adapter=bool(data.get("adapter")),
            adapter_name=str(data.get("adapter_name") or ""),
            udp_port=int(data.get("udp_port") or 0),
            last_error=str(data.get("last_error") or ""),
            uptime=int(data.get("uptime") or 0),
            peers=[VpnPeerStatus.from_dict(p) for p in (data.get("peers") or [])],
        )


# ------------------------------------------------------------------ IPC 客户端
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_INVALID_HANDLE = ctypes.c_void_p(-1).value


def _create_file_pipe(path: str) -> int:
    """Win32 ``CreateFileW`` 打开管道，返回原始句柄（失败抛 OSError）。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    create.restype = ctypes.c_void_p
    handle = create(path, _GENERIC_READ | _GENERIC_WRITE, 0, None, _OPEN_EXISTING, 0, None)
    if handle is None or handle == _INVALID_HANDLE:
        error = ctypes.get_last_error()
        raise OSError(0, f"CreateFile 失败（Win32={error}）", path)
    return int(handle)


class _WinPipeStream:
    """基于 Win32 ``ReadFile`` / ``WriteFile`` 的管道文本流（不经过 CRT fd）。"""

    def __init__(self, handle: int) -> None:
        self._handle = handle
        self._buffer = b""
        self._closed = False
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.ReadFile.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
        ]
        self._kernel32.WriteFile.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
        ]
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    def write(self, text: str) -> None:
        data = text.encode("utf-8")
        raw = ctypes.create_string_buffer(data, len(data))
        written = ctypes.c_uint32(0)
        ok = self._kernel32.WriteFile(
            ctypes.c_void_p(self._handle), raw, len(data), ctypes.byref(written), None,
        )
        if not ok or written.value != len(data):
            raise OSError(0, f"WriteFile 失败（Win32={ctypes.get_last_error()}）")

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        while b"\n" not in self._buffer:
            chunk = self._read_chunk()
            if not chunk:
                break
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line.decode("utf-8", errors="replace")

    def _read_chunk(self) -> bytes:
        buf = ctypes.create_string_buffer(4096)
        read = ctypes.c_uint32(0)
        ok = self._kernel32.ReadFile(
            ctypes.c_void_p(self._handle), buf, 4096, ctypes.byref(read), None,
        )
        if not ok:
            error = ctypes.get_last_error()
            if error == 109:  # ERROR_BROKEN_PIPE
                return b""
            raise OSError(0, f"ReadFile 失败（Win32={error}）")
        return buf.raw[: read.value]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._kernel32.CloseHandle(ctypes.c_void_p(self._handle))

    @property
    def closed(self) -> bool:
        return self._closed


class VpnClient:
    """vpnd 命名管道客户端。一次实例对应一条连接，退出时关闭即可。"""

    def __init__(
        self,
        token_path: str | os.PathLike[str] | None = None,
        pipe_path: str = PIPE_NAME,
        *,
        timeout: float = 2.0,
        stream_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.token_path = Path(token_path) if token_path else default_token_path()
        self.pipe_path = pipe_path
        self.timeout = timeout
        self._stream_factory = stream_factory or self._open_pipe
        self._stream: Any = None

    # ---------------------------------------------------------- 连接管理
    def _open_pipe(self, path: str) -> Any:
        """打开命名管道。

        Python 内建 ``open()`` 走 CRT ``fopen``，无法打开 ``\\\\.\\pipe\\...``（报 Errno 2），
        这里直接用 Win32 ``CreateFileW`` 取句柄，再用 ``ReadFile`` / ``WriteFile`` 收发。
        """
        if os.name == "nt":
            return _WinPipeStream(_create_file_pipe(path))
        raw = open(path, "r+b", buffering=0)
        return io.TextIOWrapper(raw, encoding="utf-8", newline="\n", write_through=True)

    def token(self) -> str:
        try:
            return self.token_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            return ""

    def available(self) -> bool:
        """vpnd 是否可能可用：只看 token 文件（未安装时为假，避免无谓阻塞）。"""
        return len(self.token()) >= 16

    def connect(self) -> dict[str, Any]:
        token = self.token()
        if not token:
            raise VpnClientError("vpnd 未安装或尚未启动（缺少 IPC token）")
        if self._stream is None:
            try:
                self._stream = self._stream_factory(self.pipe_path)
            except OSError as exc:
                raise VpnClientError(f"连接 vpnd 失败：{exc}") from exc
            self._write(f"AUTH {token}")
            reply = self._read()
            if not reply.get("ok"):
                raise VpnClientError(f"vpnd 鉴权失败：{reply.get('error') or 'bad-token'}")
            return reply
        return {"ok": True}

    def close(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    def __enter__(self) -> "VpnClient":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---------------------------------------------------------- 协议收发
    def _write(self, line: str) -> None:
        if self._stream is None:
            raise VpnClientError("尚未连接 vpnd")
        try:
            self._stream.write(line + "\n")
            self._stream.flush()
        except OSError as exc:
            raise VpnClientError(f"写入 vpnd 失败：{exc}") from exc

    def _read(self) -> dict[str, Any]:
        if self._stream is None:
            raise VpnClientError("尚未连接 vpnd")
        try:
            line = self._stream.readline()
        except OSError as exc:
            raise VpnClientError(f"读取 vpnd 失败：{exc}") from exc
        if not line:
            raise VpnClientError("vpnd 连接已关闭")
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise VpnClientError(f"vpnd 返回非法 JSON：{line[:120]!r}") from exc
        if not isinstance(data, dict):
            raise VpnClientError("vpnd 返回的不是对象")
        return data

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """发送一条命令并读取一条回复；``ok`` 为假时抛 :class:`VpnClientError`。"""
        if self._stream is None:
            self.connect()
        self._write(json.dumps(payload, ensure_ascii=False))
        reply = self._read()
        if not reply.get("ok"):
            raise VpnClientError(str(reply.get("error") or "vpnd 命令失败"))
        return reply

    # ---------------------------------------------------------- 业务命令
    def apply_config(self, config: dict[str, Any]) -> dict[str, Any]:
        payload = {"cmd": "config"}
        payload.update(config)
        return self.request(payload)

    def update_edge(self, edge: dict[str, Any]) -> dict[str, Any]:
        """热更新边缘令牌/地址（不重启数据面，避免打断已建立的中继路径）。"""
        return self.request({
            "cmd": "edge",
            "host": str(edge.get("host") or ""),
            "port": int(edge.get("port") or 0),
            "token": str(edge.get("token") or ""),
        })

    def status(self) -> VpnStatus:
        return VpnStatus.from_dict(self.request({"cmd": "status"}))

    def stop(self) -> None:
        self.request({"cmd": "stop"})


def try_create(
    token_path: str | os.PathLike[str] | None = None,
    pipe_path: str = PIPE_NAME,
) -> VpnClient | None:
    """vpnd 不可用时返回 ``None``（调用方回落旧隧道）；可用时返回未连接的客户端。"""
    client = VpnClient(token_path=token_path, pipe_path=pipe_path)
    return client if client.available() else None
