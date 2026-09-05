"""Server database.

SQLite, one file under the data volume. Sync ``sqlite3`` writes and transactional
reads share a lock; long reads use separate read-only connections so WAL readers
do not hold up gateway writes. Async callers submit complete DB operations to a
bounded single-worker executor; synchronous REST endpoints run in a threadpool.

Schema invariant: messages, tasks and rules all hang off
``sims``, not off devices.  Swapping a card into the other module must not
orphan its history — that is the concrete lesson from SimAdmin's
single-modem model.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from anyio import CancelScope

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
# Version 6 adds structured SIM billing and lifecycle dates used for reminders.
# Version 7 adds the covering index used by conversation summaries and threads.
# Version 8 records modem firmware and per-domain network/IMS registration.
# Version 9 records module supply voltage and its low-voltage threshold.
# Version 10 keeps what was salvaged from a truncated message.
# Version 11 records call attempts as rows rather than log text.
# Version 12 records packet-data attachment, PDP state, and roaming policy.
# Version 13 records the Agent's effective local data policy separately from
# the modem's packet attachment and PDP state.
# Version 16 makes a notification owed by a stored event durable, so a restart
# resumes it instead of losing it with the process.
# Version 17 records desired and acknowledged Agent task configurations.
#
# To add a migration: append one entry to ``MIGRATIONS`` with the next integer
# and bump this constant, and add the same columns/tables/indexes to SCHEMA so a brand
# new database gets them directly.  That is not a duplicate: SCHEMA builds the
# current shape for a new file, MIGRATIONS moves an existing file forward, and
# ``_migrate`` stamps a new file rather than replaying migrations against it.
# Never renumber or edit a released entry — a database that already ran it will
# not run it again, so an edit only affects databases that have not, and the two
# then disagree about what version N means.
SCHEMA_VERSION = 17

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
    last_seq     INTEGER NOT NULL DEFAULT 0,
    -- The agent's local event store has an identity of its own, reported at
    -- hello.  Sequence numbers are only unique *within* one store: rebuild the
    -- agent's data directory and numbering restarts at 1 while this server
    -- still holds the old numbers, so the two together are what identifies an
    -- event.  Empty for an agent old enough not to report one.
    stream_id    TEXT NOT NULL DEFAULT '',
    tasks_revision TEXT NOT NULL DEFAULT '',
    tasks_applied_revision TEXT NOT NULL DEFAULT '',
    tasks_sync_id TEXT NOT NULL DEFAULT '',
    tasks_sync_status TEXT NOT NULL DEFAULT 'pending',
    tasks_sync_error TEXT NOT NULL DEFAULT '',
    tasks_sync_sent_at TEXT,
    tasks_synced_at TEXT
);

-- Every event the agent sends is recorded in the same transaction that
-- applies it, so a replay after a lost ack cannot duplicate a message.
CREATE TABLE IF NOT EXISTS ingested (
    agent_id  TEXT NOT NULL,
    stream_id TEXT NOT NULL DEFAULT '',
    seq       INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    at        TEXT NOT NULL,
    PRIMARY KEY (agent_id, stream_id, seq)
);

CREATE TABLE IF NOT EXISTS sims (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    iccid         TEXT UNIQUE NOT NULL,
    label         TEXT NOT NULL DEFAULT '',
    phone_number  TEXT NOT NULL DEFAULT '',
    billing_type  TEXT NOT NULL DEFAULT 'unknown',
    plan_name     TEXT NOT NULL DEFAULT '',
    balance       TEXT,
    low_balance_threshold TEXT,
    currency      TEXT NOT NULL DEFAULT '',
    balance_updated_at TEXT,
    expires_at    TEXT,
    activity_due_at TEXT,
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
    eps_registered INTEGER,
    cs_registered INTEGER,
    ims_registered INTEGER,
    data_enabled INTEGER NOT NULL DEFAULT 0,
    data_attached INTEGER,
    pdp_active INTEGER,
    roaming INTEGER,
    roaming_data_allowed INTEGER NOT NULL DEFAULT 0,
    data_blocked_by_roaming INTEGER NOT NULL DEFAULT 0,
    model        TEXT NOT NULL DEFAULT '',
    hardware_model TEXT NOT NULL DEFAULT '',
    firmware     TEXT NOT NULL DEFAULT '',
    imei         TEXT NOT NULL DEFAULT '',
    operator     TEXT NOT NULL DEFAULT '',
    rssi         INTEGER,
    dbm          INTEGER,
    bars         INTEGER NOT NULL DEFAULT 0,
    rsrp         INTEGER,
    rsrq         INTEGER,
    storage_used INTEGER NOT NULL DEFAULT 0,
    storage_cap  INTEGER NOT NULL DEFAULT 0,
    voltage_mv   INTEGER,
    -- What this module's Agent considers a low supply, in millivolts.  Stored
    -- alongside the reading because the threshold lives in the Agent's
    -- per-device config: keeping a second default here would let the two
    -- disagree about whether the same voltage is a problem.
    low_voltage_mv INTEGER,
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
    -- The modem handed us this frame with octets already missing, so `body` is
    -- mojibake: it was decoded under header fields that are really message
    -- body. These three are what the agent could re-phase out of the wreckage.
    -- `recovered_body` is always a fragment of the middle — the head and tail
    -- were gone before the agent saw the frame — and an empty `recovered_code`
    -- on a truncated row means "the code did not survive", never "no code was
    -- sent". Kept apart from `body`/`is_binary` so nothing renders a fragment
    -- as if it were the whole message.
    truncated      INTEGER NOT NULL DEFAULT 0,
    recovered_body TEXT,
    recovered_code TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_sim ON messages(sim_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_peer ON messages(peer, ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(sim_id, peer, ts DESC, id DESC);

-- One row per call attempt, inbound or outbound. Kept apart from `messages`
-- rather than folded in as a direction: a call has no body to search, and its
-- success is `reached_network`, not a delivery status. For a keep-alive card
-- this is the strongest evidence there is -- an SMS proves the card can send,
-- an inbound call proves the network can still reach it.
CREATE TABLE IF NOT EXISTS calls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   TEXT NOT NULL,
    device     TEXT NOT NULL,
    sim_id     INTEGER REFERENCES sims(id) ON DELETE SET NULL,
    direction  TEXT NOT NULL,
    peer       TEXT NOT NULL DEFAULT '',
    ts         TEXT NOT NULL,
    -- The modem's own word for how the attempt ended (`alerting`, `busy`,
    -- `no_answer`, `missed`, ...). Stored verbatim rather than collapsed to a
    -- boolean so a carrier-side change shows up as a new outcome instead of
    -- silently reading as failure.
    outcome    TEXT NOT NULL DEFAULT '',
    -- Judge success by this, not by `outcome`: a keep-alive counts the moment
    -- the carrier saw the attempt, and `busy` / `no_answer` both prove that.
    reached_network INTEGER NOT NULL DEFAULT 0,
    ring_seconds    REAL NOT NULL DEFAULT 0,
    detail     TEXT NOT NULL DEFAULT '',
    seq        INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts DESC);
CREATE INDEX IF NOT EXISTS idx_calls_sim ON calls(sim_id, ts DESC);

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
    storage_cap  INTEGER,
    voltage_mv   INTEGER
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

-- A notification that is owed because an event was stored.  Written in the same
-- transaction as the event itself, so a COMMIT is already a promise to notify:
-- delivery no longer depends on one in-process callback surviving the moment.
CREATE TABLE IF NOT EXISTS notify_outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    -- The row this is about: messages.id, tasks.id or calls.id by kind.
    ref_id      INTEGER,
    -- The agent event this came from, as "agent|stream|seq".  Unique so a
    -- replay after a lost ack cannot queue the same push twice.  NULL for
    -- intents the server raises itself, which have no event to be idempotent
    -- against and are deduplicated by their caller or not at all.
    event_key   TEXT,
    -- The frame as received, kept so the text can still be rendered after a
    -- restart -- the process that held it in memory is gone by then.
    frame       TEXT NOT NULL DEFAULT '{}',
    -- Rendered once at expansion for the kinds whose text does not depend on a
    -- channel template, so a later attempt sends what the event said, not what
    -- the row happens to say now.
    title       TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    -- pending -> expanded (deliveries exist) | skipped (nothing to send)
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    expanded_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notify_outbox_event
    ON notify_outbox(event_key) WHERE event_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_notify_outbox_status ON notify_outbox(status, id);

-- One row per (intent, channel, rule): the unit that is attempted, retried and
-- eventually given up on.  ``notify_logs`` remains the audit trail of finished
-- attempts; this table is the work still owed, and it survives a restart.
CREATE TABLE IF NOT EXISTS notify_deliveries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    outbox_id   INTEGER NOT NULL REFERENCES notify_outbox(id) ON DELETE CASCADE,
    channel_id  INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    -- CASCADE, not SET NULL: a deleted rule is the reason this push existed, and
    -- nulling it would also let two deliveries collide on the unique index below
    -- in the middle of an unrelated rule deletion.
    rule_id     INTEGER REFERENCES rules(id) ON DELETE CASCADE,
    -- pending -> ok | failed (given up) | expired (too old to be worth sending)
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    -- A claim, not a state: the worker stamps these while it is sending, and a
    -- process that dies mid-send leaves a lease that simply expires, so the row
    -- is picked up again instead of sitting in a "leased" state forever.
    lease_owner TEXT,
    lease_until TEXT,
    error_code  TEXT,
    safe_detail TEXT NOT NULL DEFAULT '',
    expires_at  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
-- Expanding the same intent twice must not double-send.  SQLite counts NULLs as
-- distinct in a UNIQUE constraint, so a rule-less delivery (a task receipt, a
-- call) needs IFNULL here or every re-expansion would insert another copy.
CREATE UNIQUE INDEX IF NOT EXISTS idx_notify_deliveries_target
    ON notify_deliveries(outbox_id, channel_id, IFNULL(rule_id, 0));
CREATE INDEX IF NOT EXISTS idx_notify_deliveries_due
    ON notify_deliveries(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL DEFAULT '',
    sim_id           INTEGER REFERENCES sims(id) ON DELETE CASCADE,
    device           TEXT NOT NULL DEFAULT '',
    agent_id         TEXT NOT NULL DEFAULT '',
    -- The module this task runs on, by row identity.  ``device``/``agent_id``
    -- stay for the wire frame and for history, but a name is only unique
    -- within one agent, so routing by name alone can pick the wrong module
    -- once two hosts each have a ``modem-1``.
    device_id        INTEGER REFERENCES devices(id) ON DELETE SET NULL,
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


# How much of a provider's complaint is worth keeping on a queued delivery.
# Long enough for a real reason, short enough that a provider echoing the whole
# payload back cannot bloat the queue.
DETAIL_LIMIT = 300


def _shift(moment: str, seconds: float) -> str:
    """``moment`` moved by ``seconds``, in the same string form as ``utcnow``."""
    try:
        parsed = datetime.fromisoformat(moment)
    except ValueError:
        parsed = datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (parsed + timedelta(seconds=seconds)).astimezone(UTC).isoformat(
        timespec="seconds"
    )


def _age_seconds(at: str | None, now: str) -> int | None:
    """How long ago ``at`` was, or None if there is nothing there."""
    if not at:
        return None
    try:
        then = datetime.fromisoformat(at)
        moment = datetime.fromisoformat(now)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0, int((moment - then).total_seconds()))


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


class BadCursor(ValueError):
    """A history cursor that this filter cannot page with."""


@dataclass(frozen=True, slots=True)
class MessageScope:
    """Which messages a read is about — one condition object, shared.

    ``sim`` is the point.  A card used to be an ``int | None`` where ``None``
    carried two different meanings depending on who read it: the list treated
    it as "every card", ``mark_read`` as ``sim_id IS NULL``.  Opening the
    thread of a card-less message therefore listed another card's messages
    from the same number, while marking read touched only the card-less ones.
    The three cases are now spelled apart — a card id, ``"all"``, and
    ``"unassigned"`` — and every read of the table (body, total, export,
    unread, mark-read) builds its SQL from this one object.
    """

    sim: int | Literal["all", "unassigned"] = "all"
    direction: str | None = None
    peer: str | None = None
    search: str | None = None
    content: str | None = None

    def where(self, *, alias: str = "m") -> tuple[str, list[Any]]:
        """``WHERE`` clause and positional parameters for this scope.

        ``alias`` is empty for ``UPDATE messages``, which SQLite does not let
        us alias — the same clause has to serve both statements or the read
        and the write drift apart again.
        """
        column = f"{alias}." if alias else ""
        clauses: list[str] = []
        params: list[Any] = []
        if self.sim == "unassigned":
            clauses.append(f"{column}sim_id IS NULL")
        elif self.sim != "all":
            clauses.append(f"{column}sim_id = ?")
            params.append(self.sim)
        if self.direction:
            clauses.append(f"{column}direction = ?")
            params.append(self.direction)
        if self.peer:
            clauses.append(f"{column}peer = ?")
            params.append(self.peer)
        if self.search:
            # The salvaged fragment is searched alongside the body: on a damaged
            # message `body` is mojibake, so a search for the code that *was*
            # recovered would otherwise find nothing.
            clauses.append(
                f"({column}body LIKE ? OR {column}peer LIKE ? "
                f"OR {column}recovered_body LIKE ? OR {column}recovered_code LIKE ?)"
            )
            params.extend([f"%{self.search}%"] * 4)
        if self.content == "text":
            clauses.append(f"{column}is_binary = 0")
        elif self.content == "data":
            clauses.append(f"{column}is_binary = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    @property
    def fingerprint(self) -> str:
        """Short digest of the filter, carried inside this scope's cursors.

        A cursor is a position in one ordered result.  Handed to a different
        filter it points somewhere meaningless — pages would silently skip or
        repeat — so the filter it came from travels with it and a mismatch is
        an error rather than a wrong page.
        """
        canonical = json.dumps(
            [self.sim, self.direction, self.peer, self.search, self.content],
            ensure_ascii=False, sort_keys=True,
        )
        return hashlib.blake2s(canonical.encode(), digest_size=4).hexdigest()

    def cursor(self, row: Mapping[str, Any]) -> str:
        """Opaque position of ``row`` in this scope's ordering."""
        payload = f"1|{self.fingerprint}|{int(row['id'])}|{row['ts']}"
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    def parse_cursor(self, text: str) -> tuple[str, int]:
        """``(ts, id)`` from one of this scope's cursors.

        Deliberately strict — length, alphabet, field count, and the filter
        digest are all checked.  A cursor is client-supplied text that ends up
        in an ordering comparison, and "close enough" would mean paging from a
        position this query never produced.
        """
        if not text or len(text) > 160:
            raise BadCursor("cursor length")
        padded = text + "=" * (-len(text) % 4)
        try:
            payload = base64.urlsafe_b64decode(padded.encode("ascii")).decode()
        except (ValueError, UnicodeDecodeError) as exc:
            raise BadCursor("cursor encoding") from exc
        parts = payload.split("|", 3)
        if len(parts) != 4:
            raise BadCursor("cursor fields")
        version, fingerprint, raw_id, ts = parts
        if version != "1":
            raise BadCursor("cursor version")
        if fingerprint != self.fingerprint:
            raise BadCursor("cursor filter")
        if not raw_id.isdigit() or len(raw_id) > 19:
            raise BadCursor("cursor id")
        if len(ts) > 40:
            raise BadCursor("cursor timestamp")
        try:
            # Parsed, not merely measured: a cursor truncated in transit still
            # base64-decodes, and a clipped timestamp would page from a moment
            # that never existed.
            datetime.fromisoformat(ts)
        except ValueError as exc:
            raise BadCursor("cursor timestamp") from exc
        return ts, int(raw_id)


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
        # Admit at most 64 operations, including the one running. Submit whole
        # transactions so a cancelled caller cannot leave half an event behind.
        self._async_slots = asyncio.Semaphore(64)
        self._async_writer = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="hub-db"
        )
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
        (6, "record SIM billing details and lifecycle dates",
         "_migration_sim_lifecycle"),
        (7, "index message conversations and their newest rows",
         "_migration_message_conversation_index"),
        (8, "record modem firmware and registration domains",
         "_migration_modem_diagnostics"),
        (9, "record module supply voltage and its low-voltage threshold",
         "_migration_supply_voltage"),
        (10, "keep what was salvaged from a truncated message",
         "_migration_salvaged_messages"),
        (11, "record call attempts as rows rather than log text",
         "_migration_calls"),
        (12, "record packet-data and roaming policy state",
         "_migration_data_controls"),
        (13, "record the effective local packet-data policy",
         "_migration_data_policy"),
        (14, "identify Agent events by their local event-stream epoch",
         "_migration_event_stream_epoch"),
        (15, "address keep-alive tasks by module row rather than by name",
         "_migration_task_device_id"),
        (16, "persist the notifications a stored event owes",
         "_migration_notify_outbox"),
        (17, "track Agent task configuration acknowledgements",
         "_migration_task_sync"),
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

    def _migration_sim_lifecycle(self) -> None:
        """v5 -> v6: operator-maintained billing and renewal information."""
        self._add_columns_if_missing(
            "sims",
            {
                "billing_type": "TEXT NOT NULL DEFAULT 'unknown'",
                "plan_name": "TEXT NOT NULL DEFAULT ''",
                "balance": "TEXT",
                "low_balance_threshold": "TEXT",
                "currency": "TEXT NOT NULL DEFAULT ''",
                "balance_updated_at": "TEXT",
                "expires_at": "TEXT",
                "activity_due_at": "TEXT",
            },
        )

    def _migration_message_conversation_index(self) -> None:
        """v6 -> v7: serve a thread and its summary from one ordered index."""
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_conversation "
            "ON messages(sim_id, peer, ts DESC, id DESC)"
        )

    def _migration_modem_diagnostics(self) -> None:
        """v7 -> v8: retain the evidence needed to diagnose SMS routing."""
        self._add_columns_if_missing(
            "devices",
            {
                "eps_registered": "INTEGER",
                "cs_registered": "INTEGER",
                "ims_registered": "INTEGER",
                "hardware_model": "TEXT NOT NULL DEFAULT ''",
                "firmware": "TEXT NOT NULL DEFAULT ''",
            },
        )

    def _migration_supply_voltage(self) -> None:
        """v8 -> v9: keep the module supply voltage and the threshold it is judged by.

        Both tables get the reading: ``devices`` answers "what is it now" for the
        device page, ``device_status`` builds the trend, and a brown-out is only
        recognisable as one from the trend.  ``low_voltage_mv`` is stored beside
        the current reading because the threshold belongs to the module's supply
        and lives in the Agent's config — without it the Server would have to
        keep a second copy of the default and the two could disagree.
        """
        self._add_columns_if_missing(
            "devices",
            {
                "voltage_mv": "INTEGER",
                "low_voltage_mv": "INTEGER",
            },
        )
        self._add_columns_if_missing(
            "device_status",
            {"voltage_mv": "INTEGER"},
        )

    def _migration_salvaged_messages(self) -> None:
        """v9 -> v10: keep what was salvaged from a message that arrived damaged.

        Existing rows keep ``truncated = 0``.  Backfilling them is not possible
        and would be wrong to guess at: the salvage runs in the Agent against a
        frame it has just read, and a stored row's ``raw_pdu`` is the damaged
        PDU with no record of which fields were really body.  A message that
        was lost before this migration stays lost; only new ones are recovered.
        """
        self._add_columns_if_missing(
            "messages",
            {
                "truncated": "INTEGER NOT NULL DEFAULT 0",
                "recovered_body": "TEXT",
                "recovered_code": "TEXT",
            },
        )

    def _migration_calls(self) -> None:
        """v10 -> v11: give call attempts a table instead of a log line.

        Nothing is backfilled.  The history exists only as free text in
        ``logs``, and re-parsing Chinese log messages into structured rows would
        invent a precision the source never had — a parsed row would look
        exactly like a recorded one while resting on a regex over prose.  The
        log stays as the record of what happened before this point.
        """
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS calls (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id   TEXT NOT NULL,
                device     TEXT NOT NULL,
                sim_id     INTEGER REFERENCES sims(id) ON DELETE SET NULL,
                direction  TEXT NOT NULL,
                peer       TEXT NOT NULL DEFAULT '',
                ts         TEXT NOT NULL,
                outcome    TEXT NOT NULL DEFAULT '',
                reached_network INTEGER NOT NULL DEFAULT 0,
                ring_seconds    REAL NOT NULL DEFAULT 0,
                detail     TEXT NOT NULL DEFAULT '',
                seq        INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts DESC)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_calls_sim ON calls(sim_id, ts DESC)"
        )

    def _migration_data_controls(self) -> None:
        """v11 -> v12: expose packet data and the roaming safety policy."""
        self._add_columns_if_missing(
            "devices",
            {
                "data_attached": "INTEGER",
                "pdp_active": "INTEGER",
                "roaming": "INTEGER",
                "roaming_data_allowed": "INTEGER NOT NULL DEFAULT 0",
                "data_blocked_by_roaming": "INTEGER NOT NULL DEFAULT 0",
            },
        )

    def _migration_data_policy(self) -> None:
        """v12 -> v13: distinguish policy from modem attachment state."""
        self._add_columns_if_missing(
            "devices",
            {"data_enabled": "INTEGER NOT NULL DEFAULT 0"},
        )

    def _migration_event_stream_epoch(self) -> None:
        """v13 -> v14: make an event's identity include which store it came from.

        ``seq`` is an autoincrement in the agent's own SQLite file, so it is
        unique inside that file and nowhere else.  Rebuild the agent's data
        directory — a reinstall, a wiped volume, a restored host — and numbering
        starts over at 1 while this server still holds rows for 1..N of the old
        store.  Keyed on ``(agent_id, seq)`` alone, every event of the new store
        reads back as an already-applied duplicate and is acked without ever
        being applied: silent, permanent data loss for as long as the new
        sequence stays below the old high-water mark.

        Existing rows keep the empty stream id, which is exactly what an agent
        that does not report one sends, so their dedupe behaviour is unchanged.
        """
        self._add_columns_if_missing(
            "agents", {"stream_id": "TEXT NOT NULL DEFAULT ''"}
        )
        columns = {
            row[1] for row in self._db.execute("PRAGMA table_info(ingested)")
        }
        if "stream_id" in columns:
            return
        # The primary key itself has to change, which SQLite only does by
        # rebuilding.  Several statements, no executescript: the step must stay
        # inside the transaction ``_run_step`` opened around it.
        self._db.execute(
            "CREATE TABLE ingested_v14 ("
            "  agent_id  TEXT NOT NULL,"
            "  stream_id TEXT NOT NULL DEFAULT '',"
            "  seq       INTEGER NOT NULL,"
            "  kind      TEXT NOT NULL,"
            "  at        TEXT NOT NULL,"
            "  PRIMARY KEY (agent_id, stream_id, seq)"
            ")"
        )
        self._db.execute(
            "INSERT INTO ingested_v14 (agent_id, stream_id, seq, kind, at) "
            "SELECT agent_id, '', seq, kind, at FROM ingested"
        )
        self._db.execute("DROP TABLE ingested")
        self._db.execute("ALTER TABLE ingested_v14 RENAME TO ingested")

    def _migration_task_device_id(self) -> None:
        """v14 -> v15: pin each keep-alive task to a module row.

        ``devices`` is unique on ``(agent_id, name)``, so a bare device name
        stops being an address as soon as two agents each have a ``modem-1``.
        Back-fill only where the name resolves to exactly one module; an
        ambiguous task is disabled and reported rather than pointed at a guess,
        because guessing here means sending a real SMS from the wrong SIM.
        """
        self._add_columns_if_missing(
            "tasks",
            {"device_id": "INTEGER REFERENCES devices(id) ON DELETE SET NULL"},
        )
        rows = self._db.execute(
            "SELECT id, device, agent_id FROM tasks WHERE device <> ''"
        ).fetchall()
        ambiguous: list[str] = []
        for row in rows:
            if row["agent_id"]:
                matches = self._db.execute(
                    "SELECT id FROM devices WHERE name = ? AND agent_id = ?",
                    (row["device"], row["agent_id"]),
                ).fetchall()
            else:
                matches = self._db.execute(
                    "SELECT id FROM devices WHERE name = ?", (row["device"],)
                ).fetchall()
            if len(matches) == 1:
                self._db.execute(
                    "UPDATE tasks SET device_id = ?, agent_id = "
                    "(SELECT agent_id FROM devices WHERE id = ?) WHERE id = ?",
                    (matches[0]["id"], matches[0]["id"], row["id"]),
                )
            elif len(matches) > 1:
                self._db.execute(
                    "UPDATE tasks SET enabled = 0 WHERE id = ?", (row["id"],)
                )
                ambiguous.append(f"#{row['id']} {row['device']}")
        if ambiguous:
            # ``open_incident`` runs on the same connection and does not commit,
            # so it stays inside this step's transaction.
            self.open_incident(
                "task-device-ambiguous",
                kind="task_device_ambiguous",
                severity="warning",
                source="migration",
                title="保号任务的目标模块无法唯一确定",
                detail=(
                    "以下任务的设备名在多个 Agent 上都存在，已暂停以避免发到错误的卡，"
                    "请在任务页重新指定模块后启用：" + "、".join(ambiguous)
                ),
            )
            log.warning(
                "%d keep-alive task(s) disabled: ambiguous device name", len(ambiguous)
            )

    def _migration_notify_outbox(self) -> None:
        """v15 -> v16: make an owed notification survive the process.

        Nothing is backfilled.  The intents this table would hold for past
        events are precisely the pushes that already went out (or already
        failed) in a process that is gone; recreating them from ``messages``
        would re-send every SMS in the retention window at once.  The audit
        trail of what happened before this point stays in ``notify_logs``.
        """
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS notify_outbox (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                kind        TEXT NOT NULL,
                ref_id      INTEGER,
                event_key   TEXT,
                frame       TEXT NOT NULL DEFAULT '{}',
                title       TEXT NOT NULL DEFAULT '',
                body        TEXT NOT NULL DEFAULT '',
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  TEXT NOT NULL,
                expanded_at TEXT
            )
            """
        )
        self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_notify_outbox_event "
            "ON notify_outbox(event_key) WHERE event_key IS NOT NULL"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_notify_outbox_status "
            "ON notify_outbox(status, id)"
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS notify_deliveries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                outbox_id   INTEGER NOT NULL
                            REFERENCES notify_outbox(id) ON DELETE CASCADE,
                channel_id  INTEGER NOT NULL
                            REFERENCES channels(id) ON DELETE CASCADE,
                rule_id     INTEGER REFERENCES rules(id) ON DELETE CASCADE,
                status      TEXT NOT NULL DEFAULT 'pending',
                attempts    INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                lease_owner TEXT,
                lease_until TEXT,
                error_code  TEXT,
                safe_detail TEXT NOT NULL DEFAULT '',
                expires_at  TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
            """
        )
        self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_notify_deliveries_target "
            "ON notify_deliveries(outbox_id, channel_id, IFNULL(rule_id, 0))"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_notify_deliveries_due "
            "ON notify_deliveries(status, next_attempt_at)"
        )

    def _migration_task_sync(self) -> None:
        self._add_columns_if_missing("agents", {
            "tasks_revision": "TEXT NOT NULL DEFAULT ''",
            "tasks_applied_revision": "TEXT NOT NULL DEFAULT ''",
            "tasks_sync_id": "TEXT NOT NULL DEFAULT ''",
            "tasks_sync_status": "TEXT NOT NULL DEFAULT 'pending'",
            "tasks_sync_error": "TEXT NOT NULL DEFAULT ''",
            "tasks_sync_sent_at": "TEXT",
            "tasks_synced_at": "TEXT",
        })

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
        # Drain the controlled async writer before closing its connection.  A
        # cancelled HTTP request may still have a transaction finishing there.
        self._async_writer.shutdown(wait=True, cancel_futures=False)
        with self._lock:
            self._db.close()

    async def run(
        self, operation: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Any:
        """Run one complete synchronous DB operation off the event loop.

        The operation must own its transaction boundary when it writes.  If the
        caller is cancelled, the worker is still awaited before propagating the
        cancellation, which keeps a committed event and its ACK in order.
        """
        async with self._async_slots:
            with CancelScope(shield=True):
                loop = asyncio.get_running_loop()
                call = functools.partial(operation, *args, **kwargs)
                future = loop.run_in_executor(self._async_writer, call)
                cancelled = None
                while not future.done():
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError as exc:
                        cancelled = exc
                    except Exception:
                        break  # retrieve the worker's exception below
                if cancelled is not None:
                    # Retrieve failures too, without replacing cancellation.
                    if future.exception() is not None:
                        log.error("database operation failed during cancellation",
                                  exc_info=future.exception())
                    raise cancelled
                return future.result()

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

    @contextmanager
    def _readonly_connection(self) -> Iterator[sqlite3.Connection]:
        """Own a WAL reader for a query or stream, closing it on every exit."""
        if str(self.path) == ":memory:":
            with self._lock:
                yield self._db
            return
        connection = sqlite3.connect(
            # as_uri escapes '?' and '#' in actual filenames. StreamingResponse
            # may advance its iterator from different threadpool workers.
            f"{self.path.resolve().as_uri()}?mode=ro",
            uri=True, check_same_thread=False,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        finally:
            connection.close()

    def read_query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        """Run one read outside the shared write connection when possible."""
        with self._readonly_connection() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def read_one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self.read_query(sql, params)
        return rows[0] if rows else None

    # -- idempotency -------------------------------------------------------

    def apply_event(
        self,
        agent_id: str,
        seq: int,
        kind: str,
        apply: Callable[[], Any],
        *,
        stream_id: str = "",
    ) -> tuple[bool, Any]:
        """Claim and apply one Agent event atomically.

        ``(agent_id, stream_id, seq)`` is durable only if every business write
        made by ``apply`` commits with it.  A transient failure rolls the whole
        event back, allowing the Agent to replay it after the connection drops.
        Returns ``(False, None)`` for an already committed replay.

        ``stream_id`` names the agent's local event store.  It belongs in the
        key because ``seq`` restarts from 1 whenever that store is rebuilt, and
        without it the new store's events would be mistaken for replays of the
        old one's.  An agent that reports no stream id keeps the empty value, so
        its numbering is compared only against its own history.
        """
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._db.execute(
                    "INSERT OR IGNORE INTO ingested "
                    "(agent_id, stream_id, seq, kind, at) VALUES (?, ?, ?, ?, ?)",
                    (agent_id, stream_id, seq, kind, utcnow()),
                )
                if cursor.rowcount == 0:
                    # Older databases did not maintain agents.last_seq.  A
                    # replay is enough to reconcile it without a migration.
                    self._db.execute(
                        "UPDATE agents SET last_seq = MAX(last_seq, ?) WHERE id = ?",
                        (seq, agent_id),
                    )
                    self._db.execute("COMMIT")
                    return False, None

                result = apply()
                self._db.execute(
                    "UPDATE agents SET last_seq = MAX(last_seq, ?) WHERE id = ?",
                    (seq, agent_id),
                )
                self._db.execute("COMMIT")
                return True, result
            except BaseException:
                try:
                    if self._db.in_transaction:
                        self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    log.exception("rollback after failed event application also failed")
                raise

    # -- agents and devices ------------------------------------------------

    def upsert_agent(
        self,
        agent_id: str,
        version: str,
        protocol_version: int,
        connected: bool,
        *,
        stream_id: str = "",
    ) -> None:
        self.execute(
            "INSERT INTO agents "
            "(id, version, protocol_version, last_seen_at, connected, stream_id) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET version = excluded.version, "
            "protocol_version = excluded.protocol_version, "
            "last_seen_at = excluded.last_seen_at, connected = excluded.connected, "
            "stream_id = excluded.stream_id",
            (agent_id, version, protocol_version, utcnow(), int(connected), stream_id),
        )

    def set_agent_connected(self, agent_id: str, connected: bool) -> None:
        self.execute(
            "UPDATE agents SET connected = ?, last_seen_at = ? WHERE id = ?",
            (int(connected), utcnow(), agent_id),
        )

    def begin_task_sync(
        self, agent_id: str, revision: str, sync_id: str, *, sent_at: str | None = None
    ) -> None:
        self.execute(
            "UPDATE agents SET tasks_revision = ?, tasks_sync_id = ?, "
            "tasks_sync_status = 'pending', tasks_sync_error = '', tasks_sync_sent_at = ? "
            "WHERE id = ?",
            (revision, sync_id, sent_at, agent_id),
        )

    def finish_task_sync(
        self, agent_id: str, revision: str, sync_id: str, *, ok: bool, error: str = ""
    ) -> None:
        self.execute(
            "UPDATE agents SET tasks_sync_status = ?, tasks_sync_error = ?, "
            "tasks_applied_revision = CASE WHEN ? THEN ? ELSE tasks_applied_revision END, "
            "tasks_synced_at = CASE WHEN ? THEN ? ELSE tasks_synced_at END "
            "WHERE id = ? AND tasks_revision = ? AND tasks_sync_id = ? "
            "AND tasks_sync_status != 'applied'",
            ("applied" if ok else "failed", "" if ok else error,
             ok, revision, ok, utcnow(), agent_id, revision, sync_id),
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

    def reconcile_sim_incidents(self, today: date) -> None:
        """Reconcile balance, package-expiry, and keep-alive incidents.

        Values are deliberately operator-maintained. Carrier APIs, SMS, and
        USSD responses vary too much to turn into billing state without a
        provider-specific integration.
        """
        sims = self.query(
            "SELECT id, iccid, label, phone_number, plan_name, balance, "
            "low_balance_threshold, currency, expires_at, activity_due_at FROM sims"
        )
        for sim in sims:
            display = (
                sim["label"]
                or sim["phone_number"]
                or f"尾号 {sim['iccid'][-4:]}"
            )
            source = f"SIM {display}"
            plan = sim["plan_name"] or "未命名套餐"
            self._reconcile_sim_balance(sim=sim, source=source)
            self._reconcile_sim_deadline(
                sim_id=sim["id"],
                source=source,
                value=sim["expires_at"],
                today=today,
                fingerprint_prefix="sim-expiry",
                kind="sim_expiring",
                subject="套餐",
                date_label="套餐到期日",
                context=f"{plan}；",
                action="请及时续费并更新套餐到期日。",
            )
            self._reconcile_sim_deadline(
                sim_id=sim["id"],
                source=source,
                value=sim["activity_due_at"],
                today=today,
                fingerprint_prefix="sim-activity",
                kind="sim_activity_due",
                subject="保号期限",
                date_label="保号截止日",
                context="",
                action="请按运营商规则产生有效活动，并更新保号截止日。",
            )

    def _reconcile_sim_balance(self, *, sim: dict[str, Any], source: str) -> None:
        """Open or resolve the low-balance incident for one SIM."""
        fingerprint = f"sim-balance:{sim['id']}"
        balance_value = sim["balance"]
        threshold_value = sim["low_balance_threshold"]
        if balance_value is None:
            self.resolve_incident(
                fingerprint, detail="SIM 余额已清空，低余额提醒已关闭"
            )
            return
        if threshold_value is None:
            self.resolve_incident(
                fingerprint, detail="SIM 低余额阈值已清空，提醒已关闭"
            )
            return

        try:
            balance = Decimal(balance_value)
            threshold = Decimal(threshold_value)
            if not balance.is_finite() or not threshold.is_finite() or threshold < 0:
                raise InvalidOperation
        except (InvalidOperation, TypeError, ValueError):
            log.warning(
                "SIM %s has invalid balance data: balance=%r threshold=%r",
                sim["id"],
                balance_value,
                threshold_value,
            )
            self.resolve_incident(
                fingerprint, detail="SIM 余额或低余额阈值格式无效，提醒已关闭"
            )
            return

        currency = sim["currency"]
        current = f"{currency} {balance_value}".strip()
        threshold_display = f"{currency} {threshold_value}".strip()
        if balance > threshold:
            self.resolve_incident(
                fingerprint, detail=f"SIM 余额已恢复至 {current}"
            )
            return

        if balance < 0:
            severity = "critical"
            title = f"{source} 余额为负"
        elif balance == 0:
            severity = "critical"
            title = f"{source} 余额已用尽"
        else:
            severity = "warning"
            title = f"{source} 余额不足"

        self.open_incident(
            fingerprint,
            kind="sim_low_balance",
            severity=severity,
            source=source,
            title=title,
            detail=(
                f"当前余额 {current}，低余额阈值 {threshold_display}。"
                "请及时充值并更新余额。"
            ),
        )

    def _reconcile_sim_deadline(
        self,
        *,
        sim_id: int,
        source: str,
        value: str | None,
        today: date,
        fingerprint_prefix: str,
        kind: str,
        subject: str,
        date_label: str,
        context: str,
        action: str,
    ) -> None:
        """Apply the shared 30/7-day policy to one SIM lifecycle date."""
        fingerprint = f"{fingerprint_prefix}:{sim_id}"
        if not value:
            self.resolve_incident(
                fingerprint, detail=f"SIM {date_label}已清空，提醒已关闭"
            )
            return

        try:
            deadline = date.fromisoformat(value)
        except (TypeError, ValueError):
            log.warning("SIM %s has an invalid %s: %r", sim_id, date_label, value)
            self.resolve_incident(
                fingerprint, detail=f"SIM {date_label}格式无效，提醒已关闭"
            )
            return

        days = (deadline - today).days
        if days > 30:
            self.resolve_incident(
                fingerprint, detail=f"SIM {date_label}已更新至 {value}"
            )
            return

        if days < 0:
            severity = "critical"
            title = f"{source} {subject}已过期"
            timing = f"已过期 {-days} 天"
        elif days == 0:
            severity = "critical"
            title = f"{source} {subject}今天到期"
            timing = "今天到期"
        else:
            severity = "critical" if days <= 7 else "warning"
            title = f"{source} {subject}即将到期"
            timing = f"还有 {days} 天到期"

        self.open_incident(
            fingerprint,
            kind=kind,
            severity=severity,
            source=source,
            title=title,
            detail=f"{context}{date_label} {value}，{timing}。{action}",
        )

    # Fields a status event may legitimately set to a falsy value (0 signal,
    # offline, empty store) — absence of the key is what means "unchanged".
    _DEVICE_STATE_FIELDS = (
        "online",
        "registered",
        "radio_enabled",
        "eps_registered",
        "cs_registered",
        "ims_registered",
        "data_enabled",
        "data_attached",
        "pdp_active",
        "roaming",
        "roaming_data_allowed",
        "data_blocked_by_roaming",
        "rssi",
        "dbm",
        "bars",
        "rsrp",
        "rsrq",
        # Both may legitimately be NULL: a firmware that refuses AT+CBC has no
        # reading to give, and writing NULL over a previous good one is right —
        # "the module stopped answering" is not the same as the last value it
        # happened to report.
        "voltage_mv",
        "low_voltage_mv",
    )
    # Identity fields.  A blank here always means "this frame didn't carry it",
    # never "the module lost its IMEI" — status frames are a subset of hello,
    # so overwriting on blank would erase the card's name every 60 seconds.
    _DEVICE_IDENTITY_FIELDS = (
        "label",
        "port",
        "model",
        "hardware_model",
        "firmware",
        "imei",
        "operator",
    )

    def upsert_device(self, agent_id: str, payload: dict[str, Any]) -> int:
        name = payload.get("name", "")
        now = utcnow()

        columns: dict[str, Any] = {"agent_id": agent_id, "name": name, "last_seen_at": now}

        for field in self._DEVICE_STATE_FIELDS:
            if field in payload:
                value = payload[field]
                if field in (
                    "radio_enabled",
                    "eps_registered",
                    "cs_registered",
                    "ims_registered",
                    "data_enabled",
                    "data_attached",
                    "pdp_active",
                    "roaming",
                    "roaming_data_allowed",
                    "data_blocked_by_roaming",
                ):
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
            " storage_used, storage_cap, voltage_mv) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                # The threshold is deliberately not stored per sample: it is
                # configuration, not measurement, and it lives on `devices`.
                payload.get("voltage_mv"),
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
        truncated: bool = False,
        recovered_body: str | None = None,
        recovered_code: str | None = None,
    ) -> int:
        sim_id = self.upsert_sim(iccid)
        cursor = self.execute(
            "INSERT INTO messages "
            "(agent_id, device, sim_id, direction, peer, body, ts, status, "
            " segments, seq, error, raw_pdu, dcs, is_binary, truncated, "
            " recovered_body, recovered_code, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent_id, device, sim_id, direction, peer, body, to_utc_iso(ts), status,
             segments, seq, error, raw_pdu, dcs, int(is_binary), int(truncated),
             recovered_body or None, recovered_code or None, utcnow()),
        )
        return int(cursor.lastrowid)

    def insert_call(
        self,
        *,
        agent_id: str,
        device: str,
        direction: str,
        ts: str,
        peer: str = "",
        iccid: str = "",
        outcome: str = "",
        reached_network: bool = False,
        ring_seconds: float = 0.0,
        detail: str = "",
        seq: int | None = None,
    ) -> int:
        sim_id = self.upsert_sim(iccid)
        cursor = self.execute(
            "INSERT INTO calls "
            "(agent_id, device, sim_id, direction, peer, ts, outcome, "
            " reached_network, ring_seconds, detail, seq, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent_id, device, sim_id, direction, peer, to_utc_iso(ts), outcome,
             int(reached_network), float(ring_seconds), detail, seq, utcnow()),
        )
        return int(cursor.lastrowid)

    def _call_filter(
        self, *, sim_id: int | None = None, direction: str | None = None
    ) -> tuple[str, list[Any]]:
        """Shared WHERE for the call log, so the list and its total agree."""
        clauses: list[str] = []
        params: list[Any] = []
        if sim_id is not None:
            clauses.append("c.sim_id = ?")
            params.append(sim_id)
        if direction:
            clauses.append("c.direction = ?")
            params.append(direction)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def count_calls(
        self, *, sim_id: int | None = None, direction: str | None = None
    ) -> int:
        where, params = self._call_filter(sim_id=sim_id, direction=direction)
        row = self.one(f"SELECT COUNT(*) AS n FROM calls c {where}", tuple(params))
        return int(row["n"]) if row else 0

    def calls(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        sim_id: int | None = None,
        direction: str | None = None,
    ) -> list[dict[str, Any]]:
        """Newest call attempts first, with the card label the UI shows."""
        where, params = self._call_filter(sim_id=sim_id, direction=direction)
        params.extend((limit, offset))
        return self.query(
            "SELECT c.*, s.label AS sim_label, s.phone_number, "
            "s.iccid AS sim_iccid, d.label AS device_label FROM calls c "
            "LEFT JOIN sims s ON s.id = c.sim_id "
            "LEFT JOIN devices d ON d.agent_id = c.agent_id AND d.name = c.device "
            f"{where} ORDER BY c.ts DESC, c.id DESC LIMIT ? OFFSET ?",
            tuple(params),
        )

    def last_reached_network_at(self, sim_id: int) -> str | None:
        """When this card last provably reached the carrier, by call.

        The keep-alive question in one query.  ``reached_network`` rather than a
        successful ``outcome``: a busy or unanswered call still proves the
        carrier processed the attempt, which is what an activity window counts.
        """
        row = self.one(
            "SELECT ts FROM calls WHERE sim_id = ? AND reached_network = 1 "
            "ORDER BY ts DESC LIMIT 1",
            (sim_id,),
        )
        return row["ts"] if row else None

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

        The first pass is deliberately covering: it scans only the compact
        conversation index to find the newest threads and their counts.  Body,
        status and unread rows are then read only for the limited result set.
        Reading those columns during the group scan makes SQLite visit the
        table for every stored message, which is an order of magnitude slower
        at 100k rows.
        """
        params: dict[str, Any] = {"limit": limit}
        summary_filter = ""
        recent_filter = ""
        unread_filter = ""
        if content in {"text", "data"}:
            params["is_binary"] = int(content == "data")
            summary_filter = "WHERE m.is_binary = :is_binary"
            recent_filter = "AND recent.is_binary = :is_binary"
            unread_filter = "AND unread.is_binary = :is_binary"
        return self.read_query(
            "WITH summary AS ("
            "  SELECT m.sim_id, m.peer, MAX(m.ts) AS last_ts, "
            "         COUNT(*) AS message_count "
            "  FROM messages m "
            f"  {summary_filter} "
            "  GROUP BY m.sim_id, m.peer "
            "  ORDER BY last_ts DESC LIMIT :limit"
            ") "
            "SELECT summary.sim_id, summary.peer, latest.device, "
            "       latest.id AS last_id, latest.body AS last_body, "
            "       latest.is_binary AS last_is_binary, "
            # A damaged newest message is is_binary too, but the list must not
            # call it an operator data SMS — it was a person writing to a
            # person, and the salvage is what the preview should show.
            "       latest.truncated AS last_truncated, "
            "       latest.recovered_body AS last_recovered_body, "
            "       latest.direction AS last_direction, "
            "       latest.status AS last_status, summary.last_ts, "
            "       summary.message_count, "
            "       (SELECT COUNT(*) FROM messages unread "
            "        WHERE unread.sim_id IS summary.sim_id "
            "          AND unread.peer = summary.peer "
            "          AND unread.direction = 'in' "
            "          AND unread.read_at IS NULL "
            f"          {unread_filter}) AS unread_count, "
            "       s.label AS sim_label, s.iccid AS sim_iccid "
            "FROM summary "
            "JOIN messages latest ON latest.id = ("
            "  SELECT recent.id FROM messages recent "
            "  WHERE recent.sim_id IS summary.sim_id "
            "    AND recent.peer = summary.peer "
            f"    {recent_filter} "
            "  ORDER BY recent.ts DESC, recent.id DESC LIMIT 1"
            ") "
            "LEFT JOIN sims s ON s.id = summary.sim_id "
            "ORDER BY summary.last_ts DESC",
            params,
        )

    def mark_read(
        self, scope: MessageScope, *, through_id: int | None = None
    ) -> int:
        """Mark one thread's incoming messages read; returns how many.

        ``through_id`` is the watermark the client actually saw.  Without it a
        message that lands between the render and this request would be marked
        read before anyone read it, which is how a verification code goes
        missing: it never appears as unread anywhere.
        """
        where, params = scope.where(alias="")
        clause = f"{where} AND " if where else "WHERE "
        watermark = ""
        if through_id is not None:
            watermark = " AND id <= ?"
        now = utcnow()
        with self._lock:
            cur = self._db.execute(
                f"UPDATE messages SET read_at = ? {clause}"
                f"direction = 'in' AND read_at IS NULL{watermark}",
                (now, *params, *([through_id] if through_id is not None else [])),
            )
        return cur.rowcount

    def unread_total(self) -> int:
        row = self.read_one(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE direction = 'in' AND read_at IS NULL"
        )
        return int(row["n"]) if row else 0

    def messages(
        self,
        scope: MessageScope | None = None,
        *,
        limit: int | None = 50,
        offset: int = 0,
        before: str | None = None,
    ) -> list[dict[str, Any]]:
        """Newest first.  ``before`` pages older than one of this scope's cursors.

        Offset paging is kept for the tables that show a page number; a thread
        transcript uses the cursor instead, because an offset boundary gaps or
        repeats when a message arrives mid-scroll.
        """
        scope = scope or MessageScope()
        where, params = scope.where()
        if before is not None:
            ts, row_id = scope.parse_cursor(before)
            # Matching ORDER BY (ts DESC, id DESC): equal timestamps are common
            # — a multipart SMS stores every segment with one SCTS — so the id
            # has to break the tie here exactly as it does in the ordering.
            keyword = "AND" if where else "WHERE"
            where = f"{where} {keyword} (m.ts < ? OR (m.ts = ? AND m.id < ?))"
            params.extend([ts, ts, row_id])
        sql = (
            "SELECT m.*, s.label AS sim_label, s.iccid AS sim_iccid "
            "FROM messages m LEFT JOIN sims s ON s.id = m.sim_id "
            f"{where} ORDER BY m.ts DESC, m.id DESC"
        )
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        return self.read_query(sql, tuple(params))

    def iter_messages(
        self,
        scope: MessageScope | None = None,
        *,
        limit: int | None = None,
        batch_size: int = 500,
    ) -> Iterable[dict[str, Any]]:
        """Stream a stable read without retaining every message in memory.

        A separate read-only connection lets WAL continue accepting gateway
        writes while a large export is being downloaded. In-memory databases
        are only used by tests, where falling back to a bounded list is fine.
        """
        scope = scope or MessageScope()
        if str(self.path) == ":memory:":
            yield from self.messages(scope, limit=limit)
            return

        where, params = scope.where()
        sql = (
            "SELECT m.*, s.label AS sim_label, s.iccid AS sim_iccid "
            "FROM messages m LEFT JOIN sims s ON s.id = m.sim_id "
            f"{where} ORDER BY m.ts DESC, m.id DESC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        with self._readonly_connection() as connection:
            cursor = connection.execute(sql, tuple(params))
            while rows := cursor.fetchmany(batch_size):
                for row in rows:
                    yield dict(row)

    def count_messages(self, scope: MessageScope | None = None) -> int:
        where, params = (scope or MessageScope()).where()
        row = self.read_one(
            f"SELECT COUNT(*) AS n FROM messages m {where}", tuple(params)
        )
        return int(row["n"]) if row else 0

    def message_trend(self, *, since: str) -> list[dict[str, Any]]:
        """Daily per-card counts since a normalized UTC timestamp."""
        return self.read_query(
            "SELECT date(ts) AS day, sim_id, "
            "       SUM(CASE WHEN direction = 'in' THEN 1 ELSE 0 END) AS received, "
            "       SUM(CASE WHEN direction = 'out' THEN 1 ELSE 0 END) AS sent "
            "FROM messages WHERE ts >= ? "
            "GROUP BY date(ts), sim_id ORDER BY day",
            (since,),
        )

    # -- notification outbox -------------------------------------------------

    def enqueue_notification(
        self,
        kind: str,
        *,
        ref_id: int | None = None,
        frame: Mapping[str, Any] | None = None,
        event_key: str | None = None,
        title: str = "",
        body: str = "",
    ) -> int | None:
        """Record that a notification is owed.  Returns its id, or None.

        None means the intent was already queued: ``event_key`` identifies the
        agent event behind it, and a replay after a lost ack arrives with the
        same key.  Callers treat that as success, because the first insert is
        what will be delivered.

        Meant to be called from inside ``apply_event``'s callback, where it joins
        the event's own transaction — the point of the table is that a COMMIT is
        already a promise to notify.
        """
        cursor = self.execute(
            "INSERT OR IGNORE INTO notify_outbox "
            "(kind, ref_id, event_key, frame, title, body, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (
                kind,
                ref_id,
                event_key,
                json.dumps(dict(frame or {}), ensure_ascii=False),
                title,
                body,
                utcnow(),
            ),
        )
        return int(cursor.lastrowid) if cursor.rowcount else None

    def pending_intents(self, limit: int = 50) -> list[dict[str, Any]]:
        """Intents that still have to be turned into per-channel deliveries."""
        return self.query(
            "SELECT * FROM notify_outbox WHERE status = 'pending' "
            "ORDER BY id LIMIT ?",
            (limit,),
        )

    def add_delivery(
        self,
        outbox_id: int,
        channel_id: int,
        *,
        rule_id: int | None = None,
        expires_at: str | None = None,
    ) -> int | None:
        """Queue one push for one channel.  None if it is already queued."""
        now = utcnow()
        cursor = self.execute(
            "INSERT OR IGNORE INTO notify_deliveries "
            "(outbox_id, channel_id, rule_id, status, attempts, next_attempt_at, "
            " expires_at, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?)",
            (outbox_id, channel_id, rule_id, now, expires_at, now, now),
        )
        return int(cursor.lastrowid) if cursor.rowcount else None

    def finish_intent(
        self,
        outbox_id: int,
        status: str,
        *,
        title: str | None = None,
        body: str | None = None,
    ) -> None:
        """Mark an intent expanded (deliveries exist) or skipped (nothing to do).

        ``title``/``body`` carry the text rendered while expanding, for the kinds
        whose wording is fixed by the event rather than by a channel template.
        """
        self.execute(
            "UPDATE notify_outbox SET status = ?, expanded_at = ?, "
            "title = COALESCE(?, title), body = COALESCE(?, body) WHERE id = ?",
            (status, utcnow(), title or None, body or None, outbox_id),
        )

    def claim_deliveries(
        self, *, owner: str, lease_seconds: float, limit: int, now: str | None = None
    ) -> list[dict[str, Any]]:
        """Take ownership of up to ``limit`` due deliveries.

        Claiming and reading happen in one transaction so two workers -- or one
        worker whose previous pass has not finished -- cannot both send the same
        push.  The lease is a timestamp rather than a state: a process that dies
        holding it leaves rows that become due again on their own, which is what
        makes recovery need no cleanup pass.
        """
        moment = now or utcnow()
        until = _shift(moment, lease_seconds)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                rows = [
                    dict(row)
                    for row in self._db.execute(
                        "SELECT d.*, o.kind, o.ref_id, o.frame, o.title, o.body "
                        "FROM notify_deliveries d "
                        "JOIN notify_outbox o ON o.id = d.outbox_id "
                        "WHERE d.status = 'pending' AND d.next_attempt_at <= ? "
                        "  AND (d.lease_until IS NULL OR d.lease_until <= ?) "
                        "ORDER BY d.next_attempt_at, d.id LIMIT ?",
                        (moment, moment, limit),
                    ).fetchall()
                ]
                for row in rows:
                    self._db.execute(
                        "UPDATE notify_deliveries "
                        "SET lease_owner = ?, lease_until = ?, updated_at = ? "
                        "WHERE id = ?",
                        (owner, until, moment, row["id"]),
                    )
                    row["lease_owner"], row["lease_until"] = owner, until
                self._db.execute("COMMIT")
                return rows
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def settle_delivery(
        self,
        delivery_id: int,
        *,
        status: str,
        attempts: int,
        error_code: str | None = None,
        safe_detail: str = "",
        next_attempt_at: str | None = None,
    ) -> None:
        """Write back the outcome of one attempt.

        ``status`` stays 'pending' for a retry, with ``next_attempt_at`` saying
        when; the lease is released either way so a worker that stops between
        attempts does not park the row until its lease runs out.
        """
        self.execute(
            "UPDATE notify_deliveries SET status = ?, attempts = ?, "
            "error_code = ?, safe_detail = ?, next_attempt_at = ?, "
            "lease_owner = NULL, lease_until = NULL, updated_at = ? "
            "WHERE id = ?",
            (
                status,
                attempts,
                error_code,
                safe_detail[:DETAIL_LIMIT],
                next_attempt_at or utcnow(),
                utcnow(),
                delivery_id,
            ),
        )

    def retry_delivery(self, delivery_id: int) -> bool:
        """Put a given-up delivery back in the queue.  False if there is none.

        Attempts are deliberately not reset: the count is the history of what
        this push cost, and an operator retrying a channel they just fixed wants
        the next failure to give up promptly rather than start the budget over.
        """
        cursor = self.execute(
            "UPDATE notify_deliveries SET status = 'pending', next_attempt_at = ?, "
            "lease_owner = NULL, lease_until = NULL, updated_at = ? "
            "WHERE id = ? AND status IN ('failed', 'expired')",
            (utcnow(), utcnow(), delivery_id),
        )
        return bool(cursor.rowcount)

    def notify_backlog(self, *, now: str | None = None) -> dict[str, Any]:
        """What the queue looks like, for the operations page.

        ``oldest_pending_age_seconds`` is the number that matters: a backlog of
        20 that is seconds old is a busy minute, the same 20 an hour old means
        deliveries have stopped moving.
        """
        moment = now or utcnow()
        counts = {
            str(row["status"]): int(row["n"])
            for row in self.query(
                "SELECT status, COUNT(*) AS n FROM notify_deliveries GROUP BY status"
            )
        }
        oldest = self.one(
            "SELECT MIN(created_at) AS at FROM notify_deliveries WHERE status = 'pending'"
        )
        due = self.one(
            "SELECT COUNT(*) AS n FROM notify_deliveries "
            "WHERE status = 'pending' AND next_attempt_at <= ?",
            (moment,),
        )
        unexpanded = self.one(
            "SELECT COUNT(*) AS n FROM notify_outbox WHERE status = 'pending'"
        )
        return {
            "pending": counts.get("pending", 0),
            "failed": counts.get("failed", 0),
            "expired": counts.get("expired", 0),
            "ok": counts.get("ok", 0),
            "due_now": int((due or {}).get("n") or 0),
            "unexpanded_intents": int((unexpanded or {}).get("n") or 0),
            "oldest_pending_at": (oldest or {}).get("at"),
            "oldest_pending_age_seconds": _age_seconds((oldest or {}).get("at"), moment),
            "failures_by_reason": {
                str(row["error_code"] or "unknown"): int(row["n"])
                for row in self.query(
                    "SELECT error_code, COUNT(*) AS n FROM notify_deliveries "
                    "WHERE status IN ('failed', 'expired') "
                    "GROUP BY error_code ORDER BY n DESC LIMIT 10"
                )
            },
        }

    def next_delivery_due_at(self) -> str | None:
        """When the earliest queued push becomes due, or None if none is queued.

        The worker sleeps on this rather than on a fixed interval: a retry with
        no backoff is due immediately and should not wait out a poll, and one
        told to come back in five minutes should not be looked at sixty times
        first.  Rows under lease are included — their lease expiring is itself
        something to come back for.
        """
        row = self.one(
            "SELECT MIN(CASE WHEN lease_until > next_attempt_at "
            "THEN lease_until ELSE next_attempt_at END) AS at FROM notify_deliveries "
            "WHERE status = 'pending'"
        )
        return (row or {}).get("at")

    def stuck_deliveries(self, limit: int = 20) -> list[dict[str, Any]]:
        """Given-up deliveries, newest first, with what to call the channel."""
        return self.query(
            "SELECT d.*, o.kind, o.ref_id, c.name AS channel_name "
            "FROM notify_deliveries d "
            "JOIN notify_outbox o ON o.id = d.outbox_id "
            "LEFT JOIN channels c ON c.id = d.channel_id "
            "WHERE d.status IN ('failed', 'expired') "
            "ORDER BY d.updated_at DESC LIMIT ?",
            (limit,),
        )

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
                "day": int(self.read_one(sql, (day,))["n"]),
                "week": int(self.read_one(sql, (week,))["n"]),
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
            table: int(self.read_one(f"SELECT COUNT(*) AS n FROM {table}")["n"])
            for table in self._ROW_COUNT_TABLES
        }
        return {
            "messages": messages,
            "notifications": notifications,
            "tasks": tasks,
            "rows": rows,
        }

    # -- retention ---------------------------------------------------------

    def purge(self, **retention: int) -> dict[str, int]:
        removed = {}
        for batch in self.purge_batches(**retention):
            removed = batch
        return removed

    async def purge_async(self, **retention: int) -> dict[str, int]:
        batches = self.purge_batches(**retention)
        removed = {}
        while (batch := await self.run(next, batches, None)) is not None:
            removed = batch
        return removed

    def purge_batches(
        self,
        *,
        message_days: int,
        status_days: int,
        log_days: int = 0,
        audit_days: int = 0,
        incident_days: int = 0,
        audit_max_rows: int = 0,
        ingested_days: int = 0,
        batch_size: int = 500,
    ) -> Iterator[dict[str, int]]:
        """Commit bounded deletes, yielding cumulative counts after each batch.

        Every append-only table needs a horizon of its own.  Only notify_logs
        tied to a message disappear with it (ON DELETE CASCADE); rows from task
        receipts and channel tests carry no message_id and would otherwise
        outlive every other trace of the event.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        removed = {
            "messages": 0, "status": 0, "ingested": 0,
            "agent_logs": 0, "task_logs": 0, "notify_logs": 0,
            "audit_events": 0, "incidents": 0,
        }
        now = datetime.now(UTC)
        specs = []
        # Identifiers and predicates are private constants. Cutoffs stay fixed
        # throughout a pass, even while newer events arrive between batches.
        for key, table, days, predicate in (
            ("messages", "messages", message_days, "ts < ?"),
            (None, "sms_delivery_segments", message_days,
             "message_id IS NULL AND created_at < ?"),
            ("status", "device_status", status_days, "ts < ?"),
            ("agent_logs", "agent_logs", log_days, "ts < ?"),
            ("task_logs", "task_logs", log_days, "ts < ?"),
            ("notify_logs", "notify_logs", log_days, "ts < ?"),
            ("audit_events", "audit_events", audit_days, "ts < ?"),
            ("incidents", "incidents", incident_days,
             "status = 'resolved' AND resolved_at < ?"),
            # Opt-in only: expiring dedupe rows permits older event replays.
            ("ingested", "ingested", ingested_days, "at < ?"),
        ):
            if days > 0:
                specs.append((key, table, predicate, (now - timedelta(days=days)).isoformat()))
        if audit_max_rows > 0:
            # Keep the newest rows.  id is monotonic, so the cutoff id is the
            # one audit_max_rows back from the newest.
            row = self.one(
                "SELECT id FROM audit_events ORDER BY id DESC LIMIT 1 OFFSET ?",
                (audit_max_rows - 1,),
            )
            if row is not None:
                specs.append(("audit_events", "audit_events", "id < ?", row["id"]))
        for key, table, predicate, cutoff in specs:
            while True:
                started = time.monotonic()
                count = self.execute(
                    f"DELETE FROM {table} WHERE rowid IN "
                    f"(SELECT rowid FROM {table} WHERE {predicate} LIMIT ?)",
                    (cutoff, batch_size),
                ).rowcount
                if key is not None:
                    removed[key] += count
                log.debug("purge table=%s removed=%d duration_ms=%.3f batch_full=%s",
                          table, count, (time.monotonic() - started) * 1000,
                          count == batch_size)
                yield dict(removed)
                if count < batch_size:
                    break
        yield dict(removed)

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
