"""叠加上色：默认背景下观感不变，与底色相同的控件完全透明。"""

import unittest

from offcraft.ui.theme import BG, GLASS, GLASS_SOFT, overlay


def _alpha(value: str) -> float:
    return float(value.rsplit(",", 1)[1].rstrip(")"))


class OverlayTests(unittest.TestCase):
    def test_same_color_is_fully_transparent(self) -> None:
        self.assertEqual(overlay(BG), "rgba(242, 246, 252, 0.00)")

    def test_different_color_is_translucent(self) -> None:
        alpha = _alpha(overlay("#FFFFFF"))
        self.assertGreater(alpha, 0.5)
        self.assertLess(alpha, 1.0)

    def test_bigger_contrast_means_more_opaque(self) -> None:
        # 与底色差得越多，需要的 alpha 越高，才能保住原本的观感
        self.assertGreater(_alpha(overlay("#E6EEF9")), _alpha(overlay("#F8FAFD")))

    def test_low_contrast_stays_translucent(self) -> None:
        self.assertLessEqual(_alpha(overlay("#F8FAFD")), 0.6)

    def test_glass_constants_are_overlays(self) -> None:
        for value in (GLASS, GLASS_SOFT):
            self.assertTrue(value.startswith("rgba("))
            self.assertLess(_alpha(value), 1.0)
            self.assertGreaterEqual(_alpha(value), 0.35)


if __name__ == "__main__":
    unittest.main()
