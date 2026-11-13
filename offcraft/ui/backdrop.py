"""可自定义的窗口背景层。

自上而下叠三层：

1. 默认渐变（最底层，保证背景图带透明通道时不会露出窗口后方的黑色）；
2. 背景图片（PNG / GIF，等比铺满窗口，居中裁切）；
3. 颜色蒙版：图片 + 颜色时在图片上蒙一层很淡的纯色；只有颜色时直接铺满。

界面控件都是半透明的毛玻璃卡片，会透出这一层的颜色与纹理。
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRectF, QSize, Qt
from PyQt6.QtGui import (
    QColor,
    QLinearGradient,
    QMovie,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import QWidget

__all__ = ["Backdrop"]

#: 颜色蒙版不透明度（0-255）：“很淡”的一层色膜
TINT_ALPHA = 44

#: 与 QSS 中 QMainWindow 的默认渐变保持一致
_GRADIENT = (
    (0.0, "#F7FAFF"),
    (0.55, "#F2F6FC"),
    (1.0, "#EDF2FA"),
)

#: 原版 PCL 风格的背景渐变（上白下浅蓝）
_PCL_GRADIENT = (
    (0.0, "#FFFFFF"),
    (0.55, "#F3F8FF"),
    (1.0, "#EAF2FE"),
)


class Backdrop(QWidget):
    """铺满窗口的背景层：默认渐变 + 可选图片/GIF + 可选颜色。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QColor()
        self._image_path = ""
        self._gradient = _GRADIENT
        self._movie: QMovie | None = None
        self._pixmap: QPixmap | None = None
        self._scaled: QPixmap | None = None
        self._scaled_key: tuple[int, int, int, int] | None = None
        #: PCL 风格的外壳：内容收进一层圆角卡片，四周留出阴影（FormMain.xaml Margin 10 + 8）
        self._shell = False
        self._inset = 18
        self._radius = 6
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    # ---------------------------------------------------------------- 设置
    def set_appearance(self, color: str = "", image: str = "", style: str = "default") -> None:
        """``color`` 为 ``#RRGGBB``（空 = 无色），``image`` 为 PNG/GIF 路径（空 = 无图）。"""
        self._color = QColor(color) if color else QColor()
        self._gradient = _PCL_GRADIENT if str(style or "").lower() == "pcl" else _GRADIENT
        if image != self._image_path:
            self._image_path = image
            self._load_image(image)
        self.update()

    def shutdown(self) -> None:
        """窗口退出前停止 GIF，避免 QMovie 在对象销毁后仍触发回调。"""
        self._stop_movie()

    def set_shell(self, enabled: bool, inset: int = 18, radius: int = 6) -> None:
        """PCL 风格外壳：内容收进 ``inset`` 内边距的圆角卡片，并在外侧画柔和阴影。"""
        if (enabled, inset, radius) == (self._shell, self._inset, self._radius):
            return
        self._shell = bool(enabled)
        self._inset = int(inset)
        self._radius = int(radius)
        self.update()

    def _load_image(self, path: str) -> None:
        self._stop_movie()
        self._pixmap = None
        self._scaled = None
        self._scaled_key = None
        if not path:
            return
        if path.lower().endswith(".gif"):
            movie = QMovie(path)
            if movie.isValid():
                movie.setCacheMode(QMovie.CacheMode.CacheAll)
                movie.frameChanged.connect(self.update)
                self._movie = movie
                movie.start()
                return
            movie.deleteLater()
        pixmap = QPixmap(path)
        if not pixmap.isNull():
            self._pixmap = pixmap
        self.update()

    def _stop_movie(self) -> None:
        if self._movie is not None:
            self._movie.stop()
            self._movie.deleteLater()
            self._movie = None

    # ---------------------------------------------------------------- 绘制
    def _current_pixmap(self) -> QPixmap | None:
        if self._movie is not None:
            frame = self._movie.currentPixmap()
            if not frame.isNull():
                return frame
        return self._pixmap

    def _cover_pixmap(self, pixmap: QPixmap, size: QSize) -> QPixmap | None:
        """等比放大到铺满目标区域，并把缩放结果按帧缓存，避免 GIF 每帧重复解码。"""
        if size.width() <= 0 or size.height() <= 0:
            return None
        frame_index = self._movie.currentFrameNumber() if self._movie is not None else 0
        key = (size.width(), size.height(), frame_index, pixmap.cacheKey())
        if self._scaled is not None and self._scaled_key == key:
            return self._scaled
        scaled = pixmap.scaled(
            size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._scaled = scaled
        self._scaled_key = key
        return scaled

    def _paint_shadow(self, painter: QPainter, rect) -> None:
        """在圆角卡片外侧叠出柔和阴影（PCL 的 PanBack 阴影区）。"""
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        for step in range(self._inset, 1, -1):
            painter.setBrush(QColor(0, 0, 0, 3))
            painter.drawRoundedRect(
                QRectF(rect).adjusted(-step, -step + 2, step, step + 2),
                self._radius + step,
                self._radius + step,
            )
        painter.setBrush(QColor(0, 0, 0, 18))
        painter.drawRoundedRect(
            QRectF(rect).adjusted(-1, 1, 1, 3),
            self._radius + 1,
            self._radius + 1,
        )
        painter.restore()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self._shell:
            # PCL 风格：内容收进圆角卡片，外圈留给阴影
            rect = self.rect().adjusted(
                self._inset, self._inset, -self._inset, -self._inset
            )
            self._paint_shadow(painter, rect)
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(rect), self._radius, self._radius)
            painter.setClipPath(clip)
        else:
            rect = self.rect()

        # 1) 默认渐变做兜底（图片 PNG/GIF 带透明通道时不会露出黑色底）
        gradient = QLinearGradient(
            rect.left(), rect.top(), rect.left() + rect.width() * 0.35, rect.bottom()
        )
        for stop, color in self._gradient:
            gradient.setColorAt(stop, QColor(color))
        painter.fillRect(rect, gradient)

        # 2) 背景图片（等比铺满，居中裁切）
        frame = self._current_pixmap()
        if frame is not None:
            scaled = self._cover_pixmap(frame, rect.size())
            if scaled is not None:
                painter.drawPixmap(
                    QPoint(
                        rect.left() + (rect.width() - scaled.width()) // 2,
                        rect.top() + (rect.height() - scaled.height()) // 2,
                    ),
                    scaled,
                )
                # 3) 图片 + 颜色：蒙上一层很淡的纯色
                if self._color.isValid():
                    painter.fillRect(rect, QColor(
                        self._color.red(), self._color.green(), self._color.blue(), TINT_ALPHA
                    ))
        elif self._color.isValid():
            # 只有颜色：纯色铺满（渐变仍在最底层，仅作为透明兜底）
            painter.fillRect(rect, self._color)

        # 4) 圆角卡片描边，让边缘在浅色背景上也能看出边界
        if self._shell:
            pen = QPen(QColor(0, 0, 0, 26))
            pen.setWidth(1)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(
                QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5),
                self._radius,
                self._radius,
            )

        painter.end()
