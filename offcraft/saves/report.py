"""把存档目录扫成 :class:`~offcraft.saves.model.SaveReport`。

设计取舍（都是被真实存档规模逼出来的）：

* **只读**。启动器绝不写玩家的存档目录；所有函数都只读文件。
* **两段式开销**。区域文件的偏移表与时间戳表在头部 8 KiB 里，读它们就能知道
  "有哪些区块、什么时候写的"，完全不用解压——所以"已生成区块数 / 活跃日期"
  永远准确且极快；只有需要方块统计时才解区块 NBT。
* **区块有预算**。几万区块的存档全解会在 Python 里跑几分钟，因此解析区块的
  个数由 :class:`SaveScanOptions` 限制（页面默认 2500），方块统计再单独采样
  （默认 160 个区块）；界面会标明是"采样值"，并把"完整/已解析"一起显示，
  避免把"没扫到"误读成"没生成"。
* **退化文件不是损坏**。游戏会给未探索区域预建 **0 字节的 `.mca`**
  （实测两份真实存档的 `poi/r.-1.-1.mca` 都是 0 字节）。连 8 KiB 头部都装不
  下的文件一律当作"没有区块"跳过——早期实现把它当致命错误，导致整份存档
  点开就报"区域文件过短"。
* **单个文件/维度坏掉不影响整体**。每个 region 文件、每个维度的 entities/poi
  读取都各自兜异常，坏掉就记一条告警继续，绝不让整次扫描失败。
* **老版本兼容**。``level.dat`` 内嵌字段（``GameRules`` / ``DayTime`` /
  ``SpawnX`` …）与新拆分文件两条路径都读，谁有就用谁。
* **战利品箱不是空箱**。未打开的箱子只有 ``LootTable`` + ``LootTableSeed``，
  ``Items`` 本来就是空的，要单独标记（真实存档里 195/225 个容器都是这种）。

性能（实测 3540 区块的真实存档，纯 Python NBT 解析是唯一瓶颈）：

* NBT 的数值列表整块 ``struct.unpack``（原来逐项递归，4800 万次函数调用）；
* 字符串走 ``isascii()`` 快路径；`u1`/`_unpack` 内联边界检查；
* 索引计数先试"整段同值"再用 ``Counter``；
* 区域文件用 :meth:`RegionFile.chunks` 复用句柄，不再每个区块 open 一次。

以上把 58 秒压到 ~14 秒（全解）/ ~3 秒（默认 2500 区块预算）。
"""

from __future__ import annotations

import json
import os
import re
import struct
import time
import uuid as uuidlib
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from offcraft.saves.anvil import HEADER, RegionFile, RegionFormatError, parse_region_name
from offcraft.saves.model import (
    Advancement,
    ChunkStats,
    Container,
    DimensionInfo,
    EntityGroup,
    GameRule,
    ItemStack,
    LevelInfo,
    MapInfo,
    PlayerInfo,
    PoiRecord,
    SaveReport,
    SaveScanOptions,
    SpawnPoint,
    StructureRef,
    TimelineEvent,
    Weather,
    WorldBorder,
    WorldClock,
)
from offcraft.saves.nbt import NBTError, decompress, load_dat, read_nbt
from offcraft.saves.palette import (
    SCHEME_COMPACT,
    SCHEME_PACKED,
    is_air_block,
    decode_heightmap,
    pack_indices,
    palette_bits,
    pick_scheme,
    section_blocks,
    section_biomes,
)

__all__ = ["save_summary", "scan_save"]

#: 方块统计最多采样多少个区块（解 sections 很贵，超过这个数只统计高度图）
_BLOCK_SAMPLE = 160
#: 一个区块里最多解多少个区段做方块统计（默认全解，24 个区段）
_MAX_SECTIONS = 32
#: 时间线最多保留多少条事件
_TIMELINE_MAX = 400
#: 日志最多读多少行
_LOG_LINES = 1200
#: 日志里最多挑多少条与世界有关的行
_LOG_KEEP = 200

_PROGRESS = Callable[[float, str], None]

#: ``logs/latest.log`` 行首时间戳：``[21Nov2026 12:00:00.123]``
_LOG_STAMP = re.compile(r"^\[(\d{2})([A-Za-z]{3})(\d{4}) (\d{2}:\d{2}:\d{2})(?:\.\d+)?\]\s*(.*)$")
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_ADV_TIME = "%Y-%m-%d %H:%M:%S %z"


# --------------------------------------------------------------------- 小工具
def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _int(value: Any, default: int | None = None) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return default


def _float(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on", "开")
    return default


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _uuid_from_ints(values: Iterable[Any]) -> str:
    """``int[4]`` → 标准 UUID 字符串（NBT 里是**有符号** int，要先截成 32 位）。

    全 0 的 UUID 视为"没有 UUID"：离线启动器 / 早期版本会写 0，它不是真实
    UUID（界面上显示成一串 0000 会给玩家"这人是黑客"的错觉）。
    """
    ints = [_int(v, 0) or 0 for v in values]
    if len(ints) != 4:
        return ""
    if not any(ints):
        return ""
    try:
        raw = struct.pack(">4I", *[item & 0xFFFFFFFF for item in ints])
        return str(uuidlib.UUID(bytes=raw))
    except struct.error:
        return ""


def _human_ticks(ticks: int) -> str:
    seconds = ticks / 20
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f} 分钟"
    return f"{minutes / 60:.1f} 小时"


# ------------------------------------------------------------------ level.dat
def _level_from_tree(tree: dict) -> LevelInfo:
    """解析 ``level.dat``（新旧字段一起兜）。"""
    data = _dict(tree.get("Data"))
    if not data:
        data = _dict(tree)

    info = LevelInfo()
    info.data_version = _int(data.get("DataVersion"))
    info.storage_version = _int(data.get("version"))
    info.level_name = _text(data.get("LevelName"))
    version = _dict(data.get("Version"))
    if version:
        info.game_version = _text(version.get("Name"))
    info.game_type = _int(data.get("GameType"), 0) or 0
    info.allow_commands = _bool(data.get("allowCommands"))
    info.initialized = _bool(data.get("initialized"), True)
    info.time = _int(data.get("Time"), 0) or 0
    info.day_time = _int(data.get("DayTime"))
    info.last_played = _int(data.get("LastPlayed"))
    info.was_modded = _bool(data.get("WasModded"))

    # 难度：新版在 difficulty_settings，旧版是 Difficulty 字节
    settings = _dict(data.get("difficulty_settings"))
    if settings:
        info.difficulty = _text(settings.get("difficulty"))
        info.hardcore = _bool(settings.get("hardcore"))
    else:
        legacy = _int(data.get("Difficulty"))
        info.difficulty = {0: "peaceful", 1: "easy", 2: "normal", 3: "hard"}.get(
            legacy or 0, ""
        )
        info.hardcore = _bool(data.get("hardcore"))

    # 出生点：新版 spawn compound，旧版 SpawnX/Y/Z
    spawn = _dict(data.get("spawn"))
    if spawn:
        pos = _list(spawn.get("pos"))
        if len(pos) >= 3:
            info.spawn = SpawnPoint(
                x=_float(pos[0], 0.0) or 0.0,
                y=_float(pos[1], 0.0) or 0.0,
                z=_float(pos[2], 0.0) or 0.0,
                yaw=_float(spawn.get("yaw"), 0.0) or 0.0,
                dimension=_text(spawn.get("dimension")) or "minecraft:overworld",
                forced=_bool(spawn.get("forced")),
            )
    elif data.get("SpawnX") is not None:
        info.spawn = SpawnPoint(
            x=float(_int(data.get("SpawnX"), 0) or 0),
            y=float(_int(data.get("SpawnY"), 0) or 0),
            z=float(_int(data.get("SpawnZ"), 0) or 0),
            yaw=float(_int(data.get("SpawnAngle"), 0) or 0),
        )

    # 种子 / 生成器
    gen = _dict(data.get("WorldGenSettings"))
    if gen:
        info.seed = _int(gen.get("seed"))
        info.bonus_chest = _bool(gen.get("bonus_chest"))
        info.generate_structures = _bool(gen.get("generate_structures"), True)
        info.generator = _generator_kind(_dict(gen.get("dimensions")).get("minecraft:overworld"))
    else:
        info.seed = _int(data.get("RandomSeed"))
        info.bonus_chest = _bool(data.get("bonusChest"))
        info.generate_structures = _bool(data.get("MapFeatures"), True)

    info.server_brands = tuple(_text(item) for item in _list(data.get("ServerBrands")) if _text(item))
    packs = _dict(data.get("DataPacks"))
    info.data_packs = tuple(_text(item) for item in _list(packs.get("Enabled")) if _text(item))
    info.disabled_packs = tuple(_text(item) for item in _list(packs.get("Disabled")) if _text(item))
    for key in ("singleplayer_uuid", "SinglePlayerUUID"):
        if key in data:
            info.singleplayer_uuid = _uuid_from_ints(_list(data[key]))
            if info.singleplayer_uuid:
                break
    return info


def _generator_kind(dimension: Any) -> str:
    """从 ``WorldGenSettings.dimensions.<dim>`` 里取生成器类型。"""
    node = _dict(dimension)
    generator = _dict(node.get("generator"))
    return _text(generator.get("type")) or ""


# ------------------------------------------------------- 新版拆分出来的世界文件
def _data_file_candidates(save: Path, name: str) -> list[Path]:
    """新版世界数据文件可能落在 ``data/``、``data/minecraft/`` 两处，两种都找。"""
    return [
        save / "data" / "minecraft" / name,
        save / "data" / name,
    ]


def _load_world_file(save: Path, name: str) -> dict | None:
    for path in _data_file_candidates(save, name):
        if not path.is_file():
            continue
        try:
            tree = load_dat(path)
        except (OSError, NBTError, ValueError):
            continue
        # 统一剥掉外层：大多数文件是 {"data": {...}, "DataVersion": n}
        for key in ("data", "Data"):
            payload = tree.get(key)
            if isinstance(payload, dict):
                return payload
        return tree
    return None


def _parse_rules(save: Path, level_tree: dict) -> list[GameRule]:
    payload = _load_world_file(save, "game_rules.dat")
    if payload is None:
        # 旧版：level.dat -> Data.GameRules
        data = _dict(level_tree.get("Data")) or _dict(level_tree)
        payload = _dict(data.get("GameRules"))
    rules = []
    for key, value in payload.items():
        rules.append(GameRule(name=str(key), value=value))
    rules.sort(key=lambda rule: rule.short_name.lower())
    return rules


def _parse_border(save: Path, level_tree: dict) -> WorldBorder | None:
    for base in (save / "data" / "minecraft", save / "data"):
        path = base / "world_border.dat"
        if not path.is_file():
            continue
        try:
            payload = _world_payload(load_dat(path))
        except (OSError, NBTError, ValueError):
            continue
        if payload is None:
            continue
        return WorldBorder(
            center_x=_float(payload.get("center_x") or payload.get("CenterX"), 0.0) or 0.0,
            center_z=_float(payload.get("center_z") or payload.get("CenterZ"), 0.0) or 0.0,
            size=_float(payload.get("size") or payload.get("Size"), 0.0) or 0.0,
            damage_per_block=_float(payload.get("damage_per_block"), 0.0) or 0.0,
            safe_zone=_float(payload.get("safe_zone"), 0.0) or 0.0,
            warning_blocks=_int(payload.get("warning_blocks"), 0) or 0,
            warning_time=_int(payload.get("warning_time"), 0) or 0,
            lerp_target=_float(payload.get("lerp_target"), 0.0) or 0.0,
            lerp_time=_int(payload.get("lerp_time"), 0) or 0,
        )
    # 旧版：level.dat 里的 BorderSize / BorderCenterX …
    data = _dict(level_tree.get("Data")) or _dict(level_tree)
    if data.get("BorderSize") is not None:
        return WorldBorder(
            center_x=_float(data.get("BorderCenterX"), 0.0) or 0.0,
            center_z=_float(data.get("BorderCenterZ"), 0.0) or 0.0,
            size=_float(data.get("BorderSize"), 0.0) or 0.0,
            damage_per_block=_float(data.get("BorderDamagePerBlock"), 0.0) or 0.0,
            safe_zone=_float(data.get("BorderSafeZone"), 0.0) or 0.0,
            warning_blocks=_int(data.get("BorderWarningDistance"), 0) or 0,
            warning_time=_int(data.get("BorderWarningTime"), 0) or 0,
        )
    return None


def _world_payload(tree: dict) -> dict | None:
    for key in ("data", "Data"):
        payload = tree.get(key)
        if isinstance(payload, dict):
            return payload
    return None


def _parse_weather(save: Path, level_tree: dict) -> Weather | None:
    payload = _load_world_file(save, "weather.dat")
    if payload is not None:
        return Weather(
            raining=_bool(payload.get("raining")),
            thundering=_bool(payload.get("thundering")),
            rain_time=_int(payload.get("rain_time") or payload.get("RainTime"), 0) or 0,
            thunder_time=_int(payload.get("thunder_time") or payload.get("ThunderTime"), 0) or 0,
            clear_weather_time=_int(payload.get("clear_weather_time"), 0) or 0,
        )
    data = _dict(level_tree.get("Data")) or _dict(level_tree)
    if any(key in data for key in ("raining", "rainTime", "thundering", "thunderTime")):
        return Weather(
            raining=_bool(data.get("raining")),
            thundering=_bool(data.get("thundering")),
            rain_time=_int(data.get("rainTime"), 0) or 0,
            thunder_time=_int(data.get("thunderTime"), 0) or 0,
            clear_weather_time=_int(data.get("clearWeatherTime"), 0) or 0,
        )
    return None


def _parse_clocks(save: Path, level_tree: dict) -> list[WorldClock]:
    payload = _load_world_file(save, "world_clocks.dat")
    clocks: list[WorldClock] = []
    if payload:
        for key, value in payload.items():
            node = _dict(value)
            ticks = _int(node.get("total_ticks") or node.get("totalTicks"))
            if ticks is not None:
                clocks.append(WorldClock(dimension=str(key), total_ticks=ticks))
    if clocks:
        return clocks
    data = _dict(level_tree.get("Data")) or _dict(level_tree)
    ticks = _int(data.get("Time"))
    if ticks is not None:
        clocks.append(WorldClock(dimension="minecraft:overworld", total_ticks=ticks))
    return clocks


def _parse_tickets(save: Path) -> tuple[tuple[str, int, int], ...]:
    """``chunk_tickets.dat``：强制加载区块的票据（出生区、传送门、机器）。

    文件结构在各版本间变过（``tickets`` 可能是 list 也可能是 compound），
    这里只做"能认出坐标就记一条"的宽松解析。
    """
    out: list[tuple[str, int, int]] = []
    for path in save.rglob("chunk_tickets.dat"):
        try:
            tree = load_dat(path)
        except (OSError, NBTError, ValueError):
            continue
        payload = _world_payload(tree) or {}
        for ticket in _walk_dicts(payload):
            kind = _text(ticket.get("type") or ticket.get("Type")) or "ticket"
            coords = ticket.get("chunk_pos") or ticket.get("pos") or ticket.get("ChunkPos")
            if isinstance(coords, list) and len(coords) >= 2:
                cx = _int(coords[0], 0) or 0
                cz = _int(coords[1], 0) or 0
                out.append((kind, cx, cz))
            elif isinstance(coords, dict):
                cx = _int(coords.get("x"), 0) or 0
                cz = _int(coords.get("z"), 0) or 0
                out.append((kind, cx, cz))
    return tuple(out)


def _walk_dicts(node: Any) -> Iterable[dict]:
    """深度优先遍历所有 dict（用于结构不定的文件）。"""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_dicts(value)


# ------------------------------------------------------------------ 物品解析
_ENCHANT_KEYS = ("Enchantments", "enchantments", "StoredEnchantments", "stored_enchantments")


def _parse_enchantments(tag: dict) -> tuple[tuple[str, int], ...]:
    """把新旧两套附魔结构统一成 ``((附魔 id, 等级), …)``。

    * 1.20.5+ components：``{"levels": {"minecraft:sharpness": 5}}``
    * 1.20.5- tag：``[{"id": "minecraft:sharpness", "lvl": 5}]``
    """
    found: list[tuple[str, int]] = []
    for source in (
        _dict(tag.get("minecraft:enchantments")),
        _dict(tag.get("enchantments")),
    ):
        levels = _dict(source.get("levels") or source)
        for key, value in levels.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                found.append((str(key), int(value)))
    for key in _ENCHANT_KEYS:
        raw = tag.get(key)
        if isinstance(raw, dict):
            levels = _dict(raw.get("levels") or raw)
            for name, value in levels.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    found.append((str(name), int(value)))
        elif isinstance(raw, list):
            for entry in raw:
                node = _dict(entry)
                name = _text(node.get("id") or node.get("Id"))
                level = _int(node.get("lvl") or node.get("Level"), 0) or 0
                if name:
                    found.append((name, level))
    # 去重（同一个附魔可能两处都在）
    seen: dict[str, int] = {}
    for name, level in found:
        seen[name] = max(seen.get(name, 0), level)
    return tuple(sorted(seen.items()))


def _parse_item(node: dict) -> ItemStack | None:
    """一件物品：1.20.5 前后结构差异都在这里归一。"""
    item_id = _text(node.get("id") or node.get("Id"))
    if not item_id:
        return None
    slot = _int(node.get("Slot"), 0) or 0
    count = _int(node.get("count") if "count" in node else node.get("Count"), 1) or 1
    components = _dict(node.get("components"))
    tag = _dict(node.get("tag"))
    damage = _int(components.get("minecraft:damage"))
    if damage is None:
        damage = _int(node.get("Damage"))
    name = ""
    custom = components.get("minecraft:custom_name") or _dict(tag.get("display")).get("Name")
    if isinstance(custom, str):
        name = _plain_text(custom)
    elif isinstance(custom, dict):
        name = _plain_text(json.dumps(custom, ensure_ascii=False))
    extras = components or tag
    return ItemStack(
        slot=slot,
        item_id=item_id,
        count=count,
        damage=damage,
        custom_name=name,
        enchantments=_parse_enchantments(extras),
        extra=dict(extras) if isinstance(extras, dict) else {},
    )


_TEXT_RE = re.compile(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _plain_text(raw: str) -> str:
    """从 JSON 文本组件里抠出可读文字（``{"text":"Blade"}`` → ``Blade``）。"""
    text = raw.strip()
    if not text.startswith("{"):
        return _strip_codes(text)
    try:
        node = json.loads(text)
    except ValueError:
        match = _TEXT_RE.search(text)
        return _strip_codes(match.group(1)) if match else ""
    return _strip_codes(_text_component(node))


def _text_component(node: Any) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_text_component(item) for item in node)
    if isinstance(node, dict):
        parts = []
        if "text" in node:
            parts.append(_text(node.get("text")))
        for key in ("extra", "with"):
            if key in node:
                parts.append(_text_component(node[key]))
        if "translate" in node and not parts:
            parts.append(_text(node.get("translate")))
        return "".join(parts)
    return ""


_CODE_RE = re.compile("§.")


def _strip_codes(text: str) -> str:
    return _CODE_RE.sub("", text)


def _parse_items(raw: Any) -> tuple[ItemStack, ...]:
    items = []
    for entry in _list(raw):
        item = _parse_item(_dict(entry))
        if item is not None:
            items.append(item)
    items.sort(key=lambda stack: stack.slot)
    return tuple(items)


# ------------------------------------------------------------------ 容器解析
#: 带物品栏的方块实体 → 显示名
_CONTAINER_KINDS = {
    "minecraft:chest",
    "minecraft:trapped_chest",
    "minecraft:barrel",
    "minecraft:furnace",
    "minecraft:blast_furnace",
    "minecraft:smoker",
    "minecraft:hopper",
    "minecraft:dispenser",
    "minecraft:dropper",
    "minecraft:brewing_stand",
    "minecraft:lectern",
    "minecraft:campfire",
    "minecraft:jukebox",
    "minecraft:decorated_pot",
    "minecraft:crafter",
    "minecraft:vault",
}
_SHULKER_PREFIX = "minecraft:shulker_box"


def _parse_block_entity(node: dict) -> Container | None:
    kind = _text(node.get("id") or node.get("Id"))
    if not kind:
        return None
    is_container = kind in _CONTAINER_KINDS or kind.startswith(_SHULKER_PREFIX)
    items = _parse_items(node.get("Items") or node.get("items"))
    # 战利品箱：未打开时游戏只存 LootTable + LootTableSeed，没有 Items
    loot_table = _text(node.get("LootTable"))
    loot_seed = _int(node.get("LootTableSeed"))
    if not is_container and not items and not loot_table:
        return None
    extra: list[tuple[str, Any]] = []
    for key in ("BurnTime", "CookTime", "BrewTime", "Lock"):
        if key in node:
            extra.append((key, node[key]))
    return Container(
        kind=kind,
        x=_int(node.get("x"), 0) or 0,
        y=_int(node.get("y"), 0) or 0,
        z=_int(node.get("z"), 0) or 0,
        items=items,
        loot_table=loot_table,
        loot_seed=loot_seed,
        extra=tuple(extra),
    )


# ------------------------------------------------------------------ 区块扫描
def _chunk_status(chunk: dict) -> str:
    return _text(chunk.get("Status") or chunk.get("status"))


def _is_full(status: str) -> bool:
    return status in ("minecraft:full", "full", "")


def _region_files(folder: Path) -> list[Path]:
    try:
        return sorted(path for path in folder.iterdir() if path.suffix == ".mca")
    except OSError:
        return []


def _is_empty_file(path: Path) -> bool:
    """文件小到装不下 8 KiB 头部 → 不可能有任何区块。

    游戏会为未探索区域预建 0 字节的 ``.mca``（实测真实存档里
    ``poi/r.-1.-1.mca`` 就是 0 字节）；比头部还短的文件同样是退化的空文件。
    它们不是损坏数据，计数时应排除，读取时也当"没有区块"。
    """
    try:
        return path.stat().st_size < HEADER
    except OSError:
        return True


def _real_region_files(folder: Path) -> int:
    """目录里"真正有内容"的区域文件数（不把退化的空文件算进去）。"""
    return sum(1 for path in _region_files(folder) if not _is_empty_file(path))


def _open_region(path: Path) -> RegionFile | None:
    """打开区域文件；退化文件（装不下头部的占位）直接当作"没有区块"。

    Minecraft 会先在未探索区域的 ``poi`` / ``entities`` 里建 0 字节的
    ``.mca``，实测真实存档里就有这种文件。它们不是损坏数据，跳过即可。
    """
    try:
        region = RegionFile.open(path)
    except (OSError, RegionFormatError):
        return None
    return None if region.is_empty else region


def _region_entries(path: Path) -> list[tuple[int, int]]:
    """一个区域文件里存在的区块坐标；任何异常都当作"没有区块"。"""
    region = _open_region(path)
    if region is None:
        return []
    try:
        return region.chunk_coords()
    except (OSError, RegionFormatError):
        return []


def _scan_region_folder(
    folder: Path,
    *,
    budget: list[int],
    block_budget: list[int],
    scheme_by_chunk: bool = True,
    block_counter: Counter,
    biome_counter: Counter,
    ground_values: list[int],
    containers: list[Container],
    structures: list[StructureRef],
    stats: dict,
) -> tuple[int, int, int]:
    """扫一个 ``region/`` 目录。

    返回 ``(区域文件数, 区块总数, 完整区块数)``；同时把方块/生物群系/高度统计
    累加到传入的容器里。
    """
    files = _region_files(folder)
    total = 0
    full = 0
    parsed = 0
    broken = 0
    real_files = 0
    for path in files:
        region = _open_region(path)
        if region is None:
            continue  # 落后于头部的退化文件：不是损坏，直接跳过
        real_files += 1
        try:
            header = region.read_header()
        except (OSError, RegionFormatError):
            broken += 1
            continue
        present = 0
        for index in range(1024):
            base = index * 4
            if int.from_bytes(header[base : base + 3], "big") == 0:
                continue
            present += 1
            if budget[0] <= 0:
                # 预算用完：只数存在性，不再解 NBT
                continue
            budget[0] -= 1
            lx, lz = index & 31, index >> 5
            cx = region.rx * 32 + lx
            cz = region.rz * 32 + lz
            try:
                chunk = region.read_chunk(cx, cz)
            except (OSError, RegionFormatError, NBTError):
                broken += 1
                continue
            if chunk is None:
                broken += 1
                continue
            parsed += 1
            status = _chunk_status(chunk)
            if _is_full(status):
                full += 1
            _absorb_chunk(
                chunk,
                allow_blocks=block_budget[0] > 0,
                block_counter=block_counter,
                biome_counter=biome_counter,
                ground_values=ground_values,
                containers=containers,
                structures=structures,
                stats=stats,
            )
            if block_budget[0] > 0:
                block_budget[0] -= 1
        total += present
    stats["broken"] = stats.get("broken", 0) + broken
    stats["parsed"] = stats.get("parsed", 0) + parsed
    return real_files, total, full
    stats["broken"] = stats.get("broken", 0) + broken
    stats["parsed"] = stats.get("parsed", 0) + parsed
    return real_files, total, full


def _absorb_chunk(
    chunk: dict,
    *,
    allow_blocks: bool,
    block_counter: Counter,
    biome_counter: Counter,
    ground_values: list[int],
    containers: list[Container],
    structures: list[StructureRef],
    stats: dict,
) -> None:
    """从一个区块里抽取：方块统计、生物群系、高度、容器、结构。"""
    data_version = _int(chunk.get("DataVersion"))
    scheme = pick_scheme(data_version) if data_version is not None else SCHEME_COMPACT
    stats["data_version"] = max(stats.get("data_version") or 0, data_version or 0) or None
    min_y = _int(chunk.get("yPos"))
    if min_y is None:
        min_y = -4 if (data_version or 0) >= 2825 else 0
        stats["legacy"] = True
    # 高度图（1.18+ 存的是相对世界底部的偏移，要加回 minY * 16；值本身是
    # "地面上方第一格空气的 Y"，所以再减 1 才是地表方块的 Y）
    heightmaps = _dict(chunk.get("Heightmaps"))
    for name in ("MOTION_BLOCKING", "WORLD_SURFACE", "OCEAN_FLOOR"):
        longs = heightmaps.get(name)
        if isinstance(longs, list) and longs:
            values = decode_heightmap(longs, 256)
            offset = min_y * 16
            for value in values:
                if value:
                    ground_values.append(offset + value - 1)
            break
    # 区块坐标（用于结构去重）
    cx = _int(chunk.get("xPos"), 0) or 0
    cz = _int(chunk.get("zPos"), 0) or 0

    starts = _dict(_dict(chunk.get("structures")).get("starts"))
    for name, entries in starts.items():
        for entry in _list(entries):
            node = _dict(entry)
            structures.append(
                StructureRef(
                    structure=str(name),
                    chunk_x=_int(node.get("chunkX"), cx) or cx,
                    chunk_z=_int(node.get("chunkZ"), cz) or cz,
                )
            )

    for entry in _list(chunk.get("block_entities") or chunk.get("TileEntities")):
        container = _parse_block_entity(_dict(entry))
        if container is not None:
            containers.append(container)

    sections = _list(chunk.get("sections") or chunk.get("Sections"))
    if not allow_blocks:
        return
    # 方块 / 生物群系统计：只解前 _MAX_SECTIONS 个区段，且跳过纯空气区段
    for section in sections[:_MAX_SECTIONS]:
        node = _dict(section)
        try:
            palette, indices = section_blocks(node, scheme=scheme)
        except (NBTError, ValueError):
            continue
        if not indices:
            continue
        counts = _count_indices(indices, len(palette))
        for index, amount in counts.items():
            name = palette[index][0] if index < len(palette) else ""
            if not name:
                continue
            if is_air_block(name):
                # 空气单独计数：它永远是第一名（约占 80%），放在"出现最多的方块"
                # 里会把整张榜都挤成一条"空气 + 一堆 0 长度条"。
                stats["air"] = stats.get("air", 0) + amount
                continue
            block_counter[name] += amount
            stats["solid"] = stats.get("solid", 0) + amount
        try:
            biomes, biome_indices = section_biomes(node, scheme=scheme)
        except (NBTError, ValueError):
            continue
        if biomes and biome_indices:
            biome_counts = _count_indices(biome_indices, len(biomes))
            for index, amount in biome_counts.items():
                name = biomes[index] if index < len(biomes) else ""
                if name:
                    biome_counter[name] += amount


def _count_indices(indices: list[int], palette_size: int) -> dict[int, int]:
    """统计索引出现次数。

    实测这是方块统计里最贵的一步（3540 个区块 × 24 个区段 × 4096 个索引）：
    逐项字典累加要 10 秒。绝大多数区段的调色板只有 2~4 项，所以先试
    "全是同一个索引"（纯空气/纯石头）这种最常见的情况，再用 ``Counter``
    走 C 层计数，比手写循环快一个数量级。
    """
    if not indices:
        return {}
    first = indices[0]
    if len(indices) == 1:
        return {first: 1}
    # 常见情况 1：整个区段只有一种方块（深层石头 / 高空空气）
    if indices.count(first) == len(indices):
        return {first: len(indices)}
    # 常见情况 2：小调色板 —— Counter 在 C 层计数
    return dict(Counter(indices))


# ------------------------------------------------------------------ 维度发现
def _dimension_dirs(save: Path) -> list[tuple[str, Path]]:
    """返回 ``[(维度 key, 维度目录)]``，新旧两种布局都支持。"""
    found: list[tuple[str, Path]] = []
    dimensions = save / "dimensions"
    if dimensions.is_dir():
        for namespace in sorted(dimensions.iterdir()):
            if not namespace.is_dir():
                continue
            for dim in sorted(namespace.iterdir()):
                if dim.is_dir():
                    found.append((f"{namespace.name}:{dim.name}", dim))
    # 旧版布局：region/ 在存档根，下界 DIM-1，末地 DIM1
    legacy = [
        ("minecraft:overworld", save / "region"),
        ("minecraft:the_nether", save / "DIM-1" / "region"),
        ("minecraft:the_end", save / "DIM1" / "region"),
    ]
    keys = {key for key, _ in found}
    for key, region in legacy:
        if key in keys or not region.is_dir():
            continue
        found.append((key, region.parent))
    return found


def _dimension_paths(dimension: Path) -> dict[str, Path]:
    """一个维度目录下的 region / entities / poi 子目录。"""
    # 新版：dimensions/<ns>/<dim>/{region,entities,poi}
    # 旧版：DIM-1/region 这种，dimension 本身可能就是 region 的父目录
    paths = {}
    for name in ("region", "entities", "poi"):
        candidate = dimension / name
        if candidate.is_dir():
            paths[name] = candidate
    if "region" not in paths and dimension.name == "region" and dimension.is_dir():
        paths["region"] = dimension
    return paths


# ------------------------------------------------------------------ 实体 / POI
def _collect_entities(folder: Path, groups: Counter, positions: dict[str, list]) -> int:
    count = 0
    for path in _region_files(folder):
        region = _open_region(path)
        if region is None:
            continue
        for cx, cz in _region_entries(path):
            try:
                chunk = region.read_chunk(cx, cz)
            except (OSError, RegionFormatError, NBTError):
                continue
            if not chunk:
                continue
            for entry in _list(chunk.get("Entities") or chunk.get("entities")):
                node = _dict(entry)
                entity_id = _text(node.get("id") or node.get("Id"))
                if not entity_id:
                    continue
                count += 1
                groups[entity_id] += 1
                pos = _list(node.get("Pos"))
                if len(pos) >= 3 and len(positions.setdefault(entity_id, [])) < 4:
                    positions[entity_id].append(
                        (
                            round(_float(pos[0], 0.0) or 0.0, 1),
                            round(_float(pos[1], 0.0) or 0.0, 1),
                            round(_float(pos[2], 0.0) or 0.0, 1),
                        )
                    )
    return count


def _collect_poi(folder: Path, records: list[PoiRecord]) -> int:
    total = 0
    for path in _region_files(folder):
        region = _open_region(path)
        if region is None:
            continue
        for cx, cz in _region_entries(path):
            try:
                chunk = region.read_chunk(cx, cz)
            except (OSError, RegionFormatError, NBTError):
                continue
            if not chunk:
                continue
            sections = _dict(chunk.get("Sections"))
            for _key, section in sections.items():
                node = _dict(section)
                for entry in _list(node.get("Records")):
                    record = _dict(entry)
                    kind = _text(record.get("type") or record.get("Type"))
                    pos = _list(record.get("pos") or record.get("Pos"))
                    if not kind or len(pos) < 3:
                        continue
                    # POI 的 pos 已经是世界坐标（不像 sections 是局部坐标）
                    records.append(
                        PoiRecord(
                            kind=kind,
                            x=_int(pos[0], 0) or 0,
                            y=_int(pos[1], 0) or 0,
                            z=_int(pos[2], 0) or 0,
                        )
                    )
                    total += 1
    return total


# ------------------------------------------------------------------ 玩家解析
def _parse_player_dat(path: Path) -> PlayerInfo | None:
    try:
        tree = load_dat(path)
    except (OSError, NBTError, ValueError):
        return None
    return _player_from_tree(tree, file_name=path.name)


def _player_from_tree(tree: dict, *, file_name: str = "") -> PlayerInfo:
    info = PlayerInfo(file_name=file_name)
    info.data_version = _int(tree.get("DataVersion"))
    for key in ("UUID", "uuid"):
        if key in tree:
            info.uuid = _uuid_from_ints(_list(tree[key]))
            if info.uuid:
                break
    if not info.uuid and file_name.endswith(".dat"):
        stem = file_name[:-4]
        try:
            parsed = uuidlib.UUID(stem)
        except ValueError:
            parsed = None
        # 文件名兜底也要排除全 0 UUID：离线启动器会拿它当占位符
        if parsed is not None and parsed.int:
            info.uuid = str(parsed)
    pos = _list(tree.get("Pos"))
    if len(pos) >= 3:
        info.pos = (
            _float(pos[0], 0.0) or 0.0,
            _float(pos[1], 0.0) or 0.0,
            _float(pos[2], 0.0) or 0.0,
        )
    rotation = _list(tree.get("Rotation"))
    if len(rotation) >= 2:
        info.rotation = (_float(rotation[0], 0.0) or 0.0, _float(rotation[1], 0.0) or 0.0)
    dimension = tree.get("Dimension")
    if isinstance(dimension, str):
        info.dimension = dimension
    elif isinstance(dimension, int):
        info.dimension = {
            -1: "minecraft:the_nether",
            0: "minecraft:overworld",
            1: "minecraft:the_end",
        }.get(dimension, str(dimension))
    info.health = _float(tree.get("Health"))
    info.food_level = _int(tree.get("foodLevel"))
    info.food_saturation = _float(tree.get("foodSaturationLevel"))
    info.xp_level = _int(tree.get("XpLevel"))
    info.xp_total = _int(tree.get("XpTotal"))
    info.game_type = _int(tree.get("playerGameType"))
    info.inventory = _parse_items(tree.get("Inventory") or tree.get("inventory"))
    info.ender_items = _parse_items(tree.get("EnderItems"))
    abilities = _dict(tree.get("abilities"))
    info.abilities = tuple(sorted(abilities.items())) if abilities else ()
    # 死亡计数：旧版直接存 Score
    info.deaths = _int(tree.get("Score"), 0) or 0
    return info


def _parse_advancements(path: Path) -> tuple[Advancement, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return ()
    if not isinstance(payload, dict):
        return ()
    out: list[Advancement] = []
    for key, node in payload.items():
        if key == "DataVersion" or not isinstance(node, dict):
            continue
        criteria = node.get("criteria")
        criteria = criteria if isinstance(criteria, dict) else {}
        latest: int | None = None
        for _name, stamp in criteria.items():
            parsed = _parse_adv_stamp(stamp)
            if parsed is not None and (latest is None or parsed > latest):
                latest = parsed
        out.append(
            Advancement(
                key=str(key),
                done=_bool(node.get("done")),
                finished_at=latest,
                criteria_done=len(criteria),
                criteria_total=len(criteria),
            )
        )
    out.sort(key=lambda item: (item.finished_at or 0), reverse=True)
    return tuple(out)


def _parse_adv_stamp(raw: Any) -> int | None:
    """进度的 ``criteria`` 值是 ``2026-11-21 12:00:00 +0800`` 这种 Java 时间串。"""
    if not isinstance(raw, str):
        return None
    try:
        return int(datetime.strptime(raw.strip(), _ADV_TIME).timestamp())
    except ValueError:
        pass
    # 有些第三方工具写 ISO8601
    try:
        return int(datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _apply_stats(player: PlayerInfo, path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return
    stats = _dict(_dict(payload).get("stats"))
    custom = _dict(stats.get("minecraft:custom"))
    player.play_time = _int(custom.get("minecraft:play_time"), 0) or 0
    player.total_world_time = _int(custom.get("minecraft:total_world_time"), 0) or 0
    player.time_since_death = _int(custom.get("minecraft:time_since_death"), 0) or 0
    player.walk_cm = _int(custom.get("minecraft:walk_one_cm"), 0) or 0
    player.sprint_cm = _int(custom.get("minecraft:sprint_one_cm"), 0) or 0
    player.jump_count = _int(custom.get("minecraft:jump"), 0) or 0
    player.deaths = max(player.deaths, _int(custom.get("minecraft:deaths"), 0) or 0)
    player.blocks_mined = sum(
        value for value in _dict(stats.get("minecraft:mined")).values() if isinstance(value, (int, float))
    )
    player.blocks_placed = sum(
        value for value in _dict(stats.get("minecraft:used")).values() if isinstance(value, (int, float))
    )
    killed = _dict(stats.get("minecraft:killed"))
    player.mob_kills = sum(value for value in killed.values() if isinstance(value, (int, float)))
    player.player_kills = sum(
        value
        for key, value in killed.items()
        if isinstance(value, (int, float)) and "player" in str(key)
    )
    categories = []
    for name, values in stats.items():
        if isinstance(values, dict):
            categories.append((str(name).split(":", 1)[-1], len(values)))
    categories.sort(key=lambda item: item[1], reverse=True)
    player.stat_categories = tuple(categories)


def _scan_players(save: Path) -> list[PlayerInfo]:
    """玩家数据：新版 ``players/data/``，旧版 ``playerdata/``。"""
    players: list[PlayerInfo] = []
    folders: list[Path] = [save / "players" / "data", save / "playerdata"]
    for folder in folders:
        if not folder.is_dir():
            continue
        try:
            files = sorted(folder.glob("*.dat"))
        except OSError:
            continue
        for path in files:
            if path.name.endswith("_old.dat"):
                continue
            info = _parse_player_dat(path)
            if info is None:
                continue
            players.append(info)
    # 进度与统计目录也有两种布局
    for player in players:
        stem = player.file_name[:-4] if player.file_name.endswith(".dat") else ""
        keys = [key for key in (player.uuid, stem) if key]
        for base_name in ("players/advancements", "advancements"):
            for key in keys:
                path = save / base_name / f"{key}.json"
                if path.is_file():
                    player.advancements = _parse_advancements(path)
                    break
        for base_name in ("players/stats", "stats"):
            for key in keys:
                path = save / base_name / f"{key}.json"
                if path.is_file():
                    _apply_stats(player, path)
                    break
    players.sort(key=lambda item: (item.play_time, item.xp_total or 0), reverse=True)
    return players


def _player_names(save: Path) -> dict[str, str]:
    """UUID → 玩家名。``usercache.json`` 在游戏根目录（存档的上一级）。"""
    cache = save.parent.parent / "usercache.json"
    out: dict[str, str] = {}
    try:
        payload = json.loads(cache.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return out
    for entry in _list(payload):
        node = _dict(entry)
        uid = _text(node.get("uuid")).lower()
        name = _text(node.get("name"))
        if uid and name:
            out[uid] = name
    return out


# ------------------------------------------------------------------ 地图解析
def _parse_maps(save: Path, limit: int) -> list[MapInfo]:
    maps: list[MapInfo] = []
    folder = save / "data"
    if not folder.is_dir():
        return maps
    entries: list[tuple[int, Path]] = []
    try:
        for path in folder.glob("map_*.dat"):
            match = re.match(r"map_(-?\d+)\.dat$", path.name)
            if match:
                entries.append((int(match.group(1)), path))
    except OSError:
        return maps
    entries.sort(key=lambda item: item[0])
    for map_id, path in entries[: max(limit, 0)]:
        try:
            tree = load_dat(path)
        except (OSError, NBTError, ValueError):
            continue
        data = _world_payload(tree) or _dict(tree.get("data"))
        if not data:
            continue
        dimension = data.get("dimension")
        info = MapInfo(
            map_id=map_id,
            file_name=path.name,
            dimension=_text(dimension) if isinstance(dimension, str) else str(dimension or ""),
            scale=_int(data.get("scale"), 0) or 0,
            locked=_bool(data.get("locked")),
            tracking=_bool(data.get("tracking"), True),
            center_x=_int(data.get("xCenter"), 0) or 0,
            center_z=_int(data.get("zCenter"), 0) or 0,
            width=_int(data.get("width"), 128) or 128,
            height=_int(data.get("height"), 128) or 128,
        )
        colors = data.get("colors") or data.get("Colors")
        if isinstance(colors, (bytes, bytearray)):
            info.colors = bytes(colors)
        elif isinstance(colors, list):
            info.colors = bytes(int(value) & 0xFF for value in colors if isinstance(value, (int, float)))
        decorations = []
        for entry in _list(data.get("Decorations") or data.get("decorations")):
            node = _dict(entry)
            decorations.append(
                (
                    _text(node.get("type")) or "marker",
                    _int(node.get("x"), 0) or 0,
                    _int(node.get("z"), 0) or 0,
                    _float(node.get("rot"), 0.0) or 0.0,
                )
            )
        info.decorations = tuple(decorations)
        maps.append(info)
    return maps


# ------------------------------------------------------------------ 日志读取
def _parse_logs(save: Path, level_name: str) -> list[TimelineEvent]:
    """读游戏根目录的 ``logs/latest.log``，挑出提到这个世界的行。

    存档目录本身**不含**日志（聊天记录、种子回显、`/gamerule` 输出都只在
    ``logs/`` 里），所以这里往上一级找。找不到就返回空列表，不报错。
    """
    game_dir = save.parent.parent
    path = game_dir / "logs" / "latest.log"
    if not path.is_file():
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-_LOG_LINES:]
    except OSError:
        return []
    events: list[TimelineEvent] = []
    needle = level_name.strip().lower()
    for line in lines:
        text = line.rstrip("\r\n")
        if not text:
            continue
        match = _LOG_STAMP.match(text)
        stamp: int | None = None
        body = text
        if match:
            day, month, year, clock, body = match.groups()
            month_number = _MONTHS.get(month.lower())
            if month_number:
                try:
                    stamp = int(
                        time.mktime(
                            time.strptime(
                                f"{year}-{month_number:02d}-{day} {clock}", "%Y-%m-%d %H:%M:%S"
                            )
                        )
                    )
                except (ValueError, OverflowError):
                    stamp = None
        if needle and needle in body.lower():
            events.append(TimelineEvent(when=stamp, kind="log", title=body.strip()))
        elif any(
            marker in body
            for marker in ("Saving chunks", "Saving and pausing", "Loading level", "Started serving")
        ):
            events.append(TimelineEvent(when=stamp, kind="log", title=body.strip()))
        if len(events) >= _LOG_KEEP:
            break
    return events


# ------------------------------------------------------------------ 时间线
def _build_timeline(
    *,
    level: LevelInfo,
    players: list[PlayerInfo],
    maps: list[MapInfo],
    activity: list[tuple[int, int]],
    tickets: tuple[tuple[str, int, int], ...],
    save: Path,
) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    if level.last_played:
        events.append(
            TimelineEvent(
                when=level.last_played // 1000,
                kind="save",
                title="最后一次保存",
                detail=f"数据版本 {level.data_version or '未知'}",
            )
        )
    for player in players:
        label = player.name or player.uuid[:8] or "未知玩家"
        if player.pos:
            x, y, z = player.pos
            events.append(
                TimelineEvent(
                    when=None,
                    kind="player",
                    title=f"{label} 最后位置",
                    detail=f"{x:.0f}, {y:.0f}, {z:.0f} · {player.dimension or '未知维度'}",
                )
            )
        for adv in player.advancements:
            if not adv.done or not adv.finished_at:
                continue
            events.append(
                TimelineEvent(
                    when=adv.finished_at,
                    kind="advancement",
                    title=f"{label} 达成「{adv.display_name}」",
                )
            )
        if player.play_time:
            events.append(
                TimelineEvent(
                    when=None,
                    kind="player",
                    title=f"{label} 游玩时长 {_human_ticks(player.play_time)}",
                    detail=f"死亡 {player.deaths} 次 · 跳跃 {player.jump_count} 次",
                )
            )
    for stamp, count in activity:
        events.append(
            TimelineEvent(
                when=stamp,
                kind="chunk",
                title=f"{datetime.fromtimestamp(stamp).strftime('%Y-%m-%d')} 保存了 {count} 个区块",
            )
        )
    if tickets:
        events.append(
            TimelineEvent(
                when=None,
                kind="world",
                title=f"{len(tickets)} 个区块被强制加载",
                detail="、".join(sorted({kind for kind, _, _ in tickets})[:4]),
            )
        )
    for map_info in maps:
        events.append(
            TimelineEvent(
                when=None,
                kind="map",
                title=f"地图 #{map_info.map_id}",
                detail=f"{map_info.scale_label} · 中心 {map_info.center_x}, {map_info.center_z}",
            )
        )
    # 排序：有绝对时间的按时间倒序排在最前；没有时间的（当前状态类）按插入
    # 顺序排在后面。不能统一用 ``(item.when or 0)`` 排序——那会把"没有时间"
    # 的事件全部排到末尾，但相对顺序变成随机的（Python 的 sort 稳定，可它们
    # 中间还夹着 0 值的时间戳事件）。
    timed = sorted((item for item in events if item.when), key=lambda item: item.when, reverse=True)
    untimed = [item for item in events if not item.when]
    return timed + untimed


# ------------------------------------------------------------------ 入口
def scan_save(
    save_dir: Path | str,
    options: SaveScanOptions | None = None,
    *,
    progress: _PROGRESS | None = None,
    cancel=None,
) -> SaveReport:
    """扫描一个存档目录，返回 :class:`SaveReport`。

    ``progress`` 是 ``(0..1, 文本)`` 回调（在工作线程里调用）；``cancel`` 是
    ``threading.Event``，置位后尽快返回已解析的部分。
    """
    options = options or SaveScanOptions()
    save = Path(save_dir)
    started = time.perf_counter()
    report = SaveReport(path=save, name=save.name)

    def step(fraction: float, text: str) -> None:
        if progress is not None:
            progress(fraction, text)

    def cancelled() -> bool:
        return cancel is not None and getattr(cancel, "is_set", lambda: False)()

    # ---- level.dat
    step(0.02, "读取 level.dat")
    level_tree: dict = {}
    level_path = save / "level.dat"
    if level_path.is_file():
        try:
            level_tree = load_dat(level_path)
            report.level = _level_from_tree(level_tree)
        except (OSError, NBTError, ValueError) as exc:
            report.warnings.append(f"level.dat 解析失败：{exc}")
    else:
        report.warnings.append("找不到 level.dat，这可能不是存档目录")
    report.name = report.level.level_name or save.name

    # ---- 存档体积
    step(0.06, "统计存档体积")
    total_bytes = 0
    for root_dir, _dirs, files in os.walk(save):
        for name in files:
            try:
                total_bytes += os.path.getsize(os.path.join(root_dir, name))
            except OSError:
                continue
    report.size_bytes = total_bytes

    # ---- 世界数据文件
    step(0.1, "读取世界设置")
    report.rules = _parse_rules(save, level_tree)
    report.border = _parse_border(save, level_tree)
    report.weather = _parse_weather(save, level_tree)
    report.clocks = _parse_clocks(save, level_tree)
    gen = _load_world_file(save, "world_gen_settings.dat")
    if gen is not None:
        report.level.split_world_data = True
        report.level.seed = _int(gen.get("seed")) if gen.get("seed") is not None else report.level.seed
        if gen.get("generate_structures") is not None:
            report.level.generate_structures = _bool(gen.get("generate_structures"))
        if gen.get("bonus_chest") is not None:
            report.level.bonus_chest = _bool(gen.get("bonus_chest"))
        generator = _generator_kind(_dict(gen.get("dimensions")).get("minecraft:overworld"))
        if generator:
            report.level.generator = generator
    if _load_world_file(save, "game_rules.dat") is not None:
        report.level.split_world_data = True
    tickets = _parse_tickets(save) if options.read_entities else ()
    report.forced_chunks = tickets

    # ---- 玩家
    step(0.16, "读取玩家数据")
    report.players = _scan_players(save)
    names = _player_names(save)
    for player in report.players:
        player.name = names.get(player.uuid.lower(), "") or player.name
    # 极老存档：玩家数据内嵌在 level.dat 的 Data.Player 里
    if not report.players:
        data = _dict(level_tree.get("Data")) or _dict(level_tree)
        embedded = _dict(data.get("Player"))
        if embedded:
            report.players.append(_player_from_tree(embedded, file_name="level.dat"))

    # ---- 地图
    step(0.22, "读取地图")
    report.maps = _parse_maps(save, options.map_limit if options.read_maps else 0)

    # ---- 维度与区块
    dimensions = _dimension_dirs(save)
    budget = [max(options.chunk_limit, 0) if options.chunk_limit else 10**9]
    block_budget = [_BLOCK_SAMPLE if options.read_chunks else 0]
    block_counter: Counter = Counter()
    biome_counter: Counter = Counter()
    ground_values: list[int] = []
    containers: list[Container] = []
    structures: list[StructureRef] = []
    chunk_stats: dict[str, Any] = {}
    activity: Counter = Counter()
    total_dims = max(len(dimensions), 1)

    for index, (key, folder) in enumerate(dimensions):
        if cancelled():
            report.warnings.append("扫描被取消，结果不完整")
            break
        step(0.25 + 0.6 * index / total_dims, f"扫描 {key}")
        paths = _dimension_paths(folder)
        dim = DimensionInfo(key=key, path=str(folder))
        region = paths.get("region")
        if region is not None:
            dim.has_region = True
            # 旧版布局：region 直接在存档根，同级还有 entities/poi
            for name in ("entities", "poi"):
                sibling = folder / name
                if sibling.is_dir() and name not in paths:
                    paths[name] = sibling
            if options.read_chunks:
                try:
                    parsed_before = int(chunk_stats.get("parsed", 0))
                    files, total, full = _scan_region_folder(
                        region,
                        budget=budget,
                        block_budget=block_budget,
                        block_counter=block_counter,
                        biome_counter=biome_counter,
                        ground_values=ground_values,
                        containers=containers,
                        structures=structures,
                        stats=chunk_stats,
                    )
                    parsed = int(chunk_stats.get("parsed", 0)) - parsed_before
                except (OSError, RegionFormatError, NBTError) as exc:
                    report.warnings.append(f"{key} 区块读取失败：{exc}")
                    files, total, full, parsed = (*_count_only(region), 0)
            else:
                files, total, full = _count_only(region)
                parsed = 0
            dim.region_files = files
            dim.chunk_total = total
            dim.chunk_full = full
            dim.chunk_parsed = parsed
            dim.surface_blocks = total * 256
            # 时间戳表：不用解压就能得到"哪天写过区块"
            activity.update(_region_activity(region))
        entities_dir = paths.get("entities")
        if options.read_entities and entities_dir is not None:
            groups: Counter = Counter()
            positions: dict[str, list] = {}
            dim.entities_chunk_files = _real_region_files(entities_dir)
            try:
                dim.entity_count = _collect_entities(entities_dir, groups, positions)
            except (OSError, RegionFormatError, NBTError) as exc:
                # 单个维度读坏了不能让整次扫描失败：记一条告警继续
                report.warnings.append(f"{key} 实体读取失败：{exc}")
            for entity_id, count in groups.most_common():
                report.entities.append(
                    EntityGroup(
                        entity_id=entity_id,
                        count=count,
                        positions=tuple(positions.get(entity_id, ())),
                    )
                )
        poi_dir = paths.get("poi")
        if options.read_poi and poi_dir is not None:
            records: list[PoiRecord] = []
            dim.poi_chunk_files = _real_region_files(poi_dir)
            try:
                dim.poi_count = _collect_poi(poi_dir, records)
            except (OSError, RegionFormatError, NBTError) as exc:
                report.warnings.append(f"{key} 兴趣点读取失败：{exc}")
            report.pois.extend(records)
        report.dimensions.append(dim)

    # ---- 汇总区块统计
    report.containers = containers
    report.structures = structures
    report.chunks = ChunkStats(
        count=sum(item.chunk_total for item in report.dimensions),
        full_count=sum(item.chunk_full for item in report.dimensions),
        top_blocks=tuple(block_counter.most_common(24)),
        biomes=tuple(biome_counter.most_common(24)),
        average_ground=(
            round(sum(ground_values) / len(ground_values), 1) if ground_values else None
        ),
        min_ground=min(ground_values) if ground_values else None,
        max_ground=max(ground_values) if ground_values else None,
        solid_blocks=int(chunk_stats.get("solid", 0)),
        broken=int(chunk_stats.get("broken", 0)),
        legacy=bool(chunk_stats.get("legacy")),
        sampled_chunks=max(_BLOCK_SAMPLE - block_budget[0], 0),
    )
    if budget[0] <= 0 and options.chunk_limit:
        report.warnings.append(
            f"区块较多，只解析了前 {options.chunk_limit} 个（完整区块数按已解析部分统计）"
        )

    # ---- 活跃日期
    report.activity = sorted(activity.items())

    # ---- 日志与时间线
    step(0.9, "整理时间线")
    report.logs = _parse_logs(save, report.name)
    report.timeline = _build_timeline(
        level=report.level,
        players=report.players,
        maps=report.maps,
        activity=report.activity,
        tickets=tickets,
        save=save,
    )[:_TIMELINE_MAX]

    # ---- 杂项文件
    extras: dict[str, str] = {}
    for name, label in (
        ("icon.png", "存档图标"),
        ("icon.ico", "存档图标（旧版）"),
        ("session.lock", "会话锁"),
        ("level.dat_old", "level.dat 备份"),
        ("datapacks", "存档数据包"),
        ("generated", "生成数据"),
    ):
        path = save / name
        if path.exists():
            extras[label] = str(path)
    report.extras = extras

    report.elapsed = time.perf_counter() - started
    step(1.0, "完成")
    return report


def _count_only(folder: Path) -> tuple[int, int, int]:
    """不解析区块，只数偏移表（"已生成区块数"用）。"""
    files = _region_files(folder)
    real_files = 0
    total = 0
    for path in files:
        region = _open_region(path)
        if region is None:
            continue  # 0 字节占位文件不算"有区域文件"
        real_files += 1
        try:
            header = region.read_header()
        except (OSError, RegionFormatError):
            continue
        for index in range(1024):
            if int.from_bytes(header[index * 4 : index * 4 + 3], "big") != 0:
                total += 1
    return real_files, total, 0


def _region_activity(folder: Path) -> Counter:
    """按天聚合区域文件时间戳表 → ``Counter({当天 0 点的 epoch 秒: 区块数})``。"""
    counter: Counter = Counter()
    for path in _region_files(folder):
        try:
            with open(path, "rb") as fh:
                fh.seek(4096)
                table = fh.read(4096)
        except OSError:
            continue
        if len(table) < 4096:
            continue
        for index in range(1024):
            stamp = struct.unpack_from(">i", table, index * 4)[0]
            if stamp <= 0:
                continue
            day = int(stamp) - int(stamp) % 86400
            counter[day] += 1
    return counter


def _region_activity(folder: Path) -> Counter:
    """按天聚合区域文件时间戳表 → ``Counter({当天 0 点的 epoch 秒: 区块数})``。"""
    counter: Counter = Counter()
    for path in _region_files(folder):
        try:
            with open(path, "rb") as fh:
                fh.seek(4096)
                table = fh.read(4096)
        except OSError:
            continue
        if len(table) < 4096:
            continue
        for index in range(1024):
            stamp = struct.unpack_from(">i", table, index * 4)[0]
            if stamp <= 0:
                continue
            day = int(stamp) - int(stamp) % 86400
            counter[day] += 1
    return counter


def save_summary(path: Path | str) -> str:
    """一句话概括存档（给列表/工具提示用，不解析区块）。"""
    report = scan_save(
        path,
        SaveScanOptions(read_chunks=False, read_entities=False, read_poi=False, read_maps=False),
    )
    level = report.level
    bits = [report.name, level.game_mode_name]
    if level.game_version:
        bits.append(level.game_version)
    if level.played_hours:
        bits.append(f"{level.played_hours:.1f} 小时")
    if report.total_chunks:
        bits.append(f"{report.total_chunks} 区块")
    return " · ".join(item for item in bits if item)
