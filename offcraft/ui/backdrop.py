"""可自定义的窗口背景层。

自上而下叠三层：

1. 默认渐变（最底层，保证背景图带透明通道时不会露出窗口后方的黑色）；
2. 背景图片（PNG / GIF，等比铺满窗口，居中裁切）；
3. 颜色蒙版：图片 + 颜色时在图片上蒙一层很淡的纯色；只有颜色时直接铺满。

界面控件是白底实色卡片（见 :mod:`offcraft.ui.tokens` 的 ``elev``），会与这一层
形成可辨识的边界。渐变**预渲染成 QPixmap**：``paintEvent`` 只做一次 ``drawPixmap``，
避免每次重绘都重建 QLinearGradient（2560×1440 下这一项就要 5.9ms）。
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QSize, Qt
from PyQt6.QtGui import (
    QColor,
    QLinearGradient,
    QMovie,
    QPainter,
    QPixmap,
)
from PyQt6.QtWidgets import QWidget

from offcraft.ui.tokens import TOKENS, TOKENS_PCL

__all__ = ["Backdrop"]

#: 颜色蒙版不透明度（0-255）：“很淡”的一层色膜
TINT_ALPHA = 44


def _gradient_stops(style: str = "default") -> tuple[tuple[float, str], ...]:
    """按风格给出背景渐变（与 token 的 ``bg`` 同色系）。"""
    if str(style or "").lower() == "pcl":
        bg = TOKENS_PCL["color"]["bg"]
        return ((0.0, "#FFFFFF"), (0.55, "#F3F8FF"), (1.0, bg))
    bg = TOKENS["color"]["bg"]
    return ((0.0, "#E6EDF8"), (0.55, bg), (1.0, "#B7C6DE"))


#: 兼容旧引用
_GRADIENT = _gradient_stops("default")
#: 原版 PCL 风格的背景渐变（上白下浅蓝）
_PCL_GRADIENT = _gradient_stops("pcl")


class Backdrop(QWidget):
    """铺满窗口的背景层：默认渐变 + 可选图片/GIF + 可选颜色。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QColor()
        self._image_path = ""
        self._gradient = _GRADIENT
        self._style = "default"
        self._movie: QMovie | None = None
        self._pixmap: QPixmap | None = None
        self._scaled: QPixmap | None = None
        self._scaled_key: tuple[int, int, int, int] | None = None
        #: 预渲染的渐变底（尺寸变化时重建）
        self._base: QPixmap | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    # ---------------------------------------------------------------- 设置
    def set_appearance(self, color: str = "", image: str = "", style: str = "default") -> None:
        """``color`` 为 ``#RRGGBB``（空 = 无色），``image`` 为 PNG/GIF 路径（空 = 无图）。"""
        self._color = QColor(color) if color else QColor()
        style = str(style or "default").lower()
        if style != self._style:
            self._style = style
            self._gradient = _PCL_GRADIENT if style == "pcl" else _GRADIENT
            self._base = None
        if image != self._image_path:
            self._image_path = image
            self._load_image(image)
        self.update()

    def shutdown(self) -> None:
        """窗口退出前停止 GIF，避免 QMovie 在对象销毁后仍触发回调。"""
        self._stop_movie()

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

    def _base_pixmap(self, size: QSize) -> QPixmap | None:
        """预渲染整窗渐变；只在尺寸或风格变化时重建一次。"""
        if size.width() <= 0 or size.height() <= 0:
            return None
        if self._base is not None and self._base.size() == size:
            return self._base
        base = QPixmap(size)
        painter = QPainter(base)
        gradient = QLinearGradient(0, 0, size.width() * 0.35, size.height())
        for stop, color in self._gradient:
            gradient.setColorAt(stop, QColor(color))
        painter.fillRect(base.rect(), gradient)
        painter.end()
        self._base = base
        return base

    def _cover_pixmap(self, pixmap: QPixmap, size: QSize) -> QPixmap | None:
        """等比放大到铺满目标区域，并把缩放结果按帧缓存，避免 GIF 每帧重复解码。

        缩放用 ``FastTransformation``：背景图是「铺满后裁切」的展示用途，
        平滑缩放在整窗尺寸下每帧要 10ms 以上，视觉收益却几乎为零。
        """
        if size.width() <= 0 or size.height() <= 0:
            return None
        frame_index = self._movie.currentFrameNumber() if self._movie is not None else 0
        key = (size.width(), size.height(), frame_index, pixmap.cacheKey())
        if self._scaled is not None and self._scaled_key == key:
            return self._scaled
        scaled = pixmap.scaled(
            size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.FastTransformation,
        )
        self._scaled = scaled
        self._scaled_key = key
        return scaled

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        rect = self.rect()

        # 1) 预渲染的渐变底（图片带透明通道时不会露出黑色）
        base = self._base_pixmap(rect.size())
        if base is not None:
            painter.drawPixmap(0, 0, base)
        else:
            painter.fillRect(rect, QColor(self._gradient[-1][1]))

        frame = self._current_pixmap()
        if frame is not None:
            # 2) 背景图片（等比铺满，居中裁切）
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

        painter.end()
