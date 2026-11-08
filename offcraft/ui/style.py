"""应用级线条风格：为下拉框 / 数值框绘制统一的简约箭头。

Qt 原生样式在 Windows 上会画出带立体感的三角箭头或加减号，和整体线条
风格不统一。这里用 QProxyStyle 只接管箭头绘制，其余仍交给当前系统样式，
因此不会影响字体、弹层和原生窗口行为。
"""

from __future__ import annotations

from PyQt6.QtCore import QRect
from PyQt6.QtWidgets import QProxyStyle, QStyle

from offcraft.ui.icons import icon_pixmap

_ARROW_SIZE = 12
_ARROW_COLOR = "#6B7688"


class LineStyle(QProxyStyle):
    """把下拉箭头替换成内置 SVG 线条图标。"""

    _ARROWS = {
        QStyle.PrimitiveElement.PE_IndicatorArrowDown: "chevron-down",
        QStyle.PrimitiveElement.PE_IndicatorArrowUp: "chevron-up",
        QStyle.PrimitiveElement.PE_IndicatorSpinDown: "chevron-down",
        QStyle.PrimitiveElement.PE_IndicatorSpinUp: "chevron-up",
    }

    def drawPrimitive(self, element, option, painter, widget=None) -> None:  # noqa: N802
        name = self._ARROWS.get(element)
        if name is None:
            super().drawPrimitive(element, option, painter, widget)
            return
        pixmap = icon_pixmap(name, _ARROW_SIZE, _ARROW_COLOR)
        rect = option.rect
        x = rect.center().x() - _ARROW_SIZE // 2
        y = rect.center().y() - _ARROW_SIZE // 2
        if element in (
            QStyle.PrimitiveElement.PE_IndicatorSpinDown,
            QStyle.PrimitiveElement.PE_IndicatorSpinUp,
        ):
            # 数值框上下按钮的绘制区域本身较小，避免箭头溢出
            y = rect.center().y() - _ARROW_SIZE // 2 + (1 if element == QStyle.PrimitiveElement.PE_IndicatorSpinUp else -1)
        painter.drawPixmap(
            QRect(x, y, _ARROW_SIZE, _ARROW_SIZE),
            pixmap,
            QRect(0, 0, pixmap.width(), pixmap.height()),
        )


__all__ = ["LineStyle"]
