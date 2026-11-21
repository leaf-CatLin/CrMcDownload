"""本地皮肤资源包：把用户的皮肤写成一个资源包，离线可用、不需要任何 mod。

这是 PCL 的做法：与其依赖 CustomSkinLoader 之类的皮肤 mod + 服务端接口，
不如直接生成一个**资源包**覆盖原版皮肤贴图。优点是：

1. 完全本地——皮肤文件只存在本机 ``<数据目录>/skins/``，不上传、不联网；
2. 原版实例也能用（资源包不挑加载器，不需要 Fabric/Forge）；
3. 走玩家已经熟悉的资源包机制，冲突时用户能在游戏内自己关掉。

资源包结构（``resourcepacks/CrMCDownloadSkin/``）::

    pack.mcmeta
    pack.png
    assets/minecraft/textures/entity/player/
        steve.png        ← classic（4px 手臂）
        alex.png         ← slim（3px 手臂）
        wide/steve.png   ← 1.19.2+ 的新布局（同样覆盖，兼容新版本）

``pack.mcmeta`` 的 ``pack_format`` 必须随游戏版本变化，否则游戏不认；这里按
主版本查表，未知版本退到一个宽松的默认值。
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

__all__ = [
    "PACK_DIR_NAME",
    "build_skin_pack",
    "pack_format_for",
    "remove_skin_pack",
    "skin_pack_path",
]

#: 资源包目录名（放在 ``<游戏目录>/resourcepacks/`` 下）
PACK_DIR_NAME = "CrMCDownloadSkin"

#: 资源包内的皮肤贴图相对路径（原版玩家皮肤的落点）
CLASSIC_TARGET = Path("assets/minecraft/textures/entity/player/steve.png")
SLIM_TARGET = Path("assets/minecraft/textures/entity/player/alex.png")
#: 1.19.2 之后原版把玩家贴图挪进了 ``player/wide/`` 与 ``player/slim/``，
#: 两边都写一份，新旧版本都能覆盖到。
WIDE_TARGET = Path("assets/minecraft/textures/entity/player/wide/steve.png")

#: 游戏「大版本」→ 资源包 pack_format。
#:
#: 数据来自 Minecraft Wiki 的资源包版本表；这是**覆盖玩家皮肤**用的，
#: format 必须落在游戏接受的范围内，否则资源包会被标成「不兼容」而默认不启用。
_PACK_FORMATS = {
    1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1,
    7: 3, 8: 3, 9: 3, 10: 3, 11: 3,
    12: 4, 13: 4, 14: 4,
    15: 5, 16: 5,
    17: 7, 18: 8,
    19: 12,   # 1.19.x 内部有 9/12 两档，取 12（1.19.3+）
    20: 15,   # 1.20–1.20.1
    21: 34,   # 1.21–1.21.1
    22: 42,   # 1.21.4 附近
    23: 46,   # 1.21.6 附近
    24: 55,   # 1.21.9 附近
    25: 63,   # 兜底：更新的版本
}
#: 查不到时使用的 format：取一个较新的值，游戏通常只会提示「为更新版本制作」
_DEFAULT_FORMAT = 15


def _major_of(mc_version: str) -> int | None:
    """``1.20.1`` → 20；``1.7.10`` → 7；无法解析返回 ``None``。"""
    text = str(mc_version or "").strip()
    parts = text.split(".")
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except (TypeError, ValueError):
        return None


def pack_format_for(mc_version: str) -> int:
    """按 MC 版本给出 ``pack_format``（未知版本退到默认值）。"""
    major = _major_of(mc_version)
    if major is None:
        return _DEFAULT_FORMAT
    # 1.20.5+ 的 format 变化很碎，这里按大版本查表；查不到就用默认
    return _PACK_FORMATS.get(major, _DEFAULT_FORMAT)


def skin_pack_path(game_dir: Path | str) -> Path:
    """资源包在磁盘上的位置。"""
    return Path(game_dir) / "resourcepacks" / PACK_DIR_NAME


def _png_size(data: bytes) -> tuple[int, int] | None:
    """读 PNG 的 IHDR 拿宽高（只认 PNG，不做完整解码）。"""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _pack_icon(size: int = 64) -> bytes:
    """生成一个纯色 ``pack.png``（资源包列表里的小图标，避免没有图标时的白块）。

    直接手写最小 PNG，不引入 Pillow 依赖。
    """
    size = max(8, int(size))
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # 每行的 filter type
        for x in range(size):
            # 简单的对角渐变，辨识度够用
            shade = 60 + int(120 * (x + y) / (2 * size))
            rows.extend((shade, shade + 20, min(255, shade + 60)))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", header),
            chunk(b"IDAT", zlib.compress(bytes(rows), 9)),
            chunk(b"IEND", b""),
        ]
    )


def build_skin_pack(
    game_dir: Path | str,
    data: bytes,
    *,
    model: str = "classic",
    mc_version: str = "",
) -> Path:
    """把皮肤写成资源包，返回资源包目录。

    ``data`` 是 64×64 或 64×32 的 PNG 皮肤；``model`` 为 ``classic`` / ``slim``。
    同时写入经典与纤细两套目标文件——玩家的皮肤模型在游戏内可能被切换，
    两份都覆盖才不会出现「切成纤细后变成默认皮肤」。
    """
    root = skin_pack_path(game_dir)
    root.mkdir(parents=True, exist_ok=True)

    meta = {
        "pack": {
            "pack_format": pack_format_for(mc_version),
            "description": "CrMCDownload 本地皮肤（离线可用）",
        }
    }
    (root / "pack.mcmeta").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (root / "pack.png").write_bytes(_pack_icon())

    # 两套模型 × 新旧布局：全都写同一份皮肤，保证任何版本/模型下都能覆盖
    targets = [CLASSIC_TARGET, SLIM_TARGET, WIDE_TARGET]
    for relative in targets:
        dest = root / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return root


def remove_skin_pack(game_dir: Path | str) -> bool:
    """删掉本地皮肤资源包；返回是否真的删了东西（没装的实例调用也不报错）。"""
    import shutil

    root = skin_pack_path(game_dir)
    if not root.exists():
        return False
    try:
        shutil.rmtree(root)
        return True
    except OSError:
        return False
