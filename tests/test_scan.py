"""本机其他 .minecraft 目录扫描：发现、排除与深度限制。"""

import tempfile
import unittest
from pathlib import Path

from offcraft.instances import looks_like_minecraft_root, scan_minecraft_dirs


def _make_root(parent: Path, name: str, version: str = "1.20.4") -> Path:
    root = parent / name
    folder = root / "versions" / version
    folder.mkdir(parents=True)
    (folder / f"{version}.json").write_text("{}", encoding="utf-8")
    return root


class ScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_detect_looks_like_root(self) -> None:
        root = _make_root(self.tmp, ".minecraft")
        self.assertTrue(looks_like_minecraft_root(root))
        self.assertFalse(looks_like_minecraft_root(self.tmp))

    def test_finds_nested_root_and_skips_windows(self) -> None:
        found = _make_root(self.tmp / "Games", ".minecraft")
        _make_root(self.tmp / "Windows", ".minecraft")
        results = scan_minecraft_dirs(roots=[self.tmp], max_depth=4)
        self.assertIn(found, results)
        self.assertNotIn(self.tmp / "Windows" / ".minecraft", results)

    def test_exclude_primary_root(self) -> None:
        root = _make_root(self.tmp, ".minecraft")
        results = scan_minecraft_dirs(roots=[self.tmp], exclude=[root], max_depth=4)
        self.assertEqual(results, [])

    def test_depth_limit(self) -> None:
        deep = _make_root(self.tmp / "a" / "b" / "c" / "d", ".minecraft")
        results = scan_minecraft_dirs(roots=[self.tmp], max_depth=3)
        self.assertNotIn(deep, results)


if __name__ == "__main__":
    unittest.main()
