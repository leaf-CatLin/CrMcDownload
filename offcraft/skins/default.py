"""内置默认皮肤：没有上传过皮肤时也能预览 / 上传一张可用的 64×64 PNG。

与 ``offcraft.skins.upload`` 的校验完全兼容（64×64、PNG、< 64 KiB），不依赖
任何外部素材；进程内生成一次后缓存。
"""

from __future__ import annotations

import io
import threading

__all__ = ["DEFAULT_SKIN_NAME", "default_skin_bytes"]

DEFAULT_SKIN_NAME = "default"

_LOCK = threading.Lock()
_CACHE: dict[str, bytes] = {}

_SKIN = (238, 182, 143, 255)
_HAIR = (66, 44, 32, 255)
_SHIRT = (52, 120, 208, 255)
_SHIRT_DARK = (42, 98, 172, 255)
_PANTS = (58, 66, 92, 255)
_SHOES = (44, 44, 52, 255)
_EYE_WHITE = (246, 246, 246, 255)
_EYE = (45, 56, 74, 255)
_MOUTH = (120, 72, 60, 255)


def _draw() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.rectangle((0, 0, 31, 15), fill=_HAIR)
    draw.rectangle((0, 8, 7, 15), fill=_HAIR)
    draw.rectangle((24, 8, 31, 15), fill=_SKIN)
    draw.rectangle((8, 8, 23, 15), fill=_SKIN)
    draw.rectangle((8, 8, 23, 9), fill=_HAIR)

    draw.rectangle((10, 11, 11, 12), fill=_EYE_WHITE)
    draw.rectangle((13, 11, 14, 12), fill=_EYE_WHITE)
    draw.rectangle((10, 11, 10, 12), fill=_EYE)
    draw.rectangle((13, 11, 13, 12), fill=_EYE)
    draw.rectangle((11, 13, 12, 13), fill=_MOUTH)

    draw.rectangle((16, 16, 39, 31), fill=_SHIRT)
    draw.rectangle((20, 16, 27, 31), fill=_SHIRT_DARK)
    draw.rectangle((40, 16, 55, 31), fill=_SHIRT)
    draw.rectangle((40, 26, 55, 31), fill=_SKIN)
    draw.rectangle((32, 48, 47, 63), fill=_SKIN)
    draw.rectangle((32, 48, 47, 52), fill=_SHIRT)
    draw.rectangle((0, 16, 15, 31), fill=_PANTS)
    draw.rectangle((0, 29, 15, 31), fill=_SHOES)
    draw.rectangle((16, 48, 31, 63), fill=_PANTS)
    draw.rectangle((16, 61, 31, 63), fill=_SHOES)

    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()


def default_skin_bytes(model: str = "classic") -> bytes:
    """返回默认皮肤 PNG 字节（classic / slim 共用同一张贴图）。"""
    key = "slim" if str(model).lower() == "slim" else "classic"
    with _LOCK:
        data = _CACHE.get(key)
        if data is None:
            data = _draw()
            _CACHE[key] = data
        return data
