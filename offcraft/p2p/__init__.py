"""离线联机（P2P）子系统：把广域网里不同局域网的设备拉进同一个虚拟局域网。

Phase 1 = 端到端加密 TCP 隧道（经镜像服务中继，免管理员权限即可穿任何 NAT）：

- :mod:`crypto` —— 房间密钥派生、准入 proof、数据面 AEAD 通道。
- :mod:`transport` —— 帧编解码、中继连接、端到端加密通道。
- :mod:`room` —— 走镜像 ``/p2p/*`` 的信令客户端（名册 / 心跳 / 中继配对）。
- :mod:`tunnel` —— 本地 TCP 与加密通道的双向泵送，房主 / 加入者两个角色。

Phase 2（Wintun 虚拟网卡、UDP 打洞直连）尚未实现，见实施计划。这是**特权内置子系统**
（需要原始 socket），不进受限插件沙箱；默认关闭、UI 不暴露中继地址。
"""

from __future__ import annotations

from .crypto import (
    CryptoChannel,
    P2PError,
    RoomKeys,
    TamperError,
    canonical_name,
    derive_room_keys,
    load_or_create_device_id,
    make_channel,
    make_proof,
    new_identity,
)
from .room import PeerInfo, RoomClient
from .transport import FrameReader, RelayTransport, SecureChannel
from .tunnel import HostTunnel, JoinTunnel, forward
from .vip import (
    VIP_HOST,
    VIP_PREFIX,
    derive_peer_vip,
    display_vip,
    is_vip,
    vip_number,
)
from .vpn_client import (
    VpnClient,
    VpnClientError,
    VpnPeerStatus,
    VpnStatus,
    build_config,
    derive_peer_keys,
    try_create,
    PIPE_NAME,
)
from .port_probe import (
    DEFAULT_MC_PORT,
    detect_from_log,
    detect_local_mc_port,
    is_listening,
    read_log_tail,
)

__all__ = [
    "P2PError",
    "TamperError",
    "RoomKeys",
    "CryptoChannel",
    "canonical_name",
    "derive_room_keys",
    "load_or_create_device_id",
    "make_channel",
    "make_proof",
    "new_identity",
    "PeerInfo",
    "RoomClient",
    "FrameReader",
    "SecureChannel",
    "RelayTransport",
    "JoinTunnel",
    "HostTunnel",
    "forward",
    "VIP_PREFIX",
    "VIP_HOST",
    "is_vip",
    "vip_number",
    "display_vip",
    "derive_peer_vip",
    "PIPE_NAME",
    "VpnClient",
    "VpnClientError",
    "VpnStatus",
    "VpnPeerStatus",
    "derive_peer_keys",
    "build_config",
    "try_create",
    "DEFAULT_MC_PORT",
    "detect_from_log",
    "detect_local_mc_port",
    "is_listening",
    "read_log_tail",
]
