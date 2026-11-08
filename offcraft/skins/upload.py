"""皮肤读取 / 校验 / 上传（Pillow）。"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from offcraft.config import config_dir
from offcraft.mirror import Mirror
from offcraft.net.downloader import DownloadError, Downloader

__all__ = [
    "MAX_SKIN_BYTES",
    "NAME_PATTERN",
    "SkinError",
    "SkinInfo",
    "delete_skin",
    "detect_model",
    "fetch_skin",
    "inspect_skin",
    "list_skins",
    "load_local_skin",
    "read_skin",
    "remove_local_skin",
    "save_local_skin",
    "upload_skin",
    "validate_png",
]

NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,16}$")
MAX_SKIN_BYTES = 64 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_ALLOWED_SIZES = {(64, 64), (64, 32)}


class SkinError(ValueError):
    """皮肤校验 / 上传失败（面向用户的中文理由）。"""


@dataclass(frozen=True, slots=True)
class SkinInfo:
    name: str
    model: str            # classic | slim
    width: int
    height: int
    sha1: str
    size: int

    @property
    def filename(self) -> str:
        return f"{self.name}_slim.png" if self.model == "slim" else f"{self.name}.png"


def _require_name(name: str) -> str:
    name = str(name or "")
    if not NAME_PATTERN.match(name):
        raise SkinError("用户名不是合法的 Minecraft 名（1-16 位字母/数字/下划线）")
    return name


def validate_png(data: bytes) -> tuple[int, int]:
    """确认是真 PNG 且尺寸合法，返回 ``(宽, 高)``。"""
    if not data.startswith(_PNG_MAGIC):
        raise SkinError("文件不是有效的 PNG 图片")
    if len(data) > MAX_SKIN_BYTES:
        raise SkinError(f"皮肤文件超过 {MAX_SKIN_BYTES // 1024} KiB 上限")
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise SkinError("运行环境缺少 Pillow，无法校验皮肤") from exc
    try:
        with io.BytesIO(data) as buf:
            image = Image.open(buf)
            image.verify()
        with io.BytesIO(data) as buf:
            image = Image.open(buf)
            size = (int(image.width), int(image.height))
            fmt = (image.format or "").upper()
    except SkinError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SkinError(f"PNG 解码失败：{exc}") from exc
    if fmt != "PNG":
        raise SkinError("只接受 PNG 格式的皮肤")
    if size not in _ALLOWED_SIZES:
        raise SkinError(f"皮肤尺寸必须是 64×64 或 64×32，当前 {size[0]}×{size[1]}")
    return size


def detect_model(data: bytes) -> str:
    """64×32 恒为 classic；64×64 按右半区是否有不透明像素判 slim/classic。"""
    try:
        from PIL import Image

        with io.BytesIO(data) as buf:
            image = Image.open(buf).convert("RGBA")
        if image.size == (64, 32):
            return "classic"
        right = image.crop((32, 0, 64, 32))
        return "slim" if right.getchannel("A").getextrema()[1] > 0 else "classic"
    except Exception:  # noqa: BLE001 - 判定失败按 classic
        return "classic"


def inspect_skin(data: bytes, name: str, *, model: str = "auto") -> SkinInfo:
    """校验内存中的皮肤字节并生成信息（预览已下载 / 内置默认皮肤时用）。"""
    name = _require_name(name)
    width, height = validate_png(data)
    chosen = model if model in ("classic", "slim") else detect_model(data)
    return SkinInfo(
        name=name,
        model=chosen,
        width=width,
        height=height,
        sha1=hashlib.sha1(data).hexdigest(),
        size=len(data),
    )


def read_skin(path: Path | str, name: str, *, model: str = "auto") -> tuple[bytes, SkinInfo]:
    """从磁盘读取并校验皮肤，返回 ``(字节, 信息)``。"""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise SkinError(f"读取皮肤失败：{exc}") from exc
    return data, inspect_skin(data, name, model=model)


def upload_skin(
    data: bytes,
    info: SkinInfo,
    *,
    downloader: Downloader,
    mirror: Mirror | Any,
) -> dict[str, Any]:
    """上传到 ``POST {base}/skin/<name>.png?model=<model>``（走令牌 origin 规则）。"""
    url = mirror.api_url(f"/skin/{info.name}.png") + f"?model={info.model}"
    try:
        result = downloader.request_json(
            url,
            "POST",
            body=data,
            headers={"Content-Type": "image/png"},
            max_size=64 * 1024,
        )
    except DownloadError as exc:
        raise SkinError(f"皮肤上传失败：{exc}") from exc
    return result if isinstance(result, dict) else {}


def fetch_skin(
    name: str,
    *,
    downloader: Downloader,
    mirror: Mirror | Any,
    model: str = "auto",
    fallback: bool = True,
) -> bytes | None:
    """从镜像取回当前账户的皮肤；不存在时按 ``fallback`` 返回内置默认皮肤。"""
    name = _require_name(name)
    model = model if model in ("classic", "slim") else "auto"
    query = f"?model={model}" + ("&fallback=1" if fallback else "")
    url = mirror.api_url(f"/skin/{name}.png") + query
    try:
        data = downloader.fetch_bytes(url, max_size=MAX_SKIN_BYTES * 4)
    except DownloadError:
        if fallback:
            return None
        raise
    if data:
        try:
            validate_png(data)
        except SkinError:
            return None
    return data or None


def delete_skin(name: str, *, downloader: Downloader, mirror: Mirror | Any) -> list[str]:
    """删除镜像上的账户皮肤（classic / slim 一并删除），返回被删文件名。"""
    name = _require_name(name)
    url = mirror.api_url(f"/skin/{name}.png")
    try:
        result = downloader.request_json(url, "DELETE", max_size=64 * 1024)
    except DownloadError as exc:
        raise SkinError(f"删除皮肤失败：{exc}") from exc
    removed = result.get("removed") if isinstance(result, dict) else None
    return [str(item) for item in removed] if isinstance(removed, list) else []


def list_skins(
    *, downloader: Downloader, mirror: Mirror | Any, query: str = "", limit: int = 100
) -> list[dict[str, Any]]:
    """列出镜像皮肤库（只读，免令牌）。"""
    params = f"?limit={max(1, min(int(limit), 200))}"
    if query:
        from urllib.parse import quote

        params += f"&q={quote(str(query)[:32])}"
    url = mirror.api_url("/api/skins/list") + params
    try:
        result = downloader.fetch_json(url)
    except DownloadError as exc:
        raise SkinError(f"读取皮肤库失败：{exc}") from exc
    items = result.get("items") if isinstance(result, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def skin_cache_dir() -> Path:
    return config_dir() / "skins"


def save_local_skin(info: SkinInfo, data: bytes) -> Path:
    directory = skin_cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / info.filename
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path


def load_local_skin(name: str, *, model: str = "classic") -> bytes | None:
    name = _require_name(name)
    path = skin_cache_dir() / (f"{name}_slim.png" if model == "slim" else f"{name}.png")
    try:
        return path.read_bytes()
    except OSError:
        return None


def remove_local_skin(name: str) -> bool:
    """清掉本机缓存的两套皮肤；返回是否删掉了至少一个文件。"""
    name = _require_name(name)
    removed = False
    for filename in (f"{name}.png", f"{name}_slim.png"):
        try:
            (skin_cache_dir() / filename).unlink()
            removed = True
        except OSError:
            pass
    return removed
