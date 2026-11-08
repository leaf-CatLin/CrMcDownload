"""服务端配置：全部来自环境变量，便于 systemd / Docker 注入。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Settings", "load_env_file"]


def load_env_file(*paths: str | Path) -> dict[str, str]:
    """把 ``.env`` 注入 ``os.environ``，让 ``uv run main.py`` 直接读到部署配置。

    规则对齐 python-dotenv 的常见行为：忽略空行与 ``#`` 注释，支持 ``export``
    前缀与成对的单/双引号；同一文件内后写的键生效；**已存在的环境变量不覆盖**
    （systemd / Docker 注入的值优先）。文件不存在时静默跳过。
    """
    loaded: dict[str, str] = {}
    for raw_path in paths:
        try:
            lines = Path(raw_path).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key:
                loaded[key] = value
    for key, value in loaded.items():
        os.environ.setdefault(key, value)
    return loaded


def _int(env: dict[str, str], name: str, default: int) -> int:
    """容错地读整数环境变量（非法值回落默认值，不把服务启崩）。"""
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _float(env: dict[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default

#: 默认持久数据根目录：与 server/ 同级的 db/CrMcDownload（没有会自动创建）。
#: 数据库、皮肤、官方插件包、热门种子都放在这里，避免散落在部署目录里。
DEFAULT_DATA_ROOT = Path("../db/CrMcDownload")

DEFAULT_UPSTREAMS = {
    "meta": "https://piston-meta.mojang.com",
    "data": "https://piston-data.mojang.com",
    "libs": "https://libraries.minecraft.net",
    "assets": "https://resources.download.minecraft.net",
    "fabric": "https://meta.fabricmc.net",
    # Fabric 加载器的 jar 依赖
    "fabricmaven": "https://maven.fabricmc.net",
    # Forge / NeoForge maven（maven-metadata.xml 与安装器依赖）
    "forgemaven": "https://maven.minecraftforge.net",
    "neoforgemaven": "https://maven.neoforged.net",
    # Modrinth 搜索/版本 API（保留查询串）
    "modrinth": "https://api.modrinth.com",
    # Modrinth 文件 CDN（.jar / .mrpack / 图标）
    "modcdn": "https://cdn.modrinth.com",
    # GitHub Releases 资产（客户端安装包镜像下载，支持 Range 断点续传）
    "github": "https://github.com",
}


@dataclass(slots=True)
class Settings:
    listen: str = "127.0.0.1:8081"
    cache_dir: Path = Path("./cache")
    token: str = ""
    metadata_ttl: int = 600
    proxy: str = ""
    upstreams: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_UPSTREAMS))
    max_cache_bytes: int = 0  # 0 = 不限
    #: 单个文件超过这个大小就不进缓存（仍会正常转发），默认 1 GiB
    max_file_bytes: int = 1024 * 1024 * 1024
    #: 同时进行的上游回源请求上限，0 表示不限
    max_upstreams: int = 8
    #: 同一个键正在回源时，其他请求最多等多久（秒）
    flight_wait: float = 60.0
    #: 上游并发名额的等待时间（秒），拿不到就返回 503
    upstream_wait: float = 30.0
    #: 过期缓存清理的最小间隔（秒）
    sweep_interval: float = 300.0
    #: 持久数据根目录（默认 ../db/CrMcDownload）
    data_root: Path = DEFAULT_DATA_ROOT
    #: 联机数据层（SQLite）路径
    db_path: Path = DEFAULT_DATA_ROOT / "mirror.db"
    #: 内置热门服务器种子文件
    popular_data: Path = DEFAULT_DATA_ROOT / "popular_servers.json"
    #: 皮肤文件根目录
    skins_dir: Path = DEFAULT_DATA_ROOT / "skins"
    #: Web 看板构建产物目录（pnpm build 的 dist）
    page_dir: Path = Path("./page/dist")
    #: 后台探测间隔（秒）
    probe_interval: int = 300
    #: 是否启用后台探测
    probe_enable: bool = True
    #: 离线联机（P2P）：是否启用信令 + 中继子系统
    p2p_enable: bool = False
    #: 中继数据面监听端口（TCP；控制面走主 HTTP 端口的 /p2p）
    p2p_relay_port: int = 25577
    #: 中继对外通告地址（空则回落 settings.host，供客户端连回中继）
    p2p_relay_host: str = ""
    #: 同时在途的房间上限（内存态，防滥用）
    p2p_max_rooms: int = 256
    #: 每台设备（device_id）可持久创建的房间上限（现存未删除数）
    p2p_max_rooms_per_device: int = 10
    #: 房间/成员空闲多久后被回收（秒）
    p2p_room_ttl: int = 180
    #: 单条中继连接累计转发字节上限（超限断开，避免沦为开放中继）
    p2p_relay_bytes_cap: int = 512 * 1024 * 1024
    #: 虚拟局域网（VIP 分配 + UDP 边缘打洞/中继）：是否启用（关闭后客户端回落旧隧道）
    p2p_vpn_enable: bool = True
    #: UDP 边缘服务监听端口（数据面；HELLO 登记端点 + 按 sid 转发）
    p2p_udp_port: int = 25578
    #: 单个房间经 UDP 边缘累计转发字节上限（超限停止转发）
    p2p_udp_bytes_cap: int = 512 * 1024 * 1024
    #: edge_token 有效期（秒）：绑定 room+sid+device，到期由信令重新签发
    p2p_edge_token_ttl: int = 180
    #: 官方特权插件（离线联机）可下载包的存放目录（GET /plugin/* 从此读取）
    plugin_dir: Path = DEFAULT_DATA_ROOT / "plugins"
    #: 更新配置：GitHub 仓库、最新/最低版本、是否强制、发布页与安装包文件名
    update_repo: str = "leaf-CatLin/CrMcDownload"
    update_latest: str = "0.8.5"
    update_min: str = ""          # 空 = 不强制更新
    update_enforce: bool = True
    update_release_url: str = ""  # 空 = 按仓库 + tag 拼 GitHub Releases 页面
    update_asset: str = ""        # 空 = CrMCDownload-<version>-win64-setup.exe

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = env or os.environ
        upstreams = dict(DEFAULT_UPSTREAMS)
        for key in list(upstreams):
            value = env.get(f"UPSTREAM_{key.upper()}")
            if value:
                upstreams[key] = value.rstrip("/")
        root = Path(env.get("MIRROR_DATA_DIR") or DEFAULT_DATA_ROOT)
        return cls(
            listen=env.get("MIRROR_LISTEN", "127.0.0.1:8081"),
            cache_dir=Path(env.get("MIRROR_CACHE_DIR", "./cache")),
            token=env.get("MIRROR_TOKEN", ""),
            metadata_ttl=_int(env, "MIRROR_METADATA_TTL", 600),
            proxy=env.get("UPSTREAM_PROXY", ""),
            upstreams=upstreams,
            max_cache_bytes=_int(env, "MIRROR_MAX_CACHE_BYTES", 0),
            max_file_bytes=_int(env, "MIRROR_MAX_FILE_BYTES", 1024 * 1024 * 1024),
            max_upstreams=_int(env, "MIRROR_MAX_UPSTREAM", 8),
            flight_wait=_float(env, "MIRROR_FLIGHT_WAIT", 60.0),
            upstream_wait=_float(env, "MIRROR_UPSTREAM_WAIT", 30.0),
            sweep_interval=_float(env, "MIRROR_SWEEP_INTERVAL", 300.0),
            data_root=root,
            db_path=Path(env.get("MIRROR_DB_PATH") or root / "mirror.db"),
            popular_data=Path(env.get("MIRROR_POPULAR_DATA") or root / "popular_servers.json"),
            skins_dir=Path(env.get("MIRROR_SKINS_DIR") or root / "skins"),
            page_dir=Path(env.get("MIRROR_PAGE_DIR", "./page/dist")),
            probe_interval=_int(env, "MIRROR_SERVER_PROBE_INTERVAL", 300),
            probe_enable=(env.get("MIRROR_SERVER_PROBE_ENABLE", "on").strip().lower()
                          not in ("0", "false", "no", "off")),
            p2p_enable=(env.get("MIRROR_P2P_ENABLE", "").strip().lower()
                        in ("1", "true", "yes", "on")),
            p2p_relay_port=_int(env, "MIRROR_P2P_RELAY_PORT", 25577),
            p2p_relay_host=(env.get("MIRROR_P2P_RELAY_HOST", "").strip()),
            p2p_max_rooms=_int(env, "MIRROR_P2P_MAX_ROOMS", 256),
            p2p_max_rooms_per_device=_int(env, "MIRROR_P2P_MAX_ROOMS_PER_DEVICE", 10),
            p2p_room_ttl=_int(env, "MIRROR_P2P_ROOM_TTL", 180),
            p2p_relay_bytes_cap=_int(env, "MIRROR_P2P_RELAY_BYTES_CAP", 512 * 1024 * 1024),
            p2p_vpn_enable=(env.get("MIRROR_P2P_VPN_ENABLE", "on").strip().lower()
                            not in ("0", "false", "no", "off")),
            p2p_udp_port=_int(env, "MIRROR_P2P_UDP_PORT", 25578),
            p2p_udp_bytes_cap=_int(env, "MIRROR_P2P_UDP_BYTES_CAP", 512 * 1024 * 1024),
            p2p_edge_token_ttl=_int(env, "MIRROR_P2P_EDGE_TOKEN_TTL", 180),
            plugin_dir=Path(env.get("MIRROR_PLUGIN_DIR") or root / "plugins"),
            update_repo=(env.get("MIRROR_UPDATE_REPO") or "leaf-CatLin/CrMcDownload").strip(),
            update_latest=(env.get("MIRROR_UPDATE_LATEST_VERSION") or "0.8.5").strip(),
            update_min=(env.get("MIRROR_UPDATE_MIN_VERSION") or "").strip(),
            update_enforce=(env.get("MIRROR_UPDATE_ENFORCE", "on").strip().lower()
                            not in ("0", "false", "no", "off")),
            update_release_url=(env.get("MIRROR_UPDATE_RELEASE_URL") or "").strip(),
            update_asset=(env.get("MIRROR_UPDATE_ASSET") or "").strip(),
        )

    def prepare(self) -> None:
        """创建持久数据目录（数据库 / 皮肤 / 插件 / 热门种子），幂等。"""
        for directory in (
            self.data_root,
            self.db_path.parent,
            self.popular_data.parent,
            self.skins_dir,
            self.plugin_dir,
        ):
            Path(directory).mkdir(parents=True, exist_ok=True)

    @property
    def host(self) -> str:
        return self.listen.rsplit(":", 1)[0]

    @property
    def port(self) -> int:
        return int(self.listen.rsplit(":", 1)[1])
