"""缺 Java 时的自动安装链路：运行时挑选 + 错误识别。

启动前若找不到匹配的 Java，界面会弹窗询问并自动下载 Mojang 官方运行时。
这里覆盖两段不依赖网络与界面的纯逻辑：

1. :func:`pick_runtime_for_major` —— 按所需主版本从清单里挑组件：
   精确优先、其次更高的最小版本、兜底最高版本；
2. :func:`_looks_like_missing_java` —— 只在错误确实是「没有合适的 Java」时
   才触发弹窗，避免把缺依赖文件之类的错误误判。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from offcraft.content.java import JavaRuntime, pick_runtime_for_major

_app = QApplication.instance() or QApplication([])


def _runtime(component: str, major: int) -> JavaRuntime:
    return JavaRuntime(
        component=component,
        version=f"{major}.0.0",
        released="",
        manifest_url="https://example.invalid/manifest.json",
    )


#: 一份典型的 Mojang 清单（主版本覆盖 8 / 16 / 17 / 21 / 25）
RUNTIMES = [
    _runtime("jre-legacy", 8),
    _runtime("java-runtime-alpha", 16),
    _runtime("java-runtime-beta", 17),
    _runtime("java-runtime-gamma", 17),
    _runtime("java-runtime-delta", 21),
    _runtime("java-runtime-epsilon", 25),
]


class PickRuntimeTests(unittest.TestCase):
    def test_exact_major_wins(self) -> None:
        self.assertEqual(pick_runtime_for_major(RUNTIMES, 8).component, "jre-legacy")
        self.assertEqual(pick_runtime_for_major(RUNTIMES, 21).component, "java-runtime-delta")
        self.assertEqual(pick_runtime_for_major(RUNTIMES, 25).component, "java-runtime-epsilon")

    def test_exact_major_duplicates_are_stable(self) -> None:
        # 17 有两个组件：固定挑 component 排序第一个，保证每次结果一致
        picked = pick_runtime_for_major(RUNTIMES, 17)
        self.assertEqual(picked.major, 17)
        self.assertEqual(picked.component, "java-runtime-beta")

    def test_falls_forward_to_next_higher(self) -> None:
        # 需要 19：清单里没有 19，应当用 21 而不是 17 或 8
        picked = pick_runtime_for_major(RUNTIMES, 19)
        self.assertEqual(picked.major, 21)

    def test_above_everything_uses_highest(self) -> None:
        # 需要 30：清单最高只有 25，仍装 25（比什么都不装强）
        self.assertEqual(pick_runtime_for_major(RUNTIMES, 30).major, 25)

    def test_none_prefers_java8(self) -> None:
        # 版本没声明 javaVersion 时按老版本处理，优先 Java 8
        self.assertEqual(pick_runtime_for_major(RUNTIMES, None).major, 8)

    def test_empty_manifest_returns_none(self) -> None:
        self.assertIsNone(pick_runtime_for_major([], 17))


class MissingJavaDetectionTests(unittest.TestCase):
    """``_looks_like_missing_java`` 决定要不要弹「自动装 Java」对话框。"""

    @staticmethod
    def _check(message: str) -> bool:
        from offcraft.ui.app import _looks_like_missing_java

        return _looks_like_missing_java(message)

    def test_java_not_found_messages_trigger(self) -> None:
        for message in (
            "需要 Java 17，但只找到 Java 8（请安装 17 或更高版本，或在设置页手动选择）",
            "没有找到可用的 Java 运行时",
            "未检测到 Java",
        ):
            self.assertTrue(self._check(message), message)

    def test_other_launch_errors_do_not_trigger(self) -> None:
        for message in (
            "缺少 3 个依赖文件，请先安装/修复该版本",
            "版本 JSON 损坏",
            "游戏已经在运行了",
            "",
        ):
            self.assertFalse(self._check(message), message)


if __name__ == "__main__":
    unittest.main()
