"""联机页：热门榜 + 本地收藏/最近，SLP 状态卡片。

沿用下载页「左 20k 导航 / 右内容」骨架。所有探测/榜单都走后台线程，结果经
``window.post`` 回投 UI 主线程，主线程绝不阻塞；同一时刻只允许一个刷新任务，
重复点击合并。界面**不显示** API 地址等下载源信息（延续「不暴露下载源」规则）。
"""

from __future__ import annotations

import threading
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from offcraft.servers.directory import DirectoryError, ServerDirectory, ServerListing
from offcraft.servers.ping import ProbeResult, ping_server
from offcraft.servers.store import ServerEntry, ServerStore
from offcraft.ui.theme import MUTED, block_pixmap, human_count
from offcraft.ui.widgets import Badge, Card, ElidedLabel

__all__ = ["OnlinePage"]

SECTIONS = [("hot", "热门"), ("favorites", "我的收藏"), ("recent", "最近访问")]
LATENCY_TONE = {"green": "green", "yellow": "amber", "red": "red", "unknown": "gray"}
_EMPTY_HINTS = {
    "hot": "联网获取热门榜失败或未配置下载源，只显示本地收藏。",
    "favorites": "还没有收藏服务器，用上方「添加服务器」加入你的第一台。",
    "recent": "最近访问会记录你探测或加入过的服务器。",
}


def _listing_from(entry: ServerEntry, result: ProbeResult | None, base: ServerListing | None = None) -> ServerListing:
    """把本地条目 + 探测结果拼成展示用的 ServerListing（只写真实字段）。"""
    item = base or ServerListing(host=entry.host, port=entry.port, label=entry.label)
    if result is not None:
        item.ok = result.ok
        item.error = result.error
        item.latency_ms = result.latency_ms
        item.motd = result.motd
        item.version_name = result.version_name
        item.online = result.online
        item.max_players = result.max_players
        item.players = tuple(result.players or ())
        item.players_visible = bool(result.players)
    return item


class ServerCard(QWidget):
    """一台服务器的状态卡。"""

    def __init__(self, page: "OnlinePage", listing: ServerListing, favorited: bool, icon: bytes | None) -> None:
        super().__init__()
        self.page = page
        self.listing = listing
        self.favorited = favorited

        card = Card(self, margins=(14, 12, 14, 12))
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        self.icon = QLabel()
        self.icon.setFixedSize(46, 46)
        self.icon.setScaledContents(True)
        card.add(self.icon)

        body = QVBoxLayout()
        body.setSpacing(3)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.title = ElidedLabel(listing.name)
        self.title.setStyleSheet("font-size: 10.5pt; font-weight: 700;")
        title_row.addWidget(self.title, 1)
        self.version = Badge("—", "blue")
        title_row.addWidget(self.version)
        body.addLayout(title_row)

        self.motd = ElidedLabel(" ")
        self.motd.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        body.addWidget(self.motd)

        meta = QHBoxLayout()
        meta.setSpacing(10)
        self.players = QLabel()
        self.players.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        self.latency = Badge("…", "gray")
        meta.addWidget(self.players)
        meta.addWidget(self.latency)
        meta.addStretch(1)
        body.addLayout(meta)

        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet(f"color: {MUTED}; font-size: 8.5pt;")
        self.detail.setVisible(False)
        body.addWidget(self.detail)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        actions.addStretch(1)
        body.addLayout(actions)
        card.add_layout(body, 1)

        for text, handler in (
            ("刷新", self._on_refresh),
            ("复制", self._on_copy),
            ("详情", self._on_detail),
            ("收藏", self._on_favorite),
            ("加入游戏", self._on_join),
        ):
            button = QPushButton(text)
            button.setObjectName("ghost")
            button.setMinimumHeight(28)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(handler)
            actions.addWidget(button)

        self.update(listing, favorited, icon)

    # -- 交互（都带着当前 listing）------------------------------------
    def _on_refresh(self) -> None:
        self.page.ping_one(self.listing)

    def _on_copy(self) -> None:
        self.page.copy_address(self.listing)

    def _on_detail(self) -> None:
        visible = not self.detail.isVisible()
        self.detail.setVisible(visible)
        if visible:
            if self.listing.players:
                names = "、".join(item.name for item in self.listing.players[:20])
                self.detail.setText(f"在线玩家：{names}")
            else:
                self.detail.setText("服务器未公开玩家列表（仅显示人数）")

    def _on_favorite(self) -> None:
        self.page.toggle_favorite(self.listing)

    def _on_join(self) -> None:
        self.page.join(self.listing)

    def update(self, listing: ServerListing, favorited: bool | None = None, icon: bytes | None = None) -> None:
        self.listing = listing
        if favorited is not None:
            self.favorited = favorited
            self._favorite_label()
        if icon is not None:
            self._icon = icon
        if listing.ok:
            self._set_icon(getattr(self, "_icon", None))
            self.title.setText(listing.name)
            self.motd.setText(listing.motd or " ")
            self.version.set(listing.version_name or "—", "blue")
            tone = LATENCY_TONE.get(listing.latency_level, "gray")
            self.latency.set(f"{int(listing.latency_ms or 0)} ms", tone)
            if listing.max_players:
                self.players.setText(f"{human_count(listing.online)}/{human_count(listing.max_players)} 人在玩")
            else:
                self.players.setText(f"{human_count(listing.online)} 人在玩")
        else:
            self._set_icon(None)
            self.title.setText(listing.name)
            self.motd.setText(" ")
            self.latency.set("离线" if listing.error == "" else "离线", "red")
            self.players.setText(listing.error or "无法连接")

    def _favorite_label(self) -> None:
        pass

    def _set_icon(self, data: bytes | None) -> None:
        if data:
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                self.icon.setPixmap(pixmap)
                return
        self.icon.setPixmap(QPixmap(block_pixmap(self.listing.address, 46)))


class OnlinePage(QWidget):
    def __init__(self, window: Any) -> None:
        super().__init__()
        self.window = window
        self.store = ServerStore(window.plugin_data_dir())
        self._section = "hot"
        self._hot: list[ServerListing] = []
        self._icons: dict[str, bytes] = {}
        self._cards: dict[str, ServerCard] = {}
        self._refreshing = False
        self._loaded = False
        self._build()

    # ---------------------------------------------------------------- 布局
    def _build(self) -> None:
        from offcraft.ui.app import LEFT_W, SIDE_PAD, u  # 延迟导入避免环

        layout = QHBoxLayout(self)
        layout.setContentsMargins(SIDE_PAD, SIDE_PAD, SIDE_PAD, SIDE_PAD)
        layout.setSpacing(u(1))

        left = QWidget()
        left.setFixedWidth(LEFT_W)
        column = QVBoxLayout(left)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索服务器地址 / 名称")
        self.search.returnPressed.connect(self.refresh)
        column.addWidget(self.search)

        add_button = QPushButton("添加服务器")
        add_button.setObjectName("primary")
        add_button.setMinimumHeight(34)
        add_button.clicked.connect(self.prompt_add)
        column.addWidget(add_button)

        column.addSpacing(6)
        self.section_buttons: dict[str, QPushButton] = {}
        for key, text in SECTIONS:
            button = QPushButton(text)
            button.setObjectName("navitem")
            button.setCheckable(True)
            button.setMinimumHeight(32)
            button.clicked.connect(lambda _c, name=key: self.set_section(name))
            column.addWidget(button)
            self.section_buttons[key] = button

        refresh = QPushButton("刷新")
        refresh.setObjectName("ghost")
        refresh.setMinimumHeight(30)
        refresh.clicked.connect(self.refresh)
        column.addWidget(refresh)

        column.addStretch(1)
        self.status_label = QLabel(" ")
        self.status_label.setStyleSheet(f"color: {MUTED};")
        column.addWidget(self.status_label)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        host = QWidget()
        self.list_layout = QVBoxLayout(host)
        self.list_layout.setContentsMargins(u(0.75), u(0.75), u(0.75), u(0.75))
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(host)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.scroll)

        layout.addWidget(left)
        layout.addWidget(self.stack, 1)
        self.set_section("hot")

    # ---------------------------------------------------------------- 数据
    def ensure_loaded(self) -> None:
        self.store.load()
        if self._loaded:
            self._rebuild()
            return
        self._loaded = True
        self.refresh()

    def set_section(self, name: str) -> None:
        self._section = name
        for key, button in self.section_buttons.items():
            button.setChecked(key == name)
        self.stack.setCurrentWidget(self.scroll)
        self._rebuild()

    def _directory(self) -> ServerDirectory:
        base = getattr(self.window.config, "download_base", "") or ""
        return ServerDirectory(base, downloader=self.window.make_downloader(progress=False))

    def _current_rows(self) -> list[tuple[ServerListing, bool, bytes | None]]:
        favs = {item.key for item in self.store.list_entries()}
        rows: list[tuple[ServerListing, bool, bytes | None]] = []
        if self._section == "hot":
            for listing in self._hot:
                rows.append((listing, listing.key in favs, self._icons.get(listing.key)))
            return rows
        entries = self.store.list_entries() if self._section == "favorites" else self.store.list_recent()
        for entry in entries:
            listing = _listing_from(entry, self.store.cached(entry.host, entry.port))
            rows.append((listing, entry.key in favs, self._icons.get(entry.key)))
        return rows

    # ---------------------------------------------------------------- 渲染
    def _rebuild(self) -> None:
        for card in self._cards.values():
            card.deleteLater()
        self._cards.clear()
        while self.list_layout.count() > 1:
            self.list_layout.takeAt(0)
        rows = self._current_rows()
        for listing, favorited, icon in rows:
            self._add_card(listing, favorited, icon)
        if not rows:
            self._add_hint()

    def _add_card(self, listing: ServerListing, favorited: bool, icon: bytes | None) -> ServerCard:
        card = ServerCard(self, listing, favorited, icon)
        self._cards[listing.key] = card
        self.list_layout.insertWidget(self.list_layout.count() - 1, card)
        return card

    def _add_hint(self) -> None:
        label = QLabel(_EMPTY_HINTS.get(self._section, " "))
        label.setStyleSheet(f"color: {MUTED}; padding: 24px;")
        self.list_layout.insertWidget(self.list_layout.count() - 1, label)

    def _update_card(self, listing: ServerListing, favorited: bool, icon: bytes | None) -> None:
        card = self._cards.get(listing.key)
        if card is not None:
            card.update(listing, favorited, icon)

    # ---------------------------------------------------------------- 后台
    def refresh(self) -> None:
        if self._refreshing:
            self.status_label.setText("正在刷新……")
            return
        self._refreshing = True
        self.status_label.setText("正在刷新……")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self) -> None:
        section = self._section
        query = self.search.text().strip() if section == "hot" else ""
        allow_local = bool(getattr(self.window.config, "allow_local_servers", False))
        try:
            if section == "hot":
                hits, _total = self._directory().list(query, limit=30)
                self.window.post(lambda: self._apply_hot(hits))
                return
            entries = self.store.list_entries() if section == "favorites" else self.store.list_recent()
            favs = {item.key for item in self.store.list_entries()}
            for entry in entries:
                result = ping_server(entry.address, allow_local=allow_local)
                if result.favicon:
                    self._icons[entry.key] = result.favicon
                self.store.remember(result)
                listing = _listing_from(entry, result)
                fav = entry.key in favs
                icon = self._icons.get(entry.key)
                self.window.post(lambda l=listing, f=fav, i=icon: self._update_card(l, f, i))
        except DirectoryError as exc:
            message = str(exc)
            self.window.post(lambda m=message: self.status_label.setText(m))
        except Exception as exc:  # noqa: BLE001 - 后台线程兜底
            message = f"刷新失败：{exc}"
            self.window.post(lambda m=message: self.status_label.setText(m))
        finally:
            self._refreshing = False

    def _apply_hot(self, hits: list[ServerListing]) -> None:
        self._hot = hits
        self._refreshing = False
        self.status_label.setText(" ")
        if self._section == "hot":
            self._rebuild()

    def ping_one(self, listing: ServerListing) -> None:
        allow_local = bool(getattr(self.window.config, "allow_local_servers", False))

        def worker() -> None:
            result = ping_server(listing.address, allow_local=allow_local)
            if result.favicon:
                self._icons[listing.key] = result.favicon
            self.store.remember(result)
            updated = _listing_from(
                ServerEntry(host=listing.host, port=listing.port, label=listing.label), result
            )
            fav = self.store.get(listing.host, listing.port) is not None
            icon = self._icons.get(listing.key)
            self.window.post(lambda: self._update_card(updated, fav, icon))

        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------------------------------------------- 交互
    def copy_address(self, listing: ServerListing) -> None:
        from PyQt6.QtWidgets import QApplication

        QApplication.clipboard().setText(listing.address)
        self.window.notice("服务器地址已复制到剪贴板", "INFO")

    def join(self, listing: ServerListing) -> None:
        self.store.touch_recent(listing.host, listing.port)
        self.window.join_server(listing.address)

    def toggle_favorite(self, listing: ServerListing) -> None:
        existing = self.store.get(listing.host, listing.port)
        entry = ServerEntry(host=listing.host, port=listing.port, label=listing.label)
        if existing is not None:
            self.store.remove(listing.host, listing.port)
            self.window.notice("已取消收藏", "INFO")
        else:
            self.store.add(listing.address, listing.label)
            self.window.notice("已加入收藏", "INFO")
            self._report(entry)
        self._rebuild()

    def _report(self, entry: ServerEntry) -> None:
        address, label = entry.address, entry.label

        def worker() -> None:
            try:
                self._directory().report(address, label)
            except Exception:  # noqa: BLE001 - 上报失败不影响本地收藏
                pass

        threading.Thread(target=worker, daemon=True).start()

    def prompt_add(self) -> None:
        from PyQt6.QtWidgets import QDialog

        from offcraft.ui.app import AddServerDialog

        dialog = AddServerDialog(self.window)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            entry = self.store.add(dialog.address(), dialog.server_label())
        except Exception as exc:  # noqa: BLE001 - 非法地址
            self.window.notice(f"添加失败：{exc}", "WARN")
            return
        self._rebuild()
        self.window.notice(f"已添加 {entry.name}", "INFO")
        listing = _listing_from(entry, None)
        self.ping_one(listing)
        self._report(entry)

