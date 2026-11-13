"""启动前资源检查：加载目录、异地资源与缺失资源包。"""

import json
import tempfile
import unittest
from pathlib import Path

from offcraft.launch.resources import audit_resources


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.mc = Path(self._tmp.name)
        self.game = self.mc / "versions" / "1.20.4-fabric"
        self.game.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_isolated_marks_misplaced_mods(self) -> None:
        (self.game / "mods").mkdir()
        (self.game / "mods" / "inside.jar").write_bytes(b"jar")
        (self.mc / "mods").mkdir()
        (self.mc / "mods" / "outside.jar").write_bytes(b"jar")

        report = audit_resources(self.mc, "1.20.4-fabric", isolated=True)
        mods = {entry.path.name: entry for entry in report.entries if entry.kind == "mods"}
        self.assertFalse(mods["inside.jar"].misplaced)
        self.assertTrue(mods["outside.jar"].misplaced)
        self.assertEqual([e.path.name for e in report.misplaced], ["outside.jar"])

    def test_non_isolated_marks_version_mods(self) -> None:
        (self.mc / "mods").mkdir()
        (self.mc / "mods" / "root.jar").write_bytes(b"jar")
        other = self.mc / "versions" / "other"
        (other / "mods").mkdir(parents=True)
        (other / "mods" / "other.jar").write_bytes(b"jar")

        report = audit_resources(self.mc, "1.20.4-fabric", isolated=False)
        misplaced = {entry.path.name for entry in report.misplaced}
        self.assertEqual(misplaced, {"other.jar"})

    def test_same_name_not_reported_twice(self) -> None:
        (self.game / "mods").mkdir()
        (self.game / "mods" / "same.jar").write_bytes(b"jar")
        (self.mc / "mods").mkdir()
        (self.mc / "mods" / "same.jar").write_bytes(b"jar")
        report = audit_resources(self.mc, "1.20.4-fabric", isolated=True)
        self.assertEqual(report.misplaced, [])

    def test_disabled_mod_flagged(self) -> None:
        (self.game / "mods").mkdir()
        (self.game / "mods" / "off.jar.disabled").write_bytes(b"jar")
        report = audit_resources(self.mc, "1.20.4-fabric", isolated=True)
        entry = next(e for e in report.entries if e.kind == "mods")
        self.assertTrue(entry.disabled)

    def test_disabled_packs_still_listed(self) -> None:
        (self.game / "resourcepacks").mkdir()
        (self.game / "resourcepacks" / "a.zip.disabled").write_bytes(b"zip")
        (self.game / "shaderpacks").mkdir()
        (self.game / "shaderpacks" / "s.zip.disabled").write_bytes(b"zip")
        report = audit_resources(self.mc, "1.20.4-fabric", isolated=True)
        listed = {(entry.kind, entry.path.name, entry.disabled) for entry in report.entries}
        self.assertIn(("resourcepacks", "a.zip.disabled", True), listed)
        self.assertIn(("shaderpacks", "s.zip.disabled", True), listed)

    def test_missing_enabled_resource_pack(self) -> None:
        (self.game / "resourcepacks").mkdir()
        (self.game / "resourcepacks" / "here.zip").write_bytes(b"zip")
        packs = json.dumps(["vanilla", "file/here.zip", "file/gone.zip"])
        (self.game / "options.txt").write_text(
            f"resourcePacks:{packs}\n", encoding="utf-8"
        )
        report = audit_resources(self.mc, "1.20.4-fabric", isolated=True)
        self.assertEqual(report.missing, ["gone.zip"])
        self.assertEqual(report.problem_count, 1)


if __name__ == "__main__":
    unittest.main()
