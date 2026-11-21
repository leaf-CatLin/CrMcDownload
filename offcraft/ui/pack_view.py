"""资源包内容浏览：左侧目录树，右侧图片预览。

资源包（``.zip`` 或文件夹）本质是一个资源目录树，玩家想「看看这个包装了什么」
时最直观的方式是：左边像资源管理器一样列目录，右边看图。

实现要点：

1. 只读浏览，**不解压到磁盘**：``.zip`` 用 :class:`zipfile.ZipFile` 读，文件夹直接
   :meth:`Path.rglob`；图片按需读进内存再 ``QPixmap.loadFromData``。
2. 目录树按 ``assets/<命名空间>/<子目录>`` 分层，非图片文件也列出来（灰显），
   这样「这个包改了哪些东西」一目了然。
3. 图片预览等比缩放、超出可视区域可滚动；不是图片的文件显示文本摘要或占位。
4. 大包保护：单包最多列 ``MAX_ENTRIES`` 个条目，单张图超过 ``MAX_IMAGE_BYTES``
   不加载，避免一个几百 MB 的资源包把界面拖死。
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtGui import QBrush, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from offcraft.ui.theme import MUTED, TEXT
from offcraft.ui.widgets import Card

__all__ = ["PackBrowser", "pack_entries", "PACK_IMAGE_SUFFIXES"]

#: 视为图片的扩展名
PACK_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".tga", ".bmp")
#: 单个资源包最多列出的条目数（防止超大包把树撑爆）
MAX_ENTRIES = 4000
#: 单张图片最大字节数（超过就只显示尺寸信息，不加载）
MAX_IMAGE_BYTES = 16 * 1024 * 1024
#: 目录树默认展开到第几层（资源包常见 4~5 层，展开 3 层既不折叠得只剩顶项，
#: 也不至于一屏铺开上千行）
DEFAULT_EXPAND_DEPTH = 3


@dataclass(slots=True)
class PackEntry:
    """资源包里的一个条目。"""

    name: str            # 包内相对路径，用 / 分隔
    size: int = 0
    is_dir: bool = False

    @property
    def suffix(self) -> str:
        return Path(self.name).suffix.lower()

    @property
    def is_image(self) -> bool:
        return self.suffix in PACK_IMAGE_SUFFIXES


@dataclass(slots=True)
class PackIndex:
    """一个资源包的内容索引。"""

    root: Path
    entries: list[PackEntry] = field(default_factory=list)
    truncated: bool = False

    @property
    def image_count(self) -> int:
        return sum(1 for entry in self.entries if entry.is_image and not entry.is_dir)


def _from_zip(path: Path) -> PackIndex:
    index = PackIndex(root=path)
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if len(index.entries) >= MAX_ENTRIES:
                    index.truncated = True
                    break
                name = info.filename.replace("\\", "/")
                if name.endswith("/"):
                    continue
                index.entries.append(PackEntry(name=name, size=info.file_size))
    except (OSError, zipfile.BadZipFile):
        return index
    return index


def _from_folder(path: Path) -> PackIndex:
    index = PackIndex(root=path)
    for item in sorted(path.rglob("*")):
        if len(index.entries) >= MAX_ENTRIES:
            index.truncated = True
            break
        if not item.is_file():
            continue
        try:
            size = item.stat().st_size
        except OSError:
            size = 0
        index.entries.append(
            PackEntry(name=item.relative_to(path).as_posix(), size=size)
        )
    return index


def pack_entries(path: Path | str) -> PackIndex:
    """列出一个资源包（zip 或文件夹）的内容。"""
    root = Path(path)
    if root.is_dir():
        return _from_folder(root)
    if root.is_file():
        return _from_zip(root)
    return PackIndex(root=root)


class _CheckerCanvas(QWidget):
    """图片预览画布：透明区域铺经典灰白棋盘格。

    资源包里的贴图大量带透明通道（方块贴图、GUI 元素），直接贴在卡片底色上
    会让「哪里是透明」完全看不出来。这里按图像编辑器的惯例画 8px 的灰白棋盘格
    （QPainter 自绘，不用额外图片资源），再把图片居中叠上去。
    """

    #: 棋盘格边长（像素）
    CELL = 8
    #: 经典配色：浅灰 + 白
    LIGHT = QColor("#FFFFFF")
    DARK = QColor("#D6D6D6")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._message = ""
        self.setMinimumSize(QSize(200, 200))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_pixmap(self, pixmap: QPixmap | None) -> None:
        self._pixmap = pixmap if pixmap is not None and not pixmap.isNull() else None
        self._message = ""
        self.update()

    def set_message(self, text: str) -> None:
        """没有可显示图片时的提示文字（此时不画棋盘格）。"""
        self._pixmap = None
        self._message = text
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = self.rect()

        if self._pixmap is None:
            if self._message:
                painter.setPen(QColor(MUTED))
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._message)
            painter.end()
            return

        # 棋盘格只铺在图片实际占用的区域内（图片比可视区小的时候，
        # 四周不应出现一片棋盘格，那样屏幕会很花）
        scaled = self._scaled_for(rect.size())
        target = QRect(
            rect.x() + (rect.width() - scaled.width()) // 2,
            rect.y() + (rect.height() - scaled.height()) // 2,
            scaled.width(),
            scaled.height(),
        )
        painter.save()
        painter.setClipRect(target)
        painter.fillRect(target, self.LIGHT)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.DARK)
        cell = self.CELL
        cols = target.width() // cell + 1
        rows = target.height() // cell + 1
        for row in range(rows):
            for col in range(cols):
                if (row + col) % 2 == 0:
                    painter.drawRect(
                        target.x() + col * cell,
                        target.y() + row * cell,
                        cell,
                        cell,
                    )
        painter.restore()

        painter.drawPixmap(target, scaled)
        painter.end()

    def _scaled_for(self, available: QSize) -> QPixmap:
        """按可视区缩放：小图**整数倍放大**，大图等比缩小。

        资源包里的贴图常见 16×16 / 32×32，原样显示只有指甲盖大，根本看不清
        改了什么；这里按能放下的最大整数倍放大，并用最近邻插值保住像素画的
        硬边（平滑插值会把像素画糊成一团）。
        """
        assert self._pixmap is not None
        pixmap = self._pixmap
        if pixmap.width() <= 0 or pixmap.height() <= 0:
            return pixmap
        if pixmap.width() <= available.width() and pixmap.height() <= available.height():
            # 整数倍：max_zoom 取宽高各自能放大的倍数里较小的那个
            zoom = min(
                available.width() // pixmap.width(),
                available.height() // pixmap.height(),
            )
            if zoom <= 1:
                return pixmap
            # 上限 16 倍：一张 16×16 放大到 256×256 已经足够看清每个像素
            zoom = min(zoom, 16)
            return pixmap.scaled(
                pixmap.width() * zoom,
                pixmap.height() * zoom,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        return pixmap.scaled(
            available,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )


class PackBrowser(QWidget):
    """资源包内容浏览器：左目录树 + 右图片预览。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._index: PackIndex | None = None
        self._build()

    # ------------------------------------------------------------------ 构建
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.title = QLabel("资源包内容")
        self.title.setObjectName("h3")
        header.addWidget(self.title)
        self.summary = QLabel("")
        self.summary.setObjectName("muted")
        header.addWidget(self.summary)
        header.addStretch(1)
        self.expand_btn = QPushButton("全部展开")
        self.expand_btn.setObjectName("ghost")
        self.expand_btn.setFixedHeight(28)
        self.expand_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.expand_btn.setToolTip("展开所有目录层级")
        self.expand_btn.clicked.connect(lambda: self.tree.expandAll())
        header.addWidget(self.expand_btn)
        self.collapse_btn = QPushButton("全部折叠")
        self.collapse_btn.setObjectName("ghost")
        self.collapse_btn.setFixedHeight(28)
        self.collapse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.collapse_btn.clicked.connect(lambda: self.tree.collapseAll())
        header.addWidget(self.collapse_btn)
        root.addLayout(header)

        # 两个面板各自套一张卡片，跟其它页面保持同样的视觉层次
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(10)

        # 左侧：目录树卡片
        tree_card = Card(margins=(0, 10, 0, 10))
        tree_head = QHBoxLayout()
        tree_head.setContentsMargins(14, 0, 14, 0)
        tree_head.setSpacing(6)
        tree_title = QLabel("目录")
        tree_title.setObjectName("h4")
        tree_head.addWidget(tree_title)
        self.tree_hint = QLabel("")
        self.tree_hint.setObjectName("mutedSmall")
        tree_head.addWidget(self.tree_hint)
        tree_head.addStretch(1)
        tree_card.add_layout(tree_head)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["文件", "大小"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(16)
        # 路径靠缩进表达层级，不需要横向滚动条；关掉它，超长名靠 tooltip
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tree.setWordWrap(False)
        header_0 = self.tree.header()
        header_0.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header_0.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        # 树不吃 QSS 的 color 继承：这里显式指定表头与正文颜色，
        # 避免「文字发浅」——全局 QHeaderView 用的是 textMuted。
        self.tree.setStyleSheet(
            "QTreeWidget { padding: 0px 6px 6px 6px; color: %s; }"
            " QTreeWidget::item { color: %s; }"
            " QHeaderView::section { color: %s; font-weight: 600; }"
            % (TEXT, TEXT, MUTED)
        )
        self.tree.itemSelectionChanged.connect(self._on_selected)
        tree_card.add(self.tree, 1)
        splitter.addWidget(tree_card)

        # 右侧：预览卡片
        preview_card = Card(margins=(0, 10, 0, 10))
        preview_head = QHBoxLayout()
        preview_head.setContentsMargins(14, 0, 14, 0)
        preview_head.setSpacing(6)
        preview_title = QLabel("预览")
        preview_title.setObjectName("h4")
        preview_head.addWidget(preview_title)
        preview_head.addStretch(1)
        preview_card.add_layout(preview_head)

        self._preview_scroll = QScrollArea()
        self._preview_scroll.setWidgetResizable(True)
        self._preview_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._preview_scroll.setStyleSheet("QScrollArea { background: transparent; }")
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(14, 4, 14, 14)
        holder_layout.setSpacing(8)
        self.preview = _CheckerCanvas()
        self.preview.set_message("在左侧选择一张图片查看预览")
        holder_layout.addWidget(self.preview, 1)
        self.preview_note = QLabel("")
        self.preview_note.setObjectName("muted")
        self.preview_note.setWordWrap(True)
        self.preview_note.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        holder_layout.addWidget(self.preview_note)
        self._preview_scroll.setWidget(holder)
        preview_card.add(self._preview_scroll, 1)
        splitter.addWidget(preview_card)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        root.addWidget(splitter, 1)

    # ------------------------------------------------------------------ 加载
    def load(self, path: Path | str) -> None:
        """载入一个资源包并重建目录树。"""
        self._index = pack_entries(path)
        index = self._index
        self.title.setText(f"资源包内容 · {index.root.name}")
        self.summary.setText(
            f"{len(index.entries)} 个文件 · {index.image_count} 张图片"
            + ("（条目过多，只显示前一部分）" if index.truncated else "")
        )
        self.tree.clear()
        self.preview.set_message("在左侧选择一张图片查看预览")
        self.preview_note.setText("")
        self._build_tree(index)
        # 目录深是资源包的常态（assets/<ns>/textures/...），默认展开前几层，
        # 让玩家一眼看到结构而不是三行折叠项；再深的分支留给手动展开。
        self.tree.expandToDepth(DEFAULT_EXPAND_DEPTH)
        self.tree_hint.setText(f"{len(index.entries)} 项")

    def _build_tree(self, index: PackIndex) -> None:
        """按路径分层建树（每个中间目录复用同一个 QTreeWidgetItem）。"""
        text_brush = QBrush(QColor(TEXT))
        muted_brush = QBrush(QColor(MUTED))
        folders: dict[str, QTreeWidgetItem] = {}
        for entry in index.entries:
            parts = entry.name.split("/")
            parent = None
            prefix = ""
            for _depth, part in enumerate(parts[:-1]):
                prefix = f"{prefix}{part}/" if prefix else f"{part}/"
                node = folders.get(prefix)
                if node is None:
                    node = QTreeWidgetItem([part, ""])
                    node.setData(0, Qt.ItemDataRole.UserRole, None)
                    # 目录名与文件名都要显式给深色：树不继承 QSS 的 color，
                    # 只靠调色板时在 0.62 透明度的卡片上会显得发灰（看着「浅」）
                    node.setForeground(0, text_brush)
                    node.setForeground(1, muted_brush)
                    if parent is None:
                        self.tree.addTopLevelItem(node)
                    else:
                        parent.addChild(node)
                    folders[prefix] = node
                parent = node
            leaf = QTreeWidgetItem([parts[-1], _human_size(entry.size)])
            leaf.setData(0, Qt.ItemDataRole.UserRole, entry.name)
            leaf.setToolTip(0, entry.name)
            if entry.is_image:
                leaf.setForeground(0, text_brush)
            else:
                # 非图片灰显，但仍可点（右侧显示文件信息）。用 token 的 textMuted
                # 而不是 palette.darker()——后者作用在黑色上还是黑色，反而看不清。
                leaf.setForeground(0, muted_brush)
            leaf.setForeground(1, muted_brush)
            if parent is None:
                self.tree.addTopLevelItem(leaf)
            else:
                parent.addChild(leaf)

    # ------------------------------------------------------------------ 预览
    def _on_selected(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            return
        name = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not name or self._index is None:
            return
        data = self._read_entry(str(name))
        if data is None:
            self.preview.set_message("无法读取该文件")
            self.preview_note.setText(str(name))
            return
        if len(data) > MAX_IMAGE_BYTES:
            self.preview.set_message("图片过大，已跳过加载")
            self.preview_note.setText(f"{name} · {_human_size(len(data))}")
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self.preview.set_message("该文件不是可预览的图片")
            self.preview_note.setText(f"{name} · {_human_size(len(data))}")
            return
        # 缩放交给画布按当前可视区处理：窗口拉伸时图片会跟着自适应
        self.preview.set_pixmap(pixmap)
        self.preview_note.setText(
            f"{name} · {pixmap.width()}×{pixmap.height()} · {_human_size(len(data))}"
        )

    def _read_entry(self, name: str) -> bytes | None:
        """按需读取单个条目（zip 里解压到内存，文件夹直接读）。"""
        if self._index is None:
            return None
        root = self._index.root
        try:
            if root.is_dir():
                return (root / name).read_bytes()
            with zipfile.ZipFile(root) as archive:
                return archive.read(name)
        except (OSError, KeyError, zipfile.BadZipFile):
            return None


def _human_size(value: int) -> str:
    size = float(value or 0)
    for unit in ("B", "KiB", "MiB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"
