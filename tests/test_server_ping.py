"""联机：服务器地址、Server List Ping 协议与本地收藏。"""

import json
import socket
import threading
import unittest

from offcraft.servers.ping import (
    ProbeResult,
    build_request,
    encode_varint,
    parse_response,
    ping_server,
    read_varint,
    slp_probe,
    strip_color_codes,
)
from offcraft.servers.ssrf import ServerAddressError, format_host_port, parse_server

RESPONSE = {
    "version": {"name": "Paper 1.20.4", "protocol": 763},
    "players": {
        "max": 100,
        "online": 7,
        "sample": [{"id": "0" * 32, "name": "Alice"}, {"name": "Bob"}],
    },
    "description": {
        "text": "§a主世界§r",
        "extra": [{"text": " · "}, {"text": "生存服", "extra": [{"text": "！"}]}],
    },
    "favicon": "data:image/png;base64,iVBORw0KGgo=",
    "Modified Data Attempt": "login_start:brand:Fabric",
    "enforceSecureProfile": True,
}


def varint(value: int) -> bytes:
    return encode_varint(value)


def frame(payload: bytes) -> bytes:
    """按真实 SLP 帧格式包装：帧长 + packetId(0x00) + 字符串长度 + payload。"""
    inner = b"\x00" + varint(len(payload)) + payload
    return varint(len(inner)) + inner


class FakeServer:
    """本机假服务器：按 SLP 协议回一段 JSON（私网地址需显式放行）。"""

    def __init__(self, payload: dict | bytes | None = RESPONSE, *, framed: bool = True) -> None:
        self.payload = payload
        self.framed = framed
        self.received = bytearray()
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port = self._server.getsockname()[1]
        self._thread: threading.Thread | None = None
        self.error: Exception | None = None

    def __enter__(self) -> "FakeServer":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_args) -> None:
        self._server.close()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        try:
            conn, _ = self._server.accept()
            with conn:
                conn.settimeout(3)
                # 请求一次性发出（Handshake + 空的 Status Request），读一段即可
                chunk = conn.recv(4096)
                if chunk:
                    self.received.extend(chunk)
                if self.payload is None:
                    return
                body = self.payload
                if isinstance(body, dict):
                    body = json.dumps(body).encode("utf-8")
                if self.framed:
                    conn.sendall(frame(body))
                else:
                    conn.sendall(body)
        except Exception as exc:  # noqa: BLE001 - 交给断言方检查
            self.error = exc


class VarIntTests(unittest.TestCase):
    def test_roundtrip_boundaries(self) -> None:
        for value in (0, 1, 127, 128, 255, 300, 16383, 2_097_151, 268_435_455):
            encoded = encode_varint(value)
            decoded, size = read_varint(encoded)
            self.assertEqual(decoded, value, encoded)
            self.assertEqual(size, len(encoded))

    def test_negative_written_as_unsigned_32(self) -> None:
        self.assertEqual(encode_varint(-1), b"\xff\xff\xff\xff\x0f")
        value, size = read_varint(encode_varint(-1))
        self.assertEqual(value, 0xFFFFFFFF)
        self.assertEqual(size, 5)

    def test_read_rejects_overlong_and_truncated(self) -> None:
        with self.assertRaises(ValueError):
            read_varint(b"\x80\x80\x80\x80\x80")
        with self.assertRaises(ValueError):
            read_varint(b"\x80")


class AddressTests(unittest.TestCase):
    def test_default_port_and_case(self) -> None:
        self.assertEqual(parse_server("MC.Example.COM"), ("mc.example.com", 25565))
        self.assertEqual(parse_server("1.2.3.4:25566"), ("1.2.3.4", 25566))
        self.assertEqual(parse_server("[::1]:25565"), ("::1", 25565))

    def test_rejects_bad_input(self) -> None:
        for text in ("", " ", "http://mc.example.com", "mc.example.com:0", "mc.example.com:70000"):
            with self.assertRaises(ServerAddressError, msg=text):
                parse_server(text)
        with self.assertRaises(ServerAddressError):
            parse_server("user@mc.example.com")
        with self.assertRaises(ServerAddressError):
            parse_server("mc.example.com/path")
        with self.assertRaises(ServerAddressError):
            parse_server("-bad.example.com")

    def test_format(self) -> None:
        self.assertEqual(format_host_port("mc.example.com", 25565), "mc.example.com")
        self.assertEqual(format_host_port("mc.example.com", 25566), "mc.example.com:25566")
        self.assertEqual(format_host_port("::1", 25566), "[::1]:25566")

class ProbeTests(unittest.TestCase):
    def test_parses_full_response(self) -> None:
        with FakeServer() as fake:
            result = slp_probe("127.0.0.1", fake.port)
        self.assertIsNone(fake.error, fake.error)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.version_name, "Paper 1.20.4")
        self.assertEqual(result.version_protocol, 763)
        self.assertEqual((result.online, result.max_players), (7, 100))
        self.assertEqual([item.name for item in result.players], ["Alice", "Bob"])
        self.assertEqual(result.motd, "主世界 · 生存服！")
        self.assertEqual(result.latency_level, "green")
        self.assertTrue(result.enforce_secure_profile)
        self.assertEqual(result.brand, "Fabric")
        self.assertTrue(result.modified)
        self.assertEqual(result.favicon[:4], b"\x89PNG")

    def test_handshake_layout(self) -> None:
        payload = build_request("mc.example.com", 25565, "OffCraft")
        length, size = read_varint(payload)
        body = payload[size : size + length]
        packet_id, size1 = read_varint(body)
        protocol_raw, size2 = read_varint(body[size1:])
        host_len, size3 = read_varint(body[size1 + size2 :])
        host = body[size1 + size2 + size3 : size1 + size2 + size3 + host_len]
        port = body[size1 + size2 + size3 + host_len : size1 + size2 + size3 + host_len + 2]
        next_state, _ = read_varint(body[size1 + size2 + size3 + host_len + 2 :])
        self.assertEqual(packet_id, 0)
        self.assertEqual(protocol_raw, 0xFFFFFFFF)  # -1 按无符号 32 位写
        self.assertEqual(host, b"mc.example.com")
        self.assertEqual(port, (25565).to_bytes(2, "big"))
        self.assertEqual(next_state, 1)
        # 状态请求必须是空包，不能把 Login Start（带玩家名）混进去
        self.assertTrue(payload.endswith(b"\x01\x00"))
        self.assertEqual(payload.count(b"OffCraft"), 0)

    def test_empty_and_close(self) -> None:
        with FakeServer(payload=None) as fake:
            result = slp_probe("127.0.0.1", fake.port)
        self.assertFalse(result.ok)
        self.assertIn("关闭", result.error)

    def test_non_json_response(self) -> None:
        with FakeServer(payload=b"HTTP/1.1 200 OK\r\n\r\n") as fake:
            result = slp_probe("127.0.0.1", fake.port)
        self.assertFalse(result.ok)
        self.assertIn("Minecraft", result.error)

    def test_missing_version_field(self) -> None:
        with FakeServer(payload={"players": {}}) as fake:
            result = slp_probe("127.0.0.1", fake.port)
        self.assertFalse(result.ok)
        self.assertIn("version", result.error)

    def test_oversized_length(self) -> None:
        # 假服务器声明一个超大长度前缀（不帧包装），应命中「响应过大」
        huge = varint(99_000_000)
        with FakeServer(payload=huge, framed=False) as fake:
            result = slp_probe("127.0.0.1", fake.port)
        self.assertFalse(result.ok)
        self.assertIn("过大", result.error)

    def test_connection_refused(self) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        result = slp_probe("127.0.0.1", port)
        self.assertFalse(result.ok)
        self.assertTrue(result.error)

    def test_string_description(self) -> None:
        data = {"version": {"name": "1.8"}, "description": "§6经典服"}
        parsed = parse_response(data)
        self.assertEqual(parsed["motd"], "经典服")
        self.assertEqual(parsed["players"], ())
        self.assertFalse(parsed["enforce_secure_profile"])


class SerializationTests(unittest.TestCase):
    def test_roundtrip_keeps_fields(self) -> None:
        original = ProbeResult(
            host="mc.example.com",
            port=25566,
            ok=True,
            latency_ms=42.5,
            motd="hello",
            version_name="1.20.4",
            version_protocol=763,
            online=3,
            max_players=20,
            favicon=b"\x89PNGabc",
            brand="Fabric",
        )
        clone = ProbeResult.from_json(original.to_json())
        self.assertEqual(clone.host, original.host)
        self.assertEqual(clone.port, original.port)
        self.assertEqual(clone.motd, "hello")
        self.assertEqual(clone.latency_ms, 42.5)
        self.assertEqual(clone.favicon, b"\x89PNGabc")
        self.assertEqual(clone.address, "mc.example.com:25566")

    def test_broken_favicon_is_ignored(self) -> None:
        clone = ProbeResult.from_json({"host": "a", "favicon": "!!!"})
        self.assertIsNone(clone.favicon)

    def test_strip_color_codes(self) -> None:
        self.assertEqual(strip_color_codes("§a红§rb"), "红b")


if __name__ == "__main__":
    unittest.main()
