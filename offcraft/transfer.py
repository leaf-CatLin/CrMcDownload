"""版本与存档的 ZIP 导出 / 导入。

导出时保留顶层目录名（``版本ID/`` 或 ``存档名/``），导入时再据此还原；
导入会做路径穿越检查，避免恶意 ZIP 写到游戏目录之外。
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path, PurePosixPath

from offcraft.instances import safe_instance_id

__all__ = [
    "TransferError",
    "export_save",
    "export_version",
    "import_save",
    "import_version",
]


class TransferError(RuntimeError):
    """导入 / 导出失败时抛出，消息可直接展示给用户。"""


def _add_tree(zf: zipfile.ZipFile, base: Path, prefix: str) -> None:
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(base)
        name = PurePosixPath(prefix).joinpath(*relative.parts)
        zf.write(path, arcname=name.as_posix())


def export_version(version_dir: Path | str, archive: Path | str) -> Path:
    """把 ``versions/<id>`` 整个打包。"""
    version_dir = Path(version_dir)
    archive = Path(archive)
    if not version_dir.is_dir():
        raise TransferError(f"版本目录不存在：{version_dir}")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        _add_tree(zf, version_dir, version_dir.name)
    return archive


def export_save(save_dir: Path | str, archive: Path | str) -> Path:
    """把 ``saves/<name>`` 整个打包。"""
    save_dir = Path(save_dir)
    archive = Path(archive)
    if not save_dir.is_dir():
        raise TransferError(f"存档目录不存在：{save_dir}")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        _add_tree(zf, save_dir, save_dir.name)
    return archive


def _file_names(zf: zipfile.ZipFile) -> list[str]:
    return [info.filename for info in zf.infolist() if not info.is_dir()]


def _version_layout(names: list[str]) -> tuple[str, bool]:
    """返回 ``(版本ID, 是否自带顶层目录)``。"""
    for name in names:
        parts = PurePosixPath(name).parts
        if len(parts) == 2 and parts[1] == f"{parts[0]}.json":
            return parts[0], True
    for name in names:
        parts = PurePosixPath(name).parts
        if len(parts) == 1 and name.lower().endswith(".json"):
            return name[:-5], False
    return "", False


def _relative_parts(name: str, prefix: str | None) -> tuple[str, ...]:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise TransferError(f"压缩包内存在非法路径：{name}")
    parts = path.parts
    if prefix is None:
        return parts
    if parts and parts[0] == prefix:
        return parts[1:]
    return ()


def _extract(zf: zipfile.ZipFile, dest: Path, prefix: str | None) -> None:
    for info in zf.infolist():
        if info.is_dir():
            continue
        parts = _relative_parts(info.filename, prefix)
        if not parts:
            continue
        out = dest.joinpath(*parts)
        out.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)


def import_version(mc_dir: Path | str, archive: Path | str) -> str:
    """导入一个版本 ZIP 到 ``<mc>/versions/``，返回版本 ID。"""
    mc_dir = Path(mc_dir)
    archive = Path(archive)
    versions = mc_dir / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    try:
        zf = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise TransferError(f"不是有效的 ZIP：{exc}") from exc

    with zf:
        names = _file_names(zf)
        raw_id, nested = _version_layout(names)
        if not raw_id:
            raise TransferError("压缩包里找不到 <版本ID>/<版本ID>.json")
        version_id = safe_instance_id(raw_id)
        if not version_id:
            raise TransferError(f"版本 ID 不可用：{raw_id}")
        target = versions / version_id
        if target.exists():
            raise TransferError(f"版本 {version_id} 已存在")
        try:
            _extract(zf, target, raw_id if nested else None)
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
    return version_id


def _save_layout(names: list[str]) -> tuple[str, str | None]:
    """返回 ``(存档名, 需要剥离的顶层目录)``。"""
    tops = {PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts}
    if len(tops) == 1:
        top = tops.pop()
        return top, top
    return "", None


def _unique_dir(parent: Path, name: str) -> Path:
    target = parent / name
    index = 2
    while target.exists():
        target = parent / f"{name} ({index})"
        index += 1
    return target


def import_save(mc_dir: Path | str, archive: Path | str) -> str:
    """导入一个存档 ZIP 到 ``<mc>/saves/``，返回存档目录名（重名会自动加序号）。"""
    mc_dir = Path(mc_dir)
    archive = Path(archive)
    saves = mc_dir / "saves"
    saves.mkdir(parents=True, exist_ok=True)
    try:
        zf = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise TransferError(f"不是有效的 ZIP：{exc}") from exc

    with zf:
        names = _file_names(zf)
        if not names:
            raise TransferError("压缩包是空的")
        top, prefix = _save_layout(names)
        name = top or archive.stem or "存档"
        target = _unique_dir(saves, name)
        try:
            _extract(zf, target, prefix)
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
    return target.name
