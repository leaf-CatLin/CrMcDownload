"""合成一个最小的 Minecraft 存档，用来测解析器（不需要游戏本体）。

真实存档不适合进仓库（几个 G），而解析器的正确性又必须在"真的有 GZIP +
扇区 + 位打包"的数据上验证，所以在测试里现造一份：

* ``level.dat``：GZIP 的 NBT，带 1.20.4 的 DataVersion 与新式 ``spawn`` /
  ``difficulty_settings``；
* ``data/minecraft/`` 下的 ``game_rules.dat`` / ``world_gen_settings.dat`` /
  ``weather.dat`` / ``world_borders.dat`` / ``world_clocks.dat``；
* ``data/map_0.dat``：一张 128×128 的地图（颜色字节按公式生成）；
* ``dimensions/minecraft/overworld/region/r.0.0.mca``：**真实扇区布局**的区域
  文件，含两个区块（16 个区段 + 实体 + 高度图 + 箱子）；
* ``dimensions/.../entities`` 与 ``poi`` 各一个区域文件；
* ``players/data/<uuid>.dat`` + 进度 + 统计 JSON。

返回 :class:`SaveFixture`，测试可以直接断言里面的已知值。
"""

from __future__ import annotations

import gzip
import json
import struct
import uuid as uuidlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from offcraft.saves.nbt import write_nbt
from offcraft.saves.palette import encode_heightmap, pack_indices

__all__ = ["SaveFixture", "build_save"]

VANILLA_UUID = "1c1d9d4e-8f5a-4a6f-9d1b-2f2c3d4e5f60"
CHUNK_X, CHUNK_Z = 0, 0

#: 一个有坐标常识的高度：地面在 y=64 附近
GROUND_Y = 64


def _gzip_nbt(tree: dict) -> bytes:
    return gzip.compress(write_nbt(tree))


def _write_gzip(path: Path, tree: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_gzip_nbt(tree))


def _section_block(
    *,
    y: int,
    palette: list[str],
    indices: list[int],
    biomes: list[str],
    biome_indices: list[int] | None = None,
) -> dict:
    """按新版结构造一个 section（palette + 位打包 data）。"""
    bits = max(4, (len(palette) - 1).bit_length())
    long_data = pack_indices(indices, bits)
    out: dict[str, Any] = {
        "Y": y,
        "block_states": {
            "palette": [{"Name": name} for name in palette],
        },
    }
    if len(palette) > 1:
        out["block_states"]["data"] = long_data
    biome_bits = max(1, (len(biomes) - 1).bit_length())
    out["biomes"] = {"palette": biomes}
    if len(biomes) > 1:
        out["biomes"]["data"] = pack_indices(
            biome_indices or [0] * 64, biome_bits
        )
    return out


def _chunk_tree(
    *,
    cx: int,
    cz: int,
    status: str = "minecraft:full",
    with_chest: bool = True,
    with_structure: bool = True,
    data_version: int = 3700,
) -> dict:
    """造一个区块：岩石地面 + 草方块 + 空气，外加高度图与方块实体。"""
    sections: list[dict] = []
    for section_y in range(-4, 20):
        base_y = section_y * 16
        if base_y + 15 < GROUND_Y - 4:
            # 深层：全石头（palette 只有一项 → data 字段不存在）
            sections.append(
                _section_block(
                    y=section_y,
                    palette=["minecraft:stone"],
                    indices=[0] * 4096,
                    biomes=["minecraft:plains"],
                )
            )
        elif base_y <= GROUND_Y < base_y + 16:
            # 地表区段：石头 / 泥土 / 草方块 / 空气
            palette = ["minecraft:air", "minecraft:stone", "minecraft:dirt", "minecraft:grass_block"]
            indices = []
            for y in range(16):
                world_y = base_y + y
                for _z in range(16):
                    for _x in range(16):
                        if world_y < GROUND_Y - 3:
                            indices.append(1)
                        elif world_y < GROUND_Y:
                            indices.append(2)
                        elif world_y == GROUND_Y:
                            indices.append(3)
                        else:
                            indices.append(0)
            sections.append(
                _section_block(
                    y=section_y,
                    palette=palette,
                    indices=indices,
                    biomes=["minecraft:plains", "minecraft:forest"],
                    biome_indices=[0] * 48 + [1] * 16,
                )
            )
        else:
            sections.append(
                _section_block(
                    y=section_y,
                    palette=["minecraft:air"],
                    indices=[0] * 4096,
                    biomes=["minecraft:plains"],
                )
            )

    # 1.18+ 高度图存的是"相对世界最低 Y 的偏移"：最低区段 yPos=-4 → 世界底 -64，
    # 所以地面 Y=64 的"地表上方第一格空气"= 65，存进去要 +64 = 129。
    min_y = -4 * 16
    heightmap = encode_heightmap([GROUND_Y + 1 - min_y] * 256)
    tree: dict[str, Any] = {
        "DataVersion": data_version,
        "xPos": cx,
        "zPos": cz,
        "yPos": -4,
        "Status": status,
        "LastUpdate": 1234567,
        "InhabitedTime": 20 * 60 * 60,
        "isLightOn": 1,
        "sections": sections,
        "Heightmaps": {"MOTION_BLOCKING": heightmap, "WORLD_SURFACE": heightmap},
        "block_entities": [],
    }
    if with_chest:
        tree["block_entities"].append(
            {
                "id": "minecraft:chest",
                "x": 1,
                "y": GROUND_Y + 1,
                "z": 2,
                "Items": [
                    {
                        "Slot": 0,
                        "id": "minecraft:diamond_sword",
                        "count": 1,
                        "components": {
                            "minecraft:damage": 12,
                            "minecraft:custom_name": '{"text":"Blade"}',
                            "minecraft:enchantments": {
                                "levels": {"minecraft:sharpness": 5}
                            },
                        },
                    },
                    {
                        "Slot": 1,
                        "id": "minecraft:oak_planks",
                        "count": 32,
                    },
                ],
            }
        )
        # 未打开的战利品箱：真实存档里绝大多数箱子都是这种形态
        # （只有 LootTable + LootTableSeed，没有 Items）
        tree["block_entities"].append(
            {
                "id": "minecraft:chest",
                "x": 3,
                "y": GROUND_Y + 1,
                "z": 4,
                "LootTable": "minecraft:chests/simple_dungeon",
                "LootTableSeed": -6853588384818761955,
                "components": {},
                "keepPacked": 0,
            }
        )
    if with_structure:
        tree["structures"] = {
            "starts": {
                "minecraft:village_plains": [{"chunkX": cx, "chunkZ": cz, "id": "minecraft:village_plains"}],
                "minecraft:mineshaft": [],
            },
            "References": {},
        }
    return tree


def _write_region(
    path: Path,
    chunks: list[tuple[int, int, dict]],
    *,
    timestamp: int | list[int] = 1_700_000_000,
) -> None:
    """按 Anvil 真实布局写区域文件（偏移表 / 时间戳表 / 4 KiB 扇区）。

    ``timestamp`` 可以是单个值，也可以是逐个区块的列表——后者用来造"跨多天
    都有区块写入"的存档，好验证活跃日历。
    """
    payloads: list[tuple[int, int, bytes]] = []
    for cx, cz, tree in chunks:
        raw = write_nbt(tree)
        compressed = gzip.compress(raw)
        body = struct.pack(">i", len(compressed) + 1) + b"\x01" + compressed
        # 补齐到扇区边界
        sectors = (len(body) + 4095) // 4096
        body += b"\x00" * (sectors * 4096 - len(body))
        payloads.append((cx, cz, body))

    stamps = timestamp if isinstance(timestamp, list) else [timestamp] * len(payloads)
    header = bytearray(8192)
    offset = 2  # 前两个扇区是头部
    for index_in_file, (cx, cz, body) in enumerate(payloads):
        index = (cx & 31) + (cz & 31) * 32
        sectors = len(body) // 4096
        header[index * 4 : index * 4 + 3] = offset.to_bytes(3, "big")
        header[index * 4 + 3] = sectors
        value = stamps[min(index_in_file, len(stamps) - 1)]
        struct.pack_into(">i", header, 4096 + index * 4, value)
        offset += sectors
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(bytes(header))
        for _cx, _cz, body in payloads:
            fh.write(body)


def _player_tree() -> dict:
    return {
        "DataVersion": 3700,
        "UUID": [0x1C1D9D4E, 0x8F5A4A6F, 0x9D1B2F2C, 0x3D4E5F60],
        "Pos": [8.5, 65.0, 8.5],
        "Rotation": [90.0, 0.0],
        "Dimension": "minecraft:overworld",
        "Health": 20.0,
        "foodLevel": 17,
        "foodSaturationLevel": 2.5,
        "XpLevel": 30,
        "XpTotal": 1395,
        "playerGameType": 0,
        "Score": 3,
        "abilities": {
            "walkSpeed": 0.1,
            "flySpeed": 0.05,
            "mayfly": 0,
            "flying": 0,
            "invulnerable": 0,
            "instabuild": 0,
        },
        "Inventory": [
            {
                "Slot": 0,
                "id": "minecraft:diamond_pickaxe",
                "count": 1,
                "components": {
                    "minecraft:damage": 4,
                    "minecraft:enchantments": {
                        "levels": {"minecraft:efficiency": 4, "minecraft:unbreaking": 3}
                    },
                },
            },
            {
                "Slot": 1,
                "id": "minecraft:bread",
                "count": 12,
            },
            {
                "Slot": 103,
                "id": "minecraft:netherite_helmet",
                "count": 1,
                "tag": {"Enchantments": [{"id": "minecraft:protection", "lvl": 4}]},
            },
        ],
        "EnderItems": [
            {"Slot": 0, "id": "minecraft:shulker_box", "count": 1},
        ],
    }


def _advancements_payload() -> dict:
    return {
        "minecraft:story/mine_stone": {
            "criteria": {"get_stone": "2026-11-20 20:11:03 +0800"},
            "done": True,
        },
        "minecraft:story/smelt_iron": {
            "criteria": {"get_iron": "2026-11-20 21:02:44 +0800"},
            "done": True,
        },
        "minecraft:adventure/kill_a_mob": {
            "criteria": {"kill": "2026-11-21 09:15:00 +0800"},
            "done": False,
        },
        "DataVersion": 3700,
    }


def _stats_payload() -> dict:
    return {
        "stats": {
            "minecraft:custom": {
                "minecraft:play_time": 72000,
                "minecraft:total_world_time": 79000,
                "minecraft:time_since_death": 3600,
                "minecraft:walk_one_cm": 125000,
                "minecraft:sprint_one_cm": 40000,
                "minecraft:jump": 512,
                "minecraft:deaths": 3,
                "minecraft:leave_game": 12,
            },
            "minecraft:mined": {"minecraft:stone": 480, "minecraft:dirt": 96},
            "minecraft:crafted": {"minecraft:crafting_table": 1, "minecraft:torch": 64},
            "minecraft:used": {"minecraft:diamond_pickaxe": 210},
            "minecraft:killed": {"minecraft:zombie": 30, "minecraft:skeleton": 12},
            "minecraft:killed_by": {"minecraft:creeper": 1},
        },
        "DataVersion": 3700,
    }


def _entity_chunk_tree(cx: int, cz: int) -> dict:
    return {
        "DataVersion": 3700,
        "Position": [cx, cz],
        "Entities": [
            {
                "id": "minecraft:zombie",
                "Pos": [4.5, 65.0, 4.5],
                "Health": 20.0,
                "UUID": [1, 2, 3, 4],
            },
            {
                "id": "minecraft:zombie",
                "Pos": [9.5, 65.0, 9.5],
                "Health": 20.0,
                "UUID": [1, 2, 3, 5],
            },
            {
                "id": "minecraft:item",
                "Pos": [8.2, 64.2, 8.8],
                "Item": {"id": "minecraft:rotten_flesh", "count": 2},
            },
            {
                "id": "minecraft:armor_stand",
                "Pos": [2.0, 65.0, 2.0],
            },
        ],
    }


def _poi_chunk_tree() -> dict:
    return {
        "DataVersion": 3700,
        "Sections": {
            "0": {
                "Valid": 1,
                "Records": [
                    {"type": "minecraft:home", "free_tickets": 1, "pos": [3, 65, 4]},
                    {"type": "minecraft:meeting", "free_tickets": 0, "pos": [8, 65, 8]},
                ],
            },
            "4": {
                "Valid": 1,
                "Records": [
                    {"type": "minecraft:nether_portal", "free_tickets": 1, "pos": [6, 90, 6]},
                ],
            },
        },
    }


def _map_payload() -> dict:
    # 128×128 的像素：上半草地（基础色 1，明暗档 2），下半水（基础色 12，明暗档 2）
    colors = bytearray()
    for y in range(128):
        for _x in range(128):
            if y < 64:
                colors.append(1 * 4 + 2)
            else:
                colors.append(12 * 4 + 2)
    return {
        "DataVersion": 3700,
        "data": {
            "version": 2,
            "dimension": "minecraft:overworld",
            "scale": 1,
            "locked": 0,
            "trackingPosition": 1,
            "xCenter": 0,
            "zCenter": 0,
            "width": 128,
            "height": 128,
            "colors": bytes(colors),
            "Decorations": [
                {"type": "frame", "x": 8, "z": 8, "rot": 0.0},
            ],
        },
    }


@dataclass(slots=True)
class SaveFixture:
    """造好的合成存档 + 已知的期望值（测试直接断言这些）。"""

    root: Path
    uuid: str = VANILLA_UUID
    level_name: str = "存档测试世界"
    seed: int = 860412345678901234
    chest_item_count: int = 2
    inventory_items: int = 3
    entity_total: int = 4
    poi_total: int = 3
    chunk_count: int = 2
    ground_y: int = GROUND_Y
    paths: dict[str, Path] = field(default_factory=dict)

    @property
    def level_dat(self) -> Path:
        return self.root / "level.dat"

    @property
    def region(self) -> Path:
        return self.root / "dimensions" / "minecraft" / "overworld" / "region" / "r.0.0.mca"


def build_save(root: Path | str, *, seed: int | None = None, data_version: int = 3700) -> SaveFixture:
    """在 ``root`` 下造一份完整的合成存档。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    fixture = SaveFixture(root=root)
    seed = fixture.seed if seed is None else seed

    # ---- level.dat
    _write_gzip(
        root / "level.dat",
        {
            "Data": {
                "DataVersion": data_version,
                "version": 19133,
                "Version": {"Id": data_version, "Name": "1.20.4", "Series": "main"},
                "LevelName": fixture.level_name,
                "GameType": 0,
                "Difficulty": 2,
                "difficulty_settings": {"difficulty": "normal", "hardcore": 0, "locked": 0},
                "allowCommands": 1,
                "initialized": 1,
                "Time": 720000,
                "LastPlayed": 1_763_700_000_000,
                "spawn": {
                    "pos": [8.0, 65.0, 8.0],
                    "dimension": "minecraft:overworld",
                    "yaw": 0.0,
                    "pitch": 0.0,
                },
                "ServerBrands": ["vanilla"],
                "WasModded": 0,
                "DataPacks": {"Enabled": ["vanilla"], "Disabled": []},
                "WorldGenSettings": {
                    "seed": seed,
                    "bonus_chest": 0,
                    "generate_structures": 1,
                    "dimensions": {
                        "minecraft:overworld": {
                            "type": "minecraft:overworld",
                            "generator": {"type": "minecraft:noise", "settings": "minecraft:overworld"},
                        }
                    },
                },
            }
        },
    )

    # ---- 新版拆分文件
    data_dir = root / "data" / "minecraft"
    _write_gzip(
        data_dir / "game_rules.dat",
        {
            "data": {
                "minecraft:keep_inventory": 0,
                "minecraft:do_daylight_cycle": 1,
                "minecraft:random_tick_speed": 3,
                "minecraft:do_fire_tick": 1,
                "minecraft:pvp": 1,
            },
            "DataVersion": data_version,
        },
    )
    _write_gzip(
        data_dir / "world_gen_settings.dat",
        {
            "data": {
                "seed": seed,
                "bonus_chest": 0,
                "generate_structures": 1,
                "dimensions": {
                    "minecraft:overworld": {
                        "type": "minecraft:overworld",
                        "generator": {"type": "minecraft:noise"},
                    }
                },
            },
            "DataVersion": data_version,
        },
    )
    _write_gzip(
        data_dir / "weather.dat",
        {
            "data": {
                "rain_time": 1200,
                "raining": 1,
                "thunder_time": 300,
                "thundering": 0,
                "clear_weather_time": 0,
            },
            "DataVersion": data_version,
        },
    )
    _write_gzip(
        data_dir / "world_clocks.dat",
        {
            "data": {
                "minecraft:overworld": {"total_ticks": 720000},
                "minecraft:the_nether": {"total_ticks": 710000},
            },
            "DataVersion": data_version,
        },
    )
    _write_gzip(
        data_dir / "world_border.dat",
        {
            "data": {
                "center_x": 0.0,
                "center_z": 0.0,
                "size": 29999984.0,
                "damage_per_block": 0.2,
                "safe_zone": 5.0,
                "warning_blocks": 5,
                "warning_time": 15,
                "lerp_target": 29999984.0,
                "lerp_time": 0,
            },
            "DataVersion": data_version,
        },
    )
    _write_gzip(root / "data" / "map_0.dat", _map_payload())

    # ---- 区域文件（主世界）
    # 时间戳故意跨 4 天：既验证时间戳表解析，也让活跃日历有东西可画
    overworld = root / "dimensions" / "minecraft" / "overworld"
    chunks = [
        (CHUNK_X, CHUNK_Z, _chunk_tree(cx=CHUNK_X, cz=CHUNK_Z)),
        (CHUNK_X + 1, CHUNK_Z, _chunk_tree(cx=CHUNK_X + 1, cz=CHUNK_Z, with_chest=False)),
    ]
    _write_region(
        overworld / "region" / "r.0.0.mca",
        chunks,
        timestamp=[1_700_000_000, 1_700_000_000 + 3 * 86400],
    )
    _write_region(
        overworld / "entities" / "r.0.0.mca",
        [(CHUNK_X, CHUNK_Z, _entity_chunk_tree(CHUNK_X, CHUNK_Z))],
    )
    _write_region(overworld / "poi" / "r.0.0.mca", [(CHUNK_X, CHUNK_Z, _poi_chunk_tree())])

    # ---- 玩家
    uuid = fixture.uuid
    _write_gzip(root / "players" / "data" / f"{uuid}.dat", _player_tree())
    adv = root / "players" / "advancements" / f"{uuid}.json"
    adv.parent.mkdir(parents=True, exist_ok=True)
    adv.write_text(json.dumps(_advancements_payload(), ensure_ascii=False), encoding="utf-8")
    stats = root / "players" / "stats" / f"{uuid}.json"
    stats.parent.mkdir(parents=True, exist_ok=True)
    stats.write_text(json.dumps(_stats_payload(), ensure_ascii=False), encoding="utf-8")

    # ---- 杂项
    (root / "session.lock").write_bytes(struct.pack(">q", 1_700_000_000_000))
    (root / "level.dat_old").write_bytes(_gzip_nbt({"Data": {"DataVersion": data_version}}))

    fixture.paths = {
        "level": root / "level.dat",
        "region": overworld / "region" / "r.0.0.mca",
        "entities": overworld / "entities" / "r.0.0.mca",
        "poi": overworld / "poi" / "r.0.0.mca",
        "player": root / "players" / "data" / f"{uuid}.dat",
        "map": root / "data" / "map_0.dat",
    }
    return fixture


def build_legacy_save(root: Path | str) -> SaveFixture:
    """造一份 1.12 风格的老存档：``playerdata/``、``region/`` 在根、字段内嵌。

    用来验证"两套路径都要兼容"：没有 ``dimensions/``、没有拆分文件，
    游戏规则/出生点/玩家都在 ``level.dat`` 里。
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    fixture = SaveFixture(root=root, level_name="老存档", chunk_count=1)
    _write_gzip(
        root / "level.dat",
        {
            "Data": {
                "DataVersion": 1343,
                "version": 19132,
                "LevelName": "老存档",
                "GameType": 1,
                "Difficulty": 3,
                "hardcore": 1,
                "allowCommands": 0,
                "Time": 24000,
                "DayTime": 1000,
                "LastPlayed": 1_500_000_000_000,
                "SpawnX": 0,
                "SpawnY": 64,
                "SpawnZ": 0,
                "RandomSeed": 42,
                "MapFeatures": 1,
                "GameRules": {"doFireTick": "true", "keepInventory": "false"},
                "BorderSize": 1000.0,
                "BorderCenterX": 0.0,
                "BorderCenterZ": 0.0,
            }
        },
    )
    # 老版本 region 直接在存档根，区块没有 yPos，sections 用旧键
    chunk = _chunk_tree(cx=0, cz=0, data_version=1343, with_chest=False, with_structure=False)
    chunk.pop("yPos", None)
    _write_region(root / "region" / "r.0.0.mca", [(0, 0, chunk)])
    _write_gzip(root / "playerdata" / f"{fixture.uuid}.dat", _player_tree())
    fixture.paths = {"level": root / "level.dat", "region": root / "region" / "r.0.0.mca"}
    return fixture
