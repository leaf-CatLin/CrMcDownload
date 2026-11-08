"""版本与更新：版本比较、426 门槛、/update 301 与 /api/update 负载。"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from server_mirror.api import ApiRouter
from server_mirror.db import Database
from server_mirror.update import (
    UpdateInfo,
    is_outdated,
    request_version,
    update_info,
    version_gate,
    version_key,
)


def test_version_key_orders_prerelease_below_release():
    assert version_key("0.7.0") == ((0, 7, 0), (3, ()))
    assert version_key("v0.7.0+build.5") == ((0, 7, 0), (3, ()))
    assert version_key("0.7.0-rc.1") < version_key("0.7.0")
    assert version_key("0.7") == ((0, 7, 0), (3, ()))
    assert version_key("0.10.0") > version_key("0.9.9")
    assert version_key("nightly") is None
    assert version_key("") is None


def test_is_outdated_is_permissive_on_bad_input():
    assert is_outdated("0.6.2", "0.7.0") is True
    assert is_outdated("0.7.0", "0.7.0") is False
    assert is_outdated("0.7.1", "0.7.0") is False
    assert is_outdated("0.7.0-beta.1", "0.7.0") is True
    assert is_outdated("", "0.7.0") is False
    assert is_outdated("0.6.2", "") is False


def test_update_info_urls_and_payload():
    info = UpdateInfo(latest="0.7.0", minimum="0.7.0")
    assert info.tag == "v0.7.0"
    assert info.page_url == "https://github.com/leaf-CatLin/CrMcDownload/releases/tag/v0.7.0"
    assert info.asset_name == "CrMCDownload-0.7.0-win64-setup.exe"
    assert info.direct_url.endswith("/v0.7.0/CrMCDownload-0.7.0-win64-setup.exe")
    assert info.mirror_url.startswith("/github/leaf-CatLin/CrMcDownload/releases/download/")
    payload = info.payload("0.6.2")
    assert payload["force_update"] is True and payload["ok"] is False
    assert info.payload("0.7.0")["force_update"] is False


def test_request_version_header_before_query():
    assert request_version({"X-OffCraft-Version": "v0.7.0"}, "") == "0.7.0"
    assert request_version({}, "v=0.5.0") == "0.5.0"
    assert request_version({"x-offcraft-version": "0.7.0"}, "v=0.5.0") == "0.7.0"
    assert request_version({}, "") == ""


def test_version_gate_blocks_low_and_allows_missing():
    info = UpdateInfo(minimum="0.7.0")
    status, headers, body = version_gate(info, {"X-OffCraft-Version": "0.6.2"})
    assert status == 426
    assert headers["X-OffCraft-Min-Version"] == "0.7.0"
    payload = json.loads(body.decode("utf-8"))
    assert payload["force_update"] is True
    assert version_gate(info, {}) is None
    assert version_gate(info, {"X-OffCraft-Version": "0.7.0"}) is None


def test_version_gate_respects_enforce_switch():
    info = UpdateInfo(minimum="0.7.0", enforce=False)
    assert version_gate(info, {"X-OffCraft-Version": "0.6.2"}) is None
    assert version_gate(UpdateInfo(minimum=""), {"X-OffCraft-Version": "0.6.2"}) is None


def test_update_info_reads_settings_defaults():
    @dataclass
    class _S:
        token: str = ""

    info = update_info(_S())
    assert info.latest == "0.8.5"
    assert info.minimum == ""


@pytest.fixture()
def router(tmp_path):
    @dataclass
    class _S:
        token: str = ""
        update_latest: str = "0.7.0"
        update_min: str = "0.7.0"
        update_enforce: bool = True
        update_repo: str = "crazying-dev/CrMcDownload"
        update_release_url: str = ""
        update_asset: str = ""

    db = Database(tmp_path / "mirror.db")
    handle = ApiRouter(settings=_S(), db=db)
    yield handle
    db.close()


def test_update_redirect_is_301(router):
    status, headers, body = router.dispatch("GET", "/update", {}, None)
    assert status == 301
    assert headers["Location"].endswith("/releases/tag/v0.7.0")
    assert headers["Cache-Control"] == "no-store"
    assert body == b""


def test_api_update_forces_old_client(router):
    status, _, body = router.dispatch(
        "GET", "/api/update", {"X-OffCraft-Version": "0.5.0"}, None
    )
    payload = json.loads(body.decode("utf-8"))
    assert status == 200
    assert payload["force_update"] is True
    assert payload["latest_version"] == "0.7.0"
    assert payload["mirror_download_url"].startswith("/github/")


def test_api_update_accepts_current_client(router):
    _, _, body = router.dispatch("GET", "/api/update?version=0.7.0", {}, None)
    payload = json.loads(body.decode("utf-8"))
    assert payload["force_update"] is False and payload["ok"] is True


def test_api_list_is_gated_but_update_endpoint_is_not(router):
    status, headers, _ = router.dispatch(
        "GET", "/api/servers/list", {"X-OffCraft-Version": "0.5.0"}, None
    )
    assert status == 426 and headers["X-OffCraft-Force-Update"] == "1"
