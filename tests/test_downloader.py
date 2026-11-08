import hashlib
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from offcraft.net.downloader import (
    Cancelled,
    DownloadError,
    DownloadTask,
    Downloader,
    DownloadReport,
    ProgressTracker,
    origin_of,
    parse_origin,
    verify_file,
)

PAYLOAD = bytes(range(256)) * 4096  # 1 MiB
PAYLOAD_SHA1 = hashlib.sha1(PAYLOAD).hexdigest()


class _Handler(BaseHTTPRequestHandler):
    payload = PAYLOAD
    hits: list[tuple[str, str | None]] = []
    tokens: list[str | None] = []

    def log_message(self, *args):  # noqa: D401 - 静默
        pass

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        _Handler.hits.append((self.path, self.headers.get("Range")))
        _Handler.tokens.append(self.headers.get("X-OffCraft-Token"))
        if self.path.startswith("/missing"):
            self.send_error(404)
            return
        data = self.payload
        range_header = self.headers.get("Range")
        start = 0
        if range_header and range_header.startswith("bytes="):
            start = int(range_header.split("=", 1)[1].split("-", 1)[0] or 0)
            if start >= len(data):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.end_headers()
                return
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        self.wfile.write(body)


class _DripHandler(BaseHTTPRequestHandler):
    """支持 Range，但按块 + 间隔发送，模拟「出口很窄」的下载源。"""

    chunk = 64 * 1024
    delay = 0.05
    hits: list[tuple[str, str | None]] = []

    def log_message(self, *args):  # noqa: D401 - 静默
        pass

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        _DripHandler.hits.append((self.path, self.headers.get("Range")))
        data = PAYLOAD
        range_header = self.headers.get("Range")
        start = 0
        if range_header and range_header.startswith("bytes="):
            start = int(range_header.split("=", 1)[1].split("-", 1)[0] or 0)
            if start >= len(data):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.end_headers()
                return
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        try:
            for offset in range(0, len(body), self.chunk):
                self.wfile.write(body[offset : offset + self.chunk])
                time.sleep(self.delay)
        except (BrokenPipeError, ConnectionResetError):
            pass


class SlowSwitchTest(unittest.TestCase):
    """下载过慢时保留断点并切换到更快的源。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fast = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.slow = ThreadingHTTPServer(("127.0.0.1", 0), _DripHandler)
        for server in (cls.fast, cls.slow):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.fast_base = f"http://127.0.0.1:{cls.fast.server_address[1]}"
        cls.slow_base = f"http://127.0.0.1:{cls.slow.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        for server in (cls.fast, cls.slow):
            server.shutdown()
            server.server_close()

    def test_slow_download_switches_source(self) -> None:
        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a.bin"
            _Handler.hits.clear()
            _DripHandler.hits.clear()
            task = DownloadTask(
                f"{self.slow_base}/a", dest, PAYLOAD_SHA1, len(PAYLOAD)
            )
            fast_url = f"{self.fast_base}/a"
            downloader = Downloader(
                workers=1,
                retries=0,
                slow_threshold=5 * 1024 * 1024,
                slow_window=0.05,
                slow_warmup=0.0,
                slow_min_bytes=0,
                stall_switches=1,
                url_resolver=lambda url: fast_url if self.slow_base in url else None,
            )
            report = downloader.run([task])
            self.assertTrue(report.ok, report.failed)
            self.assertEqual(dest.read_bytes(), PAYLOAD)
            self.assertTrue(_DripHandler.hits, "应先走慢速源")
            self.assertTrue(_Handler.hits, "过慢后应切换到快速源")


class DownloaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_download_and_skip(self) -> None:
        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a.bin"
            task = DownloadTask(f"{self.base}/a", dest, PAYLOAD_SHA1, len(PAYLOAD))
            downloader = Downloader(workers=2)
            report = downloader.run([task])
            self.assertIsInstance(report, DownloadReport)
            self.assertTrue(report.ok, report.failed)
            self.assertEqual(dest.read_bytes(), PAYLOAD)
            self.assertEqual(len(report.succeeded), 1)

            again = downloader.run([task])
            self.assertEqual(len(again.skipped), 1)
            self.assertEqual(len(again.succeeded), 0)

    def test_sha1_mismatch(self) -> None:
        with TemporaryDirectory() as tmp:
            task = DownloadTask(f"{self.base}/a", Path(tmp) / "a.bin", "0" * 40, len(PAYLOAD))
            report = Downloader(retries=0).run([task])
            self.assertFalse(report.ok)
            self.assertFalse((Path(tmp) / "a.bin").exists())

    def test_resume_from_part(self) -> None:
        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a.bin"
            part = dest.with_name(dest.name + ".part")
            part.write_bytes(PAYLOAD[: len(PAYLOAD) // 2])
            _Handler.hits.clear()
            task = DownloadTask(f"{self.base}/a", dest, PAYLOAD_SHA1, len(PAYLOAD))
            report = Downloader(workers=1).run([task])
            self.assertTrue(report.ok, report.failed)
            self.assertEqual(dest.read_bytes(), PAYLOAD)
            self.assertEqual(_Handler.hits[0][1], f"bytes={len(PAYLOAD) // 2}-")
            self.assertEqual(len(_Handler.hits), 1, "续传不应整包重下")

    def test_http_error_reported(self) -> None:
        with TemporaryDirectory() as tmp:
            task = DownloadTask(f"{self.base}/missing", Path(tmp) / "x.bin")
            report = Downloader(retries=0).run([task])
            self.assertFalse(report.ok)
            self.assertIn("HTTP 404", report.failed[0][1])

    def test_progress_and_cancel(self) -> None:
        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a.bin"
            task = DownloadTask(f"{self.base}/a", dest, PAYLOAD_SHA1, len(PAYLOAD))
            cancel = threading.Event()
            tracker = ProgressTracker()

            def on_progress(state: ProgressTracker) -> None:
                if state.done_bytes > 0:
                    cancel.set()

            report = Downloader(
                workers=1, progress_callback=on_progress, progress_interval=0.0
            ).run([task], tracker=tracker, cancel=cancel)
            self.assertFalse(report.ok)
            self.assertEqual(report.failed[0][1], "已取消")
            self.assertFalse(dest.exists())

    def test_verify_file(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.bin"
            path.write_bytes(PAYLOAD)
            self.assertTrue(verify_file(path, PAYLOAD_SHA1, len(PAYLOAD)))
            self.assertFalse(verify_file(path, PAYLOAD_SHA1, len(PAYLOAD) + 1))
            self.assertFalse(verify_file(path, "0" * 40, None))

    def test_cancel_before_start(self) -> None:
        with TemporaryDirectory() as tmp:
            cancel = threading.Event()
            cancel.set()
            task = DownloadTask(f"{self.base}/a", Path(tmp) / "a.bin")
            report = Downloader().run([task], cancel=cancel)
            self.assertFalse(report.ok)
            self.assertEqual(report.failed[0][1], "已取消")


class TokenScopeTest(unittest.TestCase):
    """镜像令牌只能发给配置的镜像源（Issue #1 P1）。"""

    TOKEN = "secret-token"

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        _Handler.tokens.clear()

    def _fetch(self, downloader: Downloader, url: str) -> None:
        with TemporaryDirectory() as tmp:
            task = DownloadTask(url, Path(tmp) / "a.bin")
            downloader.run([task])

    def test_mirror_origin_gets_token(self) -> None:
        downloader = Downloader(token=self.TOKEN, token_origin=self.base, retries=0)
        self._fetch(downloader, f"{self.base}/a")
        self.assertEqual(_Handler.tokens, [self.TOKEN])

    def test_token_scope_normalised(self) -> None:
        downloader = Downloader(token=self.TOKEN, token_origin=f"{self.base}/", retries=0)
        self.assertEqual(downloader.token_scope(), f"http://127.0.0.1:{self.port}")
        self.assertTrue(downloader.sends_token(f"{self.base}/x/y"))

    def test_other_host_gets_no_token(self) -> None:
        downloader = Downloader(
            token=self.TOKEN, token_origin="https://mc.example.com", retries=0
        )
        self._fetch(downloader, f"{self.base}/a")
        self.assertEqual(_Handler.tokens, [None])

    def test_other_scheme_gets_no_token(self) -> None:
        # 同一主机的 https origin 不得因 http 请求而泄漏令牌
        downloader = Downloader(token=self.TOKEN, token_origin=f"https://127.0.0.1:{self.port}")
        self._fetch(downloader, f"{self.base}/a")
        self.assertEqual(_Handler.tokens, [None])

    def test_other_port_gets_no_token(self) -> None:
        downloader = Downloader(token=self.TOKEN, token_origin="http://127.0.0.1:9")
        self.assertFalse(downloader.sends_token(f"{self.base}/a"))

    def test_no_token_origin_sends_nothing(self) -> None:
        downloader = Downloader(token=self.TOKEN, retries=0)
        self.assertEqual(downloader.token_scope(), "")
        self._fetch(downloader, f"{self.base}/a")
        self.assertEqual(_Handler.tokens, [None])

    def test_probe_does_not_leak_token(self) -> None:
        downloader = Downloader(token=self.TOKEN, token_origin="https://mc.example.com", retries=0)
        downloader.probe(f"{self.base}/a", first_chunk=64, timeout=5)
        self.assertEqual(_Handler.tokens, [None])

    def test_origin_helpers(self) -> None:
        self.assertEqual(origin_of("https://a.b/c"), ("https", "a.b", 443))
        self.assertEqual(origin_of("http://a.b:8080/c"), ("http", "a.b", 8080))
        self.assertIsNone(origin_of("ftp://a.b/c"))
        self.assertIsNone(origin_of("not a url"))
        self.assertEqual(parse_origin("mc.example.com"), ("https", "mc.example.com", 443))
        self.assertIsNone(parse_origin(""))


if __name__ == "__main__":
    unittest.main()
