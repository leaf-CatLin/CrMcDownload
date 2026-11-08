"""离线联机信令客户端：走镜像 ``/p2p/*`` 创建/开放/加入房间，维护名册与中继配对。

所有信令请求都发向 ``download_base``（复用 :class:`Downloader.request_json` 的
令牌-origin 规则），但房间凭证（auth_key / proof）与全局下载令牌相互独立——准入
靠 proof，不依赖 ``X-OffCraft-Token``。房间用「全局唯一房间名 + 密码」标识；密码
只在本地派生密钥，绝不入库或上传。

生命周期（与服务端对齐）：
- :meth:`create`  专用入口登记一个新房间名（每设备配额，名字全局唯一）；
- :meth:`open`   房主开一个实时会话供加入（房间须已由本设备创建）；
- :meth:`join`   加入者凭密码派生的 proof 进入已开放会话（不存在/未开放即报错，绝不建房）；
- :meth:`list_rooms` / :meth:`delete` 查询/删除本设备创建过的房间。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .crypto import (
    canonical_name,
    derive_room_keys,
    make_proof,
    new_identity,
    RoomKeys,
)

__all__ = ["PeerInfo", "RoomClient"]


@dataclass(slots=True)
class PeerInfo:
    pid: str
    addr: str
    world: str = ""
    port: int = 0
    vip: str = ""       # 虚拟 IP（10.66.0.x）；服务端未启用 VPN 时为空
    udp: str = ""       # UDP 边缘登记的观测端点 "ip:port"；未登记为空

    @classmethod
    def from_roster(cls, item: dict[str, Any]) -> "PeerInfo":
        return cls(
            pid=str(item.get("pid") or ""),
            addr=str(item.get("addr") or ""),
            world=str(item.get("world") or ""),
            port=int(item.get("port") or 0),
            vip=str(item.get("vip") or ""),
            udp=str(item.get("udp") or ""),
        )


@dataclass(slots=True)
class RoomClient:
    downloader: Any
    mirror: Any
    name: str
    password: str
    device_id: str = ""
    world: str = ""
    port: int = 0
    keys: RoomKeys = field(init=False)
    pub: str = field(init=False, default="")
    nonce: str = field(init=False, default="")
    sid: str = field(init=False, default="")
    host_sid: str = field(init=False, default="")
    host_secret: str = field(init=False, default="")   # 仅房主 open 时下发；kick 鉴权用
    peers: list[PeerInfo] = field(init=False, default_factory=list)
    relay: dict[str, Any] = field(init=False, default_factory=dict)
    vip: str = field(init=False, default="")          # 本端虚拟 IP；服务端未启用 VPN 时为空
    host_vip: str = field(init=False, default="")     # 房主虚拟 IP（加入者据此直连）
    edge: dict[str, Any] = field(init=False, default_factory=dict)   # UDP 边缘 {host, port}
    edge_token: str = field(init=False, default="")   # 本端边缘令牌（名册轮询会刷新）

    def __post_init__(self) -> None:
        self.name = canonical_name(self.name)
        if not self.name:
            raise ValueError("房间名不能为空")
        if not self.password:
            raise ValueError("房间密码不能为空")
        self.keys = derive_room_keys(self.password, self.name)
        self.pub, self.nonce = new_identity()

    # -------------------------------------------------------------- 信令请求
    def _post(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.mirror.api_url(f"/p2p/{action}")
        data = self.downloader.request_json(url, "POST", body=payload)
        return data if isinstance(data, dict) else {}

    def _apply_join(self, resp: dict[str, Any]) -> dict[str, Any]:
        self.sid = str(resp.get("sid") or "")
        self.host_sid = str(resp.get("host") or "")
        self.relay = dict(resp.get("relay") or {})
        self.peers = [PeerInfo.from_roster(p) for p in (resp.get("peers") or [])]
        self._apply_vpn(resp)
        return resp

    def _apply_vpn(self, resp: dict[str, Any]) -> None:
        """吸收服务端下发的 VPN 字段（open/join/roster/announce 共用）。"""
        if resp.get("vip"):
            self.vip = str(resp["vip"])
        if resp.get("host_vip"):
            self.host_vip = str(resp["host_vip"])
        elif self.vip and not self.host_vip:
            self.host_vip = self.vip          # 房主本人的 vip 即 host_vip
        if resp.get("edge"):
            self.edge = dict(resp["edge"])
        if resp.get("edge_token"):
            self.edge_token = str(resp["edge_token"])

    # -------------------------------------------------------------- 房间管理
    def create(self) -> dict[str, Any]:
        """专用入口：登记一个新房间（全局唯一名 + 每设备配额）。"""
        return self._post("create", {"room": self.name, "device_id": self.device_id})

    def list_rooms(self) -> dict[str, Any]:
        """查询本设备创建过的房间与剩余额度。"""
        return self._post("list", {"device_id": self.device_id})

    def delete(self) -> dict[str, Any]:
        """删除本设备创建的一个房间（释放名额、名字可复用）。"""
        return self._post("delete", {"room": self.name, "device_id": self.device_id})

    # -------------------------------------------------------------- 会话
    def open(self) -> dict[str, Any]:
        """房主开一个实时会话：登记从密码派生的 auth_key（服务端仅用于校验他人 proof）。"""
        resp = self._post("open", {
            "room": self.name,
            "client_pub": self.pub,
            "auth_key": self.keys.auth_key.hex(),
            "world": self.world,
            "port": self.port,
            "device_id": self.device_id,
        })
        self.host_secret = str(resp.get("host_secret") or "")
        return self._apply_join(resp)

    def roster(self) -> dict[str, Any]:
        """拉取当前会话在线名册（只读，任何在房成员可查）；回写本地 peers。"""
        resp = self._post("roster", {"room": self.name, "sid": self.sid})
        self.peers = [PeerInfo.from_roster(p) for p in (resp.get("peers") or [])]
        self._apply_vpn(resp)
        return resp

    def kick(self, target_sid: str) -> dict[str, Any]:
        """房主踢出一个在线成员（靠 open 下发的 host_secret 鉴权）。仅房主会话可用。"""
        if not self.host_secret:
            raise ValueError("仅房主可踢出成员（缺少控制口令）。")
        return self._post("kick", {
            "room": self.name,
            "sid": self.sid,
            "host_secret": self.host_secret,
            "target": target_sid,
        })

    def rekey(self, new_password: str) -> dict[str, Any]:
        """房主重置房间密码：服务端更新 auth_key 并清出所有非房主成员。

        成功后本地才切换密钥；旧成员立即掉线，且只有拿到新密码的设备能重新加入。
        """
        if not self.host_secret:
            raise ValueError("仅房主可重置密码（缺少控制口令）。")
        new_password = str(new_password or "")
        if not new_password:
            raise ValueError("新密码不能为空")
        new_keys = derive_room_keys(new_password, self.name)
        resp = self._post("rekey", {
            "room": self.name,
            "sid": self.sid,
            "host_secret": self.host_secret,
            "auth_key": new_keys.auth_key.hex(),
        })
        if resp.get("ok"):
            self.password = new_password
            self.keys = new_keys
            self.peers = [PeerInfo.from_roster(p) for p in (resp.get("peers") or [])]
        return resp

    def join(self) -> dict[str, Any]:
        """加入者凭 proof 进入已开放会话；房间不存在/未开放由服务端报错，绝不自动建房。"""
        resp = self._post("join", {
            "room": self.name,
            "client_pub": self.pub,
            "nonce": self.nonce,
            "proof": make_proof(self.keys.auth_key, self.pub, self.nonce),
            "device_id": self.device_id,
        })
        return self._apply_join(resp)

    def announce(self, world: str | None = None, port: int | None = None) -> dict[str, Any]:
        payload = {
            "room": self.name,
            "sid": self.sid,
            "world": self.world if world is None else world,
            "port": self.port if port is None else port,
        }
        resp = self._post("announce", payload)
        self._apply_vpn(resp)
        return resp

    def request_relay(self, target_sid: str) -> dict[str, Any]:
        payload = {"room": self.name, "sid": self.sid, "peer": target_sid}
        resp = self._post("relay", payload)
        if resp.get("relay"):
            self.relay = dict(resp["relay"])
        return resp

    def leave(self) -> None:
        if not self.sid:
            return
        try:
            self._post("leave", {"room": self.name, "sid": self.sid})
        except Exception:  # noqa: BLE001 - 离开尽力而为，不影响本地清理
            pass
