"""``python -m server_mirror`` 入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from server_mirror.api import ApiRouter
from server_mirror.config import Settings, load_env_file
from server_mirror.db import Database
from server_mirror.proxy import MirrorHTTPServer
from server_mirror.skins import SkinStore
from server_mirror.servers import ServerProbe
from server_mirror.static import StaticHandler

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示
    from server_mirror.p2p import RelayServer, SignalingRouter, UdpEdgeServer

__all__ = ["main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="server_mirror", description="CrMcDownload 镜像服务")
    parser.add_argument("--listen", help="监听地址，默认 127.0.0.1:8081")
    parser.add_argument("--cache-dir", help="缓存目录")
    parser.add_argument("--token", help="访问令牌（X-OffCraft-Token）")
    parser.add_argument("--ttl", type=int, help="元数据缓存秒数")
    parser.add_argument("--proxy", help="上游代理，如 socks5h://127.0.0.1:1080")
    return parser


def _build_p2p(
    settings: Settings, db: Database
) -> tuple["SignalingRouter | None", "RelayServer | None", "UdpEdgeServer | None"]:
    """启用离线联机时构造信令 + TCP 中继 + UDP 边缘；否则全为 ``None``。

    信令需 ``db`` 做房间持久登记；UDP 边缘仅在 ``p2p_vpn_enable`` 打开时启动，
    关闭后客户端自动回落旧隧道模式。
    """
    if not getattr(settings, "p2p_enable", False):
        return None, None, None
    from server_mirror.p2p import RelayServer, SignalingRouter, UdpEdgeServer

    relay = RelayServer(settings)
    edge = UdpEdgeServer(settings) if getattr(settings, "p2p_vpn_enable", True) else None
    p2p_router = SignalingRouter(settings=settings, relay=relay, db=db, edge=edge)
    return p2p_router, relay, edge


def main(argv: list[str] | None = None) -> int:
    # 手动 `uv run main.py` 时也能读到部署目录的 .env（systemd/Docker 注入优先）。
    load_env_file(Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env")
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    if args.listen:
        settings.listen = args.listen
    if args.cache_dir:
        settings.cache_dir = Path(args.cache_dir)
    if args.token is not None:
        settings.token = args.token
    if args.ttl:
        settings.metadata_ttl = args.ttl
    if args.proxy:
        settings.proxy = args.proxy

    settings.prepare()  # 持久数据目录（默认 ../db/CrMcDownload）不存在就创建
    # 版本自动检测：最新版本来自 GitHub Releases，服务端缓存（MIRROR_UPDATE_AUTO=off 可关）
    settings.releases_provider = settings.build_releases()
    if settings.update_auto:
        settings.releases_provider.start()
    db = Database(settings.db_path)
    skins = SkinStore(settings.skins_dir)
    page = StaticHandler(settings.page_dir)
    probe = (
        ServerProbe(
            db,
            interval=settings.probe_interval,
            concurrency=max(1, settings.max_upstreams),
            popular_path=settings.popular_data,
        )
        if settings.probe_enable
        else None
    )
    router = ApiRouter(
        settings=settings,
        db=db,
        skins=skins,
        page=page,
        metrics_snapshot=None,   # 注入后填充
        cache_stats=None,
        probe_status=probe.status if probe else None,
    )
    p2p_router, relay, edge = _build_p2p(settings, db)
    router.p2p = p2p_router
    server = MirrorHTTPServer(
        (settings.host, settings.port), settings, router=router, db=db, probe=probe, relay=relay
    )
    # 指标与缓存统计需要 server 先存在，构造后再回填
    router.metrics_snapshot = server.metrics.snapshot
    router.cache_stats = server.cache.stats
    if probe is not None:
        probe.start()
    if relay is not None:
        relay.start()
    if edge is not None:
        edge.start()
    print(
        f"mirror listening on http://{settings.host}:{settings.port} "
        f"cache={settings.cache_dir} token={'on' if settings.token else 'off'} "
        f"db={settings.db_path} probe={'on' if probe else 'off'} "
        f"versions={'github' if settings.update_auto else 'static'}"
        f" p2p={'on' if p2p_router else 'off'}"
        + (f" p2p-udp={settings.host}:{edge.port}" if edge is not None else ""),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if edge is not None:
            edge.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
