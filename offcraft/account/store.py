"""正版登录会话的本地存储。

只写在客户端数据目录（``account.json``），不上传任何服务器；退出登录即删除。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from offcraft.account.microsoft import MsaSession
from offcraft.config import config_dir

__all__ = ["ACCOUNT_FILE", "clear_session", "load_session", "save_session", "session_path"]

ACCOUNT_FILE = "account.json"


def session_path(data_dir: str | Path | None = None) -> Path:
    return config_dir(data_dir) / ACCOUNT_FILE


def load_session(data_dir: str | Path | None = None) -> MsaSession | None:
    """读取本地会话；文件缺失或损坏时返回 None（不抛异常）。"""
    path = session_path(data_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    session = MsaSession.from_json(data)
    if not (session.name and session.access_token):
        return None
    return session


def save_session(session: MsaSession, data_dir: str | Path | None = None) -> Path:
    """原子写入，并尽量收紧文件权限（令牌不该被别的用户读到）。"""
    path = session_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(session.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return path


def clear_session(data_dir: str | Path | None = None) -> None:
    """退出正版登录：删除本地令牌文件。"""
    try:
        session_path(data_dir).unlink()
    except OSError:
        pass
