"""版本自动检测与缓存：GitHub Releases、TTL 刷新、退避、快照落盘与离线模式。"""

from __future__ import annotations

import json
import time

import pytest

from server_mirror.releases import (
    HttpGitHubClient,
    ReleaseInfo,
    ReleaseProvider,
    ReleaseSnapshot,
    _snapshot_from_update_payload,
)


class _Clock:
    """可手动推进的时钟，避免测试里 sleep。"""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _api(version: str = "0.9.1", *, prerelease: bool = False, draft: bool = False) -> dict:
    tag = f"v{version}"
    return {
        "tag_name": tag,
        "name": f"CrMCDownload {version}",
        "html_url": f"https://github.com/leaf-CatLin/CrMcDownload/releases/tag/{tag}",
        "body": f"## CrMCDownload {version}\n\n- 本次改动",
        "published_at": "2026-11-21T05:25:34Z",
        "prerelease": prerelease,
        "draft": draft,
        "assets": [
            {
                "name": f"CrMCDownload-{version}-win64-setup.exe",
                "size": 54_142_976,
                "browser_download_url": f"https://github.com/leaf-CatLin/CrMcDownload/releases/download/{tag}/CrMCDownload-{version}-win64-setup.exe",
                "download_count": 3,
            },
            {
                "name": f"CrMCDownload-{version}-win64.zip",
                "size": 54_262_294,
                "browser_download_url": f"https://github.com/leaf-CatLin/CrMcDownload/releases/download/{tag}/CrMCDownload-{version}-win64.zip",
                "download_count": 1,
            },
        ],
    }


class _FakeClient:
    """假 GitHub 客户端：按顺序吐出预设响应，并记录请求次数。"""

    def __init__(self, responses: list[tuple[int, object]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def fetch(self, url, *, etag="", last_modified="", headers=None):
        self.calls.append({"url": url, "etag": etag, "last_modified": last_modified})
        if not self.responses:
            return 0, "连接 GitHub 失败：离线", {}
        return self.responses.pop(0)


def _provider(tmp_path, responses, *, clock=None, **kwargs) -> tuple[ReleaseProvider, _FakeClient]:
    client = _FakeClient(responses)
    provider = ReleaseProvider(
        repo="leaf-CatLin/CrMcDownload",
        cache_file=tmp_path / "releases.json",
        clock=clock or _Clock(),
        client_factory=lambda **_: client,
        **kwargs,
    )
    return provider, client


def test_fetch_picks_latest_stable_release_and_assets(tmp_path):
    provider, _ = _provider(
        tmp_path,
        [(200, [_api("0.8.5"), _api("0.9.1")], {"etag": 'W/"abc"'}),
    ],
    )
    snapshot = provider.refresh()
    assert snapshot.source == "github"
    assert snapshot.version == "0.9.1"
    assert snapshot.tag == "v0.9.1"
    assert snapshot.etag == 'W/"abc"'
    asset = provider.asset_for()
    assert asset is not None
    assert asset.name == "CrMCDownload-0.9.1-win64-setup.exe"
    assert asset.size == 54_142_976
    # 历史按版本倒序，官网更新日志直接可用
    assert [item.version for item in snapshot.releases] == ["0.9.1", "0.8.5"]


def test_prerelease_and_draft_are_skipped(tmp_path):
    provider, _ = _provider(
        tmp_path,
        [(200, [_api("0.9.1"), _api("0.9.2", prerelease=True), _api("0.9.3", draft=True)], {})],
    )
    assert provider.refresh().version == "0.9.1"


def test_include_prerelease_switch(tmp_path):
    provider, _ = _provider(
        tmp_path,
        [(200, [_api("0.9.1"), _api("0.9.2", prerelease=True)], {})],
        include_prerelease=True,
    )
    snapshot = provider.refresh()
    assert snapshot.version == "0.9.2"
    assert snapshot.latest.channel == "beta"


def test_ttl_cache_avoids_second_request(tmp_path):
    clock = _Clock()
    provider, client = _provider(tmp_path, [(200, [_api("0.9.1")], {})], clock=clock, ttl=600)
    provider.current()
    assert len(client.calls) == 1
    clock.advance(120)
    provider.current()  # 仍在 TTL 内：不再请求
    assert len(client.calls) == 1
    clock.advance(600)
    provider.current()  # 过期：再请求一次
    assert len(client.calls) == 2


def test_etag_304_keeps_snapshot_fresh(tmp_path):
    clock = _Clock()
    provider, client = _provider(
        tmp_path,
        [(200, [_api("0.9.1")], {"etag": 'W/"v1"'}), (304, None, {"etag": 'W/"v1"'})],
        clock=clock,
        ttl=60,
    )
    first = provider.current()
    clock.advance(120)
    second = provider.current()
    assert second.version == first.version == "0.9.1"
    assert second.is_fresh(60, clock.now)
    # 第二次请求带上 If-None-Match
    assert client.calls[1]["etag"] == 'W/"v1"'


def test_failure_keeps_old_snapshot_and_backs_off(tmp_path):
    clock = _Clock()
    provider, client = _provider(
        tmp_path,
        [(200, [_api("0.9.1")], {}), (0, "连接 GitHub 失败：超时", {})],
        clock=clock,
        ttl=60,
        retry_min=30.0,
        retry_max=120.0,
    )
    provider.current()
    clock.advance(120)
    stale = provider.current()
    # 旧版本继续对外服务，但标记了错误与退避时间
    assert stale.version == "0.9.1"
    assert stale.error.startswith("连接 GitHub 失败")
    assert stale.fails == 1
    assert stale.retry_at > clock.now
    requests_before = len(client.calls)
    clock.advance(5)  # 退避窗口内：不再打网络
    provider.current()
    assert len(client.calls) == requests_before


def test_not_found_caches_empty_snapshot(tmp_path):
    provider, client = _provider(tmp_path, [(404, "Not Found", {})], ttl=60)
    snapshot = provider.current()
    assert snapshot.latest is None
    assert snapshot.version == ""
    assert "暂无已发布版本" in snapshot.error
    provider.current()
    assert len(client.calls) == 1  # 404 是确定结论，缓存住不再请求


def test_snapshot_survives_restart(tmp_path):
    clock = _Clock()
    path = tmp_path / "releases.json"
    first = ReleaseProvider(
        repo="leaf-CatLin/CrMcDownload",
        cache_file=path,
        clock=clock,
        client_factory=lambda **_: _FakeClient([(200, [_api("0.9.1")], {})]),
    )
    first.refresh()
    assert path.exists()

    restored = ReleaseProvider(repo="leaf-CatLin/CrMcDownload", cache_file=path, clock=clock, ttl=600)
    snapshot = restored.snapshot()
    assert snapshot.version == "0.9.1"
    assert snapshot.source == "github"
    assert snapshot.is_fresh(600, clock.now)
    assert [asset.name for asset in snapshot.assets][0].endswith("setup.exe")


def test_corrupt_snapshot_file_is_ignored(tmp_path):
    path = tmp_path / "releases.json"
    path.write_text("{not json", encoding="utf-8")
    provider = ReleaseProvider(
        repo="leaf-CatLin/CrMcDownload", cache_file=path, fallback_version="1.0.0"
    )
    assert provider.snapshot().source == "fallback"
    assert provider.snapshot().version == "1.0.0"


def test_schema_mismatch_is_discarded(tmp_path):
    path = tmp_path / "releases.json"
    path.write_text(json.dumps({"schema": 999, "latest": {"tag": "v9.9.9"}}), encoding="utf-8")
    provider = ReleaseProvider(
        repo="leaf-CatLin/CrMcDownload", cache_file=path, fallback_version="1.0.0"
    )
    assert provider.snapshot().version == "1.0.0"


def test_offline_mode_never_calls_network(tmp_path):
    client = _FakeClient([(200, [_api("9.9.9")], {})])
    provider = ReleaseProvider(
        repo="leaf-CatLin/CrMcDownload",
        cache_file=tmp_path / "releases.json",
        fallback_version="0.9.1",
        offline=True,
        client_factory=lambda **_: client,
    )
    snapshot = provider.current(refresh=True)
    assert client.calls == []
    assert snapshot.version == "0.9.1"
    assert snapshot.source == "fallback"


def test_upstream_bootstrap_when_github_unreachable(tmp_path):
    """GitHub 不可达且没有本地快照时，回落到另一个镜像的 /api/update。"""
    payload = {
        "latest_version": "0.9.1",
        "min_version": "0.8.5",
        "tag": "v0.9.1",
        "release_url": "https://github.com/leaf-CatLin/CrMcDownload/releases/tag/v0.9.1",
        "download_url": "https://github.com/leaf-CatLin/CrMcDownload/releases/download/v0.9.1/CrMCDownload-0.9.1-win64-setup.exe",
        "asset": {"name": "CrMCDownload-0.9.1-win64-setup.exe", "size": 54_142_976},
        "notes": "## CrMCDownload 0.9.1",
    }
    provider = ReleaseProvider(
        repo="leaf-CatLin/CrMcDownload",
        cache_file=tmp_path / "releases.json",
        upstream_base="https://mirror.example.com",
        client_factory=lambda **_: _FakeClient(
            [(0, "连接 GitHub 失败：超时", {}), (200, payload, {})]
        ),
    )
    snapshot = provider.refresh()
    assert snapshot.source == "upstream"
    assert snapshot.version == "0.9.1"
    assert snapshot.min_version == "0.8.5"
    assert snapshot.latest.assets[0].size == 54_142_976


def test_custom_asset_template_wins(tmp_path):
    provider, _ = _provider(
        tmp_path,
        [(200, [_api("0.9.1")], {})],
        asset_hint="CrMCDownload-{version}-win64.zip",
    )
    provider.refresh()
    asset = provider.asset_for()
    assert asset is not None and asset.name == "CrMCDownload-0.9.1-win64.zip"


def test_custom_release_url_template(tmp_path):
    provider, _ = _provider(
        tmp_path,
        [(200, [_api("0.9.1")], {})],
        release_url="https://mc.yjlt.top/page/changelog#v{version}",
    )
    snapshot = provider.refresh()
    assert snapshot.latest.url == "https://mc.yjlt.top/page/changelog#v0.9.1"


def test_snapshot_as_dict_reports_freshness(tmp_path):
    clock = _Clock()
    provider, _ = _provider(tmp_path, [(200, [_api("0.9.1")], {})], clock=clock, ttl=600)
    provider.refresh()
    clock.advance(900)
    data = provider.snapshot().as_dict(ttl=600, now=clock.now, stale=True)
    assert data["schema"] == 1
    assert data["latest"]["version"] == "0.9.1"
    assert data["age"] == pytest.approx(900.0)
    assert data["stale"] is True
    assert data["source"] == "github"


def test_release_info_asset_ranking_prefers_installer():
    info = ReleaseInfo.from_api(_api("0.9.1"), repo="leaf-CatLin/CrMcDownload")
    assert info is not None
    primary = info.pick_asset()
    assert primary is not None and "setup" in primary.name
    assert info.download_base.endswith("/releases/download/v0.9.1")


def test_update_payload_bootstrap_parses_asset_block():
    snapshot = _snapshot_from_update_payload(
        {
            "latest_version": "0.9.1",
            "min_version": "0.8.5",
            "asset": {"name": "CrMCDownload-0.9.1-win64-setup.exe", "size": 54_142_976},
            "notes": "变更摘要",
        },
        repo="leaf-CatLin/CrMcDownload",
    )
    assert snapshot is not None
    assert snapshot.source == "upstream"
    assert snapshot.min_version == "0.8.5"
    assert snapshot.latest.assets[0].name.endswith("setup.exe")
    assert snapshot.latest.assets[0].size == 54_142_976


def test_github_client_sets_conditional_headers():
    client = HttpGitHubClient(proxy="", token="tkn", timeout=5)
    headers = client.headers(etag='W/"abc"')
    assert headers["Authorization"] == "Bearer tkn"
    assert headers["If-None-Match"] == 'W/"abc"'
    assert headers["Accept"] == "application/vnd.github+json"
    assert client.headers(last_modified="Mon, 01 Jan 2024 00:00:00 GMT")["If-Modified-Since"]


def test_releases_url_uses_configured_repo(tmp_path):
    provider = ReleaseProvider(repo="some-owner/some-repo", cache_file=tmp_path / "r.json")
    assert provider._releases_url() == (
        "https://api.github.com/repos/some-owner/some-repo/releases?per_page=10"
    )


def test_missing_repo_degrades_to_fallback(tmp_path):
    provider = ReleaseProvider(repo="", cache_file=tmp_path / "r.json", fallback_version="2.0.0")
    snapshot = provider.current(refresh=True)
    assert snapshot.version == "2.0.0"
    assert snapshot.source == "fallback"


def test_no_snapshot_and_no_fallback_is_empty(tmp_path):
    provider = ReleaseProvider(repo="leaf-CatLin/CrMcDownload", cache_file=tmp_path / "r.json")
    snapshot = provider.snapshot()
    assert snapshot.latest is None
    assert snapshot.version == ""


def test_refresh_async_does_not_block(tmp_path):
    provider, client = _provider(tmp_path, [(200, [_api("0.9.1")], {})], ttl=0)
    provider.refresh_async()
    deadline = time.time() + 5
    while time.time() < deadline and provider.snapshot().version != "0.9.1":
        time.sleep(0.02)
    assert provider.snapshot().version == "0.9.1"
    assert len(client.calls) == 1


def test_release_for_and_asset_for_specific_version(tmp_path):
    provider, _ = _provider(tmp_path, [(200, [_api("0.8.5"), _api("0.9.1")], {})])
    provider.refresh()
    old = provider.release_for("0.8.5")
    assert old is not None and old.tag == "v0.8.5"
    assert provider.asset_for("0.8.5").name == "CrMCDownload-0.8.5-win64-setup.exe"
    assert provider.release_for("1.0.0") is None
    assert provider.asset_for("1.0.0") is None