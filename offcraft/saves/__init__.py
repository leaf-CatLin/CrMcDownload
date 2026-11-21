"""Minecraft Java 存档（Anvil）只读解析。

分层：``nbt``（NBT 二进制树）→ ``anvil``（区域文件扇区容器）→
``palette``（调色板 + 位打包）→ ``report``（语义汇总）。

这里只有读取，没有任何写回逻辑：启动器不在存档目录里做修改，
避免玩家在游戏运行时被工具写坏存档。
"""

from __future__ import annotations

from offcraft.saves.model import (
    Advancement,
    Container,
    DimensionInfo,
    ItemStack,
    LevelInfo,
    MapInfo,
    PlayerInfo,
    SaveReport,
    SaveScanOptions,
    SpawnPoint,
    TimelineEvent,
    Weather,
    WorldBorder,
    WorldClock,
)
from offcraft.saves.report import scan_save

__all__ = [
    "Advancement",
    "Container",
    "DimensionInfo",
    "ItemStack",
    "LevelInfo",
    "MapInfo",
    "PlayerInfo",
    "SaveReport",
    "SaveScanOptions",
    "SpawnPoint",
    "TimelineEvent",
    "Weather",
    "WorldBorder",
    "WorldClock",
    "scan_save",
]
