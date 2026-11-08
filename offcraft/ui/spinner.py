"""加载动画：复刻 uiball「ldrs」的 ring —— 两段圆头圆弧反向旋转。

只用 QPainter 绘制，不依赖图片或 QML，尺寸与颜色都可以随主题调整。
"""

from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget

__all__ = ["Spinner"]

#: 每帧旋转角度与刷新间隔（约 60fps）
_STEP = 5.0
_INTERVAL_MS = 16


class Spinner(QWidget):
    """圆形加载指示器：默认隐藏，调用 ``start()`` 时开始动画。"""

    def __init__(self, size: int = 36, color: str = "#3A76E8", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = max(10, int(size))
        self._color = QColor(color)
        self._angle = 0.0
        self.setFixedSize(self._size, self._size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._timer = QTimer(self)
        self._timer.setInterval(_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)
        self.hide()

    # ---------------------------------------------------------------- 控制
    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()
        if not self.isVisible():
            self.show()

    def stop(self) -> None:
        self._timer.stop()
        if self.isVisible():
            self.hide()

    def is_running(self) -> bool:
        return self._timer.isActive()

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    # ---------------------------------------------------------------- 绘制
    def _tick(self) -> None:
        self._angle = (self._angle + _STEP) % 360.0
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        size = float(self._size)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        width = max(1.8, size * 0.1)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        # 外圈：顺时针，较长的一段圆弧
        outer = QRectF(width / 2 + size * 0.06, width / 2 + size * 0.06,
                       size - width - size * 0.12, size - width - size * 0.12)
        pen = QPen(self._color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(outer, int(self._angle * 16), int(-136 * 16))

        # 内圈：逆时针，较短且更淡，形成 ldrs ring 的双弧层次
        inner_size = size * 0.5
        offset = (size - inner_size) / 2
        inner = QRectF(offset, offset, inner_size, inner_size)
        tint = QColor(self._color)
        tint.setAlpha(150)
        pen = QPen(tint, max(1.4, width * 0.75), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(inner, int(-self._angle * 2 * 16), int(96 * 16))
        painter.end()
