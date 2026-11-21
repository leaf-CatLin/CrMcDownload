"""存档详情页的离屏冒烟：合成存档 → 页面 → 断言关键内容渲染出来。

用 ``QT_QPA_PLATFORM=offscreen``，不需要真实显示器；也不需要游戏本体
（存档由 ``tests/save_fixture.py`` 现造）。
"""

import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel  # noqa: E402
from PyQt6.QtGui import QImage  # noqa: E402

from offcraft.ui.save_page import SavePage, render_map_image  # noqa: E402
from tests.save_fixture import build_save  # noqa: E402

_APP = QApplication.instance() or QApplication([])


class _StubWindow:
    """MainWindow 的最小替身：``post`` 把回调排队，由测试主循环抽取执行。

    真实 ``MainWindow.post`` 是把回调塞进 ``queue.Queue``，由 UI 线程的
    定时器 drain；这里必须照做——如果直接同步执行，回调就会在扫描线程里
    构造控件，Qt 会报 "Cannot set parent, new parent is in a different thread"。
    """

    def __init__(self) -> None:
        self.notices: list[tuple[str, str]] = []
        self.statuses: list[str] = []
        self.queue: list = []

    def post(self, callback) -> None:
        self.queue.append(callback)

    def drain(self) -> None:
        while self.queue:
            self.queue.pop(0)()

    def notice(self, text: str, level: str = "INFO") -> None:
        self.notices.append((level, text))

    def status(self, text: str) -> None:
        self.statuses.append(text)


class _TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class RenderMapTests(_TempCase):
    def test_map_image_is_painted(self) -> None:
        save = build_save(self.tmp / "world")
        from offcraft.saves.nbt import load_dat

        payload = load_dat(save.paths["map"])["data"]
        from offcraft.saves.model import MapInfo

        info = MapInfo(
            map_id=0,
            width=payload["width"],
            height=payload["height"],
            colors=payload["colors"],
        )
        image = render_map_image(info)
        self.assertEqual(image.width(), 128)
        self.assertEqual(image.height(), 128)
        # 上半草地、下半水：两个像素颜色必须不同，而且不能全透明
        top = image.pixelColor(10, 10)
        bottom = image.pixelColor(10, 120)
        self.assertNotEqual(top.name(), bottom.name())
        self.assertGreater(top.alpha(), 0)


class SavePageTests(_TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.fixture = build_save(self.tmp / "存档测试世界")
        self.window = _StubWindow()

    def _page(self) -> SavePage:
        page = SavePage(self.window, self.fixture.root, autoscan=False)
        page.start_scan()
        self._wait(page)
        return page

    def _wait(self, page: SavePage, timeout: float = 20.0) -> None:
        """等扫描线程结束，并在**主线程**里执行排队的 UI 回调。"""
        end = time.time() + timeout
        while time.time() < end:
            _APP.processEvents()
            self.window.drain()
            if not page._scanning and not self.window.queue:
                break
            time.sleep(0.02)
        _APP.processEvents()
        self.window.drain()

    def test_page_renders_report(self) -> None:
        page = self._page()
        try:
            self.assertIsNotNone(page.report)
            assert page.report is not None
            self.assertEqual(page.report.level.level_name, "存档测试世界")
            texts = {label.text() for label in page.findChildren(QLabel)}
            # 概览 / 关键字段必须出现在界面上
            self.assertIn("概览", texts)
            self.assertIn("已加载区块", texts)
            self.assertIn("历史日志", texts)
            self.assertIn(str(self.fixture.seed), texts)  # 种子可复制
            self.assertIn("床", texts)  # 兴趣点
            self.assertIn("箱子", texts)  # 容器
            self.assertGreater(len(page.report.containers), 0)
        finally:
            page.shutdown()
            page.deleteLater()
            _APP.processEvents()

    def test_page_survives_missing_level_dat(self) -> None:
        empty = self.tmp / "空目录"
        empty.mkdir()
        page = SavePage(self.window, empty, autoscan=False)
        page.start_scan()
        self._wait(page)
        try:
            # 不能崩，而且要把"读不出"这件事说清楚
            self.assertIsNotNone(page.report)
            self.assertTrue(page.report.warnings)
        finally:
            page.shutdown()
            page.deleteLater()
            _APP.processEvents()

    def test_shutdown_cancels_scan(self) -> None:
        page = SavePage(self.window, self.fixture.root, autoscan=False)
        page.shutdown()
        page.deleteLater()
        _APP.processEvents()
        # 取消后再扫描不应回写已销毁页面
        self.assertTrue(page._cancel.is_set())


class LauncherIntegrationTests(_TempCase):
    """启动页点存档 → 进详情页：确认真的接进了主窗口，而不是只有独立页面。"""

    def setUp(self) -> None:
        super().setUp()
        self.fixture = build_save(self.tmp / "存档测试世界")
        from offcraft.config import Config
        from offcraft.ui import app as app_module

        # 主窗口启动后会拉远端清单 / 探测下载源；测试里不需要联网，直接短路。
        self._saved = (
            app_module.MainWindow.refresh_remote,
            app_module.MainWindow._load_cached_manifest,
        )
        app_module.MainWindow.refresh_remote = lambda self, *a, **k: None
        app_module.MainWindow._load_cached_manifest = lambda self, *a, **k: False
        self.config = Config(game_dir=str(self.tmp))
        self.window = app_module.MainWindow(self.config, str(self.tmp / "data"))
        self.window.resize(1200, 800)

    def tearDown(self) -> None:
        from offcraft.ui import app as app_module

        app_module.MainWindow.refresh_remote, app_module.MainWindow._load_cached_manifest = self._saved
        self.window.close()
        self.window.deleteLater()
        _APP.processEvents()
        super().tearDown()

    def _drain(self, timeout: float = 6.0) -> None:
        end = time.time() + timeout
        while time.time() < end:
            _APP.processEvents()
            # 主窗口的事件队列（post 回调）由 _drain 定时器消费
            drain = getattr(self.window, "_drain", None)
            if callable(drain):
                try:
                    drain()
                except Exception:  # noqa: BLE001 - 空队列
                    pass
            time.sleep(0.02)

    def test_save_page_is_registered(self) -> None:
        from offcraft.ui.app import PAGE_TITLES

        # 存档详情是**子页面**（由 open_save_page 现场构建），不在一级页工厂里
        self.assertEqual(PAGE_TITLES["save"], "存档详情")
        self.assertNotIn("save", self.window._page_factories)

    def test_open_save_page_switches_page(self) -> None:
        self.window.open_save_page(self.fixture.root)
        self._drain()
        page = self.window.pages.get("save")
        self.assertIsNotNone(page)
        self.assertIs(self.window.stack.currentWidget(), page)
        # 子页面的返回目标是启动页
        self.assertEqual(self.window._sub_parent.get("save"), "home")
        # 页面里至少有一个 SavePage，且扫描出了世界名
        views = page.findChildren(SavePage)
        self.assertTrue(views)
        self._drain(10.0)
        assert views[0].report is not None
        self.assertEqual(views[0].report.level.level_name, "存档测试世界")

    def test_missing_save_dir_notice(self) -> None:
        self.window.open_save_page(self.tmp / "不存在")
        self.assertNotIn("save", self.window.pages)


if __name__ == "__main__":
    unittest.main()
