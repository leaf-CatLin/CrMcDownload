"""地图物品的 ``colors`` 字节 → RGB。

``data/map_<id>.dat`` 里的 ``colors`` 是 16384 字节（128×128），每字节 =
``基础色 ID × 4 + 明暗档``（0..3）。基础色表是游戏里 ``MaterialMapColor``
的 62 个常量，明暗档的系数固定为 ``[180, 220, 255, 135] / 255``。

那张 62 项的基础色表换版本会变（1.21 才加进最后 4 个），但**前 58 项从
1.8 到 1.21 完全没变**，所以这里存的是 1.21.8 的完整 62 项，并在表尾备注
哪几项是新版才有的——老存档不会用到它们，解码结果不受影响。基础色 ID 为
0..61 之外的值一律按"未知"处理，避免越界。
"""

from __future__ import annotations

from typing import Sequence

__all__ = [
    "MAP_SHADES",
    "MAP_BASE_COLORS",
    "base_color_count",
    "decode_map_colors",
    "map_color_rgb",
    "rgb_to_qt",
]

#: 明暗档系数（游戏里就是这四个整数 / 255）
MAP_SHADES = (180, 220, 255, 135)

#: 62 个基础色（0xRRGGBB）。索引 = 基础色 ID。
MAP_BASE_COLORS: tuple[int, ...] = (
    0x000000,  # 0 无 / 透明
    0x7FB238,  # 1 草
    0xF7E9A3,  # 2 沙
    0xC7C7C7,  # 3 羊毛
    0xFF0000,  # 4 火 / 红石
    0xA0A0FF,  # 5 冰
    0xA7A7A7,  # 6 金属
    0x007C00,  # 7 植物
    0xFFFFFF,  # 8 雪
    0xA4A8B8,  # 9 黏土
    0x976D4D,  # 10 泥土
    0x707070,  # 11 石头
    0x4040FF,  # 12 水
    0x8F7748,  # 13 木头
    0xFFFCF5,  # 14 石英
    0xD87F33,  # 15 橙（金 / 陶瓦）
    0xB24CD8,  # 16 品红
    0x6699D8,  # 17 淡蓝
    0xE5E533,  # 18 黄
    0x7FCC19,  # 19 黄绿
    0xF27FA5,  # 20 粉
    0x4C4C4C,  # 21 灰
    0x999999,  # 22 淡灰
    0x4C7F99,  # 23 青
    0x7F3FB2,  # 24 紫
    0x334CB2,  # 25 蓝
    0x664C33,  # 26 棕
    0x667F33,  # 27 绿
    0x993333,  # 28 红（砖）
    0x191919,  # 29 黑
    0xFAEE4D,  # 30 金
    0x5CDBD5,  # 31 钻石
    0x4A80FF,  # 32 青金石
    0x00D93A,  # 33 绿宝石
    0x815631,  # 34 荚果（可可）
    0x700200,  # 35 绯红
    0xD1B1A1,  # 36 陶瓦色
    0x9F5224,  # 37 品红陶瓦
    0x95576C,  # 38 紫陶瓦
    0x706C8A,  # 39 蓝陶瓦
    0xBA8524,  # 40 棕陶瓦
    0x677535,  # 41 绿陶瓦
    0xA04D4E,  # 42 红陶瓦
    0x392923,  # 43 黑陶瓦
    0x876B62,  # 44 灰陶瓦
    0x575C5C,  # 45 深灰陶瓦
    0x7A4958,  # 46 粉陶瓦
    0x4C3E5C,  # 47 深青陶瓦
    0x4C3223,  # 48 深棕陶瓦
    0x4C522A,  # 49 青绿陶瓦
    0x8E3C2E,  # 50 深红陶瓦
    0x251610,  # 51 深黑陶瓦
    0xBD3031,  # 52 绯红木（1.16+）
    0x943F61,  # 53 诡异木（1.16+）
    0x5C191D,  # 54 深红
    0x167E86,  # 55 蓝绿
    0x3A8E8C,  # 56 蓝绿陶瓦
    0x562C3E,  # 57 深品红
    0x14B485,  # 58 苍白橡木 / 樱花（1.20+）
    0x646464,  # 59 深色石（1.21+）
    0xD8AF93,  # 60 苍白橡木皮（1.21.4+）
    0x7FA796,  # 61 铜（1.21.5+）
)


def base_color_count() -> int:
    return len(MAP_BASE_COLORS)


def map_color_rgb(value: int) -> tuple[int, int, int] | None:
    """把一个 ``colors`` 字节解成 ``(r, g, b)``；完全透明的 0..3 返回 ``None``。"""
    if value < 0 or value > 255:
        return None
    base_id = value >> 2
    shade = value & 3
    if base_id >= len(MAP_BASE_COLORS):
        return None
    if base_id == 0:
        return None
    base = MAP_BASE_COLORS[base_id]
    factor = MAP_SHADES[shade]
    return (
        ((base >> 16) & 0xFF) * factor // 255,
        ((base >> 8) & 0xFF) * factor // 255,
        (base & 0xFF) * factor // 255,
    )


def decode_map_colors(
    colors: Sequence[int],
    width: int = 128,
    height: int = 128,
) -> list[tuple[int, int, int, int] | None]:
    """把 ``colors`` 解成逐像素 RGBA（透明像素给 ``None``），行优先。"""
    total = max(0, int(width)) * max(0, int(height))
    out: list[tuple[int, int, int, int] | None] = []
    for index in range(total):
        raw = colors[index] if index < len(colors) else 0
        if isinstance(raw, (bytes, bytearray)):
            raw = raw[0] if raw else 0
        rgb = map_color_rgb(int(raw) & 0xFF)
        out.append(None if rgb is None else (*rgb, 255))
    return out


def rgb_to_qt(rgb: tuple[int, int, int]) -> int:
    """``(r, g, b)`` → Qt 的 ``0xRRGGBB``（QImage 的 ``setPixel`` 用）。"""
    r, g, b = rgb
    return (r << 16) | (g << 8) | b
