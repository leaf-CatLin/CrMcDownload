"""轻量动效：控件进场淡入、按钮点击波纹与按下收缩、列表错开出现。

全部基于 Qt 属性动画与自绘，不引入图片资源；淡入结束后会把
``QGraphicsOpacityEffect`` 停用，避免长期挂在控件上拖慢绘制。
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
FADE_MS = 220
STAGGER_MS = 45
#: 波纹动画时长
PRESS_MS = 90


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
    if not _alive(widget) or not _alive(effect):
        return
    try:
        if widget.graphicsEffect() is effect:
            effect.setOpacity(1.0)  # 保持启用，避免反复启停影响布局与绘制
    except RuntimeError:
        pass
    try:
        widget._fade_anim = None  # noqa: SLF001
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


class _Ripple(QWidget):
    """从点击位置向外扩散的圆形波纹，画完即销毁。"""

    def __init__(self, parent: QWidget, origin: QPointF, color: QColor) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setGeometry(parent.rect())
        self._origin = origin
        self._color = QColor(color)
        self._progress = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(380)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_value)
        self._anim.finished.connect(self.deleteLater)
        self.raise_()
        self.show()
        self._anim.start()

    def _on_value(self, value) -> None:
        self._progress = float(value)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._progress <= 0:
            return
        reach = 1.0
        for corner in (
            self.rect().topLeft(),
            self.rect().topRight(),
            self.rect().bottomLeft(),
            self.rect().bottomRight(),
        ):
            dx = abs(self._origin.x() - corner.x())
            dy = abs(self._origin.y() - corner.y())
            reach = max(reach, dx + dy)
        radius = reach * self._progress
        color = QColor(self._color)
        color.setAlpha(int(self._color.alpha() * (1.0 - self._progress)))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(self._origin, radius, radius)


class MotionFilter(QObject):
    """全局事件过滤器：给所有按钮加点击波纹与轻微按下收缩。"""

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 命名
        if not isinstance(obj, QAbstractButton):
            return False
        etype = event.type()
        if etype == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton and obj.isEnabled():
                self._press(obj, event.position())
        elif etype in (
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.Leave,
            QEvent.Type.Hide,
        ):
            self._release(obj)
        return False

    # ------------------------------------------------------------------ 内部
    def _press(self, button: QAbstractButton, position: QPointF) -> None:
        self._ripple(button, position)

    def _release(self, button: QAbstractButton) -> None:
        """松开时不需要额外处理：波纹动画自己结束并销毁。"""

    @staticmethod
    def _ripple(button: QAbstractButton, position: QPointF) -> None:
        accent = button.objectName() in ("accent", "accentBig")
        color = QColor(255, 255, 255, 120) if accent else QColor(ACCENT)
        if not accent:
            color.setAlpha(46)
        try:
            _Ripple(button, position, color)
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
