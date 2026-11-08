"""Microsoft 正版（Minecraft: Java Edition）登录。

使用公开的 Minecraft 启动器客户端 ID（``00000000402b5328``）走 Live Connect：

- 设备码：客户端向微软申请 8 位代码，用户在 ``microsoft.com/link`` 输入即可；
- 浏览器登录：打开系统浏览器完成登录，再把跳转后的地址粘贴回启动器。

微软已不再允许这个客户端 ID 回调到 ``localhost``，因此浏览器方式改为
「打开浏览器 + 粘贴回调地址」，不依赖本地端口。

两条路径都只拿到 Microsoft 的 OAuth 令牌，后面还要依次换 Xbox Live、XSTS 和
Minecraft 令牌（``authenticate`` 一次走完）。令牌只保存在本机
（``offcraft.account.store``），不会上传。
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

__all__ = [
    "CLIENT_ID",
    "DeviceCode",
    "MicrosoftAuthError",
    "MsaSession",
    "authenticate",
    "authorize_url",
    "dashed_uuid",
    "exchange_code",
    "extract_code",
    "poll_device_code",
    "refresh_session",
    "request_device_code",
]

#: 公开的 Minecraft 启动器客户端 ID（Live Connect / MBI_SSL）
CLIENT_ID = os.environ.get("OFFCRAFT_MSA_CLIENT_ID", "00000000402b5328").strip()
#: Live Connect 的 Xbox Live 授权范围
SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"
CONNECT_URL = "https://login.live.com/oauth20_connect.srf"
TOKEN_URL = "https://login.live.com/oauth20_token.srf"
AUTHORIZE_URL = "https://login.live.com/oauth20_authorize.srf"
#: 该客户端 ID 已注册的回调地址（登录后微软会跳到这个页面并显示 code）
REDIRECT_URI = "https://login.live.com/oauth20_desktop.srf"

_XBL_AUTH = "https://user.auth.xboxlive.com/user/authenticate"
_XSTS_AUTH = "https://xsts.auth.xboxlive.com/xsts/authorize"
_MC_LOGIN = "https://api.minecraftservices.com/authentication/login_with_xbox"
_MC_PROFILE = "https://api.minecraftservices.com/minecraft/profile"

#: XSTS 常见的 XErr 错误码 → 人话
_XERR = {
    2148916233: "这个 Microsoft 账户还没有 Xbox 档案，请先到 xbox.com 登录一次创建档案",
    2148916235: "你所在的国家/地区不支持 Xbox Live",
    2148916236: "该账户需要完成成人验证后才能使用 Xbox Live",
    2148916237: "该账户的 Xbox Live 功能已被限制",
    2148916238: "未成年人账户需要先由家长加入 Microsoft 家庭组",
}


class MicrosoftAuthError(RuntimeError):
    """登录链路任一步失败。消息直接展示给用户。"""


@dataclass(slots=True)
class DeviceCode:
    """设备码：给用户看的链接 / 代码 + 轮询凭据。"""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int = 900
    interval: int = 5
    message: str = ""


@dataclass(slots=True)
class MsaSession:
    """一次正版登录的完整结果，也是本地持久化的结构。"""

    name: str
    uuid: str
    access_token: str
    refresh_token: str = ""
    xuid: str = ""
    #: Minecraft 令牌过期时间（unix 秒）
    expires_at: float = 0.0

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - 120

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "MsaSession":
        return cls(
            name=str(data.get("name") or ""),
            uuid=str(data.get("uuid") or ""),
            access_token=str(data.get("access_token") or ""),
            refresh_token=str(data.get("refresh_token") or ""),
            xuid=str(data.get("xuid") or ""),
            expires_at=float(data.get("expires_at") or 0.0),
        )


def dashed_uuid(value: str) -> str:
    """把 Minecraft profile 的 32 位 hex 转成带连字符的 UUID。"""
    raw = str(value or "").strip()
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        return raw


# --------------------------------------------------------------------- HTTP
def _opener(proxy: str = ""):
    text = str(proxy or "").strip()
    if text:
        return build_opener(ProxyHandler({"http": text, "https": text}))
    return build_opener()


def _http(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    json_body: bool = False,
    headers: dict[str, str] | None = None,
    timeout: float = 25.0,
    proxy: str = "",
    method: str = "POST",
) -> tuple[int, dict[str, Any]]:
    """发请求并尽量解析 JSON；HTTP 4xx 也返回响应体（OAuth 错误都在里面）。"""
    data: bytes | None = None
    if payload is not None:
        if json_body:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        else:
            data = urlencode(payload).encode("utf-8")
    head = {
        "Accept": "application/json",
        "User-Agent": "CrMCDownload-MSA/1.0",
    }
    if data is not None:
        head["Content-Type"] = (
            "application/json; charset=utf-8" if json_body else "application/x-www-form-urlencoded"
        )
    if headers:
        head.update(headers)
    request = Request(url, data=data, headers=head, method=method)
    try:
        with _opener(proxy).open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            status = int(getattr(response, "status", 200))
    except HTTPError as exc:
        status = int(exc.code)
        body = exc.read().decode("utf-8", "replace")
    except (URLError, socket.timeout, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise MicrosoftAuthError(f"网络连接失败：{reason}") from exc
    try:
        parsed = json.loads(body) if body else {}
    except ValueError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return status, parsed


def _oauth_error(status: int, data: dict[str, Any], fallback: str) -> str:
    message = str(data.get("error_description") or data.get("error") or "").strip()
    return message or f"{fallback}（HTTP {status}）"


# --------------------------------------------------------------- 设备码流程
def request_device_code(*, proxy: str = "") -> DeviceCode:
    """申请设备码；返回链接 / 代码与轮询间隔。"""
    status, data = _http(
        CONNECT_URL,
        payload={
            "client_id": CLIENT_ID,
            "scope": SCOPE,
            "response_type": "device_code",
        },
        proxy=proxy,
    )
    if status != 200 or not data.get("user_code"):
        raise MicrosoftAuthError(_oauth_error(status, data, "无法获取登录代码"))
    return DeviceCode(
        device_code=str(data.get("device_code") or ""),
        user_code=str(data.get("user_code") or ""),
        verification_uri=str(data.get("verification_uri") or "https://www.microsoft.com/link"),
        expires_in=int(data.get("expires_in") or 900),
        interval=int(data.get("interval") or 5),
        message=str(data.get("message") or ""),
    )


def poll_device_code(
    code: DeviceCode,
    *,
    proxy: str = "",
    cancel: threading.Event | None = None,
    on_wait: Callable[[int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """轮询 ``oauth20_token.srf`` 直到用户完成登录；返回 Microsoft 令牌响应。"""
    interval = max(1, int(code.interval or 5))
    deadline = time.monotonic() + max(30, int(code.expires_in or 900))
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            raise MicrosoftAuthError("已取消登录")
        status, data = _http(
            TOKEN_URL,
            payload={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": CLIENT_ID,
                "device_code": code.device_code,
            },
            proxy=proxy,
        )
        if status == 200 and data.get("access_token"):
            return data
        error = str(data.get("error") or "")
        if error == "authorization_pending":
            pass
        elif error == "slow_down":
            interval += 5
        elif error == "expired_token":
            raise MicrosoftAuthError("登录代码已过期，请重新获取")
        elif error in ("authorization_declined", "access_denied"):
            raise MicrosoftAuthError("你已取消授权")
        else:
            raise MicrosoftAuthError(_oauth_error(status, data, "登录失败"))
        if on_wait is not None:
            on_wait(max(0, int(deadline - time.monotonic())))
        sleep(interval)
    raise MicrosoftAuthError("等待登录超时，请重新获取代码")


def authorize_url(state: str = "") -> str:
    """浏览器登录地址；登录后跳到 ``oauth20_desktop.srf`` 并显示 ``code=``。"""
    query = urlencode(
        {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "state": state or uuid.uuid4().hex,
            "prompt": "select_account",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def extract_code(value: str) -> str:
    """从粘贴的回调地址（或裸 code）里取出授权码。"""
    text = str(value or "").strip().strip('"').strip("'")
    if not text:
        raise MicrosoftAuthError("请粘贴浏览器跳转后的完整地址（其中包含 code=）")
    if "code=" in text:
        candidate = text if "://" in text else "https://" + text
        query = parse_qs(urlsplit(candidate).query)
        code = str((query.get("code") or [""])[0]).strip()
        if code:
            return code
        raise MicrosoftAuthError("地址里没有找到 code 参数，请重新复制完整的跳转地址")
    # 用户也可能只粘贴了授权码本身（不含 URL 的 ://、?、/ 等符号）
    if (
        len(text) >= 20
        and " " not in text
        and "://" not in text
        and "?" not in text
        and "/" not in text
    ):
        return text
    raise MicrosoftAuthError("无法识别授权码，请粘贴浏览器地址栏里的完整地址")


def exchange_code(code: str, *, proxy: str = "", redirect_uri: str = REDIRECT_URI) -> dict[str, Any]:
    """用授权码换 Microsoft 令牌。"""
    status, data = _http(
        TOKEN_URL,
        payload={
            "client_id": CLIENT_ID,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
        },
        proxy=proxy,
    )
    if status != 200 or not data.get("access_token"):
        raise MicrosoftAuthError(_oauth_error(status, data, "授权码换取令牌失败"))
    return data


# ----------------------------------------------------------- Xbox / Minecraft
def _xbox_authenticate(ms_access: str, proxy: str) -> tuple[str, str]:
    """用 MSA 令牌换 Xbox Live 令牌；兼容带 ``d=`` 与裸令牌两种写法。"""
    last_error = "Xbox Live 认证失败，请稍后重试"
    for ticket in (f"d={ms_access}", ms_access):
        status, data = _http(
            _XBL_AUTH,
            payload={
                "Properties": {
                    "AuthMethod": "RPS",
                    "SiteName": "user.auth.xboxlive.com",
                    "RpsTicket": ticket,
                },
                "RelyingParty": "http://auth.xboxlive.com",
                "TokenType": "JWT",
            },
            json_body=True,
            proxy=proxy,
        )
        token = str(data.get("Token") or "")
        claims = (data.get("DisplayClaims") or {}).get("xui") or [{}]
        uhs = str((claims[0] or {}).get("uhs") or "")
        if status == 200 and token and uhs:
            return token, uhs
        last_error = _oauth_error(status, data, last_error)
    raise MicrosoftAuthError(last_error)


def _xsts_authorize(xbl_token: str, proxy: str) -> tuple[str, str]:
    status, data = _http(
        _XSTS_AUTH,
        payload={
            "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
            "RelyingParty": "rp://api.minecraftservices.com/",
            "TokenType": "JWT",
        },
        json_body=True,
        proxy=proxy,
    )
    if status == 401:
        xerr = data.get("XErr")
        message = _XERR.get(int(xerr), "") if xerr not in (None, "") else ""
        raise MicrosoftAuthError(message or "Xbox Live 授权被拒绝（XSTS 401）")
    token = str(data.get("Token") or "")
    claims = (data.get("DisplayClaims") or {}).get("xui") or [{}]
    uhs = str((claims[0] or {}).get("uhs") or "")
    if status != 200 or not token or not uhs:
        raise MicrosoftAuthError("Xbox Live 授权失败，请稍后重试")
    return token, uhs


def _minecraft_login(xsts: str, uhs: str, proxy: str) -> tuple[str, int]:
    status, data = _http(
        _MC_LOGIN,
        payload={"identityToken": f"XBL3.0 x={uhs};{xsts}"},
        json_body=True,
        proxy=proxy,
    )
    token = str(data.get("access_token") or "")
    if status != 200 or not token:
        raise MicrosoftAuthError("Minecraft 登录失败，请确认账户可正常使用 Xbox Live")
    return token, int(data.get("expires_in") or 86400)


def _minecraft_profile(token: str, proxy: str) -> dict[str, Any]:
    status, data = _http(
        _MC_PROFILE,
        headers={"Authorization": f"Bearer {token}"},
        proxy=proxy,
        method="GET",
    )
    if status == 404:
        raise MicrosoftAuthError("这个 Microsoft 账户尚未拥有 Minecraft: Java 版")
    if status != 200 or not data.get("id"):
        raise MicrosoftAuthError(f"读取 Minecraft 档案失败（HTTP {status}）")
    return data


def authenticate(ms_token: dict[str, Any], *, proxy: str = "") -> MsaSession:
    """Microsoft 令牌 → Xbox → XSTS → Minecraft → 档案，一次走完。"""
    ms_access = str(ms_token.get("access_token") or "")
    if not ms_access:
        raise MicrosoftAuthError("Microsoft 登录未返回访问令牌")
    xbl, uhs = _xbox_authenticate(ms_access, proxy)
    xsts, _ = _xsts_authorize(xbl, proxy)
    mc_token, expires_in = _minecraft_login(xsts, uhs, proxy)
    profile = _minecraft_profile(mc_token, proxy)
    return MsaSession(
        name=str(profile.get("name") or "Player"),
        uuid=str(profile.get("id") or ""),
        access_token=mc_token,
        refresh_token=str(ms_token.get("refresh_token") or ""),
        xuid=uhs,
        expires_at=time.time() + expires_in,
    )


def refresh_session(session: MsaSession, *, proxy: str = "") -> MsaSession:
    """用刷新令牌续期；微软可能轮换刷新令牌，因此整段重新走一遍链路。"""
    if not session.refresh_token:
        raise MicrosoftAuthError("登录信息已失效，请重新登录正版账号")
    status, data = _http(
        TOKEN_URL,
        payload={
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": session.refresh_token,
            "scope": SCOPE,
        },
        proxy=proxy,
    )
    if status != 200 or not data.get("access_token"):
        raise MicrosoftAuthError(_oauth_error(status, data, "刷新登录失败，请重新登录"))
    return authenticate(data, proxy=proxy)
