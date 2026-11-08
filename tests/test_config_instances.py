import json
import tempfile
import unittest
from pathlib import Path

from offcraft.config import Config, config_dir, load_config, save_config
from offcraft.instances import detect_loader, discover_instances


class ConfigTest(unittest.TestCase):
    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(download_base="https://mc.example.com/", username="Steve", memory_mb=8192)
            path = save_config(config, tmp)
            self.assertTrue(path.is_file())
            loaded = load_config(tmp)
            self.assertEqual(loaded.username, "Steve")
            self.assertEqual(loaded.memory_mb, 8192)
            self.assertEqual(loaded.download_base, "https://mc.example.com")

    def test_corrupt_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = config_dir(tmp)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "config.json").write_text("{not json", encoding="utf-8")
            loaded = load_config(tmp)
            self.assertEqual(loaded.username, "Player")
            backups = list(directory.glob("config.corrupt-*.json"))
            self.assertEqual(len(backups), 1)

    def test_version_override(self) -> None:
        config = Config(memory_mb=4096, version_overrides={"1.20": {"memory_mb": 8192}})
        merged = config.apply_version_overrides("1.20")
        self.assertEqual(merged["memory_mb"], 8192)
        self.assertEqual(config.apply_version_overrides("1.21")["memory_mb"], 4096)

    def test_skin_layout(self) -> None:
        self.assertEqual(Config().skin_layout, "v1")
        invalid = Config(skin_layout="v9")
        invalid.normalize()
        self.assertEqual(invalid.skin_layout, "v1")
        with tempfile.TemporaryDirectory() as tmp:
            save_config(Config(skin_layout="v3"), tmp)
            self.assertEqual(load_config(tmp).skin_layout, "v3")

    def test_download_sources_migrate(self) -> None:
        # 旧配置只有 download_base：归一化时自动补成单源列表
        legacy = Config(download_base="https://mc.example.com/")
        legacy.normalize()
        self.assertEqual(legacy.download_sources, ["https://mc.example.com"])

    def test_download_sources_multi(self) -> None:
        config = Config(
            download_base="",
            download_sources=["https://a.example.com/", "", "https://b.example.com"],
        )
        config.normalize()
        self.assertEqual(
            config.download_sources,
            ["https://a.example.com", "", "https://b.example.com"],
        )
        # 首个非空源作为主源（皮肤/联机接口使用）
        self.assertEqual(config.download_base, "https://a.example.com")

    def test_download_sources_direct_only(self) -> None:
        config = Config(download_sources=[""])
        config.normalize()
        self.assertEqual(config.download_sources, [""])
        self.assertEqual(config.download_base, "")


class InstancesTest(unittest.TestCase):
    def test_discover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            version_dir = root / "versions" / "1.20.1-forge"
            version_dir.mkdir(parents=True)
            (version_dir / "1.20.1-forge.json").write_text(
                json.dumps(
                    {
                        "id": "1.20.1-forge",
                        "inheritsFrom": "1.20.1",
                        "mainClass": "cpw.mods.bootstraplauncher.BootstrapLauncher",
                        "javaVersion": {"majorVersion": 17},
                        "libraries": [{"name": "net.minecraftforge:forge:47.0.0"}],
                    }
                ),
                encoding="utf-8",
            )
            items = discover_instances(root)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].loader, "Forge")
            self.assertEqual(items[0].mc_version, "1.20.1")
            self.assertEqual(items[0].java_major, 17)

    def test_loader_detection(self) -> None:
        self.assertEqual(detect_loader("net.fabricmc.loader.impl.launch.knot.KnotClient", []), "Fabric")
        self.assertEqual(
            detect_loader("cpw.mods.bootstraplauncher.BootstrapLauncher",
                          [{"name": "net.neoforged:neoforge:21.1.236"}]), "NeoForge"
        )
        self.assertEqual(detect_loader("net.minecraft.client.main.Main", []), "原版")


if __name__ == "__main__":
    unittest.main()
