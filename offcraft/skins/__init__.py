"""客户端皮肤：本地校验、上传与 CSL mod 安装。

只与 ``download_base`` 通信：皮肤上传走 ``POST {base}/skin/<name>.png``，令牌按
:class:`~offcraft.net.downloader.Downloader` 的 origin 规则发送；CSL mod 经内容层
（Modrinth 镜像）自动装进带加载器的实例。任何失败都只记 WARN，绝不阻断启动。
"""

from __future__ import annotations

from .avatar import head_avatar
from .csl import CSL_CONFIG_NAME, CSL_JAR_PREFIX, CslStatus, ensure_installed
from .default import DEFAULT_SKIN_NAME, default_skin_bytes
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
    "DEFAULT_SKIN_NAME",
    "SkinError",
    "SkinInfo",
    "default_skin_bytes",
    "delete_skin",
    "ensure_installed",
    "fetch_skin",
    "head_avatar",
    "list_skins",
    "load_local_skin",
    "read_skin",
    "remove_local_skin",
    "upload_skin",
]
