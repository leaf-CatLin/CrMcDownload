"""版本与更新：自动检测、版本比较、426 门槛、/update 301 与 /api/update 负载。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import pytest

from server_mirror.api import ApiRouter
from server_mirror.db import Database
from server_mirror.releases import (
    ReleaseAsset,
    ReleaseInfo,
    ReleaseProvider,
    ReleaseSnapshot,
)
from server_mirror.update import (
    UpdateInfo,
    is_outdated,
    request_version,
    update_info,
    version_gate,
    version_key,
)


def _snapshot(version: str = "0.9.1", *, min_version: str = "", source: str = "github") -> ReleaseSnapshot:
    tag = f"v{version}"
    latest = ReleaseInfo(
        tag=tag,
        version=version,
        name=f"CrMCDownload {version}",
        url=f"https://github.com/leaf-CatLin/CrMcDownload/releases/tag/{tag}",
        body=f"## CrMCDownload {version}\n\n- 自动检测到的更新日志",
        published_at="2026-11-21T05:25:34Z",
        assets=[
            ReleaseAsset(
                name=f"CrMCDownload-{version}-win64.zip",
                size=54_262_294,
                url=f"https://github.com/leaf-CatLin/CrMcDownload/releases/download/{tag}/CrMCDownload-{version}-win64.zip",
            ),
            ReleaseAsset(
                name=f"CrMCDownload-{version}-win64-setup.exe",
                size=54_142_976,
                url=f"https://github.com/leaf-CatLin/CrMcDownload/releases/download/{tag}/CrMCDownload-{version}-win64-setup.exe",
            ),
        ],
        download_base=f"https://github.com/leaf-CatLin/CrMcDownload/releases/download/{tag}",
    )
    return ReleaseSnapshot(
        latest=latest,
        min_version=min_version,
        releases=[latest],
        fetched_at=time.time(),
        last_attempt=time.time(),
        source=source,
    )


def test_version_key_orders_prerelease_below_release():
    assert version_key("0.7.0") == ((0, 7, 0), (3, ()))
    assert version_key("v0.7.0+build.5") == ((0, 7, 0), (3, ()))
    assert version_key("0.7.0-rc.1") < version_key("0.7.0")
    assert version_key("0.7") == ((0, 7, 0), (3, ()))
    assert version_key("0.10.0") > version_key("0.9.9")
    assert version_key("nightly") is None
    assert version_key("") is None


def test_version_key_puts_reissue_above_release():
    """``0.8.5-r1`` 是 0.8.5 的重发版，要比 0.8.5 新、比 0.9.1 旧。"""
    assert version_key("0.8.5-r1") > version_key("0.8.5")
    assert version_key("0.8.5-r1") < version_key("0.9.1")
    assert version_key("0.9.1-beta.2") < version_key("0.9.1")


def test_is_outdated_is_permissive_on_bad_input():
    assert is_outdated("0.6.2", "0.7.0") is True
    assert is_outdated("0.7.0", "0.7.0") is False
    assert is_outdated("0.7.1", "0.7.0") is False
    assert is_outdated("0.7.0-beta.1", "0.7.0") is True
    assert is_outdated("", "0.7.0") is False
    assert is_outdated("0.6.2", "") is False


def test_update_info_urls_and_payload():
    info = UpdateInfo(latest="0.7.0", minimum="0.7.0")
    assert info.tag_name == "v0.7.0"
    assert info.page_url == "https://github.com/leaf-CatLin/CrMcDownload/releases/tag/v0.7.0"
    assert info.asset_name == "CrMCDownload-0.7.0-win64-setup.exe"
    assert info.direct_url.endswith("/v0.7.0/CrMCDownload-0.7.0-win64-setup.exe")
    assert info.mirror_download_url.startswith("/github/leaf-CatLin/CrMcDownload/releases/download/")
    payload = info.payload("0.6.2")
    assert payload["force_update"] is True and payload["ok"] is False
    assert info.payload("0.7.0")["force_update"] is False


def test_update_info_prefers_detected_github_assets():
    """最新版本与安装包都取自 GitHub 快照，而不是环境变量。"""

    @dataclass
    class _S:
        token: str = ""
        update_latest: str = "0.0.1"
        update_min: str = ""
        update_enforce: bool = True
        update_repo: str = "leaf-CatLin/CrMcDownload"
        update_release_url: str = ""
        update_asset: str = ""

    info = update_info(_S(), snapshot=_snapshot("0.9.1"))
    assert info.latest == "0.9.1"
    assert info.tag_name == "v0.9.1"
    assert info.asset_name == "CrMCDownload-0.9.1-win64-setup.exe"
    assert info.asset_size == 54_142_976
    assert info.direct_url.endswith("/download/v0.9.1/CrMCDownload-0.9.1-win64-setup.exe")
    assert info.source == "github"
    payload = info.payload("0.9.1")
    assert payload["tag"] == "v0.9.1"
    assert payload["version_source"] == "github"
    assert payload["asset"]["size"] == 54_142_976
    assert payload["release_url"].endswith("/releases/tag/v0.9.1")
    # 全部资产都对外暴露，官网可以同时给安装版 / 免安装版
    assert [item["name"] for item in payload["assets"]] == [
        "CrMCDownload-0.9.1-win64.zip",
        "CrMCDownload-0.9.1-win64-setup.exe",
    ]


def test_update_info_falls_back_to_env_when_detection_missing():
    """检测器不可用时仍能工作：用 MIRROR_UPDATE_LATEST_VERSION 兜底。"""

    @dataclass
    class _S:
        token: str = ""
        update_latest: str = "1.2.3"
        update_min: str = "1.0.0"

    info = update_info(_S())
    assert info.latest == "1.2.3"
    assert info.minimum == "1.0.0"
    assert info.source == "env"
    assert info.asset_name == "CrMCDownload-1.2.3-win64-setup.exe"


def test_update_info_reads_settings_defaults():
    from server_mirror.config import Settings

    info = update_info(Settings())
    # 没有任何检测结果时，用内置兜底版本，避免版本接口返回空值
    assert info.latest
    assert info.minimum == ""


def test_min_version_auto_mode_tracks_previous_series():
    provider = ReleaseProvider(repo="leaf-CatLin/CrMcDownload", min_mode="auto", fallback_min="")
    assert provider._min_version("0.9.1") == "0.8.0"
    assert provider._min_version("1.3.4") == "1.2.0"
    assert provider._min_version("nightly") == ""


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
        update_cache_ttl: int = 600
        releases_provider: object = field(default=None)

    db = Database(tmp_path / "mirror.db")
    handle = ApiRouter(settings=_S(), db=db)
    yield handle
    db.close()


@pytest.fixture()
def auto_router(tmp_path):
    """带「自动检测」检测器的路由：最新版本来自 GitHub 快照。"""

    @dataclass
    class _S:
        token: str = ""
        update_latest: str = "0.0.1"
        update_min: str = ""
        update_enforce: bool = True
        update_repo: str = "leaf-CatLin/CrMcDownload"
        update_release_url: str = ""
        update_asset: str = ""
        update_cache_ttl: int = 600
        releases_provider: object = field(default=None)

    settings = _S()
    settings.releases_provider = ReleaseProvider(
        repo="leaf-CatLin/CrMcDownload",
        ttl=600,
        fallback_version="0.0.1",
        fallback_min="0.8.5",
        fetch=lambda: _snapshot("0.9.1", min_version="0.8.5"),
    )
    db = Database(tmp_path / "mirror.db")
    handle = ApiRouter(settings=settings, db=db)
    yield handle
    db.close()


def test_update_redirect_is_301(router):
    status, headers, body = router.dispatch("GET", "/update", {}, None)
    assert status == 301
    assert headers["Location"].endswith("/releases/tag/v0.7.0")
    assert headers["Cache-Control"] == "no-store"
    assert body == b""


def test_update_redirect_follows_detected_release(auto_router):
    """发新版本后 /update 自动跳到新 tag，不需要改配置。"""
    status, headers, _ = auto_router.dispatch("GET", "/update", {}, None)
    assert status == 301
    assert headers["Location"].endswith("/releases/tag/v0.9.1")
    assert headers["X-OffCraft-Latest-Version"] == "0.9.1"


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


def test_api_update_reports_detected_version_and_assets(auto_router):
    _, _, body = auto_router.dispatch("GET", "/api/update", {}, None)
    payload = json.loads(body.decode("utf-8"))
    assert payload["latest_version"] == "0.9.1"
    assert payload["min_version"] == "0.8.5"
    assert payload["tag"] == "v0.9.1"
    assert payload["version_source"] == "github"
    assert payload["asset"]["name"] == "CrMCDownload-0.9.1-win64-setup.exe"
    assert payload["asset"]["size"] == 54_142_976
    assert payload["download_url"].startswith("https://github.com/leaf-CatLin/CrMcDownload/")


def test_api_releases_exposes_changelog(auto_router):
    status, _, body = auto_router.dispatch("GET", "/api/releases?limit=3", {}, None)
    payload = json.loads(body.decode("utf-8"))
    assert status == 200
    assert payload["latest_version"] == "0.9.1"
    assert payload["releases"][0]["tag"] == "v0.9.1"
    assert "自动检测" in payload["releases"][0]["body"]
    assert payload["check"]["source"] == "github"
    assert payload["check"]["stale"] is False


def test_api_list_is_gated_but_update_endpoint_is_not(router):
    status, headers, _ = router.dispatch(
        "GET", "/api/servers/list", {"X-OffCraft-Version": "0.5.0"}, None
    )
    assert status == 426 and headers["X-OffCraft-Force-Update"] == "1"


def test_api_gate_uses_detected_min_version(auto_router):
    """门槛按检测到的最新版本算：0.8.5 放行，0.8.0 拒绝。"""
    status_old, headers, _ = auto_router.dispatch(
        "GET", "/api/stats/overview", {"X-OffCraft-Version": "0.8.0"}, None
    )
    assert status_old == 426
    assert headers["X-OffCraft-Latest-Version"] == "0.9.1"
    status_new, _, _ = auto_router.dispatch(
        "GET", "/api/stats/overview", {"X-OffCraft-Version": "0.9.1"}, None
    )
    assert status_new == 200