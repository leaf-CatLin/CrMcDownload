"""Java 运行时与加载器：纯函数 / 清单解析层测试（不联网）。"""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from offcraft.content import java as java_mod
from offcraft.content.java import JavaRuntime, JavaRuntimeError, list_java_runtimes
from offcraft.content.loader import (
    LOADERS,
    LoaderError,
    _neoforge_prefix,
    _verify_installer_jar,
    install_loader,
    installer_version_id,
    list_loader_versions,
    loader_dependency_tasks,
    supports_mods,
)
from offcraft.mirror import Mirror

MIRROR = Mirror("https://mc.example.com")


class _FakeDownloader:
    """按 URL 片段返回固定内容，用于解析层测试。"""

    def __init__(self, json_map=None, bytes_map=None) -> None:
        self.json_map = json_map or {}
        self.bytes_map = bytes_map or {}
        self.calls: list[str] = []

    def fetch_json(self, url: str):
        self.calls.append(url)
        for key, value in self.json_map.items():
            if key in url:
                return value
        raise RuntimeError(f"unexpected url {url}")

    def fetch_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        for key, value in self.bytes_map.items():
            if key in url:
                return value
        raise RuntimeError(f"unexpected url {url}")


JAVA_MANIFEST = {
    "windows-x64": {
        "java-runtime-delta": [
            {
                "manifest": {"url": "https://launchermeta.mojang.com/x/delta.json", "size": 3},
                "version": {"name": "21.0.5", "released": "2024-10-15"},
            }
        ],
        "jre-legacy": [
            {
                "manifest": {"url": "https://launchermeta.mojang.com/x/legacy.json"},
                "version": {"name": "8u352", "released": "2022-07-01"},
            }
        ],
        "minecraft-java-exe": [{"manifest": {"url": "https://x/ignored.json"}}],
    }
}


class SupportsModsTest(unittest.TestCase):
    def test_loader_matrix(self) -> None:
        for loader in ("Fabric", "Forge", "NeoForge", "Quilt"):
            self.assertTrue(supports_mods(loader), loader)
        for loader in ("原版", "vanilla", "", "OptiFine"):
            self.assertFalse(supports_mods(loader), loader)

    def test_loader_choices(self) -> None:
        keys = [key for key, _text in LOADERS]
        self.assertEqual(keys, ["vanilla", "fabric", "forge", "neoforge"])


class JavaRuntimeTest(unittest.TestCase):
    def test_major_from_table_and_version(self) -> None:
        self.assertEqual(JavaRuntime("java-runtime-delta", "21.0.5", "", "u").major, 21)
        self.assertEqual(JavaRuntime("custom-runtime", "17.0.9", "", "u").major, 17)
        self.assertEqual(JavaRuntime("custom-runtime", "weird", "", "u").major, 0)

    def test_paths(self) -> None:
        runtime = JavaRuntime("jre-legacy", "8u352", "", "u")
        self.assertEqual(runtime.label, "Java 8 · jre-legacy")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(runtime.installed(tmp))
            target = runtime.java_path(tmp)
            target.parent.mkdir(parents=True)
            target.write_text("", encoding="utf-8")
            self.assertTrue(runtime.installed(tmp))
            self.assertEqual(runtime.install_dir(tmp), Path(tmp) / "runtime" / "jre-legacy")

    def test_list_runtimes_filters_and_sorts(self) -> None:
        downloader = _FakeDownloader(json_map={"all.json": JAVA_MANIFEST})
        runtimes = list_java_runtimes(MIRROR, downloader)
        self.assertEqual([item.component for item in runtimes], ["jre-legacy", "java-runtime-delta"])
        self.assertEqual([item.major for item in runtimes], [8, 21])
        self.assertTrue(downloader.calls[0].startswith("https://mc.example.com/meta/"))

    def test_list_runtimes_empty_raises(self) -> None:
        downloader = _FakeDownloader(json_map={"all.json": {"windows-x64": {"x": []}}})
        with self.assertRaises(JavaRuntimeError):
            list_java_runtimes(MIRROR, downloader)

    def test_safe_rel_blocks_traversal(self) -> None:
        self.assertIsNone(java_mod._safe_rel("../evil.txt"))
        self.assertIsNone(java_mod._safe_rel("/etc/passwd"))
        self.assertIsNone(java_mod._safe_rel("C:/windows/system32"))
        self.assertIsNone(java_mod._safe_rel(""))
        self.assertEqual(java_mod._safe_rel("bin/java.exe"), Path("bin") / "java.exe")


class LoaderVersionsTest(unittest.TestCase):
    def test_neoforge_prefix(self) -> None:
        self.assertEqual(_neoforge_prefix("1.20.4"), "20.4.")
        self.assertEqual(_neoforge_prefix("1.21"), "21.0.")
        self.assertEqual(_neoforge_prefix("1.21.1"), "21.1.")
        self.assertEqual(_neoforge_prefix(""), "")

    def test_fabric_versions(self) -> None:
        payload = (
            b'[{"loader": {"version": "0.16.9", "stable": true}}, '
            b'{"loader": {"version": "0.16.10", "stable": true}}]'
        )
        downloader = _FakeDownloader(bytes_map={"loader/1.21.1": payload})
        builds = list_loader_versions("fabric", "1.21.1", mirror=MIRROR, downloader=downloader)
        self.assertEqual([item.version for item in builds], ["0.16.9", "0.16.10"])
        self.assertIn("mc.example.com/fabric/", downloader.calls[0])

    def test_forge_versions_filtered_and_sorted(self) -> None:
        xml = (
            b"<metadata><versioning><versions>"
            b"<version>1.20.1-47.2.0</version>"
            b"<version>1.20.1-47.4.28</version>"
            b"<version>1.19.2-43.2.0</version>"
            b"</versions></versioning></metadata>"
        )
        downloader = _FakeDownloader(bytes_map={"maven-metadata.xml": xml})
        builds = list_loader_versions("forge", "1.20.1", mirror=MIRROR, downloader=downloader)
        self.assertEqual([item.version for item in builds], ["1.20.1-47.4.28", "1.20.1-47.2.0"])

    def test_vanilla_and_unknown(self) -> None:
        downloader = _FakeDownloader()
        self.assertEqual(list_loader_versions("vanilla", "1.21", mirror=MIRROR, downloader=downloader), [])
        with self.assertRaises(LoaderError):
            list_loader_versions("liteloader", "1.21", mirror=MIRROR, downloader=downloader)

    def test_install_loader_vanilla_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = install_loader(tmp, "1.21.1", "vanilla", "", mirror=MIRROR, downloader=_FakeDownloader())
        self.assertEqual(result, "1.21.1")


PROFILE_LIBRARY = {
    "name": "net.minecraftforge:installertools:1.4.5",
    "downloads": {
        "artifact": {
            "path": "net/minecraftforge/installertools/1.4.5/installertools-1.4.5.jar",
            "url": "https://maven.minecraftforge.net/net/minecraftforge/installertools/1.4.5/installertools-1.4.5.jar",
            "sha1": "aaa",
            "size": 100,
        }
    },
}
VERSION_LIBRARY = {
    "name": "net.minecraftforge:forgespi:8.0.0",
    "downloads": {
        "artifact": {
            "path": "net/minecraftforge/forgespi/8.0.0/forgespi-8.0.0.jar",
            "url": "https://maven.minecraftforge.net/net/minecraftforge/forgespi/8.0.0/forgespi-8.0.0.jar",
            "sha1": "bbb",
            "size": 200,
        }
    },
}
PATCHED_CLIENT = {
    "name": "net.minecraftforge:forge:26.2-65.1.5:client",
    "downloads": {
        "artifact": {
            "path": "net/minecraftforge/forge/26.2-65.1.5/forge-26.2-65.1.5-client.jar",
            "url": "",
            "sha1": "ccc",
            "size": 300,
        }
    },
}


def _write_installer(path: Path, version_id: str) -> Path:
    """造一个带有 install_profile.json / version.json 的最小安装器 jar。"""
    profile = {"version": version_id, "libraries": [PROFILE_LIBRARY]}
    version = {"id": version_id, "libraries": [VERSION_LIBRARY, PATCHED_CLIENT]}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("install_profile.json", json.dumps(profile))
        archive.writestr("version.json", json.dumps(version))
    return path


class LoaderDependencyTest(unittest.TestCase):
    """安装器依赖预下载：三个来源都要覆盖，补丁后的 client jar 不能当依赖下。"""

    def test_version_id_from_installer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jar = _write_installer(Path(tmp) / "forge-installer.jar", "26.2-forge-65.1.5")
            self.assertEqual(installer_version_id(jar), "26.2-forge-65.1.5")

    def test_tasks_cover_profile_and_version_and_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jar = _write_installer(root / "forge-installer.jar", "26.2-forge-65.1.5")
            local_dir = root / "versions" / "26.2-forge-65.1.5"
            local_dir.mkdir(parents=True)
            (local_dir / "26.2-forge-65.1.5.json").write_text(
                json.dumps({"id": "26.2-forge-65.1.5", "libraries": [PROFILE_LIBRARY]}),
                encoding="utf-8",
            )
            tasks, warnings = loader_dependency_tasks(root, jar, MIRROR)
            names = sorted(task.dest.name for task in tasks)
            self.assertEqual(
                names, ["forgespi-8.0.0.jar", "installertools-1.4.5.jar"]
            )
            for task in tasks:
                self.assertTrue(task.url.startswith("https://mc.example.com/forgemaven/"))
            self.assertTrue(any("安装器生成" in text for text in warnings))

    def test_direct_variant_keeps_official_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jar = _write_installer(root / "forge-installer.jar", "26.2-forge-65.1.5")
            tasks, _warnings = loader_dependency_tasks(root, jar, MIRROR, rewrite=False)
            self.assertTrue(
                all(task.url.startswith("https://maven.minecraftforge.net/") for task in tasks)
            )

    def test_broken_installer_is_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jar = root / "broken.jar"
            jar.write_bytes(b"not a zip")
            tasks, warnings = loader_dependency_tasks(root, jar, MIRROR)
            self.assertEqual(tasks, [])
            self.assertEqual(warnings, [])
            self.assertEqual(installer_version_id(jar), "")

    def test_verify_installer_jar_accepts_valid_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jar = _write_installer(Path(tmp) / "forge-installer.jar", "26.2-forge-65.1.5")
            self.assertTrue(_verify_installer_jar(jar, loader="forge"))

    def test_verify_installer_jar_rejects_garbage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jar = Path(tmp) / "bad.jar"
            jar.write_bytes(b"this is definitely not a zip archive")
            self.assertFalse(_verify_installer_jar(jar, loader="forge"))

    def test_verify_installer_jar_rejects_missing_version_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jar = Path(tmp) / "partial.jar"
            with zipfile.ZipFile(jar, "w") as archive:
                archive.writestr("install_profile.json", "{}")
                archive.writestr("random.txt", "hi")
            self.assertFalse(_verify_installer_jar(jar, loader="forge"))


if __name__ == "__main__":
    unittest.main()
