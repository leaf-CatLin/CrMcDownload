"""皮肤页：选图 → 预览（2D 展开）→ 上传到镜像 /skin → 状态反馈。

入口挂在启动页账号行「自定义皮肤」与设置页卡片上（由 MainWindow 包成对话框）。
未配置下载源时禁用上传并给出说明；上传成功后本地缓存并刷新启动页头像。
"""

from __future__ import annotations

import threading
from typing import Any

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from offcraft.skins.avatar import head_avatar
from offcraft.skins.default import default_skin_bytes
from offcraft.skins.upload import (
    SkinError,
    delete_skin,
    fetch_skin,
    inspect_skin,
    list_skins,
    load_local_skin,
    read_skin,
    remove_local_skin,
    save_local_skin,
    upload_skin,
)
from offcraft.ui.theme import MUTED, initial_pixmap
from offcraft.ui.widgets import Badge, Card

__all__ = ["LAYOUT_CHOICES", "SkinPage"]

#: 皮肤页样式；新增布局时在这里登记，并在 SkinPage 里加对应的 ``_build_<id>``
LAYOUT_CHOICES = (
    ("v1", "双栏卡片"),
    ("v2", "工作台"),
    ("v3", "极简单栏"),
)

PREVIEW_PX = 170


class _PreviewTile(QWidget):
    """棋盘格底 + 圆角贴图预览（纯 QPainter 绘制，不依赖网页）。"""

    def __init__(self, size: int = PREVIEW_PX, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._pixmap: QPixmap | None = None

    def setPixmap(self, pixmap: QPixmap) -> None:  # noqa: N802 - 与 QLabel 保持一致
        self._pixmap = pixmap if not pixmap.isNull() else None
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        path = QPainterPath()
        path.addRoundedRect(rect, 12.0, 12.0)
        painter.setClipPath(path)
        painter.fillRect(self.rect(), QColor("#F8FAFD"))
        cell = 10
        dark = QColor("#EDF1F7")
        for row in range(0, self.height(), cell):
            for col in range(0, self.width(), cell):
                if (row // cell + col // cell) % 2 == 0:
                    painter.fillRect(col, row, cell, cell, dark)
        if self._pixmap is not None:
            target = self._pixmap.scaled(
                self.width() - 22,
                self.height() - 22,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            painter.drawPixmap(
                (self.width() - target.width()) // 2,
                (self.height() - target.height()) // 2,
                target,
            )
        painter.setClipping(False)
        painter.setPen(QPen(QColor("#E1E8F2"), 1.0))
        painter.drawPath(path)
        painter.end()


class SkinPage(QWidget):
    def __init__(self, window: Any, *, read_only: bool = False) -> None:
        super().__init__()
        self.window = window
        self.read_only = read_only
        self._data: bytes | None = None
        self._info: Any | None = None
        self._busy = False
        self._closed = False
        self._build()
        self._reload_state()
        self._refresh_library()
        if read_only:
            # 上传链路暂时不可靠，入口只保留查看；头像仍是本地缓存的那张
            self.upload.setVisible(False)
            self.upload.setEnabled(False)
            sync = getattr(self, "sync_btn", None)
            if sync is not None:
                sync.setVisible(False)
            self.info.setText("当前仅展示本地已缓存的皮肤；上传功能暂未开放。")

    def _build(self) -> None:
        style = getattr(self.window.config, "skin_layout", "v1")
        builder = getattr(self, f"_build_{style}", None) or self._build_v1
        root = QVBoxLayout(self)
        builder(root)

    # ------------------------------------------------------------- 公共部件
    def _make_status(self) -> Badge:
        self.status_badge = Badge("未选择", "gray")
        return self.status_badge

    def _make_preview(self, size: int, height: int, *, top_only: bool = False) -> QFrame:
        stage = QFrame()
        stage.setObjectName("skinStage")
        radius = (
            "border-top-left-radius: 14px; border-top-right-radius: 14px;"
            if top_only
            else "border-radius: 12px;"
        )
        stage.setStyleSheet(
            f"QFrame#skinStage {{ border: 0px; {radius}"
            " background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            " stop:0 rgba(234, 241, 254, 0.62), stop:0.55 rgba(246, 249, 254, 0.50),"
            " stop:1 rgba(255, 255, 255, 0.46)); }"
        )
        stage.setFixedHeight(height)
        stage_layout = QVBoxLayout(stage)
        stage_layout.setContentsMargins(12, 12, 12, 12)
        self.preview = _PreviewTile(size)
        self.preview.setPixmap(initial_pixmap(self.window.config.username, 128, "skin"))
        stage_layout.addWidget(self.preview, 0, Qt.AlignmentFlag.AlignCenter)
        return stage

    def _make_model_combo(self, width: int | None = 190) -> QComboBox:
        self.model = QComboBox()
        self.model.addItems(["自动判定", "经典 (classic)", "纤细 (slim)"])
        if width:
            self.model.setFixedWidth(width)
        self.model.currentIndexChanged.connect(self._model_changed)
        return self.model

    def _make_choose(self, text: str = "＋  选择皮肤图片（PNG）") -> QPushButton:
        self.choose = QPushButton(text)
        self.choose.setObjectName("skinDrop")
        self.choose.setFixedHeight(46)
        self.choose.setCursor(Qt.CursorShape.PointingHandCursor)
        self.choose.setStyleSheet(
            "QPushButton#skinDrop { text-align: left; padding: 0px 14px;"
            " border: 1px dashed #C7D7F3; background: rgba(247, 249, 253, 0.58);"
            " border-radius: 10px; color: #2D6CDF; font-size: 9.5pt; font-weight: 600; }"
        )
        self.choose.clicked.connect(self._choose_file)
        return self.choose

    def _make_upload(self) -> QPushButton:
        self.upload = QPushButton("上传皮肤到镜像")
        self.upload.setObjectName("primary")
        self.upload.setMinimumHeight(38)
        self.upload.setCursor(Qt.CursorShape.PointingHandCursor)
        self.upload.clicked.connect(self._upload)
        return self.upload

    def _make_actions(self, *, flat: bool = False) -> QHBoxLayout:
        actions = QHBoxLayout()
        actions.setSpacing(8 if not flat else 16)
        self.default_btn = QPushButton("使用默认")
        self.default_btn.setMinimumHeight(30)
        self.default_btn.clicked.connect(self._use_default)
        self.sync_btn = QPushButton("同步已上传")
        self.sync_btn.setMinimumHeight(30)
        self.sync_btn.clicked.connect(self._sync)
        self.delete_btn = QPushButton("删除")
        self.delete_btn.setMinimumHeight(30)
        if flat:
            flat_style = (
                "QPushButton { background: transparent; border: 0; color: #2D6CDF;"
                " padding: 0 2px; font-size: 8.6pt; }"
                "QPushButton:hover { color: #1F55B8; }"
            )
            for button in (self.default_btn, self.sync_btn):
                button.setStyleSheet(flat_style)
            self.delete_btn.setStyleSheet(
                "QPushButton { background: transparent; border: 0; color: #D24B45;"
                " padding: 0 2px; font-size: 8.6pt; }"
            )
        else:
            self.delete_btn.setStyleSheet(
                "QPushButton { color: #D24B45; border-color: #F2D8D6;"
                " background: rgba(255, 249, 249, 0.60); }"
            )
        self.delete_btn.clicked.connect(self._delete)
        for button in (self.default_btn, self.sync_btn, self.delete_btn):
            actions.addWidget(button)
        if flat:
            # 扁平样式按内容宽度排列，靠左成组，避免按钮被拉伸铺满整行
            actions.addStretch(1)
            for button in (self.default_btn, self.sync_btn, self.delete_btn):
                button.setSizePolicy(
                    QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
                )
        return actions

    def _make_info(self) -> QLabel:
        self.info = QLabel(" ")
        self.info.setWordWrap(True)
        self.info.setMinimumHeight(32)
        self.info.setStyleSheet(f"color: {MUTED}; font-size: 8.4pt;")
        return self.info

    def _make_meta(self) -> QLabel:
        self.meta_label = QLabel("尚未选择皮肤")
        self.meta_label.setWordWrap(True)
        self.meta_label.setStyleSheet(f"color: {MUTED}; font-size: 8.4pt;")
        return self.meta_label

    def _make_library_card(self, *, height: int | None = None) -> Card:
        card = Card(margins=(14, 12, 14, 12))
        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("镜像皮肤库")
        title.setStyleSheet("font-size: 9.5pt; font-weight: 700;")
        head.addWidget(title)
        hint = QLabel("双击预览")
        hint.setStyleSheet(f"color: {MUTED}; font-size: 8pt;")
        head.addWidget(hint)
        head.addStretch(1)
        self.library_refresh = QPushButton("刷新")
        self.library_refresh.setFixedHeight(26)
        self.library_refresh.setStyleSheet(
            "QPushButton { color: #2D6CDF; background: rgba(232, 240, 254, 0.78);"
            " border: 0px; border-radius: 13px; padding: 0px 12px; font-size: 8.2pt; }"
        )
        self.library_refresh.clicked.connect(self._refresh_library)
        head.addWidget(self.library_refresh)
        card.add_layout(head)

        self.library = QListWidget()
        self.library.setStyleSheet(
            "QListWidget { background: rgba(247, 249, 253, 0.55);"
            " border: 1px solid rgba(227, 233, 242, 0.80);"
            " border-radius: 10px; padding: 4px; font-size: 8.6pt; }"
            "QListWidget::item { padding: 6px 8px; border-radius: 6px; }"
            "QListWidget::item:selected { background: rgba(232, 240, 254, 0.82); color: #2D6CDF; }"
        )
        self.library.setWordWrap(True)
        self.library.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.library.itemDoubleClicked.connect(self._preview_library_item)
        if height:
            self.library.setFixedHeight(height)
        card.add(self.library, 1)
        return card

    # ------------------------------------------------------------- 三套布局
    def _build_v1(self, root: QVBoxLayout) -> None:
        """双栏卡片：左侧大预览，右侧操作，底部皮肤库（默认样式）。"""
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(12)
        logo = QLabel("SK")
        logo.setFixedSize(44, 44)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
            " stop:0 #4C86F3, stop:1 #2D6CDF); color: #FFFFFF;"
            " border-radius: 12px; font-size: 13pt; font-weight: 700;"
        )
        header.addWidget(logo)
        titles = QVBoxLayout()
        titles.setSpacing(3)
        title = QLabel("自定义皮肤")
        title.setStyleSheet("font-size: 17pt; font-weight: 700;")
        titles.addWidget(title)
        subtitle = QLabel("上传 64×64 / 64×32 PNG，装了 CustomSkinLoader 的实例会显示")
        subtitle.setStyleSheet(f"color: {MUTED}; font-size: 8.6pt;")
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch(1)
        header.addWidget(self._make_status())
        root.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(16)

        preview_card = Card(margins=(18, 16, 18, 16))
        preview_card.setFixedWidth(236)
        preview_card.add(self._make_preview(170, 198))
        caption = QLabel("皮肤贴图 · 全部面展开")
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        caption.setStyleSheet(f"color: {MUTED}; font-size: 8.4pt;")
        preview_card.add(caption)
        meta = self._make_meta()
        meta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_card.add(meta)
        body.addWidget(preview_card, 0)

        controls = Card(margins=(16, 14, 16, 14))
        model_row = QHBoxLayout()
        model_row.setSpacing(10)
        model_label = QLabel("模型")
        model_label.setStyleSheet("font-size: 9pt; color: #5D6B82; font-weight: 600;")
        model_row.addWidget(model_label)
        model_row.addStretch(1)
        model_row.addWidget(self._make_model_combo(190))
        controls.add_layout(model_row)
        controls.add(self._make_choose())
        controls.add(self._make_upload())
        controls.add_layout(self._make_actions())
        controls.add(self._make_info())
        controls.body.addStretch(1)
        body.addWidget(controls, 1)
        root.addLayout(body, 1)

        root.addWidget(self._make_library_card(height=128), 0)

    def _build_v2(self, root: QVBoxLayout) -> None:
        """工作台：左侧信息栏 + 右侧模型/上传/皮肤库卡片。"""
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(3)
        title = QLabel("皮肤工坊")
        title.setStyleSheet("font-size: 17pt; font-weight: 700;")
        titles.addWidget(title)
        subtitle = QLabel("选图 → 校验 → 预览 → 上传，全部在本地完成")
        subtitle.setStyleSheet(f"color: {MUTED}; font-size: 8.6pt;")
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch(1)
        header.addWidget(self._make_status())
        root.addLayout(header)

        main = QHBoxLayout()
        main.setSpacing(14)

        rail = QFrame()
        rail.setObjectName("skinRail")
        rail.setFixedWidth(248)
        rail.setStyleSheet(
            "QFrame#skinRail { background: rgba(255, 255, 255, 0.62);"
            " border: 1px solid rgba(227, 233, 242, 0.80);"
            " border-radius: 14px; }"
        )
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(0, 0, 0, 0)
        rail_layout.setSpacing(0)
        rail_layout.addWidget(self._make_preview(PREVIEW_PX, 208, top_only=True))

        rail_body = QWidget()
        rail_body_layout = QVBoxLayout(rail_body)
        rail_body_layout.setContentsMargins(14, 12, 14, 14)
        rail_body_layout.setSpacing(7)

        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        account = QLabel(self.window.config.username)
        account.setStyleSheet("font-size: 11.5pt; font-weight: 700;")
        name_row.addWidget(account)
        tag = QLabel("离线账户")
        tag.setStyleSheet(f"color: {MUTED}; font-size: 8.2pt;")
        name_row.addWidget(tag)
        name_row.addStretch(1)
        rail_body_layout.addLayout(name_row)
        rail_body_layout.addWidget(self._make_meta())

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background: rgba(227, 233, 242, 0.80);")
        rail_body_layout.addWidget(divider)
        rail_hint = QLabel("只接受 64×64 / 64×32 PNG；上传后，装了 CustomSkinLoader 的实例会显示。")
        rail_hint.setWordWrap(True)
        rail_hint.setStyleSheet(f"color: {MUTED}; font-size: 8.2pt;")
        rail_body_layout.addWidget(rail_hint)
        rail_body_layout.addStretch(1)
        rail_layout.addWidget(rail_body, 1)
        main.addWidget(rail, 0)

        work = QVBoxLayout()
        work.setSpacing(10)

        model_card = Card(margins=(14, 12, 14, 12))
        model_row = QHBoxLayout()
        model_row.setSpacing(10)
        model_label = QLabel("模型")
        model_label.setStyleSheet("font-size: 9pt; color: #5D6B82; font-weight: 600;")
        model_row.addWidget(model_label)
        model_row.addStretch(1)
        model_row.addWidget(self._make_model_combo(190))
        model_card.add_layout(model_row)
        work.addWidget(model_card, 0)

        file_card = Card(margins=(14, 12, 14, 12))
        file_card.add(self._make_choose())
        file_card.add(self._make_upload())
        file_card.add_layout(self._make_actions())
        file_card.add(self._make_info())
        work.addWidget(file_card, 0)

        work.addWidget(self._make_library_card(), 1)
        main.addLayout(work, 1)
        root.addLayout(main, 1)

    def _build_v3(self, root: QVBoxLayout) -> None:
        """极简单栏：居中标题 + 横向 Hero 卡 + 皮肤库。"""
        root.setContentsMargins(26, 18, 24, 16)
        root.setSpacing(12)

        head = QVBoxLayout()
        head.setSpacing(4)
        kicker = QLabel("SKIN STUDIO")
        kicker.setAlignment(Qt.AlignmentFlag.AlignCenter)
        kicker.setStyleSheet(f"color: {MUTED}; font-size: 8pt;")
        head.addWidget(kicker)
        title = QLabel("我的皮肤")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 19pt; font-weight: 700;")
        head.addWidget(title)
        subtitle = QLabel("一张 PNG，换一个在方块世界里的样子")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet(f"color: {MUTED}; font-size: 8.4pt;")
        head.addWidget(subtitle)
        root.addLayout(head)

        hero = Card(margins=(18, 16, 18, 16))
        row = QHBoxLayout()
        row.setSpacing(18)
        stage = self._make_preview(150, 200)
        stage.setFixedWidth(200)
        row.addWidget(stage, 0)

        info_col = QVBoxLayout()
        info_col.setSpacing(8)
        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        account = QLabel(self.window.config.username)
        account.setStyleSheet("font-size: 16pt; font-weight: 700;")
        name_row.addWidget(account)
        tag = QLabel("离线账户")
        tag.setStyleSheet(f"color: {MUTED}; font-size: 8.4pt;")
        name_row.addWidget(tag)
        name_row.addStretch(1)
        info_col.addLayout(name_row)

        status_row = QHBoxLayout()
        status_row.addWidget(self._make_status())
        status_row.addStretch(1)
        info_col.addLayout(status_row)
        info_col.addWidget(self._make_meta())

        model_row = QHBoxLayout()
        model_row.setSpacing(10)
        model_label = QLabel("模型")
        model_label.setStyleSheet("font-size: 9pt; color: #5D6B82; font-weight: 600;")
        model_row.addWidget(model_label)
        model_row.addWidget(self._make_model_combo(180))
        model_row.addStretch(1)
        info_col.addLayout(model_row)

        cta = QHBoxLayout()
        cta.setSpacing(9)
        cta.addWidget(self._make_choose("＋  选择 PNG…"), 1)
        cta.addWidget(self._make_upload(), 0)
        info_col.addLayout(cta)
        info_col.addLayout(self._make_actions(flat=True))
        info_col.addWidget(self._make_info())
        info_col.addStretch(1)
        row.addLayout(info_col, 1)
        hero.add_layout(row)
        root.addWidget(hero, 0)
        root.addWidget(self._make_library_card(), 1)

    def _label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("muted")
        return label

    # ------------------------------------------------------------------ 状态
    def shutdown(self) -> None:
        """对话框关闭时置位；后台线程回来的回调不再触碰已销毁的控件。"""
        self._closed = True

    def _is_closed(self) -> bool:
        return self._closed

    def _base(self) -> str:
        return getattr(self.window.config, "download_base", "") or ""

    def _reload_state(self) -> None:
        enabled = bool(self._base())
        online = enabled and not self._busy
        self.upload.setEnabled(enabled and self._data is not None and not self._busy)
        self.choose.setEnabled(not self._busy)
        self.default_btn.setEnabled(not self._busy)
        self.sync_btn.setEnabled(online)
        self.delete_btn.setEnabled(online)
        self.library_refresh.setEnabled(online)
        if not enabled:
            self.info.setText("尚未配置下载源，无法上传皮肤（按 F12 打开下载源设置）。")
        elif self._data is None:
            self.info.setText("请先选择一张皮肤图片，或点「使用默认」。")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._reload_state()

    # ------------------------------------------------------------------ 交互
    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择皮肤图片", "", "PNG 图片 (*.png)")
        if not path:
            return
        try:
            data, info = read_skin(
                path,
                self.window.config.username,
                model=self._model_value(),
            )
        except SkinError as exc:
            self.status_badge.set("无效", "red")
            self.info.setText(str(exc))
            self._data = None
            self.upload.setEnabled(False)
            return
        self._apply_data(data, info)
        base = getattr(self.window.config, "download_base", "") or ""
        self.upload.setEnabled(bool(base) and not self._busy)

    def _model_value(self) -> str:
        return ["auto", "classic", "slim"][self.model.currentIndex()]

    def _model_changed(self) -> None:
        if self._data is None:
            return
        try:
            info = inspect_skin(
                self._data, self.window.config.username, model=self._model_value()
            )
        except SkinError:
            return
        self._info = info
        self._apply_info(info)

    def _apply_info(self, info: Any) -> None:
        self.status_badge.set(
            "默认皮肤" if info.name == "default" else f"{info.model}", "blue"
        )
        self.meta_label.setText(f"{info.width}×{info.height} · {info.model} · {info.size} 字节")
        self.info.setText(
            f"{info.width}×{info.height} · {info.model} · {info.size} 字节 · "
            f"SHA1 {info.sha1[:12]}"
        )

    def _apply_data(self, data: bytes, info: Any, *, note: str = "") -> None:
        self._data = data
        self._info = info
        self._render_preview(data)
        self._apply_info(info)
        self.upload.setEnabled(bool(self._base()) and not self._busy)
        if note:
            self.info.setText(note + "\n" + self.info.text())

    def _use_default(self) -> None:
        # 内置贴图是经典布局，默认按 classic 标注/上传；用户显式选 slim 时才标 slim。
        model = "slim" if self._model_value() == "slim" else "classic"
        data = default_skin_bytes(model)
        try:
            info = inspect_skin(data, self.window.config.username, model=model)
        except SkinError as exc:
            self.info.setText(str(exc))
            return
        self._apply_data(data, info, note="已加载内置默认皮肤（可直接上传）。")
        self.status_badge.set("默认皮肤", "blue")

    def _sync(self) -> None:
        if self._busy or not self._base():
            return
        self._set_busy(True)
        self.status_badge.set("同步中…", "amber")
        name = self.window.config.username
        model = self._model_value()

        def work() -> None:
            try:
                downloader = self.window.make_downloader(progress=False)
                mirror = self.window.source(downloader)
                data = fetch_skin(
                    name, downloader=downloader, mirror=mirror, model=model, fallback=False
                )
                if not data:
                    raise SkinError("镜像上没有该账户的皮肤")
                info = inspect_skin(data, name, model=model)
                save_local_skin(info, data)
                avatar = head_avatar(data, size=128)
                self.window.post(lambda: self._sync_done(data, info, avatar, None))
            except Exception as exc:  # noqa: BLE001 - 统一转成界面提示
                self.window.post(lambda e=str(exc): self._sync_done(None, None, None, e))

        threading.Thread(target=work, daemon=True).start()

    def _sync_done(self, data, info, avatar, error) -> None:
        if self._closed:
            return
        self._set_busy(False)
        if error:
            self.status_badge.set("同步失败", "red")
            self.info.setText(error)
            return
        self._apply_data(data, info, note=f"已从镜像同步 {info.model} 皮肤。")
        self.status_badge.set("已同步", "green")
        if avatar:
            self.window.apply_avatar(avatar)

    def _delete(self) -> None:
        if self._busy or not self._base():
            return
        answer = QMessageBox.question(
            self,
            "删除皮肤",
            f"确定删除镜像上的 {self.window.config.username} 皮肤吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True)
        self.status_badge.set("删除中…", "amber")
        name = self.window.config.username

        def work() -> None:
            try:
                downloader = self.window.make_downloader(progress=False)
                mirror = self.window.source(downloader)
                removed = delete_skin(name, downloader=downloader, mirror=mirror)
                remove_local_skin(name)
                self.window.post(lambda: self._delete_done(removed, None))
            except Exception as exc:  # noqa: BLE001
                self.window.post(lambda e=str(exc): self._delete_done(None, e))

        threading.Thread(target=work, daemon=True).start()

    def _delete_done(self, removed, error) -> None:
        if self._closed:
            return
        self._set_busy(False)
        if error:
            self.status_badge.set("删除失败", "red")
            self.info.setText(error)
            return
        self._data = None
        self._info = None
        self.preview.setPixmap(initial_pixmap(self.window.config.username, 128, "skin"))
        self.meta_label.setText("尚未选择皮肤")
        self.status_badge.set("已删除", "gray")
        self.info.setText(
            "已删除镜像皮肤" + (f"（{len(removed)} 个文件）" if removed else "（镜像上本来就没有）")
        )
        self.window.apply_avatar(head_avatar(default_skin_bytes(), size=128))
        self._refresh_library()

    # ------------------------------------------------------------------ 皮肤库
    def _refresh_library(self) -> None:
        if not self._base():
            self.library.clear()
            item = QListWidgetItem("未配置下载源，皮肤库不可用")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.library.addItem(item)
            return
        self.library_refresh.setEnabled(False)
        self.library.clear()
        self.library.addItem("正在读取…")

        def work() -> None:
            try:
                downloader = self.window.make_downloader(progress=False)
                mirror = self.window.source(downloader)
                items = list_skins(downloader=downloader, mirror=mirror, limit=60)
                self.window.post(lambda: self._show_library(items, None))
            except Exception as exc:  # noqa: BLE001
                self.window.post(lambda e=str(exc): self._show_library([], e))

        threading.Thread(target=work, daemon=True).start()

    def _show_library(self, items: list[dict[str, Any]], error: str | None) -> None:
        if self._closed:
            return
        self.library.clear()
        if not self._busy:
            self.library_refresh.setEnabled(True)
        if error:
            item = QListWidgetItem(f"读取失败：{error}")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.library.addItem(item)
            return
        if not items:
            item = QListWidgetItem("镜像皮肤库还是空的，上传第一张吧")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.library.addItem(item)
            return
        for entry in items:
            name = str(entry.get("name") or "")
            model = str(entry.get("model") or "classic")
            label = f"{name}  ·  {model}"
            if name == self.window.config.username:
                label += "  ·  我的"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, {"name": name, "model": model})
            self.library.addItem(item)

    def _preview_library_item(self, item: QListWidgetItem) -> None:
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(payload, dict) or self._busy:
            return
        name = str(payload.get("name") or "")
        model = str(payload.get("model") or "classic")
        self.status_badge.set("读取中…", "amber")

        def work() -> None:
            try:
                downloader = self.window.make_downloader(progress=False)
                mirror = self.window.source(downloader)
                data = fetch_skin(
                    name, downloader=downloader, mirror=mirror, model=model, fallback=False
                )
                if not data:
                    raise SkinError("这张皮肤已被删除")
                info = inspect_skin(data, self.window.config.username, model=model)
                self.window.post(lambda: self._library_preview_done(data, info, name, None))
            except Exception as exc:  # noqa: BLE001
                self.window.post(lambda e=str(exc): self._library_preview_done(None, None, name, e))

        threading.Thread(target=work, daemon=True).start()

    def _library_preview_done(self, data, info, name, error) -> None:
        if self._closed:
            return
        if error:
            self.status_badge.set("读取失败", "red")
            self.info.setText(error)
            return
        self._apply_data(data, info, note=f"预览皮肤库中的 {name}（上传会存到当前账户）。")
        self.status_badge.set("库预览", "blue")

    def _render_preview(self, data: bytes) -> None:
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        scaled = pixmap.scaled(
            PREVIEW_PX - 30,
            PREVIEW_PX - 30,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        self.preview.setPixmap(scaled)

    def _upload(self) -> None:
        if self._data is None:
            return
        self.upload.setEnabled(False)
        self.status_badge.set("上传中…", "amber")
        self.window.run_task(
            "上传皮肤",
            self._upload_worker,
            on_done=self._upload_finished,
        )

    def _upload_worker(self, cancel: Any) -> str:
        data, info = self._data, self._info
        downloader = self.window.make_downloader(progress=False)
        mirror = self.window.source(downloader)
        result = upload_skin(data, info, downloader=downloader, mirror=mirror)
        save_local_skin(info, data)
        avatar = head_avatar(data, size=128)
        self.window.post(lambda a=avatar: self.window.apply_avatar(a))
        return f"已上传 {info.name}（{info.model}）· {result.get('status', 'ok')}"

    def _upload_finished(self) -> None:
        if self._closed:
            return
        self.status_badge.set("已上传", "green")
        self._reload_state()
        self._refresh_library()

    # ------------------------------------------------------------------ 头像
    @staticmethod
    def current_avatar(name: str, size: int = 128) -> bytes | None:
        """取本地缓存皮肤的头像字节（启动页头像用）；无皮肤返回 ``None``。"""
        try:
            data = load_local_skin(name, model="classic") or load_local_skin(name, model="slim")
        except SkinError:
            return None
        if not data:
            return None
        avatar = head_avatar(data, size=size)
        return avatar or None
