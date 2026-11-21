"""本轮 UX 需求的纯逻辑回归：光影前置识别、前置依赖检测、皮肤单槽。

这三块都不需要网络与真实游戏目录，用临时目录即可覆盖：

1. :func:`offcraft.launch.resources.shader_loader_name` —— 没有 Iris /
   OptiFine / Oculus 时启动页不该出现「已装光影」页签；
2. :func:`offcraft.content.install.missing_dependencies` —— 下载模组前先查前置，
   已装的不能重复报缺失；
3. 皮肤单槽（``save_slot_skin`` / ``load_slot_skin``）不按用户名隔离。
"""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from offcraft.content.install import missing_dependencies
from offcraft.content.modrinth import Dependency, ProjectVersion, VersionFile
from offcraft.launch.resources import has_shader_loader, shader_loader_name


class ShaderLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.mods = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_empty_mods_has_no_loader(self) -> None:
        self.assertEqual(shader_loader_name(self.mods), "")
        self.assertFalse(has_shader_loader(self.mods))

    def test_missing_dir_is_safe(self) -> None:
        self.assertFalse(has_shader_loader(self.mods / "nope"))

    def test_iris_detected(self) -> None:
        (self.mods / "iris-mc1.20.1-1.6.5.jar").write_bytes(b"x")
        self.assertEqual(shader_loader_name(self.mods), "Iris")

    def test_optifine_detected(self) -> None:
        (self.mods / "OptiFine_1.20.1_HD_U_I6.jar").write_bytes(b"x")
        self.assertEqual(shader_loader_name(self.mods), "OptiFine")

    def test_oculus_detected(self) -> None:
        (self.mods / "oculus-mc1.20.1-1.7.0.jar").write_bytes(b"x")
        self.assertEqual(shader_loader_name(self.mods), "Oculus")

    def test_disabled_loader_does_not_count(self) -> None:
        (self.mods / "iris-mc1.20.1-1.6.5.jar.disabled").write_bytes(b"x")
        self.assertFalse(has_shader_loader(self.mods))

    def test_non_jar_ignored(self) -> None:
        (self.mods / "iris-notes.txt").write_text("x", encoding="utf-8")
        self.assertFalse(has_shader_loader(self.mods))


class _Client:
    """桩客户端：只提供 ``project()``，用于按 slug 反查。"""

    def __init__(self, slugs: dict[str, str]) -> None:
        self._slugs = slugs

    def project(self, project_id: str) -> dict:
        slug = self._slugs.get(project_id)
        return {"slug": slug} if slug else {}


def _file(name: str) -> VersionFile:
    return VersionFile(
        url=f"https://example.invalid/{name}",
        filename=name,
        sha1="",
        sha512="",
        size=0,
        primary=True,
    )


def _version(deps: tuple[Dependency, ...]) -> ProjectVersion:
    return ProjectVersion(
        id="v1",
        project_id="p1",
        name="demo",
        version_number="1.0",
        version_type="release",
        game_versions=("1.20.1",),
        loaders=("fabric",),
        files=(_file("demo-1.0.jar"),),
        dependencies=deps,
    )


class MissingDependencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_named_dependency_present(self) -> None:
        (self.dest / "fabric-api-0.90.jar").write_bytes(b"x")
        version = _version(
            (Dependency(file_name="fabric-api-0.90.jar", dependency_type="required"),)
        )
        self.assertEqual(missing_dependencies(_Client({}), version, self.dest), [])

    def test_named_dependency_missing(self) -> None:
        version = _version(
            (Dependency(file_name="fabric-api-0.90.jar", dependency_type="required"),)
        )
        missing = missing_dependencies(_Client({}), version, self.dest)
        self.assertEqual(len(missing), 1)

    def test_optional_dependency_ignored(self) -> None:
        version = _version(
            (Dependency(file_name="optional.jar", dependency_type="optional"),)
        )
        self.assertEqual(missing_dependencies(_Client({}), version, self.dest), [])

    def test_slug_prefix_match(self) -> None:
        (self.dest / "fabric-api-0.91.0+1.20.1.jar").write_bytes(b"x")
        version = _version((Dependency(project_id="P7dR8mSH", dependency_type="required"),))
        client = _Client({"P7dR8mSH": "fabric-api"})
        self.assertEqual(missing_dependencies(client, version, self.dest), [])

    def test_slug_missing_but_unknown_slug_reports(self) -> None:
        version = _version((Dependency(project_id="unknown", dependency_type="required"),))
        missing = missing_dependencies(_Client({}), version, self.dest)
        self.assertEqual(len(missing), 1)


class SkinSlotTests(unittest.TestCase):
    """皮肤单槽：不按用户名隔离（用户明确要求）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("OFFCRAFT_DATA")
        os.environ["OFFCRAFT_DATA"] = self._tmp.name
        # config_dir() 每次调用都读环境变量，因此这里无需重载模块
        from offcraft.skins import upload as up

        self.up = up

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("OFFCRAFT_DATA", None)
        else:
            os.environ["OFFCRAFT_DATA"] = self._old
        self._tmp.cleanup()

    def test_roundtrip_classic(self) -> None:
        data = b"\x89PNG\r\n\x1a\nfake"
        path = self.up.save_slot_skin(data, model="classic")
        self.assertTrue(path.is_file())
        loaded = self.up.load_slot_skin()
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded[0], data)
        self.assertEqual(loaded[1], "classic")

    def test_switch_model_removes_other(self) -> None:
        self.up.save_slot_skin(b"classic-bytes", model="classic")
        self.up.save_slot_skin(b"slim-bytes", model="slim")
        loaded = self.up.load_slot_skin()
        assert loaded is not None
        self.assertEqual(loaded[1], "slim")
        # 另一种模型的文件必须被删掉，避免读到旧皮肤
        self.assertFalse(self.up.local_skin_path(model="classic").is_file())

    def test_remove(self) -> None:
        self.up.save_slot_skin(b"x", model="classic")
        self.assertTrue(self.up.remove_slot_skin())
        self.assertIsNone(self.up.load_slot_skin())
        # 幂等：再删一次不报错
        self.assertFalse(self.up.remove_slot_skin())


class PackIconTests(unittest.TestCase):
    """启动页列表用资源包自带的 ``pack.png`` 当图标。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _png(width: int, height: int) -> bytes:
        import struct
        import zlib

        rows = bytearray()
        for _y in range(height):
            rows.append(0)
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
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(rows)))
            + chunk(b"IEND", b"")
        )

    def _zip(self, name: str, *, with_icon: bool) -> Path:
        import zipfile

        path = self.root / name
        with zipfile.ZipFile(path, "w") as bundle:
            bundle.writestr("pack.mcmeta", '{"pack":{"pack_format":15}}')
            if with_icon:
                bundle.writestr("pack.png", self._png(64, 64))
        return path

    def test_zip_icon(self) -> None:
        from offcraft.launch.resources import pack_icon_bytes

        data = pack_icon_bytes(self._zip("a.zip", with_icon=True))
        self.assertIsNotNone(data)
        assert data is not None
        self.assertTrue(data.startswith(b"\x89PNG"))

    def test_zip_without_icon(self) -> None:
        from offcraft.launch.resources import pack_icon_bytes

        self.assertIsNone(pack_icon_bytes(self._zip("b.zip", with_icon=False)))

    def test_folder_icon(self) -> None:
        from offcraft.launch.resources import pack_icon_bytes

        folder = self.root / "packdir"
        folder.mkdir()
        (folder / "pack.mcmeta").write_text("{}", encoding="utf-8")
        self.assertIsNone(pack_icon_bytes(folder))
        (folder / "pack.png").write_bytes(self._png(32, 32))
        self.assertIsNotNone(pack_icon_bytes(folder))

    def test_disabled_zip_still_readable(self) -> None:
        """``xxx.zip.disabled`` 本身就是那个 zip（只改了后缀），图标照样能读。"""
        from offcraft.launch.resources import pack_icon_bytes

        real = self._zip("c.zip", with_icon=True)
        disabled = real.with_name(real.name + ".disabled")
        real.rename(disabled)
        self.assertIsNotNone(pack_icon_bytes(disabled))

    def test_broken_zip_is_safe(self) -> None:
        from offcraft.launch.resources import pack_icon_bytes

        broken = self.root / "broken.zip"
        broken.write_bytes(b"not a zip at all")
        self.assertIsNone(pack_icon_bytes(broken))


class CheckerCanvasTests(unittest.TestCase):
    """预览画布的透明区域用经典灰白棋盘格表示。"""

    @classmethod
    def setUpClass(cls) -> None:
        global _app
        from PyQt6.QtWidgets import QApplication

        _app = QApplication.instance() or QApplication([])

    @staticmethod
    def _transparent_pixmap(size: int = 64) -> "QPixmap":
        """造一张**全透明** pixmap。

        注意 ``QPixmap.fill()`` 不带参数默认填白色，那样会盖住棋盘格，
        必须显式填 ``Qt.GlobalColor.transparent``。
        """
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QPixmap

        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        return pixmap

    def test_canvas_paints_checkerboard_for_transparent_image(self) -> None:
        from PyQt6.QtGui import QColor

        from offcraft.ui.pack_view import _CheckerCanvas

        canvas = _CheckerCanvas()
        canvas.resize(200, 200)
        canvas.set_pixmap(self._transparent_pixmap())
        canvas.show()
        _app.processEvents()

        image = canvas.grab().toImage()
        # 棋盘格由白 (#FFFFFF) 与灰 (#D6D6D6) 交替组成，取若干点应能看到两种
        light = QColor(_CheckerCanvas.LIGHT).name()
        dark = QColor(_CheckerCanvas.DARK).name()
        seen = set()
        for y in range(0, image.height(), 3):
            for x in range(0, image.width(), 3):
                seen.add(image.pixelColor(x, y).name())
        self.assertIn(light, seen)
        self.assertIn(dark, seen)
        canvas.close()

    def test_checkerboard_area_matches_image_size(self) -> None:
        """棋盘格只铺在图片占用区域，不铺满整个画布（否则屏幕会很花）。"""
        from PyQt6.QtGui import QColor

        from offcraft.ui.pack_view import _CheckerCanvas

        canvas = _CheckerCanvas()
        canvas.resize(400, 400)
        canvas.set_pixmap(self._transparent_pixmap(32))  # 32px 贴图 → 整数倍放大
        canvas.show()
        _app.processEvents()

        image = canvas.grab().toImage()
        dark = QColor(_CheckerCanvas.DARK).name()
        # 四角属于画布留白，不应是棋盘格
        for x, y in ((2, 2), (397, 2), (2, 397), (397, 397)):
            self.assertNotEqual(image.pixelColor(x, y).name(), dark)
        canvas.close()

    def test_small_image_upscaled_by_integer_factor(self) -> None:
        """小贴图按整数倍放大，便于看清像素画（不放大就指甲盖大小）。"""
        from PyQt6.QtGui import QPixmap

        from offcraft.ui.pack_view import _CheckerCanvas

        canvas = _CheckerCanvas()
        canvas.resize(200, 200)
        pixmap = QPixmap(16, 16)
        pixmap.fill()
        canvas.set_pixmap(pixmap)
        scaled = canvas._scaled_for(canvas.size())
        # 200 // 16 = 12 倍，且必须是整数倍
        self.assertEqual(scaled.width() % 16, 0)
        self.assertGreater(scaled.width(), 16)
        self.assertEqual(scaled.width(), scaled.height())
        canvas.close()


if __name__ == "__main__":
    unittest.main()
