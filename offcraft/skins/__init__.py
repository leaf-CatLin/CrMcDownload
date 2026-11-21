"""客户端皮肤：本地校验、本地保存与资源包生成（PCL 做法）。

皮肤**只存本机**（``<数据目录>/skins/``），启动前写成
``<游戏目录>/resourcepacks/CrMCDownloadSkin/`` 覆盖原版玩家贴图——
不需要 CustomSkinLoader、不联网、原版实例也能用。

``upload`` 模块里仍保留旧的镜像上传函数（历史数据与测试在用），
但界面已经不再提供上传入口。
"""

from __future__ import annotations

from .avatar import head_avatar
from .csl import CSL_CONFIG_NAME, CSL_JAR_PREFIX, CslStatus, ensure_installed
from .default import DEFAULT_SKIN_NAME, default_skin_bytes
from .pack import build_skin_pack, pack_format_for, remove_skin_pack
from .upload import (
    SkinError,
    SkinInfo,
    delete_skin,
    fetch_skin,
    list_skins,
    load_local_skin,
    read_skin,
    remove_local_skin,
    upload_skin,
)

__all__ = [
    "CSL_CONFIG_NAME",
    "CSL_JAR_PREFIX",
    "CslStatus",
    "build_skin_pack",
    "DEFAULT_SKIN_NAME",
    "SkinError",
    "SkinInfo",
    "default_skin_bytes",
    "delete_skin",
    "ensure_installed",
    "fetch_skin",
    "head_avatar",
    "pack_format_for",
    "list_skins",
    "load_local_skin",
    "read_skin",
    "remove_local_skin",
    "remove_skin_pack",
    "upload_skin",
]
