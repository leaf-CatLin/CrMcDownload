"""安装页：选原版还是加载器（Fabric / NeoForge / Forge），冲突时变灰。

四条行：第一行较高，左侧图标 + 版本目录名称（同时是显示名）；下面三行是加载器。
点击行即选中，底部按钮按当前选择安装。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from offcraft.content.loader import (
    LOADER_CONFLICTS,
    LOADER_ROWS,
    LoaderBuild,
    list_loader_versions,
    loader_note,
    loader_support,
)
from offcraft.instances import discover_instances
from offcraft.meta.manifest import VersionSummary
from offcraft.ui.theme import block_pixmap, loader_pixmap
from offcraft.ui.widgets import Badge, Card, polish_combo

__all__ = ["InstallPage", "LoaderRow"]


class LoaderRow(QFrame):
    """一行：复选框 + 图标 + 名称/说明 + 版本下拉 + 状态徽标。"""

    picked = pyqtSignal(str)
    changed = pyqtSignal()

    def __init__(
        self,
        key: str,
        title: str,
        blurb: str,
        *,
        icon_name: str = "",
        tall: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.key = key
        self.icon_name = icon_name or title
        self.tall = tall
        self.enabled_row = True
        self.setObjectName("loaderRow")
        self.setProperty("picked", False)
        self.setProperty("dimmed", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(92 if tall else 66)
        # 行高固定（内容决定），剩余高度留给卡片底部的弹性空白，避免四行被均分拉高
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(14)

        # 三个加载器前面各一个复选框（单选语义：勾一个自动取消其它）
        self.check: QCheckBox | None = None
        if not tall:
            self.check = QCheckBox()
            self.check.setToolTip("勾选后用这个加载器安装")
            self.check.clicked.connect(self._on_check_clicked)
            layout.addWidget(self.check, 0, Qt.AlignmentFlag.AlignVCenter)

        self._icon_size = 60 if tall else 40
        self.icon = QLabel()
        self.icon.setFixedSize(self._icon_size, self._icon_size)
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.icon)

        column = QVBoxLayout()
        column.setSpacing(3)
        self.title = QLabel(title)
        self.title.setObjectName("h2" if tall else "h3")
        # 第一行（原版）不要标题，输入框自己说明用途
        # 控件此时还没加入布局，只做隐藏，避免 setVisible(True) 让它独立成窗口闪一下
        if tall:
            self.title.hide()
        column.addWidget(self.title)

        self.name_edit: QLineEdit | None = None
        if tall:
            self.name_edit = QLineEdit()
            self.name_edit.setObjectName("dirName")
            self.name_edit.setPlaceholderText("版本目录名称（同时作为显示名称）")
            self.name_edit.setMaximumWidth(320)
            self.name_edit.textChanged.connect(lambda _t: self.changed.emit())
            # 包一层：输入框限宽，但不让整列被限宽（否则说明文字只能挤在 320px 里）
            name_row = QHBoxLayout()
            name_row.setContentsMargins(0, 0, 0, 0)
            name_row.setSpacing(0)
            name_row.addWidget(self.name_edit)
            name_row.addStretch(1)
            column.addLayout(name_row)

        self.blurb = QLabel(blurb)
        self.blurb.setObjectName("muted")
        self.blurb.setWordWrap(True)
        if tall:
            self.blurb.hide()
        column.addWidget(self.blurb)
        layout.addLayout(column, 1)

        self.badge = Badge("", "gray")
        self.badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout.addWidget(
            self.badge, 0, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight
        )

        self.version_combo = QComboBox()
        self.version_combo.setMinimumWidth(186)
        self.version_combo.currentIndexChanged.connect(lambda _i: self.changed.emit())
        if tall:
            self.version_combo.hide()
        polish_combo(self.version_combo)
        layout.addWidget(self.version_combo, 0, Qt.AlignmentFlag.AlignVCenter)

        self._paint_icon(False)

    # ------------------------------------------------------------------ 状态
    def _paint_icon(self, dimmed: bool) -> None:
        size = self._icon_size
        if self.key == "vanilla":
            self.icon.setPixmap(block_pixmap(self._vanilla_seed, size, "原版"))
        else:
            self.icon.setPixmap(loader_pixmap(self.icon_name, size, dimmed=dimmed))

    _vanilla_seed = "crmcdownload"

    def set_vanilla_seed(self, seed: str) -> None:
        self._vanilla_seed = seed or "crmcdownload"
        self._paint_icon(not self.enabled_row)

    def set_state(
        self,
        enabled: bool,
        reason: str = "",
        badge_text: str = "可选",
        tone: str = "green",
    ) -> None:
        self.enabled_row = bool(enabled)
        if not enabled:
            self.setProperty("picked", False)
        self.setProperty("dimmed", not enabled)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor
        )
        self.badge.set(badge_text, tone if enabled or badge_text else "gray")
        # 第一行不显示「可选」，只在「已安装」这类有意义的状态才出现
        self.badge.setVisible(bool(badge_text) and not (self.tall and badge_text == "可选"))
        self.title.setObjectName("h2" if self.tall and enabled else ("h3" if enabled else "dimTitle"))
        self.blurb.setObjectName("muted" if enabled else "dimText")
        self.version_combo.setEnabled(enabled)
        if self.check is not None:
            # 冲突 / 不兼容：复选框变灰不可勾
            self.check.setEnabled(enabled)
            if not enabled:
                self.check.setChecked(False)
        if self.name_edit is not None:
            self.name_edit.setEnabled(True)
        self.setToolTip(reason)
        self._paint_icon(not enabled)
        self._repolish()

    def set_selected(self, on: bool) -> None:
        if not self.enabled_row:
            on = False
        self.setProperty("picked", bool(on))
        if self.check is not None:
            self.check.setChecked(bool(on))
        self._repolish()

    def _repolish(self) -> None:
        for widget in (self, self.title, self.blurb):
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    # ------------------------------------------------------------------ 版本
    def set_combo_text(self, text: str) -> None:
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        self.version_combo.addItem(text, "")
        self.version_combo.setEnabled(False)
        self.version_combo.blockSignals(False)

    def set_builds(self, builds: list[LoaderBuild]) -> None:
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        if not builds:
            self.version_combo.addItem("没有适配此 MC 版本的构建", "")
            self.version_combo.setEnabled(False)
        else:
            for build in builds:
                tag = "" if build.stable else "（测试）"
                self.version_combo.addItem(f"{build.version}{tag}", build.version)
            self.version_combo.setEnabled(True)
        self.version_combo.blockSignals(False)
        self.changed.emit()

    def selected_version(self) -> str:
        return str(self.version_combo.currentData() or "")

    def has_version(self) -> bool:
        if self.key == "vanilla":
            return True
        return bool(self.selected_version())

    # ------------------------------------------------------------------ 交互
    def _on_check_clicked(self, checked: bool) -> None:
        """勾选即选中该加载器；取消勾选就退回原版。"""
        if not self.enabled_row:
            if self.check is not None:
                self.check.setChecked(False)
            return
        self.picked.emit(self.key if checked else "vanilla")

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self.enabled_row and event.button() == Qt.MouseButton.LeftButton:
            self.picked.emit(self.key)
        super().mouseReleaseEvent(event)


class InstallPage(QWidget):
    """下载页里的「安装版本」子页（不是弹窗，返回即回到游戏版本列表）。"""

    #: 安装位置下拉框末尾的哨兵项：选中它等于「挑一个文件夹」
    PICK_LOCATION = "__pick_location__"

    def __init__(self, window: Any) -> None:
        super().__init__()
        self.window = window
        self.summary: VersionSummary | None = None
        self.picked = "vanilla"
        self._token = 0
        self._cache: dict[str, list[LoaderBuild]] = {}
        self._loading: set[str] = set()
        #: 用户手动挑过的其他安装目录
        self._locations: list[Path] = []
        self._build()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(12)
        self.back_button = QPushButton("← 返回")
        self.back_button.clicked.connect(self.window.close_install_page)
        header.addWidget(self.back_button)
        heads = QVBoxLayout()
        heads.setSpacing(2)
        self.title_label = QLabel("安装版本")
        self.title_label.setObjectName("h2")
        heads.addWidget(self.title_label)
        self.subtitle = QLabel("选择一个安装方式：原版只装 Mojang 文件，加载器才能装模组。")
        self.subtitle.setObjectName("muted")
        self.subtitle.setWordWrap(True)
        heads.addWidget(self.subtitle)
        header.addLayout(heads, 1)
        root.addLayout(header)

        # 安装位置：默认用当前游戏目录，也可以安装到本机任意文件夹。
        location = Card(margins=(12, 10, 12, 10))
        row = QHBoxLayout()
        row.setSpacing(10)
        label = QLabel("安装位置")
        label.setObjectName("muted")
        row.addWidget(label)
        self.location_combo = QComboBox()
        self.location_combo.setMinimumHeight(32)
        self.location_combo.currentIndexChanged.connect(self._on_location_changed)
        row.addWidget(self.location_combo, 1)
        self.browse_button = QPushButton("选择文件夹…")
        self.browse_button.clicked.connect(self._browse_location)
        row.addWidget(self.browse_button)
        location.add_layout(row)
        root.addWidget(location)

        wrap = Card(margins=(12, 12, 12, 12))
        self.rows: dict[str, LoaderRow] = {}
        vanilla = LoaderRow(
            "vanilla",
            "原版",
            "只装 Mojang 文件，不能装模组（资源包不受影响）。目录名就是实例的显示名。",
            tall=True,
        )
        vanilla.picked.connect(self._select)
        vanilla.changed.connect(self._refresh_footer)
        self.rows["vanilla"] = vanilla
        wrap.add(vanilla)

        for key, name, blurb in LOADER_ROWS:
            row = LoaderRow(key, name, blurb, icon_name=name)
            row.picked.connect(self._select)
            row.changed.connect(self._refresh_footer)
            self.rows[key] = row
            wrap.add(row)
        wrap.body.addStretch(1)
        root.addWidget(wrap, 1)

        footer = QHBoxLayout()
        footer.setSpacing(10)
        self.hint = QLabel("")
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        footer.addWidget(self.hint, 1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.window.close_install_page)
        footer.addWidget(self.cancel_button)
        self.install_button = QPushButton("安装")
        self.install_button.setObjectName("accent")
        self.install_button.setFixedHeight(40)
        self.install_button.setMinimumWidth(132)
        self.install_button.clicked.connect(self._install)
        footer.addWidget(self.install_button)
        root.addLayout(footer)

    # ------------------------------------------------------------------ 位置
    def install_root(self) -> Path:
        data = self.location_combo.currentData()
        if data and data != self.PICK_LOCATION:
            return Path(str(data))
        return Path(self.window.game_dir())

    def _rebuild_locations(self, select: str = "") -> None:
        current = select or str(self.install_root())
        roots: list[Path] = []
        for path in [Path(self.window.game_dir()), *self._locations]:
            if path not in roots:
                roots.append(path)
        self.location_combo.blockSignals(True)
        self.location_combo.clear()
        configured = str(getattr(self.window.config, "game_dir", "") or "")
        for index, path in enumerate(roots):
            if index == 0:
                text = f"{'当前目录' if configured else '默认目录'} · {path}"
            else:
                text = f"自定义 · {path}"
            self.location_combo.addItem(text, str(path))
        self.location_combo.addItem("选择其他文件夹…", self.PICK_LOCATION)
        index = next((i for i in range(self.location_combo.count()) if self.location_combo.itemData(i) == current), 0)
        self.location_combo.setCurrentIndex(index)
        self.location_combo.blockSignals(False)
        self.location_combo.setToolTip(current)

    def _on_location_changed(self, _index: int) -> None:
        if self.location_combo.currentData() == self.PICK_LOCATION:
            self._browse_location()
            return
        self.location_combo.setToolTip(str(self.install_root()))
        self._refresh_footer()

    def _browse_location(self) -> None:
        start = str(self.install_root())
        folder = QFileDialog.getExistingDirectory(self, "选择游戏安装目录", start)
        if not folder:
            self._rebuild_locations()
            return
        path = Path(folder)
        if path not in self._locations:
            self._locations.append(path)
        self._rebuild_locations(select=str(path))
        self._refresh_footer()

    # ------------------------------------------------------------------ 打开
    def open_for(self, summary: VersionSummary) -> None:
        self.summary = summary
        self._token += 1
        self._cache.clear()
        self._loading.clear()
        self.picked = "vanilla"
        self._rebuild_locations()
        self.title_label.setText(f"安装 Minecraft {summary.id}")
        stamp = (summary.release_time or summary.time or "")[:10]
        self.subtitle.setText(
            f"{summary.label}" + (f" · 发布于 {stamp}" if stamp else "")
            + " · 加载器决定能不能装模组与光影"
        )

        installed = {item.id for item in self.window.instances}
        vanilla = self.rows["vanilla"]
        vanilla.set_vanilla_seed(summary.id)
        if vanilla.name_edit is not None:
            vanilla.name_edit.setText(summary.id)
        if summary.id in installed:
            vanilla.set_state(True, "同名版本已存在，安装会复用已有文件", "已安装", "amber")
        else:
            vanilla.set_state(True, "原版不能装模组，但资源包不受影响", "可选", "green")

        taken = self._taken_loaders(summary.id)
        for key, name, _blurb in LOADER_ROWS:
            row = self.rows[key]
            ok, reason = loader_support(key, summary.id, version_type=summary.type)
            conflict = ""
            for other in LOADER_CONFLICTS.get(key, ()):
                if other in taken:
                    conflict = taken[other]
                    break
            if ok and conflict:
                ok = False
                reason = f"{conflict} 已为 {summary.id} 装过，Forge 与 NeoForge 不能同时安装"
            if ok:
                note = loader_note(key, summary.id)
                row.set_state(True, note, "可选", "green")
            elif conflict:
                row.set_state(False, reason, "冲突", "gray")
            else:
                row.set_state(False, reason, "不兼容", "gray")
            row.set_combo_text("选中后读取版本")

        for key, row in self.rows.items():
            row.set_selected(key == "vanilla")
        self._refresh_footer()

    def _taken_loaders(self, mc_version: str) -> dict[str, str]:
        """同 MC 版本已装过的加载器：``{loader_key: 实例 id}``。"""
        taken: dict[str, str] = {}
        try:
            instances = discover_instances(self.install_root())
        except Exception:  # noqa: BLE001 - 列不出来不影响安装
            return taken
        for item in instances:
            if item.mc_version != mc_version:
                continue
            key = item.loader.lower()
            if key in ("fabric", "forge", "neoforge"):
                taken.setdefault(key, item.id)
        return taken

    # ------------------------------------------------------------------ 选择
    def _select(self, key: str) -> None:
        row = self.rows.get(key)
        if row is None or not row.enabled_row:
            return
        self.picked = key
        for name, item in self.rows.items():
            item.set_selected(name == key)
        if key != "vanilla":
            self._ensure_versions(key)
        self._refresh_footer()

    def _ensure_versions(self, key: str) -> None:
        if key in self._cache or key in self._loading or self.summary is None:
            return
        self._loading.add(key)
        token = self._token
        mc_version = self.summary.id
        self.rows[key].set_combo_text("正在读取版本……")
        downloader = self.window.make_downloader(progress=False, workers=3)
        source = self.window.source(downloader)

        def worker() -> None:
            try:
                builds = list_loader_versions(
                    key, mc_version, mirror=source, downloader=downloader, limit=40
                )
            except Exception as exc:  # noqa: BLE001 - 展示给用户
                self.window.post(lambda: self._versions_failed(token, key, str(exc)))
                return
            self.window.post(lambda: self._versions_loaded(token, key, builds))

        threading.Thread(target=worker, daemon=True).start()

    def _versions_loaded(self, token: int, key: str, builds: list[LoaderBuild]) -> None:
        if token != self._token:
            return
        self._loading.discard(key)
        row = self.rows[key]
        if not builds:
            row.set_builds([])
            row.set_state(False, "该加载器没有适配此 MC 版本的构建", "不可用", "gray")
            self._refresh_footer()
            return
        self._cache[key] = list(builds)
        row.set_builds(list(builds))
        row.set_state(True, loader_note(key, self.summary.id if self.summary else ""), "可选", "green")
        self._refresh_footer()

    def _versions_failed(self, token: int, key: str, message: str) -> None:
        if token != self._token:
            return
        self._loading.discard(key)
        row = self.rows[key]
        row.set_combo_text("版本列表获取失败")
        row.set_state(False, f"读取版本失败：{message}", "失败", "gray")
        self._refresh_footer()

    # ------------------------------------------------------------------ 底部
    def _refresh_footer(self) -> None:
        key = self.picked
        row = self.rows.get(key)
        if row is None or self.summary is None:
            self.install_button.setEnabled(False)
            return
        if key == "vanilla":
            name = (row.name_edit.text().strip() if row.name_edit else "") or self.summary.id
            self.hint.setText(
                f"将安装原版 {self.summary.id} → {self.install_root()} / {name}（不能装模组）"
            )
            self.install_button.setText("安装原版")
            self.install_button.setEnabled(True)
            return
        title = dict((k, n) for k, n, _b in LOADER_ROWS).get(key, key)
        self.install_button.setText(f"安装 {title}")
        if key in self._loading:
            self.hint.setText(f"正在读取 {title} 版本列表……")
            self.install_button.setEnabled(False)
            return
        if not self._cache.get(key):
            self.hint.setText(f"{title} 没有适配 {self.summary.id} 的版本")
            self.install_button.setEnabled(False)
            return
        version = row.selected_version()
        self.hint.setText(
            f"将安装 Minecraft {self.summary.id} + {title} {version}"
            f" → {self.install_root()}（目录名由安装器生成，可在安装后重命名）"
        )
        self.install_button.setEnabled(bool(version))

    # ------------------------------------------------------------------ 安装
    def _install(self) -> None:
        if self.summary is None:
            return
        key = self.picked
        row = self.rows.get(key)
        if row is None:
            return
        version = "" if key == "vanilla" else row.selected_version()
        if key != "vanilla" and not version:
            self.window.notice("请先选择一个加载器版本", "WARN")
            return
        name_edit = self.rows["vanilla"].name_edit
        name = (name_edit.text().strip() if name_edit is not None else "") or self.summary.id
        self.window.start_install(
            self.summary, key, version, name, install_dir=str(self.install_root())
        )
