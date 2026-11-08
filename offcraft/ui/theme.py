"""界面主题：PCL 风格的浅色卡片、蓝色强调色与程序化图标。

不依赖任何图片资源：版本图标/项目图标都用 QPainter 画出来，
因此打包后也不会缺资源。
"""

from __future__ import annotations

import os
import tempfile
import time

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap, QPolygonF

__all__ = [
    "ACCENT",
    "ACCENT_DARK",
    "ACCENT_SOFT",
    "BG",
    "BORDER",
    "CARD",
    "DANGER",
    "MUTED",
    "OK",
    "TEXT",
    "WARN",
    "app_qss",
    "block_pixmap",
    "human_count",
    "human_size",
    "human_date",
    "initial_pixmap",
    "loader_color",
    "loader_pixmap",
]

BG = "#F2F6FC"
CARD = "#FFFFFF"
BORDER = "#E5EBF4"
TEXT = "#1B2432"
MUTED = "#7A8698"
ACCENT = "#3A76E8"
ACCENT_DARK = "#2558B8"
ACCENT_SOFT = "#EAF1FF"
OK = "#2E9E6B"
WARN = "#D18B22"
DANGER = "#D24B45"

#: 毛玻璃：半透明白 + 亮边，让可自定义背景从卡片下透出来。
#: 内层控件（输入框/按钮）叠在卡片上时两层 alpha 会相乘，若各自都接近不透明，
#: 背景就被洗成白色，因此每一层都要明显更透。
GLASS = "rgba(255, 255, 255, 0.72)"
GLASS_SOFT = "rgba(247, 249, 253, 0.60)"
GLASS_EDGE = "rgba(255, 255, 255, 0.84)"
GLASS_INPUT = "rgba(255, 255, 255, 0.56)"
GLASS_HOVER = "rgba(242, 246, 254, 0.68)"
GLASS_PRESSED = "rgba(226, 234, 248, 0.80)"

#: 加载器/内容类型的标识色
LOADER_COLORS = {
    "Fabric": "#D9A441",
    "Forge": "#5A6B8C",
    "NeoForge": "#C96A3E",
    "Quilt": "#9B6BD6",
    "原版": "#4C8C6A",
    "未知": "#8A93A3",
}

PALETTE = [
    "#2D6CDF",
    "#2E9E6B",
    "#C96A3E",
    "#7B5BD6",
    "#C4477C",
    "#3B93B8",
    "#8C7A3F",
    "#4C6B8A",
]

#: 加载器徽标上的简称
LOADER_MARKS = {
    "Fabric": "Fa",
    "NeoForge": "NF",
    "Forge": "Fo",
    "Quilt": "Q",
    "原版": "MC",
}


def _seed_value(seed: str) -> int:
    value = 0
    for char in seed or "?":
        value = (value * 131 + ord(char)) & 0xFFFFFFFF
    return value


def loader_color(loader: str) -> str:
    return LOADER_COLORS.get(loader or "", PALETTE[_seed_value(loader) % len(PALETTE)])


def block_pixmap(seed: str, size: int = 64, badge: str = "") -> QPixmap:
    """画一个等轴测方块图标（草地方块风格），颜色由 ``seed`` 决定。"""
    ratio = 2
    pix = QPixmap(size * ratio, size * ratio)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(ratio, ratio)

    base = QColor(PALETTE[_seed_value(seed) % len(PALETTE)])
    top = QColor(base).lighter(118)
    left = QColor(base).darker(112)
    right = QColor(base).darker(128)

    inset = size * 0.1
    width = size - inset * 2
    height = width * 0.56
    cx = size / 2
    top_y = size * 0.2
    mid_y = top_y + height
    bottom_y = mid_y + height * 0.92

    top_poly = QPolygonF(
        [
            QPointF(cx, top_y),
            QPointF(cx + width / 2, top_y + height / 2),
            QPointF(cx, top_y + height),
            QPointF(cx - width / 2, top_y + height / 2),
        ]
    )
    left_poly = QPolygonF(
        [
            QPointF(cx - width / 2, top_y + height / 2),
            QPointF(cx, top_y + height),
            QPointF(cx, bottom_y),
            QPointF(cx - width / 2, mid_y),
        ]
    )
    right_poly = QPolygonF(
        [
            QPointF(cx + width / 2, top_y + height / 2),
            QPointF(cx, top_y + height),
            QPointF(cx, bottom_y),
            QPointF(cx + width / 2, mid_y),
        ]
    )

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(top)
    painter.drawPolygon(top_poly)
    painter.setBrush(left)
    painter.drawPolygon(left_poly)
    painter.setBrush(right)
    painter.drawPolygon(right_poly)

    # 顶部高光
    gradient = QLinearGradient(cx - width / 2, top_y, cx + width / 2, top_y + height)
    gradient.setColorAt(0, QColor(255, 255, 255, 60))
    gradient.setColorAt(1, QColor(255, 255, 255, 0))
    painter.setBrush(gradient)
    painter.drawPolygon(top_poly)

    if badge:
        painter.setBrush(QColor("#FFFFFF"))
        painter.setPen(Qt.PenStyle.NoPen)
        radius = size * 0.17
        painter.drawEllipse(QRectF(size - radius * 2 - size * 0.02, size - radius * 2 - size * 0.02, radius * 2, radius * 2))
        painter.setPen(QColor(loader_color(badge)))
        font = QFont("Microsoft YaHei UI", int(size * 0.16))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            QRectF(size - radius * 2 - size * 0.02, size - radius * 2 - size * 0.02, radius * 2, radius * 2),
            Qt.AlignmentFlag.AlignCenter,
            badge[:1].upper(),
        )
    painter.end()
    pix.setDevicePixelRatio(ratio)
    return pix


def initial_pixmap(text: str, size: int = 44, seed: str = "") -> QPixmap:
    """圆角方块 + 首字母，用于模组/整合包图标占位。"""
    ratio = 2
    pix = QPixmap(size * ratio, size * ratio)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(ratio, ratio)
    base = QColor(PALETTE[_seed_value(seed or text) % len(PALETTE)])
    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0, base.lighter(112))
    gradient.setColorAt(1, base.darker(118))
    painter.setBrush(gradient)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.24, size * 0.24)
    painter.setPen(QColor("#FFFFFF"))
    font = QFont("Microsoft YaHei UI", int(size * 0.38))
    font.setBold(True)
    painter.setFont(font)
    letter = (text or "?").strip()[:1].upper() or "?"
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, letter)
    painter.end()
    pix.setDevicePixelRatio(ratio)
    return pix


def loader_pixmap(name: str, size: int = 44, dimmed: bool = False) -> QPixmap:
    """加载器徽标：圆角方块 + 简称（Fabric→Fa / NeoForge→NF / Forge→Fo）。"""
    ratio = 2
    pix = QPixmap(size * ratio, size * ratio)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(ratio, ratio)
    base = QColor(loader_color(name))
    if dimmed:
        base = QColor(base).lighter(150)
    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0, base.lighter(112))
    gradient.setColorAt(1, base.darker(118))
    painter.setBrush(gradient)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.26, size * 0.26)
    text = LOADER_MARKS.get(name or "") or (str(name or "?")[:2])
    painter.setPen(QColor("#FFFFFF") if not dimmed else QColor("#F3F6FA"))
    font = QFont("Microsoft YaHei UI", int(size * 0.34))
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, text)
    painter.end()
    pix.setDevicePixelRatio(ratio)
    return pix


def human_size(value: int | float) -> str:
    size = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.0f} GiB"


def human_count(value: int | float) -> str:
    number = float(value or 0)
    if number >= 100_000_000:
        return f"{number / 100_000_000:.1f} 亿"
    if number >= 10_000:
        return f"{number / 10_000:.1f} 万"
    return str(int(number))


def human_date(stamp: float | str) -> str:
    if isinstance(stamp, str):
        return stamp[:10].replace("T", " ") or "-"
    if not stamp:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))


def _qss_icon(name: str, color: str, size: int = 14) -> str:
    """把内置线条图标渲染成 PNG 缓存文件，供 QSS 的 image: url() 使用。

    QSS 的 ::down-arrow / ::up-arrow / indicator 只接受图片路径，而项目本身
    不携带图片资源，因此在首次生成样式表时用内置 SVG 渲染一份到临时目录。
    """
    try:
        from PyQt6.QtWidgets import QApplication

        if QApplication.instance() is None:
            return ""
        from offcraft.ui.icons import icon_pixmap

        path = os.path.join(
            tempfile.gettempdir(), "offcraft-ui", f"{name}-{color.strip('#')}.png"
        )
        if not os.path.isfile(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            icon_pixmap(name, size, color, ratio=2).save(path, "PNG")
        return path.replace("\\", "/")
    except Exception:
        return ""


def app_qss() -> str:
    arrow_down = _qss_icon("chevron-down", "#6B7688", 14)
    arrow_up = _qss_icon("chevron-up", "#6B7688", 14)
    check = _qss_icon("check", "#FFFFFF", 16)
    down_arrow = (
        f"QComboBox::down-arrow {{ image: url({arrow_down}); width: 13px; height: 13px; }}"
        if arrow_down
        else ""
    )
    spin_arrows = ""
    if arrow_up and arrow_down:
        spin_arrows = (
            f"QSpinBox::up-arrow {{ image: url({arrow_up}); width: 11px; height: 11px; }}"
            f"QSpinBox::down-arrow {{ image: url({arrow_down}); width: 11px; height: 11px; }}"
        )
    check_icon = f"image: url({check});" if check else ""
    return f"""
QWidget {{
    background: transparent;
    color: {TEXT};
    font-family: "Microsoft YaHei UI";
    font-size: 10pt;
}}
QMainWindow, QDialog {{
    background: qlineargradient(x1:0, y1:0, x2:0.35, y2:1,
        stop:0 #F7FAFF, stop:0.55 {BG}, stop:1 #EDF2FA);
}}
QFrame#topbar {{
    background: rgba(255, 255, 255, 0.76);
    border-bottom: 1px solid rgba(229, 235, 244, 0.85);
}}
QFrame#sidebar {{
    background: rgba(255, 255, 255, 0.76);
    border-right: 1px solid rgba(229, 235, 244, 0.85);
}}
QFrame#card {{
    background: qlineargradient(x1:0, y1:0, x2:0.6, y2:1,
        stop:0 rgba(255, 255, 255, 0.86), stop:1 rgba(255, 255, 255, 0.64));
    border: 1px solid {GLASS_EDGE};
    border-radius: 14px;
}}
QFrame#softCard {{
    background: {GLASS_SOFT};
    border: 1px solid {GLASS_EDGE};
    border-radius: 12px;
}}
QFrame#tile {{
    background: {GLASS_SOFT};
    border: 1px solid {GLASS_EDGE};
    border-radius: 12px;
}}
QFrame#tile:hover {{
    background: rgba(234, 241, 255, 0.72);
    border-color: #C9D8F5;
}}
QFrame#installHead {{
    background: {GLASS};
    border: 1px solid {GLASS_EDGE};
    border-radius: 12px;
}}
QFrame#loaderRow {{
    background: {GLASS_SOFT};
    border: 1px solid {GLASS_EDGE};
    border-radius: 12px;
}}
QFrame#loaderRow:hover {{
    background: {GLASS_HOVER};
    border-color: #C9D8F5;
}}
QFrame#loaderRow[picked="true"] {{
    background: rgba(234, 241, 255, 0.82);
    border: 2px solid {ACCENT};
}}
QFrame#loaderRow[dimmed="true"] {{
    background: rgba(244, 246, 249, 0.55);
    border-color: #E8ECF3;
}}
QLabel#dimTitle {{ color: #AEB6C4; font-size: 11pt; font-weight: 700; }}
QLabel#dimText {{ color: #B6BDC9; font-size: 8.8pt; }}
QLineEdit#dirName {{
    font-size: 11.5pt;
    font-weight: 700;
    padding: 3px 8px;
}}
QLabel#stepBadge {{
    background: rgba(238, 243, 251, 0.70);
    color: #5E6A7C;
    border-radius: 8px;
    padding: 2px 8px;
    font-size: 8.5pt;
}}
QLabel#brand {{ font-size: 14pt; font-weight: 700; color: {TEXT}; }}
QLabel#brandSub {{ font-size: 8pt; color: {MUTED}; }}
QLabel#title {{ font-size: 19pt; font-weight: 700; }}
QLabel#h2 {{ font-size: 13pt; font-weight: 700; }}
QLabel#h3 {{ font-size: 11pt; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; font-size: 9pt; }}
QLabel#mutedSmall {{ color: {MUTED}; font-size: 8pt; }}
QLabel#heroName {{ font-size: 20pt; font-weight: 700; }}
QLineEdit#heroName {{
    font-size: 11pt;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 8px;
}}
QComboBox#heroVersion {{
    font-size: 13pt;
    font-weight: 700;
    padding: 4px 10px;
    border-radius: 10px;
}}
QComboBox#heroVersion QAbstractItemView {{ font-size: 10pt; }}
QPushButton#heroMenu {{
    font-size: 16pt;
    font-weight: 700;
    color: {MUTED};
    padding: 0px;
    border-radius: 10px;
}}
QPushButton#heroMenu:hover {{ color: {TEXT}; }}
QLabel#versionTag {{ color: #B4BCC9; font-size: 8pt; }}
QListWidget#homeList {{ background: transparent; }}
QListWidget#homeList::item {{ padding: 6px 4px; border-radius: 7px; }}
QLabel#status {{ color: {MUTED}; font-size: 9pt; }}
QLabel#error {{ color: {DANGER}; font-size: 9pt; }}
QLabel#link {{ color: {ACCENT}; font-size: 9pt; }}
/* 无边框窗口的自定义窗口按钮 */
QPushButton#winBtn {{
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 0px;
}}
QPushButton#winBtn:hover {{ background: rgba(238, 243, 252, 0.80); }}
QPushButton#winBtn:pressed {{ background: rgba(226, 234, 248, 0.88); }}
QPushButton#winClose {{
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 0px;
}}
QPushButton#winClose:hover {{ background: #FDE9E8; }}
QPushButton#winClose:pressed {{ background: #F8D8D6; }}
/* 小图标按钮（详情收起、颜色样本等） */
QPushButton#iconBtn {{
    background: transparent;
    border: none;
    border-radius: 7px;
    padding: 0px;
}}
QPushButton#iconBtn:hover {{ background: rgba(238, 243, 252, 0.80); }}
QPushButton#iconBtn:pressed {{ background: rgba(226, 234, 248, 0.88); }}
QPushButton {{
    background: {GLASS_INPUT};
    border: 1px solid rgba(229, 235, 244, 0.95);
    border-radius: 9px;
    padding: 7px 14px;
}}
QPushButton:hover {{ background: {GLASS_HOVER}; border-color: #C9D8F5; }}
QPushButton:pressed {{ background: {GLASS_PRESSED}; }}
QPushButton:disabled {{ color: #AEB6C4; background: rgba(246, 248, 251, 0.50); border-color: rgba(229, 235, 244, 0.75); }}
QPushButton#nav {{
    background: transparent;
    border: none;
    border-radius: 9px;
    text-align: left;
    padding: 10px 14px;
    color: #4B5565;
    font-size: 10.5pt;
}}
QPushButton#nav:hover {{ background: rgba(241, 245, 253, 0.78); color: {TEXT}; }}
QPushButton#topnav {{
    background: transparent;
    border: none;
    border-radius: 10px;
    text-align: center;
    padding: 8px 18px;
    color: #4B5565;
    font-size: 10.5pt;
}}
QPushButton#topnav:hover {{ background: rgba(241, 245, 253, 0.78); color: {TEXT}; }}
QPushButton#topnav:checked {{
    background: rgba(234, 241, 255, 0.72);
    color: {ACCENT};
    font-weight: 700;
}}
QPushButton#nav:checked {{
    background: rgba(234, 241, 255, 0.72);
    color: {ACCENT};
    font-weight: 700;
}}
QPushButton#accent {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #4A84F0, stop:1 {ACCENT});
    color: #FFFFFF;
    border: none;
    border-radius: 9px;
    font-weight: 700;
    padding: 9px 18px;
}}
QPushButton#accent:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #5A8FF5, stop:1 #4A84F0);
}}
QPushButton#accent:pressed {{ background: {ACCENT_DARK}; }}
QPushButton#accent:disabled {{ background: #B9CDF0; color: #F4F7FD; }}
QPushButton#accentBig {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #4A84F0, stop:1 {ACCENT});
    color: #FFFFFF;
    border: none;
    border-radius: 12px;
    font-size: 12.5pt;
    font-weight: 700;
}}
QPushButton#accentBig:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #5A8FF5, stop:1 #4A84F0);
}}
QPushButton#accentBig:pressed {{ background: {ACCENT_DARK}; }}
QPushButton#accentBig:disabled {{ background: #B9CDF0; color: #F4F7FD; }}
QPushButton#danger {{ color: {DANGER}; border-color: #F0C9C7; background: rgba(255, 246, 245, 0.62); }}
QPushButton#danger:hover {{ background: rgba(253, 236, 234, 0.78); }}
QPushButton#segment {{
    background: transparent;
    border: none;
    border-radius: 7px;
    padding: 6px 16px;
    color: {MUTED};
}}
QPushButton#segment:hover {{ color: {TEXT}; background: rgba(238, 243, 251, 0.75); }}
QPushButton#segment:checked {{
    background: rgba(255, 255, 255, 0.70);
    color: {ACCENT};
    font-weight: 700;
}}
QPushButton#tile {{
    background: rgba(247, 249, 253, 0.60);
    border: 1px solid rgba(229, 235, 244, 0.85);
    border-radius: 10px;
    text-align: left;
    padding: 10px 12px;
    color: {TEXT};
}}
QPushButton#tile:hover {{ background: rgba(234, 241, 255, 0.78); border-color: #C9D8F5; }}
QLineEdit, QSpinBox, QComboBox {{
    background: {GLASS_INPUT};
    border: 1px solid rgba(229, 235, 244, 0.95);
    border-radius: 8px;
    padding: 7px 10px;
}}
/* 选中色只给输入框：QComboBox 的选中色会传给下拉列表，导致弹层选中项变深蓝 */
QLineEdit, QSpinBox {{ selection-background-color: {ACCENT}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 24px; background: transparent; }}
{down_arrow}
QSpinBox::up-button, QSpinBox::down-button {{
    subcontrol-origin: border;
    width: 18px;
    background: transparent;
    border: none;
}}
QSpinBox::up-button {{ subcontrol-position: top right; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; }}
{spin_arrows}
/* 下拉弹层（QComboBoxPrivateContainer）是独立的顶层窗口，其样式在
   offcraft/ui/widgets.py 的 style_combo_popup 里单独设置：弹层需要不透明白底，
   否则原生透明边距会在部分显卡/远程桌面下渲染成黑边。 */
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 18px; height: 18px; border-radius: 5px;
    border: 1px solid #C7D0DE; background: {GLASS_INPUT};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT}; border-color: {ACCENT};
    {check_icon}
}}
QCheckBox::indicator:disabled {{
    background: rgba(242, 244, 248, 0.55); border-color: rgba(220, 226, 236, 0.8);
}}
QCheckBox::indicator:checked:disabled {{
    background: rgba(198, 207, 221, 0.85); border-color: rgba(198, 207, 221, 0.85);
}}
QTreeWidget, QListWidget {{
    background: transparent;
    border: none;
    outline: none;
}}
QTreeWidget::item {{ padding: 7px 4px; border-radius: 7px; }}
QTreeWidget::item:hover {{ background: rgba(244, 247, 253, 0.70); }}
QTreeWidget::item:selected {{ background: rgba(234, 241, 255, 0.82); color: {TEXT}; }}
QListWidget::item {{ border-radius: 7px; }}
QListWidget::item:hover {{ background: rgba(244, 247, 253, 0.70); }}
QListWidget::item:selected {{ background: rgba(234, 241, 255, 0.82); }}
QHeaderView::section {{
    background: transparent;
    color: {MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 4px;
    font-size: 8.5pt;
}}
QProgressBar {{
    background: rgba(232, 237, 246, 0.62);
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
    font-size: 8pt;
    color: transparent;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QSlider::groove:horizontal {{ height: 4px; background: rgba(227, 233, 242, 0.75); border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    width: 16px; height: 16px; margin: -6px 0; border-radius: 8px;
    background: rgba(255, 255, 255, 0.92); border: 2px solid {ACCENT};
}}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: rgba(201, 210, 225, 0.85); border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: rgba(174, 186, 205, 0.9); }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: rgba(201, 210, 225, 0.85); border-radius: 5px; min-width: 28px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QPlainTextEdit {{ background: #0F1520; color: #D6E2F5; border-radius: 10px; padding: 8px; }}
QMenu {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px;
}}
QMenu::item {{ padding: 7px 22px 7px 14px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT_SOFT}; }}
QToolTip {{
    background: #1F2530; color: #FFFFFF; border: none; border-radius: 6px; padding: 5px 8px;
}}
"""
