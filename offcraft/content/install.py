"""内容文件安装：模组 / 资源包 / 光影，含必需依赖解析。"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from offcraft.content.modrinth import (
    ContentError,
    Dependency,
    ModrinthClient,
    ProjectVersion,
)
from offcraft.net.downloader import Downloader, DownloadTask

__all__ = [
    "CONTENT_DIRS",
    "ContentInstallError",
    "ContentInstallResult",
    "download_file",
    "install_project_version",
    "safe_filename",
]

#: 工程类型 → 游戏目录下的子目录
CONTENT_DIRS = {
    "mod": "mods",
    "resourcepack": "resourcepacks",
    "shader": "shaderpacks",
}

_BAD_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class ContentInstallError(RuntimeError):
    """内容安装失败。"""


@dataclass(slots=True)
class ContentInstallResult:
    files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.files)


def safe_filename(name: str, fallback: str = "download.bin") -> str:
    cleaned = _BAD_FILENAME.sub("_", (name or "").strip()).strip(". ")
    return cleaned or fallback


def _sha512_file(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(
    downloader: Downloader,
    url: str,
    dest: Path,
    *,
    sha1: str = "",
    sha512: str = "",
    size: int | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    """下载单个文件并校验；``sha1`` 交给下载器，``sha512`` 在落盘后复核。"""
    dest = Path(dest)
    task = DownloadTask(
        url=url,
        dest=dest,
        sha1=sha1 or None,
        size=size or None,
        label=dest.name,
    )
    try:
        downloader.download(task, cancel=cancel)
    except Exception as exc:  # noqa: BLE001 - 统一转为安装错误
        raise ContentInstallError(f"下载失败：{dest.name}（{exc}）") from exc
    if sha512 and _sha512_file(dest).lower() != sha512.lower():
        dest.unlink(missing_ok=True)
        raise ContentInstallError(f"SHA-512 校验失败：{dest.name}")
    return dest


def installed_names(dest_dir: Path | str) -> set[str]:
    """目标目录里已装文件的小写名集合（用于判断某个项目是否已存在）。"""
    try:
        return {item.name.lower() for item in Path(dest_dir).iterdir() if item.is_file()}
    except OSError:
        return set()


def missing_dependencies(
    client: ModrinthClient,
    version: ProjectVersion,
    dest_dir: Path | str,
) -> list[Dependency]:
    """找出 ``version`` 的**必需依赖**里，目标目录中还没有的那些。

    判断方式：依赖给了 ``file_name`` 就按文件名比对；否则用 ``project_id``
    去问 Modrinth 拿默认项目名 slug，再按 ``<slug>-*.jar`` 前缀匹配。
    这样「已装前置」不会重复下载，「缺前置」能准确报出来。
    """
    present = installed_names(dest_dir)
    if not present:
        return [dep for dep in version.dependencies if dep.required]
    missing: list[Dependency] = []
    for dep in version.dependencies:
        if not dep.required:
            continue
        name = (dep.file_name or "").lower()
        if name:
            if name not in present and f"{name}.disabled" not in present:
                missing.append(dep)
            continue
        # 没有文件名：按项目 slug 前缀猜（Modrinth 的文件名通常以 slug 开头）
        slug = ""
        if dep.project_id:
            try:
                meta = client.project(dep.project_id) or {}
                slug = str(meta.get("slug") or "").lower()
            except (ContentError, AttributeError, TypeError):
                slug = ""
        if slug and any(
            item.startswith(slug) or item.startswith(f"{slug}-") for item in present
        ):
            continue
        missing.append(dep)
    return missing


def install_project_version(
    client: ModrinthClient,
    version: ProjectVersion,
    dest_dir: Path | str,
    *,
    game_version: str = "",
    loader: str = "",
    cancel: threading.Event | None = None,
    include_dependencies: bool = True,
    max_files: int = 24,
) -> ContentInstallResult:
    """安装一个 Modrinth 版本（主文件 + 必需依赖），返回已安装文件。"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    result = ContentInstallResult()
    queue: list[ProjectVersion] = [version]
    seen_versions: set[str] = set()
    seen_projects: set[str] = set()
    pending_projects: list[str] = []
    installed = 0

    while queue:
        current = queue.pop(0)
        if not current.id or current.id in seen_versions:
            continue
        seen_versions.add(current.id)
        file = current.primary_file
        if file is None:
            result.warnings.append(f"{current.label} 没有可下载文件")
            continue
        if installed >= max_files:
            result.warnings.append(f"已达到单次安装上限（{max_files} 个文件）")
            break
        installed += 1
        result.files.append(
            download_file(
                client.downloader,
                client.resolve_url(file.url),
                dest_dir / safe_filename(file.filename),
                sha1=file.sha1,
                sha512=file.sha512,
                size=file.size or None,
                cancel=cancel,
            )
        )
        if not include_dependencies:
            continue
        for dep in current.dependencies:
            if not dep.required:
                continue
            if dep.version_id:
                try:
                    queue.append(client.version(dep.version_id))
                except ContentError as exc:
                    result.warnings.append(f"依赖 {dep.version_id} 获取失败：{exc}")
            elif dep.project_id and dep.project_id not in seen_projects:
                seen_projects.add(dep.project_id)
                pending_projects.append(dep.project_id)

    for project_id in pending_projects:
        if installed >= max_files:
            break
        try:
            best = client.best_version(project_id, game_version=game_version, loader=loader)
        except ContentError as exc:
            result.warnings.append(f"依赖项目 {project_id} 获取失败：{exc}")
            continue
        if best is None:
            result.warnings.append(f"依赖项目 {project_id} 没有兼容版本")
            continue
        if best.id in seen_versions:
            continue
        seen_versions.add(best.id)
        file = best.primary_file
        if file is None:
            continue
        installed += 1
        result.files.append(
            download_file(
                client.downloader,
                client.resolve_url(file.url),
                dest_dir / safe_filename(file.filename),
                sha1=file.sha1,
                sha512=file.sha512,
                size=file.size or None,
                cancel=cancel,
            )
        )
    return result
