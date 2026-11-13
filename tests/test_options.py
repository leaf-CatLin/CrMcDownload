"""options.txt 的资源包启用逻辑。"""

import json
import tempfile
import unittest
from pathlib import Path

from offcraft.launch.options import (
    add_resource_packs,
    enable_all_resource_packs,
    set_resource_pack_enabled,
)


class ResourcePackOptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.mc = Path(self._tmp.name)
        self.options = self.mc / "options.txt"
        (self.mc / "resourcepacks").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _packs(self) -> list[str]:
        for line in self.options.read_text(encoding="utf-8").splitlines():
            if line.startswith("resourcePacks:"):
                return json.loads(line[len("resourcePacks:") :])
        return []

    def test_add_pack_keeps_other_options(self) -> None:
        self.options.write_text('fov:70\nresourcePacks:["vanilla"]\n', encoding="utf-8")
        pack = self.mc / "resourcepacks" / "Fullbright.zip"
        pack.write_bytes(b"zip")
        self.assertEqual(add_resource_packs(self.mc, [pack]), 1)
        self.assertEqual(self._packs(), ["vanilla", "file/Fullbright.zip"])
        self.assertIn("fov:70", self.options.read_text(encoding="utf-8"))

    def test_add_pack_idempotent(self) -> None:
        pack = self.mc / "resourcepacks" / "a.zip"
        pack.write_bytes(b"zip")
        add_resource_packs(self.mc, [pack])
        self.assertEqual(add_resource_packs(self.mc, [pack]), 0)

    def test_folder_pack_entry(self) -> None:
        pack = self.mc / "resourcepacks" / "MyPack"
        pack.mkdir()
        add_resource_packs(self.mc, [pack])
        self.assertEqual(self._packs(), ["vanilla", "folder/MyPack"])

    def test_enable_all_only_when_key_missing(self) -> None:
        (self.mc / "resourcepacks" / "a.zip").write_bytes(b"zip")
        self.assertEqual(enable_all_resource_packs(self.mc), 1)
        self.assertEqual(self._packs(), ["vanilla", "file/a.zip"])

        self.options.write_text('resourcePacks:["vanilla"]\n', encoding="utf-8")
        self.assertEqual(enable_all_resource_packs(self.mc), 0)
        self.assertEqual(self._packs(), ["vanilla"])

    def test_set_enabled_toggles_single_pack(self) -> None:
        pack = self.mc / "resourcepacks" / "a.zip"
        pack.write_bytes(b"zip")
        self.options.write_text('resourcePacks:["vanilla"]\n', encoding="utf-8")

        self.assertTrue(set_resource_pack_enabled(self.mc, "a.zip", True))
        self.assertEqual(self._packs(), ["vanilla", "file/a.zip"])
        self.assertFalse(set_resource_pack_enabled(self.mc, "a.zip", True))

        self.assertTrue(set_resource_pack_enabled(self.mc, "a.zip", False))
        self.assertEqual(self._packs(), ["vanilla"])

    def test_set_enabled_creates_options(self) -> None:
        pack = self.mc / "resourcepacks" / "MyPack"
        pack.mkdir()
        self.assertTrue(set_resource_pack_enabled(self.mc, "MyPack", True))
        self.assertEqual(self._packs(), ["vanilla", "folder/MyPack"])


if __name__ == "__main__":
    unittest.main()
