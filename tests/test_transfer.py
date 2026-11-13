"""版本 / 存档 ZIP 导入导出的往返、重名与路径穿越。"""

import tempfile
import unittest
import zipfile
from pathlib import Path

from offcraft.transfer import TransferError, export_save, export_version, import_save, import_version


class VersionTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.mc = self.tmp / "mc"
        self.version = self.mc / "versions" / "1.20.4-fabric"
        self.version.mkdir(parents=True)
        (self.version / "1.20.4-fabric.json").write_text('{"id": "1.20.4-fabric"}', encoding="utf-8")
        (self.version / "mods").mkdir()
        (self.version / "mods" / "a.jar").write_bytes(b"jar")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_export_import_roundtrip(self) -> None:
        archive = self.tmp / "1.20.4-fabric.zip"
        export_version(self.version, archive)
        self.assertTrue(archive.is_file())

        target_mc = self.tmp / "target"
        version_id = import_version(target_mc, archive)
        self.assertEqual(version_id, "1.20.4-fabric")
        self.assertTrue(
            (target_mc / "versions" / "1.20.4-fabric" / "mods" / "a.jar").is_file()
        )

    def test_duplicate_version_rejected(self) -> None:
        archive = self.tmp / "v.zip"
        export_version(self.version, archive)
        with self.assertRaises(TransferError):
            import_version(self.mc, archive)

    def test_bad_archive(self) -> None:
        bad = self.tmp / "bad.zip"
        bad.write_bytes(b"not a zip")
        with self.assertRaises(TransferError):
            import_version(self.mc, bad)

    def test_path_traversal_rejected(self) -> None:
        evil = self.tmp / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("1.20.4/1.20.4.json", "{}")
            zf.writestr("1.20.4/../../evil.txt", "boom")
        with self.assertRaises(TransferError):
            import_version(self.tmp / "evil-mc", evil)
        self.assertFalse((self.tmp / "evil.txt").exists())


class SaveTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.mc = self.tmp / "mc"
        self.save = self.mc / "saves" / "我的世界"
        self.save.mkdir(parents=True)
        (self.save / "level.dat").write_bytes(b"data")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_roundtrip(self) -> None:
        archive = self.tmp / "save.zip"
        export_save(self.save, archive)
        name = import_save(self.tmp / "target", archive)
        self.assertEqual(name, "我的世界")
        self.assertTrue((self.tmp / "target" / "saves" / "我的世界" / "level.dat").is_file())

    def test_duplicate_name_gets_suffix(self) -> None:
        archive = self.tmp / "save.zip"
        export_save(self.save, archive)
        self.assertEqual(import_save(self.mc, archive), "我的世界 (2)")


if __name__ == "__main__":
    unittest.main()
