"""联机榜单客户端：手动上报 / 举报（隐私约定：不自动上传）。"""

import unittest

from offcraft import __version__
from offcraft.net.downloader import DownloadError
from offcraft.privacy import CONTACT_EMAIL, SECTIONS, policy_html, policy_text
from offcraft.servers.directory import DirectoryError, ServerDirectory


class _FakeDownloader:
    def __init__(self, error: str = "") -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.error = error

    def request_json(self, url, method="GET", *, body=None, headers=None, max_size=None):
        self.calls.append((url, method, dict(body or {})))
        if self.error:
            raise DownloadError(self.error)
        return {"ok": True, "id": 1}


class DirectoryReportTests(unittest.TestCase):
    def test_report_posts_address_and_label(self) -> None:
        fake = _FakeDownloader()
        directory = ServerDirectory("https://mc.example.com", downloader=fake)
        directory.report("MC.Example.COM:25566", "示例服")
        url, method, body = fake.calls[0]
        self.assertEqual(url, "https://mc.example.com/api/servers/report")
        self.assertEqual(method, "POST")
        self.assertEqual(body, {"host": "mc.example.com", "port": 25566, "label": "示例服"})

    def test_report_without_base_raises(self) -> None:
        with self.assertRaises(DirectoryError):
            ServerDirectory("", downloader=_FakeDownloader()).report("mc.example.com")

    def test_report_bad_address_raises(self) -> None:
        with self.assertRaises(DirectoryError):
            ServerDirectory("https://mc.example.com", downloader=_FakeDownloader()).report("not a server")

    def test_report_failure_raises(self) -> None:
        fake = _FakeDownloader("network down")
        directory = ServerDirectory("https://mc.example.com", downloader=fake)
        with self.assertRaises(DirectoryError):
            directory.report("mc.example.com")

    def test_complain_posts_reason_and_client_version(self) -> None:
        fake = _FakeDownloader()
        directory = ServerDirectory("https://mc.example.com", downloader=fake)
        directory.complain("mc.example.com", 25565, "含违规内容（色情、赌博、诈骗等）", "有诈骗链接", "a@b.c")
        url, method, body = fake.calls[0]
        self.assertEqual(url, "https://mc.example.com/api/servers/complaints")
        self.assertEqual(method, "POST")
        self.assertEqual(body["host"], "mc.example.com")
        self.assertEqual(body["port"], 25565)
        self.assertIn("违规内容", body["reason"])
        self.assertEqual(body["detail"], "有诈骗链接")
        self.assertEqual(body["contact"], "a@b.c")
        self.assertEqual(body["client"], __version__)

    def test_complain_truncates_long_fields(self) -> None:
        fake = _FakeDownloader()
        directory = ServerDirectory("https://mc.example.com", downloader=fake)
        directory.complain("mc.example.com", 25565, "x" * 40, "y" * 800, "z" * 200)
        _, _, body = fake.calls[0]
        self.assertEqual(len(body["reason"]), 24)
        self.assertEqual(len(body["detail"]), 500)
        self.assertEqual(len(body["contact"]), 120)

    def test_complain_failure_raises(self) -> None:
        directory = ServerDirectory("https://mc.example.com", downloader=_FakeDownloader("boom"))
        with self.assertRaises(DirectoryError):
            directory.complain("mc.example.com", 25565, "其他问题")


class PrivacyPolicyTests(unittest.TestCase):
    def test_text_covers_sections_and_promise(self) -> None:
        text = policy_text()
        for title, _ in SECTIONS:
            self.assertIn(title, text)
        self.assertIn("不会主动上传", text)
        self.assertIn(CONTACT_EMAIL, text)

    def test_html_has_list_items(self) -> None:
        html = policy_html()
        self.assertIn("<h2>", html)
        self.assertIn("<ul>", html)
        self.assertIn(CONTACT_EMAIL, html)
