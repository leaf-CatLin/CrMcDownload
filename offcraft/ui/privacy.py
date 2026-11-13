"""隐私政策对话框（「设置 → 关于 → 隐私政策」入口 / 首次下载同意门禁）。"""

from __future__ import annotations

from typing import Any

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from offcraft.privacy import policy_html

__all__ = ["PrivacyDialog"]


class PrivacyDialog(QDialog):
    """展示完整的隐私政策；正文与官网共用 ``offcraft.privacy``。

    ``require_accept=True`` 时用于首次下载前的同意门禁：必须勾选复选框
    才能点「同意并继续」，对话框被接受后 ``accepted`` 为 True。
    """

    def __init__(self, parent: Any = None, *, require_accept: bool = False) -> None:
        super().__init__(parent)
        self.require_accept = require_accept
        self.accepted = False
        self.agree: QCheckBox | None = None
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

        if require_accept:
            self.agree = QCheckBox("我已阅读并同意上述隐私政策")
            layout.addWidget(self.agree)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        if require_accept:
            cancel = QPushButton("暂不同意")
            cancel.clicked.connect(self.reject)
            buttons.addWidget(cancel)
            ok = QPushButton("同意并继续")
            ok.setObjectName("accent")
            ok.setEnabled(False)
            ok.clicked.connect(self._accept)
            assert self.agree is not None
            self.agree.toggled.connect(ok.setEnabled)
            buttons.addWidget(ok)
        else:
            close = QPushButton("关闭")
            close.setObjectName("accent")
            close.clicked.connect(self.accept)
            buttons.addWidget(close)
        layout.addLayout(buttons)

    def _accept(self) -> None:
        self.accepted = bool(self.agree is not None and self.agree.isChecked())
        if self.accepted:
            self.accept()
