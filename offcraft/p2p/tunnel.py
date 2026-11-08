"""离线联机隧道：本地 TCP <-> 端到端加密通道的双向泵送，以及房主/加入者两个角色。

Phase 1 走「经服务端中继」：

- 加入者 B（:class:`JoinTunnel`）在 ``127.0.0.1`` 起临时监听，游戏 ``localhost:port``
  连上后，B 申请中继令牌、连中继，把这条本地连接灌进加密通道。
- 房主 A（:class:`HostTunnel`）轮询 announce 领取待配对令牌，连中继后把它桥接到本机
  已「对局域网开放」的 MC 服务端口。

中继只见密文；加解密在两端的 :class:`SecureChannel` 完成。
"""

from __future__ import annotations

import select
import socket
import threading
from typing import Callable

from .crypto import make_channel, P2PError
from .room import RoomClient
from .transport import RelayTransport, SecureChannel

__all__ = ["forward", "JoinTunnel", "HostTunnel"]

_READ = 65536
_POLL = 0.25


def _shutdown(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def forward(local: socket.socket, channel: SecureChannel,
            cancel: threading.Event | None = None) -> None:
    """把本地套接字与远端加密通道对接，任一方向结束即拆除双方。

    两个泵送线程只读自己的源、写自己的目的，不跨线程关闭共享套接字（Windows 上
    对另一线程正阻塞的 socket 调 shutdown/close 可能死锁）。拆除由主线程统一
    完成；两读侧均走 ``select`` 轮询超时，因此无论 EOF、对端关闭还是套接字被拆，
    都会在 ~一个轮询周期内干净退出。阻塞直到连接关闭 / 取消 / 出错。
    """
    stop = threading.Event()

    def ended() -> bool:
        return stop.is_set() or (cancel is not None and cancel.is_set())

    def local_to_channel() -> None:
        try:
            while not ended():
                ready, _, _ = select.select([local], [], [], _POLL)
                if not ready:
                    continue
                data = local.recv(_READ)
                if not data:
                    break
                channel.send(data)
        except OSError:
            pass
        finally:
            stop.set()

    def channel_to_local() -> None:
        try:
            while not ended():
                data = channel.recv()
                if data is None:
                    break
                local.sendall(data)
        except (OSError, P2PError):
            pass
        finally:
            stop.set()

    t1 = threading.Thread(target=local_to_channel, name="p2p-fwd-l2c", daemon=True)
    t2 = threading.Thread(target=channel_to_local, name="p2p-fwd-c2l", daemon=True)
    t1.start()
    t2.start()
    while t1.is_alive() and t2.is_alive():
        if cancel is not None and cancel.is_set():
            stop.set()
        t1.join(_POLL)
        t2.join(_POLL)
    stop.set()
    if cancel is not None:
        cancel.set()
    # 主线程统一拆除：先停本地端再停加密通道（不 shutdown，避免与读线程死锁）。
    try:
        local.close()
    except OSError:
        pass
    channel.close()
    t1.join(2.0)
    t2.join(2.0)


class JoinTunnel:
    """加入者侧：本地监听 → 每条游戏连接对应一次中继配对到房主。"""

    def __init__(self, room: RoomClient, host_sid: str,
                 cancel: threading.Event | None = None) -> None:
        self._room = room
        self._host_sid = host_sid
        self._cancel = cancel or threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port: int = self._listener.getsockname()[1]
        self._thread: threading.Thread | None = None

    def serve_forever(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="p2p-join-tunnel", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._cancel.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,),
                             name="p2p-join-conn", daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            info = self._room.request_relay(self._host_sid)
            token = str(info.get("token") or "")
            relay = info.get("relay") or self._room.relay
            host = str(relay.get("host") or "")
            port = int(relay.get("port") or 0)
            if not token or not host or port <= 0:
                conn.close()
                return
            sock, reader = RelayTransport.connect(host, port, token)
            channel = SecureChannel(sock, make_channel(self._room.keys, is_host=False), reader)
            forward(conn, channel, self._cancel)
        except (OSError, P2PError):
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._cancel.set()
        _shutdown(self._listener)
        try:
            self._listener.close()
        except OSError:
            pass


class HostTunnel:
    """房主侧：轮询 announce 领取中继令牌，桥接到本机开放的 MC 端口。"""

    def __init__(self, room: RoomClient, mc_port: int, *, mc_host: str = "127.0.0.1",
                 interval: float = 2.0, cancel: threading.Event | None = None) -> None:
        self._room = room
        self._mc_host = mc_host
        self._mc_port = mc_port
        self._interval = max(0.5, float(interval))
        self._cancel = cancel or threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, on_event: Callable[[str], None] | None = None) -> None:
        self._thread = threading.Thread(
            target=self.run, args=(on_event,), name="p2p-host-tunnel", daemon=True
        )
        self._thread.start()

    def run(self, on_event: Callable[[str], None] | None = None) -> None:
        """轮询 announce；``on_event('expired'|'error'|'ok')`` 只在状态变化时回调一次。

        房间被删除/过期（HTTP 404）时停止轮询，交给面板提示用户重新开放，避免空转。
        """
        state: str | None = None

        def report(status: str) -> None:
            nonlocal state
            if status != state:
                state = status
                if on_event is not None:
                    try:
                        on_event(status)
                    except Exception:  # noqa: BLE001 - 回调异常不影响轮询
                        pass

        while not self._cancel.is_set():
            try:
                resp = self._room.announce(world=self._room.world, port=self._mc_port)
            except Exception as exc:  # noqa: BLE001 - 分类后决定重试或退出
                text = str(exc)
                if "HTTP 404" in text:
                    report("expired")
                    self._cancel.set()
                    return
                report("error")
                self._cancel.wait(self._interval)
                continue
            report("ok")
            token = str(resp.get("relay_token") or "")
            if token:
                threading.Thread(target=self._serve, args=(token,),
                                 name="p2p-host-conn", daemon=True).start()
            self._cancel.wait(self._interval)

    def _serve(self, token: str) -> None:
        relay = self._room.relay
        host = str(relay.get("host") or "")
        port = int(relay.get("port") or 0)
        if not host or port <= 0:
            return
        try:
            sock, reader = RelayTransport.connect(host, port, token)
        except (OSError, P2PError):
            return
        try:
            channel = SecureChannel(sock, make_channel(self._room.keys, is_host=True), reader)
            mc = socket.create_connection((self._mc_host, self._mc_port), timeout=15)
            forward(mc, channel, self._cancel)
        except (OSError, P2PError):
            try:
                sock.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._cancel.set()

    def close(self) -> None:
        self.stop()
