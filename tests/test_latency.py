"""镜像 / 直连择优（offcraft.net.latency）的单元测试。

用两个本地 HTTP 服务模拟「镜像」与「直连」，一个快、一个慢，
验证：择快、回退、缓存复用、未知主机与开关行为。
"""

import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from offcraft.mirror import DirectSource, Mirror
from offcraft.net.latency import SourceRouter, make_source, probe_source

PAYLOAD = b"x" * 4096


class _FastHandler(BaseHTTPRequestHandler):
    delay = 0.0
    hits = 0
    lock = threading.Lock()

    def log_message(self, *args):  # noqa: D401 - 静默
        pass

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        cls = type(self)
        with cls.lock:
            cls.hits += 1
        if cls.delay:
            time.sleep(cls.delay)
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        self.wfile.write(PAYLOAD)


def _serve(delay: float) -> tuple[ThreadingHTTPServer, str]:
    handler = type(
        "Handler",
        (_FastHandler,),
        {"delay": delay, "hits": 0, "lock": threading.Lock()},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _dead_url() -> str:
    """返回一个必定拒绝连接的本地地址。"""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}"


class SourceRouterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fast, cls.fast_base = _serve(0.0)
        cls.slow, cls.slow_base = _serve(0.15)

    @classmethod
    def tearDownClass(cls) -> None:
        for server in (cls.fast, cls.slow):
            server.shutdown()
            server.server_close()

    def setUp(self) -> None:
        self.fast.RequestHandlerClass.hits = 0
        self.slow.RequestHandlerClass.hits = 0

    def router(self, mirror_base: str, **kwargs) -> SourceRouter:
        return SourceRouter(Mirror(mirror_base), samples=1, timeout=3.0, **kwargs)

    def test_prefers_faster_mirror(self) -> None:
        router = self.router(self.fast_base)
        # 镜像（fast）与直连（slow）指向同一类资源，镜像更快 → 应选镜像
        official = f"{self.slow_base}/maven/thing.jar"
        mirrored = self.fast_base + "/forgemaven/maven/thing.jar"
        router._mirrored = lambda url: (mirrored, "route:libs")  # type: ignore[assignment]
        self.assertEqual(router.rewrite(official), mirrored)
        decision = router.decisions()[0]
        self.assertEqual(decision["mode"], "mirror")
        self.assertIsNotNone(decision["mirror_ms"])
        self.assertIsNotNone(decision["direct_ms"])
        self.assertLess(decision["mirror_ms"], decision["direct_ms"])

    def test_prefers_faster_direct(self) -> None:
        router = self.router(self.slow_base)
        mirrored = self.slow_base + "/libs/a.jar"  # 镜像慢
        official = self.fast_base + "/a.jar"  # 直连快
        router._mirrored = lambda url: (mirrored, "route:libs")  # type: ignore[assignment]
        self.assertEqual(router.rewrite(official), official)
        self.assertEqual(router.decisions()[0]["mode"], "direct")
        self.assertIn("直连更快", router.decisions()[0]["detail"])

    def test_dead_mirror_falls_back(self) -> None:
        router = self.router(_dead_url())
        official = self.fast_base + "/a.jar"
        router._mirrored = lambda url: (_dead_url() + "/libs/a.jar", "route:libs")  # type: ignore[assignment]
        self.assertEqual(router.rewrite(official), official)
        self.assertIn("镜像不可用", router.decisions()[0]["detail"])

    def test_decision_is_cached(self) -> None:
        router = self.router(self.fast_base)
        official = self.slow_base + "/a.jar"
        mirrored = self.fast_base + "/libs/a.jar"
        router._mirrored = lambda url: (mirrored, "route:libs")  # type: ignore[assignment]
        handler = self.fast.RequestHandlerClass
        handler.hits = 0
        router.rewrite(official)
        first = handler.hits
        router.rewrite(official)
        router.rewrite(official)
        self.assertEqual(handler.hits, first, "缓存命中后不应再次探测")
        self.assertGreaterEqual(first, 1)

    def test_unknown_host_passes_through(self) -> None:
        router = self.router(self.fast_base)
        url = "https://example.invalid/some/file.bin"
        self.assertEqual(router.rewrite(url), url)

    def test_api_url_delegates_to_mirror(self) -> None:
        router = self.router(self.fast_base)
        self.assertEqual(router.api_url("/skin/Player.png"), self.fast_base + "/skin/Player.png")
        self.assertEqual(router.api_url("api/skins/list"), self.fast_base + "/api/skins/list")

    def test_disabled_router_never_probes(self) -> None:
        router = SourceRouter(Mirror(self.fast_base), enabled=False, samples=1)
        handler = self.fast.RequestHandlerClass
        handler.hits = 0
        url = "https://libraries.minecraft.net/a.jar"
        self.assertEqual(
            router.rewrite(url), self.fast_base + "/libs/a.jar"
        )
        self.assertEqual(handler.hits, 0)

    def test_rank_orders_by_latency(self) -> None:
        router = self.router(self.slow_base)
        order = router.rank(
            [("mirror", self.slow_base), ("direct", self.fast_base)],
            "probe",
            "/x",
        )
        self.assertEqual([name for name, _ in order], ["direct", "mirror"])

    def test_rank_puts_unreachable_last(self) -> None:
        router = self.router(self.slow_base)
        order = router.rank(
            [("mirror", _dead_url()), ("direct", self.slow_base)],
            "probe2",
            "/x",
        )
        self.assertEqual([name for name, _ in order], ["direct", "mirror"])

    def test_ttl_zero_always_remeasures(self) -> None:
        router = self.router(self.fast_base, ttl=0)
        official = self.slow_base + "/a.jar"
        mirrored = self.slow_base + "/libs/a.jar"
        router._mirrored = lambda url: (mirrored, "route:libs")  # type: ignore[assignment]
        handler = self.slow.RequestHandlerClass
        handler.hits = 0
        router.rewrite(official)
        first = handler.hits
        router.rewrite(official)
        self.assertGreater(handler.hits, first, "ttl=0 时不应复用旧结论")

    def test_prober_token_scoped_to_mirror(self) -> None:
        """探测器的镜像令牌只能发给镜像源，不能随直连探测泄露。"""
        router = self.router(self.fast_base, token="tkn-123")
        prober = router._prober
        self.assertEqual(prober.token_scope(), self.fast_base)
        self.assertTrue(prober.sends_token(self.fast_base + "/libs/a.jar"))
        self.assertFalse(prober.sends_token(self.slow_base + "/a.jar"))


_RATE_PAYLOAD = b"r" * (256 * 1024)


class _RateHandler(BaseHTTPRequestHandler):
    """按块 + 间隔发送，用于区分「低延迟但出口窄」与「高速」的源。"""

    chunk_delay = 0.0
    chunk = 16 * 1024
    hits = 0
    lock = threading.Lock()

    def log_message(self, *args):  # noqa: D401 - 静默
        pass

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        cls = type(self)
        with cls.lock:
            cls.hits += 1
        self.send_response(200)
        self.send_header("Content-Length", str(len(_RATE_PAYLOAD)))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        try:
            for start in range(0, len(_RATE_PAYLOAD), self.chunk):
                self.wfile.write(_RATE_PAYLOAD[start : start + self.chunk])
                if cls.chunk_delay:
                    time.sleep(cls.chunk_delay)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _serve_rate(chunk_delay: float) -> tuple[ThreadingHTTPServer, str]:
    handler = type(
        "Handler",
        (_RateHandler,),
        {"chunk_delay": chunk_delay, "hits": 0, "lock": threading.Lock()},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


class MultiSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fast, cls.fast_base = _serve_rate(0.0)
        cls.slow, cls.slow_base = _serve_rate(0.02)

    @classmethod
    def tearDownClass(cls) -> None:
        for server in (cls.fast, cls.slow):
            server.shutdown()
            server.server_close()

    def test_prefers_higher_throughput(self) -> None:
        router = SourceRouter(
            sources=[self.slow_base, self.fast_base],
            samples=1,
            timeout=3.0,
            flush=False,
        )
        url = "https://libraries.minecraft.net/a.jar"
        self.assertEqual(router.rewrite(url), self.fast_base + "/libs/a.jar")
        decision = router.decisions()[0]
        self.assertEqual(decision["mode"], "mirror")
        self.assertIn("→", str(decision["detail"]))

    def test_flush_dns_before_probe(self) -> None:
        router = SourceRouter(
            sources=[self.fast_base],
            samples=1,
            timeout=3.0,
            ttl=0,
            flush_cooldown=0,
        )
        with mock.patch("offcraft.net.latency.flush_dns", return_value=True) as flushed:
            router.rewrite("https://libraries.minecraft.net/a.jar")
            router.rewrite("https://libraries.minecraft.net/b.jar")
        self.assertGreaterEqual(flushed.call_count, 2)

    def test_rescan_switches_after_decision(self) -> None:
        router = SourceRouter(
            sources=[self.fast_base],
            samples=1,
            timeout=3.0,
            flush=False,
        )
        url = "https://libraries.minecraft.net/a.jar"
        chosen = router.rewrite(url)
        # 没有更优源时 resolve_again 返回 None（下载器会关闭慢速检测继续下完）
        self.assertIsNone(router.resolve_again(chosen))


class ProbeSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.base = _serve(0.0)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_probe_source_reports_latency(self) -> None:
        with mock.patch("offcraft.net.latency.flush_dns", return_value=True):
            info = probe_source(self.base, timeout=3.0, sample_bytes=4096)
        self.assertTrue(info["ok"], info.get("error"))
        self.assertIsNotNone(info["latency"])
        self.assertEqual(info["url"], self.base + "/meta/mc/game/version_manifest_v2.json")

    def test_probe_dead_source_reports_error(self) -> None:
        with mock.patch("offcraft.net.latency.flush_dns", return_value=True):
            info = probe_source(_dead_url(), timeout=1.0, sample_bytes=4096)
        self.assertFalse(info["ok"])


class MakeSourceTest(unittest.TestCase):
    def test_empty_base_is_direct(self) -> None:
        self.assertIsInstance(make_source(""), DirectSource)

    def test_multi_sources_build_router(self) -> None:
        source = make_source(
            sources=["https://a.example.com", "https://b.example.com"], enabled=True
        )
        self.assertIsInstance(source, SourceRouter)
        self.assertEqual(source.bases, ["https://a.example.com", "https://b.example.com"])

    def test_multi_sources_direct_only(self) -> None:
        self.assertIsInstance(make_source(sources=[""]), DirectSource)

    def test_multi_sources_disabled_uses_primary(self) -> None:
        source = make_source(
            sources=["https://b.example.com", "https://a.example.com"], enabled=False
        )
        self.assertIsInstance(source, Mirror)
        self.assertEqual(source.base_url, "https://b.example.com")

    def test_invalid_base_is_direct(self) -> None:
        self.assertIsInstance(make_source("not-a-url"), DirectSource)

    def test_plain_mirror_when_disabled(self) -> None:
        source = make_source("https://mc.example.com", enabled=False)
        self.assertIsInstance(source, Mirror)
        self.assertEqual(
            source.rewrite("https://libraries.minecraft.net/a.jar"),
            "https://mc.example.com/libs/a.jar",
        )

    def test_router_when_enabled(self) -> None:
        source = make_source("https://mc.example.com", enabled=True)
        self.assertIsInstance(source, SourceRouter)
        self.assertEqual(source.base_url, "https://mc.example.com")


if __name__ == "__main__":
    unittest.main()
