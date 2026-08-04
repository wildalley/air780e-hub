"""Server database.

SQLite, one file under the data volume.  Sync ``sqlite3`` guarded by a lock:
FastAPI runs ``def`` endpoints in a threadpool, and the WebSocket gateway's
writes are sub-millisecond, so an async driver would add moving parts without
buying throughput at this scale.

Schema note (PLAN.md section 5): messages, tasks and rules all hang off
``sims``, not off devices.  Swapping a card into the other module must not
orphan its history — that is the concrete lesson from SimAdmin's
single-modem model.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

# Persisted (settings table) key for the SMS retention window, in days.  The
# operator edits it on the Notify page; when unset the env default applies.
SETTING_MESSAGE_RETENTION_DAYS = "message_retention_days"

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS agents (
    id           TEXT PRIMARY KEY,
    version      TEXT NOT NULL DEFAULT '',
    last_seen_at TEXT,
    connected    INTEGER NOT NULL DEFAULT 0,
    last_seq     INTEGER NOT NULL DEFAULT 0
);

-- Every event the agent sends is recorded here before it is applied, so a
-- replay after a lost ack cannot duplicate a message.
CREATE TABLE IF NOT EXISTS ingested (
    agent_id TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    at       TEXT NOT NULL,
    PRIMARY KEY (agent_id, seq)
);

CREATE TABLE IF NOT EXISTS sims (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    iccid         TEXT UNIQUE NOT NULL,
    label         TEXT NOT NULL DEFAULT '',
    phone_number  TEXT NOT NULL DEFAULT '',
    operator      TEXT NOT NULL DEFAULT '',
    smsc          TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id     TEXT NOT NULL,
    name         TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    port         TEXT NOT NULL DEFAULT '',
    sim_id       INTEGER REFERENCES sims(id) ON DELETE SET NULL,
    online       INTEGER NOT NULL DEFAULT 0,
    registered   INTEGER NOT NULL DEFAULT 0,
    model        TEXT NOT NULL DEFAULT '',
    imei         TEXT NOT NULL DEFAULT '',
    operator     TEXT NOT NULL DEFAULT '',
    rssi         INTEGER,
    dbm          INTEGER,
    bars         INTEGER NOT NULL DEFAULT 0,
    rsrp         INTEGER,
    rsrq         INTEGER,
    storage_used INTEGER NOT NULL DEFAULT 0,
    storage_cap  INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT,
    UNIQUE (agent_id, name)
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   TEXT NOT NULL,
    device     TEXT NOT NULL,
    sim_id     INTEGER REFERENCES sims(id) ON DELETE SET NULL,
    direction  TEXT NOT NULL,
    peer       TEXT NOT NULL,
    body       TEXT NOT NULL,
    ts         TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'received',
    segments   INTEGER NOT NULL DEFAULT 1,
    seq        INTEGER,
    error      TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_sim ON messages(sim_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_peer ON messages(peer, ts DESC);

CREATE TABLE IF NOT EXISTS device_status (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    ts           TEXT NOT NULL,
    online       INTEGER NOT NULL DEFAULT 0,
    registered   INTEGER NOT NULL DEFAULT 0,
    rssi         INTEGER,
    dbm          INTEGER,
    bars         INTEGER,
    rsrp         INTEGER,
    rsrq         INTEGER,
    storage_used INTEGER,
    storage_cap  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_status_device_ts ON device_status(device_id, ts DESC);

CREATE TABLE IF NOT EXISTS channels (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    type    TEXT NOT NULL,
    config  TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL DEFAULT '',
    sim_id     INTEGER REFERENCES sims(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    match      TEXT NOT NULL DEFAULT 'all',
    pattern    TEXT NOT NULL DEFAULT '',
    template   TEXT NOT NULL DEFAULT '',
    priority   INTEGER NOT NULL DEFAULT 0,
    enabled    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS notify_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    channel_id INTEGER REFERENCES channels(id) ON DELETE SET NULL,
    rule_id    INTEGER REFERENCES rules(id) ON DELETE SET NULL,
    status     TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 1,
    detail     TEXT NOT NULL DEFAULT '',
    ts         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notify_ts ON notify_logs(ts DESC);

CREATE TABLE IF NOT EXISTS tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL DEFAULT '',
    sim_id           INTEGER REFERENCES sims(id) ON DELETE CASCADE,
    device           TEXT NOT NULL DEFAULT '',
    agent_id         TEXT NOT NULL DEFAULT '',
    enabled          INTEGER NOT NULL DEFAULT 1,
    action           TEXT NOT NULL DEFAULT 'send_sms',
    target_number    TEXT NOT NULL DEFAULT '10086',
    content          TEXT NOT NULL DEFAULT '1',
    schedule_type    TEXT NOT NULL DEFAULT 'interval',
    schedule_expr    TEXT NOT NULL DEFAULT '25',
    jitter_seconds   INTEGER NOT NULL DEFAULT 1800,
    random_suffix    INTEGER NOT NULL DEFAULT 1,
    retry_max        INTEGER NOT NULL DEFAULT 3,
    notify_on_result INTEGER NOT NULL DEFAULT 1,
    last_run_at      TEXT,
    next_run_at      TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_logs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id  INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    ts       TEXT NOT NULL,
    status   TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    detail   TEXT NOT NULL DEFAULT '',
    error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_logs_ts ON task_logs(ts DESC);

CREATE TABLE IF NOT EXISTS agent_logs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    device   TEXT NOT NULL DEFAULT '',
    level    TEXT NOT NULL DEFAULT 'info',
    message  TEXT NOT NULL,
    ts       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_logs_ts ON agent_logs(ts DESC);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    label      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_utc_iso(value: str | None) -> str:
    """Normalise an incoming timestamp to UTC ISO-8601.

    Timestamps arrive from the agent in local time with an offset (the SCTS a
    Chinese carrier stamps looks like ``2026-08-02T18:00:00+08:00``).  Every
    range query here — history windows, retention, ordering — is a *string*
    comparison, so mixed offsets would sort as if they were all the same zone
    and a "-24 hours" window would silently include or drop rows.  Normalising
    once at ingest makes every later comparison correct by construction.
    """
    if not value:
        return utcnow()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return utcnow()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- low level ---------------------------------------------------------

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, params)

    # -- idempotency -------------------------------------------------------

    def claim_event(self, agent_id: str, seq: int, kind: str) -> bool:
        """Record an event's arrival.

        Returns False if this ``(agent_id, seq)`` was already applied — the
        agent replays after a lost ack, so duplicates are expected, not a bug.
        """
        with self._lock:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO ingested (agent_id, seq, kind, at) "
                "VALUES (?, ?, ?, ?)",
                (agent_id, seq, kind, utcnow()),
            )
            return cursor.rowcount > 0

    # -- agents and devices ------------------------------------------------

    def upsert_agent(self, agent_id: str, version: str, connected: bool) -> None:
        self.execute(
            "INSERT INTO agents (id, version, last_seen_at, connected) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET version = excluded.version, "
            "last_seen_at = excluded.last_seen_at, connected = excluded.connected",
            (agent_id, version, utcnow(), int(connected)),
        )

    def set_agent_connected(self, agent_id: str, connected: bool) -> None:
        self.execute(
            "UPDATE agents SET connected = ?, last_seen_at = ? WHERE id = ?",
            (int(connected), utcnow(), agent_id),
        )

    def mark_all_agents_disconnected(self) -> None:
        """On startup nothing is connected yet, whatever the last run thought."""
        self.execute("UPDATE agents SET connected = 0")
        self.execute("UPDATE devices SET online = 0")

    def upsert_sim(
        self,
        iccid: str,
        *,
        operator: str = "",
        smsc: str = "",
    ) -> int | None:
        """Get (or create) the SIM row for an ICCID.

        A module with no card reports an empty ICCID; that is not a SIM.
        """
        if not iccid:
            return None
        now = utcnow()
        with self._lock:
            self._db.execute(
                "INSERT INTO sims (iccid, operator, smsc, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(iccid) DO UPDATE SET last_seen_at = excluded.last_seen_at",
                (iccid, operator, smsc, now, now),
            )
            # Only fill operator/smsc when we actually learned something, so a
            # transient blank reading never wipes a good value.
            if operator:
                self._db.execute(
                    "UPDATE sims SET operator = ? WHERE iccid = ?", (operator, iccid)
                )
            if smsc:
                self._db.execute(
                    "UPDATE sims SET smsc = ? WHERE iccid = ?", (smsc, iccid)
                )
            row = self._db.execute(
                "SELECT id FROM sims WHERE iccid = ?", (iccid,)
            ).fetchone()
        return int(row["id"]) if row else None

    # Fields a status event may legitimately set to a falsy value (0 signal,
    # offline, empty store) — absence of the key is what means "unchanged".
    _DEVICE_STATE_FIELDS = (
        "online", "registered", "rssi", "dbm", "bars", "rsrp", "rsrq",
    )
    # Identity fields.  A blank here always means "this frame didn't carry it",
    # never "the module lost its IMEI" — status frames are a subset of hello,
    # so overwriting on blank would erase the card's name every 60 seconds.
    _DEVICE_IDENTITY_FIELDS = ("label", "port", "model", "imei", "operator")

    def upsert_device(self, agent_id: str, payload: dict[str, Any]) -> int:
        name = payload.get("name", "")
        now = utcnow()

        columns: dict[str, Any] = {"agent_id": agent_id, "name": name, "last_seen_at": now}

        for field in self._DEVICE_STATE_FIELDS:
            if field in payload:
                value = payload[field]
                columns[field] = (
                    int(bool(value)) if field in ("online", "registered") else value
                )
        if "bars" in payload:
            columns["bars"] = payload.get("bars") or 0
        if "storage_used" in payload:
            columns["storage_used"] = payload.get("storage_used") or 0
        if "storage_capacity" in payload:
            columns["storage_cap"] = payload.get("storage_capacity") or 0

        for field in self._DEVICE_IDENTITY_FIELDS:
            value = payload.get(field)
            if value:
                columns[field] = value

        iccid = payload.get("iccid") or ""
        if iccid:
            columns["sim_id"] = self.upsert_sim(
                iccid,
                operator=payload.get("operator", "") or "",
                smsc=payload.get("smsc", "") or "",
            )

        names = ",".join(columns)
        placeholders = ",".join(f":{key}" for key in columns)
        updates = ",".join(
            f"{key}=excluded.{key}" for key in columns if key not in ("agent_id", "name")
        )
        with self._lock:
            self._db.execute(
                f"INSERT INTO devices ({names}) VALUES ({placeholders}) "
                f"ON CONFLICT(agent_id, name) DO UPDATE SET {updates}",
                columns,
            )
            row = self._db.execute(
                "SELECT id FROM devices WHERE agent_id = ? AND name = ?",
                (agent_id, name),
            ).fetchone()
        return int(row["id"])

    def set_devices_offline(self, agent_id: str) -> None:
        self.execute("UPDATE devices SET online = 0 WHERE agent_id = ?", (agent_id,))

    def device_online(self, agent_id: str, name: str) -> bool | None:
        """Current online flag, or None if the module has never been seen.

        The offline alerter re-reads this when a grace timer fires, so a module
        that came back during the wait is not paged as down.
        """
        row = self.one(
            "SELECT online FROM devices WHERE agent_id = ? AND name = ?",
            (agent_id, name),
        )
        return None if row is None else bool(row["online"])

    def device_id(self, agent_id: str, name: str) -> int | None:
        row = self.one(
            "SELECT id FROM devices WHERE agent_id = ? AND name = ?", (agent_id, name)
        )
        return int(row["id"]) if row else None

    def record_status(self, device_id: int, payload: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO device_status "
            "(device_id, ts, online, registered, rssi, dbm, bars, rsrp, rsrq, "
            " storage_used, storage_cap) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                device_id,
                to_utc_iso(payload.get("ts")),
                int(bool(payload.get("online"))),
                int(bool(payload.get("registered"))),
                payload.get("rssi"),
                payload.get("dbm"),
                payload.get("bars"),
                payload.get("rsrp"),
                payload.get("rsrq"),
                payload.get("storage_used"),
                payload.get("storage_capacity"),
            ),
        )

    # -- messages ----------------------------------------------------------

    def insert_message(
        self,
        *,
        agent_id: str,
        device: str,
        direction: str,
        peer: str,
        body: str,
        ts: str,
        iccid: str = "",
        status: str = "received",
        segments: int = 1,
        seq: int | None = None,
        error: str | None = None,
    ) -> int:
        sim_id = self.upsert_sim(iccid)
        cursor = self.execute(
            "INSERT INTO messages "
            "(agent_id, device, sim_id, direction, peer, body, ts, status, "
            " segments, seq, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent_id, device, sim_id, direction, peer, body, to_utc_iso(ts), status,
             segments, seq, error, utcnow()),
        )
        return int(cursor.lastrowid)

    def conversations(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Threads: one row per (card, correspondent), newest activity first.

        The bare ``m.body`` / ``m.direction`` / ``m.id`` columns alongside
        ``MAX(m.ts)`` are SQLite's documented min/max behaviour — they come
        from the row that produced the maximum, which is exactly the preview
        we want.  This is not portable SQL; on another engine it needs a
        window function.
        """
        return self.query(
            "SELECT m.sim_id, m.peer, m.device, "
            "       m.id AS last_id, m.body AS last_body, "
            "       m.direction AS last_direction, m.status AS last_status, "
            "       MAX(m.ts) AS last_ts, COUNT(*) AS message_count, "
            "       s.label AS sim_label, s.iccid AS sim_iccid "
            "FROM messages m LEFT JOIN sims s ON s.id = m.sim_id "
            "GROUP BY m.sim_id, m.peer "
            "ORDER BY last_ts DESC LIMIT ?",
            (limit,),
        )

    def messages(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        sim_id: int | None = None,
        direction: str | None = None,
        peer: str | None = None,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if sim_id is not None:
            clauses.append("m.sim_id = ?")
            params.append(sim_id)
        if direction:
            clauses.append("m.direction = ?")
            params.append(direction)
        if peer:
            clauses.append("m.peer = ?")
            params.append(peer)
        if search:
            clauses.append("(m.body LIKE ? OR m.peer LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        return self.query(
            "SELECT m.*, s.label AS sim_label, s.iccid AS sim_iccid "
            "FROM messages m LEFT JOIN sims s ON s.id = m.sim_id "
            f"{where} ORDER BY m.ts DESC, m.id DESC LIMIT ? OFFSET ?",
            tuple(params),
        )

    def count_messages(self, **filters: Any) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM messages")
        return int(row["n"]) if row else 0

    # -- retention ---------------------------------------------------------

    def purge(self, *, message_days: int, status_days: int) -> dict[str, int]:
        """Delete aged rows.  Verification codes should not live forever."""
        removed = {"messages": 0, "status": 0, "ingested": 0}
        if message_days > 0:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=message_days)
            ).isoformat()
            removed["messages"] = self.execute(
                "DELETE FROM messages WHERE ts < ?", (cutoff,)
            ).rowcount
        if status_days > 0:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=status_days)
            ).isoformat()
            removed["status"] = self.execute(
                "DELETE FROM device_status WHERE ts < ?", (cutoff,)
            ).rowcount
        # Idempotency records only need to outlive a plausible replay window.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        removed["ingested"] = self.execute(
            "DELETE FROM ingested WHERE at < ?", (cutoff,)
        ).rowcount
        return removed

    # -- backup and restore ------------------------------------------------
    # Tables a genuine hub backup must contain.  A sanity gate so an arbitrary
    # .sqlite — or a truncated upload — is rejected before it ever overwrites
    # the live data.
    _REQUIRED_TABLES = frozenset(
        {"agents", "sims", "devices", "messages", "settings"}
    )

    def backup_to(self, dest: str | Path) -> None:
        """Write a consistent snapshot of the whole database to *dest*.

        Uses SQLite's online-backup API, so the copy is coherent — pending WAL
        frames included — even while the gateway is writing, without holding a
        write lock for the length of the copy.
        """
        target = sqlite3.connect(dest)
        try:
            with self._lock:
                self._db.backup(target)
        finally:
            target.close()

    @staticmethod
    def validate_backup(path: str | Path) -> None:
        """Raise ValueError unless *path* is a readable hub SQLite database.

        Runs an integrity check and confirms the core tables are present, so a
        corrupt or unrelated file is caught before ``restore_from`` copies it
        over live data.  Opened read-only — validation never mutates the upload.
        """
        try:
            conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
        except sqlite3.Error as exc:  # pragma: no cover - connect rarely fails
            raise ValueError(f"无法打开备份文件: {exc}") from exc
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                raise ValueError("备份文件未通过完整性校验,可能已损坏")
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"文件不是有效的 SQLite 数据库: {exc}") from exc
        finally:
            conn.close()
        missing = self._REQUIRED_TABLES - names
        if missing:
            raise ValueError(
                "这不是有效的 Hub 备份(缺少表: "
                + ", ".join(sorted(missing))
                + ")"
            )

    def restore_from(self, src: str | Path) -> None:
        """Replace this database's contents with those of the file at *src*.

        The upload is copied *into* the live connection with the backup API
        rather than swapped on disk, so every open cursor and the WAL stay
        valid and no process restart is needed.  Caller must have run
        ``validate_backup`` first.
        """
        source = sqlite3.connect(src)
        try:
            with self._lock:
                source.backup(self._db)
        finally:
            source.close()

    # -- settings ----------------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return row["value"]

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
