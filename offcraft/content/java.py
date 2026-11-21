"""Mojang Java 运行时：列出可用组件并下载到 ``<mc_dir>/runtime/<component>``。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from offcraft.mirror import Mirror, UnknownUpstreamError
from offcraft.net.downloader import DownloadTask, Downloader, ProgressTracker

__all__ = [
    "JAVA_MANIFEST_URL",
    "JavaRuntime",
    "JavaRuntimeError",
    "install_java_runtime",
    "list_java_runtimes",
    "pick_runtime_for_major",
    "runtime_java_path",
]

JAVA_MANIFEST_URL = (
    "https://launchermeta.mojang.com/v1/products/java-runtime/"
    "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json"
)
DEFAULT_PLATFORM = "windows-x64"

#: 组件名 → 主版本（清单里的 version 字段对 jre-legacy 是 1.8.0_xx，靠表更可靠）
_COMPONENT_MAJOR = {
    "jre-legacy": 8,
    "java-runtime-alpha": 16,
    "java-runtime-beta": 17,
    "java-runtime-gamma": 17,
    "java-runtime-gamma-snapshot": 17,
    "java-runtime-delta": 21,
    "java-runtime-epsilon": 25,
}


class JavaRuntimeError(RuntimeError):
    """Java 运行时清单或下载异常。"""


@dataclass(slots=True)
class JavaRuntime:
    """一条 Mojang Java 运行时条目。"""

    component: str
    version: str
    released: str
    manifest_url: str
    manifest_sha1: str = ""
    manifest_size: int = 0

    @property
    def major(self) -> int:
        known = _COMPONENT_MAJOR.get(self.component)
        if known:
            return known
        head = self.version.split(".", 1)[0].strip()
        try:
            return int(head)
        except ValueError:
            return 0

    @property
    def label(self) -> str:
        return f"Java {self.major} · {self.component}"

    def install_dir(self, mc_dir: Path | str) -> Path:
        return Path(mc_dir) / "runtime" / self.component

    def java_path(self, mc_dir: Path | str) -> Path:
        return self.install_dir(mc_dir) / "bin" / "java.exe"

    def installed(self, mc_dir: Path | str) -> bool:
        return self.java_path(mc_dir).is_file()


def runtime_java_path(mc_dir: Path | str, component: str) -> Path:
    return Path(mc_dir) / "runtime" / component / "bin" / "java.exe"


def list_java_runtimes(
    mirror: Mirror,
    downloader: Downloader,
    *,
    platform: str = DEFAULT_PLATFORM,
) -> list[JavaRuntime]:
    """读取 Mojang 的 java-runtime 总清单，按主版本升序返回。"""
    data = downloader.fetch_json(_rewrite(mirror, JAVA_MANIFEST_URL))
    if not isinstance(data, dict):
        raise JavaRuntimeError("Java 运行时清单格式错误")
    platform_map = data.get(platform) or data.get(DEFAULT_PLATFORM) or {}
    if not isinstance(platform_map, dict) or not platform_map:
        raise JavaRuntimeError(f"清单中没有 {platform} 平台的 Java 运行时")
    result: list[JavaRuntime] = []
    for component, entries in platform_map.items():
        if not isinstance(entries, list) or not entries:
            continue
        if not (component.startswith("jre-") or component.startswith("java-runtime")):
            continue
        entry = entries[0]
        if not isinstance(entry, dict):
            continue
        manifest = entry.get("manifest") or {}
        version = entry.get("version") or {}
        url = str(manifest.get("url") or "")
        if not url:
            continue
        result.append(
            JavaRuntime(
                component=component,
                version=str(version.get("name") or ""),
                released=str(version.get("released") or ""),
                manifest_url=url,
                manifest_sha1=str(manifest.get("sha1") or ""),
                manifest_size=int(manifest.get("size") or 0),
            )
        )
    if not result:
        raise JavaRuntimeError("Java 运行时清单为空")
    result.sort(key=lambda item: (item.major, item.component))
    return result


def pick_runtime_for_major(
    runtimes: list[JavaRuntime], required: int | None
) -> JavaRuntime | None:
    """为所需主版本挑一个最适合安装的运行时。

    规则（与启动器的 Java 选择保持一致）：
    1. 有精确同主版本的组件就用它（例如需要 17 → ``java-runtime-gamma``）；
    2. 否则退到**更高的最小**主版本（需要 17 但没有 17 → 用 21，不要用 8）；
    3. ``required`` 为空时优先 Java 8（原版老版本），再否则取最低可用版本。

    返回 ``None`` 表示清单里没有任何可用组件。
    """
    if not runtimes:
        return None
    if required is None:
        for runtime in runtimes:
            if runtime.major == 8:
                return runtime
        return sorted(runtimes, key=lambda item: item.major)[0]
    exact = [item for item in runtimes if item.major == required]
    if exact:
        return sorted(exact, key=lambda item: item.component)[0]
    higher = [item for item in runtimes if item.major > required]
    if higher:
        return sorted(higher, key=lambda item: (item.major, item.component))[0]
    # 清单里只有更低的版本：装它也比什么都没有强（启动时会给出明确版本错误）
    return sorted(runtimes, key=lambda item: item.major)[-1]


def install_java_runtime(
    mc_dir: Path | str,
    runtime: JavaRuntime,
    *,
    mirror: Mirror,
    downloader: Downloader,
    cancel=None,
    phase: Callable[[str], None] | None = None,
) -> Path:
    """下载并展开一个 Java 运行时，返回 ``bin/java.exe`` 路径。"""
    if phase:
        phase(f"获取 {runtime.component} 文件清单")
    manifest = downloader.fetch_json(_rewrite(mirror, runtime.manifest_url))
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or not files:
        raise JavaRuntimeError(f"{runtime.component} 清单为空")

    base = runtime.install_dir(mc_dir)
    tasks: list[DownloadTask] = []
    for rel, info in files.items():
        if not isinstance(info, dict):
            continue
        rel_path = _safe_rel(rel)
        if rel_path is None:
            continue
        dest = base / rel_path
        kind = info.get("type")
        if kind == "directory":
            dest.mkdir(parents=True, exist_ok=True)
            continue
        if kind != "file":
            continue
        raw = ((info.get("downloads") or {}).get("raw")) or {}
        url = str(raw.get("url") or "")
        if not url:
            continue
        tasks.append(
            DownloadTask(
                url=_rewrite(mirror, url),
                dest=dest,
                sha1=str(raw.get("sha1") or "") or None,
                size=int(raw.get("size") or 0) or None,
                label=rel_path.name,
            )
        )
    if not tasks:
        raise JavaRuntimeError("清单里没有可下载的文件")

    if phase:
        phase(f"下载 {runtime.component}（{len(tasks)} 个文件）")
    report = downloader.run(tasks, tracker=ProgressTracker(), cancel=cancel)
    if not report.ok:
        first = report.failed[0] if report.failed else (None, "未知错误")
        raise JavaRuntimeError(f"{len(report.failed)} 个文件失败：{first[1]}")

    java = runtime.java_path(mc_dir)
    if not java.is_file():
        raise JavaRuntimeError("下载完成但没有找到 bin/java.exe")
    return java


def _safe_rel(rel: str) -> Path | None:
    """清单内的相对路径，拒绝绝对路径与 ``..`` 越界。"""
    text = str(rel or "").replace("\\", "/").strip()
    if not text:
        return None
    posix = PurePosixPath(text)
    if posix.is_absolute() or any(part in ("..", "") for part in posix.parts):
        return None
    if posix.parts and ":" in posix.parts[0]:
        return None
    return Path(*posix.parts)


def _rewrite(mirror: Mirror, url: str) -> str:
    try:
        return mirror.rewrite(url)
    except UnknownUpstreamError:
        return url
