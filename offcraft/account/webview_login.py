"""内嵌 WebView2 登录窗口（独立子进程）。

PCL2 的做法是让启动器自己充当浏览器：在进程内的 Edge WebView 里打开微软授权页，
页面跳到 ``oauth20_desktop.srf?code=...`` 时直接读地址栏，因此不需要用户复制粘贴。

pywebview 的 GUI 循环必须独占主线程，和 PyQt6 冲突，所以这里把它放进子进程：
父进程用 ``--webview-login URL --webview-result FILE`` 启动本模块，
子进程把最终地址（或取消 / 错误）原子写入结果文件后退出，父进程轮询该文件即可。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

#: 子进程命令行标记（main.py 据此分流，不进入 GUI）
FLAG = "--webview-login"
RESULT_FLAG = "--webview-result"
WINDOW_TITLE = "登录 Microsoft 账户"
#: 登录窗口最长等待时间（秒）
DEFAULT_TIMEOUT = 600


# ------------------------------------------------------------------ 进程协议
def build_command(url: str, result_path: str | Path) -> list[str]:
    """构造启动子进程的命令；打包后复用同一个可执行文件。"""
    if getattr(sys, "frozen", False):
        base = [sys.executable]
    else:
        main_py = Path(__file__).resolve().parents[2] / "main.py"
        base = [sys.executable, str(main_py)]
    return [*base, FLAG, url, RESULT_FLAG, str(result_path)]


def new_result_path() -> Path:
    """每次登录用一个新的结果文件，避免读到上一次的残留。"""
    return Path(tempfile.gettempdir()) / f"crmc-msa-{uuid.uuid4().hex}.json"


def spawn_login(url: str) -> tuple[subprocess.Popen[bytes], Path]:
    """启动内嵌登录窗口；返回 (子进程, 结果文件路径)。"""
    path = new_result_path()
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        # 子进程是同一个 exe / python.exe，避免闪出一个控制台窗口
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        build_command(url, path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **kwargs,
    )
    return proc, path


def write_result(path: str | Path, payload: dict[str, Any]) -> None:
    """原子写入结果：先写临时文件再 replace，父进程不会读到半截 JSON。

    结果文件只是父进程的通信通道，写失败不应把登录窗口也带崩，因此最后兜底忽略异常。
    """
    target = Path(path)
    try:
        temp = target.with_suffix(".tmp")
        try:
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, target)
            return
        except OSError:
            temp.unlink(missing_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def read_result(path: str | Path) -> dict[str, Any] | None:
    """读取结果文件；不存在 / 内容损坏时返回 None。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def wait_result(
    proc: subprocess.Popen[bytes],
    path: str | Path,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    cancel: Any = None,
) -> dict[str, Any] | None:
    """等待登录窗口结束；返回结果，用户关闭窗口 / 超时 / 取消时返回 None。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = read_result(path)
        if data is not None:
            return data
        if cancel is not None and cancel.is_set():
            return None
        if proc.poll() is not None:
            # 子进程刚退出时可能刚写完结果，再给一次机会
            time.sleep(0.2)
            return read_result(path)
        time.sleep(0.2)
    return None


def callback_error(url: str) -> str:
    """微软在回调地址里返回 error= 时转成人话；没有则返回空串。"""
    if "error=" not in url:
        return ""
    query = parse_qs(urlsplit(url).query)
    error = str((query.get("error") or [""])[0]).strip()
    if not error:
        return ""
    if error == "access_denied":
        return "你已取消 Microsoft 授权。"
    description = str((query.get("error_description") or [""])[0]).strip()
    return description or f"Microsoft 返回错误：{error}"


# ------------------------------------------------------------------ 子进程入口
def parse_args(argv: list[str]) -> tuple[str, Path]:
    """从命令行取出授权页地址与结果文件路径。"""
    if FLAG not in argv or RESULT_FLAG not in argv:
        raise ValueError("缺少 --webview-login 或 --webview-result 参数")
    url = argv[argv.index(FLAG) + 1]
    result = argv[argv.index(RESULT_FLAG) + 1]
    if not url or not result:
        raise ValueError("--webview-login 与 --webview-result 的值不能为空")
    return url, Path(result)


def run_helper(argv: list[str]) -> int:
    """子进程主体：打开登录窗口，截获回调地址，写结果后退出。"""
    try:
        url, result_path = parse_args(argv)
    except (ValueError, IndexError) as exc:
        # 参数不合法时只能尽力把错误写回结果文件（可能没有路径）
        if RESULT_FLAG in argv:
            index = argv.index(RESULT_FLAG) + 1
            if index < len(argv):
                write_result(argv[index], {"error": str(exc)})
        return 2

    try:
        import webview  # 延迟导入：主程序没有该依赖时仍可启动
    except Exception as exc:  # noqa: BLE001 - 交给父进程展示
        write_result(result_path, {"error": f"内嵌浏览器组件不可用：{exc}"})
        return 1

    state = {"done": False}

    def finish(payload: dict[str, Any]) -> None:
        if state["done"]:
            return
        state["done"] = True
        write_result(result_path, payload)
        try:
            window.destroy()
        except Exception:  # noqa: BLE001 - 窗口可能已被用户关掉
            pass

    try:
        window = webview.create_window(
            WINDOW_TITLE,
            url,
            width=560,
            height=780,
            min_size=(420, 600),
        )

        def on_loaded() -> None:
            current = window.get_current_url() or ""
            if "code=" in current or "error=" in current:
                finish({"url": current})

        def on_closed() -> None:
            if not state["done"]:
                state["done"] = True
                write_result(result_path, {"cancelled": True})

        window.events.loaded += on_loaded
        window.events.closed += on_closed
        webview.start()
    except Exception as exc:  # noqa: BLE001 - 交给父进程展示
        write_result(result_path, {"error": f"登录窗口启动失败：{exc}"})
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    raise SystemExit(run_helper(sys.argv[1:]))
