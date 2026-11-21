"""叠加上色与设计 token：默认背景下观感不变，与底色相同的控件完全透明。

同时校验 token 层的三件事：
1. 字号 / 圆角档数是收敛的（历史上曾出现 21 种字号、13 种圆角）；
2. 关键对比度满足 WCAG 2.1 AA（正文 4.5:1，卡片边界可见）；
3. ``TOKENS_PCL`` 与默认主题键集合一致（否则切换风格会 KeyError）。
"""

import unittest

from offcraft.ui.theme import BG, GLASS, GLASS_SOFT, overlay
from offcraft.ui.tokens import RADIUS, TOKENS, TOKENS_PCL, TYPE


def _alpha(value: str) -> float:
    return float(value.rsplit(",", 1)[1].rstrip(")"))


def _channels(value: str) -> tuple[int, int, int]:
    """支持 ``#RRGGBB`` 与 ``rgba(r, g, b, a)`` 两种写法。"""
    if value.startswith("#"):
        text = value.lstrip("#")
        return tuple(int(text[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
    parts = value[value.index("(") + 1 : value.index(")")].split(",")
    return tuple(int(part) for part in parts[:3])  # type: ignore[return-value]


def _alpha_of(value: str) -> float:
    """``#RRGGBB`` 视为完全不透明，``rgba(...)`` 取第 4 个分量。"""
    if value.startswith("#"):
        return 1.0
    parts = value[value.index("(") + 1 : value.index(")")].split(",")
    return float(parts[3]) if len(parts) > 3 else 1.0


def _flatten(color: str, base: str) -> str:
    """把（可能半透明的）``color`` 合成到 ``base`` 上，返回不透明的 ``#RRGGBB``。

    卡片现在是毛玻璃（``rgba(255,255,255,0.62)``），对比度必须按**合成后**的
    实际观感算；直接拿 rgba 的 RGB 分量去算会得出错误的结论。
    """
    alpha = _alpha_of(color)
    if alpha >= 1.0:
        return color if color.startswith("#") else "#" + "".join(
            f"{channel:02X}" for channel in _channels(color)
        )
    over = _channels(color)
    under = _channels(base)
    mixed = [round(alpha * top + (1 - alpha) * bottom) for top, bottom in zip(over, under)]
    return "#" + "".join(f"{channel:02X}" for channel in mixed)


def _luminance(hex_color: str) -> float:
    text = hex_color.lstrip("#")
    channels = [int(text[index : index + 2], 16) / 255 for index in (0, 2, 4)]

    def linear(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(value) for value in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(first: str, second: str) -> float:
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class OverlayTests(unittest.TestCase):
    """``overlay`` 的契约：与底色相同 → 全透明；离底色越远 → alpha 越高；首尾有界。"""

    def test_same_color_is_fully_transparent(self) -> None:
        self.assertEqual(_alpha(overlay(BG)), 0.0)

    def test_near_color_is_nearly_transparent(self) -> None:
        near = _channels(BG)
        # 与背景只差 2 个通道值：肉眼不可辨，应当几乎透明
        self.assertLessEqual(_alpha(overlay(f"#{near[0] + 2:02X}{near[1]:02X}{near[2]:02X}")), 0.6)

    def test_further_color_is_more_opaque(self) -> None:
        near = _channels(BG)
        closer = f"#{near[0] + 10:02X}{near[1] + 10:02X}{near[2] + 10:02X}"
        farther = f"#{near[0] + 40:02X}{near[1] + 40:02X}{near[2] + 40:02X}"
        self.assertGreater(_alpha(overlay(farther)), _alpha(overlay(closer)))

    def test_alpha_is_bounded(self) -> None:
        for color in ("#000000", "#FFFFFF", BG, "#E6EEF9"):
            alpha = _alpha(overlay(color))
            self.assertGreaterEqual(alpha, 0.0)
            self.assertLessEqual(alpha, 0.94)

    def test_glass_constants_are_overlays(self) -> None:
        # 这两个常量已不再参与样式生成（QSS 改用 token 实色），但保留兼容性
        for value in (GLASS, GLASS_SOFT):
            self.assertTrue(value.startswith("rgba("))
            self.assertLess(_alpha(value), 1.0)
            self.assertGreaterEqual(_alpha(value), 0.35)


class TokenTests(unittest.TestCase):
    def test_type_scale_is_small(self) -> None:
        sizes = {size for size, _weight in TYPE.values()}
        self.assertLessEqual(len(sizes), 6, f"字号档过多：{sorted(sizes)}")
        for role in ("caption", "body", "h3", "h2", "h1", "hero"):
            self.assertIn(role, TYPE)
        # 正文必须可读：不小于 9pt
        self.assertGreaterEqual(TYPE["body"][0], 9)

    def test_radius_scale_is_three(self) -> None:
        self.assertEqual(set(RADIUS), {"control", "card", "pill"})
        self.assertLess(RADIUS["control"], RADIUS["card"])
        self.assertGreaterEqual(RADIUS["pill"], 100)

    def test_surface_stands_out_from_background(self) -> None:
        color = TOKENS["color"]
        elev = TOKENS["elev"]
        # 卡片是毛玻璃：先按实际观感合成到背景上再比对比度
        card = _flatten(elev["card"]["fill"], color["bg"])
        self.assertGreaterEqual(_contrast(card, color["bg"]), 1.15)
        # 描边是实色，直接与背景比
        self.assertGreaterEqual(_contrast(elev["card"]["border"], color["bg"]), 1.15)
        # 卡片必须**真的**是半透明（毛玻璃质感不能又被改成实色白）
        self.assertLess(_alpha_of(elev["card"]["fill"]), 1.0)
        self.assertLess(_alpha_of(elev["flat"]["fill"]), 1.0)

    def test_body_text_contrast_passes_aa(self) -> None:
        color = TOKENS["color"]
        # 文字画在卡片上：同样按合成后的卡面色算
        card = _flatten(TOKENS["elev"]["card"]["fill"], color["bg"])
        self.assertGreaterEqual(_contrast(color["text"], card), 7.0)
        self.assertGreaterEqual(_contrast(color["textMuted"], card), 4.5)
        self.assertGreaterEqual(_contrast(color["textMuted"], color["bg"]), 4.5)

    def test_pcl_tokens_cover_every_key(self) -> None:
        for group in ("color", "radius", "elev", "space", "type"):
            self.assertEqual(
                set(TOKENS_PCL[group]), set(TOKENS[group]), f"{group} 键集合不一致"
            )


if __name__ == "__main__":
    unittest.main()
