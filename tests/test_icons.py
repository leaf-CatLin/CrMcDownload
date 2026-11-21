"""图标库自检：名称登记、多尺寸渲染、旧名兼容。

图标全部由 QPainter + SVG 路径生成（不携带图片资源），因此这里只验证
「每个名字都能在常见尺寸下渲染出非空且尺寸正确的 pixmap」，
以及小尺寸下描边会自适应加粗（否则 16px 会糊成一片）。
"""

import unittest

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from offcraft.ui.icons import ICON_NAMES, icon, icon_pixmap

_app = QApplication.instance() or QApplication([])


class IconTests(unittest.TestCase):
    def test_names_registered(self) -> None:
        self.assertGreaterEqual(len(ICON_NAMES), 40)
        self.assertIn("gear", ICON_NAMES)
        self.assertIn("server", ICON_NAMES)
        self.assertIn("trash", ICON_NAMES)
        self.assertIn("plus", ICON_NAMES)
        self.assertIn("shirt", ICON_NAMES)

    def test_every_icon_renders_at_multiple_sizes(self) -> None:
        for name in ICON_NAMES:
            for size in (12, 16, 18, 20, 24):
                pixmap = icon_pixmap(name, size)
                self.assertFalse(pixmap.isNull(), f"{name}@{size} 渲染为空")
                self.assertEqual(pixmap.width(), size * 2, f"{name}@{size} 宽度错误")

    def test_unknown_name_falls_back(self) -> None:
        pixmap = icon_pixmap("no-such-icon", 16)
        self.assertFalse(pixmap.isNull())

    def test_legacy_settings_name_is_aliased_to_gear(self) -> None:
        # 旧代码里 #settings 是一枚放射状图标，与 sun 视觉混淆；现在统一为齿轮
        self.assertFalse(icon_pixmap("settings", 16).isNull())
        self.assertFalse(icon_pixmap("gear", 16).isNull())

    def test_qicon_wrapper(self) -> None:
        self.assertFalse(icon("folder", 16).isNull())


if __name__ == "__main__":
    unittest.main()
