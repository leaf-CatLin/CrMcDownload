"""界面主题：由设计 token 生成的 QSS + 程序化图标。

颜色 / 字号 / 圆角 / 间距的唯一来源是 :mod:`offcraft.ui.tokens`；这里只负责把
token 展开成 QSS，并提供 ``overlay`` 这类兼容旧调用点的派生值。改一处 token
即可全局生效，也避免同一个界面出现 21 种字号、13 种圆角。

不依赖任何图片资源：版本图标/项目图标都用 QPainter 画出来，
因此打包后也不会缺资源。
"""

from __future__ import annotations

import os
import tempfile
import time
from functools import lru_cache

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap, QPolygonF

from offcraft.ui.tokens import (
    ELEVATION,
    FONT_FAMILY,
    FONT_MONO,
    RADIUS,
    SPACE,
    TOKENS,
    TOKENS_PCL,
    TYPE,
    style_tokens,
)

__all__ = [
    "ACCENT",
    "ACCENT_DARK",
    "ACCENT_SOFT",
    "BG",
    "BORDER",
    "CARD",
    "DANGER",
    "ELEVATION",
    "FONT_FAMILY",
    "FONT_MONO",
    "MUTED",
    "OK",
    "RADIUS",
    "SELECTED",
    "SELECTED_STRONG",
    "HOVER_STRONG",
    "SPACE",
    "TEXT",
    "TOKENS",
    "TYPE",
    "WARN",
    "app_qss",
    "apply_role",
    "block_pixmap",
    "full_qss",
    "human_count",
    "human_size",
    "human_date",
    "initial_pixmap",
    "loader_color",
    "loader_pixmap",
    "overlay",
    "pcl_qss",
    "style_tokens",
]

#: 兼容旧调用点的单一值别名（全部来自默认主题的 token）
BG = TOKENS["color"]["bg"]
CARD = TOKENS["color"]["surface"]
BORDER = TOKENS["color"]["border"]
TEXT = TOKENS["color"]["text"]
MUTED = TOKENS["color"]["textMuted"]
ACCENT = TOKENS["color"]["accent"]
ACCENT_DARK = TOKENS["color"]["accentDark"]
ACCENT_SOFT = TOKENS["color"]["accentSoft"]
OK = TOKENS["color"]["ok"]
WARN = TOKENS["color"]["warn"]
DANGER = TOKENS["color"]["danger"]
#: 半透明的选中/悬停色：叠在玻璃卡片或列表上仍能透出底色
SELECTED = TOKENS["color"]["selected"]
SELECTED_STRONG = TOKENS["color"]["selectedStrong"]
HOVER_STRONG = TOKENS["color"]["hover"]


def apply_role(widget, role: str):
    """按字体档（caption/label/body/bodyStrong/h3/h2/h1/hero）设置控件角色。

    QSS 里用 ``QLabel[role="h2"]`` 之类的属性选择器接管字号，从而不再需要
    每处 ``setStyleSheet("font-size: 13pt; font-weight: 700;")``。
    """
    if widget is None:
        return widget
    widget.setProperty("role", role)
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()
    return widget


def _rgb(value: str) -> tuple[int, int, int]:
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        return (0, 0, 0)
    try:
        return tuple(int(text[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (0, 0, 0)


def overlay(
    color: str,
    base: str = BG,
    *,
    tolerance: float = 3.0,
    floor: float = 0.35,
    ceiling: float = 0.94,
) -> str:
    """把一个不透明颜色换算成叠在 ``base`` 上的半透明色。

    规则：与底色相同的控件直接 100% 透明（叠上去也看不出差别）；
    颜色不同则把 alpha 调到刚好能在默认背景上保持原色（每通道误差 ≤ tolerance），
    这样默认背景下观感不变，换成自定义背景时又能透出底层，不会变成纯色块。
    """
    target = _rgb(color)
    ground = _rgb(base)
    diff = max(abs(target[index] - ground[index]) for index in range(3))
    if diff <= tolerance:
        return f"rgba({target[0]}, {target[1]}, {target[2]}, 0.00)"
    alpha = min(ceiling, max(floor, 1 - tolerance / diff))
    return f"rgba({target[0]}, {target[1]}, {target[2]}, {alpha:.2f})"

#: 旧的「毛玻璃」派生值：QSS 已改用 token 的实色底 + 1px 描边（对比度可见），
#: 这里保留常量只为兼容 ``test_theme`` 与可能的外部调用，不再参与样式生成。
GLASS = overlay("#FFFFFF")
GLASS_SOFT = overlay("#F7F9FD", floor=0.42)
GLASS_EDGE = overlay("#FFFFFF", tolerance=1.0)
GLASS_INPUT = overlay("#FFFFFF", floor=0.55)
GLASS_HOVER = overlay("#F0F5FD", floor=0.40)
GLASS_PRESSED = overlay("#E2EAF8", floor=0.50)

#: 完整样式表缓存：``setStyleSheet`` 会重刷窗口内所有控件，重复生成字符串没意义。
_QSS_CACHE: dict[str, str] = {}

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


def _block_pixmap(seed: str, size: int, badge: str) -> QPixmap:
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


def _initial_pixmap(text: str, size: int, seed: str) -> QPixmap:
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


def _loader_pixmap(name: str, size: int, dimmed: bool) -> QPixmap:
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


#: 图标工厂缓存：``block_pixmap`` 每次调用要 0.22ms，版本表一次渲染 400 行就是 90ms。
#: 参数（seed / size / badge…）完全决定输出，因此可以安全缓存；命中时拷贝一份——
#: QPixmap 是隐式共享的，拷贝几乎零成本，但能避免调用方 setDevicePixelRatio 影响缓存对象。
@lru_cache(maxsize=512)
def _cached_block(seed: str, size: int, badge: str) -> QPixmap:
    return _block_pixmap(seed, size, badge)


@lru_cache(maxsize=512)
def _cached_initial(text: str, size: int, seed: str) -> QPixmap:
    return _initial_pixmap(text, size, seed)


@lru_cache(maxsize=256)
def _cached_loader(name: str, size: int, dimmed: bool) -> QPixmap:
    return _loader_pixmap(name, size, dimmed)


def block_pixmap(seed: str, size: int = 64, badge: str = "") -> QPixmap:
    """画一个等轴测方块图标（带缓存）。"""
    return QPixmap(_cached_block(str(seed), int(size), str(badge)))


def initial_pixmap(text: str, size: int = 44, seed: str = "") -> QPixmap:
    """圆角方块 + 首字母（带缓存）。"""
    return QPixmap(_cached_initial(str(text), int(size), str(seed)))


def loader_pixmap(name: str, size: int = 44, dimmed: bool = False) -> QPixmap:
    """加载器徽标（带缓存）。"""
    return QPixmap(_cached_loader(str(name), int(size), bool(dimmed)))


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


def _font_pt(role: str) -> str:
    """把字体档位换算成 QSS 里的 ``font-size`` + ``font-weight`` 声明。"""
    size, weight = TYPE.get(role, TYPE["body"])
    return f"font-size: {size}pt; font-weight: {weight};"


def _role_rules(prefix: str = "") -> str:
    """生成 ``QLabel[role="..."]`` 一类属性选择器，替代散落的内联字号。"""
    out: list[str] = []
    for role in TYPE:
        out.append(f'{prefix}QLabel[role="{role}"] {{ {_font_pt(role)} }}')
    for role in ("label", "body", "bodyStrong", "h3"):
        out.append(f'{prefix}QPushButton[role="{role}"] {{ {_font_pt(role)} }}')
    return "\n".join(out)


def app_qss(style: str = "default") -> str:
    """按 token 生成默认（浅色）界面样式。

    所有颜色 / 字号 / 圆角 / 间距都来自 :func:`offcraft.ui.tokens.style_tokens`，
    因此不再需要为「PCL 风格」维护第二份 340 行的重复选择器：换一份 token 即可。
    """
    t = style_tokens(style)
    c = t["color"]
    r = t["radius"]
    elev_card = t["elev"]["card"]
    elev_flat = t["elev"]["flat"]
    elev_raised = t["elev"]["raised"]
    field = t.get("field", c["surface"])
    card_fill = elev_card["fill"]

    arrow_down = _qss_icon("chevron-down", c["textMuted"], 14)
    arrow_up = _qss_icon("chevron-up", c["textMuted"], 14)
    check = _qss_icon("check", c["textOnAccent"], 16)
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
/* ---------------------------------------------------------------- 基础 */
QWidget {{
    background: transparent;
    color: {c["text"]};
    font-family: {FONT_FAMILY};
    font-size: {TYPE["body"][0]}pt;
}}
/* 容器类控件一律透明，避免在卡片右侧露出默认白底 */
QStackedWidget, QScrollArea, QListView, QTableView,
QScrollArea > QWidget > QWidget {{
    background: transparent;
    border: none;
}}
/* 列表 / 表格的 viewport 由 QAbstractScrollArea 绘制，必须单独置空，
   否则 Fusion 风格会用调色板 Base（白）填满列表区域。 */
QAbstractScrollArea, QAbstractItemView {{
    background: transparent;
    border: none;
}}
QAbstractScrollArea > QWidget,
QAbstractItemView > QWidget {{
    background: transparent;
}}
/* 主窗口背景由 Backdrop 绘制；对话框自己铺一层实色，避免透明叠透明 */
QMainWindow {{ background: transparent; }}
QDialog {{ background: {c["bg"]}; }}

/* ---------------------------------------------------------------- 字体档 */
{_role_rules()}
QLabel#brand {{ {_font_pt("h2")} color: {c["text"]}; }}
QLabel#brandSub {{ {_font_pt("caption")} color: {c["textMuted"]}; }}
QLabel#pageTitle {{ {_font_pt("bodyStrong")} color: {c["text"]}; }}
QLabel#title {{ {_font_pt("h1")}; }}
QLabel#h2 {{ {_font_pt("h2")}; }}
QLabel#h3 {{ {_font_pt("h3")}; }}
QLabel#heroName {{ {_font_pt("hero")}; }}
QLabel#muted {{ color: {c["textMuted"]}; font-size: {TYPE["caption"][0]}pt; }}
QLabel#mutedSmall {{ color: {c["textMuted"]}; font-size: {TYPE["caption"][0]}pt; }}
QLabel#status {{ color: {c["textMuted"]}; font-size: {TYPE["caption"][0]}pt; }}
QLabel#error {{ color: {c["danger"]}; font-size: {TYPE["caption"][0]}pt; }}
QLabel#link {{ color: {c["accent"]}; font-size: {TYPE["caption"][0]}pt; }}
QLabel#versionTag {{ color: {c["textFaint"]}; font-size: {TYPE["caption"][0]}pt; }}
QLabel#dimTitle {{ color: {c["textFaint"]}; {_font_pt("bodyStrong")} }}
QLabel#dimText {{ color: {c["textFaint"]}; {_font_pt("caption")} }}
QLabel#deviceCode {{
    font-family: {FONT_MONO};
    {_font_pt("hero")}
    letter-spacing: 2px;
}}
QLabel#stepBadge {{
    background: {c["surfaceSunken"]};
    color: {c["textMuted"]};
    border-radius: {r["control"]}px;
    padding: 2px 8px;
    {_font_pt("caption")}
}}

/* ---------------------------------------------------------------- 外壳 */
QFrame#topbar {{
    background: rgba(255, 255, 255, 0.72);
    border-bottom: 1px solid {c["divider"]};
}}
QFrame#sidebar {{
    background: rgba(255, 255, 255, 0.72);
    border-right: 1px solid {c["divider"]};
}}

/* ---------------------------------------------------------------- 卡片 */
QFrame#card {{
    background: {elev_card["fill"]};
    border: 1px solid {elev_card["border"]};
    border-radius: {r["card"]}px;
}}
QFrame#softCard {{
    background: {elev_flat["fill"]};
    border: 1px solid {elev_flat["border"]};
    border-radius: {r["control"]}px;
}}
QFrame#tile {{
    background: {elev_flat["fill"]};
    border: 1px solid {elev_flat["border"]};
    border-radius: {r["control"]}px;
}}
QFrame#tile:hover {{
    background: rgba(255, 255, 255, 0.82);
    border-color: {c["accentSoftBorder"]};
}}
QFrame#installHead {{
    background: {elev_raised["fill"]};
    border: 1px solid {elev_raised["border"]};
    border-radius: {r["card"]}px;
}}
QFrame#loaderRow {{
    background: {elev_flat["fill"]};
    border: 1px solid {elev_flat["border"]};
    border-radius: {r["control"]}px;
}}
QFrame#loaderRow:hover {{
    background: rgba(255, 255, 255, 0.82);
    border-color: {c["accentSoftBorder"]};
}}
QFrame#loaderRow[picked="true"] {{
    background: {c["accentSoft"]};
    border: 2px solid {c["accent"]};
}}
QFrame#loaderRow[dimmed="true"] {{
    background: {c["surfaceSunken"]};
    border-color: {c["divider"]};
}}

/* ---------------------------------------------------------------- 按钮 */
QPushButton {{
    background: {field};
    color: {c["text"]};
    border: 1px solid {c["border"]};
    border-radius: {r["control"]}px;
    padding: 7px 14px;
    min-height: 18px;
}}
QPushButton:hover {{ background: rgba(234, 241, 255, 0.88); border-color: {c["accentSoftBorder"]}; }}
/* 按下只换底色，不再改 padding：位移会让文字“跳”一下，是廉价感的来源 */
QPushButton:pressed {{ background: {c["pressed"]}; border-color: {c["accentSoftBorder"]}; }}
QPushButton:disabled {{
    color: {c["textFaint"]};
    background: {c["surfaceSunken"]};
    border-color: {c["divider"]};
}}
QPushButton#accent, QPushButton#primary {{
    background: {c["accent"]};
    color: {c["textOnAccent"]};
    border: none;
    border-radius: {r["control"]}px;
    {_font_pt("bodyStrong")}
    padding: 9px 18px;
}}
QPushButton#accent:hover, QPushButton#primary:hover {{ background: {c["accentHover"]}; }}
QPushButton#accent:pressed, QPushButton#primary:pressed {{ background: {c["accentDark"]}; }}
QPushButton#accent:disabled, QPushButton#primary:disabled {{
    background: {c["accentSoftBorder"]};
    color: {c["surface"]};
}}
QPushButton#accentBig {{
    background: {c["accent"]};
    color: {c["textOnAccent"]};
    border: none;
    border-radius: {r["card"]}px;
    {_font_pt("h3")}
}}
QPushButton#accentBig:hover {{ background: {c["accentHover"]}; }}
QPushButton#accentBig:pressed {{ background: {c["accentDark"]}; }}
QPushButton#accentBig:disabled {{ background: {c["accentSoftBorder"]}; color: {c["surface"]}; }}
QPushButton#ghost {{
    background: transparent;
    border: 1px solid {c["border"]};
    color: {c["textMuted"]};
}}
QPushButton#ghost:hover {{
    background: {c["accentSoft"]};
    border-color: {c["accentSoftBorder"]};
    color: {c["text"]};
}}
QPushButton#danger {{
    color: {c["danger"]};
    border-color: {c["danger"]};
    background: {field};
}}
QPushButton#danger:hover {{ background: rgba(194, 69, 63, 0.08); }}
QPushButton#nav {{
    background: transparent;
    border: none;
    border-radius: {r["control"]}px;
    text-align: left;
    padding: 9px 12px;
    color: {c["textMuted"]};
    {_font_pt("body")}
}}
QPushButton#nav:hover {{ background: {c["hover"]}; color: {c["text"]}; }}
QPushButton#nav:checked {{
    background: {c["accentSoft"]};
    color: {c["accent"]};
    font-weight: 600;
}}
QPushButton#navitem {{
    background: transparent;
    border: none;
    border-radius: {r["control"]}px;
    text-align: left;
    padding: 8px 12px;
    color: {c["textMuted"]};
}}
QPushButton#navitem:hover {{ background: {c["hover"]}; color: {c["text"]}; }}
QPushButton#navitem:checked {{
    background: {c["accentSoft"]};
    color: {c["accent"]};
    font-weight: 600;
}}
QPushButton#topnav {{
    background: transparent;
    border: none;
    border-radius: {r["control"]}px;
    text-align: center;
    padding: 6px 14px;
    color: {c["textMuted"]};
    {_font_pt("body")}
}}
QPushButton#topnav:hover {{ background: {c["hover"]}; color: {c["text"]}; }}
QPushButton#topnav:checked {{
    background: {c["accentSoft"]};
    color: {c["accent"]};
    font-weight: 600;
}}
QPushButton#segment {{
    background: transparent;
    border: none;
    border-radius: {r["control"]}px;
    padding: 6px 16px;
    color: {c["textMuted"]};
}}
QPushButton#segment:hover {{ color: {c["text"]}; background: {c["hover"]}; }}
QPushButton#segment:checked {{
    background: rgba(255, 255, 255, 0.86);
    color: {c["accent"]};
    font-weight: 600;
    border: 1px solid {c["border"]};
}}
QPushButton#tile, QPushButton#quickTile {{
    background: {elev_flat["fill"]};
    border: 1px solid {elev_flat["border"]};
    border-radius: {r["control"]}px;
    text-align: left;
    padding: 10px 12px;
    color: {c["text"]};
}}
QPushButton#tile:hover, QPushButton#quickTile:hover {{
    background: rgba(255, 255, 255, 0.86);
    border-color: {c["accentSoftBorder"]};
}}
QPushButton#quickTile:checked {{
    background: {c["accentSoft"]};
    border-color: {c["accentSoftBorder"]};
    color: {c["accent"]};
    font-weight: 600;
}}
QPushButton#homeTab {{
    background: transparent;
    border: none;
    border-radius: {r["control"]}px;
    padding: 6px 10px;
    color: {c["textMuted"]};
    {_font_pt("body")}
    text-align: left;
}}
QPushButton#homeTab:hover {{ color: {c["text"]}; background: {c["hover"]}; }}
QPushButton#homeTab:checked {{
    color: {c["accent"]};
    font-weight: 600;
    background: {c["accentSoft"]};
}}
QPushButton#heroMenu {{
    color: {c["textMuted"]};
    padding: 0px;
    border: none;
    background: transparent;
    border-radius: {r["control"]}px;
    {_font_pt("h3")}
}}
QPushButton#heroMenu:hover {{ color: {c["text"]}; background: {c["hover"]}; }}
QPushButton#accountCaption {{
    background: transparent;
    border: none;
    color: {c["textMuted"]};
    font-size: {TYPE["caption"][0]}pt;
    padding: 0px;
}}
QPushButton#accountCaption:hover {{ color: {c["accent"]}; }}
QLabel#dailyLine {{
    color: {c["textFaint"]};
    font-size: {TYPE["caption"][0]}pt;
    padding: 2px 4px;
}}
QLabel#dailyLine:hover {{ color: {c["textMuted"]}; }}
QLabel#statusDot {{ color: {c["ok"]}; font-size: {TYPE["h3"][0]}pt; }}

/* ---------------------------------------------------------------- 窗口按钮 */
QPushButton#winBtn {{
    background: transparent;
    border: none;
    border-radius: {r["control"]}px;
    padding: 0px;
}}
QPushButton#winBtn:hover {{ background: {c["hover"]}; }}
QPushButton#winBtn:pressed {{ background: {c["pressed"]}; }}
QPushButton#winClose {{
    background: transparent;
    border: none;
    border-radius: {r["control"]}px;
    padding: 0px;
}}
QPushButton#winClose:hover {{ background: #FDE9E8; }}
QPushButton#winClose:pressed {{ background: #F8D8D6; }}
QPushButton#iconBtn {{
    background: transparent;
    border: none;
    border-radius: {r["control"]}px;
    padding: 0px;
}}
QPushButton#iconBtn:hover {{ background: {c["hover"]}; }}
QPushButton#iconBtn:pressed {{ background: {c["pressed"]}; }}

/* ---------------------------------------------------------------- 输入 */
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {{
    background: {field};
    color: {c["text"]};
    border: 1px solid {c["border"]};
    border-radius: {r["control"]}px;
    padding: 7px 10px;
    selection-background-color: {c["accent"]};
    selection-color: {c["textOnAccent"]};
}}
QLineEdit:hover, QSpinBox:hover, QComboBox:hover {{ border-color: {c["borderStrong"]}; }}
/* 焦点环：QSS 没有 box-shadow，用 2px 边框 + 收回 1px 内边距模拟，尺寸不变 */
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{
    border: 2px solid {c["focus"]};
    padding: 6px 9px;
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    background: {c["surfaceSunken"]};
    color: {c["textFaint"]};
    border-color: {c["divider"]};
}}
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
QLineEdit#dirName {{ {_font_pt("bodyStrong")} padding: 3px 8px; }}
QLineEdit#heroName {{
    {_font_pt("h3")}
    padding: 2px 8px;
    border-radius: {r["control"]}px;
    background: transparent;
    border: 1px solid transparent;
}}
QLineEdit#heroName:hover {{ background: {c["surface"]}; border-color: {c["border"]}; }}
QLineEdit#heroName:focus {{ background: {c["surface"]}; border-color: {c["focus"]}; padding: 2px 8px; }}
QComboBox#heroVersion {{
    {_font_pt("h3")}
    padding: 4px 10px;
    border-radius: {r["control"]}px;
}}
QComboBox#heroVersion QAbstractItemView {{ {_font_pt("body")} }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 18px; height: 18px; border-radius: 5px;
    border: 1px solid {c["borderStrong"]};
    /* 未勾选：半透明，透出下层玻璃卡片 */
    background: {field};
}}
QCheckBox::indicator:hover {{ border-color: {c["accent"]}; background: rgba(255, 255, 255, 0.9); }}
QCheckBox::indicator:checked {{
    /* 勾选态是品牌色，带一点透明以免在玻璃上显得突兀 */
    background: rgba(58, 118, 232, 0.92); border-color: {c["accent"]};
    {check_icon}
}}
QCheckBox::indicator:disabled {{
    background: rgba(255, 255, 255, 0.40); border-color: {c["divider"]};
}}
QCheckBox::indicator:checked:disabled {{
    background: {c["accentSoftBorder"]}; border-color: {c["accentSoftBorder"]};
}}

/* ---------------------------------------------------------------- 列表 */
QTreeWidget, QListWidget {{ background: transparent; border: none; outline: none; }}
QTreeWidget::item {{ margin: 2px 2px; padding: 7px 6px; border-radius: {r["control"]}px; }}
QTreeWidget::item:hover {{ background: {c["hover"]}; }}
QTreeWidget::item:selected {{ background: {c["selected"]}; color: {c["accent"]}; }}
QListWidget::item {{ margin: 2px 2px; padding: 5px 6px; border-radius: {r["control"]}px; }}
QListWidget::item:hover {{ background: {c["hover"]}; }}
QListWidget::item:selected {{ background: {c["selected"]}; }}
QListWidget#homeList {{ background: transparent; }}
QListWidget#homeList::item {{ margin: 2px 2px; padding: 7px 6px; border-radius: {r["control"]}px; }}
QListWidget#settingsNav {{
    background: transparent;
    border: none;
}}
QListWidget#settingsNav::item {{
    padding: 8px 10px;
    border-radius: {r["control"]}px;
    color: {c["textMuted"]};
}}
QListWidget#settingsNav::item:hover {{ background: {c["hover"]}; color: {c["text"]}; }}
QListWidget#settingsNav::item:selected {{
    background: {c["selectedStrong"]};
    color: {c["accent"]};
    font-weight: 600;
}}
QHeaderView::section {{
    background: transparent;
    color: {c["textMuted"]};
    border: none;
    border-bottom: 1px solid {c["divider"]};
    padding: 6px 4px;
    {_font_pt("caption")}
}}

/* ---------------------------------------------------------------- 反馈 */
QProgressBar {{
    background: rgba(58, 118, 232, 0.14);
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
    font-size: 1pt;
    color: transparent;
}}
QProgressBar::chunk {{ background: {c["accent"]}; border-radius: 3px; }}
QSlider::groove:horizontal {{ height: 4px; background: rgba(58, 118, 232, 0.16); border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {c["accent"]}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    width: 16px; height: 16px; margin: -6px 0; border-radius: 8px;
    /* 手柄半透明：叠在玻璃卡片上能透出底色，描边保证可点击区域清晰 */
    background: rgba(255, 255, 255, 0.88); border: 2px solid {c["accent"]};
}}
QSlider::handle:horizontal:hover {{ border-color: {c["accentHover"]}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {c["borderStrong"]}; border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {c["textFaint"]}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {c["borderStrong"]}; border-radius: 5px; min-width: 28px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QPlainTextEdit {{
    font-family: {FONT_MONO};
    background: #0F1520;
    color: #D6E2F5;
    border-radius: {r["card"]}px;
    padding: 8px;
}}
QMenu {{
    /* 菜单是独立顶层窗口：不透明白底才能保证文字可读（透明会让下层内容透进来），
       但用轻微的半透明 + 描边保留玻璃观感 */
    background: rgba(255, 255, 255, 0.94);
    border: 1px solid {c["border"]};
    border-radius: {r["control"]}px;
    padding: 6px;
}}
QMenu::item {{ padding: 7px 22px 7px 14px; border-radius: 6px; }}
QMenu::item:selected {{ background: {c["selected"]}; color: {c["accent"]}; }}
QMenu::separator {{ height: 1px; background: {c["divider"]}; margin: 4px 8px; }}
QToolTip {{
    background: #1F2530; color: #FFFFFF; border: none;
    border-radius: 6px; padding: 5px 8px;
}}
/* 内联样式仍然用到的少量语义类（优先改为 role 属性，这里只作兜底） */
QLabel#caption {{ color: {c["textMuted"]}; font-size: {TYPE["caption"][0]}pt; }}
QLabel#emptyTitle {{ {_font_pt("bodyStrong")} color: {c["text"]}; }}
QLabel#emptyHint {{ {_font_pt("caption")} color: {c["textMuted"]}; }}
QFrame#divider {{ background: {c["divider"]}; border: none; max-height: 1px; }}
"""


def pcl_qss() -> str:
    """原版 PCL2 风格的结构性差异（配色已由 ``TOKENS_PCL`` 覆盖）。

    历史上这里重复了 340 行选择器只为了换颜色；现在颜色/圆角都由 token 提供，
    只剩「标题栏是纯蓝实色、启动按钮是描边款」这类结构差异。
    """
    c = TOKENS_PCL["color"]
    return f"""
QFrame#pclTopbar {{
    background: {c["accentHover"]};
    border: none;
}}
QLabel#pclBrand {{ color: #FFFFFF; {_font_pt("h3")} }}
QLabel#pclPageTitle {{ color: {c["accentSoft"]}; {_font_pt("body")} }}
QLabel#pclStatus {{ color: #FFFFFF; }}
QPushButton#pclTopnav {{
    background: transparent;
    border: none;
    border-radius: {RADIUS["pill"]}px;
    color: #FFFFFF;
    {_font_pt("caption")}
    padding: 0 12px;
    min-height: 27px;
    max-height: 27px;
}}
QPushButton#pclTopnav:hover {{ background: rgba(234, 242, 254, 0.20); }}
QPushButton#pclTopnav:checked {{ background: #FFFFFF; color: {c["accent"]}; }}
QPushButton#pclTitleBtn {{ background: transparent; border: none; padding: 0px; }}
QPushButton#pclTitleBtn:hover {{ background: rgba(255, 255, 255, 0.18); }}
QPushButton#pclTitleBtn:pressed {{ background: rgba(255, 255, 255, 0.26); }}
QFrame#pclTopbar QPushButton#winBtn, QFrame#pclTopbar QPushButton#winClose {{
    background: transparent;
    border: none;
    padding: 0px;
}}
QFrame#pclTopbar QPushButton#winBtn:hover {{ background: rgba(255, 255, 255, 0.18); }}
QFrame#pclTopbar QPushButton#winClose:hover {{ background: #E81123; }}
QFrame#pclLeftPane {{
    background: {c["accentSoft"]};
    border-right: 1px solid {c["accentSoftBorder"]};
}}
QWidget#pclHomeRight {{ background: {c["surface"]}; }}
QFrame#pclPill {{
    background: {c["surface"]};
    border: 1px solid {c["accentSoftBorder"]};
    border-radius: {RADIUS["pill"]}px;
}}
QLabel#pclPillText {{ color: {c["text"]}; {_font_pt("body")} }}
QPushButton#pclLaunch {{
    background: {c["surface"]};
    border: 1px solid {c["accentDark"]};
    border-radius: {RADIUS["control"]}px;
    color: {c["accentDark"]};
    {_font_pt("h3")}
    padding: 4px 8px 18px 8px;
}}
QPushButton#pclLaunch:hover {{ background: {c["accentSoft"]}; border-color: {c["accent"]}; }}
QLabel#pclLaunchVersion {{
    color: {c["textMuted"]};
    font-size: {TYPE["caption"][0]}pt;
    background: transparent;
}}
QPushButton#pclSmall {{
    background: {c["surface"]};
    border: 1px solid {c["text"]};
    border-radius: {RADIUS["control"]}px;
    color: {c["text"]};
    padding: 0 8px;
}}
QPushButton#pclSmall:hover {{ border-color: {c["accent"]}; color: {c["accent"]}; }}
QComboBox#pclVersion, QLineEdit#pclName {{
    background: {c["surface"]};
    border: 1px solid {c["border"]};
    border-radius: {RADIUS["control"]}px;
    padding: 0 8px;
    color: {c["text"]};
    {_font_pt("body")}
}}
QComboBox#pclVersion:hover, QLineEdit#pclName:hover {{ border-color: {c["accentSoftBorder"]}; }}
QComboBox#pclVersion::drop-down {{ border: none; width: 22px; }}
QLineEdit#pclName:focus {{ border-color: {c["focus"]}; }}
"""


def full_qss(style: str = "default") -> str:
    """按界面风格返回完整样式表（``default`` / ``pcl``）。

    结果按风格缓存：``setStyleSheet`` 会重刷窗口里所有控件，重复生成字符串没有意义。
    """
    key = "pcl" if str(style or "").lower() == "pcl" else "default"
    cached = _QSS_CACHE.get(key)
    if cached is None:
        cached = app_qss(key) + (pcl_qss() if key == "pcl" else "")
        _QSS_CACHE[key] = cached
    return cached
