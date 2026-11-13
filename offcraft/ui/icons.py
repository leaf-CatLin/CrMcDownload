"""内置 SVG 图标库：简约线条风格，无外部图片依赖。

所有图标使用 24×24 视图框、1.7px 圆头描边，渲染时按需要着色，
因此在深浅背景、选中/未选中状态下都能保持统一的视觉语言。
"""

from __future__ import annotations

from functools import lru_cache

from PyQt6.QtCore import QByteArray, QRectF, Qt
from PyQt6.QtGui import QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

#: 图标名 -> SVG 路径片段（仅描边，不含 fill）
_PATHS = {
    "home": '<path d="M4 10.6 12 4l8 6.6"/><path d="M6.2 9.4V19.5h11.6V9.4"/>'
    '<path d="M10 19.5v-5h4v5"/>',
    "globe": '<circle cx="12" cy="12" r="8"/>'
    '<path d="M4.4 12h15.2"/>'
    '<path d="M12 4c2.4 2.9 2.4 13.1 0 16M12 4c-2.4 2.9-2.4 13.1 0 16"/>',
    "download": '<path d="M12 4.2v10.2"/><path d="m8.2 10.6 3.8 3.8 3.8-3.8"/>'
    '<path d="M5 19.4h14"/>',
    "settings": '<circle cx="12" cy="12" r="3.1"/>'
    '<path d="M12 3.6v2.3M12 18.1v2.3M3.6 12h2.3M18.1 12h2.3"/>'
    '<path d="m6.2 6.2 1.6 1.6M16.2 16.2l1.6 1.6M17.8 6.2l-1.6 1.6M7.8 16.2l-1.6 1.6"/>',
    "blocks": '<rect x="4" y="4" width="7" height="7" rx="2"/>'
    '<rect x="13" y="4" width="7" height="7" rx="2"/>'
    '<rect x="4" y="13" width="7" height="7" rx="2"/>'
    '<rect x="13" y="13" width="7" height="7" rx="2"/>',
    "minimize": '<path d="M5.5 12h13"/>',
    "maximize": '<rect x="5.5" y="5.5" width="13" height="13" rx="2.2"/>',
    "restore": '<rect x="8.2" y="4.8" width="11" height="11" rx="2"/>'
    '<path d="M4.8 8.2v9a2 2 0 0 0 2 2h9"/>',
    "close": '<path d="m6.4 6.4 11.2 11.2M17.6 6.4 6.4 17.6"/>',
    "search": '<circle cx="10.6" cy="10.6" r="5.9"/><path d="m15 15 4.4 4.4"/>',
    "chevron-down": '<path d="m7 9.6 5 5 5-5"/>',
    "chevron-up": '<path d="m7 14.4 5-5 5 5"/>',
    "chevron-right": '<path d="m9.6 7 5 5-5 5"/>',
    "chevron-left": '<path d="m14.4 7-5 5 5 5"/>',
    "palette": '<path d="M12 3.9a8.1 8.1 0 1 0 0 16.2h1a2 2 0 0 0 1.5-3.4 2 2 0 0 1 1.5-3.4h1.4A3.9 3.9 0 0 0 20.2 10a8.1 8.1 0 0 0-8.2-6.1Z"/>'
    '<circle cx="8" cy="11.1" r="1"/><circle cx="11" cy="7.6" r="1"/><circle cx="15" cy="9.1" r="1"/>',
    "check": '<path d="m5 12.6 4.4 4.3 9.6-9.8"/>',
    "refresh": '<path d="M19.6 12a7.6 7.6 0 1 1-2.2-5.4"/><path d="M18.6 3.6v4.6h-4.6"/>',
    "cube": '<path d="m12 3.2 8 4.5v8.6l-8 4.5-8-4.5V7.7l8-4.5Z"/>'
    '<path d="M4 7.7 12 12l8-4.3M12 12v8.8"/>',
    "package": '<path d="M5.2 7.8h13.6l-1 3.2v7.8H6.2v-7.8l-1-3.2Z"/>'
    '<path d="M9 7.8V6.2a3 3 0 0 1 6 0v1.6"/>',
    "layers": '<path d="m12 3.4 8.4 4.5-8.4 4.5-8.4-4.5 8.4-4.5Z"/>'
    '<path d="m3.6 12 8.4 4.5 8.4-4.5M3.6 16.1l8.4 4.5 8.4-4.5"/>',
    "image": '<rect x="4" y="5" width="16" height="14" rx="2.2"/>'
    '<circle cx="9" cy="9.8" r="1.5"/><path d="m5.2 17.6 4.6-4.6 3 3 2.6-1.8 3.4 3.4"/>',
    "sun": '<circle cx="12" cy="12" r="3.9"/>'
    '<path d="M12 3.2v2.1M12 18.7v2.1M3.2 12h2.1M18.7 12h2.1"/>'
    '<path d="m5.7 5.7 1.5 1.5M16.8 16.8l1.5 1.5M18.3 5.7l-1.5 1.5M7.2 16.8l-1.5 1.5"/>',
    "cup": '<path d="M5.6 8.2h11.2v4.7a4.9 4.9 0 0 1-4.9 4.9h-1.4a4.9 4.9 0 0 1-4.9-4.9V8.2Z"/>'
    '<path d="M16.8 9.2H19a2.5 2.5 0 0 1 0 5h-2.2"/>'
    '<path d="M8.4 3.8c.9 1 .9 2 0 3M12 3.8c.9 1 .9 2 0 3"/>',
    "play": '<path d="M8.2 5.4v13.2L18.6 12 8.2 5.4Z"/>',
    "stop": '<rect x="7" y="7" width="10" height="10" rx="2"/>',
    "folder": '<path d="M4 7.4a2 2 0 0 1 2-2h3.8l2 2H18a2 2 0 0 1 2 2v7.2a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7.4Z"/>',
    "person": '<circle cx="12" cy="8.6" r="3.6"/>'
    '<path d="M5.4 19.6a6.6 6.6 0 0 1 13.2 0"/>',
    "puzzle": '<path d="M9.8 4.4h4.4v2.4a2.1 2.1 0 0 0 0 4.2v2.4h-2.4a2.1 2.1 0 0 0-4.2 0H5.2v-2.4a2.1 2.1 0 0 0 0-4.2V4.4h4.6Z"/>'
    '<path d="M9.8 16.1v3.5h4.4v-2.4a2.1 2.1 0 0 1 4.2 0h2.4v-5.9"/>',
}

#: 个别图标需要更粗的描边（例如复选框里的小对勾）
_STROKE_WIDTHS = {"check": 2.8, "play": 2.0, "stop": 2.0}


def _svg_source(name: str, color: str) -> str:
    body = _PATHS[name]
    width = _STROKE_WIDTHS.get(name, 1.7)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'fill="none" stroke="{color}" stroke-width="{width}" '
        'stroke-linecap="round" stroke-linejoin="round">'
        f"{body}</svg>"
    )


@lru_cache(maxsize=256)
def icon_pixmap(name: str, size: int = 18, color: str = "#5B6678", ratio: int = 2) -> QPixmap:
    """把线条图标渲染成高分辨率位图（自动带 devicePixelRatio）。"""
    if name not in _PATHS:
        name = "blocks"
    renderer = QSvgRenderer(QByteArray(_svg_source(name, color).encode("utf-8")))
    pixmap = QPixmap(size * ratio, size * ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    renderer.render(painter, QRectF(0, 0, size * ratio, size * ratio))
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def icon(name: str, size: int = 18, color: str = "#5B6678") -> QIcon:
    """返回可直接用于按钮/托盘的单色线条图标。"""
    return QIcon(icon_pixmap(name, size, color))


__all__ = ["ICON_NAMES", "icon", "icon_pixmap"]

#: 可用图标名集合（便于自检）
ICON_NAMES = tuple(sorted(_PATHS))
