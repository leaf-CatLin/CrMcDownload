"""热度榜数据层：schema 迁移、成员维护、探测入库与排序。

用 pytest fixture（``tmp_path``）建临时库；禁网、纯 SQLite。
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from server_mirror.db import SCHEMA_VERSION, Database


@pytest.fixture()
def db(tmp_path):
    handle = Database(tmp_path / "mirror.db")
    try:
        yield handle
    finally:
        handle.close()


def test_schema_initialized(db):
    row = db._conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    assert int(row["version"]) == SCHEMA_VERSION
    assert db.count_servers() == 0


def test_reopen_is_idempotent(tmp_path):
    first = Database(tmp_path / "mirror.db")
    first.upsert_server("mc.example", 25565, label="示例")
    first.close()
    second = Database(tmp_path / "mirror.db")
    assert second.count_servers() == 1
    assert second._conn.execute("SELECT version FROM schema_version").fetchone()["version"] == SCHEMA_VERSION
    second.close()


def test_upsert_dedups_and_merges_label(db):
    db.upsert_server("MC.Example", 25565, source="builtin")
    db.upsert_server("mc.example", 25565, label="补标签")
    assert db.count_servers() == 1
    rows = db.list_enabled()
    assert rows[0]["label"] == "补标签"
    assert rows[0]["host"] == "mc.example"       # 归一小写


def test_report_inserts_then_increments(db):
    db.report("new.example", 25565, label="新服")
    assert db.count_servers() == 1
    row = db.detail("new.example", 25565)
    assert row["source"] == "user"
    assert int(row["reports"]) == 1
    db.report("new.example", 25565)
    assert int(db.detail("new.example", 25565)["reports"]) == 2


def test_set_enabled_excludes_from_listing(db):
    db.upsert_server("off.example", 25565)
    db.set_enabled("off.example", 25565, False)
    assert db.list_enabled() == []
    entries, total = db.rank()
    assert total == 0 and entries == []


def test_record_ping_success_clears_fail_and_updates_seen(db):
    def fail_count() -> int:
        return int(db._conn.execute(
            "SELECT fail_count FROM servers_state WHERE host='mc.example' AND port=25565"
        ).fetchone()["fail_count"])

    db.upsert_server("mc.example", 25565)
    db.record_ping("mc.example", 25565, ok=False, error="超时")
    assert fail_count() == 1
    before = db.detail("mc.example", 25565)["last_seen"]
    db.record_ping("mc.example", 25565, ok=True, online=5, players_max=20, latency_ms=40.0)
    after = db.detail("mc.example", 25565)
    assert fail_count() == 0                 # 一次成功即清零失败计数
    assert after["last_seen"] >= before      # 成功刷新 last_seen
    assert after["online"] == 5


def test_record_ping_keeps_old_favicon_when_null(db):
    db.record_ping("mc.example", 25565, ok=True, favicon=b"\x89PNG-favicon")
    db.record_ping("mc.example", 25565, ok=True, favicon=None)
    stored = db._conn.execute(
        "SELECT favicon FROM servers_state WHERE host='mc.example'"
    ).fetchone()["favicon"]
    assert stored == b"\x89PNG-favicon"


def test_rank_orders_by_hot_then_online(db):
    now = time.time()
    db.upsert_server("quiet.example", 25565)
    db.upsert_server("busy.example", 25565)
    db.upsert_server("reported.example", 25565)
    db.report("reported.example", 25565)          # reports 抬升热度
    db.record_ping("quiet.example", 25565, ok=True, online=1)
    db.record_ping("busy.example", 25565, ok=True, online=1000, ts=now)   # 平均在线主导
    entries, total = db.rank()
    assert total == 3
    assert entries[0]["host"] == "busy.example"       # 在线人数拉高热度居首
    assert entries[1]["host"] == "reported.example"   # 有上报，胜过无热度的 quiet
    assert entries[2]["host"] == "quiet.example"


def test_rank_search_matches_host_label_motd(db):
    db.upsert_server("alpha.example", 25565, label="生存服")
    db.upsert_server("beta.example", 25565, label="空岛")
    db.record_ping("beta.example", 25565, ok=True, motd="欢迎来到空岛世界")
    entries, total = db.rank("空岛")
    assert total == 1 and entries[0]["host"] == "beta.example"
    entries, total = db.rank("alpha")
    assert total == 1 and entries[0]["host"] == "alpha.example"


def test_rank_pagination(db):
    for index in range(5):
        db.upsert_server(f"s{index}.example", 25565)
    page, total = db.rank(limit=2, offset=4)
    assert total == 5
    assert len(page) == 1


def test_detail_history_is_capped_at_24(db):
    db.upsert_server("mc.example", 25565)
    base = time.time() - 100
    for index in range(30):
        db.record_ping("mc.example", 25565, ok=True, online=index, ts=base + index)
    detail = db.detail("mc.example", 25565)
    assert len(detail["history"]) == 24
    assert detail["history"][-1]["online"] >= detail["history"][0]["online"]   # 时间正序


def test_overview_counts_online_and_players(db):
    db.upsert_server("a.example", 25565)
    db.upsert_server("b.example", 25565)
    db.record_ping("a.example", 25565, ok=True, online=3)
    db.record_ping("b.example", 25565, ok=False, error="down")
    overview = db.overview()
    assert overview["servers"] == 2
    assert overview["online_servers"] == 1
    assert overview["players"] == 3


def test_concurrent_writes_are_serialized(db):
    def worker(start: int) -> None:
        for index in range(start, start + 20):
            db.record_ping("load.example", 25565, ok=True, online=index)

    threads = [threading.Thread(target=worker, args=(offset,)) for offset in (0, 100, 200, 300)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    count = db._conn.execute("SELECT COUNT(*) c FROM ping_history WHERE host='load.example'").fetchone()["c"]
    assert count == 80


def test_prune_history_removes_old(db):
    db.upsert_server("mc.example", 25565)
    old = time.time() - 40 * 86400
    db.record_ping("mc.example", 25565, ok=True, ts=old)
    db.record_ping("mc.example", 25565, ok=True, ts=time.time())
    removed = db.prune_history(keep_days=7)
    assert removed == 1


def test_schema_upgrade_creates_complaints_table(tmp_path):
    """老库（v2）重新打开后升到 v3 并建出 ``server_complaints``。"""
    path = tmp_path / "mirror.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER NOT NULL);"
        "INSERT INTO schema_version(version) VALUES (2);"
    )
    conn.commit()
    conn.close()
    db = Database(path)
    try:
        assert db._conn.execute("SELECT version FROM schema_version").fetchone()["version"] == SCHEMA_VERSION
        assert db._table_exists("server_complaints")
        assert db.count_complaints() == 0
    finally:
        db.close()


def test_add_and_list_complaints(db):
    first = db.add_complaint(
        "MC.Example", 25565, "  外挂服  ", detail="详情", contact="a@b.c", client="1.0"
    )
    second = db.add_complaint("mc.example", 25566, "诈骗")
    assert first >= 1 and second > first
    assert db.count_complaints() == 2
    items = db.list_complaints()
    assert len(items) == 2
    assert items[0]["id"] == second                  # 新的在前
    row = next(item for item in items if item["id"] == first)
    assert row["host"] == "mc.example"               # 归一小写
    assert row["port"] == 25565
    assert row["reason"] == "外挂服"
    assert row["detail"] == "详情"
    assert row["contact"] == "a@b.c"
    assert row["client"] == "1.0"
    assert row["status"] == 0
    assert row["created_at"] > 0


def test_complaint_truncates_and_limits(db):
    long_reason = "原" * 40
    db.add_complaint("mc.example", 25565, long_reason, detail="d" * 600)
    assert db.list_complaints(limit=10)[0]["reason"] == "原" * 24
    assert len(db.list_complaints(limit=10)[0]["detail"]) == 500
    assert db.list_complaints(limit=0) == []


def test_set_complaint_status_records_handler(db):
    complaint_id = db.add_complaint("mc.example", 25565, "广告")
    assert db.set_complaint_status(complaint_id, 1, "admin") == 1
    row = db.list_complaints(status=1)[0]
    assert row["status"] == 1
    assert row["handled_by"] == "admin"
    assert row["handled_at"] > 0
    assert db.count_complaints(status=0) == 0
    assert db.count_complaints(status=1) == 1
    assert db.set_complaint_status(9999, 2, "admin") == 0   # 不存在的 id
