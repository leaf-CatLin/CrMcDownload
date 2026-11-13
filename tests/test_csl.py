"""CSL mod 安装与配置：已有则跳过、配置内容正确、失败绝不抛异常。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from offcraft.instances import Instance
from offcraft.net.downloader import DownloadError
from offcraft.skins import csl


def _instance(loader: str = "Fabric") -> Instance:
    return Instance(
        id="1.20.4-fabric", loader=loader, mc_version="1.20.4", type="release",
        java_major=17, size_bytes=0, mtime=0.0,
    )


class _Mirror:
    def __init__(self, base: str) -> None:
        self.base_url = base


class _Downloader:
    def __init__(self) -> None:
        self.downloaded = []

    def download(self, task, **_kw):
        self.downloaded.append(task)
        Path(task.dest).write_bytes(b"JAR")
        return True


class BuildConfigTests(unittest.TestCase):
    def test_loadlist_points_at_skin_root(self) -> None:
        cfg = csl.build_config("https://mirror.example/")
        self.assertTrue(cfg["enable"])
        entries = cfg["loadlist"]
        first = entries[0]
        self.assertEqual(first["type"], "CustomSkinAPI")
        self.assertEqual(first["root"], "https://mirror.example/")
        self.assertEqual(entries[1]["type"], "MojangAPI")


class EnsureInstalledTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.mc = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_no_base_skips(self) -> None:
        status = csl.ensure_installed(self.mc, _instance(), downloader=_Downloader(), mirror=_Mirror(""))
        self.assertEqual(status.state, "no_base")

    def test_missing_loader_for_vanilla(self) -> None:
        status = csl.ensure_installed(
            self.mc, _instance("原版"), downloader=_Downloader(), mirror=_Mirror("https://m.example")
        )
        self.assertEqual(status.state, "missing_loader")
        self.assertFalse(status.ready)

    def test_existing_jar_skips_download_and_writes_config(self) -> None:
        mods = csl.mods_dir_for(self.mc, _instance())
        mods.mkdir(parents=True, exist_ok=True)
        (mods / "CustomSkinLoader-1.0.jar").write_bytes(b"OLD")
        downloader = _Downloader()

        status = csl.ensure_installed(
            self.mc, _instance(), downloader=downloader, mirror=_Mirror("https://m.example")
        )

        self.assertEqual(status.state, "present")
        self.assertTrue(status.config_written)
        self.assertTrue(status.ready)
        self.assertEqual(downloader.downloaded, [])  # 已存在不再下载
        config_path = csl.config_path_for(self.mc, _instance())
        self.assertEqual(
            config_path,
            self.mc / "versions" / "1.20.4-fabric" / "CustomSkinLoader" / "CustomSkinLoader.json",
        )
        written = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(written, csl.build_config("https://m.example"))

    def test_install_success(self) -> None:
        downloader = _Downloader()
        file = mock.Mock(url="customskinloader/x.jar", filename="CustomSkinLoader-1.jar", sha1=None, size=None)
        best = mock.Mock(primary_file=file)
        with mock.patch.object(csl, "ModrinthClient") as client_cls:
            client = client_cls.return_value
            client.best_version.return_value = best
            client.resolve_url.return_value = "https://cdn.example/customskinloader/x.jar"
            status = csl.ensure_installed(
                self.mc, _instance(), downloader=downloader, mirror=_Mirror("https://m.example")
            )
        self.assertEqual(status.state, "installed")
        self.assertEqual(len(downloader.downloaded), 1)

    def test_failure_does_not_raise(self) -> None:
        # 内容层没有匹配版本：只返回 failed，不抛异常（绝不阻断启动）
        with mock.patch.object(csl, "ModrinthClient") as client_cls:
            client_cls.return_value.best_version.return_value = None
            status = csl.ensure_installed(
                self.mc, _instance(), downloader=_Downloader(), mirror=_Mirror("https://m.example")
            )
        self.assertEqual(status.state, "failed")

    def test_download_error_swallowed(self) -> None:
        file = mock.Mock(url="u", filename="CustomSkinLoader-1.jar", sha1=None, size=None)
        best = mock.Mock(primary_file=file)
        downloader = mock.Mock()
        downloader.download.side_effect = DownloadError("网络断了")
        with mock.patch.object(csl, "ModrinthClient") as client_cls:
            client = client_cls.return_value
            client.best_version.return_value = best
            client.resolve_url.return_value = "https://cdn/u"
            status = csl.ensure_installed(
                self.mc, _instance(), downloader=downloader, mirror=_Mirror("https://m.example")
            )
        self.assertEqual(status.state, "failed")


if __name__ == "__main__":
    unittest.main()
