"""调色板 + 位打包解码（区块 ``sections`` 里真正装方块的地方）。

1.13 之后区块不再存"方块 ID 数组"，而是每个区段一份 ``palette``（这个区段
里出现过的方块状态列表）+ 一份 ``data``（long 数组，索引位打包）。

规则（写错任意一条都会整段错位）：

* ``bits = max(min_bits, ceil(log2(len(palette))))``，方块 ``min_bits=4``、
  生物群系 ``min_bits=1``；
* **1.16 / 20w17a 之后不跨 long 边界**：每个 long 里最多 ``64 // bits`` 项，
  尾部装不下的位直接浪费，下个值从下一个 long 开始；
* 1.16 之前是**跨边界**的连续位流（旧算法），老存档必须用另一套解码；
* ``palette`` 只有 1 项时 ``data`` 字段**整个不存在**（不是空数组）。

索引顺序：方块 ``i = (y * 16 + z) * 16 + x``；生物群系 ``i = (y4 * 4 + z4) * 4 + x4``。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

__all__ = [
    "HEIGHTMAP_BITS",
    "HEIGHTMAP_COUNT",
    "Scheme",
    "biome_palette_name",
    "blocks_per_section",
    "decode_heightmap",
    "encode_heightmap",
    "is_air_block",
    "pack_indices",
    "palette_bits",
    "palette_name",
    "pick_scheme",
    "section_blocks",
    "section_biomes",
    "unpack_indices",
]

#: 方块区段 = 16×16×16
SECTION_VOLUME = 4096
#: 生物群系区段 = 4×4×4
BIOME_VOLUME = 64
#: 高度图：256 项 × 9 bit（每个 long 装 7 项）
HEIGHTMAP_BITS = 9
HEIGHTMAP_COUNT = 256
HEIGHTMAP_PER_LONG = 64 // HEIGHTMAP_BITS

_MIN_BITS_BLOCK = 4
_MIN_BITS_BIOME = 1

#: 位打包方案：``compact`` = 1.16+（不跨 long），``packed`` = 1.16 之前（跨 long）
Scheme = str
SCHEME_COMPACT = "compact"
SCHEME_PACKED = "packed"


def pick_scheme(data_version: int | None) -> Scheme:
    """按区块 ``DataVersion`` 选位打包方案。

    20w17a 是 DataVersion **2529**；>= 2529 不跨 long。
    取不到版本号时按新版处理（现代存档占绝对多数）。
    """
    if data_version is None:
        return SCHEME_COMPACT
    return SCHEME_PACKED if data_version < 2529 else SCHEME_COMPACT


def palette_bits(size: int, *, min_bits: int) -> int:
    """``len(palette)`` 对应的每项位宽。"""
    if size <= 1:
        return min_bits
    bits = (size - 1).bit_length()
    return max(min_bits, bits)


def unpack_indices(
    longs: Sequence[int],
    bits: int,
    count: int,
    *,
    scheme: Scheme = SCHEME_COMPACT,
) -> list[int]:
    """把 long 数组解成索引列表（长度恰好 ``count``）。"""
    if count <= 0:
        return []
    if bits <= 0:
        return [0] * count
    mask = (1 << bits) - 1
    out: list[int] = []
    if scheme == SCHEME_PACKED:
        # 1.16 之前：连续位流，一个值可以横跨两个 long
        buffer = 0
        buffered = 0
        for value in longs:
            buffer |= (int(value) & 0xFFFFFFFFFFFFFFFF) << buffered
            buffered += 64
            while buffered >= bits and len(out) < count:
                out.append(buffer & mask)
                buffer >>= bits
                buffered -= bits
            if len(out) >= count:
                break
        return out
    per_long = 64 // bits
    for value in longs:
        unsigned = int(value) & 0xFFFFFFFFFFFFFFFF
        for slot in range(per_long):
            if len(out) >= count:
                return out
            out.append((unsigned >> (slot * bits)) & mask)
    while len(out) < count:
        out.append(0)
    return out[:count]


def pack_indices(indices: Iterable[int], bits: int) -> list[int]:
    """``unpack_indices`` 的逆运算（1.16+ 方案；造测试样本用）。"""
    items = list(indices)
    if bits <= 0:
        return []
    per_long = 64 // bits
    mask = (1 << bits) - 1
    longs: list[int] = []
    for start in range(0, len(items), per_long):
        value = 0
        for slot, index in enumerate(items[start : start + per_long]):
            value |= (index & mask) << (slot * bits)
        if value >= 1 << 63:  # NBT 的 long 是有符号的
            value -= 1 << 64
        longs.append(value)
    return longs


def palette_name(entry: Any) -> str:
    """调色板条目的方块名：新版是 compound（带 ``Name`` / ``Properties``），
    极老的存档可能直接存字符串。"""
    if isinstance(entry, dict):
        name = entry.get("Name") or entry.get("name") or ""
        return str(name)
    if isinstance(entry, str):
        return entry
    return ""


def biome_palette_name(entry: Any) -> str:
    """生物群系调色板条目：多数版本是字符串，个别版本是 ``{biome: "..."}``。"""
    if isinstance(entry, dict):
        for key in ("biome", "Biome", "Name", "name"):
            if key in entry:
                return str(entry[key])
        return ""
    if isinstance(entry, str):
        return entry
    return ""


def decode_heightmap(longs: Sequence[int], count: int = HEIGHTMAP_COUNT) -> list[int]:
    """解 9 bit 高度图（``MOTION_BLOCKING`` 等）。返回**相对最低 y 的偏移**。"""
    out: list[int] = []
    for value in longs:
        unsigned = int(value) & 0xFFFFFFFFFFFFFFFF
        for slot in range(HEIGHTMAP_PER_LONG):
            if len(out) >= count:
                return out
            out.append((unsigned >> (slot * HEIGHTMAP_BITS)) & 0x1FF)
    while len(out) < count:
        out.append(0)
    return out[:count]


def encode_heightmap(values: Sequence[int]) -> list[int]:
    """``decode_heightmap`` 的逆运算（造测试样本用）。"""
    mask = (1 << HEIGHTMAP_BITS) - 1
    longs: list[int] = []
    items = list(values)
    for start in range(0, len(items), HEIGHTMAP_PER_LONG):
        value = 0
        for slot, item in enumerate(items[start : start + HEIGHTMAP_PER_LONG]):
            value |= (int(item) & mask) << (slot * HEIGHTMAP_BITS)
        if value >= 1 << 63:
            value -= 1 << 64
        longs.append(value)
    return longs


def is_air_block(block_name: str) -> bool:
    """是否是"空气类"方块（统计实体方块数时跳过）。"""
    return block_name in ("minecraft:air", "minecraft:cave_air", "minecraft:void_air", "air")


def section_blocks(
    section: dict,
    *,
    scheme: Scheme = SCHEME_COMPACT,
) -> tuple[list[tuple[str, dict]], list[int]]:
    """解一个 ``sections[]`` 元素。

    返回 ``(调色板[(方块名, 属性)], 4096 个索引)``；索引按
    ``i = (y * 16 + z) * 16 + x`` 排列。
    """
    states = section.get("block_states") or section.get("BlockStates") or {}
    if not isinstance(states, dict):
        states = {}
    raw_palette = states.get("palette") or section.get("Palette") or []
    palette: list[tuple[str, dict]] = []
    for entry in raw_palette:
        name = palette_name(entry)
        props = entry.get("Properties") if isinstance(entry, dict) else None
        palette.append((name, dict(props) if isinstance(props, dict) else {}))
    if not palette:
        palette = [("minecraft:air", {})]
    data = states.get("data")
    if data is None:
        data = section.get("BlockStates")
    if not data:
        # palette 只有一项：整个 data 字段不存在
        return palette, [0] * SECTION_VOLUME
    bits = palette_bits(len(palette), min_bits=_MIN_BITS_BLOCK)
    return palette, unpack_indices(data, bits, SECTION_VOLUME, scheme=scheme)


def section_biomes(
    section: dict,
    *,
    scheme: Scheme = SCHEME_COMPACT,
) -> tuple[list[str], list[int]]:
    """解一个区段的生物群系（1.18+ 的 4×4×4 + 位打包）。"""
    biomes = section.get("biomes") or {}
    if not isinstance(biomes, dict):
        return [], []
    raw_palette = biomes.get("palette") or []
    palette = [biome_palette_name(entry) for entry in raw_palette]
    data = biomes.get("data")
    if not data:
        return palette, [0] * BIOME_VOLUME if palette else []
    bits = palette_bits(len(palette), min_bits=_MIN_BITS_BIOME)
    return palette, unpack_indices(data, bits, BIOME_VOLUME, scheme=scheme)
