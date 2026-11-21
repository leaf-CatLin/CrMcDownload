"""内容下载页：模组 / 整合包 / 资源包 / 光影（数据来自 Modrinth）。"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from PyQt6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QSize,
    Qt,
    QTimer,
    QUrl,
)
from PyQt6.QtGui import QDesktopServices, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from offcraft.content.modrinth import ContentError, ProjectHit, ProjectVersion
from offcraft.content.target import (
    KIND_NAMES,
    Target,
    target_from_folder,
    target_from_instance,
    target_state,
)
from offcraft.instances import read_pack_metadata
from offcraft.ui.icons import icon_pixmap
from offcraft.ui.spinner import Spinner
from offcraft.ui.theme import ACCENT, MUTED, human_count, initial_pixmap
from offcraft.ui.widgets import (
    Badge,
    Card,
    ContentDelegate,
    ElidedLabel,
    ROW_H,
)

__all__ = ["ContentPage", "KINDS"]

KINDS = {
    "mod": ("模组", "为游戏添加内容，需选择 Fabric / Forge / NeoForge 等加载器。"),
    "modpack": ("整合包", "一键安装带加载器与配置的完整实例（.mrpack）。"),
    "resourcepack": ("资源包", "改变贴图、音效与界面风格。"),
    "shader": ("光影", "需要 Iris / OptiFine 等光影前置支持。"),
}

SORTS = [
    ("相关度", "relevance"),
    ("下载量", "downloads"),
    ("最近更新", "updated"),
    ("最新发布", "newest"),
]

# 下拉框末尾的哨兵项：选中它等于「打开资源管理器挑一个文件夹」
PICK_FOLDER = "__pick_folder__"

LOADER_CHOICES = [
    ("全部加载器", ""),
    ("Fabric", "fabric"),
    ("Forge", "forge"),
    ("NeoForge", "neoforge"),
    ("Quilt", "quilt"),
]

#: 缓存有效期 = 静默刷新周期（秒）：下载页每 100 秒重新拉取一次
CACHE_SECONDS = 100.0
#: 首屏条数：进入页面一次拉 60 条
PAGE_SIZE = 60
#: 自动追加条数：首屏加载完成后，每 AUTO_LOAD_SECONDS 秒往下补 30 条
AUTO_APPEND_SIZE = 30
#: 自动追加间隔（秒）
AUTO_LOAD_SECONDS = 5.0
#: 预取阈值：滚到已加载条数的这个比例时，自动在后台拉下一页
PREFETCH_RATIO = 0.5
#: 列表插入 / 退出动画时长（毫秒）
ANIM_MS = 100
#: 右侧详情卡片宽度与展开/收起动画时长（毫秒）
DETAIL_W = 292
DETAIL_MS = 180


class ContentPage(QWidget):
    def __init__(self, window: Any, kind: str) -> None:
        super().__init__()
        self.window = window
        self.kind = kind
        self.hits: list[ProjectHit] = []
        #: 每行的图标缓存：hit.id -> QPixmap（delegate 绘制时直接取，不再挂控件）
        self._icons: dict[str, QPixmap] = {}
        #: 已经发起过图标请求的 URL：避免同项目在多个分类/分页重复排队
        self._icon_requested: set[str] = set()
        self.total = 0
        self.offset = 0
        self.selected: ProjectHit | None = None
        self.versions: list[ProjectVersion] = []
        #: 手动挑选的文件夹目标（与实例目标一起组成下拉列表）
        self.extra_targets: list[Target] = []
        # 上一次有效的安装目标（取消选择文件夹时回退到这里）
        self._last_target_key = ""
        self._token = 0
        self._loaded = False
        #: 每个筛选条件组合的独立缓存：key -> (时间戳, 搜索结果)
        self._cache: dict[tuple[str, str, str, str], tuple[float, Any]] = {}
        #: 是否有请求正在进行（静默刷新遇到在途请求时跳过，避免相互取消）
        self._requesting = False
        #: 是否正在追加下一页（用于底部加载提示与防止重复请求）
        self._loading_more = False
        #: 定时自动追加（首屏加载完成后每 5s 补 30 条）
        self._auto_timer: QTimer | None = None
        #: 本轮 5s 窗口内用户是否自己触发过加载；是则跳过本轮自动追加
        self._auto_skip_round = False
        #: 用户触发加载的时间戳（用于和自动追加窗口做对比）
        self._user_load_at = 0.0
        #: 右侧详情卡片的展开/收起动画（默认收起，点击列表项才展开）
        self._detail_anim: QPropertyAnimation | None = None
        self._build()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        title, subtitle = KINDS[self.kind]
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        header = QHBoxLayout()
        head = QVBoxLayout()
        head.setSpacing(2)
        head.addWidget(self._label(title, "h2"))
        head.addWidget(self._label(subtitle, "muted"))
        header.addLayout(head, 1)
        self.mode_badge = Badge("Modrinth", "purple")
        header.addWidget(self.mode_badge)
        root.addLayout(header)

        filters = Card(margins=(14, 10, 14, 10))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText(f"搜索{title}……（回车搜索）")
        self.search.returnPressed.connect(self.reload)
        row.addWidget(self.search, 1)

        self.game_combo = QComboBox()
        self.game_combo.setMinimumWidth(128)
        self.game_combo.currentIndexChanged.connect(self.reload)
        row.addWidget(self.game_combo)

        self.loader_combo = QComboBox()
        self.loader_combo.setMinimumWidth(126)
        for label, value in LOADER_CHOICES:
            self.loader_combo.addItem(label, value)
        self.loader_combo.currentIndexChanged.connect(self.reload)
        # 只在需要隐藏时调用：控件此时还没加入布局，setVisible(True) 会让它
        # 以独立顶层窗口的身份闪一下。
        if self.kind not in ("mod", "modpack"):
            self.loader_combo.hide()
        row.addWidget(self.loader_combo)

        self.sort_combo = QComboBox()
        for label, value in SORTS:
            self.sort_combo.addItem(label, value)
        self.sort_combo.currentIndexChanged.connect(self.reload)
        row.addWidget(self.sort_combo)

        button = QPushButton("搜索")
        button.setObjectName("accent")
        button.setIcon(QIcon(icon_pixmap("search", 16, "#FFFFFF")))
        button.setIconSize(QSize(16, 16))
        button.clicked.connect(self.reload)
        row.addWidget(button)
        filters.add_layout(row)
        root.addWidget(filters)

        body = QHBoxLayout()
        body.setSpacing(12)

        results = Card(margins=(8, 8, 8, 8))
        # 加载动画（uiball ldrs ring 风格）：列表为空时占据列表位置
        self.loading_box = QWidget()
        loading_layout = QVBoxLayout(self.loading_box)
        loading_layout.setContentsMargins(0, 0, 0, 0)
        loading_layout.setSpacing(10)
        loading_layout.addStretch(1)
        self.spinner = Spinner(38, ACCENT)
        loading_layout.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignHCenter)
        self.loading_text = self._label("正在加载……", "muted")
        self.loading_text.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        loading_layout.addWidget(self.loading_text)
        loading_layout.addStretch(1)
        self.loading_box.setVisible(False)
        self.list = QListWidget()
        self.list.setObjectName("contentList")
        self.list.setSpacing(0)
        self.list.setUniformItemSizes(True)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setMouseTracking(True)
        # 行绘制交给 delegate：每行不再是一个 QWidget，滚动只花绘制时间
        self.list.setItemDelegate(ContentDelegate(self.window, self.list))
        self.list.setVisible(False)
        # 单击（而非程序化选中）才展开右侧详情
        self.list.itemClicked.connect(self._on_row_clicked)
        # 滚动到「已加载内容的一半」时自动预取下一页
        self.list.verticalScrollBar().valueChanged.connect(self._maybe_prefetch)
        results.add(self.loading_box, 1)
        results.add(self.list, 1)
        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.count_label = self._label("", "muted")
        footer.addWidget(self.count_label)
        footer.addStretch(1)
        # 底部「正在加载更多」：小 spinner + 文案，替代原来只有点击才生效的按钮
        self.more_spinner = Spinner(16, ACCENT)
        footer.addWidget(self.more_spinner)
        self.more_hint = self._label("", "muted")
        footer.addWidget(self.more_hint)
        self.more_button = QPushButton("重试")
        self.more_button.setObjectName("ghost")
        self.more_button.setFixedHeight(28)
        self.more_button.clicked.connect(lambda: self.load_more())
        self.more_button.setVisible(False)
        footer.addWidget(self.more_button)
        results.add_layout(footer)
        body.addWidget(results, 1)

        self.detail = Card(margins=(16, 16, 16, 16))
        # 默认收起：宽度动画到 0，点击列表项后再展开
        self.detail.setMinimumWidth(0)
        self.detail.setMaximumWidth(0)
        self.detail.setVisible(False)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.detail_icon = QLabel()
        self.detail_icon.setFixedSize(60, 60)
        self.detail_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self.detail_icon)
        names = QVBoxLayout()
        names.setSpacing(2)
        self.detail_title = ElidedLabel("选择左侧项目")
        self.detail_title.setProperty("role", "h3")
        names.addWidget(self.detail_title)
        self.detail_author = self._label("", "muted")
        names.addWidget(self.detail_author)
        top.addLayout(names, 1)
        self.detail_collapse = QPushButton()
        self.detail_collapse.setObjectName("iconBtn")
        self.detail_collapse.setFixedSize(26, 26)
        self.detail_collapse.setCursor(Qt.CursorShape.PointingHandCursor)
        self.detail_collapse.setIcon(QIcon(icon_pixmap("chevron-right", 15, MUTED)))
        self.detail_collapse.setIconSize(QSize(15, 15))
        self.detail_collapse.setToolTip("收起详情")
        self.detail_collapse.clicked.connect(self.collapse_detail)
        top.addWidget(self.detail_collapse)
        detail = self.detail
        detail.add_layout(top)

        self.detail_badges = QHBoxLayout()
        self.detail_badges.setSpacing(6)
        detail.add_layout(self.detail_badges)
        self.detail_desc = QLabel("")
        self.detail_desc.setWordWrap(True)
        self.detail_desc.setObjectName("muted")
        self.detail_desc.setMaximumHeight(96)
        detail.add(self.detail_desc)
        self.detail_stats = self._label("", "muted")
        detail.add(self.detail_stats)
        detail.body.addStretch(1)

        detail.add(self._label("版本", "muted"))
        self.version_combo = QComboBox()
        self.version_combo.currentIndexChanged.connect(self._on_version_changed)
        detail.add(self.version_combo)

        if self.kind == "modpack":
            detail.add(self._label("实例名称", "muted"))
            self.name_edit = QLineEdit()
            self.name_edit.setPlaceholderText("新实例名称")
            detail.add(self.name_edit)
        else:
            detail.add(self._label("安装到", "muted"))
            self.target_combo = QComboBox()
            self.target_combo.setToolTip(
                "选择已装版本（自动换算成它的游戏目录），"
                "或选「选择文件夹…」自己挑一个游戏文件夹"
            )
            self.target_combo.currentIndexChanged.connect(self._on_target_changed)
            detail.add(self.target_combo)
            self.target_path = self._label("", "mutedSmall")
            self.target_path.setWordWrap(True)
            detail.add(self.target_path)

        self.install_button = QPushButton("安装所选版本")
        self.install_button.setObjectName("accent")
        self.install_button.setFixedHeight(40)
        self.install_button.setIcon(QIcon(icon_pixmap("download", 16, "#FFFFFF")))
        self.install_button.setIconSize(QSize(16, 16))
        self.install_button.clicked.connect(self.install_selected)
        self.install_button.setEnabled(False)
        detail.add(self.install_button)

        self.open_button = QPushButton("在 Modrinth 查看")
        self.open_button.setIcon(QIcon(icon_pixmap("globe", 15, "#5B6678")))
        self.open_button.setIconSize(QSize(15, 15))
        self.open_button.clicked.connect(self._open_page)
        self.open_button.setEnabled(False)
        detail.add(self.open_button)
        self.detail_hint = self._label("", "mutedSmall")
        self.detail_hint.setWordWrap(True)
        detail.add(self.detail_hint)

        body.addWidget(self.detail)
        root.addLayout(body, 1)

    @staticmethod
    def _label(text: str, name: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName(name)
        return label

    # -------------------------------------------------------------- 详情卡片
    def _on_row_clicked(self, item: QListWidgetItem) -> None:
        """用户单击列表项：填充详情并展开右侧卡片。"""
        self._on_select(item)
        self.expand_detail()

    def expand_detail(self) -> None:
        if not self.detail.isVisible():
            self.detail.setMaximumWidth(0)
            self.detail.setVisible(True)
        if self.detail.maximumWidth() >= DETAIL_W and self._detail_anim is None:
            return
        self._animate_detail(DETAIL_W)

    def collapse_detail(self) -> None:
        if not self.detail.isVisible() or self._detail_anim is not None:
            return
        self._animate_detail(0)

    def _animate_detail(self, target: int) -> None:
        self._stop_detail_anim()
        anim = QPropertyAnimation(self.detail, b"maximumWidth", self)
        anim.setDuration(DETAIL_MS)
        anim.setStartValue(self.detail.maximumWidth())
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(lambda: self._detail_finished(target))
        self._detail_anim = anim
        anim.start()

    def _detail_finished(self, target: int) -> None:
        self._detail_anim = None
        if target <= 0:
            self.detail.setVisible(False)
            self.detail.setMaximumWidth(0)

    def _stop_detail_anim(self) -> None:
        if self._detail_anim is not None:
            self._detail_anim.stop()
            self._detail_anim = None

    def _set_loading(self, loading: bool, text: str = "正在加载……") -> None:
        """切换「加载动画 / 列表」两种状态（有旧结果时保持列表可见）。"""
        self.loading_text.setText(text)
        if loading:
            self.spinner.start()
        else:
            self.spinner.stop()
        self.loading_box.setVisible(loading)
        self.list.setVisible(not loading)

    # ----------------------------------------------------------------- data
    def refresh(self) -> None:
        self._loaded = True
        self._refresh_filters()
        self.reload()

    def ensure_loaded(self) -> None:
        """切到本页时调用：首次进入就直接拉列表 + 首个项目的版本。"""
        if not self._loaded:
            self.refresh()
            return
        self.sync_filters()
        if not self.hits:
            self.reload()
            return
        # 已有内容：超出缓存有效期就静默刷新（不重绘，完成后再做增删动画）
        cached = self._cache.get(self._cache_key())
        if cached is None or time.time() - cached[0] > CACHE_SECONDS:
            self.live_refresh()
        if self.selected is None and self.hits:
            self.list.setCurrentRow(0)
            item = self.list.item(0)
            if item is not None:
                self._on_select(item)
            return
        if self.selected is not None and not self.versions:
            self._load_versions(self.selected)

    def sync_filters(self) -> None:
        """只刷新筛选框（不触发新的网络请求）。"""
        self._refresh_filters()

    def suspend_auto_append(self) -> None:
        """页面被切走 / 窗口隐藏时暂停定时追加（不空跑网络）。"""
        self._stop_auto_append()

    def resume_auto_append(self) -> None:
        """页面重新显示时恢复定时追加（已到底或没有内容则保持停止）。"""
        if not self.hits or self.offset >= self.total:
            return
        self._start_auto_append()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        # 回到本页时恢复自动追加节奏（首屏还没跑完的话，_apply_result 里会启动）
        if self._loaded and self.hits:
            self.resume_auto_append()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().hideEvent(event)
        self.suspend_auto_append()

    def live_refresh(self) -> None:
        """100 秒静默刷新：请求完成前保持界面不变，完成后按差异做插入/退出动画。"""
        if not self._loaded or self._requesting:
            return
        query, game_version, loader, index = self._filters()
        key = self._cache_key()
        token = self._token
        self._requesting = True

        def worker() -> None:
            try:
                result = self.window.content_client().search(
                    query,
                    self.kind,
                    game_version=game_version,
                    loader=loader,
                    limit=20,
                    offset=0,
                    index=index,
                )
            except Exception:  # noqa: BLE001 - 静默刷新失败保持现状，不打扰浏览
                self.window.post(lambda: self._live_failed(token))
                return
            self.window.post(lambda: self._apply_result(token, result, False, key))

        threading.Thread(target=worker, daemon=True).start()

    def _live_failed(self, token: int) -> None:
        if token == self._token:
            self._requesting = False

    def _cache_key(self) -> tuple[str, str, str, str]:
        return self._filters()

    def _mc_dir(self) -> Path:
        return self.window.game_dir()

    def _refresh_filters(self, select_key: str = "") -> None:
        current_game = self.game_combo.currentData()
        current_loader = self.loader_combo.currentData()
        instances = list(self.window.instances)
        selected = self.window.selected_instance()

        self.game_combo.blockSignals(True)
        self.game_combo.clear()
        self.game_combo.addItem("全部版本", "")
        seen: set[str] = set()
        ordered: list[str] = []
        if selected is not None:
            ordered.append(selected.mc_version)
        for item in instances:
            if item.mc_version and item.mc_version not in ordered:
                ordered.append(item.mc_version)
        for version in ordered[:20]:
            if version and version not in seen:
                seen.add(version)
                self.game_combo.addItem(f"MC {version}", version)
        index = self.game_combo.findData(current_game)
        self.game_combo.setCurrentIndex(index if index >= 0 else (1 if selected is not None else 0))
        self.game_combo.blockSignals(False)

        self.loader_combo.blockSignals(True)
        index = self.loader_combo.findData(current_loader)
        if index < 0 and selected is not None:
            loader = selected.loader.lower()
            index = self.loader_combo.findData(loader)
        self.loader_combo.setCurrentIndex(index if index >= 0 else 0)
        self.loader_combo.blockSignals(False)

        if self.kind != "modpack":
            self._rebuild_targets(select_key)

        # 不要在这里调 content_client()：构造函数会创建 Downloader（载入系统证书库
        # ~22ms），在 GUI 线程上调用会让「打开下载页」明显卡一下。
        mode = getattr(self.window, "_content_mode", "")
        if mode:
            self.mode_badge.setText(f"Modrinth · {mode}")

    # ---------------------------------------------------------------- target
    def target_options(self) -> list[Target]:
        """已装版本 + 手动选的文件夹，按路径去重。"""
        mc_dir = self._mc_dir()
        options: list[Target] = []
        for item in list(self.window.instances):
            root = Path(item.root) if item.root else mc_dir
            isolated = bool(getattr(self.window.config, "isolation", False)) or (
                read_pack_metadata(root, item.id) is not None
            )
            options.append(target_from_instance(root, item, isolated=isolated))
        seen = {option.key for option in options}
        for extra in self.extra_targets:
            if extra.key not in seen:
                seen.add(extra.key)
                options.append(extra)
        return options

    @staticmethod
    def _target_text(target: Target) -> str:
        if target.source == "instance":
            return f"{target.instance_id} · {target.loader or '原版'}"
        name = Path(target.path).name or str(target.path)
        loader = target.loader or "未知版本"
        return f"{name} · 文件夹（{loader}）"

    def _rebuild_targets(self, select_key: str = "") -> None:
        options = self.target_options()
        current = self.target_combo.currentData()
        keep = select_key or (current.key if isinstance(current, Target) else "")
        if not keep:
            keep = self._last_target_key
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        for target in options:
            self.target_combo.addItem(self._target_text(target), target)
        index = -1
        if keep:
            for position, target in enumerate(options):
                if target.key == keep:
                    index = position
                    break
        if index < 0:
            selected = self.window.selected_instance()
            if selected is not None:
                for position, target in enumerate(options):
                    if target.instance_id == selected.id:
                        index = position
                        break
        if index < 0 and options:
            index = 0
        # 末尾固定放一个「选择文件夹…」，选中它就会打开资源管理器
        self.target_combo.addItem("选择文件夹…", PICK_FOLDER)
        self.target_combo.setCurrentIndex(index)
        self.target_combo.blockSignals(False)
        target = self.current_target()
        self._last_target_key = target.key if target is not None else ""
        self._refresh_install_state()

    def current_target(self) -> Target | None:
        target = self.target_combo.currentData()
        return target if isinstance(target, Target) else None

    def _last_target(self) -> Target | None:
        key = self._last_target_key
        if not key:
            return None
        for item in self.target_options():
            if item.key == key:
                return item
        return None

    def _restore_target(self) -> None:
        """取消文件选择时回退到上一次有效的安装目标。"""
        self.target_combo.blockSignals(True)
        index = -1
        key = self._last_target_key
        if key:
            for position in range(self.target_combo.count()):
                data = self.target_combo.itemData(position)
                if isinstance(data, Target) and data.key == key:
                    index = position
                    break
        if index < 0 and self.target_combo.count() > 1:
            index = 0
        self.target_combo.setCurrentIndex(index)
        self.target_combo.blockSignals(False)
        self._refresh_install_state()

    def pick_folder(self) -> None:
        """手动挑一个游戏文件夹作为安装目标；直接退出则回退到上一次的选择。"""
        start = str(self._mc_dir())
        target = self.current_target() or self._last_target()
        if target is not None and Path(target.path).is_dir():
            start = str(target.path)
        folder = QFileDialog.getExistingDirectory(
            self, f"选择要安装{KIND_NAMES.get(self.kind, '内容')}的游戏文件夹", start
        )
        if not folder:
            self._restore_target()
            return
        picked = target_from_folder(folder)
        if not any(item.key == picked.key for item in self.extra_targets):
            self.extra_targets.append(picked)
        self._rebuild_targets(select_key=picked.key)
        self._match_filters(picked)
        ok, reason = target_state(picked, self.kind)
        if not ok:
            self.window.notice(reason, "WARN")
        elif picked.mc_version:
            self.window.notice(
                f"安装目标：{picked.mc_version} · {picked.loader or '未知加载器'}，列表已按它筛选",
                "INFO",
            )

    def _match_filters(self, target: Target) -> None:
        """把筛选框对齐到目标，避免装错游戏版本 / 加载器的内容。"""
        changed = False
        index = self.game_combo.findData(target.mc_version) if target.mc_version else -1
        if index >= 0 and self.game_combo.currentIndex() != index:
            self.game_combo.setCurrentIndex(index)  # 信号会触发 reload
            changed = True
        if self.kind in ("mod", "modpack") and self.loader_combo.isVisible():
            loader = (target.loader or "").lower()
            lindex = self.loader_combo.findData(loader) if loader else -1
            if lindex >= 0 and self.loader_combo.currentIndex() != lindex:
                self.loader_combo.setCurrentIndex(lindex)
                changed = True
        if not changed:
            self.reload()

    def _filters(self) -> tuple[str, str, str, str]:
        return (
            self.search.text().strip(),
            str(self.game_combo.currentData() or ""),
            str(self.loader_combo.currentData() or ""),
            str(self.sort_combo.currentData() or "relevance"),
        )

    def reload(self) -> None:
        """筛选变化 / 手动搜索：优先用缓存；没有缓存时后台拉取，完成前不清空列表。"""
        self._token += 1
        self.offset = 0
        self._loading_more = False
        # 换筛选条件相当于重新开始一轮：先停掉旧的自动追加节奏，
        # 等新的首屏结果回来后再由 _apply_result 重新启动
        self._stop_auto_append()
        self._set_more_state("idle")
        key = self._cache_key()
        cached = self._cache.get(key)
        if cached is not None and time.time() - cached[0] <= CACHE_SECONDS:
            self._requesting = False
            self._apply_result(self._token, cached[1], False, key)
            return
        if not self.hits:
            self._set_loading(True, "正在搜索……")
        self._search(append=False)

    def load_more(self) -> None:
        """用户主动追加下一页（自动预取与「重试」按钮共用）。"""
        if self._requesting or self._loading_more or self.offset >= self.total:
            return
        # 用户自己触发了加载：本轮 5s 窗口内让位，不叠加自动追加
        self._mark_user_load()
        self._search(append=True)

    # -------------------------------------------------- 定时自动追加（每 5s 30 条）
    def _start_auto_append(self) -> None:
        """首屏加载完成后启动定时追加：每 5s 往列表底部补 ``AUTO_APPEND_SIZE`` 条。

        与「滚动到一半预取」是两条互补路径：用户不动鼠标时列表自己往下长，
        用户滚动时按需加载；两者由 ``_requesting`` / ``_loading_more`` 互斥，
        同一时刻只会有一个请求在飞。
        """
        if self._auto_timer is None:
            self._auto_timer = QTimer(self)
            self._auto_timer.setInterval(int(AUTO_LOAD_SECONDS * 1000))
            self._auto_timer.timeout.connect(self._auto_append_tick)
        self._auto_skip_round = False
        self._user_load_at = 0.0
        if not self._auto_timer.isActive():
            self._auto_timer.start()

    def _stop_auto_append(self) -> None:
        """暂停定时追加（切走页签 / 搜索条件变化 / 已到底时调用）。"""
        if self._auto_timer is not None and self._auto_timer.isActive():
            self._auto_timer.stop()

    def _auto_append_tick(self) -> None:
        """5 秒到点：没在请求、没到底、且本轮没有用户触发的加载，就追加 30 条。"""
        if self.offset >= self.total or not self.hits:
            # 已经到底 / 还没有内容：停表，省掉空转
            self._stop_auto_append()
            return
        if self._requesting or self._loading_more:
            # 上一轮还在飞：跳过本轮，不叠加请求
            self._auto_skip_round = False
            return
        if self._auto_skip_round:
            # 本轮窗口内用户自己加载过：让位，下一轮恢复
            self._auto_skip_round = False
            return
        self._search(append=True, size=AUTO_APPEND_SIZE)

    def _mark_user_load(self) -> None:
        """记录「用户主动触发了一次加载」：本轮 5s 窗口内不再自动追加。"""
        self._user_load_at = time.monotonic()
        self._auto_skip_round = True

    def _maybe_prefetch(self, _value: int = 0) -> None:
        """滚到已加载内容的一半时，在后台预取下一页，用户几乎无感。"""
        if not self.hits or self.offset >= self.total:
            return
        if self._requesting or self._loading_more:
            return
        bar = self.list.verticalScrollBar()
        maximum = bar.maximum()
        if maximum <= 0:
            return
        # 「已加载内容的一半」按滚动位置估计：滚动条过半即认为看掉一半
        if bar.value() < maximum * PREFETCH_RATIO:
            return
        # 滚动也是用户主动加载：标记让位，避免与自动追加抢同一轮
        self._mark_user_load()
        self._search(append=True)

    def _set_more_state(self, state: str) -> None:
        """底栏状态：``idle`` / ``loading`` / ``failed`` / ``done``。"""
        loading = state == "loading"
        failed = state == "failed"
        self.more_hint.setText(
            {
                "idle": "",
                "loading": "正在加载更多……",
                "failed": "加载失败",
                "done": "已经到底了",
            }.get(state, "")
        )
        if loading:
            self.more_spinner.start()
        else:
            self.more_spinner.stop()
        self.more_button.setVisible(failed)

    def _search(self, *, append: bool, size: int | None = None) -> None:
        """发起一次搜索/追加。

        ``size`` 缺省时：首屏用 ``PAGE_SIZE``(60)、追加用 ``PAGE_SIZE``；
        定时自动追加会显式传 ``AUTO_APPEND_SIZE``(30)。
        """
        query, game_version, loader, index = self._filters()
        key = self._cache_key()
        token = self._token = self._token + 1
        offset = self.offset
        limit = PAGE_SIZE if size is None else max(1, int(size))
        self._requesting = True
        if append:
            self._loading_more = True
            # 自动追加同样显示底栏「正在加载更多……」，让用户知道列表在自己变长
            self._set_more_state("loading")
        else:
            self.more_button.setEnabled(False)

        def worker() -> None:
            try:
                result = self.window.content_client().search(
                    query,
                    self.kind,
                    game_version=game_version,
                    loader=loader,
                    limit=limit,
                    offset=offset,
                    index=index,
                )
            except ContentError as exc:
                self.window.post(lambda text=str(exc): self._search_failed(token, text))
                return
            except Exception as exc:  # noqa: BLE001
                self.window.post(lambda text=f"搜索失败：{exc}": self._search_failed(token, text))
                return
            self.window.post(lambda: self._apply_result(token, result, append, key))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_result(self, token: int, result, append: bool, key) -> None:
        """把网络结果与当前列表做差异比对，只对新增/移除的行做 100ms 动画。"""
        if token != self._token:
            return
        self._requesting = False
        self._loading_more = False
        if not append:
            self._set_loading(False)
            # 首屏加载完成：开始「每 5s 自动追加 30 条」的节奏
            if result.hits:
                self._start_auto_append()
        self.total = result.total
        self.offset = result.offset + len(result.hits)
        if not append:
            self._cache[key] = (time.time(), result)
            if len(self._cache) > 24:
                # 独立缓存按筛选组合分开存放，超出上限时丢弃最久未更新的组合
                oldest = min(self._cache, key=lambda item: self._cache[item][0])
                self._cache.pop(oldest, None)

        old_hits = list(self.hits)
        if append:
            final_hits = old_hits + list(result.hits)
        else:
            final_hits = list(result.hits)

        old_ids = [hit.id for hit in old_hits]
        new_ids = [hit.id for hit in final_hits]
        # 静默刷新且顺序完全一致：只更新计数，不重绘列表
        if not append and old_ids == new_ids:
            self.selected = next(
                (hit for hit in final_hits if self.selected and hit.id == self.selected.id),
                self.selected,
            )
            self._update_count()
            self._requesting = False
            self.more_button.setEnabled(True)
            self.mode_badge.setText(f"Modrinth · {getattr(self.window, '_content_mode', '') or '镜像'}")
            return

        inserted = [index for index, hit in enumerate(final_hits) if hit.id not in old_ids]
        removed = [index for index, hit in enumerate(old_hits) if hit.id not in new_ids]

        self.hits = final_hits
        self._sync_rows(final_hits)
        self._update_count()
        self._requesting = False
        self._loading_more = False
        self.more_button.setEnabled(True)
        self._set_more_state("done" if self.offset >= self.total and self.hits else "idle")
        self.mode_badge.setText(f"Modrinth · {getattr(self.window, '_content_mode', '') or '镜像'}")

        selected_id = self.selected.id if self.selected is not None else ""
        keep = next(
            (index for index, hit in enumerate(final_hits) if hit.id == selected_id), -1
        )
        if keep >= 0:
            self.selected = final_hits[keep]
            self.list.blockSignals(True)
            self.list.setCurrentRow(keep)
            self.list.blockSignals(False)
        elif final_hits:
            self.selected = None
            self.list.blockSignals(True)
            self.list.setCurrentRow(0)
            self.list.blockSignals(False)
            self._on_select(self.list.item(0))
        else:
            self.selected = None

        # 只是「追加下一页」或「顺序没变」时不播动画：这两种情况占绝大多数，
        # 每帧都给整表做过渡反而更卡。
        if inserted and removed and not append:
            self._flash_rows(inserted)

    def _sync_rows(self, hits: list[ProjectHit]) -> None:
        """按 id 增量同步列表项：只增删差异项，不再 ``clear()`` 后重建整表。"""
        # 文本省略缓存按「宽度 + 原文」记账；列表内容或宽度变了就整体失效，
        # 否则会留下旧标题的省略结果（窗口 resize 时尤其明显）。
        delegate = self.list.itemDelegate()
        if hasattr(delegate, "clear_text_cache"):
            delegate.clear_text_cache()
        current = [
            self.list.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(self.list.count())
        ]
        current_ids = [hit.id if hit is not None else "" for hit in current]
        wanted = [hit.id for hit in hits]
        if current_ids == wanted:
            # 顺序完全一致：只把可能变化的字段写回（标签/下载量会随刷新变化）
            for index, hit in enumerate(hits):
                item = self.list.item(index)
                if item is not None:
                    item.setData(Qt.ItemDataRole.UserRole, hit)
            self._request_visible_icons(0, self.list.count())
            return

        wanted_set = set(wanted)
        # 先删掉不再需要的行（从后往前，避免下标漂移）
        for index in range(self.list.count() - 1, -1, -1):
            item = self.list.item(index)
            hit = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if hit is None or hit.id not in wanted_set:
                self.list.takeItem(index)
        # 再按目标顺序插入/移动
        for index, hit in enumerate(hits):
            item = self.list.item(index)
            current_hit = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if current_hit is not None and current_hit.id == hit.id:
                item.setData(Qt.ItemDataRole.UserRole, hit)
                continue
            if item is not None:
                self.list.takeItem(index)
            new_item = QListWidgetItem()
            new_item.setSizeHint(QSize(0, ROW_H))
            new_item.setData(Qt.ItemDataRole.UserRole, hit)
            cached = self._icons.get(hit.id)
            if cached is not None:
                new_item.setData(Qt.ItemDataRole.UserRole + 1, cached)
            self.list.insertItem(index, new_item)
        self._request_visible_icons(0, self.list.count())

    def _request_visible_icons(self, start: int, stop: int) -> None:
        """为还没有图标的行请求远端图标（结果写回 item 后 delegate 会自动重绘）。"""
        for index in range(max(0, start), min(stop, len(self.hits))):
            item = self.list.item(index)
            if item is None:
                continue
            hit = self.hits[index]
            if not hit.icon_url:
                continue
            if item.data(Qt.ItemDataRole.UserRole + 1) is not None:
                continue
            # 同一项目可能在不同分类/分页里反复出现：已经登记过请求就不再重复排队，
            # 否则 60 行的列表会挂出几百个等待者（每个都持有 item 引用）。
            if hit.icon_url in self._icon_requested:
                continue
            self._icon_requested.add(hit.icon_url)
            self.window.request_icon(
                hit.icon_url,
                lambda pixmap, target=item, hit_id=hit.id: self._store_icon(pixmap, target, hit_id),
            )

    def _store_icon(self, pixmap, item, hit_id: str) -> None:
        if pixmap.isNull():
            return
        self._icons[hit_id] = pixmap
        try:
            item.setData(Qt.ItemDataRole.UserRole + 1, pixmap)
        except RuntimeError:
            return
        row = self.list.row(item)
        if row >= 0:
            self.list.update(self.list.indexFromItem(item))

    def _flash_rows(self, inserted: list[int]) -> None:
        """新行轻微高亮一下（不移动控件、不抓图），给刷新一点反馈。"""
        for index in inserted[:12]:
            item = self.list.item(index)
            if item is None:
                continue
            self.list.scrollToItem(item)

    def _update_count(self) -> None:
        self.count_label.setObjectName("muted")
        self.count_label.setText(
            f"共 {human_count(self.total)} 个结果，已加载 {len(self.hits)}"
        )

    def _search_failed(self, token: int, message: str) -> None:
        if token != self._token:
            return
        self._requesting = False
        was_more = self._loading_more
        self._loading_more = False
        if was_more:
            # 追加下一页失败：保留已有列表，只在底栏提示并给「重试」
            self._set_more_state("failed")
            self.more_button.setEnabled(True)
            self.window.notice(message, "ERROR")
            return
        self._set_loading(False)
        self.count_label.setText(message)
        self.count_label.setObjectName("error")
        self.more_button.setEnabled(True)
        self.window.notice(message, "ERROR")

    # ----------------------------------------------------------------- detail
    def _on_select(self, item: QListWidgetItem) -> None:
        row = self.list.row(item)
        if not 0 <= row < len(self.hits):
            return
        hit = self.hits[row]
        self.selected = hit
        self.detail_title.setText(hit.title)
        self.detail_title.setToolTip(hit.title)
        self.detail_author.setText(hit.author or "Modrinth")
        self.detail_desc.setText(hit.description)
        self.detail_icon.setPixmap(initial_pixmap(hit.title, 60, hit.slug))
        self.detail_stats.setText(
            f"下载 {human_count(hit.downloads)}  ·  关注 {human_count(hit.follows)}"
            + (f"  ·  {hit.date_modified[:10]}" if hit.date_modified else "")
        )
        while self.detail_badges.count():
            widget = self.detail_badges.takeAt(0).widget()
            if widget is not None:
                # 先隐藏再延迟删除：deleteLater 要等事件循环才生效，
                # 期间旧徽标仍会画在原位置
                widget.hide()
                widget.deleteLater()
        for category in hit.categories[:3]:
            # 显式指定父控件：嵌套布局里的新控件若还是顶层窗口，会以 640×480
            # 的默认尺寸盖住整张详情卡片
            self.detail_badges.addWidget(Badge(category, "gray", self.detail))
        self.detail_badges.addStretch(1)
        self.open_button.setEnabled(True)
        if self.kind == "modpack":
            self.name_edit.setText(hit.title)
        if hit.icon_url:
            self.window.request_icon(hit.icon_url, self.detail_icon.setPixmap, 60)
        self._load_versions(hit)

    def _load_versions(self, hit: ProjectHit) -> None:
        """拉取当前选中项目的兼容版本（自动带上筛选框里的游戏版本/加载器）。"""
        self.versions = []
        self.version_combo.clear()
        self.version_combo.addItem("正在读取版本……", None)
        self.install_button.setEnabled(False)
        game_version, loader = "", ""
        if self.game_combo.currentData():
            game_version = str(self.game_combo.currentData())
        if self.loader_combo.isVisible() and self.loader_combo.currentData():
            loader = str(self.loader_combo.currentData())

        def worker() -> None:
            try:
                versions = self.window.content_client().versions(
                    hit.id, game_version=game_version, loader=loader
                )
            except Exception as exc:  # noqa: BLE001
                self.window.post(lambda text=str(exc): self._versions_failed(hit.id, text))
                return
            self.window.post(lambda: self._show_versions(hit, versions))

        threading.Thread(target=worker, daemon=True).start()

    def _versions_failed(self, project_id: str, message: str) -> None:
        if self.selected is None or self.selected.id != project_id:
            return
        self.version_combo.clear()
        self.version_combo.addItem("版本读取失败", None)
        self.detail_hint.setText(message)

    def _show_versions(self, hit: ProjectHit, versions) -> None:
        if self.selected is None or self.selected.id != hit.id:
            return
        self.versions = list(versions)
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        for version in self.versions[:80]:
            suffix = " · 测试版" if version.version_type != "release" else ""
            self.version_combo.addItem(f"{version.label}{suffix}", version)
        self.version_combo.blockSignals(False)
        if not self.versions:
            self.version_combo.addItem("没有兼容版本", None)
            self.detail_hint.setText("试试切换上方的游戏版本或加载器筛选。")
        else:
            self.detail_hint.setText(f"共 {len(self.versions)} 个兼容版本" if len(self.versions) > 1 else "")
        self._on_version_changed(self.version_combo.currentIndex())

    def _on_version_changed(self, index: int) -> None:
        if index < 0:
            return
        version = self.version_combo.itemData(index)
        if version is not None and self.kind == "modpack":
            self.name_edit.setText(f"{self.selected.title} {version.label}" if self.selected else "")
        self._refresh_install_state()

    def _on_target_changed(self, _index: int) -> None:
        if self.target_combo.currentData() == PICK_FOLDER:
            # 选中「选择文件夹…」= 立即弹资源管理器；取消则回退
            self.pick_folder()
            return
        target = self.current_target()
        self._last_target_key = target.key if target is not None else self._last_target_key
        self._refresh_install_state()

    def _dest_text(self, dest: Path) -> str:
        """安装目标路径的短描述：游戏目录下只显示相对部分。"""
        try:
            return f"游戏目录\\{Path(dest).relative_to(self._mc_dir())}"
        except ValueError:
            return "…\\" + "\\".join(Path(dest).parts[-3:])

    def _refresh_install_state(self) -> None:
        """安装按钮：要有版本，且目标目录能装这种内容。"""
        if not hasattr(self, "install_button"):
            return
        version = self._version()
        if self.kind == "modpack":
            self.install_button.setEnabled(version is not None)
            return
        target = self.current_target()
        if target is None:
            self.install_button.setEnabled(False)
            self.target_path.setText("还没有可选的游戏目录：先下载一个版本，或在上面选「选择文件夹…」")
            self.detail_hint.setText("先安装一个游戏版本，才能安装内容。")
            return
        dest = target.subdir(self.kind)
        self.target_path.setText(self._dest_text(dest))
        ok, reason = target_state(target, self.kind)
        self.target_path.setToolTip(f"{dest}\n{reason}" if reason else str(dest))
        if not ok:
            self.install_button.setEnabled(False)
            self.detail_hint.setText(reason)
            return
        if version is None:
            self.install_button.setEnabled(False)
            return
        self.install_button.setEnabled(True)
        if reason:
            self.detail_hint.setText(reason)

    def _open_page(self) -> None:
        if self.selected is not None:
            QDesktopServices.openUrl(QUrl(self.selected.page_url))

    # ----------------------------------------------------------------- install
    def _version(self) -> ProjectVersion | None:
        return self.version_combo.currentData()

    def install_selected(self) -> None:
        hit = self.selected
        version = self._version()
        if hit is None or version is None:
            return
        if self.kind == "modpack":
            self.window.install_modpack(hit, version, self.name_edit.text().strip())
            return
        target = self.current_target()
        if target is None:
            self.window.notice("请先选择安装位置：已装版本或任意游戏文件夹", "WARN")
            return
        ok, reason = target_state(target, self.kind)
        if not ok:
            self.window.notice(reason, "WARN")
            return
        self.window.install_content(hit, version, target, self.kind)
