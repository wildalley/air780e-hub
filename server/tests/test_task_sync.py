"""Versioned task snapshots, receipts and retries across Agent connections."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from hub_server import PROTOCOL_VERSION, __version__
from hub_server.config import Settings
from hub_server.db import SCHEMA, SCHEMA_VERSION, Database
from hub_server.gateway import Gateway, task_revision


class Socket:
    def __init__(self):
        self.frames = []
        self.fail = False

    async def send_text(self, raw):
        if self.fail:
            raise OSError("injected socket failure")
        self.frames.append(json.loads(raw))


@pytest.fixture
def gateway(tmp_path):
    db = Database(tmp_path / "hub.db")
    instance = Gateway(db, Settings(data_dir=tmp_path, agent_token="test-token"))
    try:
        yield instance
    finally:
        db.close()


async def register(gateway, agent_id="site-a"):
    return await gateway._register(agent_id, Socket(), {
        "version": __version__, "protocol_version": PROTOCOL_VERSION,
        "devices": [{"name": "a", "online": True}],
    })


def state(gateway, agent_id="site-a"):
    return gateway.db.one("SELECT * FROM agents WHERE id = ?", (agent_id,))


async def receipt(gateway, connection, frame, seq=1, **changes):
    await gateway._ingest(connection, {
        "type": "tasks_applied", "seq": seq, "revision": frame["revision"],
        "sync_id": frame["sync_id"], "ok": True, "count": len(frame["tasks"]), **changes,
    })


async def test_tasks_are_pending_until_the_current_receipt_is_committed(gateway):
    connection = await register(gateway)
    await gateway.push_tasks(connection.agent_id)
    frame = connection.websocket.frames[-1]
    assert frame["revision"] == task_revision(frame["tasks"])
    assert len(frame["sync_id"]) == 32
    assert state(gateway)["tasks_sync_status"] == "pending"
    await receipt(gateway, connection, frame)
    assert state(gateway)["tasks_sync_status"] == "applied"
    assert state(gateway)["tasks_applied_revision"] == frame["revision"]
    assert state(gateway)["tasks_synced_at"]
    assert connection.websocket.frames[-1] == {"type": "ack", "seq": 1}
    await receipt(gateway, connection, frame)
    assert gateway.db.one("SELECT COUNT(*) AS n FROM ingested")["n"] == 1


async def test_receipt_transaction_failure_does_not_ack_or_confirm(gateway, monkeypatch):
    connection = await register(gateway)
    await gateway.push_tasks(connection.agent_id)
    frame = connection.websocket.frames[-1]
    finish = gateway.db.finish_task_sync

    def fail(*args, **kwargs):
        finish(*args, **kwargs)
        raise sqlite3.OperationalError("injected transaction failure")

    monkeypatch.setattr(gateway.db, "finish_task_sync", fail)
    with pytest.raises(sqlite3.OperationalError):
        await receipt(gateway, connection, frame)
    assert state(gateway)["tasks_sync_status"] == "pending"
    assert gateway.db.one("SELECT COUNT(*) AS n FROM ingested")["n"] == 0
    assert connection.websocket.frames == [frame]
    monkeypatch.setattr(gateway.db, "finish_task_sync", finish)
    await receipt(gateway, connection, frame)
    assert state(gateway)["tasks_sync_status"] == "applied"


async def test_unconfirmed_snapshot_retries_without_changing_its_identity(gateway):
    connection = await register(gateway)
    await gateway.push_tasks(connection.agent_id)
    frame = connection.websocket.frames[-1]
    await gateway.retry_task_sync()
    assert connection.websocket.frames == [frame]
    connection.tasks_next_check = 0
    await gateway.retry_task_sync()
    assert connection.websocket.frames == [frame, frame]
    await receipt(gateway, connection, frame)
    connection.tasks_next_check = 0
    await gateway.retry_task_sync()
    assert len(connection.websocket.frames) == 3  # two snapshots and one ACK


async def test_failed_snapshot_can_be_retried_and_confirmed(gateway):
    connection = await register(gateway)
    await gateway.push_tasks(connection.agent_id)
    frame = connection.websocket.frames[-1]
    await receipt(gateway, connection, frame, ok=False, error="apply_failed")
    assert state(gateway)["tasks_sync_status"] == "failed"
    assert state(gateway)["tasks_sync_error"] == "Agent 未能保存任务配置"
    connection.tasks_next_check = 0
    await gateway.retry_task_sync()
    assert connection.websocket.frames[-1] == frame
    await receipt(gateway, connection, frame, seq=2)
    assert state(gateway)["tasks_sync_status"] == "applied"
    assert state(gateway)["tasks_sync_error"] == ""
    await receipt(gateway, connection, frame, seq=3, ok=False, error="apply_failed")
    assert state(gateway)["tasks_sync_status"] == "applied"


async def test_send_failure_remains_visible_and_retryable(gateway):
    connection = await register(gateway)
    connection.websocket.fail = True
    await gateway.push_tasks(connection.agent_id)
    assert state(gateway)["tasks_sync_status"] == "failed"
    sync_id = state(gateway)["tasks_sync_id"]
    connection.websocket.fail = False
    connection.tasks_next_check = 0
    await gateway.retry_task_sync()
    assert connection.websocket.frames[-1]["sync_id"] == sync_id
    await receipt(gateway, connection, connection.websocket.frames[-1])
    assert state(gateway)["tasks_sync_status"] == "applied"


async def test_another_agent_cannot_confirm_the_snapshot(gateway):
    owner = await register(gateway)
    other = await register(gateway, "site-b")
    await gateway.push_tasks(owner.agent_id)
    await gateway.push_tasks(other.agent_id)
    frame = owner.websocket.frames[-1]
    await receipt(gateway, other, frame)
    assert state(gateway)["tasks_sync_status"] == "pending"
    assert state(gateway, "site-b")["tasks_sync_status"] == "pending"


async def test_receipt_from_previous_connection_cannot_confirm_a_reconnect(gateway):
    old = await register(gateway)
    await gateway.push_tasks(old.agent_id)
    frame = old.websocket.frames[-1]
    await gateway._unregister(old)
    current = await register(gateway)
    await gateway.push_tasks(current.agent_id)
    new_frame = current.websocket.frames[-1]
    assert new_frame["revision"] == frame["revision"]
    assert new_frame["sync_id"] != frame["sync_id"]
    await receipt(gateway, current, frame)
    assert state(gateway)["tasks_sync_status"] == "pending"
    await receipt(gateway, current, new_frame, seq=2)
    assert state(gateway)["tasks_sync_status"] == "applied"


async def test_old_receipt_cannot_confirm_a_change_back_to_the_same_content(gateway, monkeypatch):
    connection = await register(gateway)
    tasks = []
    monkeypatch.setattr(gateway, "tasks_for", lambda _agent_id: list(tasks))
    await gateway.push_tasks(connection.agent_id)
    first = connection.websocket.frames[-1]
    tasks.append({"id": 1, "device": "a"})
    await gateway.push_tasks(connection.agent_id)
    tasks.clear()
    await gateway.push_tasks(connection.agent_id)
    last = connection.websocket.frames[-1]
    assert last["revision"] == first["revision"]
    assert last["sync_id"] != first["sync_id"]
    await receipt(gateway, connection, first)
    assert state(gateway)["tasks_sync_status"] == "pending"
    await receipt(gateway, connection, last, seq=2)
    assert state(gateway)["tasks_sync_status"] == "applied"


async def test_offline_edits_invalidate_the_previous_confirmation(gateway, monkeypatch):
    connection = await register(gateway)
    await gateway.push_tasks(connection.agent_id)
    await receipt(gateway, connection, connection.websocket.frames[-1])
    await gateway._unregister(connection)
    monkeypatch.setattr(gateway, "tasks_for", lambda _agent_id: [{"id": 1}])
    await gateway.push_tasks(connection.agent_id)
    row = state(gateway)
    assert row["tasks_sync_status"] == "pending"
    assert row["tasks_revision"] != row["tasks_applied_revision"]
    assert row["tasks_sync_sent_at"] is None


async def test_retry_loop_runs_independently_of_receipt_processing(gateway, monkeypatch):
    connection = await register(gateway)
    await gateway.push_tasks(connection.agent_id)
    connection.tasks_next_check = 0
    retried = asyncio.Event()
    retry = gateway.retry_task_sync

    async def observe_retry():
        await retry()
        retried.set()

    monkeypatch.setattr(gateway, "retry_task_sync", observe_retry)
    monkeypatch.setattr("hub_server.gateway.TASK_SYNC_RETRY_SECONDS", 0.01)
    runner = asyncio.create_task(gateway.run_task_sync())
    try:
        async with asyncio.timeout(3):
            await retried.wait()
        assert len(connection.websocket.frames) == 2
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


def test_version_16_migrates_without_claiming_tasks_were_applied(tmp_path):
    path = tmp_path / "hub.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(SCHEMA)
    for column in (
        "tasks_revision", "tasks_applied_revision", "tasks_sync_id", "tasks_sync_status",
        "tasks_sync_error", "tasks_sync_sent_at", "tasks_synced_at",
    ):
        legacy.execute(f"ALTER TABLE agents DROP COLUMN {column}")
    legacy.executescript("""
        INSERT INTO agents (id, last_seq) VALUES ('site-a', 42);
        PRAGMA user_version = 16;
    """)
    legacy.close()
    database = Database(path)
    try:
        row = database.one("SELECT * FROM agents WHERE id = 'site-a'")
        assert row["last_seq"] == 42
        assert row["tasks_sync_status"] == "pending"
        assert row["tasks_applied_revision"] == ""
        assert database.one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION
        assert path.with_name("hub.db.v16.bak").exists()
    finally:
        database.close()
