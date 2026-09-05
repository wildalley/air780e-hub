"""WAL readers, bounded async writes, and cancellation at transaction boundaries."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import contextmanager

import httpx
import pytest
from anyio import CancelScope

from hub_server.alerts import OfflineAlerter
from hub_server.config import Settings
from hub_server.db import Database, MessageScope, utcnow
from hub_server.gateway import AgentConnection, AgentUnavailable, Gateway
from hub_server.main import create_app
from hub_server.notify import Notifier


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "hub?#.db")
    try:
        yield database
    finally:
        database.close()


async def wait_for(event: threading.Event) -> None:
    async with asyncio.timeout(3):
        while not event.is_set():
            await asyncio.sleep(0.001)


def insert(db, body="test"):
    return db.insert_message(
        agent_id="site", device="a", direction="in", peer="10086",
        body=body, ts=utcnow(),
    )


class Socket:
    def __init__(self):
        self.frames = []

    async def send_text(self, raw):
        self.frames.append(json.loads(raw))


@pytest.mark.parametrize("outcome", ["commit", "rollback"])
async def test_repeated_cancellation_waits_for_the_whole_transaction(db, outcome):
    started, release = threading.Event(), threading.Event()

    def apply():
        insert(db)
        started.set()
        assert release.wait(3)
        if outcome == "rollback":
            raise sqlite3.OperationalError("injected rollback")

    task = asyncio.create_task(db.run(db.apply_event, "site", 1, "sms_in", apply))
    try:
        await wait_for(started)
        assert db.count_messages() == 0, "an uncommitted event became visible"
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "cancellation abandoned the worker"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert db.count_messages() == (outcome == "commit")
        assert db.one("SELECT COUNT(*) AS n FROM ingested")["n"] == (outcome == "commit")
        assert not db._db.in_transaction
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancellation_scope_waits_for_the_worker(db):
    started, release = threading.Event(), threading.Event()
    scope = CancelScope()

    def apply():
        started.set()
        assert release.wait(3)
        insert(db)

    async def request():
        with scope:
            await db.run(db.apply_event, "site", 1, "sms_in", apply)

    task = asyncio.create_task(request())
    try:
        await wait_for(started)
        scope.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        await task
        assert db.count_messages() == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_backpressure_cancels_an_unsubmitted_operation(db):
    db._async_slots = asyncio.Semaphore(1)
    started, release = threading.Event(), threading.Event()

    def hold():
        started.set()
        assert release.wait(3)
        return threading.get_ident()

    running = asyncio.create_task(db.run(hold))
    queued = None
    try:
        await wait_for(started)
        queued = asyncio.create_task(db.run(insert, db))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        release.set()
        worker_id = await running
        assert worker_id != threading.get_ident()
        assert await db.run(threading.get_ident) == worker_id
        assert db.count_messages() == 0
    finally:
        release.set()
        await asyncio.gather(running, *([queued] if queued else []), return_exceptions=True)


@pytest.mark.parametrize("kind", ["list", "search", "conversations", "trend", "activity"])
async def test_slow_reader_does_not_prevent_event_commit_or_ack(db, tmp_path, monkeypatch, kind):
    insert(db)
    entered, release = threading.Event(), threading.Event()
    readonly = db._readonly_connection

    @contextmanager
    def slow_reader():
        with readonly() as connection:
            def progress():
                entered.set()
                assert release.wait(3)
                return 0

            connection.set_progress_handler(progress, 1)
            yield connection

    monkeypatch.setattr(db, "_readonly_connection", slow_reader)
    reads = {
        "list": db.messages,
        "search": lambda: db.count_messages(MessageScope(search="test")),
        "conversations": db.conversations,
        "trend": lambda: db.message_trend(since="2000-01-01"),
        "activity": db.activity_stats,
    }
    reader = asyncio.create_task(asyncio.to_thread(reads[kind]))
    socket = Socket()
    gateway = Gateway(db, Settings(data_dir=tmp_path, agent_token="test"))
    try:
        await wait_for(entered)
        async with asyncio.timeout(1):
            await gateway._ingest(AgentConnection("site", socket), {
                "type": "sms_in", "seq": 1, "device": "a", "body": "new",
            })
        assert socket.frames == [{"type": "ack", "seq": 1}]
        assert not reader.done(), "the test reader should still be blocked"
    finally:
        release.set()
        await reader


async def test_slow_write_keeps_health_responsive_and_hooks_on_the_loop(tmp_path, monkeypatch):
    app = create_app(Settings(data_dir=tmp_path, agent_token="test"))
    state = app.state.hub
    entered, release = threading.Event(), threading.Event()
    original = state.gateway._apply
    loop_thread = threading.get_ident()
    hooks = []

    def apply(*args):
        assert threading.get_ident() != loop_thread
        result = original(*args)
        entered.set()
        assert release.wait(3)
        return result

    async def on_message(*args, **kwargs):
        hooks.append(threading.get_ident())
        assert state.db.count_messages() == 1

    monkeypatch.setattr(state.gateway, "_apply", apply)
    state.gateway.on_message = on_message
    socket = Socket()
    frame = {"type": "sms_in", "seq": 1, "device": "a", "body": "test"}
    connection = AgentConnection("site", socket)
    task = asyncio.create_task(state.gateway._ingest(connection, frame))
    try:
        await wait_for(entered)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            async with asyncio.timeout(1):
                assert (await client.get("/healthz")).status_code == 200
        assert socket.frames == []
        release.set()
        await task
        await state.gateway._ingest(connection, frame)
        assert hooks == [loop_thread]
        assert socket.frames == [{"type": "ack", "seq": 1}] * 2
        assert state.db.count_messages() == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await state.alerter.aclose()
        await state.notifier.aclose()
        state.close()


async def test_purge_yields_committed_batches_and_can_resume(db):
    for _ in range(7):
        insert(db)
    db.execute("UPDATE messages SET ts = '2000-01-01'")
    batches = db.purge_batches(message_days=1, status_days=0, batch_size=2)
    first = await db.run(next, batches)
    assert first["messages"] == 2
    assert db.count_messages() == 5
    batches.close()
    await db.run(insert, db, "arrived between batches")
    rest = await db.purge_async(message_days=1, status_days=0, batch_size=2)
    assert rest["messages"] == 5
    assert [row["body"] for row in db.messages()] == ["arrived between batches"]


async def test_csv_cursor_can_move_threads_and_close_early(db):
    for _ in range(3):
        insert(db)
    stream = iter(db.iter_messages(batch_size=1))
    first = await asyncio.to_thread(next, stream)
    await db.run(insert, db, "after snapshot")
    # The DB executor is a different thread from the default reader pool.
    second = await db.run(next, stream)
    assert first["id"] != second["id"]
    assert first["body"] == second["body"] == "test"
    await asyncio.to_thread(stream.close)
    assert len(list(db.iter_messages())) == 4
    with db._readonly_connection() as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM messages")


async def test_offline_recovery_and_disable_remain_consistent(db, tmp_path):
    db.upsert_device("site", {"name": "a", "online": False})
    notifier = Notifier(db, Settings(data_dir=tmp_path, agent_token="test"))
    alerter = OfflineAlerter(db, notifier, grace=0)
    try:
        alerter.note("site", "a", False)
        await alerter.drain()
        assert db.one("SELECT status FROM incidents")["status"] == "active"
        db.upsert_device("site", {"name": "a", "online": True})
        alerter.note("site", "a", True)
        await alerter.drain()
        assert db.one("SELECT status FROM incidents")["status"] == "resolved"
        db.set_setting("offline_alerts_enabled", False)
        db.upsert_device("site", {"name": "a", "online": False})
        alerter.note("site", "a", False)
        await alerter.drain()
        assert alerter.pending_count == 0
        assert db.one("SELECT status FROM incidents")["status"] == "resolved"
    finally:
        await alerter.aclose()
        await notifier.aclose()


@pytest.mark.parametrize("phase", ["register", "unregister"])
async def test_connection_identity_stays_reserved_during_db_work(db, tmp_path, monkeypatch, phase):
    entered, release = threading.Event(), threading.Event()
    gateway = Gateway(db, Settings(data_dir=tmp_path, agent_token="test"))
    hello = {"devices": [{"name": "a", "online": True}]}
    if phase == "unregister":
        connection = await gateway._register("site", Socket(), hello)
        original = gateway._persist_disconnect
    else:
        original = gateway._persist_registration

    def delayed(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(
        gateway, "_persist_registration" if phase == "register" else "_persist_disconnect",
        delayed,
    )
    task = asyncio.create_task(
        gateway._register("site", Socket(), hello) if phase == "register"
        else gateway._unregister(connection)
    )
    try:
        await wait_for(entered)
        assert "site" in gateway.connections
        assert not gateway.connections["site"].ready
        with pytest.raises(AgentUnavailable):
            await gateway.call("site", {"type": "query"})
        task.cancel()
        await asyncio.sleep(0)
        assert "site" in gateway.connections
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gateway.connections == {}
        assert db.one("SELECT connected FROM agents WHERE id = 'site'")["connected"] == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def test_in_memory_readers_share_the_original_database():
    db = Database(":memory:")
    try:
        insert(db)
        assert db.count_messages() == db.unread_total() == 1
        assert len(list(db.iter_messages())) == len(db.conversations()) == 1
        assert db.activity_stats()["messages"]["inbound"]["day"] == 1
    finally:
        db.close()


def test_readonly_connection_closes_after_query_failure(db):
    with pytest.raises(sqlite3.OperationalError):
        with db._readonly_connection() as connection:
            connection.execute("SELECT * FROM missing_table")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
