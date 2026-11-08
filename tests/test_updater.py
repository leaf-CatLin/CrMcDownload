"""自更新：响应归一化、直连/镜像选源与断点续传参数。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from offcraft.net import updater
from offcraft.net.downloader import Downloader
from offcraft.net.updater import (
    ReleaseInfo,
    UpdatePlan,
    plan_download,
    read_release,
    scan_installer_name,
)


def _mirror_payload(force: bool = False) -> dict:
    return {
        "latest_version": "0.7.0",
        "min_version": "0.7.0",
        "force_update": force,
        "release_url": "https://github.com/crazying-dev/CrMcDownload/releases/tag/v0.7.0",
        "download_url": "https://github.com/example/setup.exe",
        "mirror_download_url": "/github/crazying-dev/CrMcDownload/releases/download/v0.7.0/setup.exe",
        "asset": {"name": "setup.exe"},
    }


def test_read_release_from_mirror_payload():
    info = read_release(_mirror_payload(True), base="https://mc.example.com")
    assert info.version == "0.7.0"
    assert info.min_version == "0.7.0"
    assert info.force is True
    assert info.mirror_url == (
        "https://mc.example.com/github/crazying-dev/CrMcDownload/"
        "releases/download/v0.7.0/setup.exe"
    )


def test_read_release_keeps_absolute_mirror_url():
    payload = _mirror_payload()
    payload["mirror_download_url"] = "https://cdn.example.com/setup.exe"
    info = read_release(payload, base="https://mc.example.com")
    assert info.mirror_url == "https://cdn.example.com/setup.exe"


def test_read_release_from_github_api():
    payload = {
        "tag_name": "v0.7.0",
        "html_url": "https://github.com/x/y/releases/tag/v0.7.0",
        "assets": [{"name": "a.exe", "browser_download_url": "https://github.com/a.exe"}],
    }
    info = read_release(payload)
    assert info.version == "0.7.0"
    assert info.download_url == "https://github.com/a.exe"
    assert info.asset_name == "a.exe"


def test_plan_uses_direct_when_fast():
    calls: list[str] = []

    def fake_probe(url, **kwargs):
        calls.append(url)
        return 0.2

    release = ReleaseInfo("0.7.0", download_url="https://github.com/a.exe", mirror_url="https://m/a.exe")
    plan = plan_download(release, probe=fake_probe)
    assert plan.source == "direct"
    assert plan.dns_flushed is False
    assert calls == ["https://github.com/a.exe"]


def test_plan_flushes_dns_then_recovers():
    probes = [1.8, 0.4]
    flushed: list[bool] = []

    def fake_probe(url, **kwargs):
        return probes.pop(0)

    def fake_flush():
        flushed.append(True)
        return True

    release = ReleaseInfo("0.7.0", download_url="https://github.com/a.exe", mirror_url="https://m/a.exe")
    plan = plan_download(release, probe=fake_probe, flush=fake_flush, sleep=lambda _s: None)
    assert flushed == [True]
    assert plan.source == "direct"
    assert plan.dns_flushed is True
    assert plan.retest_ms == 0.4


def test_plan_falls_back_to_mirror_when_still_slow():
    def fake_probe(url, **kwargs):
        return 2.0

    release = ReleaseInfo("0.7.0", download_url="https://github.com/a.exe", mirror_url="https://m/a.exe")
    plan = plan_download(release, probe=fake_probe, flush=lambda: True, sleep=lambda _s: None)
    assert plan.source == "mirror"
    assert plan.url == "https://m/a.exe"


def test_plan_falls_back_to_mirror_when_direct_unreachable():
    release = ReleaseInfo("0.7.0", download_url="https://github.com/a.exe", mirror_url="https://m/a.exe")
    plan = plan_download(release, probe=lambda *_a, **_k: None, flush=lambda: False)
    assert plan.source == "mirror"
    assert plan.dns_flushed is False


def test_plan_without_mirror_keeps_direct():
    release = ReleaseInfo("0.7.0", download_url="https://github.com/a.exe")
    plan = plan_download(release, probe=lambda *_a, **_k: 5.0, flush=lambda: True, sleep=lambda _s: None)
    assert plan.source == "direct"
    assert plan.url == "https://github.com/a.exe"


def test_update_plan_detail_mentions_dns():
    plan = UpdatePlan("mirror", "https://m/a.exe", direct_ms=1.5, dns_flushed=True, reason="延迟过高")
    text = plan.detail()
    assert "清理 DNS" in text and "镜像" in text


def test_is_newer_compares_versions():
    assert updater.is_newer("0.7.0", "0.6.2") is True
    assert updater.is_newer("0.7.0", "0.7.0") is False
    assert updater.is_newer("v0.7.1", "0.7.0") is True
    assert updater.is_newer("0.7.0-beta.1", "0.7.0") is False
    assert updater.is_newer("nightly", "0.7.0") is False


def test_downloader_sends_version_header():
    headers = Downloader()._headers("https://mc.example.com/meta/")
    assert headers["X-OffCraft-Version"]


def test_installer_name_helper():
    assert scan_installer_name("https://github.com/x/releases/download/v0.7.0/setup.exe") == "setup.exe"
    assert scan_installer_name("") == ""


def test_download_installer_accepts_partial_file(monkeypatch, tmp_path):
    """断点续传：已有 .part 时由 Downloader 带 Range 继续。"""
    captured: dict[str, object] = {}

    class _Fake:
        def download(self, task, tracker=None, cancel=None, keep_part=False):
            captured["keep_part"] = keep_part
            task.dest.write_bytes(b"x")
            return True

        def close(self):
            pass

    monkeypatch.setattr(updater, "Downloader", lambda **kwargs: _Fake())
    dest = updater.download_installer(
        UpdatePlan("direct", "https://github.com/a/setup.exe"), tmp_path
    )
    assert captured["keep_part"] is True
    assert dest.name == "setup.exe"


@pytest.fixture()
def fresh_elevation(monkeypatch):
    """每个用例都从「尚未请求过 UAC」开始。"""
    monkeypatch.setattr(updater, "_elevation_state", "")
    monkeypatch.setattr(updater, "_elevation_at", 0.0)
    monkeypatch.setattr(updater, "_elevation_busy", False)


def test_elevation_skipped_outside_windows(monkeypatch, fresh_elevation):
    monkeypatch.setattr(updater.platform, "system", lambda: "Linux")
    called = []
    monkeypatch.setattr(updater, "_request_elevation", lambda: called.append(True))
    assert updater._flush_windows_elevated() is False
    assert called == []


def test_elevation_skipped_when_already_admin(monkeypatch, fresh_elevation):
    monkeypatch.setattr(updater.platform, "system", lambda: "Windows")
    monkeypatch.setattr(updater, "_is_admin", lambda: True)
    called = []
    monkeypatch.setattr(updater, "_request_elevation", lambda: called.append(True))
    assert updater._flush_windows_elevated() is False
    assert called == []


def test_elevation_approved_then_throttled(monkeypatch, fresh_elevation):
    monkeypatch.setattr(updater.platform, "system", lambda: "Windows")
    monkeypatch.setattr(updater, "_is_admin", lambda: False)
    calls: list[bool] = []

    def fake_request() -> bool:
        calls.append(True)
        return True

    monkeypatch.setattr(updater, "_request_elevation", fake_request)
    assert updater._flush_windows_elevated(timeout=1.0) is True
    # 30 分钟内不再重复弹窗
    assert updater._flush_windows_elevated(timeout=1.0) is False
    assert len(calls) == 1
    assert updater._elevation_state == "approved"


def test_elevation_denied_is_not_asked_again(monkeypatch, fresh_elevation):
    monkeypatch.setattr(updater.platform, "system", lambda: "Windows")
    monkeypatch.setattr(updater, "_is_admin", lambda: False)
    calls: list[bool] = []

    def fake_request() -> bool:
        calls.append(False)
        return False

    monkeypatch.setattr(updater, "_request_elevation", fake_request)
    assert updater._flush_windows_elevated(timeout=1.0) is False
    assert updater._flush_windows_elevated(timeout=1.0) is False
    assert len(calls) == 1
    assert updater._elevation_state == "denied"


def test_elevation_timeout_does_not_block(monkeypatch, fresh_elevation):
    monkeypatch.setattr(updater.platform, "system", lambda: "Windows")
    monkeypatch.setattr(updater, "_is_admin", lambda: False)
    release = threading.Event()

    def slow_request() -> bool:
        release.wait(5.0)  # 模拟用户一直不处理 UAC 弹窗
        return True

    monkeypatch.setattr(updater, "_request_elevation", slow_request)
    assert updater._flush_windows_elevated(timeout=0.05) is False
    # 后台请求尚未结束：不重复弹窗，也不卡住调用方
    assert updater._flush_windows_elevated(timeout=0.05) is False
    release.set()
    for _ in range(100):  # 等后台线程收尾，避免影响后续用例
        if not updater._elevation_busy:
            break
        time.sleep(0.01)


def test_flush_dns_falls_back_to_elevation(monkeypatch, fresh_elevation):
    """普通权限 ipconfig 失败后应请求提权，而不是直接返回 False。"""
    monkeypatch.setattr(updater.platform, "system", lambda: "Windows")
    monkeypatch.setattr(updater, "_is_admin", lambda: False)

    class _Result:
        returncode = 1

    monkeypatch.setattr(updater.subprocess, "run", lambda *_a, **_k: _Result())
    monkeypatch.setattr(updater, "_request_elevation", lambda: True)
    assert updater.flush_dns() is True
