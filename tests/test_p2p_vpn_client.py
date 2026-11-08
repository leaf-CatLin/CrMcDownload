"""vpnd 本地 IPC 客户端：VIP 校验、密钥派生、配置编码、握手与降级。"""

import json
import tempfile
import unittest
from pathlib import Path

from offcraft.p2p.crypto import derive_room_keys
from offcraft.p2p.room import PeerInfo
from offcraft.p2p.vip import (
    VIP_HOST,
    derive_peer_vip,
    display_vip,
    is_vip,
    vip_number,
)
from offcraft.p2p.vpn_client import (
    VpnClient,
    VpnClientError,
    VpnStatus,
    build_config,
    derive_peer_keys,
    try_create,
)

DEVICE = "ab" * 16
KEYS = derive_room_keys("secret", "room9")


class _FakeStream:
    """替身管道：按顺序吐出预置回复，记录写入内容。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.writes = []
        self.closed = False

    def write(self, text):
        self.writes.append(text)

    def flush(self):
        pass

    def readline(self):
        if not self.replies:
            return ""
        item = self.replies.pop(0)
        if item is None or item == "":
            return ""
        return json.dumps(item) + "\n"

    def close(self):
        self.closed = True


def _client(tmp, replies):
    token = Path(tmp) / "ipc_token.txt"
    token.write_text("a" * 64, encoding="ascii")
    stream = _FakeStream(replies)
    return VpnClient(token_path=token, stream_factory=lambda _path: stream), stream


class VipTest(unittest.TestCase):
    def test_valid_and_invalid(self):
        self.assertTrue(is_vip("10.66.0.1"))
        self.assertTrue(is_vip("10.66.0.254"))
        self.assertFalse(is_vip("10.66.0.0"))
        self.assertFalse(is_vip("10.66.0.255"))
        self.assertFalse(is_vip("10.66.1.5"))
        self.assertFalse(is_vip("192.168.1.8"))
        self.assertFalse(is_vip(""))
        self.assertFalse(is_vip("10.66.0.abc"))

    def test_number_and_display(self):
        self.assertEqual(vip_number("10.66.0.7"), 7)
        self.assertIsNone(vip_number("nope"))
        self.assertEqual(display_vip("10.66.0.2"), "10.66.0.2")
        self.assertEqual(display_vip(""), "—")
        self.assertEqual(display_vip("bad", fallback="未分配"), "未分配")
        self.assertEqual(VIP_HOST, "10.66.0.1")

    def test_derive_peer_vip_stable_and_in_range(self):
        first = derive_peer_vip(KEYS.auth_key, DEVICE)
        self.assertEqual(first, derive_peer_vip(KEYS.auth_key, DEVICE))
        self.assertTrue(is_vip(first))
        self.assertGreaterEqual(vip_number(first), 2)


class PeerKeysTest(unittest.TestCase):
    def test_host_and_joiner_are_symmetric(self):
        host_tx, host_rx = derive_peer_keys(KEYS, is_host=True, self_sid="a" * 32, peer_sid="b" * 32)
        join_tx, join_rx = derive_peer_keys(KEYS, is_host=False, self_sid="b" * 32, peer_sid="a" * 32)
        self.assertEqual(host_tx, join_rx)
        self.assertEqual(host_rx, join_tx)
        self.assertEqual(len(host_tx), 32)
        self.assertEqual(len(host_rx), 32)

    def test_keys_are_per_peer_and_stable(self):
        one = derive_peer_keys(KEYS, is_host=True, self_sid="a" * 32, peer_sid="b" * 32)
        two = derive_peer_keys(KEYS, is_host=True, self_sid="a" * 32, peer_sid="c" * 32)
        self.assertNotEqual(one[0], two[0])
        self.assertEqual(one, derive_peer_keys(KEYS, is_host=True, self_sid="a" * 32, peer_sid="b" * 32))

    def test_missing_sid_rejected(self):
        with self.assertRaises(VpnClientError):
            derive_peer_keys(KEYS, is_host=True, self_sid="", peer_sid="b" * 32)
        with self.assertRaises(VpnClientError):
            derive_peer_keys(KEYS, is_host=True, self_sid="a" * 32, peer_sid="")


class BuildConfigTest(unittest.TestCase):
    def test_encodes_keys_and_edges(self):
        tx, rx = derive_peer_keys(KEYS, is_host=True, self_sid="a" * 32, peer_sid="b" * 32)
        config = build_config(
            vip=VIP_HOST,
            peers=[{"sid": "b" * 32, "vip": "10.66.0.2",
                    "endpoint": "1.2.3.4:5000", "tx_key": tx, "rx_key": rx}],
            edge={"host": "mc.example.com", "port": 25578, "token": "tok"},
        )
        self.assertEqual(config["vip"], VIP_HOST)
        self.assertEqual(config["listen"], 0)
        self.assertTrue(config["use_adapter"])
        peer = config["peers"][0]
        self.assertEqual(len(bytes.fromhex(peer["tx_key"])), 32)
        self.assertEqual(len(bytes.fromhex(peer["rx_key"])), 32)
        self.assertEqual(peer["endpoint"], "1.2.3.4:5000")
        self.assertEqual(config["edge"]["port"], 25578)
        self.assertEqual(config["edge"]["token"], "tok")

    def test_keeps_peers_without_endpoint_for_relay(self):
        """未登记 UDP 端点的对端也要下发：vpnd 见空端点直接走边缘中继。"""
        config = build_config(vip=VIP_HOST, peers=[
            {"sid": "b" * 32, "vip": "10.66.0.2", "endpoint": "",
             "tx_key": b"x" * 32, "rx_key": b"y" * 32},
        ])
        self.assertEqual(len(config["peers"]), 1)
        self.assertEqual(config["peers"][0]["endpoint"], "")

    def test_rejects_bad_key_length(self):
        with self.assertRaises(VpnClientError):
            build_config(vip=VIP_HOST, peers=[
                {"sid": "b" * 32, "vip": "10.66.0.2", "endpoint": "1.2.3.4:5",
                 "tx_key": b"short", "rx_key": b"y" * 32},
            ])

    def test_requires_vip(self):
        with self.assertRaises(VpnClientError):
            build_config(vip="", peers=[])


class IpcClientTest(unittest.TestCase):
    def test_handshake_and_status(self):
        reply = {"ok": True, "running": True, "state": "running", "vip": VIP_HOST,
                 "adapter": True, "adapter_name": "CrMcDownload VPN", "udp_port": 5000,
                 "last_error": "", "uptime": 3,
                 "peers": [{"sid": "b" * 32, "vip": "10.66.0.2", "endpoint": "1.2.3.4:5000",
                            "path": "direct", "tx": 4, "rx": 5, "last_rx": 1}]}
        with tempfile.TemporaryDirectory() as tmp:
            client, stream = _client(tmp, [{"ok": True, "service": "vpnd"}, reply])
            status = client.status()
            self.assertEqual(stream.writes[0], "AUTH " + "a" * 64 + "\n")
            self.assertEqual(json.loads(stream.writes[1])["cmd"], "status")
            self.assertIsInstance(status, VpnStatus)
            self.assertTrue(status.running)
            self.assertEqual(status.vip, VIP_HOST)
            self.assertEqual(status.peers[0].path, "direct")
            self.assertEqual(status.peers[0].rx, 5)
            client.close()
            self.assertTrue(stream.closed)

    def test_apply_config_sends_all_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, stream = _client(tmp, [
                {"ok": True, "service": "vpnd"},
                {"ok": True, "vip": VIP_HOST, "udp_port": 5000, "adapter": True},
            ])
            tx, rx = derive_peer_keys(KEYS, is_host=True, self_sid="a" * 32, peer_sid="b" * 32)
            config = build_config(vip=VIP_HOST, peers=[
                {"sid": "b" * 32, "vip": "10.66.0.2", "endpoint": "1.2.3.4:5000",
                 "tx_key": tx, "rx_key": rx},
            ])
            reply = client.apply_config(config)
            payload = json.loads(stream.writes[1])
            self.assertEqual(payload["cmd"], "config")
            self.assertEqual(payload["vip"], VIP_HOST)
            self.assertEqual(len(payload["peers"]), 1)
            self.assertTrue(reply["ok"])

    def test_update_edge_sends_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, stream = _client(tmp, [{"ok": True}, {"ok": True}])
            client.update_edge({"host": "mc.example.com", "port": 25578, "token": "tok"})
            payload = json.loads(stream.writes[1])
            self.assertEqual(payload["cmd"], "edge")
            self.assertEqual(payload["host"], "mc.example.com")
            self.assertEqual(payload["port"], 25578)
            self.assertEqual(payload["token"], "tok")

    def test_error_reply_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, _stream = _client(tmp, [
                {"ok": True, "service": "vpnd"},
                {"ok": False, "error": "missing-vip"},
            ])
            with self.assertRaises(VpnClientError) as ctx:
                client.apply_config({"vip": "", "peers": []})
            self.assertIn("missing-vip", str(ctx.exception))

    def test_closed_pipe_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, _stream = _client(tmp, [{"ok": True}, ""])
            with self.assertRaises(VpnClientError):
                client.status()

    def test_missing_token_degrades(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = VpnClient(token_path=Path(tmp) / "missing.txt")
            self.assertFalse(client.available())
            self.assertIsNone(try_create(token_path=Path(tmp) / "missing.txt"))
            with self.assertRaises(VpnClientError):
                client.connect()

    def test_try_create_available_when_token_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "ipc_token.txt"
            token.write_text("b" * 64, encoding="ascii")
            client = try_create(token_path=token)
            self.assertIsNotNone(client)


class RosterVipTest(unittest.TestCase):
    def test_peer_info_parses_vip_and_udp(self):
        peer = PeerInfo.from_roster({"pid": "a" * 32, "addr": "1.2.3.4:5",
                                     "vip": "10.66.0.2", "udp": "5.6.7.8:9999"})
        self.assertEqual(peer.vip, "10.66.0.2")
        self.assertEqual(peer.udp, "5.6.7.8:9999")


if __name__ == "__main__":
    unittest.main()
