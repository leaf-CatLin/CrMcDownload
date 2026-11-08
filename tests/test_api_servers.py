"""``/api`` 分流：榜单 / 详情 / 上报的响应结构、参数校验、令牌与限流。"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from server_mirror.api import ApiRouter
from server_mirror.db import Database


@dataclass
class _Settings:
    token: str = ""


@pytest.fixture()
def router(tmp_path):
    db = Database(tmp_path / "mirror.db")
    handle = ApiRouter(settings=_Settings(), db=db)
    yield handle, db
    db.close()


def _body(result):
    status, headers, payload = result
    return status, headers, json.loads(payload.decode("utf-8"))


def test_handles_only_api_skin_page_prefixes(router):
    handle, _ = router
    assert handle.handles("/api/servers/list")
    assert handle.handles("/skin/Steve.png")
    assert handle.handles("/page/assets/x.js")
    assert not handle.handles("/meta/v2.json")


def test_list_shape_and_caching(router):
    handle, db = router
    db.upsert_server("mc.example", 25565, label="示例服")
    db.record_ping("mc.example", 25565, ok=True, online=4, players_max=10, motd="Hello")
    status, headers, data = _body(handle.dispatch("GET", "/api/servers/list", {}, None))
    assert status == 200
    assert headers["Cache-Control"] == "public, max-age=15"
    assert data["total"] == 1
    hit = data["hits"][0]
    assert hit["host"] == "mc.example"
    assert hit["online"] == 4 and hit["max"] == 10
    assert hit["motd"] == "Hello"
    assert hit["players_visible"] is False        # 无 sample
    assert "hot" in hit and "updated_at" in hit


def test_list_limit_clamped(router):
    handle, _ = router
    _, _, data = _body(handle.dispatch("GET", "/api/servers/list?limit=9999", {}, None))
    assert data["limit"] == 50
    _, _, data = _body(handle.dispatch("GET", "/api/servers/list?limit=abc", {}, None))
    assert data["limit"] == 20                     # 非法回落默认


def test_detail_requires_host(router):
    handle, _ = router
    status, _, data = _body(handle.dispatch("GET", "/api/servers/detail", {}, None))
    assert status == 400 and "host" in data["error"]


def test_detail_rejects_bad_address(router):
    handle, _ = router
    status, _, data = _body(
        handle.dispatch("GET", "/api/servers/detail?host=http%3A%2F%2Fx", {}, None)
    )
    assert status == 400


def test_detail_missing_record_404(router):
    handle, _ = router
    status, _, data = _body(handle.dispatch("GET", "/api/servers/detail?host=ghost.example", {}, None))
    assert status == 404


def test_detail_returns_history_and_players(router):
    handle, db = router
    db.upsert_server("mc.example", 25565)
    db.record_ping("mc.example", 25565, ok=True, players=[{"name": "Alice", "uuid": "u"}])
    status, _, data = _body(
        handle.dispatch("GET", "/api/servers/detail?host=mc.example&port=25565", {}, None)
    )
    assert status == 200
    assert data["players_visible"] is True
    assert data["players"][0]["name"] == "Alice"
    assert isinstance(data["history"], list)


def test_report_requires_token_when_configured(tmp_path):
    db = Database(tmp_path / "mirror.db")
    handle = ApiRouter(settings=_Settings(token="secret"), db=db)
    status, _, data = _body(
        handle.dispatch("POST", "/api/servers/report", {}, json.dumps({"host": "new.example"}).encode())
    )
    assert status == 403
    status, _, _ = _body(
        handle.dispatch(
            "POST", "/api/servers/report",
            {"x-offcraft-token": "secret"},
            json.dumps({"host": "new.example"}).encode(),
        )
    )
    assert status == 200
    assert db.detail("new.example", 25565) is not None
    db.close()


def test_report_rejects_bad_json_and_address(router):
    handle, _ = router
    status, _, _ = _body(handle.dispatch("POST", "/api/servers/report", {}, b"{not json"))
    assert status == 400
    status, _, _ = _body(
        handle.dispatch("POST", "/api/servers/report", {}, json.dumps({"host": ""}).encode())
    )
    assert status == 400


def test_report_rate_limited(router):
    handle, _ = router
    body = json.dumps({"host": "spam.example"}).encode()
    codes = [
        _body(handle.dispatch("POST", "/api/servers/report", {}, body))[0]
        for _ in range(15)
    ]
    assert 429 in codes                 # 令牌桶容量 10，第 11 次起被限


def _complaint_body(**overrides) -> bytes:
    payload = {"host": "bad.example", "port": 25565, "reason": "外挂", "detail": "详情",
               "contact": "a@b.c", "client": "1.0"}
    payload.update(overrides)
    return json.dumps(payload).encode()


def test_complaint_accepts_and_stores(router):
    handle, db = router
    status, _, data = _body(
        handle.dispatch("POST", "/api/servers/complaints", {}, _complaint_body())
    )
    assert status == 200
    assert data["ok"] is True and data["id"] > 0
    items = db.list_complaints()
    assert len(items) == 1
    assert items[0]["host"] == "bad.example"
    assert items[0]["port"] == 25565
    assert items[0]["reason"] == "外挂"
    assert items[0]["status"] == 0


def test_complaint_requires_reason(router):
    handle, db = router
    for body in (
        _complaint_body(reason=""),
        _complaint_body(reason="   "),
        json.dumps({"host": "bad.example"}).encode(),
    ):
        status, _, data = _body(
            handle.dispatch("POST", "/api/servers/complaints", {}, body)
        )
        assert status == 400
        assert "原因" in data["error"]
    assert db.count_complaints() == 0


def test_complaint_rejects_bad_address(router):
    handle, _ = router
    status, _, _ = _body(
        handle.dispatch("POST", "/api/servers/complaints", {}, _complaint_body(host=""))
    )
    assert status == 400
    status, _, _ = _body(
        handle.dispatch("POST", "/api/servers/complaints", {}, _complaint_body(port=70000))
    )
    assert status == 400
    status, _, _ = _body(handle.dispatch("POST", "/api/servers/complaints", {}, b"{not json"))
    assert status == 400


def test_complaint_get_not_allowed(router):
    handle, _ = router
    status, _, _ = _body(handle.dispatch("GET", "/api/servers/complaints", {}, None))
    assert status == 405


def test_complaint_requires_token_when_configured(tmp_path):
    db = Database(tmp_path / "mirror.db")
    handle = ApiRouter(settings=_Settings(token="secret"), db=db)
    try:
        status, _, _ = _body(
            handle.dispatch("POST", "/api/servers/complaints", {}, _complaint_body())
        )
        assert status == 403
        status, _, data = _body(
            handle.dispatch(
                "POST", "/api/servers/complaints",
                {"x-offcraft-token": "secret"}, _complaint_body(),
            )
        )
        assert status == 200 and data["id"] > 0
        assert db.count_complaints() == 1
    finally:
        db.close()


def test_complaint_rate_limited(router):
    handle, _ = router
    codes = [
        _body(handle.dispatch("POST", "/api/servers/complaints", {}, _complaint_body()))[0]
        for _ in range(15)
    ]
    assert 429 in codes                 # 与上报共用令牌桶，容量 10


def test_stats_overview_merges_metrics(tmp_path):
    db = Database(tmp_path / "mirror.db")
    db.upsert_server("mc.example", 25565)
    handle = ApiRouter(
        settings=_Settings(), db=db,
        metrics_snapshot=lambda: {"hits": 7},
        cache_stats=lambda: {"bytes": 100},
    )
    _, _, data = _body(handle.dispatch("GET", "/api/stats/overview", {}, None))
    assert data["metrics"]["hits"] == 7
    assert data["servers"]["servers"] == 1
    _, _, cache = _body(handle.dispatch("GET", "/api/stats/cache", {}, None))
    assert cache["bytes"] == 100
    status, _, _ = _body(handle.dispatch("GET", "/api/stats/unknown", {}, None))
    assert status == 404
    db.close()


def test_unknown_api_path_404(router):
    handle, _ = router
    status, _, _ = _body(handle.dispatch("GET", "/api/nope", {}, None))
    assert status == 404


def test_method_not_allowed(router):
    handle, _ = router
    status, _, _ = _body(handle.dispatch("DELETE", "/api/servers/list", {}, None))
    assert status == 405
