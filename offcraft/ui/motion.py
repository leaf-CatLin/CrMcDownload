"""轻量动效：控件进场淡入与按钮点击波纹。

两条性能纪律（此前都踩过）：

1. 淡入结束后**必须卸载** ``QGraphicsOpacityEffect``。保留它会把这个控件
   （整页页面时是整页）的每次绘制都路由到离屏 pixmap 再合成；导航几轮后会残留
   若干个 effect，直接吃掉帧预算。
2. 波纹用**每页一个复用的覆盖层**，不再每次点击都 new 一个铺满按钮的 QWidget：
   后者会让点击瞬间多出一次全尺寸合成，快速连点时尤其明显。
"""

from __future__ import annotations

from PyQt6 import sip
from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPointF,
    QPropertyAnimation,
    QTimer,
    QVariantAnimation,
    Qt,
)
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QGraphicsOpacityEffect,
    QWidget,
)

from offcraft.ui.theme import ACCENT

__all__ = ["MotionFilter", "fade_in", "install", "stagger"]

#: 淡入时长（毫秒）与列表错开的最大间隔
FADE_MS = 180
STAGGER_MS = 40
#: 波纹动画时长
RIPPLE_MS = 320


def _alive(widget) -> bool:
    """控件是否还活着：延迟触发的动画可能落在已销毁的页面上。"""
    if widget is None:
        return False
    try:
        return not sip.isdeleted(widget)
    except (RuntimeError, TypeError):
        return False


def fade_in(widget: QWidget, *, duration: int = FADE_MS) -> None:
    """让控件淡入出现；已经在淡入的控件不会叠加新的动画。"""
    if not isinstance(widget, QWidget) or not _alive(widget):
        return
    running = getattr(widget, "_fade_anim", None)
    if running is not None:
        try:
            if running.state() == QPropertyAnimation.State.Running:
                return
        except RuntimeError:
            pass

    effect = widget.graphicsEffect()
    if not isinstance(effect, QGraphicsOpacityEffect):
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
    effect.setOpacity(0.0)

    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    anim.finished.connect(lambda: _finish(widget, effect, anim))
    widget._fade_anim = anim  # noqa: SLF001 - 挂在控件上保引用防回收
    anim.start()


def _finish(widget: QWidget, effect: QGraphicsOpacityEffect, anim: QPropertyAnimation) -> None:
    """淡入完成：彻底摘掉 effect，让控件回到普通绘制路径。"""
    try:
        widget._fade_anim = None  # noqa: SLF001
    except RuntimeError:
        pass
    if _alive(widget) and _alive(effect):
        try:
            if widget.graphicsEffect() is effect:
                effect.setOpacity(1.0)
                widget.setGraphicsEffect(None)
        except RuntimeError:
            pass
    if _alive(anim):
        anim.deleteLater()


def stagger(widgets, *, step: int = STAGGER_MS, duration: int = FADE_MS) -> None:
    """一组控件依次淡入（用于安装页的加载器行、搜索结果等）。"""
    for index, widget in enumerate(widgets):
        if not isinstance(widget, QWidget):
            continue
        if step and index:
            QTimer.singleShot(
                index * step,
                lambda target=widget: fade_in(target, duration=duration),
            )
        else:
            fade_in(widget, duration=duration)


class _RippleLayer(QWidget):
    """按钮内的波纹层：每次点击复用同一个子控件，只重设起点与半径。

    波纹必须限制在按钮自己的矩形里（``parent.rect()``）。曾经为了省一次
    new 控件，把它挂到整个窗口上并按窗口矩形绘制 —— 于是点任何按钮都变成
    「全屏扩散的水波」，这属于把性能优化做过头了：正确做法是让波纹层的父控件
    就是按钮本身，这样既有复用、又天然被按钮裁剪。
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._origin = QPointF()
        self._color = QColor(ACCENT)
        self._reach = 1.0
        self._progress = 1.0
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(RIPPLE_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_value)
        self.hide()

    def play(self, rect, origin: QPointF, color: QColor) -> None:
        """``rect`` 是父控件（按钮）的矩形，``origin`` 是按钮内的坐标。"""
        self._origin = origin
        self._color = QColor(color)
        # 半径只需覆盖按钮自己的四个角
        self._reach = max(
            1.0,
            (origin - QPointF(rect.topLeft())).manhattanLength(),
            (origin - QPointF(rect.bottomRight())).manhattanLength(),
            (origin - QPointF(rect.topRight())).manhattanLength(),
            (origin - QPointF(rect.bottomLeft())).manhattanLength(),
        )
        self._progress = 0.0
        self.setGeometry(rect)
        self.show()
        self.raise_()
        self._anim.stop()
        self._anim.start()

    def _on_value(self, value) -> None:
        self._progress = float(value)
        self.update()
        if self._progress >= 1.0:
            self.hide()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._progress >= 1.0 or self._progress <= 0.0:
            return
        radius = self._reach * self._progress
        color = QColor(self._color)
        color.setAlpha(max(0, int(self._color.alpha() * (1.0 - self._progress))))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(self._origin, radius, radius)
        painter.end()


class MotionFilter(QObject):
    """全局事件过滤器：给所有按钮加点击波纹。"""

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 命名
        if not isinstance(obj, QAbstractButton):
            return False
        if event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton and obj.isEnabled():
                self._ripple(obj, event.position())
        return False

    @staticmethod
    def _ripple(button: QAbstractButton, position: QPointF) -> None:
        accent = button.objectName() in ("accent", "accentBig", "primary")
        color = QColor(255, 255, 255, 110) if accent else QColor(ACCENT)
        if not accent:
            color.setAlpha(34)
        # 波纹层是按钮的子控件：天然被按钮矩形裁剪，且同一按钮的所有点击复用它
        layer = getattr(button, "_ripple_layer", None)
        if layer is None or not _alive(layer):
            try:
                layer = _RippleLayer(button)
            except RuntimeError:
                return
            button._ripple_layer = layer  # noqa: SLF001 - 挂在按钮上保引用
        try:
            layer.play(button.rect(), position, color)
        except RuntimeError:
            pass


def install(app: QApplication | None = None) -> MotionFilter | None:
    """在应用上安装动效过滤器（重复调用只会安装一次）。"""
    app = app or QApplication.instance()
    if app is None:
        return None
    existing = getattr(app, "_motion_filter", None)
    if existing is not None:
        return existing
    filt = MotionFilter(app)
    app._motion_filter = filt  # noqa: SLF001 - 保引用防回收
    app.installEventFilter(filt)
    return filt
