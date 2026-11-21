"""启动前检查：本次要加载的模组 / 资源包 / 光影是否真的在加载目录里。

版本隔离时游戏目录是 ``versions/<id>``，玩家手工放进 ``.minecraft/mods``
的文件不会被加载；反过来关掉隔离时放进 ``versions/<id>/mods`` 也一样。
这里把两边都扫一遍，指出「不在加载目录」的条目，便于在启动页禁用 / 移动。

另外提供 :func:`has_shader_loader`：光影（shaderpack）只有装了 Iris 或
OptiFine 这类前置才会生效，没装时启动页不该给玩家一个点了没反应的「光影」页签。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from offcraft.instances import instance_game_dir

__all__ = [
    "KIND_NAMES",
    "KINDS",
    "ResourceAudit",
    "ResourceEntry",
    "audit_resources",
    "PACK_ICON_NAME",
    "has_shader_loader",
    "pack_icon_bytes",
    "shader_loader_name",
]

#: 目录名 -> 中文名（顺序即启动页分类顺序）
KIND_NAMES = {
    "mods": "模组",
    "resourcepacks": "资源包",
    "shaderpacks": "光影",
}
KINDS = tuple(KIND_NAMES)

FILE_SUFFIXES = (".jar", ".zip")
#: 关掉隔离时最多扫这么多个版本目录，避免全盘遍历拖慢启动
MAX_VERSION_DIRS = 40

#: 光影前置（按文件名前缀识别，都是 jar 里带 shaderpack 加载能力的老牌 mod）
#: - Iris：Fabric 上的光影前置，文件名 ``iris-*.jar``
#: - OptiFine：``OptiFine_*.jar`` / ``optifine*.jar``
#: - Oculus：Forge 版 Iris 移植，``oculus-*.jar``
_SHADER_LOADERS = (
    ("Iris", ("iris-", "iris_")),
    ("OptiFine", ("optifine",)),
    ("Oculus", ("oculus",)),
)


def shader_loader_name(mods_dir: Path | str) -> str:
    """返回已安装的光影前置名（Iris / OptiFine / Oculus）；没有则空串。"""
    try:
        items = list(Path(mods_dir).iterdir())
    except OSError:
        return ""
    names = []
    for item in items:
        name = item.name.lower()
        if name.endswith(".disabled"):
            # 被禁用的前置不算生效
            continue
        if not name.endswith(".jar"):
            continue
        names.append(name)
    for label, prefixes in _SHADER_LOADERS:
        if any(name.startswith(prefix) for name in names for prefix in prefixes):
            return label
    return ""


def has_shader_loader(mods_dir: Path | str) -> bool:
    """``mods`` 目录里是否有能加载光影的前置。"""
    return bool(shader_loader_name(mods_dir))


#: 资源包内声明图标的文件名（Minecraft 用它显示在资源包列表里）
PACK_ICON_NAME = "pack.png"


def pack_icon_bytes(path: Path | str) -> bytes | None:
    """读取资源包/光影包自带的 ``pack.png``，没有就返回 ``None``。

    资源包作者几乎都会在根目录放 ``pack.png``（游戏内资源包列表就显示它），
    用它当列表图标比按文件名生成的占位图直观得多。
    文件夹型资源包直接读，``.zip`` 从压缩包里取；任何异常都静默返回 ``None``，
    由调用方回退到占位图。
    """
    import zipfile

    root = Path(path)
    try:
        if root.is_dir():
            icon = root / PACK_ICON_NAME
            return icon.read_bytes() if icon.is_file() else None
        if not root.is_file():
            return None
        # 注意：``xxx.zip.disabled`` 本身就是那个 zip 文件（只是被改了扩展名），
        # zipfile 只按文件内容判断格式、不看后缀，所以直接打开即可。
        with zipfile.ZipFile(root) as bundle:
            try:
                return bundle.read(PACK_ICON_NAME)
            except KeyError:
                return None
    except (OSError, zipfile.BadZipFile):
        return None


@dataclass(frozen=True, slots=True)
class ResourceEntry:
    kind: str
    path: Path
    disabled: bool
    #: True 表示文件在别的目录里，本次启动不会加载
    misplaced: bool = False


@dataclass(slots=True)
class ResourceAudit:
    load_dir: Path
    entries: list[ResourceEntry] = field(default_factory=list)
    #: options.txt 里已启用、但文件不存在的资源包
    missing: list[str] = field(default_factory=list)

    @property
    def misplaced(self) -> list[ResourceEntry]:
        return [item for item in self.entries if item.misplaced]

    @property
    def problem_count(self) -> int:
        return len(self.misplaced) + len(self.missing)


def _is_resource(path: Path, kind: str) -> bool:
    """资源包目录与 ``*.disabled`` 文件也算资源，禁用项要照常展示。"""
    name = path.name.lower()
    if kind == "resourcepacks" and path.is_dir():
        return True
    if not path.is_file():
        return False
    if name.endswith(".disabled"):
        name = name[: -len(".disabled")]
    return name.endswith(FILE_SUFFIXES)


def _scan(folder: Path, kind: str, *, misplaced: bool) -> list[ResourceEntry]:
    try:
        items = sorted(folder.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    entries: list[ResourceEntry] = []
    for path in items:
        if path.name.startswith("."):
            continue
        try:
            if not _is_resource(path, kind):
                continue
        except OSError:
            continue
        disabled = path.name.lower().endswith(".disabled")
        entries.append(ResourceEntry(kind, path, disabled, misplaced))
    return entries


def _alternate_dirs(
    mc_dir: Path, instance_id: str, isolated: bool, kind: str
) -> list[Path]:
    """返回另一些可能放资源的位置（不含本次的加载目录）。"""
    if isolated:
        return [mc_dir / kind]
    versions = mc_dir / "versions"
    try:
        folders = [p for p in versions.iterdir() if p.is_dir() and p.name != instance_id]
    except OSError:
        return []
    folders.sort(key=lambda p: p.name.lower())
    return [folder / kind for folder in folders[:MAX_VERSION_DIRS]]


def _enabled_packs(game_dir: Path) -> list[str]:
    options = game_dir / "options.txt"
    try:
        lines = options.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.startswith("resourcePacks:"):
            continue
        try:
            data = json.loads(line[len("resourcePacks:") :].strip())
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        return [
            str(item).split("/", 1)[1]
            for item in data
            if str(item).startswith(("file/", "folder/")) and "/" in str(item)
        ]
    return []


def audit_resources(
    mc_dir: Path | str, instance_id: str, *, isolated: bool = True
) -> ResourceAudit:
    """汇总加载目录内的资源，并标出放在别处、本次不会加载的条目。"""
    mc_dir = Path(mc_dir)
    load_dir = instance_game_dir(mc_dir, instance_id, isolated)
    audit = ResourceAudit(load_dir=load_dir)
    for kind in KINDS:
        folder = load_dir / kind
        entries = _scan(folder, kind, misplaced=False)
        present = {item.path.name.lower() for item in entries}
        for alt in _alternate_dirs(mc_dir, instance_id, isolated, kind):
            for item in _scan(alt, kind, misplaced=True):
                if item.path.name.lower() in present:
                    continue
                audit.entries.append(item)
        audit.entries.extend(entries)

    packs_dir = load_dir / "resourcepacks"
    for name in _enabled_packs(load_dir):
        if not (packs_dir / name).exists():
            audit.missing.append(name)
    return audit
