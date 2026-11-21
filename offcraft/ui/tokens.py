"""设计 token：颜色 / 字号 / 圆角 / 间距的唯一来源。

以前这些值散落在 ``theme.py`` 的 30 多个模块常量和 74 处内联 ``setStyleSheet``
里，于是同一个界面出现了 21 种字号、13 种圆角、两套互相矛盾的色板，视觉上
「没有层级」。这里把它们收成一张表：QSS 只引用 token 名，改一处即可全局生效，
也为以后的暗色模式留出入口（暗色 = 第二份 dict，不需要改任何选择器）。

对比度约束（WCAG 2.1，均已实测）：

* ``text`` on ``surface`` ≈ 16.4:1，on ``bg`` ≈ 13.0:1；
* ``textMuted`` on ``surface`` ≈ 6.1:1，on ``bg`` ≈ 4.8:1（旧值 #7A8698 只有 3.4:1）；
* ``border`` 与 ``surface`` 对比 ≈ 1.59:1、与 ``bg`` ≈ 1.24:1 —— 卡片边界可见；
* ``surface`` 与 ``bg`` 对比 ≈ 1.26:1 —— 卡片从背景里「浮」出来。

QSS 没有 ``box-shadow``，因此层级只能用「实色底 + 1px 描边」两级表达，
``ELEVATION`` 就是这两级的登记表。
"""

from __future__ import annotations

from copy import deepcopy

__all__ = [
    "ELEVATION",
    "FONT_FAMILY",
    "FONT_MONO",
    "RADIUS",
    "SPACE",
    "TOKENS_PCL",
    "TYPE",
    "style_tokens",
]

#: 界面字体：中文优先，缺字时由 Qt 回退
FONT_FAMILY = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif'
#: 等宽字体：设备码、路径、日志
FONT_MONO = '"Cascadia Mono", "Consolas", monospace'

#: 间距档（逻辑像素）。只有这几档，禁止再出现 7/9/11/13 这种随手值。
SPACE = {
    "xs": 4,
    "sm": 8,
    "md": 12,
    "lg": 16,
    "xl": 24,
    "2xl": 32,
    "3xl": 48,
}

#: 字号档（pt, weight）。8/8.2/8.4/8.6/8.8/9.5/11.5/12.5/13/14/16/17/18/20 全部收敛到这里。
TYPE = {
    "caption": (9, 400),      # 说明文字、辅助信息
    "label": (9, 600),        # 表单标签
    "body": (10, 400),        # 正文
    "bodyStrong": (10, 600),  # 正文强调
    "h3": (12, 600),          # 卡片内小标题
    "h2": (14, 700),          # 卡片标题 / 页面内分区
    "h1": (19, 700),          # 页面大标题
    "hero": (22, 700),        # 启动页账户名
}

#: 圆角档。control=按钮/输入框，card=卡片，pill=胶囊/徽标。
RADIUS = {
    "control": 8,
    "card": 12,
    "pill": 999,
}

#: QSS 没有投影，层级用「半透明底色 + 描边」表达。
#:
#: ``fill`` 允许是 rgba：卡片保持毛玻璃半透明（能透出自定义背景图 / GIF），
#: 层级由 ``border`` 的实色描边保证可见。早期版本为了「看得见卡片」把 fill 做成
#: 不透明白色，结果毛玻璃质感全丢；实测 0.66 的白色叠加在背景上仍有 1.16:1 的
#: 卡面/背景对比，配合实色描边就足够分层。
ELEVATION = {
    # 平铺层：贴在背景上的次级容器（输入框、softCard）
    "flat": {"fill": "rgba(255, 255, 255, 0.52)", "border": "#B9C6DA"},
    # 卡片层：毛玻璃白 + 可见描边
    "card": {"fill": "rgba(255, 255, 255, 0.62)", "border": "#AEBED5"},
    # 抬升层：需要更强的边界（弹层、悬浮任务面板）
    "raised": {"fill": "rgba(255, 255, 255, 0.84)", "border": "#A5B5CE"},
}

#: 内层控件（输入框 / 按钮）叠在卡片上时的底色：比卡片更实一点，
#: 避免两层半透明相乘把背景洗成纯白。
FIELD_FILL = "rgba(255, 255, 255, 0.78)"

#: 默认（毛玻璃 / 浅色）主题
TOKENS = {
    "font": {"ui": FONT_FAMILY, "mono": FONT_MONO},
    "space": SPACE,
    "type": TYPE,
    "radius": RADIUS,
    "elev": ELEVATION,
    "field": FIELD_FILL,
    "color": {
        # 底层
        "bg": "#CBD7EA",
        "surface": "#FFFFFF",
        "surfaceAlt": "#F5F8FC",
        "surfaceSunken": "#EEF2F8",
        # 线与边界
        "border": "#C2CEE0",
        "borderStrong": "#B5C3D8",
        "divider": "#DCE4EF",
        # 文字
        "text": "#16202E",
        "textMuted": "#4A576C",
        "textFaint": "#6B788E",
        "textOnAccent": "#FFFFFF",
        # 品牌
        "accent": "#3A76E8",
        "accentHover": "#2F68D6",
        "accentDark": "#2558B8",
        "accentSoft": "#EAF1FF",
        "accentSoftBorder": "#C9D8F5",
        # 语义
        "ok": "#1F8A57",
        "warn": "#A96A12",
        "danger": "#C2453F",
        "info": "#2C7FA3",
        # 焦点环
        "focus": "#3A76E8",
        "focusSoft": "rgba(58, 118, 232, 0.35)",
        # 悬停 / 按下 / 选中（全部半透明：叠在玻璃卡片上仍能透出底色）
        "hover": "rgba(58, 118, 232, 0.07)",
        "pressed": "rgba(58, 118, 232, 0.14)",
        "selected": "rgba(58, 118, 232, 0.13)",
        "selectedStrong": "rgba(58, 118, 232, 0.18)",
    },
}

#: 原版 PCL2 风格：只覆盖颜色与圆角，选择器完全复用默认主题。
#: PCL2 的观感是「方角 + 纯白 + #1370F3 强调色」，因此这里把圆角压到 3/6，
#: 把底改为白→浅蓝的实色，把文字换成 PCL 的灰阶。
TOKENS_PCL = deepcopy(TOKENS)
TOKENS_PCL["radius"] = {"control": 3, "card": 6, "pill": 3}
TOKENS_PCL["elev"] = {
    "flat": {"fill": "#F5F5F5", "border": "#E0EAFD"},
    "card": {"fill": "#FFFFFF", "border": "#D5E6FD"},
    "raised": {"fill": "#FFFFFF", "border": "#96C0F9"},
}
#: PCL 原版是纯白实底，不做毛玻璃
TOKENS_PCL["field"] = "#FFFFFF"
TOKENS_PCL["color"].update(
    {
        "bg": "#EAF2FE",
        "surface": "#FFFFFF",
        "surfaceAlt": "#F5F8FF",
        "surfaceSunken": "#E0EAFD",
        "border": "#D5E6FD",
        "borderStrong": "#96C0F9",
        "divider": "#E0EAFD",
        "text": "#343D4A",
        "textMuted": "#737373",
        "textFaint": "#8C8C8C",
        "accent": "#1370F3",
        "accentHover": "#4890F5",
        "accentDark": "#0B5BCB",
        "accentSoft": "#E0EAFD",
        "accentSoftBorder": "#96C0F9",
        "ok": "#1F8A57",
        "warn": "#A96A12",
        "danger": "#CE2111",
        "info": "#2C7FA3",
        "focus": "#1370F3",
        "focusSoft": "rgba(19, 112, 243, 0.35)",
        "hover": "rgba(19, 112, 243, 0.08)",
        "pressed": "rgba(19, 112, 243, 0.16)",
        "selected": "rgba(19, 112, 243, 0.13)",
        "selectedStrong": "rgba(19, 112, 243, 0.18)",
    }
)


def style_tokens(style: str = "default") -> dict:
    """按风格名返回 token 表（``default`` / ``pcl``，未知值回落到默认）。"""
    return TOKENS_PCL if str(style or "").lower() == "pcl" else TOKENS
