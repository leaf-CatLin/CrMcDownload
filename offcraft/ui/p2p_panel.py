"""离线联机（P2P）面板：以「房间名 + 密码」创建/开放/加入/删除房间，走端到端加密隧道。

作为**特权官方插件**（capability ``net.tunnel``）的入口，只在插件页卡片「进入」后打开。
界面严守产品红线：不显示镜像域名 / 中继地址；密码只在会话内存派生密钥、落盘经机器绑定
加密（见 :mod:`offcraft.p2p.vault`）。所有信令与隧道都在后台线程进行，结果经 ``window.post``
回投主线程，主线程不阻塞。

房间语义（与服务端对齐）：

- ``创建房间``：专用入口，把全局唯一房间名登记进服务端（每设备配额，重名/超额报错）；
- ``开放``：房主为已创建的房间开一个实时会话供他人加入；
- ``加入并启动``：仅连接已开放的会话（房间不存在/未开放即报错，绝不自动建房），
  成功后本地加密存名+密码，并自动进入房主世界（虚拟局域网优先，旧隧道兜底）；
- ``删除``：房主删除自己创建的房间，释放名额、名字可复用。
"""

from __future__ import annotations

import ctypes
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from offcraft.ui.theme import MUTED
from offcraft.ui.widgets import Card

__all__ = ["P2PPanel"]

#: vpnd 路径状态 → UI 文案（与 packaging/vpn 的 SetPath 值一一对应）
_PATH_TEXT = {
    "idle": "未连接",
    "punching": "打洞中",
    "direct": "直连",
    "relay": "中继",
}


def _path_text(peer: Any) -> str:
    """单个对端的路径摘要：``10.66.0.2 直连 18ms``。"""
    name = _PATH_TEXT.get(getattr(peer, "path", ""), getattr(peer, "path", "") or "未连接")
    rtt = int(getattr(peer, "rtt_ms", 0) or 0)
    if rtt > 0:
        name += f" {rtt}ms"
    vip = str(getattr(peer, "vip", "") or "")
    return f"{vip} {name}".strip()


def _vpn_status_text(status: Any) -> str:
    """虚拟局域网状态行：本端 VIP + 直连/中继路径（人数多时只给汇总）。"""
    if status is None or not getattr(status, "running", False):
        state = getattr(status, "state", "stopped") if status is not None else "stopped"
        return f"虚拟局域网：未运行（{state}）"
    text = f"虚拟局域网：{getattr(status, 'vip', '') or '—'}"
    peers = list(getattr(status, "peers", []) or [])
    if not peers:
        text += " · 等待对端"
    elif len(peers) == 1:
        text += f" · {_path_text(peers[0])}"
    else:
        direct = sum(1 for p in peers if getattr(p, "path", "") == "direct")
        relay = sum(1 for p in peers if getattr(p, "path", "") == "relay")
        text += f" · 直连 {direct} · 中继 {relay}"
    if not getattr(status, "adapter", False):
        text += " · 虚拟网卡未就绪"
    return text


def _vpn_installer_path() -> Path | None:
    """随包分发的 ``vpn-setup.exe``。

    PyInstaller 6 的 onedir 会把非主程序文件放进 ``_internal``（即 ``sys._MEIPASS``），
    所以先查那里，再查主程序同目录，最后回落开发目录里刚编译的产物。
    """
    here = Path(sys.executable).parent
    candidates = [Path(getattr(sys, "_MEIPASS", here)) / "vpn-setup.exe",
                  here / "vpn-setup.exe",
                  Path(__file__).resolve().parents[2] / "packaging" / "vpn" / "out" / "vpn-setup.exe"]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _run_elevated(exe: Path) -> bool:
    """以管理员权限启动 exe（触发一次 UAC），返回是否成功发起。"""
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        rc = shell32.ShellExecuteW(None, "runas", str(exe), None, str(exe.parent), 1)
    except (AttributeError, OSError):
        return False
    return int(rc or 0) > 32


def _engine():
    """解析离线联机引擎：优先用插件包提供的 ``offcraft_p2p``（冻结基础包不内置）。

    基础安装包排除了 ``offcraft.p2p``，引擎随特权插件下载后以顶层包 ``offcraft_p2p``
    进入 ``sys.path``；开发/测试环境则直接命中源码树内的 ``offcraft.p2p``。两者对外接口
    完全一致，故上层无需区分来源。
    """
    import importlib

    for name in ("offcraft_p2p", "offcraft.p2p"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise ImportError("离线联机引擎未就绪，请先在插件页下载并启用「离线联机」。")


def _vault_class():
    import importlib

    for pkg in ("offcraft_p2p", "offcraft.p2p"):
        try:
            return importlib.import_module(pkg + ".vault").SavedRooms
        except ImportError:
            continue
    raise ImportError("离线联机引擎未就绪，请先在插件页下载并启用「离线联机」。")


class P2PPanel(QWidget):
    def __init__(self, window: Any) -> None:
        super().__init__()
        self.window = window
        self._room: Any = None
        self._tunnel: Any = None
        self._cancel = threading.Event()
        self._host = False
        self._roster_busy = False
        # 虚拟局域网（vpnd 数据面）：组件缺失时一律回落旧 TCP 隧道
        self._vpn_client = self._vpn_client_or_none()
        self._vpn_installer = _vpn_installer_path()
        self._vpn_supported = self._vpn_engine_ok()
        self._vpn_on = False
        self._vpn_busy = False
        self._vpn_config: dict[str, Any] | None = None
        self._vpn_edge = ""
        self._vpn_status: Any = None
        self._build()
        # 连接期间周期性拉取在线名册（后台线程 + window.post 回投，主线程不阻塞）。
        self._roster_timer = QTimer(self)
        self._roster_timer.setInterval(3000)
        self._roster_timer.timeout.connect(self._refresh_roster)
        # 虚拟局域网状态/配置同步（仅会话期间运行）：只在内容变化时才下发配置。
        self._vpn_timer = QTimer(self)
        self._vpn_timer.setInterval(2000)
        self._vpn_timer.timeout.connect(self._refresh_vpn)
        self._reload_saved()
        self._refresh_rooms()

    def _vpn_client_or_none(self):
        """vpnd 未安装 / 旧插件包不含 vpn_client 时返回 ``None``（开关隐藏）。"""
        try:
            return _engine().try_create()
        except Exception:  # noqa: BLE001 - 缺组件属正常降级，不打扰用户
            return None

    @staticmethod
    def _vpn_engine_ok() -> bool:
        """插件引擎是否带虚拟局域网能力（旧插件包没有 vpn_client）。"""
        try:
            return hasattr(_engine(), "try_create")
        except Exception:  # noqa: BLE001
            return False

    # ---------------------------------------------------------------- 布局
    def _build(self) -> None:
        from offcraft.ui.app import u

        root = QVBoxLayout(self)
        root.setContentsMargins(u(0.75), u(0.75), u(0.75), u(0.75))
        root.setSpacing(10)

        hint = QLabel(
            "把不同局域网的设备拉进同一虚拟房间：先用「创建房间」登记一个全局唯一的房间名，"
            "房主再把世界「对局域网开放」并「开放」该房间；加入者凭房间名与密码「加入」，"
            "游戏会自动进入房主世界。开启「虚拟局域网」后双方优先 UDP 直连、打洞失败自动走中继，"
            "游戏侧不再依赖本地端口映射。被踢出的设备无法换 sid 重入；"
            "房主点「重置密码」会清出所有成员，只有拿到新密码的人能回来。密码加密保存在本机（换机不可解）。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        root.addWidget(hint)

        form_card = Card(self, margins=(14, 12, 14, 12))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("房间名（全局唯一，不区分大小写）")
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("密码（仅本地派生密钥，加密存本机）")
        form.addRow("房间名", self.name_edit)
        form.addRow("密码", self.password_edit)
        form_card.add_layout(form)
        root.addWidget(form_card)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.create_button = QPushButton("创建房间")
        self.open_button = QPushButton("开放")
        self.open_button.setObjectName("primary")
        self.join_button = QPushButton("加入并启动")
        self.join_button.setObjectName("primary")
        self.delete_button = QPushButton("删除")
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("ghost")
        self.stop_button.setEnabled(False)
        self.rekey_button = QPushButton("重置密码")
        self.rekey_button.setObjectName("ghost")
        self.rekey_button.setEnabled(False)
        for button in (self.create_button, self.open_button, self.join_button,
                       self.delete_button, self.stop_button, self.rekey_button):
            button.setMinimumHeight(34)
            actions.addWidget(button)
        actions.addStretch(1)
        root.addLayout(actions)

        self.create_button.clicked.connect(self.on_create)
        self.open_button.clicked.connect(self.on_open)
        self.join_button.clicked.connect(self.on_join)
        self.delete_button.clicked.connect(self.on_delete)
        self.stop_button.clicked.connect(self.on_stop)
        self.rekey_button.clicked.connect(self.on_rekey)

        self.vpn_check = QCheckBox("虚拟局域网（UDP 直连优先，打洞失败自动中继）")
        self.vpn_check.setChecked(bool(getattr(self.window.config, "p2p_vpn", True)))
        if not (
            self._vpn_supported
            and (self._vpn_client is not None or self._vpn_installer is not None)
        ):
            # 还没加入布局时只做隐藏，避免 setVisible(True) 闪出一个独立小窗口
            self.vpn_check.hide()
        self.vpn_check.stateChanged.connect(self._on_vpn_toggle)
        root.addWidget(self.vpn_check)

        self.status_label = QLabel("未连接。")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f"color: {MUTED};")
        root.addWidget(self.status_label)

        self.vpn_label = QLabel("")
        self.vpn_label.setWordWrap(True)
        self.vpn_label.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        self.vpn_label.setVisible(False)
        root.addWidget(self.vpn_label)

        self.current_room_label = QLabel("当前房间：未连接。")
        self.current_room_label.setWordWrap(True)
        self.current_room_label.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        root.addWidget(self.current_room_label)

        self.rooms_label = QLabel("本设备已登记房间：读取中…")
        self.rooms_label.setWordWrap(True)
        self.rooms_label.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        root.addWidget(self.rooms_label)

        saved_row = QHBoxLayout()
        saved_title = QLabel("已进入的房间（双击可快速加入）")
        saved_title.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        saved_row.addWidget(saved_title)
        saved_row.addStretch(1)
        self.reveal_button = QPushButton("查看密码")
        self.reveal_button.setObjectName("ghost")
        self.reveal_button.setMinimumHeight(26)
        self.reveal_button.clicked.connect(self.on_reveal_password)
        saved_row.addWidget(self.reveal_button)
        self.forget_button = QPushButton("移除所选")
        self.forget_button.setObjectName("ghost")
        self.forget_button.setMinimumHeight(26)
        self.forget_button.clicked.connect(self.on_forget)
        saved_row.addWidget(self.forget_button)
        root.addLayout(saved_row)

        self.saved_list = QListWidget()
        self.saved_list.itemDoubleClicked.connect(self._on_saved_activate)
        root.addWidget(self.saved_list, 1)

        peers_row = QHBoxLayout()
        peers_title = QLabel("在线成员")
        peers_title.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        peers_row.addWidget(peers_title)
        peers_row.addStretch(1)
        self.kick_button = QPushButton("踢出所选")
        self.kick_button.setObjectName("ghost")
        self.kick_button.setMinimumHeight(26)
        self.kick_button.clicked.connect(self.on_kick)
        self.kick_button.setEnabled(False)
        peers_row.addWidget(self.kick_button)
        root.addLayout(peers_row)

        self.peers_list = QListWidget()
        root.addWidget(self.peers_list, 1)

        self._sync_buttons()

    # ---------------------------------------------------------------- 状态
    def _running(self) -> bool:
        return self._room is not None

    def _sync_buttons(self) -> None:
        running = self._running()
        for button in (self.create_button, self.open_button, self.join_button, self.delete_button):
            button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        # 踢出 / 重置密码仅房主、且处于会话中可用。
        self.kick_button.setEnabled(running and self._host)
        self.rekey_button.setEnabled(running and self._host)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _set_roster(self, peers: list[Any]) -> None:
        room = self._room
        host_sid = getattr(room, "host_sid", "") if room else ""
        my_sid = getattr(room, "sid", "") if room else ""
        self.peers_list.clear()
        if not peers:
            self.peers_list.addItem(QListWidgetItem("（暂无其它成员）"))
            return
        live = {}
        if self._vpn_status is not None:
            live = {p.sid: p for p in getattr(self._vpn_status, "peers", [])}
        for p in peers:
            pid = getattr(p, "pid", "") or ""
            short = pid[:8] if pid else "未知"
            if pid == host_sid:
                role = "房主"
            elif pid == my_sid:
                role = "成员（我）"
            else:
                role = "成员"
            detail = str(getattr(p, "vip", "") or "")
            status = live.get(pid)
            if status is not None:
                detail = _path_text(status)
            label = f"{role} · {short}"
            if detail:
                label += f" · {detail}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, pid)
            self.peers_list.addItem(item)

    # ---------------------------------------------------------------- 依赖
    def _device_id(self) -> str:
        from offcraft.config import save_config

        device = _engine().load_or_create_device_id(self.window.config)
        save_config(self.window.config, getattr(self.window, "data_dir", None))
        return device

    def _mirror(self):
        from offcraft.mirror import Mirror

        return Mirror(self.window.config.download_base)

    def _vault(self):
        return _vault_class()

    def _client(self, name: str, password: str, *, world: str = "", port: int = 0):
        RoomClient = _engine().RoomClient
        return RoomClient(
            downloader=self.window.make_downloader(progress=False),
            mirror=self._mirror(),
            name=name,
            password=password,
            device_id=self._device_id(),
            world=world,
            port=port,
        )

    # ---------------------------------------------------------------- 输入校验
    def _read_inputs(self, *, need_target: bool = True) -> tuple[str, str] | None:
        name = self.name_edit.text().strip()
        password = self.password_edit.text()
        if not name:
            self.window.notice("请填写房间名。", "WARN")
            return None
        if need_target and not password:
            self.window.notice("请填写房间密码。", "WARN")
            return None
        return name, password

    def _game_log_text(self) -> str:
        """尽力读取当前游戏目录的 logs/latest.log 末尾文本（拿不到返回空串）。"""
        try:
            from offcraft.ui.app import default_game_dir  # noqa: PLC0415

            log = default_game_dir() / "logs" / "latest.log"
        except Exception:  # noqa: BLE001 - 无相关入口就跳过日志探测
            return ""
        try:
            return _engine().read_log_tail(log)
        except Exception:  # noqa: BLE001
            return ""

    def _resolve_mc_port(self) -> int:
        """开放时的本机转发端口：方案 A（25565）为主、C（日志探测）兵底、否则默认 25565 并提示。"""
        try:
            port, source = _engine().detect_local_mc_port(prefer=25565, log_text=self._game_log_text())
        except Exception:  # noqa: BLE001 - 探测异常不阻断开放，用默认
            return 25565
        if source == "default":
            self.window.notice(
                "未在 25565 探测到监听，将按默认端口开放；请确认游戏内「对局域网开放」端口为 25565。",
                "WARN",
            )
        return port

    # ---------------------------------------------------------------- 动作
    def on_create(self) -> None:
        if self._running():
            return
        inputs = self._read_inputs()
        if inputs is None:
            return
        name, password = inputs
        self._set_status("正在登记房间……")

        def worker() -> None:
            try:
                resp = self._client(name, password).create()
                message = f"房间「{name}」已创建。" if resp.get("ok") else f"创建失败：{resp}"
            except Exception as exc:  # noqa: BLE001 - 后台兜底
                message = f"创建失败：{exc}"
            self.window.post(lambda m=message: (self._set_status(m), self._refresh_rooms()))

        threading.Thread(target=worker, name="p2p-create", daemon=True).start()

    def on_delete(self) -> None:
        if self._running():
            return
        inputs = self._read_inputs(need_target=False)
        if inputs is None:
            return
        name = inputs[0]
        self._set_status("正在删除房间……")

        def worker() -> None:
            try:
                resp = self._client(name, "x").delete()
                removed = bool(resp.get("ok"))
                if removed:
                    self._vault().remove(name)
                message = f"房间「{name}」已删除。" if removed else f"删除失败：{resp}"
            except Exception as exc:  # noqa: BLE001
                message = f"删除失败：{exc}"
            self.window.post(
                lambda m=message: (self._set_status(m), self._reload_saved(), self._refresh_rooms())
            )

        threading.Thread(target=worker, name="p2p-delete", daemon=True).start()

    def on_open(self) -> None:
        if self._running():
            return
        inputs = self._read_inputs()
        if inputs is None:
            return
        name, password = inputs
        port = self._resolve_mc_port()
        self._start_session(name, password, port, host=True)

    def on_join(self) -> None:
        if self._running():
            return
        inputs = self._read_inputs()
        if inputs is None:
            return
        name, password = inputs
        self._start_session(name, password, 0, host=False)

    def _start_session(self, name: str, password: str, port: int, *, host: bool) -> None:
        self.window.config.p2p_enabled = True
        self.window.config.p2p_last_room = name[:64]
        self._cancel = threading.Event()
        self._set_status("正在连接信令服务……")
        self._sync_buttons()
        threading.Thread(
            target=self._worker, args=(name, password, port, host),
            name="p2p-session", daemon=True,
        ).start()

    def _worker(self, name: str, password: str, port: int, host: bool) -> None:
        try:
            room = self._client(name, password, port=port)
            resp = room.open() if host else room.join()
            if self._cancel.is_set():
                room.leave()
                return
            self.window.post(lambda r=room, rp=resp, h=host: self._on_joined(r, rp, h))
        except Exception as exc:  # noqa: BLE001 - 后台兜底
            message = f"连接失败：{exc}"
            self.window.post(lambda m=message: self._on_failed(m))

    def _on_joined(self, room: Any, resp: dict, host: bool) -> None:
        self._room = room
        self._host = host
        self._vault().add(room.name, self.password_edit.text())
        self._reload_saved()
        self.current_room_label.setText(
            f"当前房间：{room.name}（{'房主' if host else '成员'}）"
        )
        self._set_roster(getattr(room, "peers", []))
        self._roster_timer.start()
        self._sync_buttons()
        if self._use_vpn(room):
            self._start_vpn(room, host)
        elif host:
            self._start_host_tunnel(room)
        else:
            self._start_join_tunnel(room)

    # ------------------------------------------------------------ 虚拟局域网
    def _use_vpn(self, room: Any) -> bool:
        """是否走虚拟局域网：组件可用 + 用户开启 + 服务端已下发虚拟 IP。

        中继令牌不是必要条件：只要两端能登记 UDP 端点，直连打洞不依赖边缘服务。
        """
        if not self._vpn_supported or not self.vpn_check.isChecked():
            return False
        if self._vpn_client is None and self._vpn_installer is None:
            return False
        if not getattr(room, "vip", ""):
            return False
        return bool(self._host or getattr(room, "host_vip", ""))

    def _on_vpn_toggle(self, _state: int) -> None:
        from offcraft.config import save_config

        self.window.config.p2p_vpn = self.vpn_check.isChecked()
        save_config(self.window.config, getattr(self.window, "data_dir", None))
        if self._running():
            self._set_status("虚拟局域网设置将在下次加入/开放时生效。")

    def _host_port(self, room: Any) -> int:
        """房主「对局域网开放」的游戏端口（取房主名册条目，缺失时回落 25565）。"""
        host_sid = getattr(room, "host_sid", "")
        for peer in getattr(room, "peers", []):
            if getattr(peer, "pid", "") == host_sid:
                port = int(getattr(peer, "port", 0) or 0)
                if port > 0:
                    return port
        return 25565

    @staticmethod
    def _edge_signature(room: Any) -> str:
        edge = getattr(room, "edge", None) or {}
        token = str(getattr(room, "edge_token", "") or "")
        if not token:
            return ""
        return f"{edge.get('host') or ''}:{int(edge.get('port') or 0)}:{token}"

    def _vpn_desired(self, room: Any) -> dict[str, Any]:
        """按当前名册组装 vpnd 配置：每对端独立单向密钥，密钥只在本地派生。"""
        engine = _engine()
        peers = []
        for peer in getattr(room, "peers", []):
            pid = getattr(peer, "pid", "") or ""
            if not pid or pid == getattr(room, "sid", ""):
                continue
            tx, rx = engine.derive_peer_keys(
                room.keys, is_host=self._host, self_sid=room.sid, peer_sid=pid
            )
            peers.append({
                "sid": pid,
                "vip": str(getattr(peer, "vip", "") or ""),
                "endpoint": str(getattr(peer, "udp", "") or ""),
                "tx_key": tx,
                "rx_key": rx,
            })
        edge = dict(getattr(room, "edge", None) or {})
        token = str(getattr(room, "edge_token", "") or "")
        if not token:
            edge = None       # 无令牌时只做直连打洞，不发注定失败的 HELLO
        else:
            edge["token"] = token
        return engine.build_config(vip=room.vip, peers=peers, edge=edge)

    @staticmethod
    def _config_key(config: dict[str, Any] | None) -> str:
        """配置内容指纹：忽略会热更新的边缘令牌，避免每 3 分钟重启一次数据面。"""
        if config is None:
            return ""
        copy = dict(config)
        edge = copy.get("edge")
        if edge:
            edge = dict(edge)
            edge["token"] = ""
            copy["edge"] = edge
        return json.dumps(copy, sort_keys=True, default=str)

    def _start_vpn(self, room: Any, host: bool) -> None:
        self._vpn_on = True
        self._vpn_busy = False
        self._vpn_config = None
        self._vpn_edge = ""
        self._vpn_status = None
        self.vpn_label.setVisible(True)
        self.vpn_label.setText("虚拟局域网：正在启用……")
        self._set_status("正在启用虚拟局域网……")
        threading.Thread(
            target=self._vpn_join_worker, args=(room, host, self._host_port(room)),
            name="p2p-vpn-join", daemon=True,
        ).start()

    def _ensure_vpn_installed(self) -> tuple[bool, str]:
        """首次启用：拉起一次 UAC 安装 vpnd，随后等待服务写出 IPC token。"""
        installer = self._vpn_installer
        if installer is None:
            return False, "缺少 VPN 安装组件"
        if not _run_elevated(installer):
            return False, "已取消管理员授权"
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            client = self._vpn_client_or_none()
            if client is not None:
                self._vpn_client = client
                return True, ""
            time.sleep(0.5)
        return False, "VPN 组件安装后未就绪"

    @staticmethod
    def _wait_vpn_path(client: Any, timeout: float) -> Any:
        """等到任一出口变成 direct/relay（打洞需要两端名册与 HELLO 互见，通常 1–4s）。"""
        deadline = time.monotonic() + timeout
        status = client.status()
        while time.monotonic() < deadline:
            paths = {getattr(p, "path", "") for p in getattr(status, "peers", [])}
            if paths & {"direct", "relay"}:
                break
            time.sleep(0.4)
            status = client.status()
        return status

    def _vpn_join_worker(self, room: Any, host: bool, port: int) -> None:
        client = self._vpn_client
        ok = False
        text = ""
        try:
            if client is None:
                installed, message = self._ensure_vpn_installed()
                if not installed:
                    raise RuntimeError(message)
                client = self._vpn_client
            client.connect()
            config = self._vpn_desired(room)
            reply = client.apply_config(config)
            self._vpn_config = config
            self._vpn_edge = self._edge_signature(room)
            status = client.status()
            ok = bool(reply.get("adapter")) and status.running
            if ok and not host:
                # 打洞/中继建立后再让游戏连服，避免 quickPlay 首次连接扑空
                status = self._wait_vpn_path(client, 10.0)
            text = _vpn_status_text(status)
        except Exception as exc:  # noqa: BLE001 - 数据面异常回落旧隧道
            if client is not None:
                client.close()
            text = f"虚拟局域网启用失败：{exc}"
        self.window.post(lambda: self._vpn_started(ok, text, room, host, port))

    def _vpn_started(self, ok: bool, text: str, room: Any, host: bool, port: int) -> None:
        if self._room is None:
            return
        if not ok:
            self._vpn_on = False
            self._vpn_status = None
            self.vpn_label.setVisible(False)
            client = self._vpn_client
            if client is not None:
                threading.Thread(target=self._vpn_stop_worker, args=(client,),
                                 name="p2p-vpn-cleanup", daemon=True).start()
            self.window.notice(f"{text}，已回落兼容隧道模式。", "WARN")
            if host:
                self._start_host_tunnel(room)
            else:
                self._start_join_tunnel(room)
            return
        self.vpn_label.setText(text)
        self._vpn_timer.start()
        if host:
            self._set_status("虚拟局域网已启用，等待成员加入。关闭此面板前请点「停止」。")
        else:
            address = f"{room.host_vip}:{port}"
            self._set_status(f"虚拟局域网已连接，正在进入游戏：{address}")
            self.window.join_server(address)

    def _refresh_vpn(self) -> None:
        """2s 一拍：刷新状态；仅当名册/端点/令牌变化时才下发配置（避免重启数据面）。"""
        room = self._room
        if room is None or not self._vpn_on or self._vpn_busy:
            return
        self._vpn_busy = True
        threading.Thread(target=self._vpn_worker, args=(room,),
                         name="p2p-vpn-sync", daemon=True).start()

    def _vpn_worker(self, room: Any) -> None:
        client = self._vpn_client
        status = None
        text = ""
        try:
            client.connect()
            status = client.status()
            if not status.running:
                # vpnd 被重启/停止：清缓存，下一拍重新下发完整配置
                self._vpn_config = None
                self._vpn_edge = ""
            edge = self._edge_signature(room)
            if edge and edge != self._vpn_edge and status.running:
                client.update_edge(dict(getattr(room, "edge", None) or {},
                                        token=room.edge_token))
                self._vpn_edge = edge
            desired = self._vpn_desired(room)
            if self._config_key(desired) != self._config_key(self._vpn_config):
                client.apply_config(desired)
                self._vpn_config = desired
                self._vpn_edge = self._edge_signature(room)
                status = client.status()
            text = _vpn_status_text(status)
        except Exception as exc:  # noqa: BLE001 - 断开后下次自动重连
            client.close()
            text = f"虚拟局域网异常：{exc}"
        self.window.post(lambda: self._finish_vpn_sync(text, status))

    def _finish_vpn_sync(self, text: str, status: Any) -> None:
        self._vpn_busy = False
        if self._room is None:
            return
        if status is not None:
            self._vpn_status = status
        self.vpn_label.setText(text)

    def _start_host_tunnel(self, room: Any) -> None:
        engine = _engine()
        tunnel = engine.HostTunnel(room, room.port, cancel=self._cancel)
        tunnel.start(on_event=self._host_event)
        self._tunnel = tunnel
        self._set_status("房间已开放，等待玩家加入。关闭此面板前请点「停止」。")

    def _start_join_tunnel(self, room: Any) -> None:
        engine = _engine()
        tunnel = engine.JoinTunnel(room, room.host_sid, cancel=self._cancel)
        tunnel.serve_forever()
        self._tunnel = tunnel
        address = f"localhost:{tunnel.port}"
        self._set_status(f"已进入房间，正在通过本地端口连接：{address}")
        self.window.join_server(address)

    def _on_failed(self, message: str) -> None:
        self._set_status(message)
        self._sync_buttons()

    def _host_event(self, status: str) -> None:
        """HostTunnel 的状态回调（后台线程）→ 主线程更新界面。"""
        def apply() -> None:
            if self._room is None:
                return
            if status == "expired":
                self.window.notice("房间已过期或被删除，联机会话已停止，请重新开放。", "WARN")
                self.on_stop()
            elif status == "error":
                self._set_status("信令连接不稳定，正在重试……")
            elif status == "ok":
                self._set_status("房间已开放，等待玩家加入。关闭此面板前请点「停止」。")

        self.window.post(apply)

    # ---------------------------------------------------------------- 房间额度
    def _refresh_rooms(self) -> None:
        if not getattr(self.window.config, "download_base", ""):
            self.rooms_label.setText("本设备已登记房间：未配置下载源")
            return

        def worker() -> None:
            try:
                resp = self._client("quota", "x").list_rooms()
                rooms = resp.get("rooms") or []
                remaining = resp.get("remaining")
                limit = resp.get("limit")
                names = "、".join(str(r.get("display") or r.get("name") or "") for r in rooms[:5])
                if len(rooms) > 5:
                    names += "…"
                text = f"本设备已登记 {len(rooms)}/{limit} 个房间"
                if names:
                    text += f"：{names}"
                if isinstance(remaining, int):
                    text += f"（还可创建 {remaining} 个）"
                self.window.post(lambda t=text: self.rooms_label.setText(t))
            except Exception as exc:  # noqa: BLE001 - 额度展示失败不影响联机
                self.window.post(lambda e=str(exc): self.rooms_label.setText(f"房间额度读取失败：{e}"))

        threading.Thread(target=worker, name="p2p-rooms", daemon=True).start()

    def on_rekey(self) -> None:
        """房主重置密码：清出所有成员，只有拿到新密码的设备能重新加入。"""
        room = self._room
        if room is None or not self._host:
            self.window.notice("仅房主在开放状态可重置密码。", "WARN")
            return
        new_password, ok = QInputDialog.getText(
            self, "重置房间密码", "新密码（会立即清出所有在线成员）：", QLineEdit.EchoMode.Password
        )
        if not ok or not new_password:
            return
        if new_password == self.password_edit.text():
            self.window.notice("新密码与当前密码相同，无需重置。", "WARN")
            return
        self._set_status("正在重置密码……")

        def worker() -> None:
            success = False
            try:
                resp = room.rekey(new_password)
                if resp.get("ok"):
                    success = True
                    removed = int(resp.get("removed") or 0)
                    message = f"密码已重置，已移出 {removed} 名成员。请把新密码发给要邀请的人。"
                else:
                    message = f"重置失败：{resp.get('error') or resp}"
            except Exception as exc:  # noqa: BLE001
                message = f"重置失败：{exc}"

            def apply() -> None:
                if success:
                    self.password_edit.setText(room.password)
                    self._vault().add(room.name, room.password)
                    self._reload_saved()
                    self._set_roster(getattr(room, "peers", []))
                self._set_status(message)

            self.window.post(apply)

        threading.Thread(target=worker, name="p2p-rekey", daemon=True).start()

    def on_stop(self) -> None:
        self._cancel.set()
        self._roster_timer.stop()
        self._vpn_timer.stop()
        self._vpn_on = False
        self._vpn_busy = False
        self._vpn_config = None
        self._vpn_edge = ""
        self._vpn_status = None
        self.vpn_label.setVisible(False)
        client = self._vpn_client
        if client is not None:
            threading.Thread(target=self._vpn_stop_worker, args=(client,),
                             name="p2p-vpn-stop", daemon=True).start()
        room, tunnel = self._room, self._tunnel
        self._room = self._tunnel = None
        self._host = False
        if tunnel is not None:
            try:
                tunnel.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        if room is not None:
            threading.Thread(target=room.leave, name="p2p-leave", daemon=True).start()
        self._set_status("已停止。")
        self.current_room_label.setText("当前房间：未连接。")
        self.peers_list.clear()
        self._sync_buttons()

    def _vpn_stop_worker(self, client: Any) -> None:
        """停止数据面（尽量而为：vpnd 未响应时不影响面板收尾）。"""
        try:
            client.stop()
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- 已存房间
    def _reload_saved(self) -> None:
        try:
            names = self._vault().names()
        except Exception:  # noqa: BLE001 - 存储不可用时仅清空列表
            names = []
        self.saved_list.clear()
        for name in names:
            self.saved_list.addItem(QListWidgetItem(name))

    def _on_saved_activate(self, item: QListWidgetItem) -> None:
        if self._running():
            self.window.notice("请先停止当前会话再加入其它房间。", "WARN")
            return
        name = item.text()
        try:
            password = self._vault().password(name)
        except Exception as exc:  # noqa: BLE001 - 换机后无法解密等
            self.window.notice(f"读取本地密码失败：{exc}", "WARN")
            return
        self.name_edit.setText(name)
        self.password_edit.setText(password)
        if password:
            self.on_join()

    def on_forget(self) -> None:
        item = self.saved_list.currentItem()
        if item is None:
            return
        self._vault().remove(item.text())
        self._reload_saved()

    # -------------------------------------------------------------- 在线名册/权限
    def _refresh_roster(self) -> None:
        """周期拉取当前会话在线名册（后台线程 + 回投）；重入保护避免堆叠请求。"""
        room = self._room
        if room is None or self._roster_busy:
            return
        self._roster_busy = True

        def worker() -> None:
            try:
                room.roster()
            except Exception as exc:  # noqa: BLE001 - 刷新失败等下一周期重试
                expired = "HTTP 404" in str(exc)

                def retry() -> None:
                    self._roster_busy = False
                    if expired and self._room is not None:
                        self.window.notice("房间已过期或已被删除，联机会话已停止。", "WARN")
                        self.on_stop()

                self.window.post(retry)
                return
            peers = list(getattr(room, "peers", []))

            def apply() -> None:
                self._roster_busy = False
                if self._room is not None:
                    self._set_roster(peers)
            self.window.post(apply)

        threading.Thread(target=worker, name="p2p-roster", daemon=True).start()

    def on_kick(self) -> None:
        """房主踢出选中在线成员：服务端鉴权 + 中继拆桥，本地名册稍后自动刷新。"""
        room = self._room
        if room is None or not self._host:
            self.window.notice("仅房主在开放状态可踢出成员。", "WARN")
            return
        item = self.peers_list.currentItem()
        if item is None:
            self.window.notice("请先在在线成员列表中选择要踢出的人。", "WARN")
            return
        target = item.data(Qt.ItemDataRole.UserRole)
        if not target:
            return
        if target == getattr(room, "sid", ""):
            self.window.notice("不能踢出自己。", "WARN")
            return
        self._set_status("正在踢出成员……")

        def worker() -> None:
            ok = False
            try:
                resp = room.kick(target)
                ok = bool(resp.get("ok"))
                message = "已踢出该成员。" if ok else f"踢出失败：{resp.get('error') or resp}"
            except Exception as exc:  # noqa: BLE001
                message = f"踢出失败：{exc}"

            def apply() -> None:
                if ok:
                    # UI 只能在主线程改：后台线程直接 _set_roster 会触碰 Qt 控件。
                    self._set_roster([p for p in getattr(room, "peers", [])
                                      if getattr(p, "pid", "") != target])
                self._set_status(message)

            self.window.post(apply)

        threading.Thread(target=worker, name="p2p-kick", daemon=True).start()

    def on_reveal_password(self) -> None:
        """查看本机已存房间的明文密码（机器绑定解密，仅本机可见）。"""
        item = self.saved_list.currentItem()
        if item is None:
            self.window.notice("请先选择要查看密码的房间。", "WARN")
            return
        name = item.text()
        try:
            password = self._vault().password(name)
        except Exception as exc:  # noqa: BLE001 - 换机/解密失败
            self.window.notice(f"读取本地密码失败：{exc}", "WARN")
            return
        if not password:
            self.window.notice("本机没有该房间的密码记录。", "WARN")
            return
        QMessageBox.information(self, "房间密码", f"房间「{name}」的密码：\n{password}")

    def shutdown(self) -> None:
        """面板销毁 / 退出时清理会话。"""
        self._roster_timer.stop()
        self.on_stop()
