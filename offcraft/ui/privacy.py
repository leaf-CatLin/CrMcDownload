"""隐私政策对话框（「设置 → 关于 → 隐私政策」入口）。"""

from __future__ import annotations

from typing import Any

from PyQt6.QtWidgets import QDialog, QHBoxLayout, QPushButton, QTextBrowser, QVBoxLayout

from offcraft.privacy import policy_html

__all__ = ["PrivacyDialog"]


class PrivacyDialog(QDialog):
    """展示完整的隐私政策；正文与官网共用 ``offcraft.privacy``。"""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("隐私政策")
        self.resize(700, 640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        browser = QTextBrowser(self)
        browser.setHtml(policy_html())
        browser.setOpenExternalLinks(True)
        browser.setStyleSheet("QTextBrowser { border: none; }")
        layout.addWidget(browser, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton("关闭")
        close.setObjectName("accent")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
