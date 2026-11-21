"""本地皮肤资源包：把皮肤写成资源包（PCL 做法，完全离线）。

覆盖三件事：

1. ``pack_format_for`` 按 MC 版本查表（未知版本退到默认值）；
2. ``build_skin_pack`` 生成合法的资源包目录结构，且**经典/纤细/新布局**
   三个贴图落点都写入同一份皮肤（否则切成纤细模型会退回默认皮肤）；
3. ``remove_skin_pack`` 幂等：没装过的目录调用也不报错。
"""

import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from offcraft.skins.pack import (
    CLASSIC_TARGET,
    PACK_DIR_NAME,
    SLIM_TARGET,
    WIDE_TARGET,
    build_skin_pack,
    pack_format_for,
    remove_skin_pack,
    skin_pack_path,
)


def _png(width: int, height: int) -> bytes:
    """造一个最小合法 PNG（纯色），避免测试依赖 Pillow。"""
    rows = bytearray()
    for _y in range(height):
        rows.append(0)  # filter type
        for _x in range(width):
            rows.extend((200, 120, 60))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", header),
            chunk(b"IDAT", zlib.compress(bytes(rows), 6)),
            chunk(b"IEND", b""),
        ]
    )


class PackFormatTests(unittest.TestCase):
    def test_known_versions(self) -> None:
        self.assertEqual(pack_format_for("1.12.2"), 4)
        self.assertEqual(pack_format_for("1.16.5"), 5)
        self.assertEqual(pack_format_for("1.18.2"), 8)
        self.assertEqual(pack_format_for("1.20.1"), 15)
        self.assertEqual(pack_format_for("1.21.1"), 34)

    def test_unknown_version_falls_back(self) -> None:
        # 解析不了或过于超前时给一个较新的默认值，不抛异常
        for version in ("", "bogus", "1", "1.x.0"):
            self.assertIsInstance(pack_format_for(version), int)


class BuildSkinPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.game_dir = Path(self._tmp.name)
        self.skin = _png(64, 64)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_layout_and_meta(self) -> None:
        root = build_skin_pack(self.game_dir, self.skin, mc_version="1.20.1")
        self.assertEqual(root, skin_pack_path(self.game_dir))
        self.assertEqual(root.name, PACK_DIR_NAME)
        self.assertTrue((root / "pack.mcmeta").is_file())
        self.assertTrue((root / "pack.png").is_file())

        meta = json.loads((root / "pack.mcmeta").read_text(encoding="utf-8"))
        self.assertEqual(meta["pack"]["pack_format"], 15)
        self.assertTrue(meta["pack"]["description"])

    def test_all_model_targets_written(self) -> None:
        root = build_skin_pack(self.game_dir, self.skin, mc_version="1.21.1")
        # 经典 / 纤细 / 1.19.2+ 新布局三处都要有，否则某种模型会退回默认皮肤
        for target in (CLASSIC_TARGET, SLIM_TARGET, WIDE_TARGET):
            path = root / target
            self.assertTrue(path.is_file(), f"缺少 {target}")
            self.assertEqual(path.read_bytes(), self.skin)

    def test_pack_icon_is_valid_png(self) -> None:
        root = build_skin_pack(self.game_dir, self.skin, mc_version="1.20.1")
        icon = (root / "pack.png").read_bytes()
        self.assertEqual(icon[:8], b"\x89PNG\r\n\x1a\n")

    def test_rebuild_overwrites(self) -> None:
        first = _png(64, 64)
        second = _png(64, 32)
        build_skin_pack(self.game_dir, first, mc_version="1.20.1")
        root = build_skin_pack(self.game_dir, second, mc_version="1.20.1")
        self.assertEqual((root / CLASSIC_TARGET).read_bytes(), second)

    def test_remove_is_idempotent(self) -> None:
        self.assertFalse(remove_skin_pack(self.game_dir))  # 还没建过
        build_skin_pack(self.game_dir, self.skin, mc_version="1.20.1")
        self.assertTrue(remove_skin_pack(self.game_dir))
        self.assertFalse(skin_pack_path(self.game_dir).exists())


if __name__ == "__main__":
    unittest.main()
