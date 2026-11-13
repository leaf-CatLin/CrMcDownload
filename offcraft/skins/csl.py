"""CustomSkinLoader（CSL）mod 的自动安装与配置。

只在带加载器（Fabric/Forge/NeoForge/Quilt）的实例上生效：原版实例皮肤入口置灰。
所有失败只记 WARN、返回状态，**绝不抛异常阻断启动**（由调用方在后台线程里 best-effort 执行）。

优先用 Modrinth 内容层解析出文件，落到 ``mods/CustomSkinLoader*.jar``；配置必须写
``<游戏目录>/CustomSkinLoader/CustomSkinLoader.json``（CSL 只认这个位置），
用 ``loadlist`` 把皮肤源指向镜像的 ``/skin/``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from offcraft.content.modrinth import ContentError, ModrinthClient
from offcraft.instances import Instance, instance_game_dir
from offcraft.mirror import Mirror
from offcraft.net.downloader import DownloadError, DownloadTask, Downloader

__all__ = [
    "CSL_CONFIG_DIR",
    "CSL_CONFIG_NAME",
    "CSL_JAR_PREFIX",
    "CslStatus",
    "build_config",
    "config_path_for",
    "ensure_installed",
    "mods_dir_for",
]

#: Modrinth 上 CustomSkinLoader 的 slug / project_id（人工确认的稳定值）
CSL_SLUG = "customskinloader"
CSL_JAR_PREFIX = "CustomSkinLoader"
CSL_CONFIG_DIR = "CustomSkinLoader"
CSL_CONFIG_NAME = "CustomSkinLoader.json"

#: 实例加载器 -> Modrinth loader facet
LOADERS = {"Fabric": "fabric", "Forge": "forge", "NeoForge": "neoforge", "Quilt": "quilt"}


@dataclass(slots=True)
class CslStatus:
    """安装结果，供界面提示与日志。"""

    state: str                 # installed | present | missing_loader | failed | no_base
    jar: str = ""
    config_written: bool = False
    note: str = ""

    @property
    def ready(self) -> bool:
        return self.state in ("installed", "present") and self.config_written


def mods_dir_for(
    mc_dir: Path | str, instance: Instance, *, isolated: bool = True
) -> Path:
    return instance_game_dir(mc_dir, instance.id, isolated) / "mods"


def config_path_for(
    mc_dir: Path | str, instance: Instance, *, isolated: bool = True
) -> Path:
    """CSL 只认 ``<游戏目录>/CustomSkinLoader/CustomSkinLoader.json``。"""
    return instance_game_dir(mc_dir, instance.id, isolated) / CSL_CONFIG_DIR / CSL_CONFIG_NAME


def build_config(base_url: str) -> dict[str, Any]:
    """CSL 14.7+ 的 ``loadlist`` 格式；类型用 CustomSkinAPI 指向镜像的 ``/skin/``。"""
    base = (base_url or "").rstrip("/") + "/"
    return {
        "enable": True,
        "loadlist": [
            {
                "name": "CrMCDownload",
                "type": "CustomSkinAPI",
                "root": base,
            },
            {"name": "Mojang", "type": "MojangAPI"},
        ],
    }


def _write_config(path: Path, base_url: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(build_config(base_url), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError:
        return False


def ensure_installed(
    mc_dir: Path | str,
    instance: Instance,
    *,
    downloader: Downloader,
    mirror: Mirror | Any,
    cancel: Any | None = None,
    log: Callable[[str, str], None] | None = None,
    isolated: bool = True,
) -> CslStatus:
    """确保实例装了 CSL 并写好配置；失败只记 WARN。"""

    def warn(text: str) -> None:
        if log is not None:
            log("WARN", f"CSL：{text}")

    base_url = getattr(mirror, "base_url", "") or ""
    if not base_url:
        return CslStatus("no_base", note="未配置下载源，跳过皮肤 mod")
    loader = LOADERS.get(instance.loader)
    if loader is None:
        return CslStatus("missing_loader", note="原版实例不支持皮肤 mod（需 Fabric/Forge/NeoForge/Quilt）")

    mods = mods_dir_for(mc_dir, instance, isolated=isolated)
    try:
        mods.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        warn(f"无法创建 mods 目录：{exc}")
        return CslStatus("failed", note="无法创建 mods 目录")

    existing = sorted(mods.glob(f"{CSL_JAR_PREFIX}*.jar"))
    # 配置文件必须放在 <游戏目录>/CustomSkinLoader/，放 mods/ 不会被读取。
    config_written = _write_config(
        config_path_for(mc_dir, instance, isolated=isolated), base_url
    )
    if existing:
        return CslStatus("present", jar=existing[0].name, config_written=config_written, note="已存在 CustomSkinLoader")

    if cancel is not None and getattr(cancel, "is_set", lambda: False)():
        return CslStatus("failed", note="已取消", config_written=config_written)

    try:
        client = ModrinthClient(base_url, downloader=downloader, source=getattr(downloader, "_source", None))
        best = client.best_version(CSL_SLUG, game_version=instance.mc_version, loader=loader)
        file = best.primary_file if best is not None else None
        if file is None or not file.url:
            warn(f"{instance.loader} {instance.mc_version} 没有匹配的 CustomSkinLoader 版本")
            return CslStatus("failed", note="没有匹配的 CSL 版本", config_written=config_written)
        url = client.resolve_url(file.url)
        dest = mods / (file.filename or f"{CSL_JAR_PREFIX}.jar")
        task = DownloadTask(url=url, dest=dest, sha1=file.sha1 or None, size=file.size or None,
                           label="CustomSkinLoader")
        downloader.download(task, cancel=cancel)
        return CslStatus("installed", jar=dest.name, config_written=config_written, note="已安装 CustomSkinLoader")
    except (DownloadError, ContentError, OSError) as exc:
        warn(f"安装失败（不影响启动）：{exc}")
        return CslStatus("failed", note="CSL 安装失败", config_written=config_written)
