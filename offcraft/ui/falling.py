"""彩蛋：进入「点哪掉哪」模式，逐个把界面控件丢进物理世界。

行为：

- 点击「千万别点」后页面保持原样，之后**点哪个组件，哪个组件就脱落掉落**；
- 掉落的控件用圆角白底贴图绘制，可拖拽甩飞；未点中的组件留在原位；
- 单击已掉落的组件会恢复整个页面并触发该控件原本的功能（按钮/下拉框/输入框）；
  底部提示条本身也能点，作为兜底的恢复入口；
- 不再提供 Esc、双击空白或静止自动恢复。

实现要点：页面用 ``central.grab()`` 做静态底图；组件脱落时挂一个透明度 0 的
QGraphicsOpacityEffect（不影响布局），再重新抓底图，于是原位留空、控件开始下落。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QCursor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QComboBox,
    QGraphicsOpacityEffect,
    QLabel,
    QLineEdit,
    QProgressBar,
    QSlider,
    QTabBar,
    QWidget,
)

from offcraft.ui.physics import Body, PhysicsWorld

__all__ = ["FallingOverlay"]


@dataclass
class _Piece:
    pixmap: QPixmap
    body: Body
    widget: QWidget | None = None


def _is_element(widget: QWidget) -> bool:
    """是不是可以掉落的「页面元素」：容器/背景一律排除。"""
    if isinstance(widget, QAbstractButton):
        return True
    if isinstance(widget, (QLineEdit, QComboBox, QAbstractSpinBox, QSlider, QProgressBar, QTabBar)):
        return True
    if isinstance(widget, QLabel):
        if widget.text().strip():
            return True
        pixmap = widget.pixmap()
        return pixmap is not None and not pixmap.isNull()
    return False


def _logical(pixmap: QPixmap) -> QPixmap:
    """把高分屏截图压到逻辑尺寸（DPR=1），否则坐标与像素会错位一倍多。"""
    dpr = pixmap.devicePixelRatio() or 1.0
    if dpr == 1.0:
        return pixmap
    scaled = pixmap.scaled(
        pixmap.deviceIndependentSize().toSize(),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    scaled.setDevicePixelRatio(1.0)
    return scaled


class FallingOverlay(QWidget):
    """覆盖在主窗口上的点选式彩蛋画布；关闭时恢复原页面。"""

    FPS_MS = 20
    #: 手点/高分屏缩放下的正常抖动可达十几逻辑像素，太小会把点击误判成拖拽
    CLICK_SLOP = 18
    #: 按住不超过这么久都算点击（超过视为拖拽）
    CLICK_HOLD = 2.5

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self._window = window
        self._central = window.centralWidget()
        self._pieces: list[_Piece] = []
        self._effects: dict[QWidget, QGraphicsOpacityEffect] = {}
        self._dragging: _Piece | None = None
        self._drag_offset = QPoint()
        self._press_pos = QPoint()
        self._press_time = 0.0
        self._last_pos: QPoint | None = None
        self._activatable_on_release = False
        self._restored = False
        self._backdrop = QPixmap()
        self._backdrop_offset = QPoint()

        self.setGeometry(window.rect())
        self.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._refresh_backdrop()
        self.raise_()
        self.show()
        self.setFocus()

        self._world = PhysicsWorld(width=float(self.width()), height=float(self.height()))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._last_tick = time.monotonic()
        self._timer.start(self.FPS_MS)

    # ---------------------------------------------------------------- 页面底图
    def _grab_page(self) -> tuple[QPixmap, QPoint]:
        if self._central is None:
            return QPixmap(), QPoint()
        return _logical(self._central.grab()), self._central.mapTo(self._window, QPoint(0, 0))

    def _refresh_backdrop(self) -> None:
        self._backdrop, self._backdrop_offset = self._grab_page()

    # ---------------------------------------------------------------- 选组件
    def _find_element(self, pos: QPoint) -> tuple[QWidget, QRect] | None:
        if self._central is None:
            return None
        hits: list[tuple[QWidget, QRect]] = []

        def walk(widget: QWidget) -> None:
            if not widget.isVisible() or widget.width() < 10 or widget.height() < 8:
                return
            if widget in self._effects:
                return
            if _is_element(widget):
                top_left = widget.mapTo(self._window, QPoint(0, 0))
                rect = QRect(top_left, widget.size())
                if rect.contains(pos):
                    hits.append((widget, rect))
                return
            for child in widget.children():
                if isinstance(child, QWidget):
                    walk(child)

        walk(self._central)
        if not hits:
            return None
        return min(hits, key=lambda item: item[1].width() * item[1].height())

    # ---------------------------------------------------------------- 掉落
    @staticmethod
    def _fits(widget: QWidget, rect: QRect) -> bool:
        """太薄太细的控件掉了也不好看，直接忽略（不隐藏、不生成碎块）。"""
        if rect.width() < 20 or rect.height() < 16:
            return False
        ratio = rect.width() / max(rect.height(), 1)
        return ratio <= (5.0 if isinstance(widget, QLabel) else 8.0)

    def _detach(self, widget: QWidget, rect: QRect) -> _Piece | None:
        pixmap = self._grab_widget(widget)
        if pixmap.isNull():
            return None
        trimmed, pixmap = self._trim(widget, rect, pixmap)
        if trimmed is None or not self._fits(widget, trimmed):
            return None
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(0.0)
        widget.setGraphicsEffect(effect)
        self._effects[widget] = effect
        self._refresh_backdrop()
        piece = self._spawn(widget, trimmed, pixmap)
        if piece is None:
            # 生成失败就回滚，别把控件永久藏起来却看不到碎块
            widget.setGraphicsEffect(None)
            self._effects.pop(widget, None)
            self._refresh_backdrop()
        return piece

    def _grab_widget(self, widget: QWidget) -> QPixmap:
        return _logical(widget.grab())

    def _trim(self, widget: QWidget, rect: QRect, pixmap: QPixmap) -> tuple[QRect | None, QPixmap]:
        """文字标签常常占着一大块空白；按实际文字/图标大小裁掉，避免白色大板。"""
        if not isinstance(widget, QLabel):
            return rect, pixmap
        pixmap_label = widget.pixmap()
        if pixmap_label is not None and not pixmap_label.isNull():
            dpr = pixmap_label.devicePixelRatio() or 1.0
            width = max(20, int(pixmap_label.width() / dpr))
            height = max(16, int(pixmap_label.height() / dpr))
        else:
            text = widget.text().strip()
            if not text:
                return None, pixmap
            metrics = widget.fontMetrics()
            flags = int(Qt.AlignmentFlag.AlignLeft)
            if widget.wordWrap():
                flags |= int(Qt.TextFlag.TextWordWrap)
            bounds = metrics.boundingRect(
                QRect(0, 0, max(rect.width() - 2, 40), 100000), flags, text
            )
            width = max(24, bounds.width() + 8)
            height = max(18, bounds.height() + 6)
        width = min(width, rect.width())
        height = min(height, rect.height())
        align = widget.alignment()
        x = (
            rect.left() + (rect.width() - width) // 2
            if align & Qt.AlignmentFlag.AlignHCenter
            else rect.left()
        )
        y = (
            rect.top() + (rect.height() - height) // 2
            if align & Qt.AlignmentFlag.AlignVCenter
            else rect.top()
        )
        trimmed = QRect(x, y, width, height)
        cropped = pixmap.copy(x - rect.left(), y - rect.top(), width, height)
        return trimmed, cropped

    def _spawn(self, widget: QWidget, rect: QRect, pixmap: QPixmap) -> _Piece | None:
        if not self._fits(widget, rect):
            return None
        rounded = self._rounded(pixmap)
        if rounded.isNull():
            return None
        rng = random.Random(rect.x() * 131 + rect.y() * 17 + rect.width())
        body = Body(
            x=float(rect.center().x()),
            y=float(rect.center().y()),
            width=float(rect.width()),
            height=float(rect.height()),
            vx=rng.uniform(-140, 140),
            vy=rng.uniform(-190, -60),
            angle=0.0,
            spin=rng.uniform(-5.0, 5.0),
        )
        piece = _Piece(pixmap=rounded, body=body, widget=widget)
        self._pieces.append(piece)
        return piece

    @staticmethod
    def _rounded(pixmap: QPixmap, radius: float = 9.0) -> QPixmap:
        result = QPixmap(pixmap.size())
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        rect = QRectF(0.5, 0.5, pixmap.width() - 1.0, pixmap.height() - 1.0)
        path.addRoundedRect(rect, radius, radius)
        painter.setClipPath(path)
        painter.fillRect(rect, QColor(255, 255, 255, 242))
        painter.drawPixmap(0, 0, pixmap)
        painter.setClipping(False)
        painter.setPen(QPen(QColor(211, 220, 233, 220), 1.0))
        painter.drawPath(path)
        painter.end()
        return result

    # ---------------------------------------------------------------- 帧循环
    def _tick(self) -> None:
        now = time.monotonic()
        dt = max(0.001, min(now - self._last_tick, 0.05))
        self._last_tick = now
        self._world.step_all([p.body for p in self._pieces], dt)
        self.update()

    # ---------------------------------------------------------------- 绘制
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        if not self._backdrop.isNull():
            painter.drawPixmap(self._backdrop_offset, self._backdrop)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        for piece in self._pieces:
            body = piece.body
            painter.save()
            painter.translate(body.x, body.y)
            painter.rotate(body.angle * 57.2957795)
            pixmap = piece.pixmap
            painter.drawPixmap(
                QRect(-pixmap.width() // 2, -pixmap.height() // 2,
                      pixmap.width(), pixmap.height()),
                pixmap,
            )
            painter.restore()
        self._draw_hint(painter)
        painter.end()

    def _hint_rect(self) -> QRectF:
        text = "点哪个组件哪个掉落 · 点碎块恢复原功能（点这里也可恢复）"
        font = QFont()
        font.setPointSize(9)
        metrics = QFontMetrics(font)
        width = metrics.horizontalAdvance(text) + 26
        height = metrics.height() + 10
        # 放在底部居中，避免遮住顶部导航/标题
        return QRectF((self.width() - width) / 2, self.height() - height - 14.0, width, height)

    def _draw_hint(self, painter: QPainter) -> None:
        text = "点哪个组件哪个掉落 · 点碎块恢复原功能（点这里也可恢复）"
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        rect = self._hint_rect()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 216))
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        painter.setPen(QColor(92, 103, 120))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    # ---------------------------------------------------------------- 交互
    def _hit_piece(self, pos: QPoint) -> _Piece | None:
        for piece in reversed(self._pieces):
            body = piece.body
            if abs(pos.x() - body.x) <= body.width / 2 and abs(pos.y() - body.y) <= body.height / 2:
                return piece
        return None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()
        self._press_pos = pos
        self._press_time = time.monotonic()
        piece = self._hit_piece(pos)
        if piece is None and self._hint_rect().contains(QPointF(pos)):
            # 兜底：点提示条也能恢复（碎块优先，避免挡住落在底部的碎块）
            self.restore()
            return
        if piece is not None:
            self._activatable_on_release = True
        else:
            found = self._find_element(pos)
            if found is None:
                self._activatable_on_release = False
                return
            piece = self._detach(*found)
            self._activatable_on_release = False
            if piece is None:
                return
        self._dragging = piece
        piece.body.asleep = False
        piece.body.on_floor = False
        piece.body.vx = piece.body.vy = piece.body.spin = 0.0
        self._drag_offset = pos - QPoint(int(piece.body.x), int(piece.body.y))
        self._last_pos = pos
        self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
        if self._dragging is None:
            self.setCursor(
                QCursor(
                    Qt.CursorShape.PointingHandCursor
                    if self._hit_piece(pos) is not None
                    else Qt.CursorShape.OpenHandCursor
                )
            )
            return
        body = self._dragging.body
        target = pos - self._drag_offset
        if self._last_pos is not None:
            body.vx = (target.x() - body.x) * 12.0
            body.vy = (target.y() - body.y) * 12.0
        body.x = float(target.x())
        body.y = float(target.y())
        self._last_pos = pos
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        piece = self._dragging
        self._dragging = None
        self._last_pos = None
        self.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
        if piece is None:
            return
        moved = (event.position().toPoint() - self._press_pos).manhattanLength()
        if (
            self._activatable_on_release
            and moved <= self.CLICK_SLOP
            and time.monotonic() - self._press_time <= self.CLICK_HOLD
        ):
            self._activate(piece.widget)

    def _activate(self, widget: QWidget | None) -> None:
        """恢复页面后再触发原控件，让掉下来的按钮依然「能点」。"""
        self.restore()
        if widget is None:
            return
        QTimer.singleShot(0, lambda w=widget: self._click_widget(w))

    @staticmethod
    def _click_widget(widget: QWidget) -> None:
        try:
            if not widget.isVisible() or not widget.isEnabled():
                return
            if isinstance(widget, QAbstractButton):
                widget.click()
            elif isinstance(widget, QComboBox):
                widget.showPopup()
            else:
                widget.setFocus()
        except RuntimeError:
            pass

    # ---------------------------------------------------------------- 恢复
    def restore(self) -> None:
        if self._restored:
            return
        self._restored = True
        self._timer.stop()
        for widget in list(self._effects):
            try:
                widget.setGraphicsEffect(None)
            except RuntimeError:
                pass
        self._effects.clear()
        self._pieces.clear()
        self._dragging = None
        parent = self._window
        if getattr(parent, "_falling_overlay", None) is self:
            parent._falling_overlay = None
        if self._central is not None:
            self._central.update()
        self.hide()
        self.deleteLater()
