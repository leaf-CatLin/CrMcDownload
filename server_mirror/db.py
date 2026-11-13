"""SQLite 热度榜与探测历史（标准库 sqlite3，写串行 + WAL）。

各表职责：

- ``servers``：榜单成员（内置种子 / 用户上报 / 接口导入），带热度累加字段；
- ``servers_state``：每个成员最近一次探测的完整快照（含 MOTD / 玩家列表 / 图标）；
- ``ping_history``：探测时间线，供延迟曲线与「近 24h 平均在线」热度分量使用；
- ``server_complaints``：用户手动举报的服务器，供管理端审核（状态 0/1/2）。

单写多读：整库共用一条连接 + 一把可重入锁串行写，开 WAL 让读不阻塞写。
"""

from __future__ import annotations

import hmac
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

__all__ = ["Database", "SCHEMA_VERSION"]

SCHEMA_VERSION = 4

#: 热度里「近 24h 平均在线」的统计窗口（秒）
_ONLINE_WINDOW = 24 * 3600
#: last_seen 的半衰加分窗口（秒）：刚见过的服务器最多再加这么多分
_RECENCY_WINDOW = 24 * 3600
_RECENCY_BONUS = 3.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS servers (
    host      TEXT    NOT NULL,
    port      INTEGER NOT NULL,
    label     TEXT    NOT NULL DEFAULT '',
    source    TEXT    NOT NULL DEFAULT 'builtin',   -- builtin|user|api
    added_by  TEXT    NOT NULL DEFAULT '',
    owner_hash TEXT   NOT NULL DEFAULT '',           -- 上报者管理密钥（SHA-256），仅用于修改/上下架/删除
    enabled   INTEGER NOT NULL DEFAULT 1,
    reports   INTEGER NOT NULL DEFAULT 0,
    last_seen REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (host, port)
);
CREATE TABLE IF NOT EXISTS ping_history (
    host       TEXT    NOT NULL,
    port       INTEGER NOT NULL,
    ts         REAL    NOT NULL,
    ok         INTEGER NOT NULL,
    latency_ms REAL,
    online     INTEGER,
    players_max INTEGER,
    version    TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_history_target ON ping_history (host, port, ts);
CREATE TABLE IF NOT EXISTS servers_state (
    host        TEXT    NOT NULL,
    port        INTEGER NOT NULL,
    ok          INTEGER NOT NULL DEFAULT 0,
    latency_ms  REAL,
    online      INTEGER NOT NULL DEFAULT 0,
    players_max INTEGER NOT NULL DEFAULT 0,
    version     TEXT    NOT NULL DEFAULT '',
    motd        TEXT    NOT NULL DEFAULT '',
    players_json TEXT   NOT NULL DEFAULT '[]',
    favicon     BLOB,
    error       TEXT    NOT NULL DEFAULT '',
    fail_count  INTEGER NOT NULL DEFAULT 0,
    updated_at  REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (host, port)
);
CREATE TABLE IF NOT EXISTS p2p_rooms (
    name       TEXT    NOT NULL PRIMARY KEY,   -- 规范化小写房间名，全局唯一
    display    TEXT    NOT NULL DEFAULT '',    -- 原始大小写展示名
    owner      TEXT    NOT NULL DEFAULT '',    -- 创建者 device_id
    created_at REAL    NOT NULL DEFAULT 0,
    last_open  REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_p2p_rooms_owner ON p2p_rooms (owner);
CREATE TABLE IF NOT EXISTS server_complaints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    host        TEXT    NOT NULL,
    port        INTEGER NOT NULL,
    reason      TEXT    NOT NULL DEFAULT '',
    detail      TEXT    NOT NULL DEFAULT '',
    contact     TEXT    NOT NULL DEFAULT '',
    client      TEXT    NOT NULL DEFAULT '',
    status      INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL DEFAULT 0,
    handled_at  REAL    NOT NULL DEFAULT 0,
    handled_by  TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_complaints_target ON server_complaints (host, port);
CREATE INDEX IF NOT EXISTS ix_complaints_status ON server_complaints (status, created_at);
"""


class Database:
    """镜像服务的联机数据层（线程安全）。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ) if self._table_exists("schema_version") else None
            row = cur.fetchone() if cur is not None else None
            current = int(row["version"]) if row else 0
            if current < SCHEMA_VERSION:
                self._conn.executescript(_SCHEMA)
                # 老库升级：CREATE TABLE IF NOT EXISTS 不会补列，这里显式加列
                if 0 < current < 4:
                    columns = {
                        str(row["name"])
                        for row in self._conn.execute("PRAGMA table_info(servers)")
                    }
                    if "owner_hash" not in columns:
                        self._conn.execute(
                            "ALTER TABLE servers ADD COLUMN owner_hash TEXT NOT NULL DEFAULT ''"
                        )
                if current == 0:
                    self._conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
                else:
                    self._conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
                self._conn.commit()

    def _table_exists(self, name: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        )
        return cur.fetchone() is not None

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            finally:
                self._conn.close()

    # ---------------------------------------------------------------- 成员
    def count_servers(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) c FROM servers").fetchone()["c"])

    def list_enabled(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT host, port, label, source FROM servers WHERE enabled=1"
                )
            )

    def upsert_server(
        self,
        host: str,
        port: int,
        *,
        label: str = "",
        source: str = "builtin",
        added_by: str = "",
        enabled: bool = True,
    ) -> None:
        host = str(host or "").strip().lower()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO servers(host, port, label, source, added_by, enabled)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(host, port) DO UPDATE SET
                    label     = CASE WHEN excluded.label <> '' THEN excluded.label ELSE servers.label END,
                    enabled   = MAX(servers.enabled, excluded.enabled),
                    source    = CASE WHEN servers.source='builtin' THEN excluded.source ELSE servers.source END
                """,
                (host, int(port), str(label or "").strip(), source, str(added_by or "")[:64], 1 if enabled else 0),
            )
            self._conn.commit()

    def report(
        self, host: str, port: int, label: str = "", added_by: str = "", owner_hash: str = ""
    ) -> str:
        """用户上报服务器。

        返回 ``"created"``（新建，且本次调用者就是管理员）/ ``"owned"``（本人重复上报，
        只更新备注）/ ``"exists"``（别人已收录，仅累加热度）。``owner_hash`` 是管理密钥的
        SHA-256，仅用于后续修改 / 上下架 / 删除鉴权，不对外返回。
        """
        host = str(host or "").strip().lower()
        owner_hash = str(owner_hash or "").strip()
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT owner_hash FROM servers WHERE host=? AND port=?", (host, int(port))
            ).fetchone()
            if row is not None:
                stored = str(row["owner_hash"] or "")
                if owner_hash and stored and hmac.compare_digest(stored, owner_hash):
                    self._conn.execute(
                        "UPDATE servers SET label=?, last_seen=? WHERE host=? AND port=?",
                        (str(label or "").strip(), now, host, int(port)),
                    )
                    self._conn.commit()
                    return "owned"
                self._conn.execute(
                    "UPDATE servers SET reports = reports + 1, last_seen = ?, "
                    "label = CASE WHEN label='' THEN ? ELSE label END WHERE host=? AND port=?",
                    (now, str(label or "").strip(), host, int(port)),
                )
                self._conn.commit()
                return "exists"
            self._conn.execute(
                "INSERT INTO servers(host, port, label, source, added_by, owner_hash, enabled, reports, last_seen)"
                " VALUES (?,?,?,'user',?,?,1,1,?)",
                (
                    host,
                    int(port),
                    str(label or "").strip(),
                    str(added_by or "")[:64],
                    owner_hash,
                    now,
                ),
            )
            self._conn.commit()
            return "created"

    def _owned(self, host: str, port: int, owner_hash: str) -> bool:
        """记录是否存在且管理密钥匹配（调用方需已持锁）。"""
        owner_hash = str(owner_hash or "").strip()
        if not owner_hash:
            return False
        row = self._conn.execute(
            "SELECT owner_hash FROM servers WHERE host=? AND port=?", (host, int(port))
        ).fetchone()
        if row is None:
            return False
        stored = str(row["owner_hash"] or "")
        return bool(stored) and hmac.compare_digest(stored, owner_hash)

    def update_owned(self, host: str, port: int, label: str, owner_hash: str) -> bool:
        """修改自己上报的服务器备注；没有权限返回 ``False``。"""
        host = str(host or "").strip().lower()
        with self._lock:
            if not self._owned(host, port, owner_hash):
                return False
            self._conn.execute(
                "UPDATE servers SET label=? WHERE host=? AND port=?",
                (str(label or "").strip()[:40], host, int(port)),
            )
            self._conn.commit()
            return True

    def set_visible_owned(self, host: str, port: int, enabled: bool, owner_hash: str) -> bool:
        """上架 / 下架自己上报的服务器；没有权限返回 ``False``。"""
        host = str(host or "").strip().lower()
        with self._lock:
            if not self._owned(host, port, owner_hash):
                return False
            self._conn.execute(
                "UPDATE servers SET enabled=? WHERE host=? AND port=?",
                (1 if enabled else 0, host, int(port)),
            )
            self._conn.commit()
            return True

    def delete_owned(self, host: str, port: int, owner_hash: str) -> bool:
        """删除自己上报的服务器及其探测数据；没有权限返回 ``False``。"""
        host = str(host or "").strip().lower()
        with self._lock:
            if not self._owned(host, port, owner_hash):
                return False
            self._conn.execute("DELETE FROM servers WHERE host=? AND port=?", (host, int(port)))
            self._conn.execute("DELETE FROM servers_state WHERE host=? AND port=?", (host, int(port)))
            self._conn.execute("DELETE FROM ping_history WHERE host=? AND port=?", (host, int(port)))
            self._conn.commit()
            return True

    def enabled_state(self, host: str, port: int) -> bool | None:
        """服务器当前是否上架；不存在返回 ``None``。"""
        host = str(host or "").strip().lower()
        with self._lock:
            row = self._conn.execute(
                "SELECT enabled FROM servers WHERE host=? AND port=?", (host, int(port))
            ).fetchone()
        return None if row is None else bool(row["enabled"])

    def set_enabled(self, host: str, port: int, enabled: bool) -> None:
        host = str(host or "").strip().lower()
        with self._lock:
            self._conn.execute(
                "UPDATE servers SET enabled=? WHERE host=? AND port=?",
                (1 if enabled else 0, host, int(port)),
            )
            self._conn.commit()

    # ---------------------------------------------------------------- 举报
    def add_complaint(
        self, host: str, port: int, reason: str, detail: str = "", contact: str = "", client: str = ""
    ) -> int:
        """登记一条用户举报，返回新记录 id（``created_at`` 取当前时间）。"""
        host = str(host or "").strip().lower()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO server_complaints(host, port, reason, detail, contact, client, created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    host,
                    int(port),
                    str(reason or "").strip()[:24],
                    str(detail or "").strip()[:500],
                    str(contact or "").strip()[:120],
                    str(client or "").strip()[:32],
                    time.time(),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_complaints(self, status: int | None = None, limit: int = 200) -> list[dict]:
        """按状态列出举报（``status=None`` 表示全部），新的在前。"""
        limit = max(0, int(limit))
        with self._lock:
            if status is None:
                rows = self._conn.execute(
                    "SELECT * FROM server_complaints ORDER BY created_at DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM server_complaints WHERE status=? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (int(status), limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def count_complaints(self, status: int | None = None) -> int:
        """统计举报总数；传入 ``status`` 只统计该状态。"""
        with self._lock:
            if status is None:
                row = self._conn.execute(
                    "SELECT COUNT(*) c FROM server_complaints"
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) c FROM server_complaints WHERE status=?",
                    (int(status),),
                ).fetchone()
        return int(row["c"])

    def set_complaint_status(self, complaint_id: int, status: int, handled_by: str = "") -> int:
        """更新举报处理状态并记录处理时间，返回受影响行数。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE server_complaints SET status=?, handled_at=?, handled_by=? WHERE id=?",
                (int(status), time.time(), str(handled_by or "").strip()[:64], int(complaint_id)),
            )
            self._conn.commit()
            return int(cur.rowcount)

    # -------------------------------------------------------- 离线联机房间（持久登记）
    def count_rooms(self, owner: str) -> int:
        owner = str(owner or "").strip()
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) c FROM p2p_rooms WHERE owner=?", (owner,)
                ).fetchone()["c"]
            )

    def create_room(self, name: str, display: str, owner: str, *, max_per_device: int) -> str:
        """登记一个新房间。返回 ``"ok"`` / ``"exists"``（名字全局重复）/ ``"quota"``（超每设备上限）。

        名字不区分大小写全局唯一（主键存小写）；密码 / auth_key **绝不入库**。
        先判名字冲突（409）再判配额（403）；配额只算该设备**现存**（未删除）房间。
        """
        key = str(name or "").strip().lower()
        owner = str(owner or "").strip()
        now = time.time()
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM p2p_rooms WHERE name=?", (key,)
            ).fetchone() is not None:
                return "exists"
            used = int(
                self._conn.execute(
                    "SELECT COUNT(*) c FROM p2p_rooms WHERE owner=?", (owner,)
                ).fetchone()["c"]
            )
            if used >= max(0, int(max_per_device)):
                return "quota"
            try:
                self._conn.execute(
                    "INSERT INTO p2p_rooms(name, display, owner, created_at, last_open)"
                    " VALUES (?,?,?,?,?)",
                    (key, str(display or name or "").strip()[:64], owner, now, 0),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                return "exists"
            return "ok"

    def get_room(self, name: str) -> dict | None:
        key = str(name or "").strip().lower()
        with self._lock:
            row = self._conn.execute(
                "SELECT name, display, owner, created_at, last_open FROM p2p_rooms WHERE name=?",
                (key,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_rooms(self, owner: str) -> list[dict]:
        owner = str(owner or "").strip()
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, display, created_at, last_open FROM p2p_rooms WHERE owner=?"
                " ORDER BY created_at DESC",
                (owner,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_room(self, name: str, owner: str) -> bool:
        key = str(name or "").strip().lower()
        owner = str(owner or "").strip()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM p2p_rooms WHERE name=? AND owner=?", (key, owner)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def touch_open(self, name: str) -> None:
        key = str(name or "").strip().lower()
        with self._lock:
            self._conn.execute(
                "UPDATE p2p_rooms SET last_open=? WHERE name=?", (time.time(), key)
            )
            self._conn.commit()

    # ---------------------------------------------------------------- 探测
    def record_ping(
        self,
        host: str,
        port: int,
        *,
        ok: bool,
        ts: float | None = None,
        latency_ms: float | None = None,
        online: int | None = None,
        players_max: int | None = None,
        version: str = "",
        motd: str = "",
        players: Iterable[dict[str, Any]] | None = None,
        favicon: bytes | None = None,
        error: str = "",
    ) -> None:
        host = str(host or "").strip().lower()
        ts = ts if ts is not None else time.time()
        players_json = json.dumps(list(players or []), ensure_ascii=False)
        with self._lock:
            fail_delta = 0 if ok else 1
            self._conn.execute(
                """
                INSERT INTO ping_history(host, port, ts, ok, latency_ms, online, players_max, version)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (host, int(port), ts, 1 if ok else 0, latency_ms, online, players_max, version[:120]),
            )
            self._conn.execute(
                """
                INSERT INTO servers_state(host, port, ok, latency_ms, online, players_max, version,
                                         motd, players_json, favicon, error, fail_count, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(host, port) DO UPDATE SET
                    ok=excluded.ok, latency_ms=excluded.latency_ms, online=excluded.online,
                    players_max=excluded.players_max, version=excluded.version, motd=excluded.motd,
                    players_json=excluded.players_json,
                    favicon=CASE WHEN excluded.favicon IS NULL THEN servers_state.favicon ELSE excluded.favicon END,
                    error=excluded.error,
                    fail_count=CASE WHEN excluded.ok=1 THEN 0 ELSE servers_state.fail_count+1 END,
                    updated_at=excluded.updated_at
                """,
                (
                    host, int(port), 1 if ok else 0, latency_ms, online or 0, players_max or 0,
                    version[:120], motd[:512], players_json, favicon, error[:256] if not ok else "",
                    fail_delta, ts,
                ),
            )
            if ok:
                self._conn.execute(
                    "UPDATE servers SET last_seen=? WHERE host=? AND port=?", (ts, host, int(port))
                )
            self._conn.commit()

    def prune_history(self, keep_days: int = 7) -> int:
        cutoff = time.time() - keep_days * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM ping_history WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    # ---------------------------------------------------------------- 查询
    def _avg_online(self, host: str, port: int, now: float) -> float:
        row = self._conn.execute(
            "SELECT AVG(online) a FROM ping_history "
            "WHERE host=? AND port=? AND ok=1 AND ts >= ?",
            (host, int(port), now - _ONLINE_WINDOW),
        ).fetchone()
        return float(row["a"] or 0.0) if row else 0.0

    def _hot_score(self, reports: int, last_seen: float, avg_online: float, now: float) -> float:
        recency = 0.0
        age = now - (last_seen or 0.0)
        if age < _RECENCY_WINDOW:
            recency = (_RECENCY_WINDOW - age) / _RECENCY_WINDOW * _RECENCY_BONUS
        return reports + avg_online / 50.0 + recency

    def rank(
        self, query: str = "", *, limit: int = 20, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        """热度榜（``query`` 匹配 host / label / motd / version）。返回 ``(条目, 总数)``。"""
        now = time.time()
        q = str(query or "").strip().lower()
        like = f"%{q}%"
        with self._lock:
            base = """
                FROM servers s
                LEFT JOIN servers_state st ON st.host = s.host AND st.port = s.port
            """
            where = "WHERE s.enabled=1"
            params: list[Any] = []
            if q:
                where += " AND (lower(s.host) LIKE ? OR lower(s.label) LIKE ? OR lower(st.motd) LIKE ? OR lower(st.version) LIKE ?)"
                params += [like, like, like, like]
            total = int(self._conn.execute(f"SELECT COUNT(*) c {base} {where}", params).fetchone()["c"])
            rows = self._conn.execute(
                f"""
                SELECT s.host, s.port, s.label, s.source, s.reports, s.last_seen,
                       st.ok, st.latency_ms, st.online, st.players_max, st.version,
                       st.motd, st.players_json, st.error, st.updated_at
                {base} {where}
                """,
                params,
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            avg = self._avg_online(row["host"], row["port"], now)
            item["hot"] = self._hot_score(int(row["reports"] or 0), float(row["last_seen"] or 0.0), avg, now)
            item["players"] = _safe_json_list(item.get("players_json"))
            item.pop("players_json", None)
            out.append(item)
        # 热度优先，同分按在线人数降序，再按延迟升序
        out.sort(key=lambda r: (-r["hot"], -(r.get("online") or 0), r.get("latency_ms") or 9e9))
        return out[offset : offset + limit], total

    def detail(self, host: str, port: int) -> dict[str, Any] | None:
        host = str(host or "").strip().lower()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT s.host, s.port, s.label, s.source, s.reports, s.last_seen,
                       st.ok, st.latency_ms, st.online, st.players_max, st.version,
                       st.motd, st.players_json, st.error, st.updated_at
                FROM servers s LEFT JOIN servers_state st ON st.host=s.host AND st.port=s.port
                WHERE s.host=? AND s.port=?
                """,
                (host, int(port)),
            ).fetchone()
            if row is None:
                return None
            history = self._conn.execute(
                "SELECT ts, ok, latency_ms, online FROM ping_history "
                "WHERE host=? AND port=? ORDER BY ts DESC LIMIT 24",
                (host, int(port)),
            ).fetchall()
        item = dict(row)
        item["players"] = _safe_json_list(item.get("players_json"))
        item["history"] = [dict(h) for h in reversed(history)]
        return item

    def overview(self) -> dict[str, Any]:
        with self._lock:
            total = int(self._conn.execute("SELECT COUNT(*) c FROM servers WHERE enabled=1").fetchone()["c"])
            online = int(
                self._conn.execute(
                    "SELECT COUNT(*) c FROM servers_state WHERE ok=1"
                ).fetchone()["c"]
            )
            players = int(
                self._conn.execute(
                    "SELECT COALESCE(SUM(online),0) s FROM servers_state WHERE ok=1"
                ).fetchone()["s"]
            )
        return {"servers": total, "online_servers": online, "players": players}


def _safe_json_list(text: Any) -> list[dict[str, Any]]:
    """``players_json`` 列回读：坏数据退化成空列表，不影响榜单。"""
    try:
        data = json.loads(text) if isinstance(text, str) else (text or [])
    except (TypeError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
