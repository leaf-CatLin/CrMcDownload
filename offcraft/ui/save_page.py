"""存档详情页：点启动页里的存档 → 进来看到这个世界被读出的一切。

数据全部来自 :mod:`offcraft.saves`（纯只读解析，绝不改玩家存档）。页面本身
只做三件事：把 ``SaveReport`` 排版出来、把地图颜色字节画成图片、提供几个
"跳出去"的入口（打开文件夹 / 重新扫描 / 导出 ZIP）。

排版沿用界面既有语言：``Card`` + ``_stat`` 数字块 + ``role`` 属性字号，
不引入新的字号 / 圆角 / 颜色档位；所有颜色都从 ``theme`` 取。

扫描是**异步**的（几万区块的存档要秒级时间），先出骨架再填内容；扫描期间
顶部卡片实时显示阶段文字与进度条。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from offcraft.saves.mapcolors import MAP_BASE_COLORS, decode_map_colors
from offcraft.saves.model import (
    Container,
    DimensionInfo,
    EntityGroup,
    ItemStack,
    MapInfo,
    PlayerInfo,
    SaveReport,
    SaveScanOptions,
    TimelineEvent,
)
from offcraft.saves.report import scan_save
from offcraft.ui.icons import icon_pixmap
from offcraft.ui.theme import (
    ACCENT,
    BORDER,
    DANGER,
    MUTED,
    OK,
    TEXT,
    WARN,
    block_pixmap,
    human_size,
)
from offcraft.ui.tokens import SPACE, TYPE
from offcraft.ui.widgets import Badge, Card, EmptyState, style_menu

__all__ = ["SavePage", "render_map_image"]

#: 扫描参数：够看清世界，又不至于把界面卡住。
#:
#: 实测一份 3540 区块的真实存档，全解约 14 秒（纯 Python NBT 解析是瓶颈），
#: 默认预算 2500 个区块落在 8~10 秒；再大的存档会走"只数偏移表"的快速路径，
#: 界面会标明完整区块数受扫描范围限制。
DEFAULT_OPTIONS = SaveScanOptions(
    read_chunks=True,
    chunk_limit=2500,
    read_entities=True,
    read_poi=True,
    read_maps=True,
    map_limit=24,
)

#: 侧边留白（与版本设置页保持一致）
SIDE_PAD = 20

_GAME_MODE_TONE = {0: "green", 1: "blue", 2: "amber", 3: "purple"}


def _fmt_ticks(ticks: int) -> str:
    """tick → 可读时长（72000 tick = 1 小时）。"""
    if not ticks:
        return "—"
    seconds = ticks / 20
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f} 分钟"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f} 小时"
    return f"{hours / 24:.1f} 天"


def _fmt_when(stamp: int | None, *, with_time: bool = True) -> str:
    if not stamp:
        return "—"
    try:
        moment = datetime.fromtimestamp(stamp)
    except (OverflowError, ValueError, OSError):
        return "—"
    return moment.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def _fmt_coord(value: float | int | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}".rstrip("0").rstrip(".")


def render_map_image(map_info: MapInfo) -> QImage:
    """把地图的 ``colors`` 字节画成 :class:`QImage`（逐像素 RGB32）。

    Qt 的 ``Format_RGB32`` 在小端机器上的内存布局是 ``B G R A``，这里直接按
    这个顺序拼字节，比 128×128 次 ``setPixel`` 快一个数量级。
    """
    width = max(int(map_info.width or 128), 1)
    height = max(int(map_info.height or 128), 1)
    pixels = decode_map_colors(map_info.colors, width, height)
    buffer = bytearray(width * height * 4)
    offset = 0
    for pixel in pixels:
        if pixel is None:
            buffer[offset] = 0
            buffer[offset + 1] = 0
            buffer[offset + 2] = 0
            buffer[offset + 3] = 0
        else:
            red, green, blue, alpha = pixel
            buffer[offset] = blue
            buffer[offset + 1] = green
            buffer[offset + 2] = red
            buffer[offset + 3] = alpha
        offset += 4
    image = QImage(bytes(buffer), width, height, QImage.Format.Format_RGB32)
    return image


# --------------------------------------------------------------------- 小部件
class _Stat(QFrame):
    """概览数字块：大数字 + 小标题（比一行 ``键: 值`` 更容易扫读）。"""

    def __init__(
        self,
        title: str,
        value: str = "—",
        hint: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("softCard")
        column = QVBoxLayout(self)
        column.setContentsMargins(SPACE["md"], SPACE["sm"], SPACE["md"], SPACE["sm"])
        column.setSpacing(0)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("mutedSmall")
        column.addWidget(self.title_label)
        self.value_label = QLabel(value)
        self.value_label.setProperty("role", "h2")
        self.value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(self.value_label)
        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("mutedSmall")
        self.hint_label.setVisible(bool(hint))
        column.addWidget(self.hint_label)

    def set(self, value: str, hint: str = "") -> None:
        self.value_label.setText(value)
        self.hint_label.setText(hint)
        self.hint_label.setVisible(bool(hint))


def _kv_row(name: str, value: str, *, mono: bool = False) -> QWidget:
    """一行「键  值」：值可选中复制（种子、UUID 这类一定要能复制）。"""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(SPACE["sm"])
    key = QLabel(name)
    key.setObjectName("muted")
    key.setFixedWidth(86)
    key.setAlignment(Qt.AlignmentFlag.AlignTop)
    layout.addWidget(key)
    text = QLabel(value or "—")
    text.setWordWrap(True)
    text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if mono:
        text.setStyleSheet("font-family: Consolas, monospace;")
    text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    layout.addWidget(text, 1)
    return row


def _chip_row(items: Iterable[str], tone: str = "gray") -> QWidget:
    """一排小胶囊（数据包名、兴趣点类型…）。"""
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(SPACE["xs"])
    count = 0
    for item in items:
        badge = Badge(str(item), tone)
        layout.addWidget(badge)
        count += 1
    if count == 0:
        layout.addWidget(QLabel("—"))
    layout.addStretch(1)
    return holder


class _Bar(QWidget):
    """一条横比：名字 + 比例条 + 数量（方块 / 生物群系排行）。"""

    def __init__(
        self,
        label: str,
        ratio: float,
        value: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE["sm"])
        name = QLabel(label)
        name.setFixedWidth(150)
        name.setToolTip(label)
        layout.addWidget(name)

        track = QFrame()
        track.setFixedHeight(8)
        track.setStyleSheet(f"background: {BORDER}; border-radius: 4px;")
        track_layout = QHBoxLayout(track)
        track_layout.setContentsMargins(0, 0, 0, 0)
        fill = QFrame()
        fill.setStyleSheet(f"background: {ACCENT}; border-radius: 4px;")
        fill.setFixedHeight(8)
        track_layout.addWidget(fill)
        track_layout.addStretch(1)
        self._fill = fill
        self._track = track_layout
        layout.addWidget(track, 1)

        amount = QLabel(value)
        amount.setObjectName("muted")
        amount.setMinimumWidth(68)
        amount.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(amount)
        self._ratio = max(0.0, min(ratio, 1.0))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._apply()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        self._apply()

    def _apply(self) -> None:
        total = max(self._track.geometry().width(), 1)
        width = int(total * self._ratio)
        self._fill.setFixedWidth(max(width, 2) if self._ratio > 0 else 0)


class _ActivityStrip(QWidget):
    """活跃日历：把有记录的日期按自然日补零后画成柱状，一眼看出"哪几天在玩"。

    只画"有区块写入的那几天"是不够的：真实存档里相邻日期常常跳着来，那样
    画出来是一根孤零零的柱子，完全看不出活跃区间。所以先按自然日铺满整段
    区间（最多 ``_MAX_DAYS`` 天，超了就取最近的一段），没数据的日期画成浅色底。
    """

    _MAX_DAYS = 90
    #: 每天占的像素宽度（固定值：按控件宽度均分会让 4 天和 90 天看起来一样疏）
    _DAY_PX = 14

    def __init__(self, buckets: Sequence[tuple[int, int]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._days = self._fill_days(buckets)
        self.setFixedHeight(56)
        # 宽度按天数算，再夹在合理范围内：天数少时不至于拉成一整条稀疏的线
        self.setMinimumWidth(min(max(len(self._days), 1) * self._DAY_PX + 8, 1400))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setToolTip(
            "\n".join(
                f"{_fmt_when(day, with_time=False)}：{count} 个区块" if count
                else f"{_fmt_when(day, with_time=False)}：没有记录"
                for day, count in self._days[-20:]
            )
            or "没有区块写入记录"
        )

    def _fill_days(self, buckets: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
        """把稀疏的 ``(当天 0 点, 数量)`` 补成连续的自然日序列。"""
        data = {int(day): int(count) for day, count in buckets if count > 0}
        if not data:
            return []
        first, last = min(data), max(data)
        span_days = int((last - first) // 86400) + 1
        if span_days > self._MAX_DAYS:
            first = last - (self._MAX_DAYS - 1) * 86400
            span_days = self._MAX_DAYS
        return [(first + index * 86400, data.get(first + index * 86400, 0)) for index in range(span_days)]

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        return QSize(len(self._days) * self._DAY_PX + 8, 56)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        days = self._days
        if not days:
            painter.setPen(QColor(MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "没有活跃记录")
            painter.end()
            return
        peak = max((count for _day, count in days), default=0) or 1
        # 可用宽度若被压缩，就按实际宽度均分（宁可细一点也不溢出）
        step = min(self._DAY_PX, (self.width() - 8) / max(len(days), 1))
        bar_width = max(step - 3, 2)
        base = self.height() - 4
        painter.setPen(Qt.PenStyle.NoPen)
        for index, (_day, count) in enumerate(days):
            height = max(int((count / peak) * (base - 8)), 3) if count else 3
            left = 4 + int(index * step)
            painter.setBrush(QColor(ACCENT if count else BORDER))
            painter.drawRoundedRect(left, base - height, int(bar_width), height, 2, 2)
        painter.end()


class _MapTile(QWidget):
    """一张地图：缩略图 + 名字，点击放大。"""

    def __init__(
        self,
        map_info: MapInfo,
        on_click: Callable[[MapInfo], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.map_info = map_info
        self._on_click = on_click
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # 固定宽度：放在网格里时默认会被拉满整格，标题会飘到很远的地方
        self.setFixedWidth(104)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        picture = QLabel()
        picture.setFixedSize(96, 96)
        image = render_map_image(map_info)
        pixmap = QPixmap.fromImage(image).scaled(
            96, 96, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation
        )
        picture.setPixmap(pixmap)
        picture.setStyleSheet(f"border: 1px solid {BORDER}; border-radius: 6px;")
        picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(picture)
        caption = QLabel(f"#{map_info.map_id} · {map_info.scale_label}")
        caption.setObjectName("mutedSmall")
        caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        column.addWidget(caption)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_click(self.map_info)
        super().mouseReleaseEvent(event)


# --------------------------------------------------------------------- 页面
class SavePage(QWidget):
    """存档详情页。

    ``window`` 只需要三个方法（用 duck typing，方便测试传桩对象）：
    ``post(callback)``、``notice(text, level)``、``status(text)``。
    """

    def __init__(
        self,
        window: Any,
        save_dir: Path | str,
        *,
        options: SaveScanOptions | None = None,
        autoscan: bool = True,
    ) -> None:
        super().__init__()
        self.window = window
        self.save_dir = Path(save_dir)
        self.options = options or DEFAULT_OPTIONS
        self.report: SaveReport | None = None
        self._cancel = threading.Event()
        self._scanning = False
        self._token = 0
        self._build()
        if autoscan:
            self.start_scan()

    # ------------------------------------------------------------- 骨架
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SIDE_PAD, 12, SIDE_PAD, 18)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.body = QVBoxLayout(host)
        self.body.setContentsMargins(0, 0, 8, 0)
        self.body.setSpacing(SPACE["md"])
        scroll.setWidget(host)
        outer.addWidget(scroll, 1)

        self.body.addWidget(self._build_header())
        self.status_box = self._build_status()
        self.body.addWidget(self.status_box)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(SPACE["md"])
        self.body.addWidget(self.content)
        self.body.addStretch(1)

    def _build_header(self) -> QWidget:
        card = Card(margins=(SPACE["lg"], SPACE["md"], SPACE["lg"], SPACE["md"]))
        row = QHBoxLayout()
        row.setSpacing(SPACE["md"])

        self.icon_label = QLabel()
        self.icon_label.setFixedSize(64, 64)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setStyleSheet(f"border-radius: 8px; border: 1px solid {BORDER};")
        self._apply_icon()
        row.addWidget(self.icon_label)

        names = QVBoxLayout()
        names.setSpacing(2)
        self.title_label = QLabel(self.save_dir.name)
        self.title_label.setProperty("role", "h1")
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        names.addWidget(self.title_label)

        self.badge_row = QHBoxLayout()
        self.badge_row.setSpacing(SPACE["xs"])
        self.badge_row.addStretch(1)
        names.addLayout(self.badge_row)

        self.subtitle = QLabel(str(self.save_dir))
        self.subtitle.setObjectName("mutedSmall")
        self.subtitle.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        names.addWidget(self.subtitle)
        row.addLayout(names, 1)

        buttons = QVBoxLayout()
        buttons.setSpacing(SPACE["xs"])
        self.open_button = QPushButton("打开文件夹")
        self.open_button.setObjectName("ghost")
        self.open_button.setIcon(QIcon(icon_pixmap("folder", 14)))
        self.open_button.setIconSize(QSize(14, 14))
        self.open_button.clicked.connect(self._open_folder)
        buttons.addWidget(self.open_button)

        self.rescan_button = QPushButton("重新扫描")
        self.rescan_button.setObjectName("ghost")
        self.rescan_button.setIcon(QIcon(icon_pixmap("refresh", 14)))
        self.rescan_button.setIconSize(QSize(14, 14))
        self.rescan_button.clicked.connect(self.start_scan)
        buttons.addWidget(self.rescan_button)

        self.export_button = QPushButton("导出存档")
        self.export_button.setObjectName("ghost")
        self.export_button.setIcon(QIcon(icon_pixmap("download", 14)))
        self.export_button.setIconSize(QSize(14, 14))
        self.export_button.clicked.connect(self._export)
        buttons.addWidget(self.export_button)
        buttons.addStretch(1)
        row.addLayout(buttons)
        card.add_layout(row)
        return card

    def _build_status(self) -> QWidget:
        card = Card(margins=(SPACE["lg"], SPACE["sm"], SPACE["lg"], SPACE["sm"]))
        self.status_layout = QVBoxLayout()
        self.status_layout.setSpacing(2)
        row = QHBoxLayout()
        row.setSpacing(SPACE["sm"])
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        row.addWidget(self.progress, 1)
        self.phase_label = QLabel("准备扫描……")
        self.phase_label.setObjectName("mutedSmall")
        row.addWidget(self.phase_label)
        card.add_layout(row)
        card.add_layout(self.status_layout)
        return card

    def _clear_warnings(self) -> None:
        while self.status_layout.count():
            item = self.status_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _apply_icon(self) -> None:
        for name in ("icon.png", "icon.ico"):
            path = self.save_dir / name
            if not path.is_file():
                continue
            pixmap = QPixmap(str(path))
            if pixmap.isNull():
                continue
            self.icon_label.setPixmap(
                pixmap.scaled(
                    64, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
                )
            )
            return
        # 没有图标：用世界名生成方块图（与存档列表保持同一套视觉）
        self.icon_label.setPixmap(block_pixmap(self.save_dir.name, 64))

    # ------------------------------------------------------------- 扫描
    def start_scan(self) -> None:
        """在后台线程扫描；``post`` 回主线程填界面。"""
        if self._scanning:
            return
        self._scanning = True
        self._token += 1
        token = self._token
        self._cancel.clear()
        self.phase_label.setText("读取 level.dat……")
        self.progress.setRange(0, 0)  # 忙碌态
        self.status_box.setVisible(True)
        self._clear_warnings()
        self.rescan_button.setEnabled(False)

        def progress(fraction: float, text: str) -> None:
            if token != self._token:
                return
            self._post(lambda: self._on_progress(token, fraction, text))

        def worker() -> None:
            report: SaveReport | None = None
            error = ""
            try:
                report = scan_save(
                    self.save_dir, self.options, progress=progress, cancel=self._cancel
                )
            except Exception as exc:  # noqa: BLE001 - 解析失败要能展示给用户
                error = str(exc) or exc.__class__.__name__
            self._post(lambda: self._finish(token, report, error))

        threading.Thread(target=worker, name="save-scan", daemon=True).start()

    def _post(self, callback: Callable[[], None]) -> None:
        post = getattr(self.window, "post", None)
        if callable(post):
            post(callback)
        else:  # 测试里的桩：直接同步执行
            callback()

    def _on_progress(self, token: int, fraction: float, text: str) -> None:
        if token != self._token:
            return
        if fraction < 1.0:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(max(0.0, min(fraction, 1.0)) * 100))
        self.phase_label.setText(text)

    def _finish(self, token: int, report: SaveReport | None, error: str) -> None:
        if token != self._token:
            return
        self._scanning = False
        self.rescan_button.setEnabled(True)
        if report is None:
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.phase_label.setText(f"扫描失败：{error}")
            self._clear_content()
            self.content_layout.addWidget(
                EmptyState("info", "读不出这个存档", error or "目录里没有 level.dat")
            )
            return
        self.report = report
        self._clear_content()
        self._render(report)
        elapsed = report.elapsed
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        notes = f"扫描完成 · {elapsed:.1f}s"
        if report.warnings:
            notes += f" · {len(report.warnings)} 条提示"
        self.phase_label.setText(notes)
        self._clear_warnings()
        self.status_box.setVisible(bool(report.warnings))
        for text in report.warnings[:3]:
            warn = QLabel(f"· {text}")
            warn.setObjectName("error")
            warn.setWordWrap(True)
            self.status_layout.addWidget(warn)
        status = getattr(self.window, "status", None)
        if callable(status):
            status(f"{report.name}：{report.total_chunks} 个区块 · {elapsed:.1f}s")

    def _clear_content(self) -> None:
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    # ------------------------------------------------------------- 渲染
    def _render(self, report: SaveReport) -> None:
        level = report.level
        self.title_label.setText(report.name or self.save_dir.name)
        self.subtitle.setText(f"{self.save_dir}  ·  {human_size(report.size_bytes)}")
        while self.badge_row.count():
            item = self.badge_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for text, tone in self._header_badges(report):
            self.badge_row.addWidget(Badge(text, tone))
        self.badge_row.addStretch(1)

        self._render_overview(report)
        self._render_world(report)
        self._render_timeline(report)
        if report.players:
            self._render_players(report)
        self._render_chunks(report)
        if report.containers:
            self._render_containers(report)
        if report.entities:
            self._render_entities(report)
        if report.pois:
            self._render_pois(report)
        if report.structures:
            self._render_structures(report)
        if report.maps:
            self._render_maps(report)
        if report.rules:
            self._render_rules(report)
        _ = level  # 供上面各卡片复用

    def _header_badges(self, report: SaveReport) -> list[tuple[str, str]]:
        level = report.level
        badges: list[tuple[str, str]] = []
        if level.game_version:
            badges.append((f"MC {level.game_version}", "blue"))
        badges.append((level.game_mode_name, _GAME_MODE_TONE.get(int(level.game_type), "gray")))
        badges.append((level.difficulty_name, "amber"))
        if level.hardcore:
            badges.append(("极限模式", "red"))
        if level.allow_commands:
            badges.append(("已开作弊", "purple"))
        if level.was_modded:
            badges.append(("模组改过", "amber"))
        if level.split_world_data:
            badges.append(("新版数据文件", "gray"))
        return badges

    def _card(self, title: str, hint: str = "") -> tuple[Card, QVBoxLayout]:
        card = Card(margins=(SPACE["lg"], SPACE["md"], SPACE["lg"], SPACE["md"]))
        head = QHBoxLayout()
        head.setSpacing(SPACE["sm"])
        label = QLabel(title)
        label.setProperty("role", "h3")
        head.addWidget(label)
        if hint:
            note = QLabel(hint)
            note.setObjectName("mutedSmall")
            head.addWidget(note)
        head.addStretch(1)
        card.add_layout(head)
        body = QVBoxLayout()
        body.setSpacing(SPACE["xs"])
        card.add_layout(body)
        return card, body

    # ---- 概览
    def _render_overview(self, report: SaveReport) -> None:
        level = report.level
        grid = QGridLayout()
        grid.setHorizontalSpacing(SPACE["sm"])
        grid.setVerticalSpacing(SPACE["sm"])
        # 世界上限 tick 不等于"人玩了多久"：那是世界自己的年龄（挂机也在涨），
        # 真正的游玩时长在玩家的 stats 里（play_time）。两者都展示、分别标清。
        played = max((player.play_time for player in report.players), default=0)
        texture_hint = f"{len(report.containers)} 个容器"
        pending = sum(1 for c in report.containers if c.is_loot_pending)
        if pending:
            texture_hint += f"（{pending} 个未打开）"
        stats = [
            ("世界年龄", _fmt_ticks(level.time), f"{level.days:.2f} 个游戏日"),
            ("区块", f"{report.total_chunks:,}", f"完整 {report.full_chunks:,}"),
            ("实体", f"{report.total_entities:,}", "已加载实体文件"),
            ("容器物品", f"{report.total_container_items:,}", texture_hint),
            (
                "玩家游玩",
                _fmt_ticks(played) if played else "—",
                f"{len(report.players)} 位玩家",
            ),
            ("最后保存", _fmt_when((level.last_played or 0) // 1000), ""),
        ]
        for index, (title, value, hint) in enumerate(stats):
            tile = _Stat(title, value, hint)
            grid.addWidget(tile, index // 3, index % 3)
        card = Card(margins=(SPACE["lg"], SPACE["md"], SPACE["lg"], SPACE["md"]))
        title = QLabel("概览")
        title.setProperty("role", "h3")
        card.add(title)
        card.add_layout(grid)
        self.content_layout.addWidget(card)

    # ---- 世界
    def _render_world(self, report: SaveReport) -> None:
        level = report.level
        card, body = self._card("世界", "种子可以直接拿去重新生成同一张地图")
        seed_text = str(level.seed) if level.seed is not None else "—"
        body.addWidget(_kv_row("种子", seed_text, mono=True))
        if level.generator:
            body.addWidget(_kv_row("生成器", level.generator))
        if level.spawn is not None:
            spawn = level.spawn
            body.addWidget(
                _kv_row(
                    "出生点",
                    f"{_fmt_coord(spawn.x)}, {_fmt_coord(spawn.y)}, {_fmt_coord(spawn.z)}"
                    f"  ·  {spawn.dimension}"
                    + ("（强制）" if spawn.forced else ""),
                )
            )
        body.addWidget(
            _kv_row(
                "数据版本",
                f"{level.data_version or '—'}"
                + (f"（Anvil 格式 {level.storage_version}）" if level.storage_version else ""),
            )
        )
        if level.server_brands:
            body.addWidget(_kv_row("服务端", "、".join(level.server_brands)))
        if report.border is not None:
            border = report.border
            body.addWidget(
                _kv_row(
                    "世界边界",
                    f"直径 {border.size:,.0f} · 中心 {_fmt_coord(border.center_x)}, "
                    f"{_fmt_coord(border.center_z)}",
                )
            )
        if report.weather is not None:
            weather = report.weather
            state = "雷雨" if weather.thundering else ("下雨" if weather.raining else "晴")
            body.addWidget(
                _kv_row(
                    "天气",
                    f"{state}（雨 {weather.rain_time} / 雷 {weather.thunder_time} tick）",
                )
            )
        if report.clocks:
            body.addWidget(
                _kv_row(
                    "维度时钟",
                    "  ·  ".join(
                        f"{clock.dimension.split(':')[-1]} {_fmt_ticks(clock.total_ticks)}"
                        for clock in report.clocks
                    ),
                )
            )
        packs = list(level.data_packs) + [f"{name}（禁用）" for name in level.disabled_packs]
        if packs:
            body.addWidget(_kv_row("数据包", ""))
            body.addWidget(_chip_row(packs[:12], "blue"))
        if report.forced_chunks:
            kinds = sorted({kind.split(":")[-1] for kind, _x, _z in report.forced_chunks})
            body.addWidget(
                _kv_row("强制加载", f"{len(report.forced_chunks)} 个区块 · {'、'.join(kinds[:5])}")
            )
        self.content_layout.addWidget(card)

    # ---- 历史日志
    def _render_timeline(self, report: SaveReport) -> None:
        hint = "时间来自进度时间戳、区块写入时间与日志"
        card, body = self._card("历史日志", hint)
        if report.activity:
            body.addWidget(QLabel("活跃日期"))
            strip = _ActivityStrip(report.activity)
            # 固定在左侧：加一层行布局，条子自己不会撑满整张卡片
            strip_row = QHBoxLayout()
            strip_row.setContentsMargins(0, 0, 0, 0)
            strip_row.addWidget(strip, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            strip_row.addStretch(1)
            body.addLayout(strip_row)
        if not report.timeline:
            body.addWidget(QLabel("没有可读的时间记录（存档里没有进度时间戳）"))
        for event in report.timeline[:40]:
            body.addWidget(self._event_row(event))
        if report.logs:
            toggle = QPushButton(f"展开游戏日志（{len(report.logs)} 行）")
            toggle.setObjectName("ghost")
            toggle.setCheckable(True)
            view = QPlainTextEdit()
            view.setReadOnly(True)
            view.setFixedHeight(220)
            view.setVisible(False)
            view.setStyleSheet("font-family: Consolas, monospace;")
            view.setPlainText(
                "\n".join(
                    f"{_fmt_when(item.when)}  {item.title}" if item.when else item.title
                    for item in report.logs
                )
            )

            def flip(checked: bool) -> None:
                view.setVisible(checked)

            toggle.toggled.connect(flip)
            body.addWidget(toggle)
            body.addWidget(view)
        self.content_layout.addWidget(card)

    @staticmethod
    def _event_row(event: TimelineEvent) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(SPACE["sm"])
        icon = QLabel()
        icon.setFixedSize(16, 16)
        icon.setPixmap(icon_pixmap(event.icon, 14, MUTED))
        layout.addWidget(icon)
        stamp = QLabel(_fmt_when(event.when) if event.when else "当前")
        stamp.setObjectName("mutedSmall")
        stamp.setFixedWidth(112)
        layout.addWidget(stamp)
        text = QLabel(event.title)
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(text, 1)
        if event.detail:
            note = QLabel(event.detail)
            note.setObjectName("mutedSmall")
            layout.addWidget(note)
        return row

    # ---- 玩家
    def _render_players(self, report: SaveReport) -> None:
        card, body = self._card("玩家", "坐标与背包来自 players/data/<uuid>.dat")
        for player in report.players:
            body.addWidget(self._player_block(player))
        self.content_layout.addWidget(card)

    def _player_block(self, player: PlayerInfo) -> QWidget:
        holder = QFrame()
        holder.setObjectName("softCard")
        column = QVBoxLayout(holder)
        column.setContentsMargins(SPACE["md"], SPACE["sm"], SPACE["md"], SPACE["sm"])
        column.setSpacing(SPACE["xs"])

        head = QHBoxLayout()
        head.setSpacing(SPACE["sm"])
        name = QLabel(player.name or (player.uuid[:8] if player.uuid else "未知玩家"))
        name.setProperty("role", "bodyStrong")
        head.addWidget(name)
        if player.game_mode_name:
            head.addWidget(Badge(player.game_mode_name, "green"))
        if player.food_level is not None:
            head.addWidget(Badge(f"饥饿 {player.food_level}/20", "gray"))
        if player.xp_level is not None:
            head.addWidget(Badge(f"Lv.{player.xp_level}", "purple"))
        head.addStretch(1)
        if player.uuid:
            uuid_label = QLabel(player.uuid)
            uuid_label.setObjectName("mutedSmall")
            uuid_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            head.addWidget(uuid_label)
        column.addLayout(head)

        where = "—"
        if player.pos:
            x, y, z = player.pos
            where = f"{_fmt_coord(x)}, {_fmt_coord(y)}, {_fmt_coord(z)}"
        elif player.uuid and player.file_name == "level.dat":
            where = "内嵌在 level.dat（极老版本）"
        column.addWidget(
            _kv_row("位置", f"{where}  ·  {player.dimension or '未知维度'}")
        )
        stats = [
            f"游玩 {_fmt_ticks(player.play_time or player.total_world_time)}",
            f"生命 {_fmt_coord(player.health)}",
            f"击杀 {player.mob_kills}",
            f"死亡 {player.deaths}",
            f"跳跃 {player.jump_count}",
            f"行走 {player.walk_cm / 100 / 1000:.1f} km",
            f"挖掘 {player.blocks_mined:,}",
            f"进度 {player.advancement_count}/{len(player.advancements)}",
        ]
        summary = QLabel("  ·  ".join(item for item in stats if not item.endswith("—")))
        summary.setObjectName("mutedSmall")
        summary.setWordWrap(True)
        column.addWidget(summary)

        if player.inventory:
            column.addWidget(QLabel(f"背包（{player.inventory_count} 件 / {len(player.inventory)} 格）"))
            column.addWidget(self._item_grid(player.inventory))
        if player.ender_items:
            column.addWidget(QLabel(f"末影箱（{len(player.ender_items)} 格）"))
            column.addWidget(self._item_grid(player.ender_items, limit=18))
        if player.advancements:
            done = [item.display_name for item in player.advancements if item.done][:8]
            if done:
                column.addWidget(_chip_row(done, "green"))
        return holder

    @staticmethod
    def _item_grid(items: Sequence[ItemStack], *, columns: int = 4, limit: int = 36) -> QWidget:
        """把物品排成网格。

        背包常有 30~41 格，一列平铺会把"玩家"卡片拉到一整屏，导致后面的
        卡片全被挤出视野。这里按 ``columns`` 分栏并限制条数，剩下的用一行
        "还有 N 格"带过。
        """
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(SPACE["md"])
        grid.setVerticalSpacing(1)
        for index, item in enumerate(items[:limit]):
            label = f"{item.display_name} ×{item.count}"
            if item.enchantments:
                label += "  " + " ".join(
                    f"{name.split(':')[-1]}{level}" for name, level in item.enchantments[:2]
                )
            cell = QLabel(label)
            cell.setToolTip(
                f"{item.item_id}\n槽位 {item.slot}"
                + (f"\n耐久损失 {item.damage}" if item.damage else "")
                + (
                    "\n附魔：" + "、".join(f"{n} {lv}" for n, lv in item.enchantments)
                    if item.enchantments
                    else ""
                )
            )
            if item.enchantments:
                cell.setStyleSheet(f"color: {ACCENT};")
            elif item.damage:
                cell.setStyleSheet(f"color: {WARN};")
            grid.addWidget(cell, index // columns, index % columns)
        if len(items) > limit:
            more = QLabel(f"… 还有 {len(items) - limit} 格")
            more.setObjectName("mutedSmall")
            grid.addWidget(more, (limit // columns) + 1, 0, 1, columns)
        return holder

    # ---- 区块
    def _render_chunks(self, report: SaveReport) -> None:
        hint = (
            f"方块统计基于前 {report.chunks.sampled_chunks} 个区块的采样"
            if report.chunks.sampled_chunks
            else "只统计已完全生成的区块"
        )
        card, body = self._card("已加载区块", hint)

        # 维度表：用 holder 承载 grid（布局对象只能有一个父对象）
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(SPACE["xl"])
        grid.setVerticalSpacing(2)
        headers = ["维度", "区域文件", "区块", "完整 / 已解析", "实体", "兴趣点"]
        for column, text in enumerate(headers):
            label = QLabel(text)
            label.setObjectName("mutedSmall")
            grid.addWidget(label, 0, column)
        for row, dim in enumerate(report.dimensions, start=1):
            # "完整"只能从解出来的区块判断，受扫描预算限制。没解过的区块
            # 不是"没生成"，所以把已解析数一起写出来，避免看起来像坏档。
            full_text = f"{dim.chunk_full:,}"
            if dim.chunk_parsed and dim.chunk_parsed != dim.chunk_total:
                full_text = f"{dim.chunk_full:,} / {dim.chunk_parsed:,}"
            values = [
                dim.display_name,
                f"{dim.region_files}",
                f"{dim.chunk_total:,}",
                full_text,
                f"{dim.entity_count:,}",
                f"{dim.poi_count:,}",
            ]
            for column, text in enumerate(values):
                label = QLabel(text)
                if column == 0:
                    label.setToolTip(
                        f"{dim.key}\n{dim.path}"
                        + (
                            "\n（区块较多，完整/已解析标注了扫描范围）"
                            if dim.chunk_parsed and dim.chunk_parsed != dim.chunk_total
                            else ""
                        )
                    )
                else:
                    label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                grid.addWidget(label, row, column)
        # 数字列按内容收紧、把剩余宽度留给第一列，避免 6 列被摊得满屏
        for column in range(6):
            grid.setColumnStretch(column, 1 if column == 0 else 0)
        body.addWidget(holder)

        if report.chunks.top_blocks:
            totals = [count for _name, count in report.chunks.top_blocks]
            peak = max(totals) if totals else 1
            body.addWidget(QLabel("出现最多的方块（不含空气）"))
            for name, count in report.chunks.top_blocks[:10]:
                label = name.split(":")[-1].replace("_", " ")
                body.addWidget(_Bar(label, count / peak, f"{count:,}"))
        if report.chunks.biomes:
            totals = [count for _name, count in report.chunks.biomes]
            peak = max(totals) if totals else 1
            body.addWidget(QLabel("生物群系"))
            for name, count in report.chunks.biomes[:8]:
                label = name.split(":")[-1].replace("_", " ")
                body.addWidget(_Bar(label, count / peak, f"{count:,}"))
        if report.chunks.average_ground is not None:
            body.addWidget(
                _kv_row(
                    "地表高度",
                    f"平均 Y={report.chunks.average_ground}  "
                    f"（{report.chunks.min_ground} … {report.chunks.max_ground}）",
                )
            )
        if report.chunks.solid_blocks:
            body.addWidget(_kv_row("实体方块", f"{report.chunks.solid_blocks:,}（采样，不含空气）"))
        if report.chunks.broken:
            body.addWidget(_kv_row("损坏区块", f"{report.chunks.broken}", mono=False))
        self.content_layout.addWidget(card)

    # ---- 容器
    def _render_containers(self, report: SaveReport) -> None:
        total = sum(container.total_count for container in report.containers)
        pending = sum(1 for container in report.containers if container.is_loot_pending)
        empty = sum(1 for container in report.containers if not container.items and not container.is_loot_pending)
        parts = [f"{len(report.containers)} 个", f"共 {total} 件物品"]
        if pending:
            parts.append(f"{pending} 个未打开的战利品箱")
        if empty:
            parts.append(f"{empty} 个空")
        card, body = self._card("容器", " · ".join(parts))
        # 排序：有物品的先（那是真正的"资产"），其次未打开的战利品箱
        # （还有内容可期待），最后才是空箱子——空的最没信息量。
        def rank(item: Container) -> tuple[int, int]:
            if item.items:
                return (0, -item.total_count)
            if item.is_loot_pending:
                return (1, 0)
            return (2, 0)

        ordered = sorted(report.containers, key=rank)
        for container in ordered[:40]:
            body.addWidget(self._container_row(container))
        if len(ordered) > 40:
            more = QLabel(f"… 还有 {len(ordered) - 40} 个容器")
            more.setObjectName("mutedSmall")
            body.addWidget(more)
        self.content_layout.addWidget(card)

    @staticmethod
    def _container_row(container: Container) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(SPACE["sm"])
        kind = QLabel(container.kind_name)
        kind.setFixedWidth(60)
        layout.addWidget(kind)
        where = QLabel(f"{container.x}, {container.y}, {container.z}")
        where.setObjectName("mutedSmall")
        where.setFixedWidth(112)
        layout.addWidget(where)
        if container.items:
            names = "、".join(
                f"{item.display_name}×{item.count}" for item in container.items[:6]
            )
            if len(container.items) > 6:
                names += f" 等 {len(container.items)} 种"
            text = QLabel(names)
            text.setWordWrap(True)
        elif container.is_loot_pending:
            # 未打开的战利品箱：Items 为空是正常的，内容要等玩家首次打开才生成
            text = QLabel(f"未打开 · {container.loot_table.split(':')[-1]}")
            text.setObjectName("mutedSmall")
            text.setToolTip(
                f"战利品表：{container.loot_table}\n"
                f"随机种子：{container.loot_seed}\n"
                "玩家第一次打开这个箱子时，游戏才按战利品表生成内容。"
            )
        else:
            text = QLabel("空")
            text.setObjectName("mutedSmall")
        layout.addWidget(text, 1)
        return row

    # ---- 实体
    def _render_entities(self, report: SaveReport) -> None:
        card, body = self._card("实体", f"共 {report.total_entities} 个")
        peak = max((group.count for group in report.entities), default=1)
        for group in sorted(report.entities, key=lambda item: item.count, reverse=True)[:20]:
            body.addWidget(_Bar(group.display_name, group.count / max(peak, 1), f"{group.count:,}"))
        self.content_layout.addWidget(card)

    # ---- 兴趣点
    def _render_pois(self, report: SaveReport) -> None:
        counts: dict[str, int] = {}
        for record in report.pois:
            counts[record.display_name] = counts.get(record.display_name, 0) + 1
        card, body = self._card("兴趣点", "床 / 工作站 / 传送门驱动村民 AI 与刷铁机判定")
        body.addWidget(_chip_row([f"{name} ×{count}" for name, count in counts.items()], "gray"))
        for record in report.pois[:30]:
            body.addWidget(
                _kv_row(record.display_name, f"{record.x}, {record.y}, {record.z}")
            )
        self.content_layout.addWidget(card)

    # ---- 结构
    def _render_structures(self, report: SaveReport) -> None:
        counts: dict[str, int] = {}
        for item in report.structures:
            counts[item.display_name] = counts.get(item.display_name, 0) + 1
        card, body = self._card("结构", "来自区块的 structures.starts")
        body.addWidget(_chip_row([f"{name} ×{count}" for name, count in counts.items()], "purple"))
        self.content_layout.addWidget(card)

    # ---- 地图
    def _render_maps(self, report: SaveReport) -> None:
        card, body = self._card("地图", f"{len(report.maps)} 张已绘制 · 点击放大")
        holder = QWidget()
        strip = QGridLayout(holder)
        strip.setContentsMargins(0, 0, 0, 0)
        strip.setHorizontalSpacing(SPACE["md"])
        strip.setVerticalSpacing(SPACE["md"])
        for index, map_info in enumerate(report.maps[:16]):
            tile = _MapTile(map_info, self._show_map)
            strip.addWidget(tile, index // 6, index % 6, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        strip.setColumnStretch(6, 1)
        body.addWidget(holder)
        self.content_layout.addWidget(card)

    def _show_map(self, map_info: MapInfo) -> None:
        dialog = QWidget(self, Qt.WindowType.Dialog)
        dialog.setWindowTitle(f"地图 #{map_info.map_id}")
        layout = QVBoxLayout(dialog)
        image = render_map_image(map_info)
        size = 512
        view = QLabel()
        view.setPixmap(
            QPixmap.fromImage(image).scaled(
                size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation
            )
        )
        view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(view)
        caption = QLabel(
            f"#{map_info.map_id} · {map_info.scale_label} · "
            f"中心 {map_info.center_x}, {map_info.center_z} · {map_info.dimension}"
        )
        caption.setObjectName("mutedSmall")
        caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(caption)
        dialog.resize(size + 40, size + 80)
        dialog.show()
        self._map_dialog = dialog

    # ---- 游戏规则
    def _render_rules(self, report: SaveReport) -> None:
        card, body = self._card("游戏规则", f"{len(report.rules)} 条")
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(SPACE["md"])
        grid.setVerticalSpacing(1)
        for index, rule in enumerate(report.rules[:48]):
            name = QLabel(rule.short_name)
            name.setObjectName("muted")
            value = QLabel(rule.display_value)
            if isinstance(rule.value, bool):
                value.setStyleSheet(f"color: {OK if rule.value else MUTED};")
            grid.addWidget(name, index // 3, (index % 3) * 2)
            grid.addWidget(value, index // 3, (index % 3) * 2 + 1)
        # 用 holder 承载 grid 再整体加入布局：直接 addLayout(grid) 会在 grid
        # 已有父对象（holder）时触发 "already has a parent" 警告。
        body.addWidget(holder)
        if len(report.rules) > 48:
            more = QLabel(f"… 还有 {len(report.rules) - 48} 条")
            more.setObjectName("mutedSmall")
            body.addWidget(more)
        self.content_layout.addWidget(card)

    # ------------------------------------------------------------- 动作
    def _open_folder(self) -> None:
        try:
            import os

            os.startfile(str(self.save_dir))  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            notify = getattr(self.window, "notice", None)
            if callable(notify):
                notify(f"打不开文件夹：{self.save_dir}", "WARN")

    def _export(self) -> None:
        export = getattr(self.window, "_export_save", None)
        if callable(export):
            export(self.save_dir)
            return
        notify = getattr(self.window, "notice", None)
        if callable(notify):
            notify("当前环境不支持从详情页导出存档", "WARN")

    def shutdown(self) -> None:
        """页面销毁前取消后台扫描（避免扫描线程往已删除的控件回写）。"""
        self._cancel.set()
        self._token += 1


# --------------------------------------------------------------------- 尺寸
def page_size_hint() -> QSize:  # pragma: no cover - 仅供布局调试
    return QSize(900, 600)
