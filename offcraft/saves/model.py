"""存档解析结果的纯数据模型。

这一层不碰 Qt、不碰文件系统：``report.scan_save()`` 负责读盘并把结果填成
这些 dataclass，界面只消费这些对象。这样解析逻辑可以直接单测，界面也能在没有
真实存档（用 ``tests/fixtures`` 里合成的存档）时跑冒烟。

约定：

* 所有时间都是 **Unix 秒**（除了 ``LevelInfo.last_played`` 是毫秒）；
* 所有坐标都是世界方块坐标；
* 拿不到的字段一律 ``None`` / 空容器，**不用 0 代替缺失值**——0 在坐标和
  时间上都是合法值，混用会让界面显示"最后游玩：1970"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "Advancement",
    "ChunkStats",
    "Container",
    "DimensionInfo",
    "EntityGroup",
    "GameRule",
    "ItemStack",
    "LevelInfo",
    "MapInfo",
    "PlayerInfo",
    "PoiRecord",
    "SaveReport",
    "SaveScanOptions",
    "SpawnPoint",
    "StructureRef",
    "TimelineEvent",
    "Weather",
    "WorldBorder",
    "WorldClock",
]


# ------------------------------------------------------------------ 世界元数据
@dataclass(frozen=True, slots=True)
class SpawnPoint:
    x: float
    y: float
    z: float
    yaw: float = 0.0
    dimension: str = "minecraft:overworld"
    forced: bool = False

    @property
    def block(self) -> tuple[int, int, int]:
        return (int(self.x // 1), int(self.y // 1), int(self.z // 1))


@dataclass(frozen=True, slots=True)
class WorldClock:
    """各维度独立时钟（新版 ``world_clocks.dat``）。"""

    dimension: str
    total_ticks: int

    @property
    def days(self) -> float:
        return self.total_ticks / 24000 if self.total_ticks else 0.0


@dataclass(frozen=True, slots=True)
class WorldBorder:
    center_x: float = 0.0
    center_z: float = 0.0
    size: float = 0.0
    damage_per_block: float = 0.0
    safe_zone: float = 0.0
    warning_blocks: int = 0
    warning_time: int = 0
    lerp_target: float = 0.0
    lerp_time: int = 0


@dataclass(frozen=True, slots=True)
class Weather:
    raining: bool = False
    thundering: bool = False
    rain_time: int = 0
    thunder_time: int = 0
    clear_weather_time: int = 0


@dataclass(frozen=True, slots=True)
class GameRule:
    """一条游戏规则。``value`` 保留原始类型（bool / int / str）。"""

    name: str
    value: Any

    @property
    def short_name(self) -> str:
        return self.name.split(":", 1)[-1]

    @property
    def display_value(self) -> str:
        if isinstance(self.value, bool):
            return "开" if self.value else "关"
        return str(self.value)


@dataclass(slots=True)
class LevelInfo:
    """``level.dat`` 里的 ``Data``：世界本身的元数据。"""

    data_version: int | None = None
    #: Anvil 格式版本（``version``，当前 19133）
    storage_version: int | None = None
    game_version: str = ""
    level_name: str = ""
    game_type: int = 0
    difficulty: str = ""
    hardcore: bool = False
    allow_commands: bool = False
    initialized: bool = True
    #: 世界总 tick（年龄）：``Time``
    time: int = 0
    #: 当日时间 0..24000（新版在 world_clocks.dat）
    day_time: int | None = None
    #: 最后游玩时间（epoch **毫秒**）
    last_played: int | None = None
    spawn: SpawnPoint | None = None
    seed: int | None = None
    generator: str = ""
    bonus_chest: bool = False
    generate_structures: bool = True
    server_brands: tuple[str, ...] = ()
    was_modded: bool = False
    data_packs: tuple[str, ...] = ()
    disabled_packs: tuple[str, ...] = ()
    singleplayer_uuid: str = ""
    #: 是否找到了拆分出来的新版世界数据文件
    split_world_data: bool = False

    @property
    def days(self) -> float:
        return self.time / 24000 if self.time else 0.0

    @property
    def played_hours(self) -> float:
        return self.time / 72000 if self.time else 0.0

    @property
    def game_mode_name(self) -> str:
        return {
            0: "生存",
            1: "创造",
            2: "冒险",
            3: "旁观",
        }.get(int(self.game_type), f"未知（{self.game_type}）")

    @property
    def difficulty_name(self) -> str:
        text = str(self.difficulty or "").lower()
        return {
            "peaceful": "和平",
            "easy": "简单",
            "normal": "普通",
            "hard": "困难",
        }.get(text, text or "未知")


# ------------------------------------------------------------------ 维度与区块
@dataclass(slots=True)
class DimensionInfo:
    """一个维度（主世界 / 下界 / 末地 / 自定义）的磁盘与统计信息。"""

    key: str
    #: 目录里的实际路径（用于"打开文件夹"）
    path: str = ""
    region_files: int = 0
    chunk_total: int = 0
    #: 实际解过 NBT 的区块数（受扫描预算限制，可能小于 ``chunk_total``）
    chunk_parsed: int = 0
    #: ``Status == "full"`` 的区块数（只统计 ``chunk_parsed`` 里的）
    chunk_full: int = 0
    entities_chunk_files: int = 0
    entity_count: int = 0
    poi_chunk_files: int = 0
    poi_count: int = 0
    #: 已生成区块覆盖的方块面积（= 区块数 × 256）
    surface_blocks: int = 0
    data_version: int | None = None
    #: ``None`` 表示该维度没有 region 目录
    has_region: bool = False

    @property
    def display_name(self) -> str:
        name = self.key.split(":", 1)[-1]
        return {
            "overworld": "主世界",
            "the_nether": "下界",
            "the_end": "末地",
        }.get(name, name)

    @property
    def surface_area(self) -> int:
        """已生成地表面积（平方米 = 方块数）。"""
        return self.chunk_total * 256


@dataclass(frozen=True, slots=True)
class ChunkStats:
    """区块聚合统计（只统计 ``full`` 的区块，避免把没生成完的算进"已探索"）。"""

    count: int = 0
    full_count: int = 0
    #: 最常出现的方块 (名字, 次数) 前 N 项
    top_blocks: tuple[tuple[str, int], ...] = ()
    #: 出现过的生物群系 (名字, 区段数)
    biomes: tuple[tuple[str, int], ...] = ()
    #: 平均地表高度（按 MOTION_BLOCKING 高度图）
    average_ground: float | None = None
    min_ground: int | None = None
    max_ground: int | None = None
    #: 非空气方块总数（估算，来自 sections 的采样）
    solid_blocks: int = 0
    #: 扫描跳过的损坏区块数
    broken: int = 0
    #: 参与方块统计的区块数（有预算，所以是采样值）
    sampled_chunks: int = 0
    #: 是否有区块缺失 yPos / 旧版字段（用于提示"老存档"）
    legacy: bool = False


@dataclass(frozen=True, slots=True)
class StructureRef:
    """``structures.starts`` 里的一个结构起点。"""

    structure: str
    chunk_x: int
    chunk_z: int

    @property
    def display_name(self) -> str:
        name = self.structure.split(":", 1)[-1]
        return name.replace("_", " ")


@dataclass(frozen=True, slots=True)
class PoiRecord:
    """兴趣点（床 / 工作站 / 传送门 / 会合点）。"""

    kind: str
    x: int
    y: int
    z: int

    @property
    def display_name(self) -> str:
        name = self.kind.split(":", 1)[-1]
        return {
            "home": "床",
            "meeting": "村庄会合点",
            "nether_portal": "下界传送门",
            "beehive": "蜂巢",
            "bee_nest": "蜂巢",
            "lightning_rod": "避雷针",
            "lodestone": "磁石",
        }.get(name, name.replace("_", " "))


# ------------------------------------------------------------------ 容器与实体
@dataclass(frozen=True, slots=True)
class ItemStack:
    """一件物品。1.20.5 起用 ``components``，之前用 ``tag``，两者都保留原文。"""

    slot: int
    item_id: str
    count: int = 1
    damage: int | None = None
    custom_name: str = ""
    #: ``{"minecraft:sharpness": 5}`` 这类附魔（新版 components / 旧版 tag 都已归一）
    enchantments: tuple[tuple[str, int], ...] = ()
    #: 原样保留的组件 / tag 树，界面不展示但可用于日后扩展
    extra: dict = field(default_factory=dict, hash=False, compare=False)

    @property
    def short_id(self) -> str:
        return self.item_id.split(":", 1)[-1]

    @property
    def display_name(self) -> str:
        return self.custom_name or self.short_id.replace("_", " ")


@dataclass(frozen=True, slots=True)
class Container:
    """方块容器（箱子 / 木桶 / 熔炉 / 漏斗 / 潜影盒 …）。"""

    kind: str
    x: int
    y: int
    z: int
    items: tuple[ItemStack, ...] = ()
    #: 未打开的战利品箱：游戏这时只存 ``LootTable`` + ``LootTableSeed``，
    #: ``Items`` 为空，内容要等玩家第一次打开才生成。这不是"空箱子"。
    loot_table: str = ""
    loot_seed: int | None = None
    #: 熔炉的 ``BurnTime``、酿造台的 ``BrewTime`` 之类
    extra: tuple[tuple[str, Any], ...] = ()

    @property
    def kind_name(self) -> str:
        return {
            "minecraft:chest": "箱子",
            "minecraft:trapped_chest": "陷阱箱",
            "minecraft:barrel": "木桶",
            "minecraft:furnace": "熔炉",
            "minecraft:blast_furnace": "高炉",
            "minecraft:smoker": "烟熏炉",
            "minecraft:hopper": "漏斗",
            "minecraft:dispenser": "发射器",
            "minecraft:dropper": "投掷器",
            "minecraft:brewing_stand": "酿造台",
            "minecraft:shulker_box": "潜影盒",
            "minecraft:chest_boat": "运输船",
            "minecraft:lectern": "讲台",
            "minecraft:decorated_pot": "饰纹陶罐",
            "minecraft:campfire": "营火",
            "minecraft:jukebox": "唱片机",
            "minecraft:crafter": "合成器",
            "minecraft:vault": "宝库",
        }.get(self.kind, self.kind.split(":", 1)[-1].replace("_", " "))

    @property
    def total_count(self) -> int:
        return sum(item.count for item in self.items)

    @property
    def is_loot_pending(self) -> bool:
        """是不是"还没打开过的战利品箱"（内容尚未生成）。"""
        return bool(self.loot_table) and not self.items

    @property
    def state_label(self) -> str:
        if self.is_loot_pending:
            return "未打开（内容未生成）"
        if not self.items:
            return "空"
        return f"{len(self.items)} 种"


@dataclass(frozen=True, slots=True)
class EntityGroup:
    """按类型聚合的实体（掉落物单独归一类）。"""

    entity_id: str
    count: int
    #: 前几个实体的坐标（界面用来"跳过去看一眼"）
    positions: tuple[tuple[float, float, float], ...] = ()

    @property
    def display_name(self) -> str:
        name = self.entity_id.split(":", 1)[-1]
        return {
            "item": "掉落物",
            "zombie": "僵尸",
            "skeleton": "骷髅",
            "creeper": "苦力怕",
            "spider": "蜘蛛",
            "enderman": "末影人",
            "villager": "村民",
            "cow": "牛",
            "pig": "猪",
            "sheep": "羊",
            "chicken": "鸡",
            "horse": "马",
            "wolf": "狼",
            "cat": "猫",
            "armor_stand": "盔甲架",
            "minecart": "矿车",
            "chest_minecart": "运输矿车",
            "furnace_minecart": "动力矿车",
            "hopper_minecart": "漏斗矿车",
            "tnt_minecart": "TNT 矿车",
            "boat": "船",
            "oak_boat": "橡木船",
            "experience_orb": "经验球",
            "falling_block": "下落方块",
            "item_frame": "物品展示框",
            "painting": "画",
            "arrow": "箭",
            "snow_golem": "雪傀儡",
            "iron_golem": "铁傀儡",
            "salmon": "鲑鱼",
            "cod": "鳕鱼",
            "tropical_fish": "热带鱼",
            "pufferfish": "河豚",
            "squid": "鱿鱼",
            "glow_squid": "发光鱿鱼",
            "bat": "蝙蝠",
            "drowned": "溺尸",
            "axolotl": "美西螈",
            "fox": "狐狸",
            "rabbit": "兔子",
            "bee": "蜜蜂",
            "endermite": "末影螨",
            "silverfish": "蠹虫",
            "wandering_trader": "流浪商人",
            "pillager": "掠夺者",
            "vindicator": "卫道士",
            "evoker": "唤魔者",
            "ravager": "劫掠兽",
            "warden": "监守者",
            "allay": "悦灵",
            "breeze": "微风",
        }.get(name, name.replace("_", " "))


# ------------------------------------------------------------------ 玩家
@dataclass(frozen=True, slots=True)
class Advancement:
    """一条进度。只有全部 criteria 达成时 ``done`` 才为真。"""

    key: str
    done: bool
    #: 最近一次完成条件的时间（epoch 秒）
    finished_at: int | None = None
    criteria_done: int = 0
    criteria_total: int = 0

    @property
    def display_name(self) -> str:
        return self.key.split(":", 1)[-1].replace("/", " / ").replace("_", " ")


@dataclass(slots=True)
class PlayerInfo:
    """``players/data/<uuid>.dat`` + 进度 + 统计。"""

    uuid: str = ""
    file_name: str = ""
    name: str = ""
    dimension: str = ""
    pos: tuple[float, float, float] | None = None
    rotation: tuple[float, float] | None = None
    health: float | None = None
    food_level: int | None = None
    food_saturation: float | None = None
    xp_level: int | None = None
    xp_total: int | None = None
    game_type: int | None = None
    data_version: int | None = None
    inventory: tuple[ItemStack, ...] = ()
    ender_items: tuple[ItemStack, ...] = ()
    abilities: tuple[tuple[str, Any], ...] = ()
    #: ``minecraft:custom`` 里的计时 / 行为统计
    play_time: int = 0
    total_world_time: int = 0
    time_since_death: int = 0
    walk_cm: int = 0
    sprint_cm: int = 0
    jump_count: int = 0
    deaths: int = 0
    mob_kills: int = 0
    player_kills: int = 0
    blocks_mined: int = 0
    blocks_placed: int = 0
    advancements: tuple[Advancement, ...] = ()
    stat_categories: tuple[tuple[str, int], ...] = ()
    #: 解析该玩家时遇到的非致命问题（缺进度文件等）
    notes: tuple[str, ...] = ()

    @property
    def play_hours(self) -> float:
        return self.play_time / 72000 if self.play_time else 0.0

    @property
    def advancement_count(self) -> int:
        return sum(1 for item in self.advancements if item.done)

    @property
    def game_mode_name(self) -> str:
        if self.game_type is None:
            return ""
        return {0: "生存", 1: "创造", 2: "冒险", 3: "旁观"}.get(
            int(self.game_type), f"未知（{self.game_type}）"
        )

    @property
    def inventory_count(self) -> int:
        return sum(item.count for item in self.inventory)


# ------------------------------------------------------------------ 地图
@dataclass(slots=True)
class MapInfo:
    """``data/map_<id>.dat``：已绘制的地图像素与装饰点。"""

    map_id: int
    file_name: str = ""
    dimension: str = ""
    scale: int = 0
    locked: bool = False
    tracking: bool = True
    center_x: int = 0
    center_z: int = 0
    width: int = 128
    height: int = 128
    colors: bytes = b""
    #: 装饰点：(类型, x, z, 旋转)
    decorations: tuple[tuple[str, int, int, float], ...] = ()

    @property
    def has_pixels(self) -> bool:
        return bool(self.colors)

    @property
    def scale_label(self) -> str:
        return f"1:{2 ** max(self.scale, 0)}"

    @property
    def span_blocks(self) -> int:
        return (128 << max(self.scale, 0))


# ------------------------------------------------------------------ 时间线
@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """一条"历史日志"。时间可能为 ``None``（只有 tick 序/顺序没有绝对时间）。"""

    when: int | None
    kind: str
    title: str
    detail: str = ""

    @property
    def icon(self) -> str:
        return {
            "save": "save",
            "advancement": "check",
            "death": "sword",
            "chunk": "blocks",
            "player": "person",
            "world": "globe",
            "map": "image",
        }.get(self.kind, "info")


# ------------------------------------------------------------------ 汇总报告
@dataclass(frozen=True, slots=True)
class SaveScanOptions:
    """扫描深度：默认值刻意保守，几万区块的存档也不会卡住界面。"""

    #: 是否解析区块 NBT（关掉就只读容器/时间戳，速度快一个数量级）
    read_chunks: bool = True
    #: 最多解析多少个区块（0 = 不限）
    chunk_limit: int = 4000
    #: 是否解析实体与 POI
    read_entities: bool = True
    read_poi: bool = True
    #: 是否解析地图像素
    read_maps: bool = True
    #: 最多展示多少张地图
    map_limit: int = 24


@dataclass(slots=True)
class SaveReport:
    """一次扫描的全部结果。界面拿到它就够渲染整页了。"""

    path: Path
    name: str = ""
    level: LevelInfo = field(default_factory=LevelInfo)
    dimensions: list[DimensionInfo] = field(default_factory=list)
    players: list[PlayerInfo] = field(default_factory=list)
    containers: list[Container] = field(default_factory=list)
    entities: list[EntityGroup] = field(default_factory=list)
    pois: list[PoiRecord] = field(default_factory=list)
    structures: list[StructureRef] = field(default_factory=list)
    maps: list[MapInfo] = field(default_factory=list)
    rules: list[GameRule] = field(default_factory=list)
    clocks: list[WorldClock] = field(default_factory=list)
    border: WorldBorder | None = None
    weather: Weather | None = None
    chunks: ChunkStats = field(default_factory=ChunkStats)
    timeline: list[TimelineEvent] = field(default_factory=list)
    #: 按天统计的区块写入次数 ``(当天 0 点的 epoch 秒, 区块数)``，升序。
    #: 来自区域文件的时间戳表，能在不解析任何区块的情况下画出"哪天玩过"。
    activity: list[tuple[int, int]] = field(default_factory=list)
    #: 日志里与世界相关的行（``logs/latest.log``，不在存档目录里）
    logs: list[TimelineEvent] = field(default_factory=list)
    #: 强制加载的区块票据 ``(类型, cx, cz)``
    forced_chunks: tuple[tuple[str, int, int], ...] = ()
    #: 磁盘占用
    size_bytes: int = 0
    #: 扫描耗时（秒）
    elapsed: float = 0.0
    #: 扫描过程中的告警 / 说明（例如"区块使用 LZ4，需要 lz4 库"）
    warnings: list[str] = field(default_factory=list)
    #: 存档目录里发现的其它文件（数据包、日志、图标…）
    extras: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------ 便利属性
    @property
    def full_chunks(self) -> int:
        return sum(item.chunk_full for item in self.dimensions)

    @property
    def total_chunks(self) -> int:
        return sum(item.chunk_total for item in self.dimensions)

    @property
    def total_entities(self) -> int:
        return sum(item.entity_count for item in self.dimensions)

    @property
    def total_container_items(self) -> int:
        return sum(item.total_count for item in self.containers)

    @property
    def total_inventory_items(self) -> int:
        return sum(player.inventory_count for player in self.players)

    @property
    def level_seed(self) -> int | None:
        return self.level.seed

    def dimension(self, key: str) -> DimensionInfo | None:
        return next((item for item in self.dimensions if item.key == key), None)
