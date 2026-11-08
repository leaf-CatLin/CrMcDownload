"""账户提供者抽象：离线账户与 Microsoft 正版账户。

:class:`LaunchAccount` 与旧的 :class:`offcraft.launch.auth.OfflineProfile` 字段兼容
（``name/uuid/access_token/user_type/xuid/auth_session``），command.py 可直接消费。

正版账户的令牌由 :mod:`offcraft.account.store` 保存在本机，启动时不足两分钟过期
就自动用刷新令牌续期。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from offcraft.account.microsoft import MicrosoftAuthError, dashed_uuid, refresh_session
from offcraft.account.store import load_session, save_session
from offcraft.launch.auth import OfflineProfile, offline_uuid, validate_name

__all__ = [
    "AccountProvider",
    "LaunchAccount",
    "MicrosoftAccount",
    "OfflineAccount",
    "get_provider",
    "register_provider",
]


@dataclass(frozen=True, slots=True)
class LaunchAccount:
    """一次启动所需的账户信息（离线 / 正版统一形态）。"""

    name: str
    uuid: str
    access_token: str = "0"
    user_type: str = "legacy"
    xuid: str = ""
    java_agent: str | None = None
    extra_jvm: list[str] = field(default_factory=list)

    @property
    def auth_session(self) -> str:
        return f"token:{self.access_token}:{self.uuid}"

    def to_profile(self) -> OfflineProfile:
        """向后兼容：转成旧的 :class:`OfflineProfile`（丢弃 agent/jvm）。"""
        return OfflineProfile(
            name=self.name,
            uuid=self.uuid,
            access_token=self.access_token,
            user_type=self.user_type,
            xuid=self.xuid,
        )


@runtime_checkable
class AccountProvider(Protocol):
    """账户提供者接口。"""

    id: str
    label: str

    def acquire(self, config: Any, data_dir: Any = None) -> LaunchAccount:
        """从配置 / 本地令牌里取出本次启动要用的账户。"""
        ...

    def capabilities(self) -> set[str]:
        """能力声明：``skin`` / ``cape`` / ``online-mode-server`` 等。"""
        ...


class OfflineAccount:
    """默认实现：把现有 ``make_profile`` 包一层，账户不可在线校验。"""

    id = "offline"
    label = "离线账户"

    def acquire(self, config: Any, data_dir: Any = None) -> LaunchAccount:
        name = validate_name(getattr(config, "username", "") or "Player")
        return LaunchAccount(name=name, uuid=offline_uuid(name))

    def capabilities(self) -> set[str]:
        # 离线账户没有官方 GameProfile：皮肤只能靠 CSL mod
        return {"skin"}


class MicrosoftAccount:
    """Microsoft 正版账户：使用本地保存的 Minecraft 令牌，必要时自动续期。"""

    id = "microsoft"
    label = "正版账户"

    def acquire(self, config: Any, data_dir: Any = None) -> LaunchAccount:
        session = load_session(data_dir)
        if session is None:
            raise MicrosoftAuthError("正版账号尚未登录，请点击启动页的账户名完成登录")
        if time.time() >= session.expires_at - 120:
            session = refresh_session(session, proxy=getattr(config, "proxy", "") or "")
            save_session(session, data_dir)
        return LaunchAccount(
            name=session.name,
            uuid=dashed_uuid(session.uuid),
            access_token=session.access_token,
            user_type="msa",
            xuid=session.xuid,
        )

    def capabilities(self) -> set[str]:
        return {"skin", "cape", "online-mode-server"}


_PROVIDERS: dict[str, AccountProvider] = {
    "offline": OfflineAccount(),
    "microsoft": MicrosoftAccount(),
}


def register_provider(provider: AccountProvider) -> None:
    """注册一个账户提供者（外置登录实现在这里挂进来）。"""
    _PROVIDERS[provider.id] = provider


def get_provider(config: Any) -> AccountProvider:
    """按 ``config.account_provider`` 取提供者，缺失时回落离线。"""
    pid = str(getattr(config, "account_provider", "offline") or "offline")
    return _PROVIDERS.get(pid) or _PROVIDERS["offline"]
