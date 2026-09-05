"""Durable local state for the agent.

Plain synchronous ``sqlite3`` on the event loop.  At this workload — a
handful of messages a minute, a status sample a minute — every statement here
is sub-millisecond, and the complexity of an async driver buys nothing.  If
that ever stops being true the call sites are already narrow enough to move
behind a thread executor.

The agent is the source of truth.  Two things must survive a
restart or a network outage:

* the outbound event queue, so nothing received is ever lost
* the keep-alive task table, so scheduling does not depend on the server
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

-- The durable outbound queue.  AUTOINCREMENT matters: without it SQLite
-- reuses rowids after deletes, and a replayed seq would look like an old
-- event to the server.
CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,
    payload     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    acked       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_unacked ON events(acked, seq);

-- Local message history, independent of whatever the server has.
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device      TEXT    NOT NULL,
    iccid       TEXT,
    direction   TEXT    NOT NULL,
    peer        TEXT    NOT NULL,
    body        TEXT    NOT NULL,
    ts          TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'received',
    segments    INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts DESC);

-- Keep-alive tasks, mirrored from the server but executed locally.
CREATE TABLE IF NOT EXISTS tasks (
    id               INTEGER PRIMARY KEY,
    device           TEXT    NOT NULL,
    name             TEXT    NOT NULL DEFAULT '',
    enabled          INTEGER NOT NULL DEFAULT 1,
    action           TEXT    NOT NULL DEFAULT 'send_sms',
    target_number    TEXT    NOT NULL DEFAULT '',
    content          TEXT    NOT NULL DEFAULT '',
    schedule_type    TEXT    NOT NULL DEFAULT 'interval',
    schedule_expr    TEXT    NOT NULL DEFAULT '25',
    jitter_seconds   INTEGER NOT NULL DEFAULT 1800,
    random_suffix    INTEGER NOT NULL DEFAULT 1,
    retry_max        INTEGER NOT NULL DEFAULT 3,
    notify_on_result INTEGER NOT NULL DEFAULT 1,
    last_run_at      TEXT,
    next_run_at      TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

TASK_FIELDS = (
    "id", "device", "name", "enabled", "action", "target_number", "content",
    "schedule_type", "schedule_expr", "jitter_seconds", "random_suffix",
    "retry_max", "notify_on_result",
)

# kv key holding the label for this store's sequence-number space.
STREAM_ID_KEY = "stream_id"
TASK_REVISION_KEY = "tasks_revision"


def task_revision(tasks: list[dict[str, Any]]) -> str:
    encoded = json.dumps(tasks, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Event:
    seq: int
    kind: str
    payload: dict[str, Any]

    def to_frame(self) -> dict[str, Any]:
        return {"type": self.kind, "seq": self.seq, **self.payload}


class LocalStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._ensure_stream_id()

    def close(self) -> None:
        self._db.close()

    # -- outbound queue ----------------------------------------------------

    def append_event(self, kind: str, payload: dict[str, Any]) -> Event:
        now = utcnow()
        cursor = self._db.execute(
            "INSERT INTO events (kind, payload, created_at) VALUES (?, ?, ?)",
            (kind, json.dumps(payload, ensure_ascii=False), now),
        )
        return Event(seq=int(cursor.lastrowid), kind=kind, payload=payload)

    def unacked_events(self, limit: int = 500) -> list[Event]:
        rows = self._db.execute(
            "SELECT seq, kind, payload FROM events "
            "WHERE acked = 0 ORDER BY seq LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            Event(seq=row["seq"], kind=row["kind"], payload=json.loads(row["payload"]))
            for row in rows
        ]

    def events_from(self, seq: int, limit: int = 500) -> list[Event]:
        rows = self._db.execute(
            "SELECT seq, kind, payload FROM events WHERE seq >= ? ORDER BY seq LIMIT ?",
            (seq, limit),
        ).fetchall()
        return [
            Event(seq=row["seq"], kind=row["kind"], payload=json.loads(row["payload"]))
            for row in rows
        ]

    def ack_through(self, seq: int) -> int:
        """Cumulative ack: mark everything up to and including ``seq`` done."""
        cursor = self._db.execute(
            "UPDATE events SET acked = 1 WHERE acked = 0 AND seq <= ?", (seq,)
        )
        self._db.execute("DELETE FROM events WHERE acked = 1 AND seq <= ?", (seq,))
        return cursor.rowcount

    def unacked_count(self) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM events WHERE acked = 0"
        ).fetchone()
        return int(row["n"])

    def last_seq(self) -> int:
        row = self._db.execute("SELECT MAX(seq) AS s FROM events").fetchone()
        if row["s"] is not None:
            return int(row["s"])
        # The table can be empty because everything was acked and deleted;
        # the sequence generator still remembers where it got to.
        row = self._db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'events'"
        ).fetchone()
        return int(row["seq"]) if row else 0

    def stream_id(self) -> str:
        """Stable label for *this* sequence-number space.

        Sequence numbers are only unique within one store file: lose or replace
        it and AUTOINCREMENT restarts at 1, so seq 1..N are handed out a second
        time to entirely different events.  The server dedupes on
        (agent_id, stream_id, seq), so a rebuilt queue has to say so — without
        this it looks like a replay of events the server already applied and
        every message on the new queue is silently dropped.
        """
        return self.get(STREAM_ID_KEY) or ""

    def _ensure_stream_id(self) -> None:
        """Label the sequence space, once, when the store is opened.

        Open time is when the answer is knowable: a file with no sequence
        history at all is a new space and gets a fresh label, while one that
        has already handed out numbers keeps the empty legacy label.  Those
        numbers were ingested under it, and re-labelling them on upgrade would
        un-dedupe every event still waiting for an ACK.
        """
        if self.get(STREAM_ID_KEY) is not None:
            return
        self.set(STREAM_ID_KEY, "" if self.last_seq() else secrets.token_hex(8))

    def trim_events(self, keep: int) -> int:
        """Drop the oldest *status* events when the queue runs away.

        Received messages are never discarded — a lost status sample is a gap
        in a graph, a lost message is a lost verification code.
        """
        excess = self.unacked_count() - keep
        if excess <= 0:
            return 0
        cursor = self._db.execute(
            "DELETE FROM events WHERE seq IN ("
            "  SELECT seq FROM events WHERE acked = 0 AND kind = 'status'"
            "  ORDER BY seq LIMIT ?"
            ")",
            (excess,),
        )
        if cursor.rowcount:
            log.warning("event queue over %d, dropped %d status events",
                        keep, cursor.rowcount)
        return cursor.rowcount

    # -- local message history --------------------------------------------

    def record_message(
        self,
        *,
        device: str,
        direction: str,
        peer: str,
        body: str,
        ts: str,
        iccid: str | None = None,
        status: str = "received",
        segments: int = 1,
    ) -> int:
        cursor = self._db.execute(
            "INSERT INTO messages "
            "(device, iccid, direction, peer, body, ts, status, segments, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (device, iccid, direction, peer, body, ts, status, segments, utcnow()),
        )
        return int(cursor.lastrowid)

    def recent_messages(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def message_count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"])

    # -- tasks -------------------------------------------------------------

    def replace_tasks(
        self,
        tasks: Iterable[dict[str, Any]],
        *,
        revision: str = "",
        sync_id: str = "",
    ) -> int:
        """Apply a full ``sync_tasks`` payload.

        Full-replace semantics: anything absent from the payload is deleted.
        Run times are preserved for tasks that survive, so re-syncing does not
        reset a keep-alive schedule — *unless* the schedule itself changed, in
        which case the planned next run is dropped and the scheduler re-plans
        from the new expression.
        """
        incoming = list(tasks)
        keep: set[int] = set()
        for task in incoming:
            task_id = task.get("id") if isinstance(task, dict) else None
            if type(task_id) is not int or task_id <= 0 or task_id in keep:
                raise ValueError("tasks need distinct positive integer ids")
            keep.add(task_id)

        # This connection uses autocommit; the context manager alone does not
        # start a transaction. The replacement and its receipt must commit together.
        self._db.execute("BEGIN IMMEDIATE")
        with self._db:
            if keep:
                placeholders = ",".join("?" * len(keep))
                self._db.execute(
                    f"DELETE FROM tasks WHERE id NOT IN ({placeholders})",
                    tuple(keep),
                )
            else:
                self._db.execute("DELETE FROM tasks")

            for task in incoming:
                values = {field: task.get(field) for field in TASK_FIELDS}
                values["enabled"] = int(bool(task.get("enabled", True)))
                values["random_suffix"] = int(bool(task.get("random_suffix", True)))
                values["notify_on_result"] = int(bool(task.get("notify_on_result", True)))
                columns = ",".join(TASK_FIELDS)
                placeholders = ",".join(f":{field}" for field in TASK_FIELDS)
                updates = ",".join(
                    f"{field}=excluded.{field}" for field in TASK_FIELDS if field != "id"
                )
                if self._schedule_changed(values):
                    updates += ",next_run_at=NULL"
                self._db.execute(
                    f"INSERT INTO tasks ({columns}) VALUES ({placeholders}) "
                    f"ON CONFLICT(id) DO UPDATE SET {updates}",
                    values,
                )
            self.set(TASK_REVISION_KEY, revision)
            if sync_id:
                self.append_event("tasks_applied", {
                    "sync_id": sync_id, "revision": revision,
                    "ok": True, "count": len(incoming),
                })
        return len(incoming)

    def _schedule_changed(self, values: dict[str, Any]) -> bool:
        row = self._db.execute(
            "SELECT schedule_type, schedule_expr, jitter_seconds FROM tasks "
            "WHERE id = ?",
            (values.get("id"),),
        ).fetchone()
        if row is None:
            return False  # new task; there is no plan to invalidate
        return (
            row["schedule_type"] != values.get("schedule_type")
            or str(row["schedule_expr"]) != str(values.get("schedule_expr"))
            or int(row["jitter_seconds"] or 0) != int(values.get("jitter_seconds") or 0)
        )

    def all_tasks(self) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def task(self, task_id: int) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def mark_task_run(self, task_id: int, *, last_run: str, next_run: str | None) -> None:
        self._db.execute(
            "UPDATE tasks SET last_run_at = ?, next_run_at = ? WHERE id = ?",
            (last_run, next_run, task_id),
        )

    def set_task_next_run(self, task_id: int, next_run: str | None) -> None:
        self._db.execute(
            "UPDATE tasks SET next_run_at = ? WHERE id = ?", (next_run, task_id)
        )

    # -- key/value ---------------------------------------------------------

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
