"""OffCraft 入口：无子命令时打开图形界面，否则交给 CLI。

内嵌登录窗口复用同一个可执行文件：``--webview-login`` 直接进入子进程模式，
这样打包后无需额外分发一个 exe。
"""

from __future__ import annotations

import sys

_COMMANDS = {"list", "install", "check", "instances", "launch", "pack", "gui"}

if __name__ == "__main__":
    from offcraft.account.webview_login import FLAG, run_helper

    if FLAG in sys.argv[1:]:
        raise SystemExit(run_helper(sys.argv[1:]))

    from offcraft.cli import main

    if not any(arg in _COMMANDS for arg in sys.argv[1:]):
        sys.argv.append("gui")
    raise SystemExit(main())
