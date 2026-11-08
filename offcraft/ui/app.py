"""CrMCDownload 主窗口（PyQt6）。

界面参考 PCL2：左侧导航 + 卡片式内容区，右侧是可操作的主体。

- 「启动游戏」：账户、当前实例概览、内存滑杆、启动按钮、版本列表与快捷入口。
- 「下载中心」：游戏版本 + Modrinth 的模组 / 整合包 / 资源包 / 光影 + Java 运行时。
- 「插件」：插件市场 / 官方 / 社区 / 其他 / 已下载（仅框架，接口未接入）。
- 「设置」：游戏目录、版本隔离、Java、内存。

所有网络请求都在后台线程执行，结果通过 ``self.events`` 回到界面线程，
因此搜索、安装、下载过程中窗口不会卡死。
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import QEvent, QPoint, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from offcraft import APP_NAME, __version__
from offcraft.config import Config, config_dir, save_config
from offcraft.net import updater as update_client
from offcraft.content.install import download_file, install_project_version, safe_filename
from offcraft.daily import FALLBACK, load_cached, today_sentence
from offcraft.content.java import (
    JavaRuntime,
    install_java_runtime,
    list_java_runtimes,
)
from offcraft.content.loader import (
    LOADERS,
    LoaderBuild,
    install_loader,
    list_loader_versions,
)
from offcraft.content.modpack import unique_instance_name
from offcraft.content.target import (
    CONTENT_SUBDIRS,
    Target,
    target_from_instance,
    target_state,
)
from offcraft.content.modrinth import ModrinthClient, ProjectHit, ProjectVersion
from offcraft.account import get_provider
from offcraft.instances import (
    Instance,
    discover_instances,
    instance_game_dir,
    read_pack_metadata,
    rename_instance,
    safe_instance_id,
)
from offcraft.launch.auth import validate_name
from offcraft.launch.command import LaunchError, LaunchOptions, build_command
from offcraft.launch.java import export_java_cache, list_java, prime_java_cache, probe_java
from offcraft.launch.process import GameProcess
from offcraft.meta.manifest import VersionManifest, VersionSummary
from offcraft.meta.version import load_version
from offcraft.net.downloader import Downloader, ProgressTracker
from offcraft.net.latency import make_source, probe_source
from offcraft.plugins.registry import PluginRegistry
from offcraft.skins.avatar import head_avatar
from offcraft.skins.default import default_skin_bytes
from offcraft.ui.content_page import ContentPage
from offcraft.ui.chrome import TitleBar, enable_native_chrome, native_hit_test
from offcraft.ui.backdrop import Backdrop
from offcraft.ui.falling import FallingOverlay
from offcraft.ui.icons import icon_pixmap
from offcraft.ui.install_page import InstallPage
from offcraft.ui.spinner import Spinner
from offcraft.ui.style import LineStyle
from offcraft.ui.online_page import OnlinePage
from offcraft.ui.skin_page import LAYOUT_CHOICES, SkinPage
from offcraft.ui.theme import (
    ACCENT,
    DANGER,
    MUTED,
    OK,
    app_qss,
    block_pixmap,
    human_date,
    human_size,
    initial_pixmap,
)
from offcraft.ui.widgets import Badge, Card, ElidedLabel, Tile, ComboPolisher, polish_combo

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

#: 下载中心的内容页签
CONTENT_TABS = [
    ("mod", "模组"),
    ("modpack", "整合包"),
    ("resourcepack", "资源包"),
    ("shader", "光影"),
]

#: 顶部导航（顺序：启动 → 联机 → 下载 → 设置 → 插件）
NAV_ITEMS = [
    ("home", "启动"),
    ("online", "联机"),
    ("download", "下载"),
    ("settings", "设置"),
    ("plugins", "插件"),
]

#: 导航图标（SVG 线条图标名）
NAV_ICONS = {
    "home": "home",
    "online": "globe",
    "download": "download",
    "settings": "settings",
    "plugins": "blocks",
}

#: 草图单位长度：1k = 16px（窗口 75k × 50k → 1200 × 800）
UNIT_PX = 16


def u(k: float) -> int:
    """把草图里的单位长度（k）换算成像素。"""
    return int(round(k * UNIT_PX))


#: 窗口长宽比 3:2（按草图单位长度换算）
WINDOW_RATIO = (3, 2)
WINDOW_DEFAULT = (u(75), u(50))
WINDOW_MIN = (u(67.5), u(45))

#: 顶部菜单栏 5k
TOPBAR_H = u(5)
#: 内容区左右分栏：左 20k / 右 55k
LEFT_W = u(20)
RIGHT_W = u(55)
#: 单侧内边距
SIDE_PAD = u(1.5)
#: 启动页头像边长（比草图小一些，避免占去左栏一半高度）
AVATAR_PX = 128

#: 下载中心全部页签（游戏版本 + 内容 + Java）
DOWNLOAD_TABS = [("game", "游戏版本")] + CONTENT_TABS + [("java", "Java")]

#: 下载页左侧分类图标
DOWNLOAD_TAB_ICONS = {
    "game": "cube",
    "mod": "package",
    "modpack": "layers",
    "resourcepack": "image",
    "shader": "sun",
    "java": "cup",
}

#: 插件页分区（仅框架）
PLUGIN_TABS = [
    ("market", "市场"),
    ("official", "官方"),
    ("community", "社区"),
    ("other", "其他"),
    ("downloaded", "已下载"),
]

#: 游戏版本分类
SEGMENTS = [
    ("release", "正式版"),
    ("snapshot", "快照"),
    ("old", "远古版"),
    ("installed", "已安装"),
]

SEGMENT_KINDS = {
    "release": {"release"},
    "snapshot": {"snapshot"},
    "old": {"old_beta", "old_alpha"},
}

LOADER_TONES = {
    "Fabric": "amber",
    "Forge": "gray",
    "NeoForge": "red",
    "Quilt": "purple",
    "原版": "green",
}


def default_game_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / ".minecraft"


def format_size(value: int) -> str:
    return human_size(value)


def _reveal(folder: Path | str) -> None:
    path = Path(folder)
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))  # type: ignore[attr-defined]
    except OSError:
        pass


def _rate_text(speed: float | None) -> str:
    """把字节/秒格式化成界面友好的一小段文字。"""
    if not speed or speed <= 0:
        return "—"
    if speed >= 1024 * 1024:
        return f"{speed / (1024 * 1024):.1f} MB/s"
    return f"{speed / 1024:.0f} KB/s"


class SourceDialog(QDialog):
    """下载源设置（仅 F12 可打开）：支持多源、测延迟与速率、智能择优。"""

    probed = pyqtSignal(object)

    def __init__(self, parent: QWidget, config: Config) -> None:
        super().__init__(parent)
        self.config = config
        self._rows: list[QWidget] = []
        self._edits: dict[QWidget, QLineEdit] = {}
        self._labels: dict[QWidget, QLabel] = {}
        self._closing = False
        self.probed.connect(self._apply_results)
        self.setWindowTitle("下载源设置")
        self.setMinimumWidth(640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        hint = QLabel(
            "支持多个下载源：先测各源的延迟与下载速度，自动选择更快的源；下载过慢时会重新探测并切换。"
            "首个非空源同时用于皮肤/联机等自有接口，留空表示官方直连。"
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(200)
        host = QWidget()
        self._rows_layout = QVBoxLayout(host)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(6)
        self._rows_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(host)
        layout.addWidget(scroll)

        tools = QHBoxLayout()
        tools.setSpacing(10)
        add = QPushButton("添加源")
        add.clicked.connect(lambda: self._add_row(""))
        self.test_btn = QPushButton("测试全部源")
        self.test_btn.clicked.connect(self._probe_all)
        tools.addWidget(add)
        tools.addWidget(self.test_btn)
        tools.addStretch(1)
        layout.addLayout(tools)

        bases = list(getattr(config, "download_sources", None) or [])
        if not bases:
            bases = [config.download_base]
        for base in bases:
            self._add_row(base)

        self.token = QLineEdit(config.download_token)
        self.proxy = QLineEdit(config.proxy)
        self.workers = QSpinBox()
        self.workers.setRange(1, 16)
        self.workers.setValue(int(config.max_workers))
        for name, widget in (
            ("访问令牌", self.token),
            ("代理", self.proxy),
            ("并发下载数", self.workers),
        ):
            row = QHBoxLayout()
            row.setSpacing(10)
            label = QLabel(name)
            label.setObjectName("muted")
            label.setFixedWidth(76)
            row.addWidget(label)
            row.addWidget(widget, 1)
            layout.addLayout(row)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        ok = QPushButton("保存")
        ok.setObjectName("accent")
        ok.clicked.connect(self.accept)
        buttons.addWidget(ok)
        layout.addLayout(buttons)

    # ------------------------------------------------------------- 源列表
    def _add_row(self, text: str = "") -> None:
        row = QFrame()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        edit = QLineEdit(str(text or ""))
        edit.setPlaceholderText("https://…（留空 = 官方直连）")
        label = QLabel("未测试")
        label.setObjectName("muted")
        label.setFixedWidth(200)
        remove = QPushButton("删除")
        remove.setFixedWidth(56)
        remove.clicked.connect(lambda _=False, widget=row: self._remove_row(widget))
        layout.addWidget(edit, 1)
        layout.addWidget(label)
        layout.addWidget(remove)
        self._rows.append(row)
        self._edits[row] = edit
        self._labels[row] = label
        self._rows_layout.addWidget(row)

    def _remove_row(self, row: QWidget) -> None:
        if len(self._rows) <= 1:
            # 至少保留一行：清空即表示官方直连
            self._edits[row].clear()
            self._labels[row].setText("未测试")
            return
        self._rows.remove(row)
        self._edits.pop(row, None)
        self._labels.pop(row, None)
        row.setParent(None)
        row.deleteLater()

    def _row_base(self, row: QWidget) -> str:
        """读取一行的源地址；允许省略协议（默认 https）。"""
        text = self._edits[row].text().strip().rstrip("/")
        if text and "://" not in text:
            text = "https://" + text
        return text

    # ------------------------------------------------------------- 测试
    def _probe_all(self) -> None:
        self.test_btn.setEnabled(False)
        for label in self._labels.values():
            label.setText("测试中…")
        proxy = self.proxy.text().strip()
        token = self.token.text().strip()
        targets = [(row, self._row_base(row)) for row in self._rows]

        def worker() -> None:
            results: list[tuple[QWidget, dict[str, object]]] = []
            for row, base in targets:
                try:
                    info = probe_source(base, proxy=proxy, token=token, timeout=5.0)
                except Exception as exc:  # noqa: BLE001 - 把失败原因展示给用户
                    info = {"base": base, "ok": False, "error": str(exc)}
                results.append((row, info))
            if not self._closing:
                self.probed.emit(results)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_results(self, results: object) -> None:
        rows = list(results or [])
        best_row: QWidget | None = None
        best_score: float | None = None
        for row, info in rows:
            label = self._labels.get(row)
            if label is None:
                continue
            if not info.get("ok"):
                label.setText("不可达")
                continue
            latency = float(info.get("latency") or 0.0)
            speed = info.get("speed")
            rate = float(speed) if speed else None
            score = latency + (1048576 / rate if rate else 0.0)
            if best_score is None or score < best_score:
                best_score, best_row = score, row
            text = f"{latency * 1000:.0f}ms · {_rate_text(rate)}"
            label.setText(text)
        if best_row is not None:
            label = self._labels.get(best_row)
            if label is not None:
                label.setText("★ " + label.text())
        self.test_btn.setEnabled(True)

    # ------------------------------------------------------------- 保存
    def accept(self) -> None:
        self._closing = True
        super().accept()

    def reject(self) -> None:
        self._closing = True
        super().reject()

    def apply(self) -> None:
        bases: list[str] = []
        for row in self._rows:
            text = self._row_base(row)
            if text not in bases:
                bases.append(text)
        self.config.download_sources = bases
        self.config.download_base = next((item for item in bases if item), "")
        self.config.download_token = self.token.text().strip()
        self.config.proxy = self.proxy.text().strip()
        self.config.max_workers = int(self.workers.value())


class AccountDialog(QDialog):
    """离线账户：只影响游戏内显示的用户名。"""

    def __init__(self, parent: QWidget, name: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("离线账户")
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        hint = QLabel("离线账户无需登录，用户名会作为游戏内昵称（3-16 位字母 / 数字 / 下划线）。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.edit = QLineEdit(name)
        self.edit.setMaxLength(16)
        self.edit.selectAll()
        layout.addWidget(self.edit)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        ok = QPushButton("保存")
        ok.setObjectName("accent")
        ok.clicked.connect(self.accept)
        buttons.addWidget(ok)
        layout.addLayout(buttons)

    def value(self) -> str:
        return self.edit.text().strip()


class LoaderPickDialog(QDialog):
    """选择加载器与版本（安装游戏或给已有版本补装加载器时使用）。"""

    def __init__(
        self,
        window: "MainWindow",
        mc_version: str,
        *,
        allow_vanilla: bool = True,
        loader: str = "vanilla",
        title: str = "选择加载器",
    ) -> None:
        super().__init__(window)
        self.window = window
        self.mc_version = mc_version
        self._builds: list[LoaderBuild] = []
        self._token = 0
        self.setWindowTitle(title)
        self.setMinimumWidth(430)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        heading = QLabel(f"Minecraft {mc_version}")
        heading.setObjectName("h2")
        layout.addWidget(heading)
        layout.addWidget(QLabel("加载器决定能不能装模组与光影：原版只能装资源包。"))

        self.loader_combo = QComboBox()
        for key, text in LOADERS:
            if key == "vanilla" and not allow_vanilla:
                continue
            self.loader_combo.addItem(text, key)
        index = self.loader_combo.findData(loader)
        self.loader_combo.setCurrentIndex(index if index >= 0 else 0)
        self.loader_combo.currentIndexChanged.connect(self._load_versions)
        layout.addWidget(self.loader_combo)

        self.version_combo = QComboBox()
        layout.addWidget(self.version_combo)

        self.info = QLabel("")
        self.info.setObjectName("muted")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.ok_button = QPushButton("安装" if allow_vanilla else "添加加载器")
        self.ok_button.setObjectName("accent")
        self.ok_button.clicked.connect(self._accept)
        buttons.addWidget(self.ok_button)
        layout.addLayout(buttons)

        self._load_versions()

    def _load_versions(self) -> None:
        key = str(self.loader_combo.currentData() or "vanilla")
        self._token += 1
        token = self._token
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        if key == "vanilla":
            self.version_combo.addItem("不安装加载器（原版）", "")
            self.version_combo.setEnabled(False)
            self.version_combo.blockSignals(False)
            self.info.setText("原版实例不能安装模组与光影，资源包不受影响。")
            return
        self.version_combo.setEnabled(False)
        self.version_combo.addItem("正在读取可用版本……", "")
        self.version_combo.blockSignals(False)
        self.info.setText("正在从镜像读取版本列表……")

        downloader = self.window.make_downloader(progress=False, workers=3)
        mirror = self.window.source(downloader)

        def worker() -> None:
            try:
                builds = list_loader_versions(
                    key, self.mc_version, mirror=mirror, downloader=downloader, limit=40
                )
            except Exception as exc:  # noqa: BLE001 - 展示给用户
                self.window.post(lambda: self._failed(token, str(exc)))
                return
            self.window.post(lambda: self._loaded(token, builds))

        threading.Thread(target=worker, daemon=True).start()

    def _loaded(self, token: int, builds: list[LoaderBuild]) -> None:
        if token != self._token:
            return
        self._builds = list(builds)
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        if not builds:
            self.version_combo.addItem("没有可用版本", "")
            self.version_combo.setEnabled(False)
            self.info.setText("该加载器没有适配这个 MC 版本，换一个加载器试试。")
        else:
            for build in builds:
                self.version_combo.addItem(build.label, build.version)
            self.version_combo.setEnabled(True)
            self.info.setText(
                f"共 {len(builds)} 个版本，默认最新；Forge / NeoForge 需要本机已安装 Java。"
            )
        self.version_combo.blockSignals(False)

    def _failed(self, token: int, message: str) -> None:
        if token != self._token:
            return
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        self.version_combo.addItem("获取失败", "")
        self.version_combo.setEnabled(False)
        self.version_combo.blockSignals(False)
        self.info.setText(f"版本列表获取失败：{message}")

    def _accept(self) -> None:
        key = str(self.loader_combo.currentData() or "vanilla")
        if key != "vanilla" and not str(self.version_combo.currentData() or ""):
            QMessageBox.information(self, APP_NAME, "请先选择一个加载器版本。")
            return
        self.accept()

    def choice(self) -> tuple[str, str]:
        key = str(self.loader_combo.currentData() or "vanilla")
        if key == "vanilla":
            return "vanilla", ""
        return key, str(self.version_combo.currentData() or ""),


class InstanceDialog(QDialog):
    """单个实例的设置：目录、Java、删除。"""

    def __init__(self, window: "MainWindow", instance: Instance) -> None:
        super().__init__(window)
        self.window = window
        self.instance = instance
        self.setWindowTitle(f"版本设置 · {instance.id}")
        self.setMinimumWidth(600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        header = QHBoxLayout()
        icon = QLabel()
        icon.setFixedSize(52, 52)
        icon.setPixmap(block_pixmap(instance.id, 52, instance.loader))
        header.addWidget(icon)
        names = QVBoxLayout()
        names.setSpacing(1)
        title = QLabel(instance.id)
        title.setObjectName("h2")
        names.addWidget(title)
        subtitle = QLabel(
            f"{instance.loader} · MC {instance.mc_version} · {human_size(instance.size_bytes)}"
        )
        subtitle.setObjectName("muted")
        names.addWidget(subtitle)
        header.addLayout(names, 1)
        layout.addLayout(header)

        pack = read_pack_metadata(window.game_dir(), instance.id)
        if pack:
            layout.addWidget(self._hint(f"整合包：{pack.get('name', '')} {pack.get('version', '')}".strip()))
        if instance.broken:
            broken = QLabel(instance.broken)
            broken.setObjectName("error")
            broken.setWordWrap(True)
            layout.addWidget(broken)

        layout.addWidget(self._row("游戏目录", str(window.game_dir_for(instance))))
        layout.addWidget(self._row("版本目录", str(window.game_dir() / "versions" / instance.id)))

        java_row = QHBoxLayout()
        java_row.setSpacing(10)
        java_label = QLabel("Java")
        java_label.setObjectName("muted")
        java_label.setFixedWidth(76)
        java_row.addWidget(java_label)
        self.java_combo = QComboBox()
        self.java_combo.addItem("跟随全局设置", "")
        for candidate in list_java(window.game_dir()):
            self.java_combo.addItem(f"Java {candidate.major} · {candidate.source}", str(candidate.path))
        index = self.java_combo.findData(window.config.java_overrides.get(instance.id, ""))
        if index >= 0:
            self.java_combo.setCurrentIndex(index)
        java_row.addWidget(self.java_combo, 1)
        layout.addLayout(java_row)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        open_game = QPushButton("打开游戏目录")
        open_game.clicked.connect(lambda: _reveal(window.game_dir_for(instance)))
        buttons.addWidget(open_game)
        open_version = QPushButton("打开版本目录")
        open_version.clicked.connect(lambda: _reveal(window.game_dir() / "versions" / instance.id))
        buttons.addWidget(open_version)
        buttons.addStretch(1)
        remove = QPushButton("删除实例")
        remove.setObjectName("danger")
        remove.clicked.connect(self._remove)
        buttons.addWidget(remove)
        done = QPushButton("完成")
        done.setObjectName("accent")
        done.clicked.connect(self._done)
        buttons.addWidget(done)
        layout.addLayout(buttons)

    @staticmethod
    def _hint(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("muted")
        label.setWordWrap(True)
        return label

    @staticmethod
    def _row(name: str, value: str) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        label = QLabel(name)
        label.setObjectName("muted")
        label.setFixedWidth(76)
        layout.addWidget(label)
        edit = QLineEdit(value)
        edit.setReadOnly(True)
        edit.setCursorPosition(0)
        layout.addWidget(edit, 1)
        return widget

    def _done(self) -> None:
        path = self.java_combo.currentData()
        if path:
            self.window.config.java_overrides[self.instance.id] = str(path)
        else:
            self.window.config.java_overrides.pop(self.instance.id, None)
        save_config(self.window.config, self.window.data_dir)
        self.accept()

    def _remove(self) -> None:
        answer = QMessageBox.question(
            self, APP_NAME, f"确定删除实例 {self.instance.id}？此操作不可恢复。"
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        shutil.rmtree(self.window.game_dir() / "versions" / self.instance.id, ignore_errors=True)
        self.window.notice(f"已删除实例 {self.instance.id}", "WARN")
        self.window.refresh_instances()
        self.accept()


class MainWindow(QMainWindow):
    """主窗口。"""

    def __init__(self, config: Config, data_dir: str | Path | None = None) -> None:
        super().__init__()
        self.config = config
        self.data_dir = data_dir
        app = QApplication.instance()
        if app is not None and not isinstance(app.style(), LineStyle):
            app.setStyle(LineStyle(app.style()))
        # Java 探测磁盘缓存预热：避免每次启动都遍历安装目录 + spawn java -version
        prime_java_cache(getattr(config, "java_cache", None))
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.manifest: VersionManifest | None = None
        self.instances: list[Instance] = []
        self.selected: Instance | None = None
        self.process: GameProcess | None = None
        self.cancel_event: threading.Event | None = None
        self.log_records: deque[tuple[str, str, str]] = deque(maxlen=3000)
        self._segment = "release"
        self._current_tab = "game"
        self._remote_loading = False
        self._icons: dict[str, QPixmap] = {}
        self._icon_waiters: dict[str, list[tuple[Callable[[QPixmap], None], int]]] = {}
        self._icon_loading: set[str] = set()
        self._content_client: ModrinthClient | None = None
        self._content_key: tuple[Any, ...] | None = None
        self._version_rows: list[VersionSummary] = []
        #: 下载源缓存（按源列表/代理/择优开关区分），保留探测结论避免重复测时
        self._sources: dict[tuple[Any, ...], Any] = {}
        #: 官方内置插件注册表：host 适配器把宿主原语受限地递给插件
        self._plugin_nav_items: list[tuple[str, str, Callable[[], Any]]] = []
        self.plugin_registry = PluginRegistry(self.data_dir, host=_PluginHost(self))
        self.plugin_registry.load()
        #: 离线联机「进入」面板（插件页卡片打开时持有，退出时清理会话）
        self._offline_panel: Any = None

        # 无边框窗口：标题栏、窗口按钮、阴影与缩放全部由应用自己绘制
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle(APP_NAME)
        self._window_icon = QIcon(block_pixmap("crmcdownload", 64))
        self.setWindowIcon(self._window_icon)
        self.setStyleSheet(app_qss())
        self.setMinimumSize(*WINDOW_MIN)
        width = int((config.window or {}).get("w", WINDOW_DEFAULT[0]) or WINDOW_DEFAULT[0])
        # 长宽比固定 3:2：宽度为准，高度由此推算
        height = int(round(width * WINDOW_RATIO[1] / WINDOW_RATIO[0]))
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            if height > available.height() - 60:
                height = max(WINDOW_MIN[1], available.height() - 60)
                width = int(round(height * WINDOW_RATIO[0] / WINDOW_RATIO[1]))
            width = min(width, available.width() - 40)
        width = max(width, WINDOW_MIN[0])
        height = max(int(round(width * WINDOW_RATIO[1] / WINDOW_RATIO[0])), WINDOW_MIN[1])
        self.resize(width, height)

        self._build()
        # 自定义背景：颜色 / PNG / GIF（设置页可随时修改）
        self.apply_background()
        # 所有下拉框统一去黑边（不依赖事件过滤器时序：弹层是独立顶层窗口）
        self.polish_combos()
        QShortcut(QKeySequence("F12"), self, activated=self.open_source_dialog)
        self._refresh_source_badge()
        self._refresh_java_combo()
        self._load_cached_manifest()
        self.refresh_instances()
        self.refresh_remote()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._drain)
        self._timer.start(80)

        #: 下载页内容静默刷新（每 100 秒重新拉取一次，不打断当前浏览）
        self._content_timer = QTimer(self)
        self._content_timer.setInterval(100_000)
        self._content_timer.timeout.connect(self._refresh_download_content)

        #: 系统托盘与「关闭到托盘」状态
        self.tray: QSystemTrayIcon | None = None
        self._force_quit = False
        self._tray_notice_shown = False
        self._build_tray()
        if self.tray is not None:
            app = QApplication.instance()
            if app is not None:
                # 关闭窗口只是隐藏到托盘，真正的退出由托盘菜单触发
                app.setQuitOnLastWindowClosed(False)

    def polish_combos(self) -> None:
        """建完界面后把每个下拉框的弹层都设成无边框透明窗口。"""
        for combo in self.findChildren(QComboBox):
            polish_combo(combo)

    # ------------------------------------------------------------- 自定义背景
    def apply_background(self) -> None:
        """把配置里的背景色 / 背景图同步到最底层背景层。"""
        backdrop = getattr(self, "backdrop", None)
        if backdrop is None:
            return
        backdrop.set_appearance(
            getattr(self.config, "bg_color", ""),
            getattr(self.config, "bg_image", ""),
        )

    # ------------------------------------------------------------- 布局
    def _label(self, text: str, name: str = "muted") -> QLabel:
        label = QLabel(text)
        label.setObjectName(name)
        return label

    @staticmethod
    def _hline() -> QFrame:
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background: rgba(233, 238, 246, 0.80); border: none;")
        return line

    def _build(self) -> None:
        # 背景层同时作为 central widget：渐变/图片/GIF 画在最底层，界面控件叠在上面
        central = Backdrop()
        self.backdrop = central
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.topbar = self._build_topbar()
        root.addWidget(self.topbar)

        self.stack = QStackedWidget()
        # 页面懒构建：首屏只造「启动」页，其余页首次切入时才构建（降低启动耗时）
        self._page_factories: dict[str, Callable[[], QWidget]] = {
            "home": self._build_home_page,
            "online": self._build_online_page,
            "download": self._build_download_page,
            "settings": self._build_settings_page,
            "plugins": self._build_plugins_page,
        }
        self.pages: dict[str, QWidget] = {}
        root.addWidget(self.stack, 1)

        # 版本号（右下）、下载源（左下）、任务面板（浮层）都不占内容区高度
        self._build_overlays()
        self._ensure_page("home")
        self.show_page("home")
        self._place_overlays()

    def _ensure_page(self, name: str) -> QWidget | None:
        """按需构建并返回页面；已构建则直接返回。"""
        page = self.pages.get(name)
        if page is not None:
            return page
        factory = self._page_factories.get(name)
        if factory is None:
            return None
        page = factory()
        self.pages[name] = page
        self.stack.addWidget(page)
        return page

    def _build_topbar(self) -> QWidget:
        """顶部菜单栏（5k）：logo 在左，四项导航居中，右侧显示状态与窗口按钮。"""
        bar = TitleBar(self)
        bar.setFixedHeight(TOPBAR_H)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(u(1.5), 0, u(0.75), 0)
        layout.setSpacing(u(0.75))

        side = u(14)

        brand = QWidget()
        brand.setFixedWidth(side)
        brand_row = QHBoxLayout(brand)
        brand_row.setContentsMargins(0, 0, 0, 0)
        brand_row.setSpacing(u(0.5))
        logo = QLabel()
        logo.setFixedSize(36, 36)
        logo.setPixmap(block_pixmap("crmcdownload", 36))
        brand_row.addWidget(logo)
        name = QLabel(APP_NAME)
        name.setObjectName("brand")
        brand_row.addWidget(name)
        brand_row.addStretch(1)
        layout.addWidget(brand)
        layout.addStretch(1)

        self.nav_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        nav_row = QHBoxLayout()
        nav_row.setSpacing(u(0.375))
        for key, text in NAV_ITEMS:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setObjectName("topnav")
            button.setMinimumHeight(40)
            button.setMinimumWidth(u(6))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._set_nav_icon(button, key, False)
            button.toggled.connect(
                lambda checked, b=button, k=key: self._set_nav_icon(b, k, checked)
            )
            button.clicked.connect(lambda _c, page=key: self.show_page(page))
            group.addButton(button)
            nav_row.addWidget(button)
            self.nav_buttons[key] = button
        layout.addLayout(nav_row)
        layout.addStretch(1)

        status = QWidget()
        status.setFixedWidth(u(11))
        status_row = QHBoxLayout(status)
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(u(0.375))
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet(f"color: {OK}; font-size: 11pt;")
        status_row.addWidget(self.status_dot)
        self.status_label = ElidedLabel("就绪")
        self.status_label.setObjectName("status")
        self.status_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        status_row.addWidget(self.status_label, 1)
        layout.addWidget(status)
        layout.addWidget(self._build_window_controls())
        self._sync_window_controls()
        return bar

    def _set_nav_icon(self, button: QPushButton, key: str, active: bool) -> None:
        """导航图标随选中状态在灰/强调蓝之间切换。"""
        color = ACCENT if active else "#4B5565"
        button.setIcon(QIcon(icon_pixmap(NAV_ICONS.get(key, "blocks"), 18, color)))
        button.setIconSize(QSize(18, 18))

    def _set_tab_icon(self, button: QPushButton, key: str, active: bool) -> None:
        """下载页分类图标随选中状态着色。"""
        color = ACCENT if active else "#6B7688"
        button.setIcon(QIcon(icon_pixmap(DOWNLOAD_TAB_ICONS.get(key, "blocks"), 18, color)))
        button.setIconSize(QSize(18, 18))

    def _build_window_controls(self) -> QWidget:
        """标题栏右侧的最小化 / 最大化 / 关闭按钮。"""
        wrap = QWidget()
        wrap.setFixedWidth(u(7))
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(u(0.25))
        row.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.win_min = QPushButton()
        self.win_min.setObjectName("winBtn")
        self.win_min.setFixedSize(32, 30)
        self.win_min.setCursor(Qt.CursorShape.PointingHandCursor)
        self.win_min.setToolTip("最小化")
        self.win_min.clicked.connect(self.showMinimized)

        self.win_max = QPushButton()
        self.win_max.setObjectName("winBtn")
        self.win_max.setFixedSize(32, 30)
        self.win_max.setCursor(Qt.CursorShape.PointingHandCursor)
        self.win_max.clicked.connect(self._toggle_maximize)

        self.win_close = QPushButton()
        self.win_close.setObjectName("winClose")
        self.win_close.setFixedSize(32, 30)
        self.win_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.win_close.setToolTip("关闭（可关闭到系统托盘）")
        self.win_close.clicked.connect(self.close)

        for button in (self.win_min, self.win_max, self.win_close):
            row.addWidget(button)
        return wrap

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _sync_window_controls(self) -> None:
        """按当前窗口状态切换最大化/还原图标。"""
        if not hasattr(self, "win_max"):
            return
        name = "restore" if self.isMaximized() else "maximize"
        self.win_max.setIcon(QIcon(icon_pixmap(name, 16, "#5B6678")))
        self.win_max.setIconSize(QSize(16, 16))
        self.win_max.setToolTip("还原" if self.isMaximized() else "最大化")
        self.win_min.setIcon(QIcon(icon_pixmap("minimize", 16, "#5B6678")))
        self.win_min.setIconSize(QSize(16, 16))
        self.win_close.setIcon(QIcon(icon_pixmap("close", 16, "#5B6678")))
        self.win_close.setIconSize(QSize(16, 16))

    # ------------------------------------------------------------- 窗口外壳
    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        if not getattr(self, "_chrome_ready", False):
            self._chrome_ready = True
            enable_native_chrome(self)

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync_window_controls()

    def nativeEvent(self, event_type, message):  # noqa: N802 - Qt 命名
        """接管 Windows 的 WM_NCHITTEST，让无边框窗口保留原生缩放热区。"""
        code = native_hit_test(self, message)
        if code is not None:
            return True, code
        # 不转交 super()：基类实现返回的 sip 对象在部分 PyQt 版本会触发访问冲突
        return False, 0

    # ------------------------------------------------------------- 系统托盘
    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(self._tray_icon(), self)
        tray.setToolTip(f"{APP_NAME} {__version__}")
        menu = QMenu(self)
        action_show = menu.addAction(QIcon(icon_pixmap("home", 16)), "显示主界面")
        action_show.triggered.connect(self._restore_from_tray)
        action_download = menu.addAction(QIcon(icon_pixmap("download", 16)), "下载中心")
        action_download.triggered.connect(self._open_download_from_tray)
        action_launch = menu.addAction(QIcon(icon_pixmap("play", 16)), "启动游戏")
        action_launch.triggered.connect(self._launch_or_stop)
        menu.addSeparator()
        action_quit = menu.addAction(QIcon(icon_pixmap("close", 16)), "退出")
        action_quit.triggered.connect(self._quit_from_tray)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self.tray = tray

    def _tray_icon(self) -> QIcon:
        """优先使用打包内置的品牌图标，缺失时回退到程序化方块图标。"""
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
        for name in ("icon.png", "icon.ico"):
            path = base / "assets" / name
            if path.is_file():
                return QIcon(str(path))
        return self._window_icon

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._restore_from_tray()

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _open_download_from_tray(self) -> None:
        self._restore_from_tray()
        self.goto_tab("mod")

    def _quit_from_tray(self) -> None:
        self._force_quit = True
        if self.tray is not None:
            self.tray.hide()
        if self.close():
            QApplication.quit()

    # ------------------------------------------------------------- 下载页联动
    def _sync_content_timer(self, name: str) -> None:
        timer = getattr(self, "_content_timer", None)
        if timer is None:
            return
        if name == "download":
            if not timer.isActive():
                timer.start()
        else:
            timer.stop()

    def _refresh_download_content(self) -> None:
        """下载页内容每 100 秒静默拉取一次；窗口隐藏到托盘时暂停。"""
        if not self.isVisible():
            return
        page = self.pages.get("download")
        if page is None or self.stack.currentWidget() is not page:
            return
        if self._current_tab in ("game", "java"):
            return
        content = getattr(self, "content_pages", {}).get(self._current_tab)
        if content is not None:
            content.live_refresh()

    def _build_home_page(self) -> QWidget:
        """启动页：左 20k（头像 / 名称 / 启动），右 55k（版本条 / 模组存档 / 配置）。"""
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        left = QWidget()
        left.setFixedWidth(LEFT_W)
        column = QVBoxLayout(left)
        column.setContentsMargins(SIDE_PAD, u(1), SIDE_PAD, u(2))
        column.setSpacing(0)

        panel = Card(margins=(u(1.25), u(1.25), u(1.25), u(1.25)))

        self.avatar = QLabel()
        self.avatar.setFixedSize(AVATAR_PX, AVATAR_PX)
        self.avatar.setPixmap(self._avatar_pixmap(self.config.username, AVATAR_PX))
        avatar_row = QHBoxLayout()
        avatar_row.setContentsMargins(0, 0, 0, 0)
        avatar_row.addStretch(1)
        avatar_row.addWidget(self.avatar)
        avatar_row.addStretch(1)
        panel.add_layout(avatar_row)

        caption = self._label("离线账户", "mutedSmall")
        caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        panel.add(caption)

        self.account_name = QLineEdit(self.config.username)
        self.account_name.setObjectName("heroName")
        self.account_name.setFixedHeight(u(2.5))
        self.account_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.account_name.setMaxLength(16)
        self.account_name.setToolTip("离线账户名：点击即可修改，回车生效")
        self.account_name.editingFinished.connect(self._apply_account_name)
        panel.add(self.account_name)

        daily = QFrame()
        daily.setObjectName("dailyCard")
        daily.setStyleSheet(
            "QFrame#dailyCard { background: rgba(247, 249, 253, 0.58);"
            " border: 1px solid rgba(227, 233, 242, 0.80);"
            " border-radius: 10px; }"
        )
        daily_layout = QVBoxLayout(daily)
        daily_layout.setContentsMargins(10, 7, 10, 8)
        daily_layout.setSpacing(3)
        daily_head = QLabel("每日一言")
        daily_head.setStyleSheet(f"color: {MUTED}; font-size: 8pt; font-weight: 600;")
        daily_layout.addWidget(daily_head)
        self.daily_label = QLabel(FALLBACK.display)
        self.daily_label.setWordWrap(True)
        self.daily_label.setMaximumHeight(60)
        self.daily_label.setStyleSheet(f"color: {MUTED}; font-size: 8.6pt;")
        daily_layout.addWidget(self.daily_label)
        panel.add(daily)

        panel.body.addStretch(1)
        self.home_hint = self._label("", "muted")
        self.home_hint.setWordWrap(True)
        self.home_hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        panel.add(self.home_hint)

        panel.body.addSpacing(u(0.75))
        self.launch_button = QPushButton("启动游戏")
        self.launch_button.setObjectName("accentBig")
        self.launch_button.setFixedHeight(u(3.5))
        self.launch_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.launch_button.setIcon(QIcon(icon_pixmap("play", 20, "#FFFFFF")))
        self.launch_button.setIconSize(QSize(20, 20))
        self.launch_button.clicked.connect(self._launch_or_stop)
        panel.add(self.launch_button)

        panel.body.addSpacing(u(0.75))
        # 左栏固定宽度有限：把五字的「自定义皮肤」单独占整行，另两个四字按钮平分下一行，
        # 避免三按钮等宽时文字被挤压裁切。
        skin_button = QPushButton("自定义皮肤")
        skin_button.setObjectName("skinEntry")
        skin_button.setFixedHeight(u(2.25))
        skin_button.setCursor(Qt.CursorShape.PointingHandCursor)
        skin_button.setIcon(QIcon(icon_pixmap("person", 15, ACCENT)))
        skin_button.setIconSize(QSize(15, 15))
        skin_button.setToolTip("上传自定义皮肤（需镜像支持 /skin）")
        skin_button.clicked.connect(self.open_skin_page)
        panel.add(skin_button)

        actions = QHBoxLayout()
        actions.setSpacing(u(0.75))
        instance_button = QPushButton("版本设置")
        instance_button.setFixedHeight(u(2.25))
        instance_button.setCursor(Qt.CursorShape.PointingHandCursor)
        instance_button.setIcon(QIcon(icon_pixmap("settings", 15, "#5B6678")))
        instance_button.setIconSize(QSize(15, 15))
        instance_button.clicked.connect(self._open_instance_dialog)
        actions.addWidget(instance_button, 1)
        open_button = QPushButton("打开目录")
        open_button.setFixedHeight(u(2.25))
        open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        open_button.setIcon(QIcon(icon_pixmap("folder", 15, "#5B6678")))
        open_button.setIconSize(QSize(15, 15))
        open_button.clicked.connect(self._open_game_dir)
        actions.addWidget(open_button, 1)
        panel.add_layout(actions)

        column.addWidget(panel)
        layout.addWidget(left)

        right = QWidget()
        right_column = QVBoxLayout(right)
        right_column.setContentsMargins(0, 0, 0, 0)
        right_column.setSpacing(0)
        # 5k + 25k + 15k = 45k，随窗口高度等比缩放
        right_column.addWidget(self._build_home_version_bar(), 5)
        right_column.addWidget(self._build_home_middle(), 25)
        right_column.addWidget(self._build_home_config(), 15)
        layout.addWidget(right, 1)
        self._load_daily_sentence()
        return page

    def _load_daily_sentence(self) -> None:
        """每日一言：先沿用当天缓存，没有缓存再到后台拉取，保证不阻塞启动。"""
        data_dir = config_dir(self.data_dir)

        def work() -> None:
            sentence = load_cached(data_dir)
            if sentence is None:
                try:
                    downloader = self.make_downloader(progress=False)
                    sentence = today_sentence(data_dir, downloader=downloader)
                except Exception:  # noqa: BLE001 - 彩蛋文案不能影响启动
                    sentence = FALLBACK
            self.post(lambda s=sentence: self._apply_daily_sentence(s))

        threading.Thread(target=work, daemon=True).start()

    def _apply_daily_sentence(self, sentence) -> None:
        label = getattr(self, "daily_label", None)
        if label is None:
            return
        label.setText(sentence.display)
        label.setToolTip(sentence.display)

    def _build_home_version_bar(self) -> QWidget:
        """右侧顶部 5k：左边是当前版本（可下拉切换），右边是目录路径。"""
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(SIDE_PAD, u(0.75), SIDE_PAD, u(0.75))
        row.setSpacing(u(0.75))

        self.version_combo = QComboBox()
        self.version_combo.setObjectName("heroVersion")
        self.version_combo.setMinimumWidth(u(17))
        self.version_combo.setFixedHeight(u(3.5))
        self.version_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.version_combo.setToolTip("当前版本：点击下拉切换")
        self.version_combo.currentIndexChanged.connect(self._on_version_selected)
        self.version_combo.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.version_combo.customContextMenuRequested.connect(
            lambda pos: self._instance_menu(self.version_combo.mapToGlobal(pos))
        )
        row.addWidget(self.version_combo)

        self.instance_menu_button = QPushButton("⋯")
        self.instance_menu_button.setObjectName("heroMenu")
        self.instance_menu_button.setFixedSize(u(2.5), u(3.5))
        self.instance_menu_button.setToolTip("版本设置 / 安装加载器 / 打开目录 / 删除实例")
        self.instance_menu_button.clicked.connect(
            lambda: self._instance_menu(
                self.instance_menu_button.mapToGlobal(QPoint(u(0.5), u(3.5)))
            )
        )
        row.addWidget(self.instance_menu_button)

        self.path_label = ElidedLabel("")
        self.path_label.setObjectName("mutedSmall")
        self.path_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.path_label.setToolTip("当前版本的目录")
        row.addWidget(self.path_label, 1)
        return bar

    def _build_home_middle(self) -> QWidget:
        """右侧中部 25k：左 27k 是模组列表，右 18k 是存档列表。"""
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        mods = Card(margins=(u(1.25), u(0.75), u(1.25), u(1)))
        head = QHBoxLayout()
        head.setSpacing(u(0.5))
        head.addWidget(self._label("模组", "h2"))
        self.mods_count = self._label("", "mutedSmall")
        head.addWidget(self.mods_count)
        head.addStretch(1)
        mods_open = QPushButton("打开目录")
        mods_open.setObjectName("segment")
        mods_open.setToolTip("打开当前版本的 mods 目录")
        mods_open.setIcon(QIcon(icon_pixmap("folder", 14, "#5B6678")))
        mods_open.setIconSize(QSize(14, 14))
        mods_open.clicked.connect(self._open_mods_dir)
        head.addWidget(mods_open)
        mods.add_layout(head)
        self.mods_list = QListWidget()
        self.mods_list.setObjectName("homeList")
        self.mods_list.setFrameShape(QFrame.Shape.NoFrame)
        mods.add(self.mods_list, 1)
        row.addWidget(mods, 27)

        saves = Card(margins=(u(1.25), u(0.75), u(1.25), u(1)))
        head2 = QHBoxLayout()
        head2.setSpacing(u(0.5))
        head2.addWidget(self._label("存档", "h2"))
        self.saves_count = self._label("", "mutedSmall")
        head2.addWidget(self.saves_count)
        head2.addStretch(1)
        saves_open = QPushButton("打开目录")
        saves_open.setObjectName("segment")
        saves_open.setToolTip("打开当前版本的 saves 目录")
        saves_open.setIcon(QIcon(icon_pixmap("folder", 14, "#5B6678")))
        saves_open.setIconSize(QSize(14, 14))
        saves_open.clicked.connect(self._open_saves_dir)
        head2.addWidget(saves_open)
        saves.add_layout(head2)
        self.saves_list = QListWidget()
        self.saves_list.setObjectName("homeList")
        self.saves_list.setFrameShape(QFrame.Shape.NoFrame)
        saves.add(self.saves_list, 1)
        row.addWidget(saves, 18)
        return holder

    def _build_home_config(self) -> QWidget:
        """右侧底部 15k：内存与 Java（下拉框修改）。"""
        card = Card(margins=(u(1.25), u(0.75), u(1.25), u(1.25)))
        head = QHBoxLayout()
        head.setSpacing(u(0.5))
        head.addWidget(self._label("配置", "h2"))
        head.addStretch(1)
        self.config_hint = self._label("", "mutedSmall")
        head.addWidget(self.config_hint)
        card.add_layout(head)

        memory_row = QHBoxLayout()
        memory_row.setSpacing(u(0.75))
        memory_row.addWidget(self._label("内存"))
        self.home_memory = QSlider(Qt.Orientation.Horizontal)
        self.home_memory.setRange(1024, 32768)
        self.home_memory.setSingleStep(512)
        self.home_memory.setPageStep(1024)
        self.home_memory.setValue(int(self.config.memory_mb))
        self.home_memory.valueChanged.connect(self._on_memory_changed)
        memory_row.addWidget(self.home_memory, 1)
        self.memory_value = QLabel(f"{self.config.memory_mb} MB")
        self.memory_value.setMinimumWidth(u(5))
        self.memory_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.memory_value.setStyleSheet("font-weight: 700;")
        memory_row.addWidget(self.memory_value)
        card.add_layout(memory_row)

        java_row = QHBoxLayout()
        java_row.setSpacing(u(0.75))
        java_row.addWidget(self._label("Java"))
        self.home_java = QComboBox()
        self.home_java.setToolTip("启动游戏使用的 Java（自动选择会按版本要求匹配）")
        self.home_java.currentIndexChanged.connect(self._on_home_java_changed)
        java_row.addWidget(self.home_java, 1)
        detect = QPushButton("重新检测")
        detect.setIcon(QIcon(icon_pixmap("refresh", 14, "#5B6678")))
        detect.setIconSize(QSize(14, 14))
        detect.clicked.connect(self._refresh_java_combo)
        java_row.addWidget(detect)
        card.add_layout(java_row)

        jvm_row = QHBoxLayout()
        jvm_row.setSpacing(u(0.75))
        jvm_row.addWidget(self._label("参数"))
        self.home_jvm = QLineEdit(" ".join(self.config.extra_jvm))
        self.home_jvm.setPlaceholderText("额外 JVM 参数，例如 -XX:+UseG1GC")
        self.home_jvm.editingFinished.connect(self._apply_home_jvm)
        jvm_row.addWidget(self.home_jvm, 1)
        card.add_layout(jvm_row)
        card.body.addStretch(1)

        more_row = QHBoxLayout()
        more_row.setSpacing(u(0.75))
        note = self._label("参数与 Java 会随启动命令一起生效", "mutedSmall")
        more_row.addWidget(note)
        more_row.addStretch(1)
        more = QPushButton("更多设置")
        more.setFixedHeight(u(2))
        more.clicked.connect(lambda: self.show_page("settings"))
        more_row.addWidget(more)
        card.add_layout(more_row)
        return card

    def _apply_home_jvm(self) -> None:
        """启动页的 JVM 参数：回车即保存，并同步设置页。"""
        self.config.extra_jvm = [part for part in self.home_jvm.text().split(" ") if part]
        save_config(self.config, self.data_dir)
        if hasattr(self, "settings_jvm"):
            self.settings_jvm.setText(" ".join(self.config.extra_jvm))

    def _home_content_dir(self, name: str) -> Path:
        instance = self.selected
        base = self.game_dir_for(instance) if instance is not None else self.game_dir()
        return base / name

    def _open_mods_dir(self) -> None:
        _reveal(self._home_content_dir("mods"))

    def _open_saves_dir(self) -> None:
        _reveal(self._home_content_dir("saves"))

    @staticmethod
    def _add_empty_item(widget: QListWidget, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        widget.addItem(item)

    @staticmethod
    def _list_files(folder: Path, suffixes: tuple[str, ...]) -> list[tuple[Path, bool]]:
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return []
        found: list[tuple[Path, bool]] = []
        for path in entries:
            if not path.is_file():
                continue
            lowered = path.name.lower()
            if lowered.endswith(suffixes):
                found.append((path, lowered.endswith(".disabled")))
        return found

    @staticmethod
    def _list_dirs(folder: Path) -> list[Path]:
        try:
            return sorted(
                (path for path in folder.iterdir() if path.is_dir()),
                key=lambda p: p.name.lower(),
            )
        except OSError:
            return []

    def _refresh_home_lists(self) -> None:
        """重新读取当前版本的 mods / saves 目录。"""
        if not hasattr(self, "mods_list"):
            return
        instance = self.selected
        base = self.game_dir_for(instance) if instance is not None else self.game_dir()
        mods = self._list_files(base / "mods", (".jar", ".zip", ".jar.disabled", ".zip.disabled"))
        self.mods_list.clear()
        for path, disabled in mods:
            text = f"{path.name}（已禁用）" if disabled else path.name
            item = QListWidgetItem(QIcon(block_pixmap(path.name, 20)), text)
            item.setToolTip(str(path))
            self.mods_list.addItem(item)
        self.mods_count.setText(f"{len(mods)} 个" if mods else "")
        if not mods:
            self._add_empty_item(self.mods_list, "暂无模组")

        saves = self._list_dirs(base / "saves")
        self.saves_list.clear()
        for folder in saves:
            item = QListWidgetItem(QIcon(block_pixmap(folder.name, 20)), folder.name)
            try:
                item.setToolTip(f"{folder}\n{human_date(folder.stat().st_mtime)}")
            except OSError:
                item.setToolTip(str(folder))
            self.saves_list.addItem(item)
        self.saves_count.setText(f"{len(saves)} 个" if saves else "")
        if not saves:
            self._add_empty_item(self.saves_list, "暂无存档")

    def _build_download_page(self) -> QWidget:
        """下载页：左侧 20k 分类导航，右侧 55k 内容。"""
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        left = QWidget()
        left.setFixedWidth(LEFT_W)
        column = QVBoxLayout(left)
        column.setContentsMargins(SIDE_PAD, u(1), SIDE_PAD, u(2))
        column.setSpacing(u(0.5))
        column.addWidget(self._label("下载", "title"))
        self.download_sub = self._label("游戏版本、模组、整合包、资源包与光影，全部经由自建镜像加速")
        self.download_sub.setWordWrap(True)
        column.addWidget(self.download_sub)
        column.addSpacing(u(1.5))

        self.tab_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for key, text in DOWNLOAD_TABS:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setObjectName("nav")
            button.setMinimumHeight(u(2.75))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._set_tab_icon(button, key, False)
            button.toggled.connect(
                lambda checked, b=button, k=key: self._set_tab_icon(b, k, checked)
            )
            button.clicked.connect(lambda _c, name=key: self.switch_tab(name))
            group.addButton(button)
            column.addWidget(button)
            self.tab_buttons[key] = button
        column.addStretch(1)

        badge_row = QHBoxLayout()
        badge_row.setContentsMargins(0, 0, 0, 0)
        self.source_badge = Badge("", "green")
        self.source_badge.setVisible(False)
        badge_row.addWidget(self.source_badge)
        badge_row.addStretch(1)
        column.addLayout(badge_row)
        layout.addWidget(left)

        right = QWidget()
        right_column = QVBoxLayout(right)
        right_column.setContentsMargins(0, u(1), SIDE_PAD, u(2.5))
        right_column.setSpacing(0)

        self.dl_stack = QStackedWidget()
        self.dl_stack.addWidget(self._build_game_tab())
        self.content_pages: dict[str, ContentPage] = {}
        for key, _text in CONTENT_TABS:
            content = ContentPage(self, key)
            self.content_pages[key] = content
            self.dl_stack.addWidget(content)
        self.dl_stack.addWidget(self._build_java_tab())
        self.install_page = InstallPage(self)
        self.dl_stack.addWidget(self.install_page)
        right_column.addWidget(self.dl_stack, 1)
        layout.addWidget(right, 1)

        self.tab_buttons["game"].setChecked(True)
        return page

    def _build_game_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        banner = QFrame()
        banner.setObjectName("softCard")
        banner_layout = QHBoxLayout(banner)
        banner_layout.setContentsMargins(16, 12, 16, 12)
        banner_layout.setSpacing(12)
        self.banner_icon = QLabel()
        self.banner_icon.setFixedSize(44, 44)
        self.banner_icon.setPixmap(block_pixmap("latest", 44))
        banner_layout.addWidget(self.banner_icon)
        banner_text = QVBoxLayout()
        banner_text.setSpacing(1)
        self.banner_title = QLabel("最新正式版")
        self.banner_title.setObjectName("h3")
        banner_text.addWidget(self.banner_title)
        self.banner_hint = self._label("正在获取版本清单……")
        banner_text.addWidget(self.banner_hint)
        banner_layout.addLayout(banner_text, 1)
        self.banner_button = QPushButton("安装最新版")
        self.banner_button.setObjectName("accent")
        self.banner_button.setIcon(QIcon(icon_pixmap("download", 15, "#FFFFFF")))
        self.banner_button.setIconSize(QSize(15, 15))
        self.banner_button.setEnabled(False)
        self.banner_button.clicked.connect(self._install_latest)
        banner_layout.addWidget(self.banner_button)
        layout.addWidget(banner)

        filters = Card(margins=(12, 10, 12, 10))
        row = QHBoxLayout()
        row.setSpacing(10)
        segment_wrap = QFrame()
        segment_wrap.setObjectName("softCard")
        segment_layout = QHBoxLayout(segment_wrap)
        segment_layout.setContentsMargins(4, 3, 4, 3)
        segment_layout.setSpacing(2)
        self.segment_buttons: dict[str, QPushButton] = {}
        segment_group = QButtonGroup(self)
        segment_group.setExclusive(True)
        for key, text in SEGMENTS:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setObjectName("segment")
            button.clicked.connect(lambda _c, name=key: self._on_segment(name))
            segment_group.addButton(button)
            segment_layout.addWidget(button)
            self.segment_buttons[key] = button
        self.segment_buttons["release"].setChecked(True)
        row.addWidget(segment_wrap)
        row.addStretch(1)
        self.game_search = QLineEdit()
        self.game_search.setPlaceholderText("搜索版本号")
        self.game_search.setMaximumWidth(220)
        self.game_search.textChanged.connect(lambda _text: self._render_versions())
        row.addWidget(self.game_search)
        filters.add_layout(row)
        layout.addWidget(filters)

        body = Card(margins=(12, 10, 12, 10))
        self.game_tree = QTreeWidget()
        self.game_tree.setHeaderLabels(["版本", "类型", "发布时间", "状态"])
        self.game_tree.setRootIsDecorated(False)
        self.game_tree.setUniformRowHeights(True)
        self.game_tree.itemDoubleClicked.connect(lambda _i, _c: self._on_game_action())
        head = self.game_tree.header()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        body.add(self.game_tree, 1)
        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.game_spinner = Spinner(15, ACCENT)
        footer.addWidget(self.game_spinner)
        self.game_count = self._label("")
        footer.addWidget(self.game_count)
        footer.addStretch(1)
        self.game_install_button = QPushButton("安装所选版本")
        self.game_install_button.setObjectName("accent")
        self.game_install_button.setIcon(QIcon(icon_pixmap("download", 15, "#FFFFFF")))
        self.game_install_button.setIconSize(QSize(15, 15))
        self.game_install_button.clicked.connect(self._on_game_action)
        footer.addWidget(self.game_install_button)
        body.add_layout(footer)
        layout.addWidget(body, 1)
        # 下载页懒构建：建好后补一次可能已加载的清单（横幅 + 版本表）
        self._update_banner()
        self._render_versions()
        return page

    def _build_plugins_page(self) -> QWidget:
        """插件页：左 20k 分区导航，右侧渲染官方内置插件（其余分区保留框架）。"""
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        left = QWidget()
        left.setFixedWidth(LEFT_W)
        column = QVBoxLayout(left)
        column.setContentsMargins(SIDE_PAD, u(1), SIDE_PAD, u(2))
        column.setSpacing(u(0.5))
        column.addWidget(self._label("插件", "title"))
        intro = self._label("仅官方内置插件；不动态加载第三方代码。")
        intro.setWordWrap(True)
        column.addWidget(intro)
        column.addSpacing(u(1.5))

        self.plugin_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for key, text in PLUGIN_TABS:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setObjectName("nav")
            button.setMinimumHeight(u(2.75))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _c, name=key: self._switch_plugin(name))
            group.addButton(button)
            column.addWidget(button)
            self.plugin_buttons[key] = button
        column.addStretch(1)
        self.plugin_badge = Badge("官方", "gray")
        badge_row = QHBoxLayout()
        badge_row.setContentsMargins(0, 0, 0, 0)
        badge_row.addWidget(self.plugin_badge)
        badge_row.addStretch(1)
        column.addLayout(badge_row)
        layout.addWidget(left)

        # 右侧：官方列表与其余分区空状态二选一
        self.plugin_detail_stack = QStackedWidget()
        self.plugin_detail_stack.addWidget(self._build_plugin_stub())       # index 0
        self.plugin_detail_stack.addWidget(self._build_plugin_official())   # index 1
        layout.addWidget(self.plugin_detail_stack, 1)
        self._switch_plugin("official")
        return page

    def _build_plugin_official(self) -> QWidget:
        wrap = QWidget()
        wrap_column = QVBoxLayout(wrap)
        wrap_column.setContentsMargins(0, u(1), SIDE_PAD, u(2.5))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        host = QWidget()
        self._plugin_official_layout = QVBoxLayout(host)
        self._plugin_official_layout.setContentsMargins(0, 0, u(0.5), 0)
        self._plugin_official_layout.setSpacing(10)
        self._plugin_official_layout.addStretch(1)
        scroll.setWidget(host)
        wrap_column.addWidget(scroll, 1)
        return wrap

    def _build_plugin_stub(self) -> QWidget:
        empty = Card(margins=(28, 28, 28, 22))
        icon_row = QHBoxLayout()
        icon_row.addStretch(1)
        icon = QLabel()
        icon.setFixedSize(72, 72)
        icon.setPixmap(block_pixmap("plugins", 72))
        icon_row.addWidget(icon)
        icon_row.addStretch(1)
        empty.add_layout(icon_row)
        self.plugin_title = QLabel("插件市场")
        self.plugin_title.setObjectName("h2")
        self.plugin_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.add(self.plugin_title)
        self.plugin_desc = QLabel("")
        self.plugin_desc.setObjectName("muted")
        self.plugin_desc.setWordWrap(True)
        self.plugin_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.add(self.plugin_desc)
        empty.body.addStretch(1)
        tiles = QGridLayout()
        tiles.setSpacing(10)
        entries = [
            ("浏览插件", "连接插件源（待接入）"),
            ("导入本地插件", "从 .zip / .py 安装（暂不支持）"),
            ("插件目录", "打开插件存放目录"),
        ]
        for index, (title, hint) in enumerate(entries):
            tiles.addWidget(Tile(title, hint, lambda _c=False, t=title: self._plugin_stub(t)), 0, index)
        empty.add_layout(tiles)
        return empty

    PLUGIN_DESCS = {
        "market": "插件市场：后续可对接插件索引，目前仅保留页面框架与导航。",
        "official": "官方插件：随启动器发布的插件列在这里。",
        "community": "社区插件：来自社区仓库的插件将在这里展示（不加载第三方代码）。",
        "other": "其他插件：手动导入或第三方来源（暂不支持）。",
        "downloaded": "已下载插件：等同于官方已启用列表。",
    }

    def _switch_plugin(self, key: str) -> None:
        for name, button in self.plugin_buttons.items():
            button.setChecked(name == key)
        official = key in ("official", "downloaded")
        self.plugin_detail_stack.setCurrentIndex(1 if official else 0)
        if official:
            self._rebuild_official_plugins()
            return
        for name, text in PLUGIN_TABS:
            if name == key:
                self.plugin_title.setText(text)
                break
        self.plugin_desc.setText(self.PLUGIN_DESCS.get(key, ""))

    def _rebuild_official_plugins(self) -> None:
        layout = getattr(self, "_plugin_official_layout", None)
        if layout is None:
            return
        while layout.count() > 1:
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        records = self.plugin_registry.records
        # 离线联机作为特权插件：置顶专用卡片（状态机），不重复进通用插件卡列表
        layout.insertWidget(layout.count() - 1, self._make_offline_mp_card())
        for record in records:
            if record.manifest.id == "offline-multiplayer":
                continue
            layout.insertWidget(layout.count() - 1, self._make_plugin_card(record))
        tone = "green" if any(r.status == "loaded" for r in records) else "gray"
        self.plugin_badge.set(f"官方 · {len(records)} 个", tone)

    def _make_plugin_card(self, record) -> QWidget:
        card = Card()
        top = QHBoxLayout()
        manifest = record.manifest
        name = QLabel(manifest.name)
        name.setStyleSheet("font-size: 11pt; font-weight: 700;")
        top.addWidget(name)
        status_text, tone = self._plugin_status(record)
        top.addWidget(Badge(status_text, tone))
        top.addWidget(Badge(f"v{manifest.version}", "gray"))
        top.addWidget(Badge("官方" if manifest.official else "第三方", "purple"))
        top.addStretch(1)
        toggle = QPushButton("停用" if record.enabled else "启用")
        toggle.setObjectName("ghost")
        toggle.setEnabled(record.status != "incompatible")
        toggle.clicked.connect(
            lambda _c, pid=manifest.id, en=record.enabled: self._toggle_plugin(pid, not en)
        )
        top.addWidget(toggle)
        card.add_layout(top)
        desc = QLabel(manifest.description)
        desc.setObjectName("muted")
        desc.setWordWrap(True)
        card.add(desc)
        caps = "、".join(manifest.capabilities) or "无"
        card.add(self._label(f"能力：{caps}", "mutedSmall"))
        if record.error:
            err = QLabel(record.error)
            err.setStyleSheet(f"color: {DANGER}; font-size: 8.5pt;")
            err.setWordWrap(True)
            card.add(err)
        return card

    @staticmethod
    def _plugin_status(record) -> tuple[str, str]:
        return {
            "loaded": ("已加载", "green"),
            "disabled": ("未启用", "gray"),
            "incompatible": ("不兼容", "amber"),
            "error": ("加载失败", "red"),
        }.get(record.status, (record.status, "gray"))

    def _toggle_plugin(self, plugin_id: str, enabled: bool) -> None:
        if self.plugin_registry.set_enabled(plugin_id, enabled):
            self.notice(("已启用 " if enabled else "已停用 ") + plugin_id, "INFO")
        else:
            self.notice("该插件当前不可切换", "WARN")
        self._rebuild_official_plugins()

    # -------------------------------------------------- 离线联机特权插件卡片
    def _make_offline_mp_card(self) -> QWidget:
        """插件页顶部专用卡片：未下载 / 已装未启用 / 已启用 三态，入口只在此。"""
        from offcraft.plugins import bundle

        card = Card()
        top = QHBoxLayout()
        name = QLabel("离线联机")
        name.setStyleSheet("font-size: 11pt; font-weight: 700;")
        top.addWidget(name)
        installed = bundle.is_installed(self.data_dir)
        record = self.plugin_registry.get(bundle.PLUGIN_ID)
        if not installed:
            state, tone = "未下载", "gray"
        elif record is not None and record.status == "loaded":
            state, tone = "已启用", "green"
        elif record is not None and record.status == "error":
            state, tone = "加载失败", "red"
        else:
            state, tone = "已装未启用", "amber"
        top.addWidget(Badge(state, tone))
        top.addWidget(Badge("官方 · 特权", "purple"))
        top.addStretch(1)
        if not installed:
            download = QPushButton("下载")
            download.setObjectName("primary")
            download.setMinimumHeight(30)
            download.clicked.connect(self._download_offline_mp)
            top.addWidget(download)
        elif record is not None and record.status == "loaded":
            enter = QPushButton("进入")
            enter.setObjectName("primary")
            enter.setMinimumHeight(30)
            enter.clicked.connect(self._open_offline_mp)
            disable = QPushButton("停用")
            disable.setObjectName("ghost")
            disable.setMinimumHeight(30)
            disable.clicked.connect(lambda _c=False: self._set_offline_enabled(False))
            top.addWidget(enter)
            top.addWidget(disable)
        else:
            enable = QPushButton("启用")
            enable.setObjectName("primary")
            enable.setMinimumHeight(30)
            enable.clicked.connect(lambda _c=False: self._set_offline_enabled(True))
            redownload = QPushButton("重新下载")
            redownload.setObjectName("ghost")
            redownload.setMinimumHeight(30)
            redownload.clicked.connect(self._download_offline_mp)
            top.addWidget(enable)
            top.addWidget(redownload)
        card.add_layout(top)
        desc = QLabel(
            "把不同局域网的设备拉进同一虚拟房间（端到端加密，经服务端中继只见密文）。"
            "按需下载，基础安装包不附带；启用后方可创建/开放/加入房间。"
        )
        desc.setObjectName("muted")
        desc.setWordWrap(True)
        card.add(desc)
        card.add(self._label("能力：net.tunnel（仅官方特权，第三方不可得）", "mutedSmall"))
        if record is not None and record.error:
            err = QLabel(record.error)
            err.setStyleSheet(f"color: {DANGER}; font-size: 8.5pt;")
            err.setWordWrap(True)
            card.add(err)
        return card

    def _reload_offline_registry(self) -> None:
        from offcraft.plugins import bundle

        if self.plugin_registry.get(bundle.PLUGIN_ID) is None:
            self.plugin_registry.reload()

    def _set_offline_enabled(self, enabled: bool) -> None:
        from offcraft.plugins import bundle

        if not bundle.is_installed(self.data_dir):
            self.notice("请先下载离线联机插件。", "WARN")
            return
        self._reload_offline_registry()
        if self.plugin_registry.set_enabled(bundle.PLUGIN_ID, enabled):
            self.notice(("已启用 离线联机" if enabled else "已停用 离线联机"), "INFO")
        else:
            self.notice("插件当前不可切换（可能完整性校验失败）", "WARN")
        self._rebuild_official_plugins()

    def _open_offline_mp(self) -> None:
        """「进入」：把离线联机面板包成对话框打开（会话随对话框关闭清理）。"""
        from offcraft.ui.p2p_panel import P2PPanel

        dialog = QDialog(self)
        dialog.setWindowTitle("离线联机")
        dialog.resize(u(44), u(32))
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        panel = P2PPanel(self)
        layout.addWidget(panel)
        self._offline_panel = panel
        try:
            dialog.exec()
        finally:
            try:
                panel.shutdown()
            except Exception:  # noqa: BLE001 - 清理尽力而为
                pass
            self._offline_panel = None

    def _download_offline_mp(self) -> None:
        """后台拉取服务端发布的插件包：先校 zip sha256 再解包落地，然后刷新卡片。"""
        from offcraft.plugins import bundle

        base = (self.config.download_base or "").rstrip("/")
        if not base:
            self.notice("未配置下载源，无法下载离线联机插件。", "WARN")
            return
        self.notice("正在下载离线联机插件……", "INFO")

        def worker() -> None:
            try:
                downloader = self.make_downloader(progress=False)
                manifest = downloader.fetch_json(base + bundle.MANIFEST_PATH) or {}
                zip_bytes = downloader.fetch_bytes(base + bundle.ZIP_PATH)
                bundle.install_zip(
                    self.data_dir, zip_bytes,
                    expected_sha256=str(manifest.get("sha256") or ""),
                    abi=str(manifest.get("abi") or ""),
                )
                self.plugin_registry.reload()
                self.post(lambda: (self._rebuild_official_plugins(),
                                   self.notice("离线联机插件已下载，点「启用」开始使用。", "INFO")))
            except Exception as exc:  # noqa: BLE001 - 下载/校验失败不阻断 UI
                message = f"下载失败：{exc}"
                self.post(lambda m=message: self.notice(m, "WARN"))
                self.post(lambda: self._rebuild_official_plugins())

        threading.Thread(target=worker, name="plugin-offline-dl", daemon=True).start()

    def _refresh_plugins_page(self) -> None:
        if self.pages.get("plugins") is not None:
            self._rebuild_official_plugins()

    def _plugin_stub(self, title: str) -> None:
        if title == "插件目录":
            _reveal(self.plugin_data_dir())
            return
        self.notice(f"{title}：仅官方内置插件，第三方接口暂不开放", "WARN")

    # ------------------------------------------------------------- 联机 / 皮肤
    def _build_online_page(self) -> QWidget:
        self.online_page = OnlinePage(self)
        return self.online_page

    def plugin_data_dir(self) -> Path:
        """收藏 / 最近 / 探测缓存 / 插件状态所在目录（与 config.json 同目录）。"""
        return config_dir(self.data_dir)

    @staticmethod
    def _version_at_least(mc_version: str, target: tuple[int, int, int]) -> bool:
        parts: list[int] = []
        for token in re.split(r"[.\-+]", str(mc_version or "")):
            if token.isdigit():
                parts.append(int(token))
            else:
                break
        return tuple(parts) >= target

    def join_server(self, address: str) -> None:
        """加入游戏：≥1.20.2 走 quickPlayMultiplayer 直连，否则复制地址提示手动添加。"""
        QApplication.clipboard().setText(address)
        instance = self.selected
        if instance is None:
            self.notice(f"已复制地址 {address}：请先在启动页选择一个版本，再加入服务器", "WARN")
            return
        if not self._version_at_least(instance.mc_version, (1, 20, 2)):
            self.notice(
                f"已复制地址 {address}：{instance.mc_version} 不支持直连，请在游戏内手动添加服务器",
                "INFO",
            )
            return
        self.launch_game(instance, quick_play=address)

    def _avatar_pixmap(self, name: str, size: int) -> QPixmap:
        """账号头像：优先本地缓存皮肤 → 内置默认皮肤 → 首字母色块。"""
        data = SkinPage.current_avatar(name, size)
        if data:
            pixmap = QPixmap()
            if pixmap.loadFromData(data) and self._has_visible_pixels(pixmap):
                return self._round_pixmap(pixmap, size)
        # 缓存皮肤损坏（全透明）时回退到内置默认皮肤，避免头像留白
        data = head_avatar(default_skin_bytes(), size=size)
        pixmap = QPixmap()
        if data and pixmap.loadFromData(data):
            return self._round_pixmap(pixmap, size)
        return initial_pixmap(name, size, "account")

    @staticmethod
    def _round_pixmap(pixmap: QPixmap, size: int) -> QPixmap:
        """给头像裁出圆角，跟上卡片风格。"""
        result = QPixmap(pixmap.size())
        result.setDevicePixelRatio(pixmap.devicePixelRatio())
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(pixmap.rect()), size * 0.18, size * 0.18)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()
        return result

    @staticmethod
    def _has_visible_pixels(pixmap: QPixmap) -> bool:
        """头像是否至少有一个不透明像素（全透明的皮肤缓存等同无效）。"""
        image = pixmap.toImage()
        for y in range(0, image.height(), 4):
            for x in range(0, image.width(), 4):
                if image.pixelColor(x, y).alpha() > 0:
                    return True
        return False

    def apply_avatar(self, data: bytes) -> None:
        """上传成功后刷新启动页头像；字节无效时保持初始头像不变。"""
        if not data:
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            scaled = pixmap.scaled(
                AVATAR_PX,
                AVATAR_PX,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.avatar.setPixmap(self._round_pixmap(scaled, AVATAR_PX))

    def open_skin_page(self) -> None:
        """把皮肤页包成对话框弹出（入口：启动页账号行与设置页卡片）。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("自定义皮肤")
        dialog.resize(u(48), u(36))
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        page = SkinPage(self)
        layout.addWidget(page)
        dialog.finished.connect(lambda _code: page.shutdown())
        dialog.exec()

    def register_plugin_nav(self, plugin_id: str, label: str, factory: Callable[[], Any]) -> None:
        """插件申请在导航挂载一项：本次仅登记，不新增顶层入口（官方插件用不到）。"""
        self._plugin_nav_items.append((plugin_id, label, factory))
        self.post_log("INFO", f"[plugins] {plugin_id} 登记了导航项“{label}”（未接入顶层导航）")


    def _build_java_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        banner = QFrame()
        banner.setObjectName("softCard")
        banner_layout = QHBoxLayout(banner)
        banner_layout.setContentsMargins(16, 12, 16, 12)
        banner_layout.setSpacing(12)
        icon = QLabel()
        icon.setFixedSize(44, 44)
        icon.setPixmap(block_pixmap("java", 44))
        banner_layout.addWidget(icon)
        text = QVBoxLayout()
        text.setSpacing(1)
        title = QLabel("Java 运行时")
        title.setObjectName("h3")
        text.addWidget(title)
        text.addWidget(
            self._label("官方 Java 下载；启动游戏与运行 Forge / NeoForge 安装器都要用到")
        )
        banner_layout.addLayout(text, 1)
        refresh = QPushButton("刷新列表")
        refresh.setIcon(QIcon(icon_pixmap("refresh", 15, "#5B6678")))
        refresh.setIconSize(QSize(15, 15))
        refresh.clicked.connect(self._refresh_java_tab)
        banner_layout.addWidget(refresh)
        layout.addWidget(banner)

        body = Card(margins=(12, 10, 12, 10))
        self.java_tree = QTreeWidget()
        self.java_tree.setHeaderLabels(["组件", "版本", "主版本", "状态"])
        self.java_tree.setRootIsDecorated(False)
        self.java_tree.setUniformRowHeights(True)
        head = self.java_tree.header()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        body.add(self.java_tree, 1)

        footer = QHBoxLayout()
        self.java_status = self._label("正在读取 Java 列表……")
        footer.addWidget(self.java_status, 1)
        self.java_set_button = QPushButton("设为启动 Java")
        self.java_set_button.clicked.connect(self._set_selected_java)
        footer.addWidget(self.java_set_button)
        self.java_open_button = QPushButton("打开目录")
        self.java_open_button.setIcon(QIcon(icon_pixmap("folder", 15, "#5B6678")))
        self.java_open_button.setIconSize(QSize(15, 15))
        self.java_open_button.clicked.connect(self._open_java_dir)
        footer.addWidget(self.java_open_button)
        self.java_install_button = QPushButton("下载并安装")
        self.java_install_button.setObjectName("accent")
        self.java_install_button.setIcon(QIcon(icon_pixmap("download", 15, "#FFFFFF")))
        self.java_install_button.setIconSize(QSize(15, 15))
        self.java_install_button.clicked.connect(self._install_selected_java)
        footer.addWidget(self.java_install_button)
        body.add_layout(footer)
        layout.addWidget(body, 1)

        self.java_runtimes: list[JavaRuntime] = []
        self._java_loading = False
        return page

    def _refresh_java_tab(self) -> None:
        if getattr(self, "_java_loading", False):
            return
        self._java_loading = True
        self.java_status.setText("正在读取 Java 列表……")
        downloader = self.make_downloader(progress=False, workers=2)
        mirror = self.source(downloader)

        def worker() -> None:
            try:
                runtimes = list_java_runtimes(mirror, downloader)
            except Exception as exc:  # noqa: BLE001 - 展示给用户
                self.post(lambda: self._java_failed(str(exc)))
                return
            self.post(lambda: self._apply_java_runtimes(runtimes))

        threading.Thread(target=worker, daemon=True).start()

    def _java_failed(self, message: str) -> None:
        self._java_loading = False
        self.java_status.setText(f"获取失败：{message}")

    def _apply_java_runtimes(self, runtimes: list[JavaRuntime]) -> None:
        self._java_loading = False
        self.java_runtimes = list(runtimes)
        mc_dir = self.game_dir()
        self.java_tree.clear()
        installed = 0
        for runtime in runtimes:
            done = runtime.installed(mc_dir)
            installed += int(done)
            node = QTreeWidgetItem(
                [
                    runtime.component,
                    runtime.version or "-",
                    f"Java {runtime.major}" if runtime.major else "-",
                    "已安装" if done else "未安装",
                ]
            )
            node.setData(0, Qt.ItemDataRole.UserRole, runtime)
            node.setIcon(0, QIcon(block_pixmap(runtime.component, 22, "java")))
            self.java_tree.addTopLevelItem(node)
        if self.java_tree.topLevelItemCount():
            self.java_tree.setCurrentItem(self.java_tree.topLevelItem(0))
        self.java_status.setText(
            f"共 {len(runtimes)} 个运行时，已安装 {installed} 个（存于 {mc_dir / 'runtime'}）"
        )

    def _selected_java_runtime(self) -> JavaRuntime | None:
        node = getattr(self, "java_tree", None)
        if node is None:
            return None
        current = node.currentItem()
        return current.data(0, Qt.ItemDataRole.UserRole) if current is not None else None

    def _install_selected_java(self) -> None:
        runtime = self._selected_java_runtime()
        if runtime is None:
            self.notice("请先选择一个 Java 运行时", "WARN")
            return
        mc_dir = self.game_dir()

        def worker(cancel: threading.Event) -> str:
            downloader = self.make_downloader()
            path = install_java_runtime(
                mc_dir,
                runtime,
                mirror=self.source(downloader),
                downloader=downloader,
                cancel=cancel,
                phase=self.set_phase,
            )
            return f"Java 已安装：{path}"

        self.run_task(
            f"下载 {runtime.component}",
            worker,
            on_done=lambda: (self._refresh_java_tab(), self._refresh_java_combo()),
        )

    def _set_selected_java(self) -> None:
        runtime = self._selected_java_runtime()
        if runtime is None:
            self.notice("请先选择一个 Java 运行时", "WARN")
            return
        path = runtime.java_path(self.game_dir())
        if not path.is_file():
            self.notice("先下载这个运行时，再设为启动 Java", "WARN")
            return
        self.config.java_overrides["__global__"] = str(path)
        save_config(self.config, self.data_dir)
        self._refresh_java_combo()
        self.notice(f"启动 Java 已设为 {runtime.component}")

    def _open_java_dir(self) -> None:
        _reveal(self.game_dir() / "runtime")

    def _build_settings_page(self) -> QWidget:
        """设置页：左 20k 标题与操作，右 55k 设置卡片。"""
        page = QWidget()
        outer = QHBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        left = QWidget()
        left.setFixedWidth(LEFT_W)
        side = QVBoxLayout(left)
        side.setContentsMargins(SIDE_PAD, u(1), SIDE_PAD, u(2))
        side.setSpacing(u(0.75))
        side.addWidget(self._label("设置", "title"))
        intro = self._label("游戏目录、版本隔离、Java 与内存在这里调整。")
        intro.setWordWrap(True)
        side.addWidget(intro)
        side.addSpacing(u(1.5))
        save_button = QPushButton("保存设置")
        save_button.setObjectName("accent")
        save_button.setFixedHeight(u(2.5))
        save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        save_button.clicked.connect(self.save_settings)
        side.addWidget(save_button)
        data_button = QPushButton("打开数据目录")
        data_button.clicked.connect(self._open_data_dir)
        side.addWidget(data_button)
        side.addStretch(1)
        outer.addWidget(left)

        right = QWidget()
        right_column = QVBoxLayout(right)
        right_column.setContentsMargins(0, u(1), SIDE_PAD, u(2))
        right_column.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.setSpacing(12)

        look = Card()
        look.add(self._label("外观", "h2"))
        look_hint = self._label(
            "背景支持纯色、PNG 或 GIF。图片与颜色同时启用时，会在图片上蒙一层很淡的颜色；"
            "默认渐变始终保留在最底层，图片带透明通道也不会露底。"
        )
        look_hint.setWordWrap(True)
        look.add(look_hint)

        bg_row = QHBoxLayout()
        bg_row.setSpacing(8)
        self.settings_bg_name = ElidedLabel("")
        bg_row.addWidget(self.settings_bg_name, 1)
        bg_pick = QPushButton("选择图片")
        bg_pick.setIcon(QIcon(icon_pixmap("image", 14, "#5B6678")))
        bg_pick.setIconSize(QSize(14, 14))
        bg_pick.clicked.connect(self._choose_background_image)
        bg_row.addWidget(bg_pick)
        bg_clear = QPushButton("清除")
        bg_clear.setIcon(QIcon(icon_pixmap("close", 12, "#5B6678")))
        bg_clear.setIconSize(QSize(12, 12))
        bg_clear.clicked.connect(self._clear_background_image)
        bg_row.addWidget(bg_clear)
        look.add_layout(bg_row)

        color_row = QHBoxLayout()
        color_row.setSpacing(8)
        self.settings_bg_swatch = QPushButton()
        self.settings_bg_swatch.setFixedSize(30, 30)
        self.settings_bg_swatch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_bg_swatch.setToolTip("打开色盘选择背景颜色")
        self.settings_bg_swatch.clicked.connect(self._choose_background_color)
        color_row.addWidget(self.settings_bg_swatch)
        self.settings_bg_color_text = self._label("")
        color_row.addWidget(self.settings_bg_color_text, 1)
        bg_color_clear = QPushButton("清除颜色")
        bg_color_clear.clicked.connect(self._clear_background_color)
        color_row.addWidget(bg_color_clear)
        look.add_layout(color_row)
        layout.addWidget(look)

        game = Card()
        game.add(self._label("游戏", "h2"))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.settings_game_dir = QLineEdit(self.config.game_dir or str(self.game_dir()))
        row.addWidget(self.settings_game_dir, 1)
        browse = QPushButton("浏览")
        browse.clicked.connect(self._browse_game_dir)
        row.addWidget(browse)
        game.add_layout(row)
        game.add(self._label("游戏目录：版本、模组、存档与配置都保存在这里"))
        self.settings_isolate = QCheckBox("版本隔离（每个版本独立 mods / 存档 / 配置，推荐开启）")
        self.settings_isolate.setChecked(bool(self.config.isolation))
        game.add(self.settings_isolate)
        self.settings_skip_assets = QCheckBox("安装时跳过资源文件（assets，体积更小但部分音效缺失）")
        self.settings_skip_assets.setChecked(bool(self.config.skip_assets))
        game.add(self.settings_skip_assets)
        layout.addWidget(game)

        java = Card()
        java.add(self._label("Java 与内存", "h2"))
        java_row = QHBoxLayout()
        java_row.setSpacing(8)
        self.settings_java = QComboBox()
        java_row.addWidget(self.settings_java, 1)
        detect = QPushButton("重新检测")
        detect.setIcon(QIcon(icon_pixmap("refresh", 14, "#5B6678")))
        detect.setIconSize(QSize(14, 14))
        detect.clicked.connect(self._refresh_java_combo)
        java_row.addWidget(detect)
        add_java = QPushButton("添加 Java")
        add_java.clicked.connect(self._choose_java)
        java_row.addWidget(add_java)
        java.add_layout(java_row)
        self.settings_java.currentIndexChanged.connect(self._on_java_changed)

        memory_row = QHBoxLayout()
        memory_row.setSpacing(10)
        memory_row.addWidget(self._label("内存"))
        self.settings_memory = QSlider(Qt.Orientation.Horizontal)
        self.settings_memory.setRange(1024, 32768)
        self.settings_memory.setSingleStep(512)
        self.settings_memory.setPageStep(1024)
        self.settings_memory.setValue(int(self.config.memory_mb))
        memory_row.addWidget(self.settings_memory, 1)
        self.settings_memory_value = QLabel(f"{self.config.memory_mb} MB")
        self.settings_memory_value.setMinimumWidth(76)
        self.settings_memory_value.setStyleSheet("font-weight: 700;")
        memory_row.addWidget(self.settings_memory_value)
        java.add_layout(memory_row)
        self.settings_memory.valueChanged.connect(
            lambda value: self.settings_memory_value.setText(f"{value} MB")
        )

        self.settings_jvm = QLineEdit(" ".join(self.config.extra_jvm))
        self.settings_jvm.setPlaceholderText("额外的 JVM 参数，例如 -XX:+UseG1GC")
        java.add(self.settings_jvm)
        layout.addWidget(java)

        download = Card()
        download.add(self._label("下载", "h2"))
        # 下载源地址 / 令牌 / 代理不在界面上展示（只能按 F12 打开设置窗口改），
        # 控件保留但不入布局，供 _load_settings_values / save_settings 读写。
        self.settings_base = QLineEdit(self.config.download_base)
        self.settings_token = QLineEdit(self.config.download_token)
        self.settings_token.setPlaceholderText("访问令牌（可选）")
        self.settings_proxy = QLineEdit(self.config.proxy)
        self.settings_proxy.setPlaceholderText("代理（可选，如 http://127.0.0.1:7890）")
        for field in (self.settings_base, self.settings_token, self.settings_proxy):
            field.setReadOnly(True)
            field.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            field.setVisible(False)
        self.settings_fastest = QCheckBox("探测各下载源的延迟与速度，自动择优（下载过慢时切源）")
        self.settings_fastest.setChecked(bool(getattr(self.config, "prefer_fastest", True)))
        self.settings_fastest.setToolTip(
            "开启后，探测前会清理 DNS 缓存，再测各源的首字节延迟与下载速度，\n"
            "按「延迟 + 速率」选出更快的源（结论 10 分钟内复用）；\n"
            "下载过程中速率过低会重新探测并切换到更快的源（断点续传）。"
        )
        download.add(self.settings_fastest)
        workers_row = QHBoxLayout()
        workers_row.setSpacing(8)
        workers_row.addWidget(self._label("并发下载数"))
        self.settings_workers = QSpinBox()
        self.settings_workers.setRange(1, 32)
        self.settings_workers.setValue(int(self.config.max_workers))
        self.settings_workers.setEnabled(False)
        workers_row.addWidget(self.settings_workers)
        workers_row.addStretch(1)
        download.add_layout(workers_row)
        layout.addWidget(download)

        skin_card = Card()
        skin_card.add(self._label("皮肤", "h2"))
        skin_text = self._label("为当前账户上传自定义皮肤（需装了 CustomSkinLoader 的实例才会显示）")
        skin_text.setWordWrap(True)
        skin_card.add(skin_text)
        style_row = QHBoxLayout()
        style_row.setSpacing(8)
        style_row.addWidget(self._label("皮肤页样式"))
        self.settings_skin_layout = QComboBox()
        for key, name in LAYOUT_CHOICES:
            self.settings_skin_layout.addItem(name, key)
        self.settings_skin_layout.setToolTip("保存设置后，重新打开皮肤页即可切换布局")
        style_row.addWidget(self.settings_skin_layout)
        style_row.addStretch(1)
        skin_card.add_layout(style_row)
        skin_open = QPushButton("上传自定义皮肤")
        skin_open.clicked.connect(self.open_skin_page)
        skin_card.add(skin_open)
        layout.addWidget(skin_card)

        if self.plugin_registry.get("diagnostics") is not None:
            layout.addWidget(self._build_diagnostics_card())

        layout.addWidget(self._build_update_card())

        about = Card()
        about.add(self._label("关于", "h2"))
        about_text = self._label(
            f"{APP_NAME} v{__version__} · 自建镜像启动器，仅依赖 Python 标准库 + PyQt6"
        )
        about_text.setWordWrap(True)
        about.add(about_text)
        data_text = self._label(f"数据目录：{config_dir(self.data_dir)}")
        data_text.setWordWrap(True)
        about.add(data_text)
        about_row = QHBoxLayout()
        about_row.setSpacing(8)
        open_data = QPushButton("打开数据目录")
        open_data.clicked.connect(self._open_data_dir)
        about_row.addWidget(open_data)
        open_data.setCursor(Qt.CursorShape.PointingHandCursor)
        easter = QPushButton("千万别点")
        easter.setObjectName("ghost")
        easter.setToolTip("真的别点。")
        easter.setCursor(Qt.CursorShape.PointingHandCursor)
        easter.clicked.connect(self._trigger_easter_egg)
        about_row.addWidget(easter)
        about_row.addStretch(1)
        about.add_layout(about_row)
        layout.addWidget(about)

        layout.addStretch(1)
        scroll.setWidget(holder)
        right_column.addWidget(scroll, 1)
        outer.addWidget(right, 1)
        return page

    # ------------------------------------------------------------- 自更新
    def _build_update_card(self) -> Card:
        card = Card()
        card.add(self._label("更新", "h2"))
        hint = self._label(
            "检查更新先测 GitHub 直连延迟；过高时清理 DNS 缓存并重测，"
            "仍然偏高就改用镜像下载，支持断点续传。"
        )
        hint.setWordWrap(True)
        card.add(hint)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.update_status = self._label(f"当前版本 v{__version__}")
        self.update_status.setWordWrap(True)
        row.addWidget(self.update_status, 1)
        self.update_button = QPushButton("检查更新")
        self.update_button.setIcon(QIcon(icon_pixmap("refresh", 14, "#5B6678")))
        self.update_button.setIconSize(QSize(14, 14))
        self.update_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_button.clicked.connect(self.check_for_update)
        row.addWidget(self.update_button)
        card.add_layout(row)
        return card

    def check_for_update(self) -> None:
        """后台检查更新：探测直连 → 清 DNS 重测 → 必要时走镜像。"""
        if getattr(self, "_update_running", False):
            return
        self._update_running = True
        self.update_button.setEnabled(False)
        self.update_status.setText("正在检查更新…")
        base = (self.config.download_base or "").strip()
        token = self.config.download_token or ""
        proxy = self.config.proxy or None

        def report(text: str) -> None:
            self.events.put(("call", lambda t=text: self.update_status.setText(t)))

        def progress(tracker) -> None:
            snap = tracker.snapshot()
            percent = snap.get("percent")
            if percent is None:
                report(f"正在下载安装包… {snap['done_bytes'] / 1048576:.1f} MB")
            else:
                report(f"正在下载安装包… {percent:.0f}%（{snap['speed'] / 1048576:.1f} MB/s）")

        def worker() -> None:
            try:
                release = update_client.fetch_release(base, token=token, proxy=proxy)
                if not update_client.is_newer(release.version, __version__):
                    report(f"已是最新版本 v{__version__}（最新 v{release.version or '未知'}）")
                    return
                report(f"发现新版本 v{release.version}，正在选择下载源…")
                plan = update_client.plan_download(release, proxy=proxy)
                report(f"{plan.detail()}，开始下载…")
                dest = update_client.download_installer(
                    plan,
                    config_dir(self.data_dir) / "updates",
                    proxy=proxy,
                    token=token,
                    mirror_origin=base,
                    progress=progress,
                )
                report(f"v{release.version} 已下载到 {dest}，双击安装即可")
            except Exception as exc:  # noqa: BLE001 - 更新失败不应影响主流程
                report(f"更新失败：{exc}")
            finally:
                self.events.put(("call", self._finish_update_check))

        threading.Thread(target=worker, name="update-check", daemon=True).start()

    def _finish_update_check(self) -> None:
        self._update_running = False
        if hasattr(self, "update_button"):
            self.update_button.setEnabled(True)

    def _trigger_easter_egg(self) -> None:
        """彩蛋：进入「点哪掉哪」模式，页面保持原样。重复点击不会叠加。"""
        if getattr(self, "_falling_overlay", None) is not None:
            return
        self._falling_overlay = FallingOverlay(self)

    # ------------------------------------------------------------- 连通性自检（官方插件）
    def _build_diagnostics_card(self) -> Card:
        card = Card()
        card.add(self._label("连通性自检", "h2"))
        hint = self._label("检查下载源与热度榜接口是否连通（官方 diagnostics 插件，只读探测）。")
        hint.setWordWrap(True)
        card.add(hint)
        self.diag_status = self._label("尚未运行", "muted")
        card.add(self.diag_status)
        self.diag_body = QVBoxLayout()
        self.diag_body.setSpacing(4)
        card.add_layout(self.diag_body)
        run = QPushButton("运行自检")
        run.setObjectName("ghost")
        run.clicked.connect(self._run_diagnostics)
        card.add(run)
        return card

    def _run_diagnostics(self) -> None:
        record = self.plugin_registry.get("diagnostics")
        if record is None or record.instance is None:
            self.notice("自检插件未加载", "WARN")
            return
        self.diag_status.setText("检测中……")

        def worker(_cancel: threading.Event) -> str:
            rows = record.instance.report()
            self.post(lambda r=rows: self._render_diag_rows(r))
            return "自检完成"

        self.run_task("连通性自检", worker)

    def _render_diag_rows(self, rows: list[dict]) -> None:
        while self.diag_body.count():
            item = self.diag_body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for row in rows:
            line = QHBoxLayout()
            line.setSpacing(8)
            ok = bool(row.get("ok"))
            dot = QLabel("●" if ok else "✕")
            dot.setStyleSheet(f"color: {OK if ok else DANGER};")
            line.addWidget(dot)
            name = QLabel(str(row.get("label", "")))
            name.setMinimumWidth(88)
            line.addWidget(name)
            detail = self._label(str(row.get("detail", "")), "mutedSmall")
            detail.setWordWrap(True)
            line.addWidget(detail, 1)
            self.diag_body.addLayout(line)
        self.diag_status.setText("完成")

    def _build_overlays(self) -> None:
        """版本号（右下）与任务面板（浮层），都不占内容区高度。"""
        central = self.centralWidget()
        if central is None:
            return

        self.version_label = QLabel(f"v{__version__}", central)
        self.version_label.setObjectName("versionTag")
        self.version_label.setToolTip(f"{APP_NAME} v{__version__}")
        self.version_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.task_panel = self._build_task_panel(central)
        self.task_panel.setVisible(False)

    def _place_overlays(self) -> None:
        central = self.centralWidget()
        if central is None:
            return
        width, height = central.width(), central.height()
        bottom = max(0, height - u(0.4))
        if hasattr(self, "version_label"):
            self.version_label.adjustSize()
            self.version_label.move(
                max(0, width - self.version_label.width() - u(1.25)),
                max(0, bottom - self.version_label.height()),
            )
            self.version_label.raise_()

        if hasattr(self, "task_panel") and self.task_panel.isVisible():
            hint = self.task_panel.sizeHint()
            panel_w = min(max(hint.width(), u(30)), max(u(20), width - u(4)))
            panel_h = max(hint.height(), u(5))
            stack_bottom = bottom - self.version_label.height() - u(0.75)
            self.task_panel.setGeometry(
                max(0, width - panel_w - u(1.25)),
                max(0, stack_bottom - panel_h),
                panel_w,
                panel_h,
            )
            self.task_panel.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._place_overlays()

    def _build_task_panel(self, parent: QWidget | None = None) -> Card:
        panel = Card(parent, margins=(u(1.25), u(0.75), u(1.25), u(0.75)))
        row = QHBoxLayout()
        row.setSpacing(u(0.5))
        self.task_title = QLabel("")
        self.task_title.setObjectName("h3")
        row.addWidget(self.task_title)
        self.task_phase = self._label("")
        row.addWidget(self.task_phase, 1)
        self.task_percent = QLabel("0%")
        self.task_percent.setStyleSheet("font-weight: 700;")
        row.addWidget(self.task_percent)
        self.task_cancel = QPushButton("取消")
        self.task_cancel.clicked.connect(self.cancel_task)
        row.addWidget(self.task_cancel)
        panel.add_layout(row)
        self.task_bar = QProgressBar()
        self.task_bar.setRange(0, 100)
        self.task_bar.setValue(0)
        panel.add(self.task_bar)
        self.task_detail = self._label("")
        panel.add(self.task_detail)
        panel.setVisible(False)
        return panel

    # ------------------------------------------------------------- 导航
    def show_page(self, name: str) -> None:
        page = self._ensure_page(name)
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        for key, button in self.nav_buttons.items():
            button.setChecked(key == name)
        self._sync_content_timer(name)
        if name == "home":
            self.refresh_instances()
        elif name == "online":
            self.online_page.ensure_loaded()
        elif name == "download":
            self.switch_tab(self._current_tab)
            self._refresh_source_badge()
        elif name == "settings":
            self._load_settings_values()
        elif name == "plugins":
            self._refresh_plugins_page()

    def goto_content(self, kind: str) -> None:
        self.show_page("download")
        self.switch_tab(kind)

    def goto_tab(self, key: str) -> None:
        self.show_page("download")
        self.switch_tab(key)

    def switch_tab(self, key: str) -> None:
        order = [name for name, _text in DOWNLOAD_TABS]
        if key not in order:
            key = "game"
        self._current_tab = key
        self.dl_stack.setCurrentIndex(order.index(key))
        for name, button in self.tab_buttons.items():
            button.setChecked(name == key)
        if key == "game":
            self._render_versions()
            return
        if key == "java":
            self._refresh_java_tab()
            return
        page = self.content_pages.get(key)
        if page is None:
            return
        # 一进页就直接拉列表 + 首个项目的版本（首次进入全量拉，之后只补缺失的）
        page.ensure_loaded()

    def _refresh_source_badge(self) -> None:
        """下载源不在界面上展示（仅 F12 窗口可见），这里只负责保持徽标隐藏。"""
        if hasattr(self, "source_badge"):
            self.source_badge.setVisible(False)

    # ------------------------------------------------------------- 实例
    def game_dir(self) -> Path:
        return Path(self.config.game_dir) if self.config.game_dir else default_game_dir()

    def game_dir_for(self, instance: Instance | None) -> Path:
        mc_dir = self.game_dir()
        if instance is None:
            return mc_dir
        isolated = bool(self.config.isolation) or read_pack_metadata(mc_dir, instance.id) is not None
        return instance_game_dir(mc_dir, instance.id, isolated)

    def selected_instance(self) -> Instance | None:
        return self.selected

    def refresh_instances(self) -> None:
        def worker() -> None:
            try:
                found = discover_instances(self.game_dir())
            except Exception as exc:  # noqa: BLE001
                self.post_log("ERROR", f"读取实例失败：{exc}")
                return
            self.events.put(("instances", found))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_instances(self, instances: list[Instance]) -> None:
        previous = self.selected.id if self.selected is not None else (self.config.last_version or "")
        self.instances = list(instances)
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        for item in self.instances:
            loader = item.loader or "原版"
            label = item.id if loader in ("原版", "未知") else f"{item.id} · {loader}"
            self.version_combo.addItem(QIcon(block_pixmap(item.id, 22, item.loader)), label, item)
            index = self.version_combo.count() - 1
            self.version_combo.setItemData(
                index, item.broken or f"{item.id} · {loader}", Qt.ItemDataRole.ToolTipRole
            )

        target = -1
        for index, item in enumerate(self.instances):
            if item.id == previous:
                target = index
                break
        if target < 0 and self.instances:
            target = 0
        if target >= 0:
            self.version_combo.setCurrentIndex(target)
        self.version_combo.blockSignals(False)

        self.selected = self.version_combo.currentData() if target >= 0 else None
        self._update_hero()

        total = sum(item.size_bytes for item in self.instances)
        if self.instances:
            self.status(f"{len(self.instances)} 个实例 · {human_size(total)}")
        self._render_versions()

    def _on_version_selected(self, index: int) -> None:
        instance = self.version_combo.itemData(index) if index >= 0 else None
        self.selected = instance
        if instance is not None:
            self.config.last_version = instance.id
        self._update_hero()

    def _update_hero(self) -> None:
        """把当前实例的信息写到启动页（提示、目录、模组 / 存档列表）。"""
        instance = self.selected
        if instance is None:
            self.home_hint.setText("还没有可用的游戏版本，去「下载」页安装一个吧")
            self.path_label.setText(str(self.game_dir()))
            self.config_hint.setText("")
            self.launch_button.setEnabled(False)
            self._refresh_home_lists()
            return
        parts = [f"MC {instance.mc_version}", human_size(instance.size_bytes)]
        loader = instance.loader or "原版"
        if loader not in ("原版", "未知"):
            parts.insert(1, loader)
        pack = read_pack_metadata(self.game_dir(), instance.id)
        if pack and pack.get("name"):
            parts.insert(0, f"整合包 {pack.get('name')}")
        if instance.broken:
            parts.append("版本 JSON 损坏")
        self.home_hint.setText("  ·  ".join(parts))
        self.path_label.setText(str(self.game_dir_for(instance)))
        self.config_hint.setText(f"最近使用 {human_date(instance.mtime)}")
        self.launch_button.setEnabled(True)
        self._refresh_home_lists()

    def _on_memory_changed(self, value: int) -> None:
        self.config.memory_mb = int(value)
        self.memory_value.setText(f"{value} MB")
        if hasattr(self, "settings_memory"):
            self.settings_memory.blockSignals(True)
            self.settings_memory.setValue(int(value))
            self.settings_memory.blockSignals(False)
            self.settings_memory_value.setText(f"{value} MB")

    def _instance_menu(self, global_pos) -> None:
        """版本下拉框右侧的「⋯」菜单：版本设置 / 加载器 / 目录 / 删除。"""
        instance = self.selected
        menu = QMenu(self)
        settings = menu.addAction("版本设置")
        add_loader = menu.addAction("安装加载器…")
        menu.addSeparator()
        open_game = menu.addAction("打开游戏目录")
        open_version = menu.addAction("打开版本目录")
        menu.addSeparator()
        refresh = menu.addAction("刷新版本列表")
        remove = menu.addAction("删除实例")
        for action in (settings, add_loader, open_game, open_version, remove):
            action.setEnabled(instance is not None)
        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        if chosen is refresh:
            self.refresh_instances()
        elif chosen is remove and instance is not None:
            self._delete_instance(instance)
        elif chosen is settings:
            self._open_instance_dialog()
        elif chosen is add_loader and instance is not None:
            self._install_loader_on(instance)
        elif chosen is open_game:
            _reveal(self.game_dir_for(instance))
        elif chosen is open_version and instance is not None:
            _reveal(self.game_dir() / "versions" / instance.id)

    def _delete_instance(self, instance: Instance) -> None:
        answer = QMessageBox.question(
            self, APP_NAME, f"确定删除实例 {instance.id}？此操作不可恢复。"
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        shutil.rmtree(self.game_dir() / "versions" / instance.id, ignore_errors=True)
        self.notice(f"已删除实例 {instance.id}", "WARN")
        self.refresh_instances()

    def _apply_account_name(self) -> None:
        """名称框回车：校验后保存并刷新头像。"""
        name = self.account_name.text().strip()
        if name == self.config.username:
            self.account_name.setText(self.config.username)
            return
        try:
            validate_name(name)
        except ValueError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            self.account_name.setText(self.config.username)
            return
        self.config.username = name
        save_config(self.config, self.data_dir)
        self.avatar.setPixmap(self._avatar_pixmap(name, AVATAR_PX))
        self.notice(f"已切换离线账户：{name}")

    def _edit_account(self) -> None:
        """兼容旧入口：直接聚焦名称框改名。"""
        self.account_name.setFocus()
        self.account_name.selectAll()

    # ------------------------------------------------------------- 启动
    def _launch_or_stop(self) -> None:
        if self.process is not None and self.process.running:
            self.stop_game()
            return
        if self.selected is None:
            self.notice("请先选择一个版本", "WARN")
            return
        self.launch_game(self.selected)

    def launch_game(self, instance: Instance, quick_play: str | None = None) -> None:
        if self.process is not None and self.process.running:
            self.notice("游戏已经在运行了", "WARN")
            return
        save_config(self.config, self.data_dir)
        game_dir = self.game_dir()
        target_dir = self.game_dir_for(instance)
        options = LaunchOptions(
            memory_mb=self.config.memory_mb,
            width=self.config.width,
            height=self.config.height,
            fullscreen=self.config.fullscreen,
            java_path=self.config.java_overrides.get(instance.id)
            or self.config.java_overrides.get("__global__"),
            extra_jvm=list(self.config.extra_jvm),
            game_dir=str(target_dir),
            quick_play_server=quick_play,
        )

        def worker(cancel: threading.Event) -> str:
            t_load = time.perf_counter()
            version = load_version(game_dir, instance.id)
            profile = get_provider(self.config).acquire(self.config)
            load_ms = (time.perf_counter() - t_load) * 1000
            plan = self._build_command_with_repair(
                version, game_dir, profile, options, cancel
            )
            for warning in plan.warnings:
                self.post_log("WARN", warning)
            self.post_log("INFO", f"启动 {instance.id}（{plan.java.display}）")
            seg = plan.timings
            self.post_log(
                "PERF",
                "启动耗时(ms) "
                f"load={load_ms:.0f} "
                f"classpath={seg.get('classpath', 0):.0f} "
                f"natives={seg.get('natives', 0):.0f} "
                f"java={seg.get('java', 0):.0f} "
                f"assemble={seg.get('assemble', 0):.0f}",
            )

            def on_line(line: str) -> None:
                self.post_log("INFO", line)

            def on_exit(code: int) -> None:
                level = "INFO" if code == 0 else "ERROR"
                self.post_log(level, f"游戏进程退出，代码 {code}")
                self.events.put(("launch_done", None))

            t_spawn = time.perf_counter()
            proc = GameProcess(plan, on_line=on_line, on_exit=on_exit)
            self.process = proc
            proc.start()
            self.post_log("PERF", f"spawn={(time.perf_counter() - t_spawn) * 1000:.0f}ms")
            # 回写 Java 磁盘缓存，下次启动免重复探测（失败不影响启动）
            try:
                self.config.java_cache = export_java_cache()
                save_config(self.config, self.data_dir)
            except OSError:
                pass
            self.events.put(("launch_started", None))
            return "游戏运行中"

        self.run_task(f"启动 {instance.id}", worker)

    def _build_command_with_repair(
        self,
        version,
        game_dir: Path,
        profile,
        options: LaunchOptions,
        cancel: threading.Event,
    ):
        """构建启动命令；加载器实例缺依赖时先自动补齐再重试。

        Forge / NeoForge 安装器只负责写版本 JSON 与打补丁，依赖得由启动器下；
        历史版本（或安装器被网络卡住时）会少一堆库，这里用版本 JSON 自愈，
        不再让用户看到「缺少 N 个依赖文件」。
        """
        try:
            return build_command(version, game_dir, profile, options=options)
        except LaunchError as exc:
            if "缺少" not in str(exc) or not version.raw.get("inheritsFrom"):
                raise
        from offcraft.install.installer import install_local

        downloader = self.make_downloader()
        mirror = self.source(downloader)
        self.post_log("WARN", f"{version.id} 缺少依赖文件，正在自动补齐……")
        self.set_phase(f"补齐 {version.id} 依赖")
        result = install_local(
            game_dir, version.id, mirror, downloader, cancel=cancel
        )
        if not result.ok:
            first = result.report.failed[0] if result.report.failed else (None, "未知错误")
            raise RuntimeError(f"依赖补齐失败：{first[1]}")
        self.post_log(
            "INFO",
            f"已补齐 {len(result.report.succeeded)} 个依赖文件，重新启动",
        )
        repaired = load_version(game_dir, version.id)
        return build_command(repaired, game_dir, profile, options=options)

    def stop_game(self) -> None:
        if self.process is not None:
            self.post_log("WARN", "正在结束游戏……")
            self.process.stop()

    # ------------------------------------------------------------- 版本下载
    def _on_segment(self, key: str) -> None:
        self._segment = key
        self._render_versions()

    def _render_versions(self) -> None:
        if not hasattr(self, "game_tree"):
            return
        self.game_tree.clear()
        self._version_rows = []

        if self._segment == "installed":
            self.game_spinner.stop()
            self.game_tree.setHeaderLabels(["版本", "加载器", "最近使用", "大小"])
            for item in self.instances:
                node = QTreeWidgetItem(
                    [item.id, item.loader, human_date(item.mtime), human_size(item.size_bytes)]
                )
                node.setData(0, Qt.ItemDataRole.UserRole, ("instance", item))
                node.setIcon(0, QIcon(block_pixmap(item.id, 26, item.loader)))
                self.game_tree.addTopLevelItem(node)
            self.game_count.setText(f"{len(self.instances)} 个实例")
            self.game_install_button.setText("启动所选版本")
            self.game_install_button.setEnabled(bool(self.instances))
            return

        self.game_install_button.setText("安装所选版本")
        self.game_tree.setHeaderLabels(["版本", "类型", "发布时间", "状态"])
        manifest = self.manifest
        if manifest is None:
            self.game_count.setText("版本清单加载中……")
            self.game_spinner.start()
            self.game_install_button.setEnabled(False)
            return
        self.game_spinner.stop()
        kinds = SEGMENT_KINDS.get(self._segment, {"release"})
        items = manifest.filter(kinds, self.game_search.text())
        installed = {item.id for item in self.instances}
        for summary in items[:400]:
            node = QTreeWidgetItem(
                [
                    summary.id,
                    summary.label,
                    summary.release_time[:10],
                    "已安装" if summary.id in installed else "可安装",
                ]
            )
            node.setData(0, Qt.ItemDataRole.UserRole, ("summary", summary))
            node.setIcon(0, QIcon(block_pixmap(summary.id, 26)))
            self.game_tree.addTopLevelItem(node)
            self._version_rows.append(summary)
        self.game_count.setText(f"{len(items)} 个版本，显示前 {len(self._version_rows)} 个")
        self.game_install_button.setEnabled(bool(self._version_rows))
        if self._version_rows:
            self.game_tree.setCurrentItem(self.game_tree.topLevelItem(0))

    def _on_game_action(self) -> None:
        node = self.game_tree.currentItem()
        if node is None:
            self.notice("请先选择一个版本", "WARN")
            return
        payload = node.data(0, Qt.ItemDataRole.UserRole)
        if not payload:
            return
        kind, value = payload
        if kind == "instance":
            self.launch_game(value)
        else:
            self.install_game(value)

    def _install_latest(self) -> None:
        if self.manifest is None or not self.manifest.latest_release:
            self.notice("版本清单尚未就绪", "WARN")
            return
        summary = self.manifest.find(self.manifest.latest_release)
        if summary is not None:
            self.install_game(summary)

    def install_game(self, summary: VersionSummary) -> None:
        """点「安装」→ 进入安装页（不再是弹窗）。"""
        if self.manifest is None:
            self.notice("版本清单尚未就绪，请稍后再试", "WARN")
            return
        if self.cancel_event is not None and not self.cancel_event.is_set():
            self.notice("已有任务在进行，请等待完成或取消", "WARN")
            return
        self.show_install_page(summary)

    def show_install_page(self, summary: VersionSummary) -> None:
        """把下载页右侧切到安装页，并填入版本信息与加载器行。"""
        if self.stack.currentWidget() is not self.pages["download"]:
            self.show_page("download")
        self.install_page.open_for(summary)
        self.dl_stack.setCurrentWidget(self.install_page)
        self.tab_buttons["game"].setChecked(True)

    def close_install_page(self) -> None:
        self.switch_tab("game")

    def start_install(
        self,
        summary: VersionSummary,
        loader_key: str = "vanilla",
        loader_version: str = "",
        dir_name: str = "",
    ) -> None:
        """安装页点「安装」后的后台任务：原版 / 原版 + 加载器，最后可选改名。"""
        manifest = self.manifest
        if manifest is None:
            self.notice("版本清单尚未就绪，请稍后再试", "WARN")
            return
        if self.cancel_event is not None and not self.cancel_event.is_set():
            self.notice("已有任务在进行，请等待完成或取消", "WARN")
            return

        loader_label = dict(LOADERS).get(loader_key, loader_key)
        wanted = safe_instance_id(dir_name)
        title = f"安装 {summary.id}"
        if loader_key != "vanilla":
            title = f"安装 {summary.id} + {loader_label}"

        def worker(cancel: threading.Event) -> str:
            from offcraft.install.installer import install, prepare_install
            from offcraft.install.plan import InstallOptions

            downloader = self.make_downloader()
            mirror = self.source(downloader)
            options = InstallOptions(include_assets=not self.config.skip_assets)
            plan = prepare_install(
                self.game_dir(), summary.id, manifest, mirror, downloader, options=options
            )
            self.post_log("INFO", f"{summary.id}：{plan.summary()}")
            for warning in plan.warnings:
                self.post_log("WARN", warning)
            result = install(plan, downloader, tracker=ProgressTracker(), cancel=cancel)
            if not result.ok:
                first = result.report.failed[0] if result.report.failed else (None, "未知错误")
                raise RuntimeError(f"{len(result.report.failed)} 个文件失败：{first[1]}")
            files = len(result.report.succeeded)
            version_id = summary.id
            if loader_key != "vanilla":
                self.set_phase(f"安装 {loader_label} {loader_version}")
                version_id = install_loader(
                    self.game_dir(),
                    summary.id,
                    loader_key,
                    loader_version,
                    mirror=mirror,
                    downloader=downloader,
                    manifest=manifest,
                    java=self.config.java_overrides.get("__global__"),
                    cancel=cancel,
                    phase=self.set_phase,
                )
            note = ""
            if wanted and wanted != version_id:
                version_id, note = rename_instance(self.game_dir(), version_id, wanted)
                if note:
                    self.post_log("WARN", note)
            self.config.last_version = version_id
            save_config(self.config, self.data_dir)
            suffix = f"（{note}）" if note else ""
            if loader_key != "vanilla":
                return (
                    f"{version_id} 安装完成"
                    f"（{files} 个文件 + {loader_label} {loader_version}）{suffix}"
                )
            return f"{version_id} 安装完成，共 {files} 个文件{suffix}"

        self.run_task(title, worker)
        self.switch_tab("game")

    def _install_loader_on(self, instance: Instance) -> None:
        """给已有实例补装加载器，装上后就能装模组与光影。"""
        if self.process is not None and self.process.running:
            self.notice("游戏正在运行，请先关闭游戏", "WARN")
            return
        dialog = LoaderPickDialog(
            self,
            instance.mc_version,
            allow_vanilla=False,
            title=f"给 {instance.id} 添加加载器",
        )
        if not dialog.exec():
            return
        key, version = dialog.choice()
        if key == "vanilla" or not version:
            return
        label = dict(LOADERS).get(key, key)

        def worker(cancel: threading.Event) -> str:
            downloader = self.make_downloader()
            mirror = self.source(downloader)
            manifest = self.ensure_manifest(cancel)
            self.set_phase(f"安装 {label} {version}")
            version_id = install_loader(
                self.game_dir(),
                instance.mc_version,
                key,
                version,
                mirror=mirror,
                downloader=downloader,
                manifest=manifest,
                java=self.config.java_overrides.get("__global__"),
                cancel=cancel,
                phase=self.set_phase,
            )
            self.config.last_version = version_id
            save_config(self.config, self.data_dir)
            return f"{version_id} 已就绪，可以安装模组了"

        self.run_task(f"安装 {label} {version}", worker)

    # ------------------------------------------------------------- 内容下载
    def content_client(self) -> ModrinthClient:
        key = (
            tuple(self.source_bases()),
            self.config.proxy,
            self.config.download_token,
        )
        if self._content_client is None or self._content_key != key:
            self._content_client = ModrinthClient(
                self.config.download_base or "",
                downloader=self.make_downloader(workers=4),
                proxy=self.config.proxy,
                token=self.config.download_token,
                source=self.source(),
                prefer_fastest=bool(getattr(self.config, "prefer_fastest", True)),
            )
            self._content_key = key
        return self._content_client

    def install_content(
        self,
        hit: ProjectHit,
        version: ProjectVersion,
        target: Target,
        kind: str,
    ) -> None:
        """把内容装到目标（版本实例或手选文件夹）的对应子目录。"""
        game_version = target.mc_version
        loader = (
            target.loader.lower()
            if target.loader in ("Fabric", "Forge", "NeoForge", "Quilt")
            else ""
        )
        dest = target.subdir(kind)
        label = target.label

        def worker(cancel: threading.Event) -> str:
            client = self.content_client()
            result = install_project_version(
                client,
                version,
                dest,
                game_version=game_version,
                loader=loader,
                cancel=cancel,
            )
            for warning in result.warnings:
                self.post_log("WARN", warning)
            if not result.files:
                raise RuntimeError("没有下载到任何文件")
            names = "、".join(path.name for path in result.files[:3])
            more = f" 等 {len(result.files)} 个文件" if len(result.files) > 3 else ""
            return f"已安装到 {label}/{dest.name}：{names}{more}"

        self.run_task(f"安装 {hit.title}", worker)

    def install_modpack(self, hit: ProjectHit, version: ProjectVersion, name: str) -> None:
        file = version.primary_file
        if file is None:
            self.notice("该版本没有可下载的文件", "WARN")
            return

        def worker(cancel: threading.Event) -> str:
            from offcraft.content.modpack import install_modpack as install_mrpack

            client = self.content_client()
            cache = config_dir(self.data_dir) / "cache" / "modpacks"
            cache.mkdir(parents=True, exist_ok=True)
            archive = download_file(
                client.downloader,
                client.resolve_url(file.url),
                cache / safe_filename(file.filename),
                sha1=file.sha1,
                sha512=file.sha512,
                size=file.size or None,
                cancel=cancel,
            )
            self.set_phase("校验整合包")
            target_name = unique_instance_name(self.game_dir(), name or hit.title)
            downloader = self.make_downloader()
            mirror = self.source(downloader)
            manifest = self.ensure_manifest(cancel)
            result = install_mrpack(
                self.game_dir(),
                archive,
                mirror=mirror,
                downloader=downloader,
                manifest=manifest,
                instance_name=target_name,
                isolated=True,
                allow_untrusted=bool(getattr(self.config, "allow_untrusted_sources", False)),
                cancel=cancel,
                phase=self.set_phase,
            )
            for warning in result.warnings:
                self.post_log("WARN", warning)
            self.config.last_version = result.instance_id
            save_config(self.config, self.data_dir)
            skipped = f"，跳过 {result.skipped} 个" if result.skipped else ""
            return (
                f"整合包 {result.instance_id} 安装完成"
                f"（{result.installed}/{result.total} 个文件{skipped}）"
            )

        self.run_task(f"安装整合包 {hit.title}", worker)

    # ------------------------------------------------------------- 版本清单
    def ensure_manifest(self, cancel: threading.Event | None = None, force: bool = False) -> VersionManifest:
        # force=True 时即使已有缓存清单也会重新向下载源拉取（启动时即刷新版本任务）
        if self.manifest is not None and not force:
            return self.manifest
        downloader = self.make_downloader(progress=False, workers=2)
        data = downloader.fetch_json(self.source(downloader).rewrite(MANIFEST_URL))
        manifest = VersionManifest.from_json(data)
        self.manifest = manifest
        try:
            path = config_dir(self.data_dir) / "cache" / "version_manifest.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        self.events.put(("manifest", manifest))
        return manifest

    def refresh_remote(self) -> None:
        if self._remote_loading:
            return
        self._remote_loading = True
        self.status("正在获取版本清单……")

        def worker() -> None:
            try:
                self.ensure_manifest(force=True)
            except Exception as exc:  # noqa: BLE001
                self.post_log("ERROR", f"版本清单获取失败：{exc}")
                if self.manifest is not None:
                    # 已有缓存清单时不影响使用，只提示刷新失败
                    self.events.put(("status", "版本清单刷新失败，继续使用本地缓存"))
                else:
                    self.events.put(("status", "版本清单获取失败，可在「设置」中修改下载源"))
            finally:
                self._remote_loading = False

        threading.Thread(target=worker, daemon=True).start()

    def _load_cached_manifest(self) -> bool:
        path = config_dir(self.data_dir) / "cache" / "version_manifest.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            manifest = VersionManifest.from_json(data)
        except (OSError, ValueError, KeyError, TypeError):
            return False
        self._apply_manifest(manifest)
        self._append_log("INFO", f"已加载缓存的版本清单（{len(manifest.versions)} 个版本）")
        return True

    def _apply_manifest(self, manifest: VersionManifest) -> None:
        self.manifest = manifest
        self._update_banner()
        self._render_versions()
        self.status(f"版本清单已就绪：{len(manifest.versions)} 个版本")

    def _update_banner(self) -> None:
        """把当前清单信息写进下载页顶部的最新版卡片（下载页懒构建，未建则跳过）。"""
        if not hasattr(self, "banner_title"):
            return
        manifest = self.manifest
        if manifest is not None and manifest.latest_release:
            summary = manifest.find(manifest.latest_release)
            self.banner_title.setText(f"最新正式版 {manifest.latest_release}")
            hint = f"发布于 {summary.release_time[:10]}" if summary else ""
            if manifest.latest_snapshot:
                hint = f"{hint}  ·  最新快照 {manifest.latest_snapshot}".strip()
            self.banner_hint.setText(hint)
            self.banner_icon.setPixmap(block_pixmap(manifest.latest_release, 44))
            self.banner_button.setEnabled(True)

    # ------------------------------------------------------------- 网络工具
    def make_downloader(self, *, progress: bool = True, workers: int | None = None) -> Downloader:
        return Downloader(
            proxy=self.config.proxy or None,
            token=self.config.download_token or None,
            # 令牌只发给配置的主镜像源，官方源/第三方 CDN 一律不带
            token_origin=self.config.download_base or None,
            workers=workers or self.config.max_workers,
            timeout=30,
            progress_callback=(
                (lambda tracker: self.events.put(("progress", tracker.snapshot())))
                if progress
                else None
            ),
            progress_interval=0.25,
            # 下载过慢时重新探测各源，并从断点切换到更快的源
            url_resolver=self._resolve_source_url,
        )

    def source_bases(self) -> list[str]:
        """当前配置的下载源列表（空串表示官方直连，末尾始终保留直连兜底）。"""
        raw = getattr(self.config, "download_sources", None) or [
            self.config.download_base
        ]
        bases: list[str] = []
        for item in raw:
            text = str(item or "").strip().rstrip("/")
            if text not in bases:
                bases.append(text)
        if not bases:
            bases = [""]
        if "" not in bases:
            bases.append("")
        return bases

    def _resolve_source_url(self, url: str) -> str | None:
        """下载器报告速率过低时：重新探测所有源并返回更快的地址。"""
        try:
            source = self.source()
        except Exception:  # noqa: BLE001 - 重测失败不应中断下载
            return None
        resolver = getattr(source, "resolve_again", None)
        if not callable(resolver):
            return None
        try:
            new_url = resolver(url)
        except Exception as exc:  # noqa: BLE001 - 重测失败不影响断点续传
            self.post_log("WARN", f"重新探测下载源失败：{exc}")
            return None
        if new_url and new_url != url:
            self.post_log("INFO", f"下载过慢，已切换下载源：{new_url}")
            return new_url
        return None

    def source(self, downloader: Downloader | None = None):
        """当前下载源：在多源与官方直连之间按「延迟 + 速率」择优。

        返回对象与 :class:`offcraft.mirror.Mirror` 接口一致（``rewrite`` 等），
        可直接传给安装计划、加载器安装等函数。探测结论按 10 分钟缓存，
        因此同一个窗口内不会反复测时。
        """
        enabled = bool(getattr(self.config, "prefer_fastest", True))
        bases = self.source_bases()
        key = (tuple(bases), self.config.proxy or "", self.config.download_token or "", enabled)
        cached = self._sources.get(key)
        if cached is None:
            cached = make_source(
                sources=bases,
                downloader=downloader,
                proxy=self.config.proxy,
                token=self.config.download_token,
                enabled=enabled,
            )
            self._sources[key] = cached
        return cached

    def source_summary(self) -> str:
        """形如「镜像 libs 18ms / 直连 240ms → 镜像」的说明，供界面展示。"""
        rows: list[str] = []
        for source in self._sources.values():
            summary = getattr(source, "summary", None)
            if callable(summary):
                text = summary()
                if text and text != "尚未探测":
                    rows.append(text)
        return "；".join(rows)

    def request_icon(
        self, url: str, callback: Callable[[QPixmap], None], size: int = 46
    ) -> None:
        if not url:
            return
        cached = self._icons.get(url)
        if cached is not None:
            callback(self._scale_icon(cached, size))
            return
        if url in self._icon_loading:
            self._icon_waiters.setdefault(url, []).append((callback, size))
            return
        self._icon_loading.add(url)
        self._icon_waiters[url] = [(callback, size)]

        def worker() -> None:
            data = b""
            try:
                data = self.make_downloader(progress=False, workers=1).fetch_bytes(
                    url, max_size=4 * 1024 * 1024
                )
            except Exception:  # noqa: BLE001 - 图标失败不影响主流程
                data = b""
            self.events.put(("icon", (url, data)))

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _scale_icon(pixmap: QPixmap, size: int) -> QPixmap:
        if pixmap.width() <= size and pixmap.height() <= size:
            return pixmap
        return pixmap.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _apply_icon(self, payload: tuple[str, bytes]) -> None:
        url, data = payload
        self._icon_loading.discard(url)
        waiters = self._icon_waiters.pop(url, [])
        pixmap = QPixmap()
        if not data or not pixmap.loadFromData(data):
            return
        self._icons[url] = pixmap
        for callback, size in waiters:
            try:
                callback(self._scale_icon(pixmap, size))
            except RuntimeError:
                continue

    # ------------------------------------------------------------- 任务
    def run_task(
        self,
        title: str,
        worker: Callable[[threading.Event], str | None],
        on_done: Callable[[], None] | None = None,
    ) -> None:
        if self.cancel_event is not None and not self.cancel_event.is_set():
            self.notice("已有任务在进行，请等待完成或取消", "WARN")
            return
        cancel = threading.Event()
        self.cancel_event = cancel
        self.task_panel.setVisible(True)
        self._place_overlays()
        self.task_title.setText(title)
        self.task_phase.setText("准备中……")
        self.task_detail.setText("")
        self.task_percent.setText("0%")
        self.task_bar.setRange(0, 0)
        self.task_cancel.setEnabled(True)
        self.status(title)
        self._append_log("INFO", title)

        def runner() -> None:
            message = ""
            error: str | None = None
            try:
                message = worker(cancel) or ""
            except Exception as exc:  # noqa: BLE001 - 统一回传到界面
                error = str(exc) or exc.__class__.__name__
            self.events.put(("task_done", (message, error, on_done)))

        threading.Thread(target=runner, daemon=True).start()

    def cancel_task(self) -> None:
        if self.cancel_event is not None and not self.cancel_event.is_set():
            self.cancel_event.set()
            self.task_cancel.setEnabled(False)
            self.task_phase.setText("正在取消……")
            self.post_log("WARN", "正在取消当前任务……")

    def _finish_task(self, message: str, error: str | None) -> None:
        self.task_panel.setVisible(False)
        self.task_bar.setRange(0, 100)
        self.task_bar.setValue(0)
        self.task_percent.setText("0%")
        self.task_detail.setText("")
        self.task_cancel.setEnabled(True)
        self.cancel_event = None
        if error:
            self._append_log("ERROR", error)
            self.status_dot.setStyleSheet(f"color: {DANGER}; font-size: 11pt;")
            self.status_label.setText(error if len(error) <= 140 else error[:137] + "…")
        else:
            if message:
                self._append_log("INFO", message)
            self.status_dot.setStyleSheet(f"color: {OK}; font-size: 11pt;")
            self.status_label.setText(message or "完成")
        self.refresh_instances()

    def _show_progress(self, snapshot: dict[str, Any]) -> None:
        percent = snapshot.get("percent")
        if percent is None:
            self.task_bar.setRange(0, 0)
            self.task_percent.setText(
                human_size(snapshot.get("done_bytes") or 0)
            )
        else:
            self.task_bar.setRange(0, 100)
            self.task_bar.setValue(int(percent))
            self.task_percent.setText(f"{percent:.0f}%")
        speed = snapshot.get("speed") or 0
        current = snapshot.get("current") or "下载中"
        self.task_detail.setText(
            f"{current}  ·  {human_size(snapshot.get('done_bytes') or 0)}"
            f" / {human_size(snapshot.get('total_bytes') or 0)}"
            f"  ·  {human_size(speed)}/s"
        )

    # ------------------------------------------------------------- 事件
    def post(self, callback: Callable[[], None]) -> None:
        self.events.put(("call", callback))

    def log(self, text: str, level: str = "INFO") -> None:
        self.post_log(level, text)

    def post_log(self, level: str, text: str) -> None:
        self.events.put(("log", (level, text)))

    def status(self, text: str) -> None:
        self.events.put(("status", text))

    def notice(self, text: str, level: str = "INFO") -> None:
        self.post_log(level, text)
        self.status(text if len(text) <= 140 else text[:137] + "…")

    def set_phase(self, text: str) -> None:
        self.events.put(("task_phase", text))

    def _append_log(self, level: str, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_records.append((stamp, level, text))

    def _drain(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                return
            try:
                if kind == "call":
                    payload()
                elif kind == "log":
                    level, text = payload
                    self._append_log(level, text)
                elif kind == "status":
                    self.status_label.setText(str(payload))
                elif kind == "task_phase":
                    self.task_phase.setText(str(payload))
                elif kind == "progress":
                    self._show_progress(payload)
                elif kind == "instances":
                    self._apply_instances(payload)
                elif kind == "manifest":
                    self._apply_manifest(payload)
                elif kind == "icon":
                    self._apply_icon(payload)
                elif kind == "launch_started":
                    self.launch_button.setText("结束游戏")
                    self.launch_button.setIcon(QIcon(icon_pixmap("stop", 20, "#FFFFFF")))
                elif kind == "launch_done":
                    self.process = None
                    self.launch_button.setText("启动游戏")
                    self.launch_button.setIcon(QIcon(icon_pixmap("play", 20, "#FFFFFF")))
                    self.status_dot.setStyleSheet(f"color: {OK}; font-size: 11pt;")
                    self.status_label.setText("游戏已退出")
                elif kind == "task_done":
                    message, error, callback = payload
                    self._finish_task(message, error)
                    if callback is not None and error is None:
                        callback()
            except Exception as exc:  # noqa: BLE001 - 单条事件失败不应拖死界面
                self._append_log("ERROR", f"界面事件处理失败：{exc}")

    # ------------------------------------------------------------- 目录 / 日志
    def _open_game_dir(self) -> None:
        _reveal(self.game_dir())

    def _open_version_dir(self) -> None:
        if self.selected is None:
            return
        _reveal(self.game_dir() / "versions" / self.selected.id)

    def _open_data_dir(self) -> None:
        _reveal(config_dir(self.data_dir))

    def _open_instance_dialog(self) -> None:
        if self.selected is None:
            self.notice("请先选择一个版本", "WARN")
            return
        dialog = InstanceDialog(self, self.selected)
        dialog.exec()

    def open_source_dialog(self) -> None:
        dialog = SourceDialog(self, self.config)
        if not dialog.exec():
            return
        dialog.apply()
        save_config(self.config, self.data_dir)
        self._refresh_source_badge()
        self._load_settings_values()
        self.manifest = None
        self._content_client = None
        self._sources.clear()
        self.post_log("INFO", "下载源已更新")
        self.refresh_remote()

    # ------------------------------------------------------------- 设置
    def _load_settings_values(self) -> None:
        if not hasattr(self, "settings_game_dir"):
            return
        self.settings_game_dir.setText(self.config.game_dir or str(self.game_dir()))
        self.settings_isolate.setChecked(bool(self.config.isolation))
        self.settings_skip_assets.setChecked(bool(self.config.skip_assets))
        self.settings_memory.blockSignals(True)
        self.settings_memory.setValue(int(self.config.memory_mb))
        self.settings_memory.blockSignals(False)
        self.settings_memory_value.setText(f"{self.config.memory_mb} MB")
        self.settings_jvm.setText(" ".join(self.config.extra_jvm))
        if hasattr(self, "home_jvm"):
            self.home_jvm.setText(" ".join(self.config.extra_jvm))
        self.settings_base.setText(self.config.download_base)
        self.settings_token.setText(self.config.download_token)
        self.settings_proxy.setText(self.config.proxy)
        self.settings_workers.setValue(int(self.config.max_workers))
        if hasattr(self, "settings_fastest"):
            self.settings_fastest.setChecked(bool(getattr(self.config, "prefer_fastest", True)))
        if hasattr(self, "settings_skin_layout"):
            index = self.settings_skin_layout.findData(getattr(self.config, "skin_layout", "v1"))
            self.settings_skin_layout.setCurrentIndex(index if index >= 0 else 0)
        self._sync_appearance_controls()
        self._refresh_java_combo()

    # ------------------------------------------------------------- 背景外观
    def _sync_appearance_controls(self) -> None:
        """同步设置页里的背景图文件名与颜色样本。"""
        if hasattr(self, "settings_bg_name"):
            image = getattr(self.config, "bg_image", "")
            self.settings_bg_name.setText(
                Path(image).name if image else "未选择图片（支持 PNG / GIF）"
            )
            self.settings_bg_name.setToolTip(image or "")
        if hasattr(self, "settings_bg_color_text"):
            color = getattr(self.config, "bg_color", "")
            self.settings_bg_color_text.setText(
                f"颜色 {color}" if color else "未设置颜色（使用默认渐变）"
            )
        if hasattr(self, "settings_bg_swatch"):
            color = getattr(self.config, "bg_color", "")
            fill = color or "rgba(255, 255, 255, 0.9)"
            self.settings_bg_swatch.setStyleSheet(
                "QPushButton {"
                f" background: {fill};"
                " border: 1px solid #C7D0DE; border-radius: 9px;"
                "}"
            )

    def _choose_background_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择背景图片",
            self.config.bg_image or "",
            "图片 (*.png *.gif *.jpg *.jpeg *.bmp *.webp);;所有文件 (*.*)",
        )
        if not path:
            return
        self.config.bg_image = path
        self.apply_background()
        self._sync_appearance_controls()
        save_config(self.config, self.data_dir)

    def _clear_background_image(self) -> None:
        if not self.config.bg_image:
            return
        self.config.bg_image = ""
        self.apply_background()
        self._sync_appearance_controls()
        save_config(self.config, self.data_dir)

    def _choose_background_color(self) -> None:
        start = QColor(self.config.bg_color) if self.config.bg_color else QColor("#EAF1FF")
        color = QColorDialog.getColor(start, self, "选择背景颜色")
        if not color.isValid():
            return
        self.config.bg_color = color.name().upper()
        self.apply_background()
        self._sync_appearance_controls()
        save_config(self.config, self.data_dir)

    def _clear_background_color(self) -> None:
        if not self.config.bg_color:
            return
        self.config.bg_color = ""
        self.apply_background()
        self._sync_appearance_controls()
        save_config(self.config, self.data_dir)

    def save_settings(self) -> None:
        self.config.game_dir = self.settings_game_dir.text().strip()
        self.config.isolation = self.settings_isolate.isChecked()
        self.config.skip_assets = self.settings_skip_assets.isChecked()
        self.config.memory_mb = int(self.settings_memory.value())
        self.config.extra_jvm = [part for part in self.settings_jvm.text().split(" ") if part]
        if hasattr(self, "settings_fastest"):
            self.config.prefer_fastest = self.settings_fastest.isChecked()
        if hasattr(self, "settings_skin_layout"):
            self.config.skin_layout = self.settings_skin_layout.currentData() or "v1"
        save_config(self.config, self.data_dir)

        self.home_memory.blockSignals(True)
        self.home_memory.setValue(int(self.config.memory_mb))
        self.home_memory.blockSignals(False)
        self.memory_value.setText(f"{self.config.memory_mb} MB")
        self.home_jvm.setText(" ".join(self.config.extra_jvm))
        self._refresh_java_combo()

        self.manifest = None
        self._content_client = None
        self._sources.clear()
        self._refresh_source_badge()
        self._load_cached_manifest()
        self.refresh_remote()
        self.refresh_instances()
        self.notice("设置已保存")

    def _browse_game_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择 .minecraft 目录", str(self.game_dir()))
        if folder:
            self.settings_game_dir.setText(folder)

    def _refresh_java_combo(self) -> None:
        if not hasattr(self, "settings_java") and not hasattr(self, "home_java"):
            return
        candidates = list_java(self.game_dir())
        override = self.config.java_overrides.get("__global__", "")
        for combo in (getattr(self, "home_java", None), getattr(self, "settings_java", None)):
            if combo is None:
                continue
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("自动选择（按版本要求）", "")
            for candidate in candidates:
                combo.addItem(
                    f"Java {candidate.major} · {candidate.source}", str(candidate.path)
                )
            index = combo.findData(override)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)
        if not candidates and hasattr(self, "settings_java"):
            self.settings_java.setToolTip("未检测到 Java，可点「添加 Java」手动指定")

    def _on_java_changed(self, index: int) -> None:
        if index < 0:
            return
        self._set_java_override(self.settings_java.itemData(index) or "")

    def _on_home_java_changed(self, index: int) -> None:
        if index < 0:
            return
        self._set_java_override(self.home_java.itemData(index) or "")

    def _set_java_override(self, path: str) -> None:
        """两个 Java 下拉框（启动页 / 设置页）共用一份全局选择。"""
        if path:
            self.config.java_overrides["__global__"] = str(path)
        else:
            self.config.java_overrides.pop("__global__", None)
        save_config(self.config, self.data_dir)
        for combo in (getattr(self, "home_java", None), getattr(self, "settings_java", None)):
            if combo is None:
                continue
            index = combo.findData(path)
            if index >= 0 and combo.currentIndex() != index:
                combo.blockSignals(True)
                combo.setCurrentIndex(index)
                combo.blockSignals(False)

    def _choose_java(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "选择 java.exe", "", "Java (java.exe);;所有文件 (*.*)"
        )
        if not path:
            return
        major = probe_java(path)
        if major is None:
            QMessageBox.warning(self, APP_NAME, "无法识别该 Java，请确认是有效的 java.exe")
            return
        self.config.java_overrides["__global__"] = path
        save_config(self.config, self.data_dir)
        self._refresh_java_combo()
        index = self.settings_java.findData(path)
        if index >= 0:
            self.settings_java.setCurrentIndex(index)
        self.notice(f"已添加 Java {major}：{path}")

    # ------------------------------------------------------------- 收尾
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self.config.window = {"w": self.width(), "h": self.height()}
        save_config(self.config, self.data_dir)
        # 关闭按钮默认收进系统托盘；从托盘菜单「退出」时才真正结束进程
        if (
            not self._force_quit
            and self.tray is not None
            and self.tray.isVisible()
        ):
            event.ignore()
            self.hide()
            if not self._tray_notice_shown:
                self._tray_notice_shown = True
                self.tray.showMessage(
                    APP_NAME,
                    "已最小化到系统托盘，右键托盘图标可显示或退出。",
                    QSystemTrayIcon.MessageIcon.Information,
                    2000,
                )
            return
        if self.process is not None and self.process.running:
            answer = QMessageBox.question(self, APP_NAME, "游戏仍在运行，确定退出启动器？")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.process.stop()
        self._timer.stop()
        self._content_timer.stop()
        if hasattr(self, "backdrop"):
            self.backdrop.shutdown()
        if self.tray is not None:
            self.tray.hide()
        panel = getattr(self, "_offline_panel", None)
        if panel is not None:
            try:
                panel.shutdown()
            except Exception:  # noqa: BLE001 - 清理尽力而为，不阻断退出
                pass
        # 经插件注册表遍历 teardown（离线联机会话由其插件实例负责关停）
        try:
            self.plugin_registry.unload_all()
        except Exception:  # noqa: BLE001
            pass
        event.accept()


class AddServerDialog(QDialog):
    """添加服务器：名称（可选）/ 地址 / 端口。实际校验交给 ServerStore。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("添加服务器")
        self.setMinimumWidth(u(24))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        title = QLabel("添加服务器")
        title.setObjectName("h2")
        layout.addWidget(title)
        hint = QLabel("例：mc.example.com 或 1.2.3.4:25565（默认端口 25565）")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("备注名（可选）")
        layout.addWidget(self._labeled("名称", self.name_edit))

        row = QHBoxLayout()
        row.setSpacing(8)
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("服务器地址")
        self.port_edit = QLineEdit("25565")
        self.port_edit.setFixedWidth(u(5))
        self.port_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.host_edit, 1)
        row.addWidget(QLabel(":"))
        row.addWidget(self.port_edit)
        addr_wrap = QWidget()
        addr_wrap.setLayout(row)
        layout.addWidget(self._labeled("地址 / 端口", addr_wrap))

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("ghost")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("添加")
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        layout.addLayout(buttons)

    @staticmethod
    def _labeled(text: str, widget: QWidget) -> QWidget:
        wrap = QWidget()
        column = QVBoxLayout(wrap)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)
        label = QLabel(text)
        label.setObjectName("mutedSmall")
        column.addWidget(label)
        column.addWidget(widget)
        return wrap

    def address(self) -> str:
        host = self.host_edit.text().strip()
        port = self.port_edit.text().strip()
        if port and port != "25565":
            inner = f"[{host}]" if ":" in host else host
            return f"{inner}:{port}"
        return host

    def server_label(self) -> str:
        return self.name_edit.text().strip()


class _PluginHost:
    """把 MainWindow 的宿主原语受限地递给官方插件（实现 PluginHost 协议）。

    插件永远拿不到本适配器，只通过 PluginContext 的白名单访问器调用这里。
    ``mirror_probe`` / ``mirror_download`` 把下载源 origin 封在内部：插件只给相对路径。
    """

    def __init__(self, window: "MainWindow") -> None:
        self.window = window

    def notice(self, text: str, level: str = "INFO") -> None:
        self.window.notice(f"[插件] {text}", level)

    def post_log(self, level: str, text: str) -> None:
        self.window.post_log(level, text)

    def register_nav_item(self, plugin_id: str, label: str, factory) -> None:
        self.window.register_plugin_nav(plugin_id, label, factory)

    def run_task(self, name: str, fn) -> None:
        self.window.run_task(name, fn)

    def list_instances(self) -> list[Any]:
        return list(self.window.instances)

    def save_plugin_config(self) -> None:
        self.window.plugin_registry.save()

    def mirror_probe(self, path: str) -> dict:
        base = self.window.config.download_base or ""
        if not base:
            raise RuntimeError("未配置下载源，无法探测")
        url = f"{base.rstrip('/')}{path}"
        data = self.window.make_downloader(progress=False).fetch_json(url)
        return data if isinstance(data, dict) else {}

    def mirror_download(self, url: str, dest: Any) -> Any:
        data = self.window.make_downloader(progress=False).fetch_bytes(url)
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target


def run_app(config: Config, data_dir: str | Path | None = None) -> None:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")
    os.environ.setdefault("QT_SCALE_FACTOR", "1")
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(app_qss())
    # 下拉框弹出层是独立的顶层窗口，只能靠全局事件过滤器去掉系统黑边
    app._combo_polisher = ComboPolisher(app)  # noqa: SLF001 - 保引用防回收
    app.installEventFilter(app._combo_polisher)
    window = MainWindow(config, data_dir)
    window.show()
    app.exec()
