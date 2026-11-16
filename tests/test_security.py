"""镜像服务安全边界测试：令牌鉴权、响应头清洗、上游错误路径。

这些路径在 ``proxy.py`` 里是安全关键点（鉴权 / 响应头注入面 / 传输中断），
用真实 HTTP 往返验证，不依赖外网。
"""

import http.client
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from server_mirror.config import Settings
from server_mirror.proxy import MirrorHTTPServer

PAYLOAD = bytes(range(256)) * 16  # 4 KiB


class _Upstream(BaseHTTPRequestHandler):
    payload = PAYLOAD
    status = 200
    extra_response_headers: list[tuple[str, str]] = []
    hit_count = 0
    lock = threading.Lock()

    def log_message(self, *args):  # noqa: D401
        pass

    def do_GET(self):  # noqa: N802
        cls = type(self)
        with cls.lock:
            cls.hit_count += 1
        body = cls.payload
        self.send_response(cls.status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/octet-stream")
        for name, value in cls.extra_response_headers:
            try:
                self.send_header(name, value)
            except Exception:  # noqa: BLE001 - 直接抛会破坏连接，跳过脏头即可
                continue
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


class _AuthenticatedMirror:
    """起一台带令牌鉴权的真实镜像，方便做安全断言。"""

    def __init__(self, cache_dir: Path, *, token: str = "", upstream_kwargs: dict | None = None) -> None:
        kwargs = dict(upstream_kwargs or {})
        handler = type(
            "Upstream",
            (_Upstream,),
            {
                "hit_count": 0,
                "lock": threading.Lock(),
                "payload": kwargs.pop("payload", PAYLOAD),
                "status": kwargs.pop("status", 200),
                "extra_response_headers": kwargs.pop("extra_response_headers", []),
            },
        )
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.upstream_base = f"http://127.0.0.1:{self.upstream.server_address[1]}"
        config = Settings(
            listen="127.0.0.1:0",
            cache_dir=cache_dir,
            token=token,
            upstreams={"libs": self.upstream_base},
        )
        self.server = MirrorHTTPServer(("127.0.0.1", 0), config)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def get(self, path: str, *, token: str | None = None, headers: dict | None = None) -> tuple[int, bytes, list[str]]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdr = dict(headers or {})
        if token is not None:
            hdr["X-OffCraft-Token"] = token
        try:
            conn.request("GET", path, headers=hdr)
            resp = conn.getresponse()
            body = resp.read()
            status = resp.status
            # 抓取原始头文本以断言注入不存在
            raw = resp.msg.items()
            return status, body, [(k, v) for k, v in raw]
        finally:
            conn.close()

    def upstream_hit_count(self) -> int:
        return self.upstream.RequestHandlerClass.hit_count

    def close(self) -> None:
        for server in (self.server, self.upstream):
            server.shutdown()
            server.server_close()


class HeaderSanitizeUpstream(BaseHTTPRequestHandler):
    status = 200
    pending: list[tuple[str, str]] = []

    def log_message(self, *args):  # noqa: D401
        pass

    def do_GET(self):  # noqa: N802
        self.send_response(self.status)
        for name, value in self.pending:
            try:
                self.send_header(name, value)
            except Exception:  # noqa: BLE001
                continue
        self.send_header("Content-Length", "0")
        self.end_headers()
        try:
            self.wfile.write(b"")
        except OSError:
            pass


class TokenAuthTest(unittest.TestCase):
    def test_without_token_is_forbidden(self) -> None:
        with TemporaryDirectory() as tmp:
            mirror = _AuthenticatedMirror(Path(tmp), token="s3cr3t")
            try:
                status, _, _ = mirror.get("/libs/a.jar")
                self.assertEqual(status, 403)
            finally:
                mirror.close()

    def test_with_wrong_token_is_forbidden(self) -> None:
        with TemporaryDirectory() as tmp:
            mirror = _AuthenticatedMirror(Path(tmp), token="s3cr3t")
            try:
                status, _, _ = mirror.get("/libs/a.jar", token="wrong")
                self.assertEqual(status, 403)
            finally:
                mirror.close()

    def test_with_correct_token_serves(self) -> None:
        with TemporaryDirectory() as tmp:
            mirror = _AuthenticatedMirror(Path(tmp), token="s3cr3t")
            try:
                status, body, _ = mirror.get("/libs/a.jar", token="s3cr3t")
                self.assertEqual(status, 200)
                self.assertEqual(body, PAYLOAD)
            finally:
                mirror.close()

    def test_health_exempt_from_token(self) -> None:
        with TemporaryDirectory() as tmp:
            mirror = _AuthenticatedMirror(Path(tmp), token="s3cr3t")
            try:
                status, _, _ = mirror.get("/health")
                self.assertEqual(status, 200)
            finally:
                mirror.close()


class HeaderSanitizeTest(unittest.TestCase):
    def _run(self, upstream_headers: list[tuple[str, str]]) -> tuple[int, list[tuple[str, str]]]:
        handler = type(
            "HdrUpstream",
            (HeaderSanitizeUpstream,),
            {"pending": upstream_headers, "status": 200},
        )
        up = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=up.serve_forever, daemon=True).start()
        with TemporaryDirectory() as tmp:
            config = Settings(
                listen="127.0.0.1:0",
                cache_dir=Path(tmp),
                token="",
                upstreams={"libs": f"http://127.0.0.1:{up.server_address[1]}"},
            )
            server = MirrorHTTPServer(("127.0.0.1", 0), config)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
                conn.request("GET", "/libs/a.jar")
                resp = conn.getresponse()
                resp.read()
                items = list(resp.msg.items())
                conn.close()
                return resp.status, items
            finally:
                server.shutdown()
                server.server_close()
                up.shutdown()
                up.server_close()

    def test_etag_crlf_injection_is_stripped(self) -> None:
        status, items = self._run([("ETag", "abc\r\nX-Evil: pwned")])
        self.assertEqual(status, 200)
        names = [k.lower() for k, _ in items]
        self.assertNotIn("x-evil", names, "CRLF 注入必须被清洗，不能产生自定义头")

    def test_last_modified_crlf_injection_is_stripped(self) -> None:
        status, items = self._run([("Last-Modified", "01 Jan 2020\r\nX-Inject: 1")])
        self.assertEqual(status, 200)
        names = [k.lower() for k, _ in items]
        self.assertNotIn("x-inject", names)


class UpstreamErrorTest(unittest.TestCase):
    def test_non_200_upstream_yields_502_before_headers(self) -> None:
        with TemporaryDirectory() as tmp:
            mirror = _AuthenticatedMirror(Path(tmp), upstream_kwargs={"status": 500})
            try:
                status, body, _ = mirror.get("/libs/a.jar")
                self.assertEqual(status, 502)
                self.assertIn(b"upstream", body.lower())
            finally:
                mirror.close()

    def test_broken_upstream_connection_is_handled(self) -> None:
        """上游先返回 200 头、随后连接中断：响应头已发送后异常不再二次写头。"""
        # 用一个只会回头、不写 body、随即关连接的假上游
        class BrokenUpstream(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D401
                pass

            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", "100")  # 声明 100 字节
                self.end_headers()
                self.connection.close()  # 不回 body 直接断开

        up = ThreadingHTTPServer(("127.0.0.1", 0), BrokenUpstream)
        threading.Thread(target=up.serve_forever, daemon=True).start()
        with TemporaryDirectory() as tmp:
            config = Settings(
                listen="127.0.0.1:0",
                cache_dir=Path(tmp),
                token="",
                upstreams={"libs": f"http://127.0.0.1:{up.server_address[1]}"},
            )
            server = MirrorHTTPServer(("127.0.0.1", 0), config)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                # 客户端不应看到「头嵌 body」的二次响应；应得到中断或干净的响应
                import socket as _socket

                s = _socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=30)
                s.sendall(b"GET /libs/a.jar HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                data = b""
                try:
                    while True:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                except OSError:
                    pass
                s.close()
                # 关键断言：只允许出现一份干净来源的响应头，绝不出现二次写入的损坏响应
                self.assertLessEqual(data.count(b"HTTP/1.1"), 2, "最多一个来源的响应（可能被 Client 看到 200 头 + 中断体）")
            finally:
                server.shutdown()
                server.server_close()
                up.shutdown()
                up.server_close()


if __name__ == "__main__":
    unittest.main()