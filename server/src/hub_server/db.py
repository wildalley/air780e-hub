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
# Version 2 adds ``messages.raw_pdu`` and ``messages.dcs``.
# Version 3 records the Agent/Server WebSocket protocol version.
# Version 4 adds per-segment SMS delivery reports.
# Version 5 reclassifies stored data PDUs that predate the current checks.
#
# To add a migration: append one entry to ``MIGRATIONS`` with the next integer
# and bump this constant, and add the same columns/tables to SCHEMA so a brand
# new database gets them directly.  That is not a duplicate: SCHEMA builds the
# current shape for a new file, MIGRATIONS moves an existing file forward, and
# ``_migrate`` stamps a new file rather than replaying migrations against it.
# Never renumber or edit a released entry — a database that already ran it will
# not run it again, so an edit only affects databases that have not, and the two
# then disagree about what version N means.
SCHEMA_VERSION = 5

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
    protocol_version INTEGER NOT NULL DEFAULT 0,
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
    -- The PDU exactly as the modem returned it, and its TP-DCS.  The agent has
    -- always sent both; not keeping them meant a garbled message could not be
    -- diagnosed after the fact, because the agent deletes the message from the
    -- modem once it is read (delete_after_read defaults on) and stores no PDU
    -- of its own.  The decoded body was all that survived, which is not enough
    -- to tell a decoder bug from a message that was never text to begin with.
    raw_pdu    TEXT,
    dcs        INTEGER,
    -- Set when the payload was data rather than text: an 8-bit TP-DCS, a
    -- port-addressing UDH (OTA provisioning, WAP push, SIM toolkit), a
    -- malformed UDH whose payload boundary cannot be trusted, or an empty
    -- service-centre-specific PID message. The UI renders what it is.
    is_binary  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_sim ON messages(sim_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_peer ON messages(peer, ts DESC);

-- One row per outbound segment. Rows with no message_id are status reports
-- that beat their sms_out event to the Server and will be reconciled later.
CREATE TABLE IF NOT EXISTS sms_delivery_segments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id        INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    agent_id          TEXT NOT NULL,
    device            TEXT NOT NULL,
    segment_index     INTEGER,
    modem_reference   INTEGER NOT NULL,
    recipient         TEXT NOT NULL DEFAULT '',
    recipient_key     TEXT NOT NULL DEFAULT '',
    submitted_at      TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',
    status_code       INTEGER,
    service_center_ts TEXT,
    discharge_ts      TEXT,
    reported_at       TEXT,
    raw_pdu           TEXT,
    event_seq         INTEGER,
    created_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sms_delivery_message_segment
    ON sms_delivery_segments(message_id, segment_index);
CREATE INDEX IF NOT EXISTS idx_sms_delivery_match
    ON sms_delivery_segments(agent_id, device, modem_reference, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_sms_delivery_unmatched
    ON sms_delivery_segments(message_id, created_at DESC);

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
-- The dashboard reads every device at once, so it filters on ts alone and the
-- composite index above cannot serve it — ts is not its leading column.  Without
-- this the 15-second refresh scans the whole table however short the window is.
CREATE INDEX IF NOT EXISTS idx_status_ts ON device_status(ts);

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


def _pdu_is_data(raw_pdu: str) -> bool:
    """Classify a stored inbound PDU without decoding its user data as text."""
    try:
        data = bytes.fromhex("".join(raw_pdu.split()))
    except ValueError:
        return False
    if not data:
        return False

    tpdu = 1 + data[0]  # SMSC length excludes its own length octet.
    if tpdu >= len(data):
        return False
    first = data[tpdu]
    if (first & 0x03) != 0:  # only stored inbound SMS-DELIVER PDUs
        return False

    pos = tpdu + 1
    if pos + 2 > len(data):
        return False
    address_digits = data[pos]
    pos += 2 + (address_digits + 1) // 2
    # TP-PID, TP-DCS, TP-SCTS (7 octets), TP-UDL.
    if pos + 10 > len(data):
        return False
    pid = data[pos]
    dcs = data[pos + 1]
    udl = data[pos + 9]
    body = data[pos + 10 :]

    if (dcs & 0xC0) == 0x00:
        uses_8bit = ((dcs >> 2) & 0x03) == 1
    elif (dcs & 0xF0) == 0xF0:
        uses_8bit = bool(dcs & 0x04)
    else:
        uses_8bit = False
    if uses_8bit or (udl == 0 and pid >= 0xC0):
        return True
    if not (first & 0x40):
        return False
    if not body:
        return True

    header_end = body[0] + 1
    if header_end > len(body):
        return True
    header = body[1:header_end]
    cursor = 0
    while cursor < len(header):
        if cursor + 2 > len(header):
            return True
        element_end = cursor + 2 + header[cursor + 1]
        if element_end > len(header):
            return True
        if header[cursor] in {0x04, 0x05}:  # application port addressing
            return True
        cursor = element_end
    return False


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
            self._prepare_schema(pre_existing=pre_existing)

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

    def _prepare_schema(self, *, pre_existing: bool) -> None:
        """Snapshot and validate an old database before any schema writes."""
        version = self._user_version()
        if version > SCHEMA_VERSION:
            raise SchemaTooNew(
                f"database schema version {version} is newer than this server "
                f"supports ({SCHEMA_VERSION}); upgrade the server or restore a "
                f"backup taken from this version"
            )
        snapshot = (
            self._snapshot_before_migration(version)
            if pre_existing and version < SCHEMA_VERSION
            else None
        )
        # SCHEMA also supplies tables that existed before formal versioning.
        # It is intentionally applied only after the snapshot is durable.
        self._db.executescript(SCHEMA)
        self._migrate(pre_existing=pre_existing, snapshot=snapshot)

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

    # Ordered migrations from version 1 onwards: (version, description, method).
    # Each runs inside its own transaction and only on databases below it.
    #
    # The third element names a method rather than holding SQL, because a step
    # generally has to look at the database before deciding what to do — see
    # ``_add_columns_if_missing``.  Whatever the method runs, it must use
    # ``execute`` and never ``executescript``: the latter COMMITs before it
    # runs, which would close the transaction ``_run_step`` opened and leave a
    # failed migration half applied with no way to roll it back.
    MIGRATIONS: tuple[tuple[int, str, str], ...] = (
        (2, "keep the raw PDU, TP-DCS and binary flag of every message",
         "_migration_message_diagnostics"),
        (3, "record the Agent WebSocket protocol version",
         "_migration_agent_protocol_version"),
        (4, "track delivery reports for each outbound SMS segment",
         "_migration_sms_delivery_segments"),
        (5, "reclassify stored data-message PDUs",
         "_migration_data_messages"),
    )

    def _add_columns_if_missing(self, table: str, columns: dict[str, str]) -> None:
        """``ALTER TABLE ... ADD COLUMN`` for each column not already there.

        The check is required, not defensive padding.  SCHEMA and MIGRATIONS
        describe the same shape from two directions: SCHEMA builds it for a new
        file, MIGRATIONS walks an old one forward.  A table that did not exist
        when the database was created is built by SCHEMA at the *current* shape,
        columns and all — and then an unconditional ADD COLUMN for those same
        columns fails on a duplicate.  Skipping what is present makes the step
        agree with whichever way the table got here.
        """
        present = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in present:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _migration_message_diagnostics(self) -> None:
        """v1 -> v2: the columns that make a garbled message diagnosable.

        The agent had always sent the PDU and TP-DCS; the server dropped both.
        Once the modem's own copy is deleted (``delete_after_read`` defaults on)
        the decoded body was all that survived, which cannot distinguish a
        decoder bug from a payload that was never text.
        """
        self._add_columns_if_missing(
            "messages",
            {
                "raw_pdu": "TEXT",
                "dcs": "INTEGER",
                "is_binary": "INTEGER NOT NULL DEFAULT 0",
            },
        )

    def _migration_agent_protocol_version(self) -> None:
        """v2 -> v3: make wire compatibility observable after disconnect."""
        self._add_columns_if_missing(
            "agents", {"protocol_version": "INTEGER NOT NULL DEFAULT 0"}
        )

    def _migration_sms_delivery_segments(self) -> None:
        """v3 -> v4: normalized modem references and their delivery state."""
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS sms_delivery_segments ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE, "
            "agent_id TEXT NOT NULL, device TEXT NOT NULL, segment_index INTEGER, "
            "modem_reference INTEGER NOT NULL, recipient TEXT NOT NULL DEFAULT '', "
            "recipient_key TEXT NOT NULL DEFAULT '', submitted_at TEXT, "
            "status TEXT NOT NULL DEFAULT 'pending', status_code INTEGER, "
            "service_center_ts TEXT, discharge_ts TEXT, reported_at TEXT, "
            "raw_pdu TEXT, event_seq INTEGER, created_at TEXT NOT NULL)"
        )
        self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sms_delivery_message_segment "
            "ON sms_delivery_segments(message_id, segment_index)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sms_delivery_match "
            "ON sms_delivery_segments"
            "(agent_id, device, modem_reference, submitted_at DESC)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sms_delivery_unmatched "
            "ON sms_delivery_segments(message_id, created_at DESC)"
        )

    def _migration_data_messages(self) -> None:
        """v4 -> v5: hide already-stored data PDUs fixed by the new Agent."""
        last_id = 0
        while rows := self._db.execute(
            "SELECT id, raw_pdu FROM messages "
            "WHERE id > ? AND direction = 'in' AND is_binary = 0 "
            "AND raw_pdu IS NOT NULL ORDER BY id LIMIT 500",
            (last_id,),
        ).fetchall():
            malformed = [
                (row["id"],)
                for row in rows
                if _pdu_is_data(row["raw_pdu"])
            ]
            if malformed:
                self._db.executemany(
                    "UPDATE messages SET is_binary = 1 WHERE id = ?", malformed
                )
            last_id = int(rows[-1]["id"])

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

    def _migrate(
        self, *, pre_existing: bool, snapshot: Path | None
    ) -> None:
        version = self._user_version()
        if version > SCHEMA_VERSION:
            raise SchemaTooNew(
                f"database schema version {version} is newer than this server "
                f"supports ({SCHEMA_VERSION}); upgrade the server or restore a "
                f"backup taken from this version"
            )
        if version == SCHEMA_VERSION:
            return

        if not pre_existing:
            # A file we just created already has the current shape: SCHEMA ran
            # against it a moment ago.  Stamping it here rather than replaying
            # the migrations is what keeps a migration free to say
            # ``ALTER TABLE ... ADD COLUMN`` — replaying that against a table
            # SCHEMA had already created would fail on a duplicate column, on
            # every first start.  Migrations exist to move *old* databases
            # forward, and this one has no past.
            self._set_user_version(SCHEMA_VERSION)
            return

        if version == 0:
            self._run_step(
                0,
                1,
                "reconcile pre-versioning schema",
                self._reconcile_to_baseline,
                snapshot,
            )
            version = 1

        for target, description, method in self.MIGRATIONS:
            if target <= version:
                continue
            self._run_step(
                version, target, description, getattr(self, method), snapshot
            )
            version = target

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

    def upsert_agent(
        self, agent_id: str, version: str, protocol_version: int, connected: bool
    ) -> None:
        self.execute(
            "INSERT INTO agents "
            "(id, version, protocol_version, last_seen_at, connected) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET version = excluded.version, "
            "protocol_version = excluded.protocol_version, "
            "last_seen_at = excluded.last_seen_at, connected = excluded.connected",
            (agent_id, version, protocol_version, utcnow(), int(connected)),
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
        raw_pdu: str | None = None,
        dcs: int | None = None,
        is_binary: bool = False,
    ) -> int:
        sim_id = self.upsert_sim(iccid)
        cursor = self.execute(
            "INSERT INTO messages "
            "(agent_id, device, sim_id, direction, peer, body, ts, status, "
            " segments, seq, error, raw_pdu, dcs, is_binary, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent_id, device, sim_id, direction, peer, body, to_utc_iso(ts), status,
             segments, seq, error, raw_pdu, dcs, int(is_binary), utcnow()),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _recipient_key(recipient: str) -> str:
        return "".join(char for char in str(recipient) if char.isdigit())

    @classmethod
    def _same_recipient(cls, left: str, right: str) -> bool:
        """Match E.164 and local forms without guessing short service numbers."""
        a, b = cls._recipient_key(left), cls._recipient_key(right)
        if not a or not b:
            return not a and not b
        if a == b:
            return True
        return min(len(a), len(b)) >= 7 and (a.endswith(b) or b.endswith(a))

    @staticmethod
    def _delivery_state(status_code: int | None, claimed: str | None) -> str:
        if status_code is not None:
            if 0x00 <= status_code <= 0x1F:
                return "delivered"
            if 0x20 <= status_code <= 0x3F:
                return "pending"
            if 0x40 <= status_code <= 0x7F:
                return "failed"
        return claimed if claimed in {"pending", "delivered", "failed"} else "pending"

    @staticmethod
    def _terminal_delivery_state(current: str, incoming: str) -> str:
        """An old temporary report must not regress a terminal outcome."""
        if incoming == "pending" and current in {"delivered", "failed"}:
            return current
        if incoming == "failed" and current == "delivered":
            return current
        return incoming

    @staticmethod
    def _timestamp_distance(left: str | None, right: str | None) -> float:
        if not left or not right:
            return float("inf")
        try:
            return abs(
                (datetime.fromisoformat(left) - datetime.fromisoformat(right)).total_seconds()
            )
        except (TypeError, ValueError):
            return float("inf")

    def attach_sms_segments(
        self,
        *,
        message_id: int,
        agent_id: str,
        device: str,
        recipient: str,
        references: list[int],
        submitted_at: str,
    ) -> None:
        """Attach modem references, reconciling reports that arrived first."""
        submitted = to_utc_iso(submitted_at)
        key = self._recipient_key(recipient)
        cutoff = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        with self._lock:
            for segment_index, reference in enumerate(references, start=1):
                candidates = self._db.execute(
                    "SELECT * FROM sms_delivery_segments "
                    "WHERE message_id IS NULL AND agent_id = ? AND device = ? "
                    "AND modem_reference = ? AND created_at >= ? ORDER BY id DESC",
                    (agent_id, device, reference, cutoff),
                ).fetchall()
                early = next(
                    (
                        row for row in candidates
                        if self._same_recipient(row["recipient"], recipient)
                    ),
                    None,
                )
                if early is not None:
                    self._db.execute(
                        "UPDATE sms_delivery_segments SET message_id = ?, "
                        "segment_index = ?, submitted_at = ?, recipient = ?, "
                        "recipient_key = ? WHERE id = ?",
                        (message_id, segment_index, submitted, recipient, key, early["id"]),
                    )
                else:
                    self._db.execute(
                        "INSERT INTO sms_delivery_segments "
                        "(message_id, agent_id, device, segment_index, modem_reference, "
                        "recipient, recipient_key, submitted_at, status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                        (
                            message_id, agent_id, device, segment_index, reference,
                            recipient, key, submitted, utcnow(),
                        ),
                    )
            self._update_message_delivery_status(message_id)

    def record_sms_delivery(
        self,
        *,
        agent_id: str,
        device: str,
        reference: int,
        recipient: str,
        status_code: int | None,
        status: str | None,
        service_center_ts: str | None,
        discharge_ts: str | None,
        reported_at: str,
        raw_pdu: str | None,
        event_seq: int | None,
    ) -> int | None:
        """Store a report and return the correlated outbound message, if any."""
        report_state = self._delivery_state(status_code, status)
        service_ts = to_utc_iso(service_center_ts) if service_center_ts else None
        discharge = to_utc_iso(discharge_ts) if discharge_ts else None
        reported = to_utc_iso(reported_at)
        key = self._recipient_key(recipient)
        unmatched_cutoff = (datetime.now(UTC) - timedelta(days=14)).isoformat()

        with self._lock:
            candidates = self._db.execute(
                "SELECT * FROM sms_delivery_segments "
                "WHERE message_id IS NOT NULL AND agent_id = ? AND device = ? "
                "AND modem_reference = ? "
                "ORDER BY submitted_at DESC, id DESC",
                (agent_id, device, reference),
            ).fetchall()
            candidates = [
                row for row in candidates
                if self._same_recipient(row["recipient"], recipient)
            ]
            matched = None
            if candidates:
                matched = (
                    min(
                        candidates,
                        key=lambda row: self._timestamp_distance(
                            row["submitted_at"], service_ts
                        ),
                    )
                    if service_ts else candidates[0]
                )

            if matched is not None:
                effective = self._terminal_delivery_state(matched["status"], report_state)
                if effective != report_state:
                    # This is a stale temporary/failure report rejected by the
                    # state machine. Do not pair a retained terminal state with
                    # the rejected report's TP-ST, timestamps, or raw PDU.
                    message_id = int(matched["message_id"])
                    self._update_message_delivery_status(message_id)
                    return message_id
                self._db.execute(
                    "UPDATE sms_delivery_segments SET status = ?, status_code = ?, "
                    "service_center_ts = ?, discharge_ts = ?, reported_at = ?, "
                    "raw_pdu = ?, event_seq = ? WHERE id = ?",
                    (
                        effective, status_code, service_ts, discharge, reported,
                        raw_pdu, event_seq, matched["id"],
                    ),
                )
                message_id = int(matched["message_id"])
                self._update_message_delivery_status(message_id)
                return message_id

            # A second report can supersede a temporary report before sms_out
            # arrives. Keep one unmatched row per recent logical segment.
            unmatched = self._db.execute(
                "SELECT * FROM sms_delivery_segments "
                "WHERE message_id IS NULL AND agent_id = ? AND device = ? "
                "AND modem_reference = ? AND created_at >= ? ORDER BY id DESC",
                (agent_id, device, reference, unmatched_cutoff),
            ).fetchall()
            existing = next(
                (
                    row for row in unmatched
                    if self._same_recipient(row["recipient"], recipient)
                ),
                None,
            )
            if existing is not None:
                effective = self._terminal_delivery_state(existing["status"], report_state)
                if effective == report_state:
                    self._db.execute(
                        "UPDATE sms_delivery_segments SET recipient = ?, "
                        "recipient_key = ?, status = ?, status_code = ?, "
                        "service_center_ts = ?, discharge_ts = ?, reported_at = ?, "
                        "raw_pdu = ?, event_seq = ? WHERE id = ?",
                        (
                            recipient, key, effective, status_code, service_ts,
                            discharge, reported, raw_pdu, event_seq, existing["id"],
                        ),
                    )
            else:
                self._db.execute(
                    "INSERT INTO sms_delivery_segments "
                    "(message_id, agent_id, device, modem_reference, recipient, "
                    "recipient_key, status, status_code, service_center_ts, "
                    "discharge_ts, reported_at, raw_pdu, event_seq, created_at) "
                    "VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        agent_id, device, reference, recipient, key, report_state,
                        status_code, service_ts, discharge, reported, raw_pdu,
                        event_seq, utcnow(),
                    ),
                )
            return None

    def _update_message_delivery_status(self, message_id: int) -> None:
        message = self._db.execute(
            "SELECT segments FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if message is None:
            return
        rows = self._db.execute(
            "SELECT status, status_code FROM sms_delivery_segments "
            "WHERE message_id = ? ORDER BY segment_index",
            (message_id,),
        ).fetchall()
        if not rows:
            return
        expected = max(int(message["segments"] or 1), len(rows))
        delivered = sum(row["status"] == "delivered" for row in rows)
        failed = [row for row in rows if row["status"] == "failed"]
        if delivered >= expected:
            aggregate, error = "delivered", None
        elif delivered:
            aggregate = "partial"
            error = "部分分段投递失败" if failed else None
        elif failed:
            aggregate = "failed"
            codes = ", ".join(
                f"0x{row['status_code']:02X}"
                for row in failed if row["status_code"] is not None
            )
            error = f"短信投递失败{f' (TP-ST {codes})' if codes else ''}"
        else:
            aggregate, error = "pending", None
        self._db.execute(
            "UPDATE messages SET status = ?, error = ? WHERE id = ?",
            (aggregate, error, message_id),
        )

    def conversations(
        self, *, limit: int = 200, content: str | None = None
    ) -> list[dict[str, Any]]:
        """Threads: one row per (card, correspondent), newest activity first.

        The bare ``m.body`` / ``m.direction`` / ``m.id`` columns alongside
        ``MAX(m.ts)`` are SQLite's documented min/max behaviour — they come
        from the row that produced the maximum, which is exactly the preview
        we want.  This is not portable SQL; on another engine it needs a
        window function.
        """
        where, params = self._message_filter(content=content)
        params.append(limit)
        return self.query(
            "SELECT m.sim_id, m.peer, m.device, "
            "       m.id AS last_id, m.body AS last_body, "
            "       m.is_binary AS last_is_binary, "
            "       m.direction AS last_direction, m.status AS last_status, "
            "       MAX(m.ts) AS last_ts, COUNT(*) AS message_count, "
            "       SUM(CASE WHEN m.direction = 'in' AND m.read_at IS NULL "
            "               THEN 1 ELSE 0 END) AS unread_count, "
            "       s.label AS sim_label, s.iccid AS sim_iccid "
            "FROM messages m LEFT JOIN sims s ON s.id = m.sim_id "
            f"{where} "
            "GROUP BY m.sim_id, m.peer "
            "ORDER BY last_ts DESC LIMIT ?",
            tuple(params),
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
        content: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = self._message_filter(
            sim_id=sim_id, direction=direction, peer=peer, search=search,
            content=content,
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
        content: str | None = None,
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
        if content == "text":
            clauses.append("m.is_binary = 0")
        elif content == "data":
            clauses.append("m.is_binary = 1")
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
        content: str | None = None,
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
                content=content,
            )
            return

        where, params = self._message_filter(
            sim_id=sim_id, direction=direction, peer=peer, search=search,
            content=content,
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
        content: str | None = None,
    ) -> int:
        where, params = self._message_filter(
            sim_id=sim_id, direction=direction, peer=peer, search=search,
            content=content,
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
        "sms_delivery_segments", "sims", "devices", "channels", "rules", "tasks",
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
            # Matched rows cascade with messages. Unmatched reports have no
            # parent, so give them the same retention horizon explicitly.
            self.execute(
                "DELETE FROM sms_delivery_segments "
                "WHERE message_id IS NULL AND created_at < ?",
                (cutoff,),
            )
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
                # Older valid backups carry their own lower user_version.
                # Prepare them exactly like a database opened at startup so the
                # restored bytes are snapshotted before any migration writes.
                self._prepare_schema(pre_existing=True)
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
