"""可复用的小部件：卡片、徽标、内容行与列表绘制器。"""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtCore import QEvent, QObject, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from offcraft.content.modrinth import ProjectHit
from offcraft.ui.icons import icon_pixmap
from offcraft.ui.theme import (
    ACCENT,
    ACCENT_SOFT,
    SELECTED,
    HOVER_STRONG,
    FONT_FAMILY,
    MUTED,
    TEXT,
    TYPE,
    human_count,
    initial_pixmap,
)

__all__ = [
    "Badge",
    "Card",
    "Combo",
    "ComboPolisher",
    "ContentDelegate",
    "ContentRow",
    "ElidedLabel",
    "EmptyState",
    "ROW_H",
    "Tile",
    "polish_combo",
    "style_menu",
]

#: 弹层专用样式：只作用在这个弹层窗口及其子控件，不影响其它列表
_POPUP_QSS = f"""
QFrame#comboPopup {{
    background: rgba(255, 255, 255, 0.94);
    border: 0px;
}}
QAbstractItemView {{
    background: transparent;
    border: 0px;
    border-radius: 0px;
    padding: 4px;
    outline: 0;
    selection-background-color: {SELECTED};
    selection-color: {TEXT};
}}
QAbstractItemView::item {{
    min-height: 26px;
    padding: 2px 8px;
    border-radius: 6px;
}}
QAbstractItemView::item:hover {{ background: {HOVER_STRONG}; }}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 4px 2px;
}}
QScrollBar::handle:vertical {{
    background: rgba(160, 176, 200, 0.75);
    border-radius: 3px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: rgba(140, 158, 186, 0.85); }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""


def style_combo_popup(popup: QWidget, view: QAbstractItemView | None = None) -> None:
    """把下拉弹层设成不透明白底、无边框，并让选中项用主题浅蓝。

    弹层的黑边来自原生弹层窗口自身的边框/透明边距：这里给弹层整窗铺不透明白底，
    不依赖窗口透明（透明在部分显卡/远程桌面下会被绘制成黑边）。
    """
    if view is None:
        view = popup.findChild(QAbstractItemView)
    popup.setObjectName("comboPopup")
    popup.setStyleSheet(_POPUP_QSS)
    popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
    popup.setContentsMargins(0, 0, 0, 0)
    if not popup.isVisible():
        # 弹层已显示时改窗口标志会让 Qt 重建窗口（等于把弹层关掉），只在空闲时设置
        popup.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        popup.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
    if view is not None:
        view.setFrameShape(QFrame.Shape.NoFrame)
        view.setContentsMargins(0, 0, 0, 0)
        # 按像素滚动比按条目跳更顺滑；横向不需要滚动条
        view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        palette = view.palette()
        base = QColor("#FFFFFF")
        base.setAlpha(240)  # 240/255 ≈ 0.94，与弹层 QSS 一致
        palette.setColor(QPalette.ColorRole.Base, base)
        palette.setColor(QPalette.ColorRole.Highlight, QColor(SELECTED))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(TEXT))
        view.setPalette(palette)


def polish_combo(combo: QComboBox) -> None:
    """保留给外部调用的兼容入口（当前统一由 ComboPolisher 在弹层显示时处理）。"""
    return None


class Combo(QComboBox):
    """通用下拉框：限制弹层高度，未获得焦点时忽略滚轮。

    版本下拉框在启动页顶部，鼠标滚轮滑过时如果直接响应，很容易在滚动页面时
    误切换版本；只有用户点过下拉框（获得焦点）后才允许滚轮切换。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMaxVisibleItems(12)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)


class ComboPolisher(QObject):
    """全局事件过滤器：下拉弹层出现时去掉黑边并设成磨砂白。

    顺带把 ``QMenu`` 也纳入磨砂处理：菜单是独立顶层窗口，光靠 QSS 的
    ``rgba()`` 背景在不透明的顶层窗口上不会生效（被窗口自身的缺省底盖掉），
    必须同时打开 ``WA_TranslucentBackground`` 才有通透感。
    """

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 命名
        # 只在弹层真正显示时处理：构建页面时访问 combo.view() 会提前创建
        # 所有下拉弹层的顶层窗口，Windows 上会闪出一排小窗口。
        if event.type() == QEvent.Type.Show and isinstance(obj, QWidget):
            flags = obj.windowFlags()
            if flags & Qt.WindowType.Popup:
                view = obj.findChild(QAbstractItemView)
                if view is not None:
                    style_combo_popup(obj, view)
            elif isinstance(obj, QMenu):
                style_menu(obj)
        return False


def style_menu(menu: QMenu) -> None:
    """给弹出菜单开启窗口透明，让 QSS 的半透明背景真正生效。"""
    if getattr(menu, "_glass_ready", False):
        return
    try:
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        menu.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
    except (RuntimeError, AttributeError):
        return
    menu._glass_ready = True  # noqa: SLF001 - 只标记一次，避免重复改窗口标志


class Card(QFrame):
    """白底圆角卡片，带一个默认的竖直布局。"""

    def __init__(self, parent: QWidget | None = None, *, margins: tuple[int, int, int, int] = (18, 16, 18, 16)) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(*margins)
        self.body.setSpacing(10)

    def add(self, widget: QWidget, stretch: int = 0) -> None:
        self.body.addWidget(widget, stretch)

    def add_layout(self, layout, stretch: int = 0) -> None:
        self.body.addLayout(layout, stretch)


class Badge(QLabel):
    """圆角小标签。"""

    TONES = {
        "blue": ("rgba(233, 241, 254, 0.72)", ACCENT),
        "green": ("rgba(231, 246, 238, 0.72)", "#2E9E6B"),
        "amber": ("rgba(253, 243, 227, 0.72)", "#B47616"),
        "red": ("rgba(253, 236, 234, 0.72)", "#C2453F"),
        "gray": ("rgba(241, 243, 247, 0.72)", "#6C7688"),
        "purple": ("rgba(242, 236, 253, 0.72)", "#7B5BD6"),
    }

    def __init__(self, text: str = "", tone: str = "gray", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        background, color = self.TONES.get(tone, self.TONES["gray"])
        self.setStyleSheet(
            f"background: {background}; color: {color}; border-radius: 8px;"
            "padding: 2px 8px; font-size: 8.5pt;"
        )

    def set(self, text: str, tone: str | None = None) -> None:
        self.setText(text)
        if tone:
            self.set_tone(tone)


class ElidedLabel(QLabel):
    """单行省略标签（保留完整文本作为 tooltip）。"""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)
        self.setToolTip(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt 命名
        self._full = text
        metrics = self.fontMetrics()
        available = max(self.width(), 80)
        self.setToolTip(text)
        super().setText(metrics.elidedText(text, Qt.TextElideMode.ElideRight, available))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        metrics = self.fontMetrics()
        self.setToolTip(self._full)
        super().setText(metrics.elidedText(self._full, Qt.TextElideMode.ElideRight, max(self.width(), 60)))


class EmptyState(QWidget):
    """统一空状态：图标 + 标题 + 说明 + 可选主按钮。

    旧实现有兩套空态语言：列表页只是往列表里塞一条灰色文字（``_add_empty_item``），
    插件页却是 72px 方块 + 标题 + 三张卡片。这里收成同一个组件，语言统一、可复用。
    """

    def __init__(
        self,
        icon_name: str = "info",
        title: str = "",
        hint: str = "",
        action: tuple[str, Callable[[], None]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(16, 22, 16, 22)
        column.setSpacing(8)
        column.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)

        self.icon = QLabel()
        self.icon.setFixedSize(40, 40)
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon.setPixmap(icon_pixmap(icon_name, 30, MUTED))
        # 图标直接进竖排布局并显式居中：套一层 QHBoxLayout 时行高会被压成 0，
        # 图标与下面的标题就会叠在一起（踩过一次）。
        column.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignHCenter)

        self.title = QLabel(title)
        self.title.setObjectName("emptyTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.title, 0, Qt.AlignmentFlag.AlignHCenter)

        if hint:
            self.hint = QLabel(hint)
            self.hint.setObjectName("emptyHint")
            self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.hint.setWordWrap(True)
            column.addWidget(self.hint, 0, Qt.AlignmentFlag.AlignHCenter)
        else:
            self.hint = None

        if action is not None:
            text, callback = action
            button = QPushButton(text)
            button.setObjectName("ghost")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False: callback())
            button.setMaximumWidth(180)
            column.addWidget(button, 0, Qt.AlignmentFlag.AlignHCenter)

    def set_text(self, title: str, hint: str = "") -> None:
        self.title.setText(title)
        if self.hint is not None:
            self.hint.setText(hint)


class ContentRow(QWidget):
    """内容搜索结果行：图标、标题、简介、下载量。

    保留这个控件只为「详情面板 / 单独预览」这类一次性场景复用；列表本身已经
    换成 :class:`ContentDelegate` 直接绘制——每行一个 QWidget（一行 6~8 个控件、
    4 处内联 QSS）在 100 行时要 1.6 秒、滚动每档 13ms，是实测最大的卡顿来源。
    """

    def __init__(self, hit: ProjectHit, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.hit = hit
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 7, 10, 7)
        layout.setSpacing(12)

        self.icon = QLabel()
        self.icon.setFixedSize(46, 46)
        self.icon.setPixmap(initial_pixmap(hit.title, 46, hit.slug or hit.id))
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.icon)

        text = QVBoxLayout()
        text.setSpacing(2)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.title = QLabel(hit.title)
        self.title.setProperty("role", "bodyStrong")
        title_row.addWidget(self.title)
        self.kind = Badge(
            {"mod": "模组", "modpack": "整合包", "resourcepack": "资源包", "shader": "光影"}.get(
                hit.project_type, hit.project_type
            ),
            "blue",
        )
        title_row.addWidget(self.kind)
        title_row.addStretch(1)
        text.addLayout(title_row)
        self.desc = ElidedLabel(hit.description)
        self.desc.setProperty("role", "caption")
        self.desc.setObjectName("muted")
        text.addWidget(self.desc)
        layout.addLayout(text, 1)

        stats = QVBoxLayout()
        stats.setSpacing(2)
        self.downloads = QLabel(f"↓ {human_count(hit.downloads)}")
        self.downloads.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.downloads.setProperty("role", "caption")
        self.downloads.setObjectName("muted")
        stats.addWidget(self.downloads)
        self.author = QLabel(hit.author or "")
        self.author.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.author.setProperty("role", "caption")
        self.author.setObjectName("muted")
        stats.addWidget(self.author)
        layout.addLayout(stats)

    def set_icon(self, pixmap) -> None:
        self.icon.setPixmap(pixmap)


#: 内容行高度 / 图标边长 / 右侧信息列宽（delegate 与列表共用同一套数字）
ROW_H = 64
ROW_ICON = 44
ROW_STATS_W = 92


class ContentDelegate(QStyledItemDelegate):
    """内容列表的行绘制器：一次 ``paint`` 画完整行，不再为每行创建控件。

    数据全部放在 ``QListWidgetItem`` 上：

    * ``Qt.ItemDataRole.UserRole``     → :class:`ProjectHit`
    * ``Qt.ItemDataRole.UserRole + 1`` → 该行的图标 ``QPixmap``（可选）

    这样滚动时只有绘制开销，刷新时只增删数据项，100 行从 1.6s 降到毫秒级。
    """

    def __init__(self, window: Any = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.window = window
        self._bookmark = (
            {"mod": "模组", "modpack": "整合包", "resourcepack": "资源包", "shader": "光影"}
        )
        # 绘制时高频用到的对象全部预先构造：每帧 new 一个 QFont/QColor 在滚动时很贵。
        # 字体必须显式带 family：只有 pointSize 的 QFont 在每次 setFont 时都会触发
        # 字体回退查找，滚动时能明显感到顿感。
        self._title_font = QFont(FONT_FAMILY.split(",")[0].strip().strip('"'))
        self._title_font.setPointSizeF(TYPE["bodyStrong"][0])
        self._title_font.setWeight(QFont.Weight.DemiBold)
        self._small_font = QFont(FONT_FAMILY.split(",")[0].strip().strip('"'))
        self._small_font.setPointSizeF(TYPE["caption"][0])
        # 半透明画刷：行底色叠在玻璃卡片上仍能透出背景
        self._selected_brush = QColor(232, 240, 255, 110)
        self._hover_brush = QColor(240, 245, 253, 90)
        self._title_pen = QColor(TEXT)
        self._muted_pen = QColor(MUTED)
        self._accent_pen = QColor(ACCENT)
        #: 省略后的文本缓存：(宽度, 原文) -> 省略结果。
        #: 滚动时同一行的文本会被反复 elidedText + drawText（8 次/帧），缓存后
        #: 滚动一帧从 ~9ms 降到 ~3ms。列表内容变化时由 page 调 clear_text_cache 清空。
        self._text_cache: dict[tuple[str, int, bool], str] = {}

    def clear_text_cache(self) -> None:
        self._text_cache.clear()

    def _elide(self, text: str, width: int, *, strong: bool) -> str:
        """带缓存的省略文本（``strong`` 区分标题/说明用的字体）。"""
        key = (text, width, strong)
        cached = self._text_cache.get(key)
        if cached is not None:
            return cached
        metrics = QFontMetrics(self._title_font if strong else self._small_font)
        value = metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(width, 1))
        if len(self._text_cache) > 2048:
            self._text_cache.clear()
        self._text_cache[key] = value
        return value

    # ------------------------------------------------------------------ 尺寸
    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt 命名
        return QSize(0, ROW_H)

    # ------------------------------------------------------------------ 绘制
    def paint(self, painter, option, index) -> None:  # noqa: N802 - Qt 命名
        hit = index.data(Qt.ItemDataRole.UserRole)
        if hit is None:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = option.rect.adjusted(2, 1, -2, -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        # 行底：选中 / 悬停各一档
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._selected_brush if selected else self._hover_brush)
            painter.drawRoundedRect(rect, 8, 8)

        left = rect.left() + 8
        top = rect.top()

        # 图标（远端图标加载完成后由 page 写回 item，重绘即生效）
        icon = index.data(Qt.ItemDataRole.UserRole + 1)
        icon_rect = QRect(left, top + (ROW_H - ROW_ICON) // 2 - 1, ROW_ICON, ROW_ICON)
        if isinstance(icon, QPixmap) and not icon.isNull():
            painter.drawPixmap(icon_rect, icon)
        else:
            painter.drawPixmap(
                icon_rect, initial_pixmap(hit.title, ROW_ICON, hit.slug or hit.id)
            )
        text_left = icon_rect.right() + 12

        # 标题 + 类型徽标
        painter.setFont(self._title_font)
        metrics = painter.fontMetrics()
        title_rect = QRect(
            text_left, top + 12, rect.width() - ROW_STATS_W - (text_left - rect.left()), 20
        )
        painter.setPen(self._title_pen)
        title = self._elide(hit.title, title_rect.width() - 70, strong=True)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)

        label = self._bookmark.get(hit.project_type, hit.project_type)
        painter.setFont(self._small_font)
        metrics = painter.fontMetrics()
        badge_w = metrics.horizontalAdvance(label) + 16
        badge_rect = QRect(title_rect.left() + title_rect.width() - badge_w, top + 13, badge_w, 18)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._selected_brush)
        painter.drawRoundedRect(badge_rect, 9, 9)
        painter.setPen(self._accent_pen)
        painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, label)

        # 简介（单行省略）
        desc_rect = QRect(
            text_left,
            top + 36,
            rect.width() - ROW_STATS_W - (text_left - rect.left()) - 8,
            18,
        )
        painter.setPen(self._muted_pen)
        desc = self._elide(hit.description or "", desc_rect.width(), strong=False)
        painter.drawText(desc_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, desc)

        # 右侧统计（下载量 / 作者）
        stats_rect = QRect(rect.right() - ROW_STATS_W - 6, top + 12, ROW_STATS_W, 40)
        painter.drawText(
            stats_rect.adjusted(0, 0, 0, -20),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"↓ {human_count(hit.downloads)}",
        )
        author = self._elide(hit.author or "", stats_rect.width(), strong=False)
        painter.drawText(
            stats_rect.adjusted(0, 20, 0, 0),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            author,
        )
        painter.restore()


#: 悬停行底色（比选中色更淡）
EA_SOFT_HOVER = "#F2F6FE"


class Tile(QFrame):
    """可点击的快捷入口卡片（标题 + 说明 + 回调）。"""

    clicked = pyqtSignal()

    def __init__(
        self,
        title: str,
        hint: str = "",
        callback: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("tile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(56)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(0)
        self.title_label = QLabel(title)
        self.title_label.setProperty("role", "bodyStrong")
        self.hint_label = QLabel(hint)
        self.hint_label.setProperty("role", "caption")
        self.hint_label.setObjectName("muted")
        layout.addWidget(self.title_label)
        layout.addWidget(self.hint_label)
        if callback is not None:
            self.clicked.connect(callback)

    def set_hint(self, text: str) -> None:
        self.hint_label.setText(text)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        inside = self.rect().contains(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and inside:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

