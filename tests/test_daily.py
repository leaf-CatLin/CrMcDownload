"""每日一言：解析、缓存与网络失败兜底。"""

import tempfile
import unittest
from pathlib import Path

from offcraft.daily import (
    FALLBACK,
    Sentence,
    cache_path,
    load_cached,
    parse_payload,
    store_cached,
    today_sentence,
)


class _StubDownloader:
    def __init__(self, payload=None, text=b"", *, fail_json=False, fail_text=False):
        self.payload = payload
        self.text = text
        self.fail_json = fail_json
        self.fail_text = fail_text

    def fetch_json(self, url):
        if self.fail_json:
            raise OSError("offline")
        return self.payload

    def fetch_bytes(self, url, *, max_size=0):
        if self.fail_text:
            raise OSError("offline")
        return self.text


class DailySentenceTest(unittest.TestCase):
    def test_parse_json_payload(self):
        sentence = parse_payload(
            {"content": "戏会落幕，人生却不会。", "source": "原神", "author": "云堇"}
        )
        self.assertEqual(sentence.text, "戏会落幕，人生却不会。")
        self.assertEqual(sentence.work, "原神")
        self.assertEqual(sentence.author, "云堇")
        self.assertEqual(sentence.display, "戏会落幕，人生却不会。 —— 云堇《原神》")

    def test_parse_null_author(self):
        sentence = parse_payload(
            {"content": "Heavy clips in my, in my front seat.",
             "source": "Murder In My Mind", "author": None}
        )
        self.assertEqual(sentence.author, "")
        self.assertEqual(sentence.display, "Heavy clips in my, in my front seat. —— 《Murder In My Mind》")

    def test_parse_text_payload(self):
        self.assertEqual(parse_payload("  只此一句  ").text, "只此一句")

    def test_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            sentence = Sentence("句", "作者", "作品")
            path = store_cached(tmp, sentence, day="2026-11-03")
            self.assertEqual(path, cache_path(tmp, "2026-11-03"))
            loaded = load_cached(tmp, day="2026-11-03")
            self.assertEqual(loaded, sentence)
            self.assertIsNone(load_cached(tmp, day="2026-11-04"))

    def test_today_uses_cache_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            cached = Sentence("缓存句")
            store_cached(tmp, cached, day=_today())
            stub = _StubDownloader({"content": "网络句"})
            self.assertEqual(today_sentence(tmp, stub), cached)

    def test_fetch_and_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubDownloader({"content": "新句", "source": "作品", "author": "作者"})
            sentence = today_sentence(tmp, stub)
            self.assertEqual(sentence.text, "新句")
            self.assertEqual(load_cached(tmp), sentence)

    def test_network_failure_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubDownloader(fail_json=True, fail_text=True)
            self.assertEqual(today_sentence(tmp, stub), FALLBACK)
            self.assertIsNone(load_cached(tmp))

    def test_text_endpoint_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubDownloader(
                {"content": ""}, "戏会落幕，人生却不会。 云堇 原神", fail_json=True
            )
            sentence = today_sentence(tmp, stub)
            self.assertEqual(sentence.text, "戏会落幕，人生却不会。")
            self.assertEqual(sentence.author, "云堇")
            self.assertEqual(sentence.work, "原神")


def _today() -> str:
    import time

    return time.strftime("%Y-%m-%d")


if __name__ == "__main__":
    unittest.main()
