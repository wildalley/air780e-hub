"""Server database.

SQLite, one file under the data volume.  Sync ``sqlite3`` guarded by a lock:
FastAPI runs ``def`` endpoints in a threadpool, and the WebSocket gateway's
writes are sub-millisecond, so an async driver would add moving parts without
buying throughput at this scale.

Schema invariant: messages, tasks and rules all hang off
``sims``, not off devices.  Swapping a card into the other module must not
orphan its history — that is the concrete lesson from SimAdmin's
single-modem model.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Schema version, held in SQLite's own ``PRAGMA user_version``.
#
# Version 1 is the schema as of 2026-08-08 — every table in SCHEMA plus the two
# columns that used to be patched in by unversioned ALTERs (``messages.read_at``
# and ``devices.radio_enabled``).  Databases created before versioning read back
# as 0 and are reconciled to 1 on first open; see ``Database._migrate``.
#
# To add a migration: append one entry to ``MIGRATIONS`` with the next integer
# and bump this constant.  Never renumber or edit a released entry — a database
# that already ran it will not run it again, so an edit only affects databases
# that have not, and the two then disagree about what version N means.
SCHEMA_VERSION = 1

# Persisted (settings table) key for the SMS retention window, in days.  The
# operator edits it on the Notify page; when unset the env default applies.
SETTING_MESSAGE_RETENTION_DAYS = "message_retention_days"


class SchemaTooNew(RuntimeError):
    """The database was written by a newer Server than this one.

    Refusing beats proceeding: a newer schema may hold columns and tables this
    build never writes, and letting it run would silently drop whatever the
    newer Server was persisting.  Downgrades restore a backup instead.
    """


class MigrationFailed(RuntimeError):
    """A migration raised and was rolled back; the snapshot path is attached."""

    def __init__(self, message: str, *, snapshot: Path | None = None) -> None:
        super().__init__(message)
        self.snapshot = snapshot


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
    radio_enabled INTEGER,
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
    read_at    TEXT,
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

CREATE TABLE IF NOT EXISTS audit_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    action    TEXT NOT NULL,
    target    TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL DEFAULT 'ok',
    detail    TEXT NOT NULL DEFAULT '',
    client_ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_events_ts ON audit_events(ts DESC);

CREATE TABLE IF NOT EXISTS incidents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint     TEXT UNIQUE NOT NULL,
    kind            TEXT NOT NULL,
    severity        TEXT NOT NULL DEFAULT 'warning',
    source          TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'active',
    occurrences     INTEGER NOT NULL DEFAULT 1,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    acknowledged_at TEXT,
    resolved_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_status_seen
    ON incidents(status, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_resolved_at
    ON incidents(resolved_at);

-- The log tables are read newest-first and pruned oldest-first; both the API
-- queries and retention rely on these.
CREATE INDEX IF NOT EXISTS idx_agent_logs_ts ON agent_logs(ts);
CREATE INDEX IF NOT EXISTS idx_task_logs_ts ON task_logs(ts);
CREATE INDEX IF NOT EXISTS idx_notify_logs_ts ON notify_logs(ts);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # Whether this file already held a schema *before* we touched it.  Read
        # it now: ``executescript(SCHEMA)`` creates every table, after which a
        # brand-new file is indistinguishable from an existing one, and only an
        # existing one has data worth snapshotting before a migration.
        pre_existing = self._has_schema()
        self._db = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._migrate(pre_existing=pre_existing)

    def _has_schema(self) -> bool:
        """True if the file exists and already contains hub tables."""
        if str(self.path) == ":memory:" or not self.path.exists():
            return False
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        except sqlite3.Error:
            return False
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'devices'"
            ).fetchone()
            return row is not None
        except sqlite3.DatabaseError:
            return False
        finally:
            conn.close()

    # -- schema versioning -------------------------------------------------

    def _user_version(self) -> int:
        return int(self._db.execute("PRAGMA user_version").fetchone()[0])

    def _set_user_version(self, version: int) -> None:
        # PRAGMA takes no parameters, hence the interpolation; the value is an
        # int from MIGRATIONS, never external input.
        self._db.execute(f"PRAGMA user_version = {int(version)}")

    def _reconcile_to_baseline(self) -> None:
        """Bring a pre-versioning database up to schema version 1.

        Kept column-by-column and idempotent because version 0 is not one known
        state: it is every database created before versioning existed, and any
        of these columns may or may not be present.  From version 1 on, ordered
        migrations can assume the previous step ran.
        """
        columns = {
            table: {
                row[1] for row in self._db.execute(f"PRAGMA table_info({table})")
            }
            for table in ("messages", "devices")
        }
        if "read_at" not in columns["messages"]:
            self._db.execute("ALTER TABLE messages ADD COLUMN read_at TEXT")
        if "radio_enabled" not in columns["devices"]:
            self._db.execute("ALTER TABLE devices ADD COLUMN radio_enabled INTEGER")

    # Ordered migrations from version 1 onwards: (version, description,
    # statements).  Each runs inside its own transaction and only on databases
    # below it.
    #
    # Statements are listed individually rather than as one script on purpose:
    # ``executescript`` COMMITs before it runs, which would close the
    # transaction ``_run_step`` opened and leave a failed migration half
    # applied with no way to roll it back.
    MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = ()

    def _snapshot_before_migration(self, from_version: int) -> Path | None:
        """Copy the database aside before migrating it.

        Returns the snapshot path, or None for an in-memory database (nothing to
        recover) .  A failure here aborts the migration: proceeding would leave
        no way back if a later step corrupts the file.
        """
        if str(self.path) == ":memory:":
            return None
        dest = self.path.with_name(f"{self.path.name}.v{from_version}.bak")
        try:
            target = sqlite3.connect(dest)
            try:
                self._db.backup(target)
            finally:
                target.close()
        except (sqlite3.Error, OSError) as exc:
            raise MigrationFailed(
                f"could not snapshot the database before migrating: {exc}"
            ) from exc
        log.info("pre-migration snapshot written to %s", dest)
        return dest

    def _migrate(self, *, pre_existing: bool) -> None:
        version = self._user_version()
        if version > SCHEMA_VERSION:
            raise SchemaTooNew(
                f"database schema version {version} is newer than this server "
                f"supports ({SCHEMA_VERSION}); upgrade the server or restore a "
                f"backup taken from this version"
            )
        if version == SCHEMA_VERSION:
            return

        # Only an existing database needs protecting.  A file we just created is
        # empty, and snapshotting it would litter the data directory on every
        # first start.
        snapshot = (
            self._snapshot_before_migration(version)
            if pre_existing and version < SCHEMA_VERSION
            else None
        )

        if version == 0:
            self._run_step(
                0,
                1,
                "reconcile pre-versioning schema",
                self._reconcile_to_baseline,
                snapshot,
            )
            version = 1

        for target, description, statements in self.MIGRATIONS:
            if target <= version:
                continue
            self._run_step(
                version,
                target,
                description,
                lambda statements=statements: self._run_statements(statements),
                snapshot,
            )
            version = target

    def _run_statements(self, statements: Iterable[str]) -> None:
        for statement in statements:
            self._db.execute(statement)

    def _run_step(
        self,
        from_version: int,
        to_version: int,
        description: str,
        apply: Callable[[], None],
        snapshot: Path | None,
    ) -> None:
        """Apply one migration step atomically, or roll it back and raise.

        The connection runs in autocommit, so the transaction is explicit.  The
        version bump goes inside it: a step that committed its DDL but not its
        version would be replayed on the next start.  ``apply`` must therefore
        not call ``executescript``, which would COMMIT this transaction out from
        under the step; see ``MIGRATIONS``.
        """
        log.info("migrating schema %d -> %d: %s", from_version, to_version, description)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            apply()
            self._set_user_version(to_version)
        except Exception as exc:
            # Best effort: report why the migration failed, not why the cleanup
            # after it did.  A rollback that cannot run leaves the original
            # exception as the useful one.
            try:
                self._db.execute("ROLLBACK")
            except sqlite3.Error:
                log.exception("rollback after the failed migration also failed")
            raise MigrationFailed(
                f"migration {from_version} -> {to_version} ({description}) failed "
                f"and was rolled back: {exc}",
                snapshot=snapshot,
            ) from exc
        self._db.execute("COMMIT")

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
        "online",
        "registered",
        "radio_enabled",
        "rssi",
        "dbm",
        "bars",
        "rsrp",
        "rsrq",
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
                if field == "radio_enabled":
                    columns[field] = None if value is None else int(bool(value))
                elif field in ("online", "registered"):
                    columns[field] = int(bool(value))
                else:
                    columns[field] = value
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
            "       SUM(CASE WHEN m.direction = 'in' AND m.read_at IS NULL "
            "               THEN 1 ELSE 0 END) AS unread_count, "
            "       s.label AS sim_label, s.iccid AS sim_iccid "
            "FROM messages m LEFT JOIN sims s ON s.id = m.sim_id "
            "GROUP BY m.sim_id, m.peer "
            "ORDER BY last_ts DESC LIMIT ?",
            (limit,),
        )

    def mark_read(self, *, sim_id: int | None, peer: str) -> int:
        """Mark one conversation's incoming messages as read; returns count."""
        now = utcnow()
        with self._lock:
            cur = self._db.execute(
                "UPDATE messages SET read_at = ? "
                "WHERE direction = 'in' AND read_at IS NULL "
                "  AND peer = ? AND sim_id IS ?",
                (now, peer, sim_id),
            )
        return cur.rowcount

    def unread_total(self) -> int:
        row = self.one(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE direction = 'in' AND read_at IS NULL"
        )
        return int(row["n"]) if row else 0

    def messages(
        self,
        *,
        limit: int | None = 50,
        offset: int = 0,
        sim_id: int | None = None,
        direction: str | None = None,
        peer: str | None = None,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = self._message_filter(
            sim_id=sim_id, direction=direction, peer=peer, search=search
        )
        sql = (
            "SELECT m.*, s.label AS sim_label, s.iccid AS sim_iccid "
            "FROM messages m LEFT JOIN sims s ON s.id = m.sim_id "
            f"{where} ORDER BY m.ts DESC, m.id DESC"
        )
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        return self.query(sql, tuple(params))

    @staticmethod
    def _message_filter(
        *,
        sim_id: int | None = None,
        direction: str | None = None,
        peer: str | None = None,
        search: str | None = None,
    ) -> tuple[str, list[Any]]:
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
        return where, params

    def iter_messages(
        self,
        *,
        limit: int | None = None,
        sim_id: int | None = None,
        direction: str | None = None,
        peer: str | None = None,
        search: str | None = None,
        batch_size: int = 500,
    ) -> Iterable[dict[str, Any]]:
        """Stream a stable read without retaining every message in memory.

        A separate read-only connection lets WAL continue accepting gateway
        writes while a large export is being downloaded. In-memory databases
        are only used by tests, where falling back to a bounded list is fine.
        """
        if str(self.path) == ":memory:":
            yield from self.messages(
                limit=limit,
                sim_id=sim_id,
                direction=direction,
                peer=peer,
                search=search,
            )
            return

        where, params = self._message_filter(
            sim_id=sim_id, direction=direction, peer=peer, search=search
        )
        sql = (
            "SELECT m.*, s.label AS sim_label, s.iccid AS sim_iccid "
            "FROM messages m LEFT JOIN sims s ON s.id = m.sim_id "
            f"{where} ORDER BY m.ts DESC, m.id DESC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        connection = sqlite3.connect(
            f"file:{self.path.resolve()}?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        try:
            cursor = connection.execute(sql, tuple(params))
            while rows := cursor.fetchmany(batch_size):
                for row in rows:
                    yield dict(row)
        finally:
            connection.close()

    def count_messages(
        self,
        *,
        sim_id: int | None = None,
        direction: str | None = None,
        peer: str | None = None,
        search: str | None = None,
    ) -> int:
        where, params = self._message_filter(
            sim_id=sim_id, direction=direction, peer=peer, search=search
        )
        row = self.one(
            f"SELECT COUNT(*) AS n FROM messages m {where}", tuple(params)
        )
        return int(row["n"]) if row else 0

    # -- operations ---------------------------------------------------------

    # Every append-only table, so the operator can see which one is actually
    # growing before the disk gauge moves.  Ordered biggest-first at the API
    # boundary, not here.
    _ROW_COUNT_TABLES = (
        "messages", "device_status", "notify_logs", "task_logs",
        "agent_logs", "audit_events", "incidents", "ingested",
        "sims", "devices", "channels", "rules", "tasks",
    )

    def activity_stats(self) -> dict[str, Any]:
        """Throughput and success rates over 24h / 7d, plus per-table rows.

        Rates come out as counts rather than percentages: a channel with 1/1
        success reads as 100% but says far less than 900/1000, and the caller
        cannot recover the denominator from a percentage.  Windows are
        half-open on the recent side (`ts >= cutoff`), so the two never
        double-count a row at the boundary.
        """
        now = datetime.now(UTC)
        day = (now - timedelta(days=1)).isoformat(timespec="seconds")
        week = (now - timedelta(days=7)).isoformat(timespec="seconds")

        def window(sql: str) -> dict[str, int]:
            return {
                "day": int(self.one(sql, (day,))["n"]),
                "week": int(self.one(sql, (week,))["n"]),
            }

        messages = {
            "inbound": window(
                "SELECT COUNT(*) AS n FROM messages "
                "WHERE direction = 'in' AND ts >= ?"
            ),
            "outbound": window(
                "SELECT COUNT(*) AS n FROM messages "
                "WHERE direction = 'out' AND ts >= ?"
            ),
            # Scoped to outbound: a received message has no send outcome, and
            # leaving it unscoped would let an inbound row make
            # outbound - failed go negative at the caller.
            "failed": window(
                "SELECT COUNT(*) AS n FROM messages "
                "WHERE direction = 'out' AND status = 'failed' AND ts >= ?"
            ),
        }
        notifications = {
            "ok": window(
                "SELECT COUNT(*) AS n FROM notify_logs "
                "WHERE status = 'ok' AND ts >= ?"
            ),
            "failed": window(
                "SELECT COUNT(*) AS n FROM notify_logs "
                "WHERE status = 'failed' AND ts >= ?"
            ),
        }
        # 'skipped' is not a run — the scheduler emits it when the module was
        # unavailable and nothing was attempted.  Counting it as either outcome
        # would misreport the success rate, so it gets its own bucket.
        tasks = {
            "ok": window(
                "SELECT COUNT(*) AS n FROM task_logs "
                "WHERE status = 'ok' AND ts >= ?"
            ),
            "failed": window(
                "SELECT COUNT(*) AS n FROM task_logs "
                "WHERE status = 'failed' AND ts >= ?"
            ),
            "skipped": window(
                "SELECT COUNT(*) AS n FROM task_logs "
                "WHERE status = 'skipped' AND ts >= ?"
            ),
        }
        # Table names are a fixed private tuple, never caller input.
        rows = {
            table: int(self.one(f"SELECT COUNT(*) AS n FROM {table}")["n"])
            for table in self._ROW_COUNT_TABLES
        }
        return {
            "messages": messages,
            "notifications": notifications,
            "tasks": tasks,
            "rows": rows,
        }

    # -- retention ---------------------------------------------------------

    def purge(
        self,
        *,
        message_days: int,
        status_days: int,
        log_days: int = 0,
        audit_days: int = 0,
        incident_days: int = 0,
        audit_max_rows: int = 0,
    ) -> dict[str, int]:
        """Delete aged rows.  Verification codes should not live forever.

        Every append-only table needs a horizon of its own.  Only notify_logs
        tied to a message disappear with it (ON DELETE CASCADE); rows from task
        receipts and channel tests carry no message_id and would otherwise
        outlive every other trace of the event.
        """
        removed = {
            "messages": 0, "status": 0, "ingested": 0,
            "agent_logs": 0, "task_logs": 0, "notify_logs": 0,
            "audit_events": 0, "incidents": 0,
        }
        if message_days > 0:
            cutoff = (
                datetime.now(UTC) - timedelta(days=message_days)
            ).isoformat()
            removed["messages"] = self.execute(
                "DELETE FROM messages WHERE ts < ?", (cutoff,)
            ).rowcount
        if status_days > 0:
            cutoff = (
                datetime.now(UTC) - timedelta(days=status_days)
            ).isoformat()
            removed["status"] = self.execute(
                "DELETE FROM device_status WHERE ts < ?", (cutoff,)
            ).rowcount
        if log_days > 0:
            cutoff = (
                datetime.now(UTC) - timedelta(days=log_days)
            ).isoformat()
            for table in ("agent_logs", "task_logs", "notify_logs"):
                removed[table] = self.execute(
                    f"DELETE FROM {table} WHERE ts < ?", (cutoff,)
                ).rowcount
        if audit_days > 0:
            cutoff = (
                datetime.now(UTC) - timedelta(days=audit_days)
            ).isoformat()
            removed["audit_events"] = self.execute(
                "DELETE FROM audit_events WHERE ts < ?", (cutoff,)
            ).rowcount
        if audit_max_rows > 0:
            # Keep the newest rows.  id is monotonic, so the cutoff id is the
            # one audit_max_rows back from the newest.
            row = self.one(
                "SELECT id FROM audit_events ORDER BY id DESC LIMIT 1 OFFSET ?",
                (audit_max_rows - 1,),
            )
            if row is not None:
                removed["audit_events"] += self.execute(
                    "DELETE FROM audit_events WHERE id < ?", (row["id"],)
                ).rowcount
        if incident_days > 0:
            # Only closed incidents age out; an unresolved one stays until it
            # recovers or an admin acts on it, however old it is.
            cutoff = (
                datetime.now(UTC) - timedelta(days=incident_days)
            ).isoformat()
            removed["incidents"] = self.execute(
                "DELETE FROM incidents "
                "WHERE status = 'resolved' AND resolved_at IS NOT NULL "
                "AND resolved_at < ?",
                (cutoff,),
            ).rowcount
        # Idempotency records only need to outlive a plausible replay window.
        cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
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

    @classmethod
    def validate_backup(cls, path: str | Path) -> None:
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
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"文件不是有效的 SQLite 数据库: {exc}") from exc
        finally:
            conn.close()
        missing = cls._REQUIRED_TABLES - names
        if missing:
            raise ValueError(
                "这不是有效的 Hub 备份(缺少表: "
                + ", ".join(sorted(missing))
                + ")"
            )
        # A backup from a newer Server may carry columns this build never writes;
        # restoring it would silently drop them on the next write.  Caught here,
        # before the file overwrites live data.
        if version > SCHEMA_VERSION:
            raise ValueError(
                f"备份来自更新版本的 Server(schema {version},当前支持 "
                f"{SCHEMA_VERSION});请先升级 Server 再恢复"
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
                # Older valid backups predate additive operational tables and
                # carry their own (lower) user_version.  Reapplying the schema
                # and running the ordered migrations brings the restored data to
                # the current version, so the live process stays usable without
                # a restart.  pre_existing=True: this file now holds real data,
                # so a migration must snapshot it first.
                self._db.executescript(SCHEMA)
                self._migrate(pre_existing=True)
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

    # -- operations -------------------------------------------------------

    def record_audit(
        self,
        action: str,
        *,
        target: str = "",
        status: str = "ok",
        detail: str = "",
        client_ip: str = "",
    ) -> None:
        """Persist metadata about an admin action, never its request body."""
        self.execute(
            "INSERT INTO audit_events (ts, action, target, status, detail, client_ip) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                utcnow(),
                action[:128],
                target[:256],
                status[:32],
                detail[:1000],
                client_ip[:64],
            ),
        )

    def open_incident(
        self,
        fingerprint: str,
        *,
        kind: str,
        severity: str,
        source: str,
        title: str,
        detail: str = "",
    ) -> dict[str, Any]:
        """Open or refresh one logical incident identified by fingerprint."""
        now = utcnow()
        with self._lock:
            self._db.execute(
                "INSERT INTO incidents "
                "(fingerprint, kind, severity, source, title, detail, status, "
                " occurrences, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', 1, ?, ?) "
                "ON CONFLICT(fingerprint) DO UPDATE SET "
                "kind = excluded.kind, severity = excluded.severity, "
                "source = excluded.source, title = excluded.title, "
                "detail = excluded.detail, "
                "status = CASE WHEN incidents.status = 'resolved' "
                "              THEN 'active' ELSE incidents.status END, "
                "occurrences = incidents.occurrences + 1, "
                "first_seen_at = CASE WHEN incidents.status = 'resolved' "
                "                     THEN excluded.first_seen_at "
                "                     ELSE incidents.first_seen_at END, "
                "last_seen_at = excluded.last_seen_at, "
                "acknowledged_at = CASE WHEN incidents.status = 'resolved' "
                "                       THEN NULL ELSE incidents.acknowledged_at END, "
                "resolved_at = NULL",
                (
                    fingerprint[:256], kind[:64], severity[:16], source[:128],
                    title[:256], detail[:1000], now, now,
                ),
            )
            row = self._db.execute(
                "SELECT * FROM incidents WHERE fingerprint = ?", (fingerprint[:256],)
            ).fetchone()
        return dict(row)

    def resolve_incident(self, fingerprint: str, *, detail: str = "") -> bool:
        now = utcnow()
        with self._lock:
            if detail:
                cursor = self._db.execute(
                    "UPDATE incidents SET status = 'resolved', detail = ?, "
                    "last_seen_at = ?, resolved_at = ? "
                    "WHERE fingerprint = ? AND status != 'resolved'",
                    (detail[:1000], now, now, fingerprint[:256]),
                )
            else:
                cursor = self._db.execute(
                    "UPDATE incidents SET status = 'resolved', last_seen_at = ?, "
                    "resolved_at = ? WHERE fingerprint = ? AND status != 'resolved'",
                    (now, now, fingerprint[:256]),
                )
        return cursor.rowcount > 0

    def set_incident_status(self, incident_id: int, status: str) -> dict[str, Any] | None:
        if status not in {"active", "acknowledged", "resolved"}:
            raise ValueError("invalid incident status")
        now = utcnow()
        acknowledged = now if status == "acknowledged" else None
        resolved = now if status == "resolved" else None
        with self._lock:
            self._db.execute(
                "UPDATE incidents SET status = ?, acknowledged_at = ?, resolved_at = ? "
                "WHERE id = ?",
                (status, acknowledged, resolved, incident_id),
            )
            row = self._db.execute(
                "SELECT * FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
        return dict(row) if row else None
