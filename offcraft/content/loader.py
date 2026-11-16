"""加载器（Fabric / Forge / NeoForge）：列出可选版本并安装到 ``versions/<id>``。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from offcraft.mirror import Mirror, UnknownUpstreamError
from offcraft.net.downloader import DownloadTask, Downloader, ProgressTracker

__all__ = [
    "LOADERS",
    "LOADER_CONFLICTS",
    "LOADER_ROWS",
    "LoaderBuild",
    "LoaderError",
    "install_loader",
    "installer_version_id",
    "list_loader_versions",
    "loader_dependency_tasks",
    "loader_note",
    "loader_support",
    "loader_title",
    "mc_sort_key",
    "supports_mods",
]

#: 安装版本时可选的加载器
LOADERS: list[tuple[str, str]] = [
    ("vanilla", "原版"),
    ("fabric", "Fabric"),
    ("forge", "Forge"),
    ("neoforge", "NeoForge"),
]

#: 安装页里的加载器行（顺序即展示顺序，第一个高的行是原版）
LOADER_ROWS: list[tuple[str, str, str]] = [
    ("fabric", "Fabric", "轻量、随版本更新最快；绝大多数客户端模组优先支持 Fabric。"),
    ("neoforge", "NeoForge", "Forge 的社区分支，1.20.2 及以后的新版本首选，API 与 Forge 同源。"),
    ("forge", "Forge", "老牌加载器，1.12.2 等旧版本与旧整合包生态最完整。"),
]

#: 互斥的加载器：同一实例 / 同一 MC 版本只能装其中一个
LOADER_CONFLICTS: dict[str, tuple[str, ...]] = {
    "forge": ("neoforge",),
    "neoforge": ("forge",),
}

#: 能装模组 / 光影的加载器
MOD_LOADERS = {"Fabric", "Forge", "NeoForge", "Quilt"}

FABRIC_LIST = "https://meta.fabricmc.net/v2/versions/loader/{mc}"
FABRIC_PROFILE = "https://meta.fabricmc.net/v2/versions/loader/{mc}/{loader}/profile/json"
FORGE_METADATA = "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml"
FORGE_INSTALLER = (
    "https://maven.minecraftforge.net/net/minecraftforge/forge/{v}/forge-{v}-installer.jar"
)
NEOFORGE_METADATA = "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
NEOFORGE_INSTALLER = (
    "https://maven.neoforged.net/releases/net/neoforged/neoforge/{v}/neoforge-{v}-installer.jar"
)

_INSTALLER_TIMEOUT = 900.0

#: 加载器安装器 jar 内的描述文件
PROFILE_ENTRY = "install_profile.json"
VERSION_ENTRY = "version.json"


class LoaderError(RuntimeError):
    """加载器列表或安装失败。"""


def supports_mods(loader: str) -> bool:
    """该实例是否支持模组 / 光影（原版实例不支持）。"""
    return str(loader or "").strip() in MOD_LOADERS


def loader_title(loader: str) -> str:
    """``fabric`` → ``Fabric``；未知 key 原样返回。"""
    key = str(loader or "").strip().lower()
    return dict(LOADERS).get(key, str(loader or ""))


def mc_sort_key(mc_version: str) -> tuple[int, ...]:
    """把 ``1.20.2`` / ``26.3`` 这类版本号变成可比较的元组。"""
    return _version_key(mc_version)


def loader_support(
    loader: str, mc_version: str, *, version_type: str = "release"
) -> tuple[bool, str]:
    """静态兼容性判断：该加载器能不能装到这个 MC 版本上。

    只靠版本号规则（不联网），返回 ``(是否兼容, 不兼容原因)``。
    """
    key = str(loader or "").strip().lower()
    if key in ("", "vanilla"):
        return True, ""
    if version_type and version_type != "release":
        return False, "加载器只提供正式版，快照 / 远古版请装原版"
    version = mc_sort_key(mc_version)
    if key == "fabric":
        if version and version < (1, 14):
            return False, "Fabric 只支持 1.14 及以上版本"
        return True, ""
    if key == "neoforge":
        if version and version < (1, 20, 2):
            return False, "NeoForge 只支持 1.20.2 及以上版本"
        return True, ""
    if key == "forge":
        return True, ""
    return False, f"未知加载器：{loader}"


def loader_note(loader: str, mc_version: str) -> str:
    """展示用的中性提示（不阻止安装）。"""
    key = str(loader or "").strip().lower()
    version = mc_sort_key(mc_version)
    if key == "forge" and version and version >= (1, 20, 4):
        return "1.20.4 之后官方建议改用 NeoForge"
    if key == "fabric":
        return "需要 Fabric API 才能跑大多数模组"
    if key == "neoforge":
        return "与 Forge 不能同时安装"
    return ""


@dataclass(slots=True)
class LoaderBuild:
    loader: str
    version: str
    mc_version: str
    stable: bool = True
    note: str = ""

    @property
    def label(self) -> str:
        tag = "" if self.stable else "（测试）"
        return f"{self.loader} {self.version}{tag}"


def _rewrite(mirror: Mirror, url: str) -> str:
    try:
        return mirror.rewrite(url)
    except UnknownUpstreamError:
        return url


def _candidates(mirror: Mirror, url: str) -> list[str]:
    """镜像优先，失败再回退官方源（镜像可能尚未升级出对应路由）。"""
    mirrored = _rewrite(mirror, url)
    return [mirrored, url] if mirrored != url else [url]


def _fetch_json(mirror: Mirror, downloader: Downloader, url: str):
    errors: list[str] = []
    for candidate in _candidates(mirror, url):
        try:
            return downloader.fetch_json(candidate)
        except Exception as exc:  # noqa: BLE001 - 镜像不可用时回退
            errors.append(f"{candidate.split('/')[2]}: {exc}")
    raise LoaderError("获取失败：" + "；".join(errors))


def _parse_lenient(raw: bytes):
    """容错解析：镜像缓存可能返回重复拼接的 JSON，只取第一段。"""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise LoaderError("响应为空")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            value, _end = json.JSONDecoder().raw_decode(text)
            return value
        except json.JSONDecodeError as exc:
            raise LoaderError(f"JSON 解析失败：{exc}") from exc


def _fetch_bytes(mirror: Mirror, downloader: Downloader, url: str) -> bytes:
    errors: list[str] = []
    for candidate in _candidates(mirror, url):
        try:
            return downloader.fetch_bytes(candidate)
        except Exception as exc:  # noqa: BLE001 - 镜像不可用时回退
            errors.append(f"{candidate.split('/')[2]}: {exc}")
    raise LoaderError("获取失败：" + "；".join(errors))


def _fabric_versions(mirror: Mirror, downloader: Downloader, mc_version: str) -> list[LoaderBuild]:
    data = _parse_lenient(_fetch_bytes(mirror, downloader, FABRIC_LIST.format(mc=mc_version)))
    if not isinstance(data, list):
        raise LoaderError("Fabric 版本列表格式错误")
    result: list[LoaderBuild] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        loader = entry.get("loader") or {}
        version = str(loader.get("version") or "")
        if not version:
            continue
        result.append(
            LoaderBuild(
                loader="fabric",
                version=version,
                mc_version=mc_version,
                stable=bool(loader.get("stable", True)),
            )
        )
    return result


def _maven_versions(
    mirror: Mirror, downloader: Downloader, url: str, prefix: str, loader: str, mc_version: str
) -> list[LoaderBuild]:
    raw = _fetch_bytes(mirror, downloader, url)
    try:
        root = ET.fromstring(raw.decode("utf-8", errors="replace"))
    except ET.ParseError as exc:
        raise LoaderError(f"读取 {loader} 版本列表失败：{exc}") from exc
    versions = [node.text.strip() for node in root.iter("version") if node.text]
    result = [
        LoaderBuild(loader=loader, version=value, mc_version=mc_version)
        for value in versions
        if value.startswith(prefix)
    ]
    return result


def _neoforge_prefix(mc_version: str) -> str:
    """1.20.4 → ``20.4.``；1.21 → ``21.0.``；1.21.1 → ``21.1.``。"""
    parts = [part for part in str(mc_version).split(".") if part]
    if not parts:
        return ""
    if parts[0] == "1":
        rest = parts[1:]
    else:
        rest = parts
    if not rest:
        return ""
    if len(rest) == 1:
        return f"{rest[0]}.0."
    return f"{rest[0]}.{rest[1]}."


def list_loader_versions(
    loader: str,
    mc_version: str,
    *,
    mirror: Mirror,
    downloader: Downloader,
    limit: int = 40,
) -> list[LoaderBuild]:
    """列出某个加载器对指定 MC 版本的可用版本（新→旧）。"""
    key = str(loader or "").lower()
    if key in ("", "vanilla"):
        return []
    if key == "fabric":
        builds = _fabric_versions(mirror, downloader, mc_version)
    elif key == "forge":
        builds = _maven_versions(
            mirror, downloader, FORGE_METADATA, f"{mc_version}-", "forge", mc_version
        )
        builds.sort(key=lambda item: _version_key(item.version.split("-", 1)[-1]), reverse=True)
    elif key == "neoforge":
        prefix = _neoforge_prefix(mc_version)
        if not prefix:
            return []
        builds = _maven_versions(
            mirror, downloader, NEOFORGE_METADATA, prefix, "neoforge", mc_version
        )
        builds.sort(key=lambda item: _version_key(item.version), reverse=True)
    else:
        raise LoaderError(f"不支持的加载器：{loader}")
    return builds[: max(1, limit)]


def _version_key(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(text).replace("-", ".").replace("_", ".").split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts[:6])


def _pick_java(mc_dir: Path, override: str | Path | None = None) -> Path:
    if override:
        return Path(override)
    from offcraft.launch.java import list_java  # 延迟导入：本模块在 downloader 分支也能单独测试

    candidates = list_java(mc_dir)
    if not candidates:
        raise LoaderError("未检测到 Java，无法运行加载器安装器（请先在设置里添加 Java）")
    best = max(candidates, key=lambda item: item.major)
    return Path(best.path)


def _download(
    mirror: Mirror,
    downloader: Downloader,
    url: str,
    dest: Path,
    *,
    cancel=None,
    label: str = "",
    sha1: str | None = None,
) -> bool:
    """镜像优先下载单个文件，失败回退官方源。

    提供 ``sha1`` 时，已存在的缓存文件也先校验指纹（避免复用上一次的坏文件），
    指纹不符则重新下载；DownloadTask 下载过程中同样会做 sha1 校验。
    """
    if dest.is_file() and dest.stat().st_size > 0:
        if not sha1 or _file_sha1(dest) == _norm_sha1(sha1):
            return True
    for candidate in _candidates(mirror, url):
        report = downloader.run(
            [DownloadTask(url=candidate, dest=dest, label=label, sha1=sha1)],
            tracker=ProgressTracker(),
            cancel=cancel,
        )
        if report.ok and dest.is_file() and dest.stat().st_size > 0:
            return True
    return False


def _norm_sha1(sha1: str | None) -> str | None:
    return sha1.strip().lower() if sha1 else None


def _file_sha1(path: Path) -> str | None:
    """计算文件 SHA-1；读取失败返回 None（调用方据此视为「无法校验」）。"""
    try:
        digest = hashlib.sha1()
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _version_dirs(mc_dir: Path) -> set[str]:
    folder = mc_dir / "versions"
    if not folder.is_dir():
        return set()
    return {item.name for item in folder.iterdir() if item.is_dir()}


def _run_installer(
    mc_dir: Path,
    jar: Path,
    java: Path,
    *,
    cancel: threading.Event | None = None,
    phase: Callable[[str], None] | None = None,
    offline: bool = True,
) -> list[str]:
    """运行加载器安装器，返回最后几行输出。

    默认带 ``--offline``：依赖已由 :func:`_download_installer_dependencies`
    经镜像备齐，这样安装器不会去官方 maven 拉文件（本机会超时卡死）。
    输出必须边跑边读：安装器输出超过管道缓冲区后会卡在 write 上，
    旧实现等进程结束再 ``read()``，大输出时会直接死锁。
    """
    if phase:
        phase("运行加载器安装器（可能需要几分钟）")
    command = [str(java), "-jar", str(jar), "--installClient", str(mc_dir)]
    if offline:
        command.append("--offline")
    flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    proc = subprocess.Popen(
        command,
        cwd=str(mc_dir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    tail: deque[str] = deque(maxlen=60)

    def _pump(stream) -> None:
        try:
            for raw in iter(stream.readline, b""):
                text = raw.decode("utf-8", errors="replace").strip()
                if text:
                    tail.append(text)
        except (OSError, ValueError):  # 进程被终止时管道会关
            pass

    reader = threading.Thread(target=_pump, args=(proc.stdout,), daemon=True)
    reader.start()
    deadline = time.monotonic() + _INSTALLER_TIMEOUT
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                raise LoaderError("已取消安装")
            if time.monotonic() > deadline:
                proc.terminate()
                raise LoaderError("安装器运行超时")
            time.sleep(0.25)
    finally:
        if proc.poll() is None:
            proc.kill()
        reader.join(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()
    output = list(tail)
    if proc.returncode != 0:
        detail = " / ".join(output[-8:]) if output else f"退出码 {proc.returncode}"
        raise LoaderError(f"安装器执行失败：{detail}")
    return output


def _zip_json(jar: Path, name: str) -> dict:
    """读取安装器 jar 里的 JSON（不存在或损坏时返回空 dict）。"""
    try:
        with zipfile.ZipFile(jar) as archive:
            return json.loads(archive.read(name).decode("utf-8", errors="replace"))
    except (KeyError, OSError, ValueError, zipfile.BadZipFile):
        return {}


def _verify_installer_jar(jar: Path, *, loader: str) -> bool:
    """校验安装器 jar 是一个可读的 ZIP 且内含预期的描述文件。

    Forge / NeoForge 的安装器在 Maven 上不暴露 SHA-1，无法做外部指纹比对；
    这里退而求其次做自洽校验：能正常 list 且能读出版本描述，即可基本排除
    网络损坏/截断导致的「半个 jar」。校验失败时调用方应删除并重下。
    """
    try:
        with zipfile.ZipFile(jar) as archive:
            if archive.testzip() is not None:
                return False
            names = set(archive.namelist())
            # 后续 ``installer_version_id`` 要从 jar 里读 version.json，
            # 缺了它说明文件错配/截断，直接判失败。
            if VERSION_ENTRY not in names:
                return False
    except (OSError, zipfile.BadZipFile):
        return False
    return True


def installer_version_id(jar: Path | str) -> str:
    """安装器将要生成的版本 id（如 ``26.2-forge-65.1.5``）。"""
    jar = Path(jar)
    bundled = _zip_json(jar, VERSION_ENTRY)
    profile = _zip_json(jar, PROFILE_ENTRY)
    return str(bundled.get("id") or profile.get("version") or "")


def loader_dependency_tasks(
    mc_dir: Path | str,
    jar: Path | str,
    mirror: Mirror,
    *,
    version_id: str = "",
    rewrite: bool = True,
) -> tuple[list[DownloadTask], list[str]]:
    """安装器需要的全部依赖 → 下载任务。

    Forge / NeoForge 安装器自己下载依赖时用的是官方 maven，在本机经常
    超时挂住（症状：日志停在某个库上、退出码却仍是 0）；这里把
    *安装器自身 install_profile 的库* 与 *版本 JSON 的库* 一并经镜像
    先备齐，安装器就能全程「File exists: Checksum validated」本地完成。
    """
    from offcraft.install.plan import InstallOptions, library_tasks

    mc_dir = Path(mc_dir)
    jar = Path(jar)
    libraries: list[dict] = []
    profile = _zip_json(jar, PROFILE_ENTRY)
    bundled = _zip_json(jar, VERSION_ENTRY)
    for source in (profile.get("libraries"), bundled.get("libraries")):
        libraries.extend(item for item in (source or []) if isinstance(item, dict))
    target = str(bundled.get("id") or profile.get("version") or version_id or "")
    local = mc_dir / "versions" / target / f"{target}.json" if target else None
    if local is not None and local.is_file():
        try:
            created = json.loads(local.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            created = {}
        libraries.extend(
            item for item in (created.get("libraries") or []) if isinstance(item, dict)
        )
    return library_tasks(
        libraries,
        mc_dir,
        mirror,
        options=InstallOptions(include_assets=False),
        rewrite=rewrite,
    )


def _run_tasks(
    downloader: Downloader,
    tasks: list[DownloadTask],
    *,
    cancel: threading.Event | None = None,
) -> list[tuple[Path, str]]:
    """执行一批下载任务，返回失败的 ``(目标文件, 原因)`` 列表。"""
    report = downloader.run(tasks, tracker=ProgressTracker(), cancel=cancel)
    return [(Path(task.dest), message) for task, message in report.failed]


def _missing_files(mc_dir: Path, version_id: str) -> list[Path]:
    """启动前缺的依赖文件（与启动器同一套 classpath 计算）。"""
    from offcraft.launch.libraries import build_classpath
    from offcraft.meta.version import VersionError, load_version

    try:
        version = load_version(mc_dir, version_id)
    except VersionError as exc:
        raise LoaderError(f"读取 {version_id} 失败：{exc}") from exc
    return list(build_classpath(version, mc_dir).missing)


def _download_installer_dependencies(
    mc_dir: Path,
    jar: Path,
    mirror: Mirror,
    downloader: Downloader,
    *,
    phase: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
) -> None:
    """经镜像先备齐安装器所需依赖（镜像失败再回退官方源）。"""
    tasks, _warnings = loader_dependency_tasks(mc_dir, jar, mirror)
    if not tasks:
        return
    if phase:
        phase(f"准备依赖（{len(tasks)} 个文件）")
    failed = _run_tasks(downloader, tasks, cancel=cancel)
    if not failed:
        return
    direct, _ = loader_dependency_tasks(mc_dir, jar, mirror, rewrite=False)
    alternatives = {Path(task.dest): task for task in direct}
    retry = [
        alternatives[dest] for dest, _message in failed if dest in alternatives
    ]
    if retry:
        if phase:
            phase(f"换官方源重试（{len(retry)} 个文件）")
        failed = _run_tasks(downloader, retry, cancel=cancel)
    if failed:
        first = failed[0][1]
        raise LoaderError(f"依赖下载失败（{len(failed)} 个）：{first}")


def install_loader(
    mc_dir: Path | str,
    mc_version: str,
    loader: str,
    version: str,
    *,
    mirror: Mirror,
    downloader: Downloader,
    manifest=None,
    java: str | Path | None = None,
    cancel: threading.Event | None = None,
    phase: Callable[[str], None] | None = None,
) -> str:
    """安装加载器，返回可启动的版本 id。"""
    mc_dir = Path(mc_dir)
    key = str(loader or "").lower()
    if key == "vanilla":
        return mc_version

    if key == "fabric":
        if phase:
            phase("获取 Fabric profile")
        profile = _parse_lenient(
            _fetch_bytes(
                mirror, downloader, FABRIC_PROFILE.format(mc=mc_version, loader=version)
            )
        )
        if not isinstance(profile, dict) or not profile.get("mainClass"):
            raise LoaderError("Fabric profile 格式错误")
        version_id = str(profile.get("id") or f"fabric-loader-{version}-{mc_version}")
        target = mc_dir / "versions" / version_id
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{version_id}.json").write_text(
            json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if manifest is not None:
            from offcraft.install.installer import install_local

            if phase:
                phase("下载 Fabric 依赖库")
            result = install_local(
                mc_dir, version_id, mirror, downloader, cancel=cancel
            )
            if not result.ok:
                first = result.report.failed[0] if result.report.failed else (None, "未知错误")
                raise LoaderError(f"Fabric 依赖下载失败：{first[1]}")
        return version_id

    if key in ("forge", "neoforge"):
        if key == "forge":
            url = FORGE_INSTALLER.format(v=version)
            name = f"forge-{version}-installer.jar"
        else:
            url = NEOFORGE_INSTALLER.format(v=version)
            name = f"neoforge-{version}-installer.jar"
        cache_dir = mc_dir / "cache" / "installers"
        cache_dir.mkdir(parents=True, exist_ok=True)
        jar = cache_dir / name
        if phase:
            phase(f"下载 {loader} 安装器")
        if not _download(
            mirror, downloader, url, jar, cancel=cancel, label=name
        ):
            raise LoaderError(f"{loader} 安装器下载失败")
        if not _verify_installer_jar(jar, loader=key):
            # 安装器损坏/截断：清掉旧文件，让下次重新下载
            try:
                jar.unlink(missing_ok=True)
            except OSError:
                pass
            raise LoaderError(f"{loader} 安装器文件损坏，请重试")
        java_path = _pick_java(mc_dir, java)
        # 关键：先把安装器自己（install_profile）与版本 JSON 里的依赖全部经镜像备齐。
        # 否则安装器会自己去官方 maven 拉，本机经常超时卡住、退出码却是 0，
        # 结果只写了个版本 JSON，依赖少一堆（就是 26.2-forge-65.1.5 启动失败的原因）。
        _download_installer_dependencies(
            mc_dir, jar, mirror, downloader, phase=phase, cancel=cancel
        )
        expected = installer_version_id(jar)
        before = _version_dirs(mc_dir)
        try:
            _run_installer(mc_dir, jar, java_path, cancel=cancel, phase=phase)
        except LoaderError:
            if cancel is not None and cancel.is_set():
                raise
            if phase:
                phase(f"{loader} 离线安装未成功，改用联网重试")
            _run_installer(
                mc_dir, jar, java_path, cancel=cancel, phase=phase, offline=False
            )
        version_id = ""
        if expected and (mc_dir / "versions" / expected).is_dir():
            version_id = expected
        else:
            created = sorted(_version_dirs(mc_dir) - before)
            if created:
                created.sort(
                    key=lambda item: (item == mc_version, _version_key(item)), reverse=True
                )
                version_id = created[0]
        if not version_id:
            raise LoaderError(f"{loader} 安装器没有生成版本目录")

        # 安装器负责写 JSON 与打补丁（补丁后的 client jar 由它的 processor 生成，
        # 上游 maven 上没有）；剩下的依赖按版本 JSON 再校一遍，缺什么补什么。
        from offcraft.install.installer import InstallError, install_local

        if phase:
            phase(f"校验 {loader} 依赖")
        try:
            result = install_local(
                mc_dir, version_id, mirror, downloader, cancel=cancel
            )
        except InstallError as exc:
            raise LoaderError(f"{loader} 依赖补全失败：{exc}") from exc
        if not result.ok:
            first = result.report.failed[0] if result.report.failed else (None, "未知错误")
            raise LoaderError(f"{loader} 依赖下载失败：{first[1]}")
        missing = _missing_files(mc_dir, version_id)
        if missing:
            names = "、".join(path.name for path in missing[:3])
            raise LoaderError(
                f"{loader} 安装不完整，还缺 {len(missing)} 个依赖（{names}…），请重试"
            )
        return version_id

    raise LoaderError(f"不支持的加载器：{loader}")
