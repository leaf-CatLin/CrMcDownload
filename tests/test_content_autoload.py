"""下载页的定时自动追加：首屏 60 条之后，每 5s 往下补 30 条。

这里只用离屏 Qt 与桩客户端，不联网，因此可以在 CI 里跑。
覆盖四条契约：

1. 首屏一次 60 条（``PAGE_SIZE``）；
2. 首屏完成后启动 5 秒定时器，到点追加 30 条（``AUTO_APPEND_SIZE``）；
3. 本轮窗口内用户自己加载过，则**跳过**这次自动追加，下一轮恢复；
4. 列表到底后停表，不再空转请求。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from offcraft.ui import content_page as cp

_app = QApplication.instance() or QApplication([])


class _Hit:
    def __init__(self, index: int) -> None:
        self.id = f"hit-{index}"
        self.slug = f"slug-{index}"
        self.title = f"项目 {index}"
        self.description = "描述"
        self.project_type = "mod"
        self.downloads = index
        self.follows = 0
        self.author = "作者"
        self.icon_url = ""
        self.categories: list[str] = []
        self.date_modified = ""


class _Result:
    def __init__(self, hits, total: int, offset: int) -> None:
        self.hits = hits
        self.total = total
        self.offset = offset


class _Client:
    """桩客户端：记录每次 search 的 (limit, offset)，返回固定总数。"""

    api_mode = "镜像"

    def __init__(self, total: int) -> None:
        self.total = total
        self.calls: list[tuple[int, int]] = []

    def search(self, query, kind, game_version="", loader="", limit=60, offset=0, index="relevance"):
        self.calls.append((limit, offset))
        hits = [_Hit(offset + i) for i in range(max(0, min(limit, self.total - offset)))]
        return _Result(hits, self.total, offset)

    def resolve_url(self, url: str) -> str:
        return url

    def versions(self, *args, **kwargs):
        return []


class _Window:
    """ContentPage 只依赖 window 上的少量接口。"""

    def __init__(self, client: _Client) -> None:
        self.client = client
        self.icons: dict[str, object] = {}

    def content_client(self) -> _Client:
        return self.client

    def request_icon(self, url, callback, size=46) -> None:
        # 桩里不发网络请求：直接给个空 pixmap 占位即可
        return None

    def post(self, callback) -> None:
        callback()


def _make_page(total: int = 300) -> tuple[cp.ContentPage, _Client]:
    client = _Client(total)
    page = cp.ContentPage(_Window(client), "mod")
    return page, client


def _first_screen(page: cp.ContentPage, total: int = 300) -> _Client:
    """直接喂一页首屏结果（绕开网络与线程）。"""
    client = page.window.content_client()
    result = client.search("", "mod", limit=cp.PAGE_SIZE, offset=0)
    page._token = 7
    page._apply_result(7, result, False, None)
    return client


class AutoAppendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page, self.client = _make_page()

    def tearDown(self) -> None:
        self.page.close()
        self.page.deleteLater()
        _app.processEvents()

    def test_first_screen_is_sixty(self) -> None:
        client = _first_screen(self.page)
        self.assertEqual(client.calls[0], (cp.PAGE_SIZE, 0))
        self.assertEqual(cp.PAGE_SIZE, 60)
        self.assertEqual(len(self.page.hits), 60)

    def test_timer_starts_after_first_screen(self) -> None:
        _first_screen(self.page)
        self.assertIsNotNone(self.page._auto_timer)
        self.assertTrue(self.page._auto_timer.isActive())
        self.assertEqual(self.page._auto_timer.interval(), int(cp.AUTO_LOAD_SECONDS * 1000))

    def test_tick_appends_thirty(self) -> None:
        client = _first_screen(self.page)
        client.calls.clear()
        self.page._auto_append_tick()
        self.assertEqual(client.calls, [(cp.AUTO_APPEND_SIZE, 60)])
        self.assertEqual(cp.AUTO_APPEND_SIZE, 30)
        self.assertEqual(len(self.page.hits), 90)

    def test_user_load_skips_one_round(self) -> None:
        client = _first_screen(self.page)
        # 模拟用户在本轮窗口内点了「加载更多」
        self.page.load_more()
        client.calls.clear()
        self.page._auto_append_tick()   # 本轮应让位
        self.assertEqual(client.calls, [])
        self.page._auto_append_tick()   # 下一轮恢复
        self.assertEqual(client.calls, [(cp.AUTO_APPEND_SIZE, self.page.offset - cp.AUTO_APPEND_SIZE)])

    def test_scroll_prefetch_counts_as_user_load(self) -> None:
        _first_screen(self.page)
        self.page._mark_user_load()
        client = self.page.window.content_client()
        client.calls.clear()
        self.page._auto_append_tick()
        self.assertEqual(client.calls, [])

    def test_stops_at_end(self) -> None:
        page, client = _make_page(total=70)
        try:
            result = client.search("", "mod", limit=cp.PAGE_SIZE, offset=0)
            page._token = 3
            page._apply_result(3, result, False, None)
            self.assertEqual(len(page.hits), 60)
            page._auto_append_tick()          # 补到 70（到底）
            self.assertEqual(len(page.hits), 70)
            client.calls.clear()
            page._auto_append_tick()          # 已到底：不该再发请求，且应停表
            self.assertEqual(client.calls, [])
            self.assertFalse(page._auto_timer.isActive())
        finally:
            page.close()
            page.deleteLater()
            _app.processEvents()

    def test_suspend_and_resume(self) -> None:
        _first_screen(self.page)
        self.page.suspend_auto_append()
        self.assertFalse(self.page._auto_timer.isActive())
        self.page.resume_auto_append()
        self.assertTrue(self.page._auto_timer.isActive())


if __name__ == "__main__":
    unittest.main()
