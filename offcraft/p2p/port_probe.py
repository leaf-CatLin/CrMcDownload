"""本机 Minecraft「对局域网开放」端口探测：为「开放房间」确定隧道转发的本机端口。

方案（产品决策）：默认固定 25565（A）；当 25565 上没有监听时，回退到从游戏日志
尽力探测实际局域网端口（C）；都拿不到则回落 25565 并让上层给出提示。探测只读、
绝不联网，失败一律安全返回默认值。

集成服务器对局域网开放时，日志里常见的端口线索（不同版本措辞略有差异，取最近一条）：
- ``Start listening on 0.0.0.0:54321`` / ``[Server thread/INFO]: ... :54321``
- ``opening port 54321`` / ``LocalMinecraftServer ... 54321``
"""

from __future__ import annotations

import re
import socket

__all__ = ["DEFAULT_MC_PORT", "is_listening", "detect_from_log", "detect_local_mc_port",
           "read_log_tail"]

#: Minecraft「对局域网开放」的默认端口
DEFAULT_MC_PORT = 25565

_PROBE_TIMEOUT = 0.5
_LOG_TAIL_BYTES = 256 * 1024

# 只取末尾 2-5 位、且尽量贴近“listening / port / server”关键字的整数
_LOG_PATTERNS = (
    re.compile(r"listening on\s+\S*?:(\d{2,5})", re.IGNORECASE),
    re.compile(r"opening port\s+(\d{2,5})", re.IGNORECASE),
    re.compile(r"(?:LocalMinecraftServer|Dedicated Server).*?(\d{4,5})", re.IGNORECASE),
)


def is_listening(port: int, host: str = "127.0.0.1", timeout: float = _PROBE_TIMEOUT) -> bool:
    """本机 ``host:port`` 是否有 TCP 监听（connect_ex 返回 0 视为在听）。"""
    if not 1 <= port <= 65535:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def detect_from_log(text: str) -> int:
    """从日志文本里取“最后出现”的一个合法端口；无匹配返回 0。"""
    found = 0
    for line in text.splitlines():
        for pattern in _LOG_PATTERNS:
            match = pattern.search(line)
            if match:
                try:
                    value = int(match.group(1))
                except (TypeError, ValueError):
                    continue
                if 1 <= value <= 65535:
                    found = value
    return found


def detect_local_mc_port(
    prefer: int = DEFAULT_MC_PORT, log_text: str = ""
) -> tuple[int, str]:
    """解析房主应转发的本机端口，返回 ``(port, source)``。

    - ``source=="prefer"``：25565 正在监听（方案 A 命中）。
    - ``source=="log"``：从日志探测到端口（方案 C 兜底）。
    - ``source=="default"``：都没命中，回落默认端口（上层应提示核对游戏内端口）。
    """
    if is_listening(prefer):
        return prefer, "prefer"
    if log_text:
        port = detect_from_log(log_text)
        if port:
            return port, "log"
    return prefer, "default"


def read_log_tail(path, max_bytes: int = _LOG_TAIL_BYTES) -> str:
    """尽力读取日志文件末尾若干字节；文件不存在/无权限时返回空串（不抛）。"""
    from pathlib import Path

    try:
        p = Path(path)
        if not p.is_file():
            return ""
        size = p.stat().st_size
        with p.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            raw = handle.read()
        return raw.decode("utf-8", "replace")
    except OSError:
        return ""
