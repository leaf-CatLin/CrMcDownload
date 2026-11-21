"""皮肤页：选图 → 本地保存 → 生成资源包（PCL 做法，完全离线）。

与旧实现的区别（按用户要求）：**移除一切上传 / 镜像皮肤库 / 删除远端**逻辑。
皮肤只存本机，通过生成本地资源包生效——这正是 PCL 的思路：

1. 皮肤 PNG 保存到 ``<数据目录>/skins/<账户名>.png``（头像、下次启动都用它）；
2. 每次启动前把它写成 ``<游戏目录>/resourcepacks/CrMCDownloadSkin/``，
   覆盖原版玩家贴图；不需要 CustomSkinLoader，原版实例也能用；
3. 好处：不联网、不依赖 mod、不进服务端，换账户/换实例都不会互相干扰。

入口挂在启动页「皮肤」按钮与设置页卡片上（由 MainWindow 包成对话框）。
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from offcraft.skins.avatar import head_avatar
from offcraft.skins.default import default_skin_bytes
from offcraft.skins.pack import PACK_DIR_NAME, remove_skin_pack
from offcraft.skins.upload import (
    SkinError,
    inspect_skin,
    load_slot_skin,
    read_skin,
    remove_slot_skin,
    save_slot_skin,
)
from offcraft.ui.theme import MUTED
from offcraft.ui.widgets import Badge, Card

__all__ = ["LAYOUT_CHOICES", "SkinPage"]

#: 皮肤页样式；新增布局时在这里登记，并在 SkinPage 里加对应的 ``_build_<id>``
LAYOUT_CHOICES = (
    ("v1", "双栏卡片"),
    ("v2", "工作台"),
    ("v3", "极简单栏"),
)

PREVIEW_PX = 170


def _pixmap_from_bytes(data: bytes) -> QPixmap:
    pixmap = QPixmap()
    if data:
        pixmap.loadFromData(data)
    return pixmap


class _PreviewTile(QWidget):
    """棋盘格底 + 圆角贴图预览（纯 QPainter 绘制，不依赖网页）。"""

    def __init__(self, size: int = PREVIEW_PX, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def setPixmap(self, pixmap: QPixmap) -> None:  # noqa: N802 - 与 QLabel 保持一致
        self._pixmap = pixmap
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        # 棋盘格：透明区域一眼可辨
        cell = 12
        painter.fillRect(rect, QColor("#FFFFFF"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#E8EDF4"))
        for row in range(0, rect.height(), cell):
            for col in range(0, rect.width(), cell):
                if (row // cell + col // cell) % 2 == 0:
                    painter.drawRect(col, row, cell, cell)
        if self._pixmap is not None and not self._pixmap.isNull():
            path = QPainterPath()
            path.addRoundedRect(0, 0, rect.width(), rect.height(), 10, 10)
            painter.setClipPath(path)
            scaled = self._pixmap.scaled(
                rect.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (rect.width() - scaled.width()) // 2
            y = (rect.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        painter.end()


class SkinPage(QWidget):
    """本地皮肤编辑页：选图、预览、保存、生成资源包。"""

    def __init__(self, window: Any, *, read_only: bool = False) -> None:
        super().__init__()
        self.window = window
        self.read_only = read_only
        self._data: bytes | None = None
        self._info: Any | None = None
        self._busy = False
        self._closed = False
        self._build()
        self._load_saved()
        if read_only:
            # 只读入口（例如从启动页快速查看）：允许看，但不给保存/清除按钮
            for name in ("save", "choose", "default_btn", "remove"):
                button = getattr(self, name, None)
                if button is not None:
                    button.setVisible(False)

    # ------------------------------------------------------------------ 构建
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
        stage.setFixedHeight(height)
        stage.setStyleSheet(
            "QFrame#skinStage { background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
            " stop:0 rgba(234, 241, 255, 0.9), stop:1 rgba(255, 255, 255, 0.9));"
            " border: 1px solid rgba(227, 233, 242, 0.9); border-radius: 14px; }"
        )
        layout = QVBoxLayout(stage)
        layout.setContentsMargins(0, 12 if top_only else 0, 0, 0)
        layout.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.preview = _PreviewTile(size)
        row.addWidget(self.preview)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        return stage

    def _make_model_combo(self, width: int | None = 190) -> QComboBox:
        self.model = QComboBox()
        self.model.addItem("自动判定（按图片）", "auto")
        self.model.addItem("经典（4px 手臂）", "classic")
        self.model.addItem("纤细（3px 手臂）", "slim")
        if width:
            self.model.setMinimumWidth(width)
        self.model.currentIndexChanged.connect(self._model_changed)
        return self.model

    def _make_choose(self, text: str = "＋  选择皮肤图片（PNG）") -> QPushButton:
        self.choose = QPushButton(text)
        self.choose.setObjectName("ghost")
        self.choose.setMinimumHeight(38)
        self.choose.setCursor(Qt.CursorShape.PointingHandCursor)
        self.choose.clicked.connect(self._choose_file)
        return self.choose

    def _make_save(self) -> QPushButton:
        self.save = QPushButton("保存到本地并应用")
        self.save.setObjectName("primary")
        self.save.setMinimumHeight(38)
        self.save.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save.setToolTip(
            "皮肤只保存在本机，并生成为资源包；下次启动游戏时自动生效（无需联网、无需 mod）"
        )
        self.save.clicked.connect(self._save_local)
        return self.save

    def _make_actions(self, *, flat: bool = False) -> QHBoxLayout:
        self.sync_btn = QPushButton("使用默认")
        self.sync_btn.setObjectName("ghost")
        self.sync_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sync_btn.setToolTip("恢复内置的 Steve 风格默认皮肤")
        self.sync_btn.clicked.connect(self._use_default)
        self.default_btn = self.sync_btn

        self.delete_btn = QPushButton("清除本地皮肤")
        self.delete_btn.setObjectName("danger")
        self.delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_btn.clicked.connect(self._clear)
        self.remove = self.delete_btn

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.sync_btn)
        row.addWidget(self.delete_btn)
        row.addStretch(1)
        return row

    def _make_info(self) -> QLabel:
        self.info = QLabel("皮肤保存在本机，并会生成为资源包，下次启动自动生效。")
        self.info.setWordWrap(True)
        self.info.setStyleSheet(f"color: {MUTED}; font-size: 8.4pt;")
        return self.info

    def _make_meta(self) -> QLabel:
        self.meta_label = QLabel("尚未选择皮肤")
        self.meta_label.setStyleSheet(f"color: {MUTED}; font-size: 8.4pt;")
        return self.meta_label

    def _make_model_label(self) -> QLabel:
        label = QLabel("模型")
        label.setStyleSheet(f"font-size: 9pt; color: {MUTED}; font-weight: 600;")
        return label

    # ------------------------------------------------------------------ 布局
    def _build_v1(self, root: QVBoxLayout) -> None:
        """双栏卡片：左侧大预览，右侧操作（默认样式）。"""
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
        subtitle = QLabel("选一张 64×64 / 64×32 PNG，保存到本机并自动生成资源包")
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
        model_row.addWidget(self._make_model_label())
        model_row.addStretch(1)
        model_row.addWidget(self._make_model_combo(190))
        controls.add_layout(model_row)
        controls.add(self._make_choose())
        controls.add(self._make_save())
        controls.add_layout(self._make_actions())
        controls.add(self._make_info())
        controls.body.addStretch(1)
        body.addWidget(controls, 1)
        root.addLayout(body, 1)
        root.addStretch(1)

    def _build_v2(self, root: QVBoxLayout) -> None:
        """工作台：左侧信息栏 + 右侧模型/选图卡片。"""
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(3)
        title = QLabel("皮肤工坊")
        title.setStyleSheet("font-size: 17pt; font-weight: 700;")
        titles.addWidget(title)
        subtitle = QLabel("选图 → 校验 → 预览 → 保存，全部在本地完成")
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
        tag = QLabel("本地皮肤")
        tag.setStyleSheet(f"color: {MUTED}; font-size: 8.2pt;")
        name_row.addWidget(tag)
        name_row.addStretch(1)
        rail_body_layout.addLayout(name_row)
        rail_body_layout.addWidget(self._make_meta())

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background: rgba(227, 233, 242, 0.80);")
        rail_body_layout.addWidget(divider)
        rail_hint = QLabel("只接受 64×64 / 64×32 PNG；保存后会生成本地资源包，启动时自动生效。")
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
        model_row.addWidget(self._make_model_label())
        model_row.addStretch(1)
        model_row.addWidget(self._make_model_combo(190))
        model_card.add_layout(model_row)
        work.addWidget(model_card, 0)

        file_card = Card(margins=(14, 12, 14, 12))
        file_card.add(self._make_choose())
        file_card.add(self._make_save())
        file_card.add_layout(self._make_actions())
        file_card.add(self._make_info())
        work.addWidget(file_card, 0)
        work.addStretch(1)
        main.addLayout(work, 1)
        root.addLayout(main, 1)

    def _build_v3(self, root: QVBoxLayout) -> None:
        """极简单栏：居中标题 + 横向 Hero 卡。"""
        root.setContentsMargins(26, 18, 24, 16)
        root.setSpacing(12)

        head = QVBoxLayout()
        head.setSpacing(4)
        kicker = QLabel("SKIN")
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
        tag = QLabel("本地皮肤")
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
        model_row.addWidget(self._make_model_label())
        model_row.addWidget(self._make_model_combo(180))
        model_row.addStretch(1)
        info_col.addLayout(model_row)

        cta = QHBoxLayout()
        cta.setSpacing(9)
        cta.addWidget(self._make_choose("＋  选择 PNG…"), 1)
        cta.addWidget(self._make_save(), 0)
        info_col.addLayout(cta)
        info_col.addLayout(self._make_actions(flat=True))
        info_col.addWidget(self._make_info())
        info_col.addStretch(1)
        row.addLayout(info_col, 1)
        hero.add_layout(row)
        root.addWidget(hero, 0)
        root.addStretch(1)

    # ------------------------------------------------------------------ 状态
    def _load_saved(self) -> None:
        """打开页面时读回本地已保存的皮肤。"""
        try:
            slot = load_slot_skin()
        except SkinError:
            slot = None
        if slot is None:
            self.preview.setPixmap(_pixmap_from_bytes(default_skin_bytes()))
            self.meta_label.setText("尚未选择皮肤（显示默认皮肤）")
            self.status_badge.set("默认皮肤", "gray")
            self._reload_state()
            return
        data, _slot_model = slot
        try:
            info = inspect_skin(data, self.window.config.username, model="auto")
        except SkinError:
            info = None
        self._apply_data(data, info, note="已载入本机保存的皮肤。")
        self.status_badge.set("已保存", "green")

    def _apply_data(self, data: bytes, info: Any, *, note: str = "") -> None:
        self._data = data
        self._info = info
        pixmap = _pixmap_from_bytes(data)
        if not pixmap.isNull():
            self.preview.setPixmap(pixmap)
        if info is not None:
            self.meta_label.setText(
                f"{getattr(info, 'width', '?')}×{getattr(info, 'height', '?')} · "
                f"{getattr(info, 'model', '?')} · {len(data)} B"
            )
        self.status_badge.set("待保存", "amber")
        if note:
            self.info.setText(note)
        self._reload_state()

    def _reload_state(self) -> None:
        has_data = self._data is not None
        for name in ("save", "choose", "default_btn", "delete_btn"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setEnabled(not self._busy)
        if getattr(self, "save", None) is not None:
            self.save.setEnabled(has_data and not self._busy)

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
            self._reload_state()
            return
        self._apply_data(data, info, note="图片已通过校验，点「保存到本地并应用」生效。")

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
        self._apply_data(self._data, info)

    def _use_default(self) -> None:
        data = default_skin_bytes()
        try:
            info = inspect_skin(data, self.window.config.username, model="classic")
        except SkinError:
            info = None
        self._apply_data(data, info, note="已载入内置默认皮肤，保存后生效。")

    def _save_local(self) -> None:
        """保存到本机 + 刷新头像 + 提示会在下次启动生成资源包。"""
        if self._data is None or self._info is None:
            return
        model = getattr(self._info, "model", "classic") or "classic"
        try:
            path = save_slot_skin(self._data, model=model)
        except OSError as exc:
            self.status_badge.set("保存失败", "red")
            self.info.setText(f"保存失败：{exc}")
            return
        avatar = head_avatar(self._data, size=128)
        if avatar:
            self.window.apply_avatar(avatar)
        self.status_badge.set("已保存", "green")
        self.info.setText(
            f"已保存到本机：{path}\n"
            f"启动游戏时会自动生成资源包（resourcepacks/{PACK_DIR_NAME}），"
            "无需联网、无需安装 mod。"
        )

    def _clear(self) -> None:
        answer = QMessageBox.question(
            self,
            "清除本地皮肤",
            "确定清除本机保存的皮肤吗？（游戏内会恢复默认皮肤）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        remove_slot_skin()
        # 顺手把已生成的资源包也删掉，否则游戏里还会是旧皮肤
        try:
            remove_skin_pack(self._game_dir())
        except OSError:
            pass
        self._data = None
        self._info = None
        self.preview.setPixmap(_pixmap_from_bytes(default_skin_bytes()))
        self.meta_label.setText("尚未选择皮肤")
        self.status_badge.set("已清除", "gray")
        self.info.setText("已清除本地皮肤，游戏内将恢复默认皮肤。")
        self.window.apply_avatar(head_avatar(default_skin_bytes(), size=128))
        self._reload_state()

    def _game_dir(self):
        getter = getattr(self.window, "game_dir", None)
        if callable(getter):
            return getter()
        return ""

    # ------------------------------------------------------------------ 生命周期
    def shutdown(self) -> None:
        self._closed = True

    # ------------------------------------------------------------------ 头像
    @staticmethod
    def current_avatar(name: str, size: int = 128) -> bytes | None:
        """取本地缓存皮肤的头像字节（启动页头像用）；无皮肤返回 ``None``。"""
        try:
            slot = load_slot_skin()
        except SkinError:
            return None
        if slot is None:
            return None
        data, _model = slot
        avatar = head_avatar(data, size=size)
        return avatar or None
