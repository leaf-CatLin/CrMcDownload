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
import webbrowser
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QPoint, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPalette,
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
    QTabWidget,
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
from offcraft.account.microsoft import (
    MicrosoftAuthError,
    authenticate,
    authorize_url,
    exchange_code,
    extract_code,
    poll_device_code,
    request_device_code,
)
from offcraft.account.store import clear_session, load_session, save_session
from offcraft.account.webview_login import callback_error, spawn_login, wait_result
from offcraft.instances import (
    Instance,
    discover_instances,
    instance_game_dir,
    read_pack_metadata,
    rename_instance,
    safe_instance_id,
    scan_minecraft_dirs,
)
from offcraft.launch.auth import validate_name
from offcraft.launch.command import LaunchError, LaunchOptions, build_command
from offcraft.launch.java import export_java_cache, list_java, prime_java_cache, probe_java
from offcraft.launch.options import (
    add_resource_packs,
    enable_all_resource_packs,
    set_resource_pack_enabled,
)
from offcraft.launch.process import GameProcess
from offcraft.launch.resources import KIND_NAMES, KINDS, ResourceAudit, audit_resources
from offcraft.meta.manifest import VersionManifest, VersionSummary
from offcraft.meta.version import load_version
from offcraft.net.downloader import Downloader, DownloadTask, ProgressTracker
from offcraft.net.latency import make_source, probe_source
from offcraft.plugins.registry import PluginRegistry
from offcraft.privacy import PRIVACY_VERSION
from offcraft.skins.avatar import head_avatar
from offcraft.skins.csl import ensure_installed as ensure_skin_mod
from offcraft.skins.default import default_skin_bytes
from offcraft.transfer import (
    export_save,
    export_version,
    import_save,
    import_version,
)
from offcraft.ui.content_page import ContentPage
from offcraft.ui.chrome import TitleBar, enable_native_chrome, native_hit_test
from offcraft.ui.backdrop import Backdrop
from offcraft.ui.falling import FallingOverlay
from offcraft.ui.icons import icon_pixmap
from offcraft.ui.install_page import InstallPage
from offcraft.ui.motion import fade_in, install as install_motion, stagger
from offcraft.ui.spinner import Spinner
from offcraft.ui.style import LineStyle
from offcraft.ui.online_page import OnlinePage
from offcraft.ui.privacy import PrivacyDialog
from offcraft.ui.skin_page import LAYOUT_CHOICES, SkinPage
from offcraft.ui.theme import (
    ACCENT,
    DANGER,
    MUTED,
    OK,
    block_pixmap,
    full_qss,
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

#: 顶部导航（顺序与 PCL 标题栏一致：启动 → 下载 → 联机 → 插件 → 设置）
NAV_ITEMS = [
    ("home", "启动"),
    ("download", "下载"),
    ("online", "联机"),
    ("plugins", "插件"),
    ("settings", "设置"),
]

#: 顶部导航图标
NAV_ICONS = {
    "home": "play",
    "download": "download",
    "online": "globe",
    "plugins": "puzzle",
    "settings": "settings",
}

#: 页面标题（显示在 PCL 风格标题栏中）
PAGE_TITLES = {
    "home": "启动",
    "online": "联机",
    "download": "下载",
    "settings": "设置",
    "plugins": "插件",
    "instance": "版本设置",
    "account": "账户设置",
    "skin": "皮肤",
    "launching": "启动游戏",
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

#: PCL 风格：标题栏 48px（FormMain.xaml PanTitle）、启动页左栏 300px（PageLaunchLeft Width）
PCL_TOPBAR_H = 48
PCL_LEFT_W = 300
PCL_AVATAR_PX = 92

#: 下载中心全部页签（游戏版本 + 内容 + Java + 服务端）
DOWNLOAD_TABS = [("game", "游戏版本")] + CONTENT_TABS + [("java", "Java"), ("server", "服务端")]

#: 下载页左侧分类图标
DOWNLOAD_TAB_ICONS = {
    "game": "cube",
    "mod": "package",
    "modpack": "layers",
    "resourcepack": "image",
    "shader": "sun",
    "java": "cup",
    "server": "blocks",
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

#: 启动页资源分类（左侧导航 / 右侧内容）
HOME_RESOURCE_TABS = [
    ("mods", "模组"),
    ("saves", "存档"),
    ("resourcepacks", "资源"),
    ("shaderpacks", "光影"),
]


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


def _bat_quote(part: str) -> str:
    """把一段参数包成 .bat 里安全的形式（含空格/引号时加引号）。"""
    if part and not re.search(r'[\s"]', part):
        return part
    return '"' + part.replace('"', '""') + '"'


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
    """账户中心：离线账户改名，或登录 Microsoft 正版账号。"""

    device_prompt = pyqtSignal(str, str)
    login_note = pyqtSignal(str)
    login_done = pyqtSignal(object)
    login_failed = pyqtSignal(str)
    #: 结束忙碌状态但不算失败（用户关闭窗口 / 取消授权）
    login_reset = pyqtSignal(str)
    #: 内嵌窗口不可用，降级到系统浏览器 + 手动粘贴
    browser_fallback = pyqtSignal(str)

    def __init__(self, parent: QWidget, config: Config, data_dir: str | Path | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.data_dir = data_dir
        self.session = load_session(data_dir)
        self._alive = True
        self._cancel = threading.Event()
        self._login_proc: Any = None
        self._device_code = ""
        self.setWindowTitle("账户")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # ------------------------------------------------ 离线账户
        offline = QWidget()
        offline_layout = QVBoxLayout(offline)
        offline_layout.setContentsMargins(4, 10, 4, 4)
        offline_layout.setSpacing(8)
        hint = QLabel("离线账户无需登录，用户名会作为游戏内昵称（3-16 位字母 / 数字 / 下划线）。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        offline_layout.addWidget(hint)
        self.edit = QLineEdit(config.username)
        self.edit.setMaxLength(16)
        self.edit.selectAll()
        offline_layout.addWidget(self.edit)
        offline_layout.addStretch(1)
        self.tabs.addTab(offline, "离线账户")

        # ------------------------------------------------ 正版账户
        msa = QWidget()
        msa_layout = QVBoxLayout(msa)
        msa_layout.setContentsMargins(4, 10, 4, 4)
        msa_layout.setSpacing(8)
        self.msa_status = QLabel()
        self.msa_status.setObjectName("h2")
        self.msa_status.setWordWrap(True)
        msa_layout.addWidget(self.msa_status)
        self.msa_note = QLabel()
        self.msa_note.setObjectName("muted")
        self.msa_note.setWordWrap(True)
        msa_layout.addWidget(self.msa_note)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.device_button = QPushButton("设备码登录")
        self.device_button.setToolTip("显示一个 8 位代码，在任意浏览器里输入即可完成登录")
        self.device_button.clicked.connect(self._login_device)
        buttons.addWidget(self.device_button)
        self.browser_button = QPushButton("正版登录")
        self.browser_button.setToolTip(
            "优先在启动器内弹出登录窗口；内嵌窗口不可用时自动改用系统浏览器，并提示粘贴跳转地址"
        )
        self.browser_button.clicked.connect(self._login_browser)
        buttons.addWidget(self.browser_button)
        self.logout_button = QPushButton("退出正版登录")
        self.logout_button.setObjectName("ghost")
        self.logout_button.clicked.connect(self._logout)
        buttons.addWidget(self.logout_button)
        buttons.addStretch(1)
        msa_layout.addLayout(buttons)

        # 设备码：醒目显示代码并配「复制代码」按钮（不自动占用剪贴板）
        self.device_row = QWidget()
        device_layout = QHBoxLayout(self.device_row)
        device_layout.setContentsMargins(0, 0, 0, 0)
        device_layout.setSpacing(10)
        self.device_code_label = QLabel()
        self.device_code_label.setObjectName("deviceCode")
        self.device_code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        device_layout.addWidget(self.device_code_label)
        self.copy_code_button = QPushButton("复制代码")
        self.copy_code_button.setToolTip("把上方设备码复制到剪贴板")
        self.copy_code_button.clicked.connect(self._copy_device_code)
        device_layout.addWidget(self.copy_code_button)
        device_layout.addStretch(1)
        self.device_row.setVisible(False)
        msa_layout.addWidget(self.device_row)

        # 浏览器登录：内嵌窗口不可用时才回退到系统浏览器 + 粘贴跳转地址
        self.paste_row = QWidget()
        paste_layout = QHBoxLayout(self.paste_row)
        paste_layout.setContentsMargins(0, 0, 0, 0)
        paste_layout.setSpacing(6)
        self.paste_edit = QLineEdit()
        self.paste_edit.setPlaceholderText("粘贴浏览器地址栏里的完整地址（含 code=）")
        paste_layout.addWidget(self.paste_edit)
        self.paste_button = QPushButton("完成登录")
        self.paste_button.clicked.connect(self._finish_browser)
        paste_layout.addWidget(self.paste_button)
        self.paste_row.setVisible(False)
        msa_layout.addWidget(self.paste_row)

        msa_layout.addStretch(1)
        self.tabs.addTab(msa, "正版账户")

        # ------------------------------------------------ 底部按钮
        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        ok = QPushButton("保存")
        ok.setObjectName("accent")
        ok.clicked.connect(self._save)
        footer.addWidget(ok)
        layout.addLayout(footer)

        self.device_prompt.connect(self._on_device_prompt)
        self.login_note.connect(self.msa_note.setText)
        self.login_done.connect(self._on_login_done)
        self.login_failed.connect(self._on_login_failed)
        self.login_reset.connect(self._on_login_reset)
        self.browser_fallback.connect(self._on_browser_fallback)

        if config.account_provider == "microsoft":
            self.tabs.setCurrentIndex(1)
        self._refresh_msa()

    # ------------------------------------------------------------- 状态
    def done(self, result: int) -> None:  # noqa: D401 - Qt 回调
        self._alive = False
        self._cancel.set()
        proc = self._login_proc
        self._login_proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        super().done(result)

    def _refresh_msa(self) -> None:
        session = self.session
        busy = not self.device_button.isEnabled()
        self._hide_device_code()
        if session is None:
            self.msa_status.setText("尚未登录正版账号")
            if not busy:
                self.msa_note.setText(
                    "登录后启动游戏会自动携带正版令牌。需要能访问微软服务，"
                    "且该账户已购买 Minecraft: Java 版。"
                )
            self.device_button.setVisible(True)
            self.browser_button.setVisible(True)
            self.logout_button.setVisible(False)
        else:
            self.msa_status.setText(f"已登录：{session.name}")
            if not busy:
                self.msa_note.setText("登录信息只保存在本机，退出登录会立即删除。")
            self.device_button.setVisible(False)
            self.browser_button.setVisible(False)
            self.logout_button.setVisible(True)
            self.paste_row.setVisible(False)

    def _busy(self, busy: bool, text: str = "") -> None:
        self.device_button.setEnabled(not busy)
        self.browser_button.setEnabled(not busy)
        self.logout_button.setEnabled(not busy)
        self.paste_button.setEnabled(not busy)
        if text:
            self.msa_note.setText(text)

    # ------------------------------------------------------------- 设备码
    def _login_device(self) -> None:
        self._hide_device_code()
        self._busy(True, "正在向微软申请登录代码……")
        threading.Thread(
            target=self._device_worker, args=(self.config.proxy or "",), daemon=True
        ).start()

    def _device_worker(self, proxy: str) -> None:
        try:
            code = request_device_code(proxy=proxy)
            if not self._alive:
                return
            self.device_prompt.emit(code.verification_uri, code.user_code)
            token = poll_device_code(
                code,
                proxy=proxy,
                cancel=self._cancel,
                on_wait=lambda left: self.login_note.emit(f"等待你在浏览器中完成登录……剩余 {left} 秒"),
            )
            session = authenticate(token, proxy=proxy)
            if self._alive:
                self.login_done.emit(session)
        except Exception as exc:  # noqa: BLE001 - 统一展示给用户
            if self._alive:
                self.login_failed.emit(str(exc))

    def _on_device_prompt(self, url: str, code: str) -> None:
        self._device_code = code
        self.device_code_label.setText(code)
        self.device_row.setVisible(True)
        self.msa_note.setText(
            f"已打开 {url}，请在页面中输入下方代码完成登录，完成后会自动继续。"
        )
        webbrowser.open(url)

    def _copy_device_code(self) -> None:
        if not self._device_code:
            return
        QApplication.clipboard().setText(self._device_code)
        self.msa_note.setText(f"代码 {self._device_code} 已复制到剪贴板，粘贴到浏览器即可。")

    def _hide_device_code(self) -> None:
        self._device_code = ""
        self.device_code_label.clear()
        self.device_row.setVisible(False)

    # ------------------------------------------------------------- 浏览器
    def _login_browser(self) -> None:
        """正版登录：优先子进程内嵌 WebView2 自动截获跳转，失败时自动回退系统浏览器。"""
        self._hide_device_code()
        url = authorize_url()
        try:
            proc, path = spawn_login(url)
        except OSError as exc:
            self._open_browser_fallback(f"内嵌登录窗口启动失败：{exc}，已改用系统浏览器。")
            return
        self._login_proc = proc
        self._busy(True, "已打开登录窗口，请在窗口内完成 Microsoft 登录，完成后会自动继续。")
        threading.Thread(
            target=self._embedded_worker,
            args=(proc, path, self.config.proxy or ""),
            daemon=True,
        ).start()

    def _embedded_worker(self, proc: Any, path: Any, proxy: str) -> None:
        data = None
        try:
            data = wait_result(proc, path, cancel=self._cancel)
        finally:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
        if not self._alive:
            return
        if data is None:
            # 内嵌窗口没写出结果（启动失败 / 意外退出 / 超时）：自动回退系统浏览器
            self.browser_fallback.emit("内嵌登录窗口未能完成")
            return
        if data.get("cancelled"):
            self.login_reset.emit("已关闭登录窗口，未完成登录。")
            return
        if data.get("error"):
            self.browser_fallback.emit(str(data["error"]))
            return
        url = str(data.get("url") or "")
        denied = callback_error(url)
        if denied:
            self.login_reset.emit(denied)
            return
        try:
            code = extract_code(url)
        except MicrosoftAuthError as exc:
            self.login_reset.emit(str(exc))
            return
        self.login_note.emit("正在用授权码换取登录令牌……")
        self._browser_worker(code, proxy)

    def _open_browser_fallback(self, message: str = "") -> None:
        """内嵌窗口不可用时的自动降级：系统浏览器 + 粘贴跳转地址（同一按钮完成）。"""
        self._hide_device_code()
        url = authorize_url()
        webbrowser.open(url)
        self.paste_edit.clear()
        self.paste_row.setVisible(True)
        self.paste_edit.setFocus()
        prefix = f"{message} " if message else ""
        self.msa_note.setText(
            prefix
            + "已在浏览器打开登录页，完成登录后地址栏会跳到 login.live.com/oauth20_desktop.srf"
            "（其中含 code=），把整条地址复制粘贴到下方，再点「完成登录」。"
        )

    def _finish_browser(self) -> None:
        try:
            code = extract_code(self.paste_edit.text())
        except MicrosoftAuthError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self._busy(True, "正在用授权码换取登录令牌……")
        threading.Thread(
            target=self._browser_worker, args=(code, self.config.proxy or ""), daemon=True
        ).start()

    def _browser_worker(self, code: str, proxy: str) -> None:
        try:
            token = exchange_code(code, proxy=proxy)
            session = authenticate(token, proxy=proxy)
            if self._alive:
                self.login_done.emit(session)
        except Exception as exc:  # noqa: BLE001 - 统一展示给用户
            if self._alive:
                self.login_failed.emit(str(exc))

    def _on_login_reset(self, message: str) -> None:
        self._busy(False, message)

    def _on_browser_fallback(self, error: str) -> None:
        self._busy(False)
        self._open_browser_fallback(f"{error}，已改用系统浏览器。")

    # ------------------------------------------------------------- 结果
    def _on_login_done(self, session: object) -> None:
        try:
            save_session(session, self.data_dir)  # type: ignore[arg-type]
        except OSError as exc:
            self._on_login_failed(f"登录信息保存失败：{exc}")
            return
        self.session = session  # type: ignore[assignment]
        self.config.account_provider = "microsoft"
        save_config(self.config, self.data_dir)
        self._busy(False, f"已登录：{self.session.name}")
        self.accept()

    def _on_login_failed(self, message: str) -> None:
        self._busy(False)
        self._refresh_msa()
        QMessageBox.warning(self, "正版登录失败", message)

    def _logout(self) -> None:
        answer = QMessageBox.question(self, APP_NAME, "确定退出正版登录？本地令牌会被删除。")
        if answer != QMessageBox.StandardButton.Yes:
            return
        clear_session(self.data_dir)
        self.session = None
        self.config.account_provider = "offline"
        save_config(self.config, self.data_dir)
        self._refresh_msa()

    # ------------------------------------------------------------- 保存
    def _save(self) -> None:
        if self.tabs.currentIndex() == 0:
            try:
                name = validate_name(self.edit.text().strip())
            except ValueError as exc:
                QMessageBox.warning(self, APP_NAME, str(exc))
                return
            self.config.username = name
            self.config.account_provider = "offline"
            save_config(self.config, self.data_dir)
            self.accept()
            return
        if self.session is None:
            QMessageBox.warning(self, APP_NAME, "请先完成正版登录（设备码或浏览器登录）。")
            return
        self.config.account_provider = "microsoft"
        save_config(self.config, self.data_dir)
        self.accept()


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
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle(APP_NAME)
        self._window_icon = QIcon(block_pixmap("crmcdownload", 64))
        self.setWindowIcon(self._window_icon)
        self.setStyleSheet(full_qss(getattr(config, "ui_style", "default")))
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

    # ------------------------------------------------------------- 自定义背景
    def apply_background(self) -> None:
        """把配置里的背景色 / 背景图同步到最底层背景层。"""
        backdrop = getattr(self, "backdrop", None)
        if backdrop is None:
            return
        backdrop.set_appearance(
            getattr(self.config, "bg_color", ""),
            getattr(self.config, "bg_image", ""),
            getattr(self.config, "ui_style", "default"),
        )

    def _apply_ui_style(self) -> None:
        """切换界面风格：重设样式表，并按新风格重建标题栏与页面。"""
        self.setStyleSheet(full_qss(getattr(self.config, "ui_style", "default")))
        self.apply_background()
        # 切换动作由设置页下拉框触发：等当前信号处理完再拆掉旧页面
        QTimer.singleShot(0, self._rebuild_layout)

    def _apply_shell(self) -> None:
        """PCL 风格：窗口外留 18px 阴影区，内容收进 6px 圆角卡片（最大化时取消）。"""
        if not hasattr(self, "_root_layout") or not hasattr(self, "backdrop"):
            return
        shell = self._pcl() and not self.isMaximized() and not self.isFullScreen()
        inset = 18 if shell else 0
        self._shell_inset = inset
        # 默认风格需要不透明底，避免半透明窗口首帧露出白底；PCL 风格必须保持透明
        self.setAutoFillBackground(not self._pcl())
        if not self._pcl():
            palette = self.palette()
            palette.setColor(QPalette.ColorRole.Window, QColor("#F2F6FC"))
            self.setPalette(palette)
        self.backdrop.set_shell(shell, 18, 6)
        self._root_layout.setContentsMargins(inset, inset, inset, inset)
        bar = getattr(self, "topbar", None)
        if bar is not None:
            bar.setProperty("shell", "true" if shell else "false")
            bar.style().unpolish(bar)
            bar.style().polish(bar)
        self._place_overlays()

    def _rebuild_layout(self) -> None:
        """风格变化后重建标题栏与全部页面，保留当前所在的一级页面。"""
        if not hasattr(self, "stack"):
            return
        current = self.stack.currentWidget()
        name = next((key for key, page in self.pages.items() if page is current), "home")
        target = self._sub_parent.get(name, name)
        if target not in self._page_factories:
            target = "home"

        for key, page in list(self.pages.items()):
            self.pages.pop(key, None)
            self.stack.removeWidget(page)
            page.deleteLater()
        self._sub_parent.clear()
        self._account_dialog = None

        # 旧页面里的控件引用全部作废，避免重建过程中访问已销毁对象
        for attr in (
            "home_right", "mods_list", "saves_list", "packs_list",
            "shaders_list", "mods_count", "saves_count", "packs_count",
            "shaders_count", "resource_stack", "resource_hint", "resource_tabs",
            "version_combo", "instance_menu_button", "path_label", "daily_label",
            "login_type_label", "avatar", "account_name", "account_switch",
            "home_hint", "launch_button",
        ):
            setattr(self, attr, None)
        self.resource_buttons = {}

        old = getattr(self, "topbar", None)
        central = self.centralWidget()
        root = central.layout() if central is not None else None
        if old is not None:
            if root is not None:
                root.removeWidget(old)
            old.setParent(None)
            old.deleteLater()
        self.topbar = self._build_topbar()
        if root is not None:
            root.insertWidget(0, self.topbar)
        self._apply_shell()

        self._ensure_page(target)
        self.show_page(target)
        self._place_overlays()

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
        self._root_layout = root
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
        #: 子页面（版本设置 / 账户 / 皮肤）→ 所属的一级页面，用于高亮左侧导航
        self._sub_parent: dict[str, str] = {}
        root.addWidget(self.stack, 1)

        # 版本号（右下）、下载源（左下）、任务面板（浮层）都不占内容区高度
        self._build_overlays()
        self._apply_shell()
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

    def _pcl(self) -> bool:
        """当前是否是「原版 PCL」界面风格。"""
        return str(getattr(self.config, "ui_style", "default") or "default") == "pcl"

    def _avatar_px(self) -> int:
        """启动页头像边长：PCL 风格用小头像，默认风格用大头像。"""
        return PCL_AVATAR_PX if self._pcl() else AVATAR_PX

    def _build_topbar(self) -> QWidget:
        """按界面风格构建标题栏：默认风格是 5k 菜单栏，PCL 风格是 48px 深色标题栏。"""
        return self._build_topbar_pcl() if self._pcl() else self._build_topbar_default()

    def _build_topbar_default(self) -> QWidget:
        """默认风格顶部菜单栏（5k）：logo 在左，五项导航居中，右侧状态与窗口按钮。"""
        bar = TitleBar(self)
        bar.setFixedHeight(TOPBAR_H)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(u(1.5), 0, u(1.5), 0)
        layout.setSpacing(u(0.75))

        side = u(14)

        brand = QWidget()
        brand.setFixedWidth(side)
        brand_row = QHBoxLayout(brand)
        brand_row.setContentsMargins(0, 0, 0, 0)
        brand_row.setSpacing(u(0.5))
        self.brand_logo = QLabel()
        self.brand_logo.setFixedSize(36, 36)
        self.brand_logo.setPixmap(block_pixmap("crmcdownload", 36))
        brand_row.addWidget(self.brand_logo)
        name = QLabel(APP_NAME)
        name.setObjectName("brand")
        brand_row.addWidget(name)
        brand_row.addStretch(1)
        layout.addWidget(brand)
        layout.addStretch(1)

        # 默认风格的页面大标题在页面内部，这里只为兼容 _update_topbar 保留一个隐藏标题
        self.page_title = QLabel("启动")
        self.page_title.setObjectName("pageTitle")
        self.page_title.setVisible(False)
        layout.addWidget(self.page_title)

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
            button.clicked.connect(lambda _c, page=key: self.show_page(page))
            group.addButton(button)
            nav_row.addWidget(button)
            self.nav_buttons[key] = button
            self._set_nav_icon(button, key, False)
        layout.addLayout(nav_row)
        layout.addStretch(1)

        status = QWidget()
        status.setFixedWidth(side)
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

    def _build_topbar_pcl(self) -> QWidget:
        """PCL2 标题栏（FormMain.xaml PanTitle）：深色 #343D4A、48px、白 logo + 居中导航。"""
        bar = TitleBar(self)
        bar.setObjectName("pclTopbar")
        bar.setFixedHeight(PCL_TOPBAR_H)
        grid = QGridLayout(bar)
        grid.setContentsMargins(19, 0, 12, 0)
        grid.setSpacing(0)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)

        # 左：PCL 白色 logo（子页面时整体换成「返回箭头 + 页面名」）
        left = QWidget()
        left_row = QHBoxLayout(left)
        left_row.setContentsMargins(0, 0, 0, 0)
        left_row.setSpacing(4)
        self.brand_logo = QLabel("PCL")
        self.brand_logo.setObjectName("pclBrand")
        self.brand_logo.setToolTip(APP_NAME)
        left_row.addWidget(self.brand_logo)

        self.back_button = QPushButton()
        self.back_button.setObjectName("pclTitleBtn")
        self.back_button.setFixedSize(28, 28)
        self.back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_button.setToolTip("返回")
        self.back_button.setIcon(QIcon(icon_pixmap("chevron-left", 17, "#FFFFFF")))
        self.back_button.setIconSize(QSize(17, 17))
        self.back_button.clicked.connect(self._go_back)
        self.back_button.setVisible(False)
        left_row.addWidget(self.back_button)

        self.page_title = QLabel("启动")
        self.page_title.setObjectName("pclPageTitle")
        self.page_title.setVisible(False)
        left_row.addWidget(self.page_title)
        left_row.addStretch(1)
        grid.addWidget(left, 0, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        # 中：标题栏导航（PCL 的 MyRadioButton：白字 + 图标）
        self.nav_bar = QWidget()
        nav_row = QHBoxLayout(self.nav_bar)
        nav_row.setContentsMargins(0, 0, 0, 0)
        nav_row.setSpacing(10)
        self.nav_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for key, text in NAV_ITEMS:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setObjectName("pclTopnav")
            button.setFixedHeight(27)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setIcon(QIcon(icon_pixmap(NAV_ICONS.get(key, "blocks"), 16, "#FFFFFF")))
            button.setIconSize(QSize(16, 16))
            button.clicked.connect(lambda _c, page=key: self.show_page(page))
            group.addButton(button)
            nav_row.addWidget(button)
            self.nav_buttons[key] = button
        grid.addWidget(self.nav_bar, 0, 1, Qt.AlignmentFlag.AlignCenter)

        controls = self._build_window_controls()
        grid.addWidget(controls, 0, 2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # PCL 标题栏不显示状态文字，但任务进度仍会写入它（保持隐藏）
        self.status_label = ElidedLabel("就绪", bar)
        self.status_label.setObjectName("pclStatus")
        self.status_label.setVisible(False)
        self._sync_window_controls()
        return bar

    def _go_back(self) -> None:
        """标题栏返回箭头：回到所属一级页面（子页面回启动页）。"""
        current = ""
        widget = getattr(self, "stack", None)
        if widget is not None:
            page = widget.currentWidget()
            current = next((name for name, item in self.pages.items() if item is page), "")
        self.show_page(self._sub_parent.get(current, "home"))

    def _update_topbar(self, name: str) -> None:
        """同步标题栏：默认风格高亮导航项，PCL 风格切换 logo / 返回箭头与页面名。"""
        if not hasattr(self, "page_title"):
            return
        if self._pcl():
            is_main = name in self.nav_buttons
            self.page_title.setText(PAGE_TITLES.get(name, name))
            self.page_title.setVisible(not is_main)
            self.back_button.setVisible(not is_main)
            self.brand_logo.setVisible(is_main)
            self.nav_bar.setVisible(is_main)
            for key, button in self.nav_buttons.items():
                active = key == name
                button.setChecked(active)
                # PCL 的 MyRadioButton：未选中白色图标，选中后变蓝
                button.setIcon(
                    QIcon(
                        icon_pixmap(
                            NAV_ICONS.get(key, "blocks"),
                            16,
                            "#1370F3" if active else "#FFFFFF",
                        )
                    )
                )
            return
        for key, button in self.nav_buttons.items():
            active = key == self._sub_parent.get(name, name)
            if button.isChecked() != active:
                button.setChecked(active)
            self._set_nav_icon(button, key, active)

    def _set_nav_icon(self, button: QPushButton, key: str, active: bool) -> None:
        """顶部导航图标随选中状态着色。"""
        color = ACCENT if active else "#6B7688"
        button.setIcon(QIcon(icon_pixmap(NAV_ICONS.get(key, "blocks"), 16, color)))
        button.setIconSize(QSize(16, 16))

    def _set_tab_icon(self, button: QPushButton, key: str, active: bool) -> None:
        """下载页分类图标随选中状态着色。"""
        color = ACCENT if active else "#6B7688"
        button.setIcon(QIcon(icon_pixmap(DOWNLOAD_TAB_ICONS.get(key, "blocks"), 18, color)))
        button.setIconSize(QSize(18, 18))

    def _build_window_controls(self) -> QWidget:
        """标题栏右侧的最小化 / 最大化 / 关闭按钮（PCL 风格无最大化，28px 见方）。"""
        compact = self._pcl()
        wrap = QWidget()
        wrap.setFixedWidth(60 if compact else u(7))
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4 if compact else u(0.25))
        row.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        size = QSize(28, 28) if compact else QSize(32, 30)

        self.win_min = QPushButton()
        self.win_min.setObjectName("winBtn")
        self.win_min.setFixedSize(size)
        self.win_min.setCursor(Qt.CursorShape.PointingHandCursor)
        self.win_min.setToolTip("最小化")
        self.win_min.clicked.connect(self.showMinimized)

        self.win_max = QPushButton()
        self.win_max.setObjectName("winBtn")
        self.win_max.setFixedSize(size)
        self.win_max.setCursor(Qt.CursorShape.PointingHandCursor)
        self.win_max.clicked.connect(self._toggle_maximize)
        # 只在需要隐藏时调用：此时按钮还没加入标题栏，setVisible(True) 会闪出一个独立小窗口
        if compact:
            self.win_max.hide()

        self.win_close = QPushButton()
        self.win_close.setObjectName("winClose")
        self.win_close.setFixedSize(size)
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
        """按当前窗口状态切换最大化/还原图标；PCL 蓝色标题栏用白色图标。"""
        if not hasattr(self, "win_max"):
            return
        color = "#FFFFFF" if self._pcl() else "#5B6678"
        name = "restore" if self.isMaximized() else "maximize"
        self.win_max.setIcon(QIcon(icon_pixmap(name, 16, color)))
        self.win_max.setIconSize(QSize(16, 16))
        self.win_max.setToolTip("还原" if self.isMaximized() else "最大化")
        self.win_min.setIcon(QIcon(icon_pixmap("minimize", 16, color)))
        self.win_min.setIconSize(QSize(16, 16))
        self.win_close.setIcon(QIcon(icon_pixmap("close", 16, color)))
        self.win_close.setIconSize(QSize(16, 16))

    # ------------------------------------------------------------- 窗口外壳
    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        if not getattr(self, "_chrome_ready", False):
            self._chrome_ready = True
            enable_native_chrome(self)
        self._place_overlays()
        # 半透明窗口在部分机器上首帧会停在白色底，主动补一次完整重绘
        QTimer.singleShot(0, self._first_paint)

    def _first_paint(self) -> None:
        """首帧补绘：避免启动瞬间内容区显示成一块纯白。"""
        central = self.centralWidget()
        if central is not None:
            central.update()
            central.repaint()
        self.update()
        self.repaint()

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync_window_controls()
            self._apply_shell()

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
        """启动页：默认风格是「左账户卡 + 右版本栏/资源面板」，PCL 风格按 PageLaunch 复刻。"""
        return self._build_home_page_pcl() if self._pcl() else self._build_home_page_default()

    def _build_home_page_default(self) -> QWidget:
        """默认风格启动页：左 20k 账户卡，右 55k（版本栏 + 资源面板）。"""
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        left = QWidget()
        left.setFixedWidth(LEFT_W)
        column = QVBoxLayout(left)
        column.setContentsMargins(SIDE_PAD, u(0.75), SIDE_PAD, u(2))
        column.setSpacing(u(0.75))

        panel = Card(margins=(u(1.25), u(1.25), u(1.25), u(1.25)))

        self.avatar = QLabel()
        self.avatar.setFixedSize(AVATAR_PX, AVATAR_PX)
        self.avatar.setPixmap(self._avatar_pixmap(self._account_display_name(), AVATAR_PX))
        self.avatar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.avatar.setToolTip("点击查看 / 更换皮肤")
        self.avatar.mouseReleaseEvent = lambda _event: self.open_skin_page()  # type: ignore[method-assign]
        avatar_row = QHBoxLayout()
        avatar_row.setContentsMargins(0, 0, 0, 0)
        avatar_row.addStretch(1)
        avatar_row.addWidget(self.avatar)
        avatar_row.addStretch(1)
        panel.add_layout(avatar_row)

        caption = QPushButton(self._account_caption())
        caption.setFlat(True)
        caption.setCursor(Qt.CursorShape.PointingHandCursor)
        caption.setToolTip("切换离线账户 / 登录 Microsoft 正版账号")
        caption.setStyleSheet(
            "QPushButton { border: none; background: transparent; color: #7A8698;"
            " font-size: 9pt; padding: 0px; }"
            "QPushButton:hover { color: #2F6FD0; }"
        )
        caption.clicked.connect(self._open_account_page)
        self.account_switch = caption
        panel.add(caption)

        self.account_name = QLineEdit(self._account_display_name())
        self.account_name.setObjectName("heroName")
        self.account_name.setFixedHeight(u(2.5))
        self.account_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.account_name.setMaxLength(16)
        self.account_name.setToolTip("离线账户名：点击即可修改，回车生效")
        self.account_name.editingFinished.connect(self._apply_account_name)
        if self.config.account_provider == "microsoft":
            self.account_name.setReadOnly(True)
            self.account_name.setToolTip("正版账户名由 Microsoft 提供，点上方账户标签可切换账户")
        panel.add(self.account_name)

        panel.body.addSpacing(u(0.5))
        daily = QFrame()
        daily.setObjectName("softCard")
        daily_box = QVBoxLayout(daily)
        daily_box.setContentsMargins(u(0.75), u(0.6), u(0.75), u(0.6))
        self.daily_label = QLabel(FALLBACK.display)
        self.daily_label.setObjectName("mutedSmall")
        self.daily_label.setWordWrap(True)
        self.daily_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        daily_box.addWidget(self.daily_label)
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
        self.launch_button.clicked.connect(self._launch_or_stop)
        panel.add(self.launch_button)

        panel.body.addSpacing(u(0.75))
        actions = QHBoxLayout()
        actions.setSpacing(u(0.75))
        instance_button = QPushButton("版本设置")
        instance_button.setFixedHeight(u(2.25))
        instance_button.clicked.connect(self._open_instance_page)
        actions.addWidget(instance_button, 1)
        account_button = QPushButton("账户设置")
        account_button.setFixedHeight(u(2.25))
        account_button.clicked.connect(self._open_account_page)
        actions.addWidget(account_button, 1)
        panel.add_layout(actions)

        column.addWidget(panel)
        layout.addWidget(left)

        right = QWidget()
        right_column = QVBoxLayout(right)
        right_column.setContentsMargins(0, u(0.75), SIDE_PAD, u(2))
        right_column.setSpacing(0)
        right_column.addWidget(self._build_home_version_bar())
        middle = self._build_home_middle()
        self.home_right = middle
        right_column.addWidget(middle, 1)
        layout.addWidget(right, 1)

        self._load_daily_sentence()
        self._apply_simple_mode()
        return page

    def _build_home_version_bar(self) -> QWidget:
        """右侧顶部版本栏：当前版本下拉 + 版本菜单 + 目录路径。"""
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, u(0.75))
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

    def _build_home_page_pcl(self) -> QWidget:
        """PCL 风格启动页（PageLaunchLeft）：登录方式 + 账户 + 启动按钮 + 版本选择。"""
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        left = QFrame()
        left.setObjectName("pclLeftPane")
        left.setFixedWidth(PCL_LEFT_W)
        column = QVBoxLayout(left)
        column.setContentsMargins(24, 0, 24, 24)
        column.setSpacing(0)

        # 顶部：登录方式胶囊（PCL PanTypeOne，27px 高，点击切换账户）
        column.addSpacing(24)
        type_row = QHBoxLayout()
        type_row.addStretch(1)
        pill = QFrame()
        pill.setObjectName("pclPill")
        pill.setFixedHeight(27)
        pill.setCursor(Qt.CursorShape.PointingHandCursor)
        pill.setToolTip("切换账户")
        pill.mouseReleaseEvent = lambda _event: self._open_account_page()  # type: ignore[method-assign]
        pill_layout = QHBoxLayout(pill)
        pill_layout.setContentsMargins(12, 0, 12, 0)
        pill_layout.setSpacing(6)
        self.login_type_label = QLabel(
            "正版登录" if self.config.account_provider == "microsoft" else "离线登录"
        )
        self.login_type_label.setObjectName("pclPillText")
        pill_layout.addWidget(self.login_type_label)
        type_row.addWidget(pill)
        type_row.addStretch(1)
        column.addLayout(type_row)

        # 中部：账户区（PCL PanLogin 垂直居中）
        column.addStretch(1)
        column.addWidget(self._build_home_account_card())
        column.addStretch(1)

        # 启动按钮（PCL BtnLaunch 高 54，版本名显示在按钮内部下方）
        self.launch_button = QPushButton("启动游戏")
        self.launch_button.setObjectName("pclLaunch")
        self.launch_button.setFixedHeight(54)
        self.launch_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.launch_button.clicked.connect(self._launch_or_stop)
        self.home_hint = QLabel("", self.launch_button)
        self.home_hint.setObjectName("pclLaunchVersion")
        self.home_hint.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        hint_row = QVBoxLayout(self.launch_button)
        hint_row.setContentsMargins(24, 27, 24, 9)
        hint_row.addWidget(self.home_hint)
        column.addWidget(self.launch_button)

        # 版本选择（PCL BtnVersion 高 35）+ 版本设置（BtnMore）
        column.addSpacing(14)
        version_row = QHBoxLayout()
        version_row.setSpacing(12)
        self.version_combo = QComboBox()
        self.version_combo.setObjectName("pclVersion")
        self.version_combo.setMinimumWidth(u(9))
        self.version_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        self.version_combo.setFixedHeight(35)
        self.version_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.version_combo.setToolTip("当前版本：点击下拉切换")
        self.version_combo.currentIndexChanged.connect(self._on_version_selected)
        self.version_combo.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.version_combo.customContextMenuRequested.connect(
            lambda pos: self._instance_menu(self.version_combo.mapToGlobal(pos))
        )
        version_row.addWidget(self.version_combo, 1)

        self.instance_menu_button = QPushButton("版本设置")
        self.instance_menu_button.setObjectName("pclSmall")
        self.instance_menu_button.setFixedHeight(35)
        self.instance_menu_button.setFixedWidth(76)
        self.instance_menu_button.setToolTip("版本设置 / 安装加载器 / 打开目录 / 删除实例")
        self.instance_menu_button.clicked.connect(self._open_instance_page)
        version_row.addWidget(self.instance_menu_button)
        column.addLayout(version_row)
        layout.addWidget(left)

        # 右栏：按你的要求不放任何卡片，仅保留空白背景
        right = QWidget()
        right.setObjectName("pclHomeRight")
        self.home_right = right
        layout.addWidget(right, 1)

        self._apply_simple_mode()
        return page

    def _build_home_account_card(self) -> QWidget:
        """账户区（PCL PanLogin）：皮肤头像 + 用户名输入框，垂直居中。"""
        card = QWidget()
        card.setObjectName("pclLogin")
        column = QVBoxLayout(card)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(15)
        column.addStretch(1)

        self.avatar = QLabel()
        self.avatar.setFixedSize(PCL_AVATAR_PX, PCL_AVATAR_PX)
        self.avatar.setPixmap(
            self._avatar_pixmap(self._account_display_name(), PCL_AVATAR_PX)
        )
        self.avatar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.avatar.setToolTip("点击查看 / 更换皮肤")
        self.avatar.mouseReleaseEvent = lambda _event: self.open_skin_page()  # type: ignore[method-assign]
        avatar_row = QHBoxLayout()
        avatar_row.setContentsMargins(0, 0, 0, 0)
        avatar_row.addStretch(1)
        avatar_row.addWidget(self.avatar)
        avatar_row.addStretch(1)
        column.addLayout(avatar_row)

        self.account_name = QLineEdit(self._account_display_name())
        self.account_name.setObjectName("pclName")
        self.account_name.setFixedHeight(28)
        self.account_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.account_name.setMaxLength(16)
        self.account_name.setToolTip("离线账户名：点击即可修改，回车生效")
        self.account_name.editingFinished.connect(self._apply_account_name)
        if self.config.account_provider == "microsoft":
            self.account_name.setReadOnly(True)
            self.account_name.setToolTip("正版账户名由 Microsoft 提供，点上方登录方式可切换账户")
        column.addWidget(self.account_name)
        column.addStretch(1)
        return card

    def _apply_simple_mode(self) -> None:
        """简洁模式：隐藏右侧区域，只留下版本 / 启动 / 账户。"""
        simple = bool(getattr(self.config, "simple_mode", False))
        widget = getattr(self, "home_right", None)
        if widget is not None:
            widget.setVisible(not simple)

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

    def _build_home_middle(self) -> QWidget:
        """资源面板：左侧是分类（模组 / 存档 / 资源 / 光影），右侧是内容。"""
        card = Card(margins=(0, 0, 0, 0))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        nav = QWidget()
        nav.setFixedWidth(u(6.2))
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(u(0.9), u(1), u(0.5), u(0.9))
        nav_layout.setSpacing(u(0.3))
        self.resource_tabs = QButtonGroup(card)
        self.resource_tabs.setExclusive(True)
        self.resource_buttons: dict[str, QPushButton] = {}
        for index, (key, title) in enumerate(HOME_RESOURCE_TABS):
            button = QPushButton(title)
            button.setObjectName("homeTab")
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFixedHeight(u(2.1))
            self.resource_tabs.addButton(button, index)
            nav_layout.addWidget(button)
            self.resource_buttons[key] = button
        nav_layout.addStretch(1)
        self.resource_hint = QLabel("")
        self.resource_hint.setWordWrap(True)
        self.resource_hint.setStyleSheet(f"color: {DANGER}; font-size: 8pt;")
        self.resource_hint.setVisible(False)
        nav_layout.addWidget(self.resource_hint)
        row.addWidget(nav)

        self.resource_stack = QStackedWidget()
        mods_page, self.mods_list, self.mods_count = self._home_list_page(
            "模组",
            [],
            self._mods_menu,
        )
        self.resource_stack.addWidget(mods_page)

        saves_page, self.saves_list, self.saves_count = self._home_list_page(
            "存档",
            [("导入", "从 ZIP 导入存档", "download", self._import_save)],
            self._saves_menu,
        )
        self.resource_stack.addWidget(saves_page)

        self.packs_list, self.packs_count = self._pack_page("resourcepacks", "资源")
        self.shaders_list, self.shaders_count = self._pack_page("shaderpacks", "光影")
        row.addWidget(self.resource_stack, 1)
        card.add_layout(row, 1)
        self.resource_tabs.idClicked.connect(self.resource_stack.setCurrentIndex)
        # 每 30 秒自动重新检查一次资源（只有版本设置页在前台时才刷）
        old_timer = getattr(self, "_home_refresh_timer", None)
        if old_timer is not None:
            old_timer.stop()
            old_timer.deleteLater()
        self._home_refresh_timer = QTimer(self)
        self._home_refresh_timer.setInterval(30_000)
        self._home_refresh_timer.timeout.connect(self._auto_refresh_home)
        self._home_refresh_timer.start()
        return card

    def _auto_refresh_home(self) -> None:
        """每 30 秒刷新一次资源列表：默认风格在启动页，PCL 风格在版本设置页。"""
        for name in ("home", "instance"):
            page = self.pages.get(name)
            if page is not None and self.stack.currentWidget() is page:
                self._refresh_home_lists()
                return

    def _blank_menu(self, widget: QListWidget, pos) -> None:
        """列表空白处右键：刷新当前列表。"""
        menu = QMenu(self)
        refresh = menu.addAction("刷新")
        if menu.exec(widget.mapToGlobal(pos)) is refresh:
            self._refresh_home_lists()

    def _home_list_page(self, title: str, actions, menu) -> tuple[QWidget, QListWidget, QLabel]:
        """一页列表：标题 + 数量 + 操作按钮 + 列表（右键菜单由 ``menu`` 提供）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(u(1.25), u(1), u(1.25), u(1))
        layout.setSpacing(u(0.5))

        head = QHBoxLayout()
        head.setSpacing(u(0.5))
        head.addWidget(self._label(title, "h2"))
        count = self._label("", "mutedSmall")
        head.addWidget(count)
        head.addStretch(1)
        for text, tip, icon, slot in actions:
            button = QPushButton(text)
            button.setObjectName("segment")
            button.setToolTip(tip)
            button.setIcon(QIcon(icon_pixmap(icon, 14, "#5B6678")))
            button.setIconSize(QSize(14, 14))
            button.clicked.connect(slot)
            head.addWidget(button)
        layout.addLayout(head)

        widget = QListWidget()
        widget.setObjectName("homeList")
        widget.setFrameShape(QFrame.Shape.NoFrame)
        widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        def on_menu(pos, target=widget) -> None:
            if target.itemAt(pos) is None:
                self._blank_menu(target, pos)
            else:
                menu(target, pos)

        widget.customContextMenuRequested.connect(on_menu)
        layout.addWidget(widget, 1)
        return page, widget, count

    def _pack_page(self, kind: str, title: str) -> tuple[QListWidget, QLabel]:
        """资源包 / 光影页：只保留列表，刷新走右键空白处与 30 秒自动刷新。"""
        page, widget, count = self._home_list_page(
            title,
            [],
            lambda target, pos, current=kind: self._packs_menu(target, current, pos),
        )
        self.resource_stack.addWidget(page)
        return widget, count

    # ------------------------------------------------------- 版本设置（页内）
    def _show_sub_page(self, key: str, builder, parent: str = "home") -> None:
        """把子页面（版本设置 / 账户 / 皮肤）放进主栈里，返回键回到 ``parent``。"""
        old = self.pages.pop(key, None)
        if old is not None:
            self.stack.removeWidget(old)
            old.deleteLater()
        page = builder()
        self.pages[key] = page
        self._sub_parent[key] = parent
        self.stack.addWidget(page)
        self.stack.setCurrentWidget(page)
        fade_in(page)
        self._update_topbar(key)

    def _build_instance_page(self) -> QWidget:
        """版本设置页：版本信息 + 资源列表 + 全局配置（内存 / Java / JVM）+ 目录与导出。"""
        instance = self.selected
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(SIDE_PAD, u(0.75), SIDE_PAD, u(1.5))
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        root = QVBoxLayout(host)
        root.setContentsMargins(0, 0, u(0.5), 0)
        root.setSpacing(u(0.75))
        scroll.setWidget(host)
        outer.addWidget(scroll)

        if instance is None:
            root.addWidget(self._label("还没有选择版本", "muted"))
            root.addStretch(1)
            return page

        info = Card(margins=(u(1.25), u(1), u(1.25), u(1)))
        head = QHBoxLayout()
        head.setSpacing(u(0.85))
        icon = QLabel()
        icon.setFixedSize(52, 52)
        icon.setPixmap(block_pixmap(instance.id, 52, instance.loader))
        head.addWidget(icon)
        names = QVBoxLayout()
        names.setSpacing(2)
        names.addWidget(self._label(instance.id, "h2"))
        subtitle = (
            f"{instance.loader} · MC {instance.mc_version} · {human_size(instance.size_bytes)}"
        )
        names.addWidget(self._label(subtitle, "muted"))
        head.addLayout(names, 1)
        info.add_layout(head)
        pack = read_pack_metadata(
            Path(instance.root) if instance.root else self.game_dir(), instance.id
        )
        if pack:
            info.add(self._label(f"整合包：{pack.get('name', '')} {pack.get('version', '')}".strip(), "muted"))
        if instance.broken:
            broken = QLabel(instance.broken)
            broken.setObjectName("error")
            broken.setWordWrap(True)
            info.add(broken)
        info.add(self._readonly_row("游戏目录", str(self.game_dir_for(instance))))
        info.add(
            self._readonly_row(
                "版本目录",
                str((Path(instance.root) if instance.root else self.game_dir()) / "versions" / instance.id),
            )
        )
        root.addWidget(info)

        config = Card(margins=(u(1.25), u(1), u(1.25), u(1)))
        config.add_layout(self._section_head("配置", "内存、Java 与 JVM 参数会随启动命令生效"))
        memory_row = QHBoxLayout()
        memory_row.setSpacing(u(0.75))
        memory_row.addWidget(self._label("内存"))
        self.instance_memory = QSlider(Qt.Orientation.Horizontal)
        self.instance_memory.setRange(1024, 32768)
        self.instance_memory.setSingleStep(512)
        self.instance_memory.setPageStep(1024)
        self.instance_memory.setValue(int(self.config.memory_mb))
        self.instance_memory.valueChanged.connect(self._on_memory_changed)
        memory_row.addWidget(self.instance_memory, 1)
        self.instance_memory_value = QLabel(f"{self.config.memory_mb} MB")
        self.instance_memory_value.setMinimumWidth(u(5))
        self.instance_memory_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.instance_memory_value.setStyleSheet("font-weight: 700;")
        memory_row.addWidget(self.instance_memory_value)
        config.add_layout(memory_row)

        java_row = QHBoxLayout()
        java_row.setSpacing(u(0.75))
        java_row.addWidget(self._label("Java"))
        self.instance_java = QComboBox()
        self.instance_java.setToolTip("启动游戏使用的 Java（自动选择会按版本要求匹配）")
        self.instance_java.currentIndexChanged.connect(self._on_java_changed)
        java_row.addWidget(self.instance_java, 1)
        detect = QPushButton("重新检测")
        detect.setIcon(QIcon(icon_pixmap("refresh", 14, "#5B6678")))
        detect.setIconSize(QSize(14, 14))
        detect.clicked.connect(self._refresh_java_combo)
        java_row.addWidget(detect)
        config.add_layout(java_row)

        jvm_row = QHBoxLayout()
        jvm_row.setSpacing(u(0.75))
        jvm_row.addWidget(self._label("参数"))
        self.instance_jvm = QLineEdit(" ".join(self.config.extra_jvm))
        self.instance_jvm.setPlaceholderText("额外 JVM 参数，例如 -XX:+UseG1GC")
        self.instance_jvm.editingFinished.connect(self._apply_settings_jvm)
        jvm_row.addWidget(self.instance_jvm, 1)
        config.add_layout(jvm_row)
        root.addWidget(config)

        actions = Card(margins=(u(1.25), u(0.85), u(1.25), u(0.85)))
        buttons = QHBoxLayout()
        buttons.setSpacing(u(0.75))
        open_game = QPushButton("打开游戏目录")
        open_game.clicked.connect(lambda: _reveal(self.game_dir_for(instance)))
        buttons.addWidget(open_game)
        script = QPushButton("导出启动脚本")
        script.setToolTip("把启动命令导出为 .bat，方便脱离启动器运行")
        script.clicked.connect(lambda: self._export_launch_script(instance))
        buttons.addWidget(script)
        buttons.addStretch(1)
        remove = QPushButton("删除实例")
        remove.setObjectName("danger")
        remove.clicked.connect(lambda: self._delete_instance(instance))
        buttons.addWidget(remove)
        actions.add_layout(buttons)
        root.addWidget(actions)

        # 资源列表：PCL 风格放在版本设置页（原启动页右栏）；默认风格仍在启动页
        if self._pcl():
            resource = self._build_home_middle()
            resource.setMinimumHeight(u(20))
            root.addWidget(resource)
            if (instance.loader or "原版") in ("原版", "未知", ""):
                mods_button = self.resource_buttons.get("mods")
                if mods_button is not None:
                    mods_button.setVisible(False)
                    self.resource_stack.setCurrentIndex(1)
                    self.resource_buttons["saves"].setChecked(True)
            self._refresh_home_lists()

        root.addStretch(1)
        self._refresh_java_combo()
        return page

    @staticmethod
    def _section_head(title: str, hint: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        head = QLabel(title)
        head.setObjectName("h2")
        row.addWidget(head)
        note = QLabel(hint)
        note.setObjectName("mutedSmall")
        row.addWidget(note)
        row.addStretch(1)
        return row

    @staticmethod
    def _readonly_row(name: str, value: str) -> QWidget:
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

    def _apply_settings_jvm(self) -> None:
        """JVM 参数：回车即保存（设置页与版本设置页共用同一份配置）。"""
        sender = self.sender()
        text = sender.text() if isinstance(sender, QLineEdit) else ""
        if not text:
            for widget in (
                getattr(self, "settings_jvm", None),
                getattr(self, "instance_jvm", None),
            ):
                if widget is not None:
                    text = widget.text()
                    break
        self.config.extra_jvm = [part for part in text.split(" ") if part]
        save_config(self.config, self.data_dir)
        self._sync_jvm_edits()

    def _sync_jvm_edits(self) -> None:
        text = " ".join(self.config.extra_jvm)
        for widget in (
            getattr(self, "settings_jvm", None),
            getattr(self, "instance_jvm", None),
        ):
            if widget is not None and widget.text() != text:
                widget.setText(text)

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
    def _list_dirs(folder: Path) -> list[Path]:
        try:
            return sorted(
                (path for path in folder.iterdir() if path.is_dir()),
                key=lambda p: p.name.lower(),
            )
        except OSError:
            return []

    def _resource_widgets_alive(self) -> bool:
        """资源列表只存在于「版本设置」页，页面销毁后要避免再访问。"""
        widget = getattr(self, "mods_list", None)
        return widget is not None and not sip.isdeleted(widget)

    def _refresh_home_lists(self) -> None:
        """重新读取当前版本的模组 / 存档 / 资源 / 光影，并标出不在加载目录的资源。"""
        if not self._resource_widgets_alive():
            return
        instance = self.selected
        base = self.game_dir_for(instance) if instance is not None else self.game_dir()
        report = ResourceAudit(load_dir=base)
        if instance is not None:
            mc_dir = Path(instance.root) if instance.root else self.game_dir()
            isolated = bool(self.config.isolation) or read_pack_metadata(mc_dir, instance.id) is not None
            try:
                report = audit_resources(mc_dir, instance.id, isolated=isolated)
            except OSError:
                report = ResourceAudit(load_dir=base)

        buckets: dict[str, list] = {kind: [] for kind in KINDS}
        for entry in report.entries:
            buckets.setdefault(entry.kind, []).append(entry)

        self.mods_count.setText(
            self._fill_resource_list(self.mods_list, buckets["mods"], "暂无模组")
        )
        self.packs_count.setText(
            self._fill_resource_list(self.packs_list, buckets["resourcepacks"], "暂无资源包")
        )
        self.shaders_count.setText(
            self._fill_resource_list(self.shaders_list, buckets["shaderpacks"], "暂无光影")
        )

        saves = self._list_dirs(base / "saves")
        self.saves_list.clear()
        for folder in saves:
            item = QListWidgetItem(QIcon(block_pixmap(folder.name, 20)), folder.name)
            item.setData(Qt.ItemDataRole.UserRole, str(folder))
            try:
                item.setToolTip(f"{folder}\n{human_date(folder.stat().st_mtime)}")
            except OSError:
                item.setToolTip(str(folder))
            self.saves_list.addItem(item)
        self.saves_count.setText(f"{len(saves)} 个" if saves else "")
        if not saves:
            self._add_empty_item(self.saves_list, "暂无存档")

        problems = report.problem_count
        self.resource_hint.setText(f"{problems} 个资源不会加载" if problems else "")
        self.resource_hint.setToolTip(
            "\n".join(
                [f"{KIND_NAMES[e.kind]}：{e.path.name} 不在加载目录" for e in report.misplaced]
                + [f"资源包：{name} 已启用但文件缺失" for name in report.missing]
            )
        )
        self.resource_hint.setVisible(bool(problems))

    def _fill_resource_list(self, widget: QListWidget, entries, empty_text: str) -> str:
        """填充一个资源列表，返回数量的展示文本。"""
        widget.clear()
        for entry in entries:
            text = entry.path.name
            if entry.disabled:
                text += "（已禁用）"
            if entry.misplaced:
                text += "（不在加载目录）"
            item = QListWidgetItem(QIcon(block_pixmap(entry.path.name, 20)), text)
            tip = str(entry.path)
            if entry.misplaced:
                tip += "\n不在本次启动的加载目录，游戏不会加载它（右键可移动）"
            item.setToolTip(tip)
            item.setData(Qt.ItemDataRole.UserRole, str(entry.path))
            item.setData(Qt.ItemDataRole.UserRole + 1, entry.kind)
            widget.addItem(item)
        if not entries:
            self._add_empty_item(widget, empty_text)
        return f"{len(entries)} 个" if entries else ""

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
        if not self._pcl():
            column.addWidget(self._label("下载", "title"))
            self.download_sub = self._label(
                "游戏版本、模组、整合包、资源包与光影，全部经由自建镜像加速"
            )
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
        self.dl_stack.addWidget(self._build_server_tab())
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
        """插件页：左 20k 分区导航，右侧渲染官方插件（其余分区保留框架）。"""
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        left = QWidget()
        left.setFixedWidth(LEFT_W)
        column = QVBoxLayout(left)
        column.setContentsMargins(SIDE_PAD, u(1), SIDE_PAD, u(2))
        column.setSpacing(u(0.5))
        if not self._pcl():
            column.addWidget(self._label("插件", "title"))
            intro = self._label(
                "启动器插件：市场 / 官方 / 社区 / 其他 / 已下载（接口未接入，先做框架）"
            )
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
        "official": "官方插件：启动器提供的官方插件（离线联机按需下载）列在这里。",
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
            size = self.avatar.width() if hasattr(self, "avatar") else self._avatar_px()
            scaled = pixmap.scaled(
                size,
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.avatar.setPixmap(self._round_pixmap(scaled, size))

    def open_skin_page(self) -> None:
        """皮肤页改为页内切换（入口：点击启动页头像）。"""

        def build() -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(SIDE_PAD, u(0.75), SIDE_PAD, u(1.5))
            layout.setSpacing(u(0.75))
            skin = SkinPage(self, read_only=True)
            layout.addWidget(skin, 1)
            page.destroyed.connect(lambda _obj=None: skin.shutdown())
            return page

        self._show_sub_page("skin", build)

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
        if not self._ensure_privacy():
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

    # ------------------------------------------------------------- 服务端下载
    def _build_server_tab(self) -> QWidget:
        """服务端页：选一个版本，下载官方 server.jar（参考 PCL 的服务端下载）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        card = Card()
        card.add(self._label("服务端", "h2"))
        hint = self._label(
            "下载官方的原版服务端 jar。已装 Forge / Fabric 的整合包服务端请用对应安装器，"
            "这里只提供 Mojang 发布的 server.jar。"
        )
        hint.setWordWrap(True)
        card.add(hint)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.server_combo = QComboBox()
        self.server_combo.setMinimumWidth(220)
        self.server_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        row.addWidget(self.server_combo, 1)
        self.server_button = QPushButton("下载服务端")
        self.server_button.setObjectName("accent")
        self.server_button.setIcon(QIcon(icon_pixmap("download", 15, "#FFFFFF")))
        self.server_button.setIconSize(QSize(15, 15))
        self.server_button.clicked.connect(self._download_server)
        row.addWidget(self.server_button)
        card.add_layout(row)
        self.server_note = self._label("")
        self.server_note.setWordWrap(True)
        card.add(self.server_note)
        layout.addWidget(card)
        layout.addStretch(1)
        self._refresh_server_tab()
        return page

    def _refresh_server_tab(self) -> None:
        """用版本清单填充服务端下拉框（只列正式版）。"""
        combo = getattr(self, "server_combo", None)
        if combo is None:
            return
        current = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        if self.manifest is not None:
            for summary in self.manifest.versions:
                if summary.type != "release":
                    continue
                combo.addItem(summary.id, summary)
        if current:
            index = combo.findText(current)
            if index >= 0:
                combo.setCurrentIndex(index)
        combo.blockSignals(False)
        if not combo.count():
            self.server_note.setText("版本清单尚未就绪，稍后会自动刷新")

    def _download_server(self) -> None:
        summary = self.server_combo.currentData()
        if not isinstance(summary, VersionSummary):
            self.notice("版本清单尚未就绪，请稍后再试", "WARN")
            return
        if self.cancel_event is not None and not self.cancel_event.is_set():
            self.notice("已有任务在进行，请等待完成或取消", "WARN")
            return
        if not self._ensure_privacy():
            return
        folder = config_dir(self.data_dir) / "servers"
        default = str(folder / f"server-{summary.id}.jar")
        target, _filter = QFileDialog.getSaveFileName(
            self, "保存服务端 jar", default, "JAR 文件 (*.jar)"
        )
        if not target:
            return
        dest = Path(target)
        title = f"下载服务端 {summary.id}"

        def worker(cancel: threading.Event) -> str:
            downloader = self.make_downloader()
            mirror = self.source(downloader)
            raw = downloader.fetch_json(mirror.rewrite(summary.url))
            if not isinstance(raw, dict):
                raise RuntimeError("版本 JSON 格式错误")
            server = (raw.get("downloads") or {}).get("server") or {}
            url = server.get("url")
            if not url:
                raise RuntimeError(f"{summary.id} 没有官方服务端（可能是快照或远古版本）")
            self.set_phase(f"下载 {summary.id} 服务端")
            ok = downloader.download(
                DownloadTask(
                    url=str(url),
                    dest=dest,
                    sha1=server.get("sha1"),
                    size=server.get("size"),
                    label=f"server-{summary.id}.jar",
                ),
                tracker=ProgressTracker(),
                cancel=cancel,
            )
            _reveal(dest.parent)
            return f"服务端已保存：{dest}" if ok else f"服务端已存在：{dest}"

        self.run_task(title, worker)

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
        """设置页：左侧目录 + 右侧内容，所有修改立即保存。"""
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(SIDE_PAD, u(1), SIDE_PAD, u(2))
        outer.setSpacing(u(0.75))
        if not self._pcl():
            outer.addWidget(self._label("设置", "title"))

        body = QHBoxLayout()
        body.setSpacing(u(1))
        self.settings_nav = QListWidget()
        self.settings_nav.setObjectName("settingsNav")
        self.settings_nav.setFixedWidth(u(11))
        self.settings_nav.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_nav.setStyleSheet(
            "QListWidget#settingsNav { background: transparent; border: none; }"
            "QListWidget#settingsNav::item { padding: 8px 10px; border-radius: 8px; }"
        )
        body.addWidget(self.settings_nav)
        self.settings_stack = QStackedWidget()
        body.addWidget(self.settings_stack, 1)
        outer.addLayout(body, 1)

        #: (导航标题, 卡片) —— 每张卡片独占一个右侧页面
        sections: list[tuple[str, QWidget]] = []

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
        self.settings_simple = QCheckBox("简洁模式：主界面只保留头像与启动，隐藏资源面板与快捷按钮")
        self.settings_simple.setChecked(bool(getattr(self.config, "simple_mode", False)))
        self.settings_simple.setToolTip(
            "开启后启动页更干净；版本选择仍可从顶部下拉框或「⋯」菜单进入"
        )
        look.add(self.settings_simple)
        style_row = QHBoxLayout()
        style_row.setSpacing(8)
        style_row.addWidget(self._label("界面风格"))
        self.settings_ui_style = QComboBox()
        self.settings_ui_style.addItem("默认（毛玻璃）", "default")
        self.settings_ui_style.addItem("原版 PCL", "pcl")
        self.settings_ui_style.setToolTip(
            "原版 PCL：复刻 PCL2 的白底卡片、浅蓝描边、方角按钮与 #1370F3 强调色"
        )
        style_row.addWidget(self.settings_ui_style)
        style_row.addStretch(1)
        look.add_layout(style_row)
        sections.append(("外观", look))

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
        self.settings_scan_other = QCheckBox("自动检测本机其他目录下的 MC 版本")
        self.settings_scan_other.setChecked(bool(getattr(self.config, "scan_other_versions", True)))
        self.settings_scan_other.setToolTip(
            "关闭后只显示当前游戏目录里的版本，启动更快"
        )
        game.add(self.settings_scan_other)
        sections.append(("游戏", game))

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
        sections.append(("Java 与内存", java))

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
        sections.append(("下载", download))

        skin_card = Card()
        skin_card.add(self._label("皮肤", "h2"))
        skin_text = self._label("查看当前账户的皮肤（上传功能暂未开放，也可以直接点启动页头像进入）")
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
        skin_open = QPushButton("查看我的皮肤")
        skin_open.clicked.connect(self.open_skin_page)
        skin_card.add(skin_open)
        sections.append(("皮肤", skin_card))

        sections.append(("更新", self._build_update_card()))

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
        privacy = QPushButton("隐私政策")
        privacy.setToolTip("查看数据收集与存储说明：你添加的服务器只保存在本机，不会自动上传。")
        privacy.setCursor(Qt.CursorShape.PointingHandCursor)
        privacy.clicked.connect(lambda: PrivacyDialog(self).exec())
        about_row.addWidget(privacy)
        easter = QPushButton("千万别点")
        easter.setObjectName("ghost")
        easter.setToolTip("真的别点。")
        easter.setCursor(Qt.CursorShape.PointingHandCursor)
        easter.clicked.connect(self._trigger_easter_egg)
        about_row.addWidget(easter)
        about_row.addStretch(1)
        about.add_layout(about_row)
        sections.append(("关于", about))

        for title, card in sections:
            self.settings_nav.addItem(QListWidgetItem(title))
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            holder = QWidget()
            column = QVBoxLayout(holder)
            column.setContentsMargins(0, 0, 10, 0)
            column.setSpacing(12)
            column.addWidget(card)
            column.addStretch(1)
            scroll.setWidget(holder)
            self.settings_stack.addWidget(scroll)
        self.settings_nav.currentRowChanged.connect(self.settings_stack.setCurrentIndex)
        self.settings_nav.setCurrentRow(0)
        self._wire_settings_autosave()
        return page

    def _wire_settings_autosave(self) -> None:
        """所有设置项变化即写回配置，不再需要「保存设置」按钮。"""
        for widget in (
            getattr(self, "settings_simple", None),
            getattr(self, "settings_isolate", None),
            getattr(self, "settings_skip_assets", None),
            getattr(self, "settings_scan_other", None),
            getattr(self, "settings_fastest", None),
        ):
            if widget is not None:
                widget.toggled.connect(lambda _checked: self._autosave_settings())
        if hasattr(self, "settings_skin_layout"):
            self.settings_skin_layout.currentIndexChanged.connect(
                lambda _index: self._autosave_settings()
            )
        if hasattr(self, "settings_ui_style"):
            self.settings_ui_style.currentIndexChanged.connect(
                lambda _index: self._autosave_settings()
            )
        if hasattr(self, "settings_game_dir"):
            self.settings_game_dir.editingFinished.connect(self._autosave_settings)
        if hasattr(self, "settings_jvm"):
            self.settings_jvm.editingFinished.connect(self._autosave_settings)
        if hasattr(self, "settings_memory"):
            self.settings_memory.sliderReleased.connect(self._autosave_settings)

    def _autosave_settings(self) -> None:
        """静默保存：只写配置与本地界面状态，不触发网络刷新。"""
        if not hasattr(self, "settings_game_dir"):
            return
        previous_style = getattr(self.config, "ui_style", "default")
        self._collect_settings()
        save_config(self.config, self.data_dir)
        if getattr(self.config, "ui_style", "default") != previous_style:
            self._apply_ui_style()
        self._apply_simple_mode()

    def _collect_settings(self) -> None:
        self.config.game_dir = self.settings_game_dir.text().strip()
        self.config.isolation = self.settings_isolate.isChecked()
        self.config.skip_assets = self.settings_skip_assets.isChecked()
        self.config.memory_mb = int(self.settings_memory.value())
        self.config.extra_jvm = [part for part in self.settings_jvm.text().split(" ") if part]
        if hasattr(self, "settings_fastest"):
            self.config.prefer_fastest = self.settings_fastest.isChecked()
        if hasattr(self, "settings_skin_layout"):
            self.config.skin_layout = self.settings_skin_layout.currentData() or "v1"
        if hasattr(self, "settings_ui_style"):
            self.config.ui_style = self.settings_ui_style.currentData() or "default"
        if hasattr(self, "settings_scan_other"):
            self.config.scan_other_versions = self.settings_scan_other.isChecked()
            if not self.config.scan_other_versions:
                self._scan_cache = None
        if hasattr(self, "settings_simple"):
            self.config.simple_mode = self.settings_simple.isChecked()

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
        if hasattr(self, "update_button"):
            self.update_button.setEnabled(False)
        self._set_update_status("正在检查更新…")
        base = (self.config.download_base or "").strip()
        token = self.config.download_token or ""
        proxy = self.config.proxy or None

        def report(text: str) -> None:
            self.events.put(("call", lambda t=text: self._set_update_status(t)))

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

    def _set_update_status(self, text: str) -> None:
        """更新状态写到设置页（页面可能还没构建）。"""
        for name in ("update_status",):
            label = getattr(self, name, None)
            if label is not None and not sip.isdeleted(label):
                label.setText(text)

    def _finish_update_check(self) -> None:
        self._update_running = False
        if hasattr(self, "update_button"):
            self.update_button.setEnabled(True)

    def _trigger_easter_egg(self) -> None:
        """彩蛋：进入「点哪掉哪」模式，页面保持原样。重复点击不会叠加。"""
        if getattr(self, "_falling_overlay", None) is not None:
            return
        self._falling_overlay = FallingOverlay(self)

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
        inset = getattr(self, "_shell_inset", 0)
        right = width - inset
        bottom = max(0, height - inset - u(0.4))
        if hasattr(self, "version_label"):
            # PCL 标题栏不显示版本号，隐藏右下角的版本浮标
            self.version_label.setVisible(not self._pcl())
            self.version_label.adjustSize()
            self.version_label.move(
                max(0, right - self.version_label.width() - u(1.25)),
                max(0, bottom - self.version_label.height()),
            )
            self.version_label.raise_()

        if hasattr(self, "task_panel") and self.task_panel.isVisible():
            hint = self.task_panel.sizeHint()
            panel_w = min(
                max(hint.width(), u(30)),
                max(u(20), width - inset * 2 - u(4)),
            )
            panel_h = max(hint.height(), u(5))
            stack_bottom = bottom - self.version_label.height() - u(0.75)
            self.task_panel.setGeometry(
                max(0, right - panel_w - u(1.25)),
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
        # 窗口还没显示时不做淡入：否则首帧会以 opacity=0 映射到屏幕上，
        # 表现为启动瞬间内容区是一块白色空白。
        if self.isVisible():
            fade_in(page)
        self._update_topbar(name)
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
        if key == "server":
            self._refresh_server_tab()
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
        if instance.root:
            mc_dir = Path(instance.root)
        isolated = bool(self.config.isolation) or read_pack_metadata(mc_dir, instance.id) is not None
        return instance_game_dir(mc_dir, instance.id, isolated)

    def selected_instance(self) -> Instance | None:
        return self.selected

    def refresh_instances(self) -> None:
        def worker() -> None:
            root = self.game_dir()
            try:
                found = [replace(item, root=str(root)) for item in discover_instances(root)]
            except Exception as exc:  # noqa: BLE001
                self.post_log("ERROR", f"读取实例失败：{exc}")
                return
            # 先显示主目录的版本，全盘扫描的结果稍后再追加，避免启动时空白等待
            self.events.put(("instances", list(found)))
            # 本机其他目录里的版本：只放在下拉框下半部分，失败不影响主目录
            # 全盘扫描较慢，5 分钟内复用上次结果；手动「刷新版本列表」会清缓存。
            if not getattr(self.config, "scan_other_versions", True):
                return
            cached = getattr(self, "_scan_cache", None)
            if cached is not None and cached[0] == str(root) and time.monotonic() - cached[1] < 300:
                others = list(cached[2])
            else:
                try:
                    others = scan_minecraft_dirs(exclude=[root])
                except Exception as exc:  # noqa: BLE001
                    self.post_log("WARN", f"扫描其他版本失败：{exc}")
                    others = []
                self._scan_cache = (str(root), time.monotonic(), list(others))
            seen = {str(root)}
            for other in others:
                key = str(other)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    extra = discover_instances(other)
                except Exception:  # noqa: BLE001
                    continue
                found.extend(replace(item, root=key) for item in extra)
            self.events.put(("instances", found))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_instances(self, instances: list[Instance]) -> None:
        previous = self.selected.id if self.selected is not None else (self.config.last_version or "")
        previous_root = self.selected.root if self.selected is not None else ""
        self.instances = list(instances)
        home = Path(self.game_dir())
        local = [item for item in self.instances if not item.root or Path(item.root) == home]
        others = [item for item in self.instances if item not in local]
        self.version_combo.blockSignals(True)
        self.version_combo.clear()

        def add_item(item: Instance, *, secondary: bool) -> None:
            loader = item.loader or "原版"
            # 名称超过 10 个字符就省略，配合 AdjustToContents 让下拉框自适应宽度
            name = item.id if len(item.id) <= 10 else item.id[:10] + "…"
            label = name if loader in ("原版", "未知") else f"{name} · {loader}"
            if secondary:
                label += f" · {Path(item.root).name or item.root}"
            self.version_combo.addItem(QIcon(block_pixmap(item.id, 22, item.loader)), label, item)
            index = self.version_combo.count() - 1
            self.version_combo.setItemData(
                index,
                item.broken or f"{item.id} · {loader}\n{Path(item.root) if item.root else ''}".strip(),
                Qt.ItemDataRole.ToolTipRole,
            )

        for item in local:
            add_item(item, secondary=False)
        if others and local:
            # 只加一条细分割线，不做显眼的分组标题
            self.version_combo.insertSeparator(self.version_combo.count())
            for item in others:
                add_item(item, secondary=True)
        elif others:
            for item in others:
                add_item(item, secondary=True)

        target = -1
        for index in range(self.version_combo.count()):
            item = self.version_combo.itemData(index)
            if isinstance(item, Instance) and item.id == previous and item.root == previous_root:
                target = index
                break
        if target < 0:
            for index in range(self.version_combo.count()):
                item = self.version_combo.itemData(index)
                if isinstance(item, Instance) and item.id == previous:
                    target = index
                    break
        if target < 0:
            target = next(
                (
                    index
                    for index in range(self.version_combo.count())
                    if isinstance(self.version_combo.itemData(index), Instance)
                ),
                -1,
            )
        if target >= 0:
            self.version_combo.setCurrentIndex(target)
        self.version_combo.blockSignals(False)

        current = self.version_combo.currentData() if target >= 0 else None
        self.selected = current if isinstance(current, Instance) else None
        self._update_hero()

        total = sum(item.size_bytes for item in self.instances)
        if self.instances:
            extra = f" · 其他目录 {len(others)} 个" if others else ""
            self.status(f"{len(self.instances)} 个实例 · {human_size(total)}{extra}")
        self._render_versions()

    def _on_version_selected(self, index: int) -> None:
        instance = self.version_combo.itemData(index) if index >= 0 else None
        if not isinstance(instance, Instance):
            return
        self.selected = instance
        self.config.last_version = instance.id
        self._update_hero()

    def _update_hero(self) -> None:
        """把当前实例的信息写到启动页（提示、目录、模组 / 存档列表）。"""
        instance = self.selected
        if instance is None:
            self.home_hint.setText("还没有可用的游戏版本，去「下载」页安装一个吧")
            self._set_path_label(str(self.game_dir()))
            self.launch_button.setEnabled(False)
            self._refresh_home_lists()
            return
        parts = [f"MC {instance.mc_version}", human_size(instance.size_bytes)]
        loader = instance.loader or "原版"
        if loader not in ("原版", "未知"):
            parts.insert(1, loader)
        mc_dir = Path(instance.root) if instance.root else self.game_dir()
        pack = read_pack_metadata(mc_dir, instance.id)
        if pack and pack.get("name"):
            parts.insert(0, f"整合包 {pack.get('name')}")
        if instance.broken:
            parts.append("版本 JSON 损坏")
        # PCL 的启动按钮里只写版本名，默认风格才展示详细信息
        self.home_hint.setText(instance.id if self._pcl() else "  ·  ".join(parts))
        self._set_path_label(str(self.game_dir_for(instance)))
        # 原版实例没有模组加载器，模组分类直接隐藏
        has_loader = loader not in ("原版", "未知", "")
        mods_button = getattr(self, "resource_buttons", {}).get("mods")
        stack = getattr(self, "resource_stack", None)
        if (
            self._resource_widgets_alive()
            and mods_button is not None
            and not sip.isdeleted(mods_button)
            and stack is not None
            and not sip.isdeleted(stack)
        ):
            mods_button.setVisible(has_loader)
            if not has_loader and stack.currentIndex() == 0:
                stack.setCurrentIndex(1)
                self.resource_buttons["saves"].setChecked(True)
        self.launch_button.setEnabled(True)
        self._refresh_home_lists()

    def _set_path_label(self, text: str) -> None:
        """默认风格启动页有目录路径标签；PCL 风格没有，写入时直接跳过。"""
        label = getattr(self, "path_label", None)
        if label is not None and not sip.isdeleted(label):
            label.setText(text)

    def _on_memory_changed(self, value: int) -> None:
        self.config.memory_mb = int(value)
        for label in (
            getattr(self, "memory_value", None),
            getattr(self, "settings_memory_value", None),
            getattr(self, "instance_memory_value", None),
        ):
            if label is not None:
                label.setText(f"{value} MB")
        for slider in (
            getattr(self, "home_memory", None),
            getattr(self, "settings_memory", None),
            getattr(self, "instance_memory", None),
        ):
            if slider is not None and slider.value() != int(value):
                slider.blockSignals(True)
                slider.setValue(int(value))
                slider.blockSignals(False)

    def _instance_menu(self, global_pos) -> None:
        """版本下拉框右侧的「⋯」菜单：版本设置 / 加载器 / 导入导出 / 目录 / 删除。"""
        instance = self.selected
        menu = QMenu(self)
        settings = menu.addAction("版本设置（页内）")
        add_loader = menu.addAction("安装加载器…")
        menu.addSeparator()
        export_zip = menu.addAction("导出版本（ZIP）…")
        import_zip = menu.addAction("导入版本（ZIP）…")
        menu.addSeparator()
        open_game = menu.addAction("打开游戏目录")
        open_version = menu.addAction("打开版本目录")
        menu.addSeparator()
        refresh = menu.addAction("刷新版本列表")
        remove = menu.addAction("删除实例")
        for action in (settings, add_loader, open_game, open_version, remove, export_zip):
            action.setEnabled(instance is not None)
        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        if chosen is refresh:
            self._scan_cache = None
            self.refresh_instances()
        elif chosen is remove and instance is not None:
            self._delete_instance(instance)
        elif chosen is settings:
            self._open_instance_page()
        elif chosen is add_loader and instance is not None:
            self._install_loader_on(instance)
        elif chosen is export_zip and instance is not None:
            self._export_instance(instance)
        elif chosen is import_zip:
            self._import_version()
        elif chosen is open_game:
            _reveal(self.game_dir_for(instance))
        elif chosen is open_version and instance is not None:
            _reveal(Path(instance.root or self.game_dir()) / "versions" / instance.id)

    def _delete_instance(self, instance: Instance) -> None:
        answer = QMessageBox.question(
            self, APP_NAME, f"确定删除实例 {instance.id}？此操作不可恢复。"
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        root = Path(instance.root) if instance.root else self.game_dir()
        shutil.rmtree(root / "versions" / instance.id, ignore_errors=True)
        self.notice(f"已删除实例 {instance.id}", "WARN")
        self.refresh_instances()

    # --------------------------------------------------------- 隐私 / 导入导出
    def _ensure_privacy(self) -> bool:
        """首次下载前必须同意当前版本的隐私政策。"""
        if bool(getattr(self.config, "privacy_accepted", False)) and str(
            getattr(self.config, "privacy_version", "")
        ) == PRIVACY_VERSION:
            return True
        dialog = PrivacyDialog(self, require_accept=True)
        dialog.exec()
        if not dialog.accepted:
            self.notice("需要同意隐私政策后才能下载", "WARN")
            return False
        self.config.privacy_accepted = True
        self.config.privacy_version = PRIVACY_VERSION
        save_config(self.config, self.data_dir)
        return True

    def _export_instance(self, instance: Instance) -> None:
        suffix = f"{safe_instance_id(instance.id) or instance.id}.zip"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出版本（ZIP）", str(Path.home() / suffix), "ZIP 压缩包 (*.zip)"
        )
        if not path:
            return
        root = Path(instance.root) if instance.root else self.game_dir()
        version_dir = root / "versions" / instance.id

        def worker(cancel: threading.Event) -> str:
            export_version(version_dir, path)
            return f"已导出版本 {instance.id} → {path}"

        self.run_task(f"导出版本 {instance.id}", worker)

    def _import_version(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "导入版本（ZIP）", str(Path.home()), "ZIP 压缩包 (*.zip)"
        )
        if not path:
            return
        target_root = (
            Path(self.selected.root)
            if self.selected is not None and self.selected.root
            else self.game_dir()
        )

        def worker(cancel: threading.Event) -> str:
            version_id = import_version(target_root, path)
            return f"已导入版本 {version_id} → {target_root}"

        self.run_task("导入版本", worker)

    def _export_save(self, save_dir: Path) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出存档（ZIP）", str(Path.home() / f"{save_dir.name}.zip"), "ZIP 压缩包 (*.zip)"
        )
        if not path:
            return

        def worker(cancel: threading.Event) -> str:
            export_save(save_dir, path)
            return f"已导出存档 {save_dir.name} → {path}"

        self.run_task(f"导出存档 {save_dir.name}", worker)

    def _import_save(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "导入存档（ZIP）", str(Path.home()), "ZIP 压缩包 (*.zip)"
        )
        if not path:
            return
        instance = self.selected
        if instance is None:
            self.notice("请先选择一个版本", "WARN")
            return
        base = self.game_dir_for(instance)

        def worker(cancel: threading.Event) -> str:
            name = import_save(base, path)
            return f"已导入存档 {name}"

        self.run_task("导入存档", worker)

    # ------------------------------------------------------------- 右键菜单
    def _mods_menu(self, widget: QListWidget, pos) -> None:
        item = widget.itemAt(pos)
        if item is None:
            return
        raw = item.data(Qt.ItemDataRole.UserRole)
        if not raw:
            return
        path = Path(str(raw))
        disabled = path.name.lower().endswith(".disabled")
        menu = QMenu(self)
        action = menu.addAction("启用此模组" if disabled else "禁用此模组")
        action.setToolTip("禁用只是把文件名加上 .disabled，游戏不会加载它")
        move = menu.addAction("移动到加载目录")
        move.setToolTip("把模组从其他目录移到当前版本的 mods 目录")
        chosen = menu.exec(widget.mapToGlobal(pos))
        if chosen is action:
            self._toggle_mod(path)
        elif chosen is move:
            self._move_resource(path, "mods")

    def _toggle_mod(self, path: Path) -> None:
        disabled = path.name.lower().endswith(".disabled")
        if disabled:
            target = path.with_name(path.name[: -len(".disabled")])
        else:
            target = path.with_name(path.name + ".disabled")
        try:
            if target.exists():
                self.notice(f"目标文件已存在：{target.name}", "WARN")
                return
            path.rename(target)
        except OSError as exc:
            self.notice(f"修改模组失败：{exc}", "ERROR")
            return
        self.notice(f"已{'启用' if disabled else '禁用'}模组 {target.name}")
        self._refresh_home_lists()

    def _packs_menu(self, widget: QListWidget, kind: str, pos) -> None:
        """资源包 / 光影：禁用（改后缀并从 options.txt 移除）与移动到加载目录。"""
        item = widget.itemAt(pos)
        if item is None:
            return
        raw = item.data(Qt.ItemDataRole.UserRole)
        if not raw:
            return
        path = Path(str(raw))
        disabled = path.name.lower().endswith(".disabled")
        menu = QMenu(self)
        action = menu.addAction("启用此资源包" if disabled else "禁用此资源包")
        action.setToolTip("禁用会改名为 .disabled，并从 options.txt 的 resourcePacks 里移除")
        move = menu.addAction("移动到加载目录")
        move.setToolTip(f"把文件移到当前版本的 {kind} 目录")
        menu.addSeparator()
        reveal = menu.addAction("打开所在目录")
        chosen = menu.exec(widget.mapToGlobal(pos))
        if chosen is action:
            self._toggle_pack(path, kind)
        elif chosen is move:
            self._move_resource(path, kind)
        elif chosen is reveal:
            _reveal(path.parent)

    def _toggle_pack(self, path: Path, kind: str) -> None:
        disabled = path.name.lower().endswith(".disabled")
        if disabled:
            target = path.with_name(path.name[: -len(".disabled")])
        else:
            target = path.with_name(path.name + ".disabled")
        try:
            if target.exists():
                self.notice(f"目标文件已存在：{target.name}", "WARN")
                return
            path.rename(target)
            game = self.game_dir_for(self.selected)
            if kind == "resourcepacks" and path.parent.parent == game:
                # 禁用的包要同步移出 options.txt，重新启用时再加回去
                if disabled:
                    set_resource_pack_enabled(game, target.name, True)
                else:
                    set_resource_pack_enabled(game, path.name, False)
        except OSError as exc:
            self.notice(f"修改资源失败：{exc}", "ERROR")
            return
        self.notice(f"已{'启用' if disabled else '禁用'} {target.name}")
        self._refresh_home_lists()

    def _move_resource(self, path: Path, kind: str) -> None:
        """把放错目录的资源移到当前版本的加载目录。"""
        instance = self.selected
        base = self.game_dir_for(instance) if instance is not None else self.game_dir()
        target = base / kind / path.name
        if target == path:
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                self.notice(f"加载目录已有同名文件：{path.name}", "WARN")
                return
            shutil.move(str(path), str(target))
        except OSError as exc:
            self.notice(f"移动失败：{exc}", "ERROR")
            return
        if kind == "resourcepacks":
            try:
                add_resource_packs(base, [target])
            except OSError:
                pass
        self.notice(f"已移动到加载目录：{path.name}")
        self._refresh_home_lists()

    def _saves_menu(self, widget: QListWidget, pos) -> None:
        item = widget.itemAt(pos)
        if item is None:
            return
        raw = item.data(Qt.ItemDataRole.UserRole)
        if not raw:
            return
        folder = Path(str(raw))
        menu = QMenu(self)
        export = menu.addAction("导出存档（ZIP）…")
        chosen = menu.exec(widget.mapToGlobal(pos))
        if chosen is export:
            self._export_save(folder)

    def _account_caption(self) -> str:
        """启动页账户标签：离线 / 正版（未登录时也明确提示）。"""
        if self.config.account_provider == "microsoft":
            session = load_session(self.data_dir)
            return f"正版账户 · {session.name}" if session else "正版账户（未登录）"
        return "离线账户"

    def _account_display_name(self) -> str:
        if self.config.account_provider == "microsoft":
            session = load_session(self.data_dir)
            return session.name if session else "未登录"
        return self.config.username

    def _refresh_account_ui(self) -> None:
        """登录 / 退出 / 改名后同步启动页与设置页的账户信息。"""
        name = self._account_display_name()
        online = self.config.account_provider == "microsoft" and load_session(self.data_dir) is not None
        if hasattr(self, "account_name"):
            self.account_name.setText(name)
            self.account_name.setReadOnly(online)
            self.account_name.setToolTip(
                "正版账户名由 Microsoft 提供，点下方「账户」按钮可切换账户"
                if online
                else "离线账户名：点击即可修改，回车生效"
            )
        if hasattr(self, "account_switch"):
            self.account_switch.setText(self._account_caption())
        if hasattr(self, "login_type_label"):
            self.login_type_label.setText("正版登录" if online else "离线登录")
        if hasattr(self, "avatar"):
            self.avatar.setPixmap(self._avatar_pixmap(name, self._avatar_px()))

    def _open_account_dialog(self) -> None:
        """账户中心：离线改名 / 正版登录 / 退出登录。"""
        previous = self._account_caption()
        dialog = AccountDialog(self, self.config, self.data_dir)
        # 持有引用：登录线程可能还在收尾，避免对话框对象提前释放
        self._account_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._account_dialog = None
        self._refresh_account_ui()
        current = self._account_caption()
        if current != previous:
            self.notice(f"当前账户：{current}")

    def _apply_account_name(self) -> None:
        """名称框回车：校验后保存并刷新头像。正版账户名不可改。"""
        if self.config.account_provider == "microsoft":
            self.account_name.setText(self._account_display_name())
            return
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
        self.avatar.setPixmap(self._avatar_pixmap(name, self._avatar_px()))
        self.notice(f"已切换离线账户：{name}")

    def _edit_account(self) -> None:
        """兼容旧入口：直接聚焦名称框改名。"""
        self.account_name.setFocus()
        self.account_name.selectAll()

    # ------------------------------------------------------------- 启动
    def _report_resource_audit(
        self, mc_dir: Path, instance_id: str, isolated: bool
    ) -> None:
        """启动前检查：本次要加载的资源是否都在加载目录里。"""
        try:
            report = audit_resources(mc_dir, instance_id, isolated=isolated)
        except OSError as exc:
            self.post_log("WARN", f"资源检查失败（{exc}）")
            return
        for entry in report.misplaced:
            self.post_log(
                "WARN",
                f"{KIND_NAMES[entry.kind]} {entry.path.name} 不在加载目录"
                f"（{report.load_dir / entry.kind}），本次不会加载",
            )
        for name in report.missing:
            self.post_log("WARN", f"资源包 {name} 已启用但文件缺失，将在游戏内显示为不可用")
        if report.problem_count:
            self.post_log(
                "INFO", f"资源检查：{report.problem_count} 项异常，可在启动页右键禁用或移动"
            )
        else:
            self.post_log("INFO", f"资源检查：全部位于 {report.load_dir}")

    def _prepare_custom_skin(
        self,
        instance: Instance,
        mc_dir: Path,
        isolated: bool,
        cancel: threading.Event,
    ) -> None:
        """启动前确保 CustomSkinLoader 与配置就位，否则上传的皮肤不会生效。"""
        if not bool(getattr(self.config, "skin_enabled", True)):
            return
        try:
            downloader = self.make_downloader(progress=False, workers=2)
            mirror = self.source(downloader)
            status = ensure_skin_mod(
                mc_dir,
                instance,
                downloader=downloader,
                mirror=mirror,
                cancel=cancel,
                log=self.post_log,
                isolated=isolated,
            )
        except Exception as exc:  # noqa: BLE001 - 皮肤失败不影响启动
            self.post_log("WARN", f"皮肤：检查 CustomSkinLoader 失败（{exc}）")
            return
        if status.ready:
            self.post_log("INFO", f"皮肤：{status.note or 'CustomSkinLoader 已就绪'}")
        elif status.state in ("no_base", "missing_loader"):
            self.post_log("INFO", f"皮肤：{status.note}")
        elif status.note:
            self.post_log("WARN", f"皮肤：{status.note}")

    # ------------------------------------------------------------- 启动中页面
    def _build_launching_page(self) -> QWidget:
        """启动中页面：点了启动但游戏窗口还没出现时显示。"""
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(SIDE_PAD, u(1), SIDE_PAD, u(2))
        outer.setSpacing(u(0.75))
        outer.addStretch(1)

        card = Card(margins=(u(2), u(2), u(2), u(2)))
        card.setMaximumWidth(u(34))
        ring_row = QHBoxLayout()
        ring_row.addStretch(1)
        self.launch_spinner = Spinner(44)
        ring_row.addWidget(self.launch_spinner)
        ring_row.addStretch(1)
        card.add_layout(ring_row)

        self.launch_phase = self._label("正在启动 Minecraft…", "h2")
        self.launch_phase.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.launch_phase.setWordWrap(True)
        card.add(self.launch_phase)

        self.launch_detail = self._label("正在准备启动参数与资源文件，游戏窗口出现后会自动回到启动页。")
        self.launch_detail.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.launch_detail.setWordWrap(True)
        card.add(self.launch_detail)

        cancel_row = QHBoxLayout()
        cancel_row.addStretch(1)
        cancel = QPushButton("取消启动")
        cancel.clicked.connect(self._cancel_launching)
        cancel_row.addWidget(cancel)
        self.launching_cancel = cancel
        cancel_row.addStretch(1)
        card.add_layout(cancel_row)

        outer.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(1)
        return page

    def _open_launching_page(self) -> None:
        if "launching" not in self.pages:
            self.pages["launching"] = self._build_launching_page()
            self.stack.addWidget(self.pages["launching"])
        self._sub_parent["launching"] = "home"
        self.launch_phase.setText("正在启动 Minecraft…")
        self.launch_detail.setText("正在准备启动参数与资源文件，游戏窗口出现后会自动回到启动页。")
        self.launching_cancel.setEnabled(True)
        self.launching_cancel.setText("取消启动")
        self.launch_spinner.start()
        self.show_page("launching")

    def _cancel_launching(self) -> None:
        self.launching_cancel.setEnabled(False)
        self.launching_cancel.setText("正在取消…")
        self.stop_game()

    def _finish_launching(self) -> None:
        page = self.pages.get("launching")
        if page is not None and self.stack.currentWidget() is page:
            self.show_page("home")

    def _launch_or_stop(self) -> None:
        if self.process is not None and self.process.running:
            self.stop_game()
            return
        if self.selected is None:
            self.notice("请先选择一个版本", "WARN")
            return
        self._open_launching_page()
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
            mc_dir = Path(instance.root) if instance.root else game_dir
            isolated = bool(self.config.isolation) or read_pack_metadata(mc_dir, instance.id) is not None
            self._report_resource_audit(mc_dir, instance.id, isolated)
            self._prepare_custom_skin(instance, mc_dir, isolated, cancel)
            try:
                enabled = enable_all_resource_packs(target_dir)
                if enabled:
                    self.post_log("INFO", f"资源包：已自动启用 {enabled} 个（options.txt 原先未设置）")
            except OSError as exc:
                self.post_log("WARN", f"资源包：写入 options.txt 失败（{exc}）")
            t_load = time.perf_counter()
            version = load_version(mc_dir, instance.id)
            profile = get_provider(self.config).acquire(self.config, self.data_dir)
            load_ms = (time.perf_counter() - t_load) * 1000
            plan = self._build_command_with_repair(
                version, mc_dir, profile, options, cancel
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
        stagger(list(self.install_page.rows.values()))
        self.tab_buttons["game"].setChecked(True)

    def close_install_page(self) -> None:
        self.switch_tab("game")

    def start_install(
        self,
        summary: VersionSummary,
        loader_key: str = "vanilla",
        loader_version: str = "",
        dir_name: str = "",
        install_dir: str = "",
    ) -> None:
        """安装页点「安装」后的后台任务：原版 / 原版 + 加载器，最后可选改名。"""
        manifest = self.manifest
        if manifest is None:
            self.notice("版本清单尚未就绪，请稍后再试", "WARN")
            return
        if self.cancel_event is not None and not self.cancel_event.is_set():
            self.notice("已有任务在进行，请等待完成或取消", "WARN")
            return
        if not self._ensure_privacy():
            return
        # 允许安装到任意目录；成功后切到这个目录，首页版本列表会跟着变。
        root = Path(install_dir) if install_dir else self.game_dir()

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
                root, summary.id, manifest, mirror, downloader, options=options
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
                    root,
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
                version_id, note = rename_instance(root, version_id, wanted)
                if note:
                    self.post_log("WARN", note)
            self.config.last_version = version_id
            if str(root) != str(self.game_dir()):
                self.config.game_dir = str(root)
                self.post_log("INFO", f"安装目录已切换为 {root}")
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
        if not self._ensure_privacy():
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
        root = Path(instance.root) if instance.root else self.game_dir()

        def worker(cancel: threading.Event) -> str:
            downloader = self.make_downloader()
            mirror = self.source(downloader)
            manifest = self.ensure_manifest(cancel)
            self.set_phase(f"安装 {label} {version}")
            version_id = install_loader(
                root,
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
        if not self._ensure_privacy():
            return
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
            if kind == "resourcepack":
                # 只下载不启用的话游戏里不会加载，这里直接写进 options.txt。
                added = add_resource_packs(target.path, result.files)
                if added:
                    self.post_log("INFO", f"已在 options.txt 启用 {added} 个资源包")
            names = "、".join(path.name for path in result.files[:3])
            more = f" 等 {len(result.files)} 个文件" if len(result.files) > 3 else ""
            return f"已安装到 {label}/{dest.name}：{names}{more}"

        self.run_task(f"安装 {hit.title}", worker)

    def install_modpack(self, hit: ProjectHit, version: ProjectVersion, name: str) -> None:
        if not self._ensure_privacy():
            return
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
        self._refresh_server_tab()
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
            self.status_label.setText(error if len(error) <= 140 else error[:137] + "…")
            if hasattr(self, "launch_phase") and not self.launch_button.text().startswith("结束"):
                self._finish_launching()
        else:
            if message:
                self._append_log("INFO", message)
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
                    if hasattr(self, "launch_phase"):
                        self.launch_phase.setText(str(payload))
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
                    if hasattr(self, "launch_detail"):
                        self.launch_detail.setText("游戏已启动，正在等待窗口出现…")
                    QTimer.singleShot(2500, self._finish_launching)
                elif kind == "launch_done":
                    self.process = None
                    self.launch_button.setText("启动游戏")
                    self.launch_button.setIcon(QIcon(icon_pixmap("play", 20, "#FFFFFF")))
                    self.status_label.setText("游戏已退出")
                    self._finish_launching()
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

    def _open_instance_page(self) -> None:
        if self.selected is None:
            self.notice("请先选择一个版本", "WARN")
            return
        self._show_sub_page("instance", self._build_instance_page)

    def _open_account_page(self) -> None:
        """账户设置：不再弹窗，改成整页并带返回键。"""
        def build() -> QWidget:
            page = QWidget()
            root = QVBoxLayout(page)
            root.setContentsMargins(SIDE_PAD, u(0.75), SIDE_PAD, u(1.5))
            root.setSpacing(u(0.75))
            dialog = AccountDialog(self, self.config, self.data_dir)
            dialog.setWindowFlags(Qt.WindowType.Widget)
            self._account_dialog = dialog
            dialog.finished.connect(lambda _result: self._close_account_page())
            root.addWidget(dialog, 1)
            return page

        self._show_sub_page("account", build)

    def _close_account_page(self) -> None:
        self._refresh_account_ui()
        self.show_page("home")
        self.notice(f"当前账户：{self._account_caption()}")

    def _export_launch_script(self, instance: Instance) -> None:
        """把当前版本的启动命令导出成 .bat，双击即可脱离启动器运行。"""
        suggested = f"{safe_filename(instance.id)}.bat"
        target, _filter = QFileDialog.getSaveFileName(
            self, "导出启动脚本", suggested, "批处理脚本 (*.bat)"
        )
        if not target:
            return
        root = Path(instance.root) if instance.root else self.game_dir()
        game_dir = self.game_dir_for(instance)
        options = LaunchOptions(
            memory_mb=self.config.memory_mb,
            width=self.config.width,
            height=self.config.height,
            fullscreen=self.config.fullscreen,
            java_path=self.config.java_overrides.get(instance.id)
            or self.config.java_overrides.get("__global__"),
            extra_jvm=list(self.config.extra_jvm),
            game_dir=str(game_dir),
        )

        def worker(cancel: threading.Event) -> str:
            version = load_version(root, instance.id)
            profile = get_provider(self.config).acquire(self.config, self.data_dir)
            plan = build_command(version, root, profile, options=options)
            lines = [
                "@echo off",
                "chcp 65001 >nul",
                f'cd /d "{plan.cwd}"',
                " ".join(_bat_quote(part) for part in plan.argv),
                "",
            ]
            Path(target).write_text("\r\n".join(lines), encoding="utf-8")
            return f"已导出启动脚本：{target}"

        self.run_task(f"导出 {instance.id} 启动脚本", worker)

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
        if hasattr(self, "settings_ui_style"):
            index = self.settings_ui_style.findData(getattr(self.config, "ui_style", "default"))
            self.settings_ui_style.blockSignals(True)
            self.settings_ui_style.setCurrentIndex(index if index >= 0 else 0)
            self.settings_ui_style.blockSignals(False)
        if hasattr(self, "settings_scan_other"):
            self.settings_scan_other.setChecked(
                bool(getattr(self.config, "scan_other_versions", True))
            )
        if hasattr(self, "settings_simple"):
            self.settings_simple.setChecked(bool(getattr(self.config, "simple_mode", False)))
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
        """完整保存：写配置后重新扫描版本 / 拉取清单（设置项已改为自动保存）。"""
        self._collect_settings()
        save_config(self.config, self.data_dir)
        self._apply_simple_mode()

        self._on_memory_changed(int(self.config.memory_mb))
        self._sync_jvm_edits()
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
            self._autosave_settings()
            self.refresh_instances()

    def _refresh_java_combo(self) -> None:
        combos = [
            combo
            for combo in (
                getattr(self, "settings_java", None),
                getattr(self, "instance_java", None),
            )
            if combo is not None
        ]
        if not combos:
            return
        candidates = list_java(self.game_dir())
        override = self.config.java_overrides.get("__global__", "")
        for combo in combos:
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
            if not candidates:
                combo.setToolTip("未检测到 Java，可点「重新检测」或到设置页手动添加")

    def _on_java_changed(self, index: int) -> None:
        if index < 0:
            return
        combo = self.sender()
        if not isinstance(combo, QComboBox):
            combo = getattr(self, "settings_java", None)
        if combo is None:
            return
        self._set_java_override(combo.itemData(index) or "")

    def _set_java_override(self, path: str) -> None:
        """全局 Java 选择：只影响启动命令，写回配置。"""
        if path:
            self.config.java_overrides["__global__"] = str(path)
        else:
            self.config.java_overrides.pop("__global__", None)
        save_config(self.config, self.data_dir)
        for combo in (
            getattr(self, "settings_java", None),
            getattr(self, "instance_java", None),
        ):
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
    app.setStyleSheet(full_qss(getattr(config, "ui_style", "default")))
    # 下拉框弹出层是独立的顶层窗口，只能靠全局事件过滤器去掉系统黑边
    app._combo_polisher = ComboPolisher(app)  # noqa: SLF001 - 保引用防回收
    app.installEventFilter(app._combo_polisher)
    # 按钮点击波纹 / 按压缩放由全局动效过滤器统一处理
    install_motion(app)
    window = MainWindow(config, data_dir)
    window.show()
    app.exec()
    # 先释放窗口再退出：动效用的 QGraphicsEffect 必须早于 QApplication 销毁
    window.close()
    window.deleteLater()
    app.processEvents()
