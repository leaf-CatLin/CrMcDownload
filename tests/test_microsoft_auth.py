"""正版登录：设备码 / 浏览器授权 / 令牌链 / 本地存储的离线单元测试。"""

from __future__ import annotations

import time

import pytest

from offcraft.account import MicrosoftAccount
from offcraft.account import microsoft as msa


def _fake_http_factory(profile_name="Steve", profile_id="069a79f44f8a4f2c9b6e4a4d0b7f1c2a"):
    calls: list[tuple[str, str, dict]] = []

    def fake(url: str, **kwargs):
        payload = kwargs.get("payload") or {}
        calls.append((url, kwargs.get("method", "POST"), payload))
        if url.endswith("/user/authenticate"):
            return 200, {"Token": "xbl-token", "DisplayClaims": {"xui": [{"uhs": "1234567890"}]}}
        if url.endswith("/xsts/authorize"):
            return 200, {"Token": "xsts-token", "DisplayClaims": {"xui": [{"uhs": "1234567890"}]}}
        if url.endswith("/authentication/login_with_xbox"):
            return 200, {"access_token": "mc-token", "expires_in": 86400}
        if url.endswith("/minecraft/profile"):
            return 200, {"id": profile_id, "name": profile_name}
        raise AssertionError(f"未预期的请求：{url}")

    return fake, calls


def test_authenticate_chain_builds_session(monkeypatch):
    fake, calls = _fake_http_factory()
    monkeypatch.setattr(msa, "_http", fake)

    session = msa.authenticate({"access_token": "ms", "refresh_token": "refresh"}, proxy="")

    assert session.name == "Steve"
    assert session.access_token == "mc-token"
    assert session.refresh_token == "refresh"
    assert session.xuid == "1234567890"
    assert session.expires_at > time.time()
    assert any("login_with_xbox" in url for url, _, _ in calls)
    # XSTS 用的是 Xbox 令牌
    xsts_payload = next(p for u, _, p in calls if "xsts" in u)
    assert xsts_payload["Properties"]["UserTokens"] == ["xbl-token"]


def test_xsts_known_error_is_friendly(monkeypatch):
    def fake(url: str, **kwargs):
        if url.endswith("/user/authenticate"):
            return 200, {"Token": "xbl", "DisplayClaims": {"xui": [{"uhs": "u"}]}}
        if url.endswith("/xsts/authorize"):
            return 401, {"XErr": 2148916233}
        raise AssertionError(url)

    monkeypatch.setattr(msa, "_http", fake)
    with pytest.raises(msa.MicrosoftAuthError) as info:
        msa.authenticate({"access_token": "ms"})
    assert "Xbox 档案" in str(info.value)


def test_profile_without_game(monkeypatch):
    def fake(url: str, **kwargs):
        if url.endswith("/user/authenticate"):
            return 200, {"Token": "xbl", "DisplayClaims": {"xui": [{"uhs": "u"}]}}
        if url.endswith("/xsts/authorize"):
            return 200, {"Token": "xsts", "DisplayClaims": {"xui": [{"uhs": "u"}]}}
        if url.endswith("/login_with_xbox"):
            return 200, {"access_token": "mc", "expires_in": 3600}
        return 404, {}

    monkeypatch.setattr(msa, "_http", fake)
    with pytest.raises(msa.MicrosoftAuthError) as info:
        msa.authenticate({"access_token": "ms"})
    assert "Minecraft: Java 版" in str(info.value)


def test_device_code_poll_handles_pending_then_success(monkeypatch):
    responses = [
        (400, {"error": "authorization_pending"}),
        (400, {"error": "slow_down"}),
        (200, {"access_token": "ms-token", "refresh_token": "r"}),
    ]

    def fake(url: str, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(msa, "_http", fake)
    code = msa.DeviceCode(device_code="dc", user_code="ABC", verification_uri="https://x", interval=1)
    waits: list[int] = []
    token = msa.poll_device_code(code, sleep=lambda _s: None, on_wait=waits.append)
    assert token["access_token"] == "ms-token"
    assert waits  # 上报了剩余时间


def test_device_code_declined(monkeypatch):
    monkeypatch.setattr(msa, "_http", lambda url, **kw: (400, {"error": "authorization_declined"}))
    code = msa.DeviceCode(device_code="dc", user_code="ABC", verification_uri="https://x", interval=1)
    with pytest.raises(msa.MicrosoftAuthError) as info:
        msa.poll_device_code(code, sleep=lambda _s: None)
    assert "取消" in str(info.value)


def test_device_code_cancel_event(monkeypatch):
    code = msa.DeviceCode(device_code="dc", user_code="ABC", verification_uri="https://x", interval=1)
    cancel = __import__("threading").Event()
    cancel.set()
    with pytest.raises(msa.MicrosoftAuthError):
        msa.poll_device_code(code, cancel=cancel, sleep=lambda _s: None)


def test_exchange_code_failure_message(monkeypatch):
    monkeypatch.setattr(
        msa,
        "_http",
        lambda url, **kw: (400, {"error": "invalid_grant", "error_description": "代码已使用"}),
    )
    with pytest.raises(msa.MicrosoftAuthError) as info:
        msa.exchange_code("abc")
    assert "代码已使用" in str(info.value)


def test_authorize_url_contains_client_and_redirect():
    url = msa.authorize_url("state-1")
    assert f"client_id={msa.CLIENT_ID}" in url
    assert "redirect_uri=https%3A%2F%2Flogin.live.com%2Foauth20_desktop.srf" in url
    assert "state=state-1" in url


def test_extract_code_accepts_full_url_and_bare_code():
    assert msa.extract_code("https://login.live.com/oauth20_desktop.srf?code=ABCdef123&lc=1033") == "ABCdef123"
    assert msa.extract_code("login.live.com/oauth20_desktop.srf?code=XYZ") == "XYZ"
    assert msa.extract_code("a" * 28) == "a" * 28
    with pytest.raises(msa.MicrosoftAuthError):
        msa.extract_code("https://login.live.com/oauth20_desktop.srf?lc=1033")
    with pytest.raises(msa.MicrosoftAuthError):
        msa.extract_code("")


def test_xbox_auth_retries_without_d_prefix(monkeypatch):
    calls: list[str] = []

    def fake(url: str, **kwargs):
        payload = kwargs.get("payload") or {}
        if url.endswith("/user/authenticate"):
            ticket = payload["Properties"]["RpsTicket"]
            calls.append(ticket)
            if ticket.startswith("d="):
                return 400, {"error": "invalid_grant"}
            return 200, {"Token": "xbl", "DisplayClaims": {"xui": [{"uhs": "u"}]}}
        if url.endswith("/xsts/authorize"):
            return 200, {"Token": "xsts", "DisplayClaims": {"xui": [{"uhs": "u"}]}}
        if url.endswith("/login_with_xbox"):
            return 200, {"access_token": "mc", "expires_in": 3600}
        return 200, {"id": "id", "name": "Steve"}

    monkeypatch.setattr(msa, "_http", fake)
    session = msa.authenticate({"access_token": "ms"})
    assert session.name == "Steve"
    assert len(calls) == 2 and calls[1] == "ms"


def test_session_store_roundtrip(tmp_path):
    from offcraft.account.store import clear_session, load_session, save_session

    session = msa.MsaSession(
        name="Alex", uuid="deadbeef", access_token="mc", refresh_token="r", xuid="x", expires_at=1.0
    )
    path = save_session(session, tmp_path)
    assert path.is_file()
    loaded = load_session(tmp_path)
    assert loaded is not None and loaded.name == "Alex" and loaded.uuid == "deadbeef"
    clear_session(tmp_path)
    assert load_session(tmp_path) is None


def test_store_ignores_corrupt_file(tmp_path):
    from offcraft.account.store import load_session, session_path

    session_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert load_session(tmp_path) is None


def test_provider_returns_msa_account(tmp_path):
    from offcraft.account.store import save_session

    save_session(
        msa.MsaSession(
            name="Alex",
            uuid="069a79f44f8a4f2c9b6e4a4d0b7f1c2a",
            access_token="mc-token",
            refresh_token="r",
            xuid="123",
            expires_at=time.time() + 3600,
        ),
        tmp_path,
    )
    account = MicrosoftAccount().acquire(object(), tmp_path)
    assert account.name == "Alex"
    assert account.user_type == "msa"
    assert account.access_token == "mc-token"
    assert account.uuid == "069a79f4-4f8a-4f2c-9b6e-4a4d0b7f1c2a"
    assert account.xuid == "123"


def test_provider_without_session_raises(tmp_path):
    with pytest.raises(msa.MicrosoftAuthError):
        MicrosoftAccount().acquire(object(), tmp_path)


def test_provider_refreshes_expired_session(tmp_path, monkeypatch):
    from offcraft.account.store import load_session, save_session

    old = msa.MsaSession(
        name="Old", uuid="u", access_token="old", refresh_token="r", xuid="1", expires_at=0.0
    )
    save_session(old, tmp_path)
    fresh = msa.MsaSession(
        name="New", uuid="u2", access_token="new", refresh_token="r2", xuid="2", expires_at=time.time() + 3600
    )

    def fake_refresh(session, proxy=""):
        assert session.name == "Old"
        return fresh

    monkeypatch.setattr("offcraft.account.refresh_session", fake_refresh)
    account = MicrosoftAccount().acquire(object(), tmp_path)
    assert account.access_token == "new"
    assert account.name == "New"
    # 续期结果要回写本地
    assert load_session(tmp_path).access_token == "new"


def test_config_normalizes_provider():
    from offcraft.config import Config

    config = Config.from_json({"account_provider": "microsoft"})
    assert config.account_provider == "microsoft"
    config = Config.from_json({"account_provider": "something-else"})
    assert config.account_provider == "offline"


def test_dashed_uuid():
    assert msa.dashed_uuid("069a79f44f8a4f2c9b6e4a4d0b7f1c2a") == "069a79f4-4f8a-4f2c-9b6e-4a4d0b7f1c2a"
    assert msa.dashed_uuid("bad") == "bad"


# ---------------------------------------------------------------- 内嵌登录窗口
def test_webview_command_reuses_executable(tmp_path, monkeypatch):
    from offcraft.account import webview_login as wl

    monkeypatch.setattr(wl.sys, "frozen", True, raising=False)
    cmd = wl.build_command("https://example.com/login", tmp_path / "r.json")
    assert cmd[0] == wl.sys.executable
    assert wl.FLAG in cmd and wl.RESULT_FLAG in cmd
    assert cmd[cmd.index(wl.FLAG) + 1] == "https://example.com/login"
    assert str(tmp_path / "r.json") in cmd


def test_webview_command_dev_mode_runs_main_py(monkeypatch):
    from offcraft.account import webview_login as wl

    monkeypatch.delattr(wl.sys, "frozen", raising=False)
    cmd = wl.build_command("https://example.com/login", "result.json")
    assert cmd[0] == wl.sys.executable
    assert cmd[1].endswith("main.py")


def test_webview_result_roundtrip_and_corrupt(tmp_path):
    from offcraft.account import webview_login as wl

    path = tmp_path / "result.json"
    assert wl.read_result(path) is None
    wl.write_result(path, {"url": "https://login.live.com/oauth20_desktop.srf?code=abc"})
    assert wl.read_result(path)["url"].endswith("code=abc")
    path.write_text("{bad json", encoding="utf-8")
    assert wl.read_result(path) is None


def test_webview_wait_result_reads_file_and_respects_cancel(tmp_path):
    import threading
    from types import SimpleNamespace

    from offcraft.account import webview_login as wl

    fake = SimpleNamespace(poll=lambda: None)
    assert wl.wait_result(fake, tmp_path / "missing.json", timeout=0.4) is None

    path = tmp_path / "ok.json"
    wl.write_result(path, {"url": "code=xyz"})
    assert wl.wait_result(fake, path, timeout=1.0)["url"] == "code=xyz"

    cancel = threading.Event()
    cancel.set()
    assert wl.wait_result(fake, tmp_path / "missing.json", timeout=5.0, cancel=cancel) is None


def test_webview_callback_error_messages():
    from offcraft.account import webview_login as wl

    assert wl.callback_error("https://login.live.com/oauth20_desktop.srf?code=abc") == ""
    assert "取消" in wl.callback_error(
        "https://login.live.com/oauth20_desktop.srf?error=access_denied"
    )
    message = wl.callback_error(
        "https://login.live.com/oauth20_desktop.srf?error=invalid_scope&error_description=bad"
    )
    assert "bad" in message


def test_webview_parse_args_requires_flags():
    from offcraft.account import webview_login as wl

    with pytest.raises(ValueError):
        wl.parse_args(["x"])
    url, path = wl.parse_args([wl.FLAG, "https://a", wl.RESULT_FLAG, "r.json"])
    assert url == "https://a" and str(path) == "r.json"
