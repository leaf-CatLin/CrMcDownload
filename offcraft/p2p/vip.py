"""虚拟局域网虚拟 IP（``10.66.0.0/24``）校验与展示辅助。

规则与服务端 ``server_mirror.p2p`` 完全一致：房主固定 ``10.66.0.1``，加入者按
``HMAC-SHA256(auth_key, "offcraft-vip-v1|" || device)`` 前两字节映射到 ``.2`` - ``.254``。
这里只用于 UI 展示与本地校验，真正分配仍以服务端下发的 ``vip`` 为准。
"""

from __future__ import annotations

import hmac
from hashlib import sha256

__all__ = [
    "VIP_PREFIX",
    "VIP_HOST",
    "VIP_FIRST",
    "VIP_LAST",
    "is_vip",
    "vip_number",
    "display_vip",
    "derive_peer_vip",
]

VIP_PREFIX = "10.66.0"
VIP_HOST = f"{VIP_PREFIX}.1"
VIP_FIRST = 2
VIP_LAST = 254


def vip_number(value: str) -> int | None:
    """把 ``10.66.0.x`` 解析成主机号；格式或网段不符返回 ``None``。"""
    text = str(value or "").strip()
    parts = text.split(".")
    if len(parts) != 4:
        return None
    if ".".join(parts[:3]) != VIP_PREFIX:
        return None
    if not parts[3].isdigit():
        return None
    number = int(parts[3])
    if not 0 <= number <= 255:
        return None
    return number


def is_vip(value: str) -> bool:
    """是否为可用的虚拟 IP（``.1`` - ``.254``，排除网络/广播地址）。"""
    number = vip_number(value)
    return number is not None and number >= 1 and number <= 254


def display_vip(value: str, fallback: str = "—") -> str:
    """UI 展示：合法 ``vip`` 原样返回，否则给占位符（避免把空串画到界面上）。"""
    return value if is_vip(value) else fallback


def derive_peer_vip(auth_key: bytes, device: str) -> str:
    """本地复算加入者的稳定虚拟 IP（与服务端算法逐字节一致，供展示/自测）。"""
    digest = hmac.new(auth_key, b"offcraft-vip-v1|" + str(device).encode("utf-8"), sha256).digest()
    span = VIP_LAST - VIP_FIRST + 1
    return f"{VIP_PREFIX}.{VIP_FIRST + int.from_bytes(digest[:2], 'big') % span}"
