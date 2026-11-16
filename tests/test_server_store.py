"""联机：本地收藏、最近访问与探测缓存。"""

import tempfile
import unittest
from pathlib import Path

from offcraft.servers.ping import ProbeResult
from offcraft.servers.store import RECENT_MAX, ServerEntry, ServerStore, key_of


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class EntryTests(unittest.TestCase):
    def test_key_normalizes_case_and_default_port(self) -> None:
        self.assertEqual(key_of("MC.Example.COM"), "mc.example.com:25565")
        self.assertEqual(ServerEntry.parse("mc.example.com:25566").key, "mc.example.com:25566")

    def test_name_prefers_label(self) -> None:
        self.assertEqual(ServerEntry("a.example.com", 25566, "备注").name, "备注")
        self.assertEqual(ServerEntry("a.example.com", 25566).name, "a.example.com:25566")

    def test_from_json_non_numeric_port_tolerated(self) -> None:
        entry = ServerEntry.from_json({"host": "mc.example.com", "port": "not-a-number"})
        self.assertEqual(entry.host, "mc.example.com")
        self.assertEqual(entry.port, 25565)

    def test_from_json_numeric_port_used(self) -> None:
        entry = ServerEntry.from_json({"host": "mc.example.com", "port": 25566})
        self.assertEqual(entry.port, 25566)


class StoreTests(StoreTestCase):
    def test_add_dedupe_and_rename(self) -> None:
        store = ServerStore(self.dir)
        store.add("MC.Example.com:25565", "示例")
        again = store.add("mc.example.com", "换名")
        self.assertEqual(len(store.list_entries()), 1)
        self.assertEqual(again.label, "换名")
        self.assertEqual(again.port, 25565)

    def test_add_keeps_label_when_second_has_none(self) -> None:
        store = ServerStore(self.dir)
        store.add("mc.example.com", "示例")
        store.add("mc.example.com")
        self.assertEqual(store.list_entries()[0].label, "示例")

    def test_remove_clears_recent_and_cache(self) -> None:
        store = ServerStore(self.dir)
        store.add("mc.example.com", "示例")
        store.touch_recent("mc.example.com")
        store.remember(ProbeResult(host="mc.example.com", ok=True, motd="x"))
        self.assertTrue(store.remove("mc.example.com"))
        self.assertEqual(store.list_entries(), [])
        self.assertEqual(store.list_recent(), [])
        self.assertIsNone(store.cached("mc.example.com"))
        self.assertFalse(store.remove("mc.example.com"))

    def test_persist_across_reload(self) -> None:
        store = ServerStore(self.dir)
        store.add("mc.example.com", "示例")
        store.touch_recent("other.example.com")
        store.remember(ProbeResult(host="mc.example.com", ok=True, motd="标题", online=4))
        reloaded = ServerStore(self.dir)
        self.assertEqual([item.host for item in reloaded.list_entries()], ["mc.example.com"])
        self.assertEqual([item.host for item in reloaded.list_recent()], ["other.example.com"])
        cached = reloaded.cached("mc.example.com")
        self.assertIsNotNone(cached)
        self.assertEqual(cached.motd, "标题")  # type: ignore[union-attr]
        self.assertEqual(cached.online, 4)  # type: ignore[union-attr]

    def test_recent_order_and_limit(self) -> None:
        store = ServerStore(self.dir)
        for index in range(RECENT_MAX + 5):
            store.touch_recent(f"h{index}.example.com")
        recent = store.list_recent()
        self.assertEqual(len(recent), RECENT_MAX)
        self.assertEqual(recent[0].host, f"h{RECENT_MAX + 4}.example.com")

    def test_recent_moves_existing_key_to_front(self) -> None:
        store = ServerStore(self.dir)
        store.touch_recent("a.example.com")
        store.touch_recent("b.example.com")
        store.touch_recent("a.example.com")
        self.assertEqual([item.host for item in store.list_recent()], ["a.example.com", "b.example.com"])

    def test_cache_expiry(self) -> None:
        store = ServerStore(self.dir)
        store.remember(ProbeResult(host="mc.example.com", ok=True))
        self.assertIsNotNone(store.cached("mc.example.com"))
        self.assertIsNone(store.cached("mc.example.com", ttl=0.0001))
        self.assertIsNone(store.cached("nope.example.com"))

    def test_cache_size_capped(self) -> None:
        store = ServerStore(self.dir)
        for index in range(80):
            store.remember(ProbeResult(host=f"h{index}.example.com", ok=True))
        self.assertLessEqual(len(store.cache), 64)

    def test_forget(self) -> None:
        store = ServerStore(self.dir)
        store.remember(ProbeResult(host="mc.example.com", ok=True))
        store.forget("mc.example.com")
        self.assertIsNone(store.cached("mc.example.com"))

    def test_corrupt_file_self_heals(self) -> None:
        (self.dir / "servers.json").write_text("{not json", encoding="utf-8")
        store = ServerStore(self.dir)
        self.assertEqual(store.list_entries(), [])
        self.assertEqual(len(list(self.dir.glob("servers.corrupt-*.json"))), 1)

    def test_wrong_root_type_is_ignored(self) -> None:
        (self.dir / "servers.json").write_text("[1, 2]", encoding="utf-8")
        store = ServerStore(self.dir)
        self.assertEqual(store.list_entries(), [])

    def test_merge_adds_only_new_keys(self) -> None:
        store = ServerStore(self.dir)
        store.add("mc.example.com")
        merged = store.merge(
            [ServerEntry("mc.example.com"), ServerEntry("other.example.com", 25566)]
        )
        self.assertEqual(len(merged), 2)
        self.assertEqual(len(store.list_entries()), 1)  # merge 不写收藏

    def test_find_accepts_user_text(self) -> None:
        store = ServerStore(self.dir)
        store.add("mc.example.com:25566")
        self.assertIsNotNone(store.find("MC.Example.com:25566"))
        self.assertIsNone(store.find("not a host!"))


if __name__ == "__main__":
    unittest.main()
