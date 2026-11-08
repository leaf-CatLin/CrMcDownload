"""无边框窗口辅助：标题栏拖动、双击最大化、Windows 原生缩放与圆角。

设计要点：
- 窗口使用 ``FramelessWindowHint`` 去掉系统标题栏；
- 通过 ``WM_NCHITTEST`` 在窗口边缘返回缩放热区，配合 ``WS_THICKFRAME``
  获得与原生窗口一致的拖拽缩放、Aero Snap 与阴影；
- 标题栏拖动走 ``startSystemMove()``，可直接触发系统贴边分屏；
- 在 Windows 11 上额外请求 DWM 圆角，让无边框窗口更精致。
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtWidgets import QApplication, QFrame

#: 边缘缩放热区宽度（像素）
RESIZE_EDGE = 6

#: WM_NCHITTEST 返回值
_HT_LEFT, _HT_RIGHT, _HT_TOP, _HT_BOTTOM = 10, 11, 12, 15
_HT_TOPLEFT, _HT_TOPRIGHT = 13, 14
_HT_BOTTOMLEFT, _HT_BOTTOMRIGHT = 16, 17


class TitleBar(QFrame):
    """可拖动 / 双击最大化的自定义标题栏。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("topbar")
        self._drag_origin: QPoint | None = None

    def mousePressEvent(self, event):  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            # 先记录起点：只有真正移动后才进入系统拖动，避免吞掉双击最大化
            self._drag_origin = event.globalPosition().toPoint()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt 命名
        if (
            self._drag_origin is not None
            and event.buttons() & Qt.MouseButton.LeftButton
            and (event.globalPosition().toPoint() - self._drag_origin).manhattanLength()
            >= QApplication.startDragDistance()
        ):
            handle = self.window().windowHandle()
            if handle is not None and hasattr(handle, "startSystemMove"):
                self._drag_origin = None
                handle.startSystemMove()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt 命名
        self._drag_origin = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            if window.isMaximized():
                window.showNormal()
            else:
                window.showMaximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


def enable_native_chrome(window) -> None:
    """给无边框窗口补上原生缩放边框、阴影和圆角（仅 Windows 生效）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = wintypes.HWND(int(window.winId()))
        user32 = ctypes.windll.user32
        gwl_style = -16
        ws_thickframe = 0x00040000
        style = user32.GetWindowLongW(hwnd, gwl_style)
        user32.SetWindowLongW(hwnd, gwl_style, style | ws_thickframe)

        # Windows 11：DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_ROUND = 2
        try:
            dwmapi = ctypes.windll.dwmapi
            preference = ctypes.c_int(2)
            dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(preference), ctypes.sizeof(preference)
            )
        except Exception:
            pass

        user32.SetWindowPos(
            hwnd,
            wintypes.HWND(0),
            0,
            0,
            0,
            0,
            0x0001 | 0x0002 | 0x0004 | 0x0020,  # NOSIZE | NOMOVE | NOZORDER | FRAMECHANGED
        )
    except Exception:
        # 原生样式不可用时保持 Qt 的无边框实现，不影响功能
        return


def native_hit_test(window, message) -> int | None:
    """处理 ``WM_NCHITTEST``：返回缩放方向码，非边缘返回 ``None``。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        msg = wintypes.MSG.from_address(int(message))
    except Exception:
        return None
    if msg.message != 0x0084 or not window.isVisible():
        return None
    if window.isMaximized() or window.isFullScreen():
        return None

    point = window.mapFromGlobal(QPoint(msg.pt.x, msg.pt.y))
    width, height = window.width(), window.height()
    left = point.x() < RESIZE_EDGE
    right = point.x() >= width - RESIZE_EDGE
    top = point.y() < RESIZE_EDGE
    bottom = point.y() >= height - RESIZE_EDGE

    if top and left:
        return _HT_TOPLEFT
    if top and right:
        return _HT_TOPRIGHT
    if bottom and left:
        return _HT_BOTTOMLEFT
    if bottom and right:
        return _HT_BOTTOMRIGHT
    if left:
        return _HT_LEFT
    if right:
        return _HT_RIGHT
    if top:
        return _HT_TOP
    if bottom:
        return _HT_BOTTOM
    return None


__all__ = ["TitleBar", "enable_native_chrome", "native_hit_test"]
