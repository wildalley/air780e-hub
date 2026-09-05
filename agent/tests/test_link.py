"""Agent/Server WebSocket contract tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import air780e_agent.link as link_module
from air780e_agent import PROTOCOL_VERSION
from air780e_agent.commands import CommandHandler
from air780e_agent.config import ServerConfig
from air780e_agent.link import BATCH, ServerLink
from air780e_agent.store import LocalStore

_END = object()


class _Socket:
    def __init__(self, *, event_count: int = 0, acknowledge: bool = False) -> None:
        self.frames: list[dict[str, Any]] = []
        self._event_count = event_count
        self._acknowledge = acknowledge
        self._events_sent = 0
        self._sent = asyncio.Event()
        self.closed: tuple[int, str] | None = None
        self._incoming: asyncio.Queue[str | object] = asyncio.Queue()
        if event_count == 0:
            self._incoming.put_nowait(_END)

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        self.frames.append(frame)
        self._sent.set()
        if not isinstance(frame.get("seq"), int):
            return
        self._events_sent += 1
        if self._acknowledge:
            self._incoming.put_nowait(json.dumps({"type": "ack", "seq": frame["seq"]}))
        if self._events_sent == self._event_count:
            self._incoming.put_nowait(_END)

    def push(self, frame: dict[str, Any]) -> None:
        self._incoming.put_nowait(json.dumps(frame))

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)
        self._incoming.put_nowait(_END)

    async def wait_for_events(self, count: int) -> None:
        async with asyncio.timeout(3):
            while self._events_sent < count:
                self._sent.clear()
                await self._sent.wait()

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self._incoming.get()
        if frame is _END:
            raise StopAsyncIteration
        return frame


class _Connection:
    def __init__(self, socket: _Socket) -> None:
        self.socket = socket

    async def __aenter__(self) -> _Socket:
        return self.socket

    async def __aexit__(self, *_args: object) -> None:
        return None


def _make_link(store: LocalStore, on_command: CommandHandler | None = None) -> ServerLink:
    async def ignore_command(_frame: dict[str, Any]) -> None:
        return None

    def reject_command(frame: dict[str, Any], error: str) -> None:
        if frame.get("cmd_id") is not None:
            store.append_event("cmd_result", {
                "cmd_id": frame["cmd_id"], "ok": False, "data": None, "error": error,
            })
            link.wake()

    link = ServerLink(
        ServerConfig(url="wss://hub.test/ws", token="secret"),
        agent_id="site-a",
        version="0.1.0",
        store=store,
        on_command=on_command or ignore_command,
        on_command_rejected=reject_command,
        describe_devices=lambda: [{"name": "a"}],
    )
    return link


async def _connect_once(monkeypatch, store: LocalStore, socket: _Socket) -> None:
    monkeypatch.setattr(
        link_module,
        "connect",
        lambda *_args, **_kwargs: _Connection(socket),
    )
    async with asyncio.timeout(3.0):
        await _make_link(store)._connect_once()


async def test_hello_advertises_the_wire_protocol_version(tmp_path, monkeypatch):
    socket = _Socket()
    store = LocalStore(tmp_path / "agent.db")
    try:
        stream = store.stream_id()
        await _connect_once(monkeypatch, store, socket)
    finally:
        store.close()

    assert socket.frames == [{
        "type": "hello",
        "agent_id": "site-a",
        "version": "0.1.0",
        "protocol_version": PROTOCOL_VERSION,
        "last_seq": 0,
        "stream_id": stream,
        "devices": [{"name": "a"}],
    }]


async def test_hello_carries_the_stored_event_stream_label(tmp_path, monkeypatch):
    """A fresh queue announces a stream id, and keeps it across restarts.

    The label is what lets the server tell "these numbers are new" from "the
    agent is replaying numbers I have already applied", so it has to live in
    the same file as the sequence generator and survive a reconnect.
    """
    store = LocalStore(tmp_path / "agent.db")
    try:
        store.append_event("sms_in", {"device": "a"})
        first = store.stream_id()
        assert first, "a brand-new queue mints a label"

        socket = _Socket()
        await _connect_once(monkeypatch, store, socket)
        assert socket.frames[0]["stream_id"] == first
    finally:
        store.close()

    reopened = LocalStore(tmp_path / "agent.db")
    try:
        assert reopened.stream_id() == first, "the label is not regenerated"
    finally:
        reopened.close()

    # A replacement store file is a new sequence space and must say so.
    (tmp_path / "agent.db").unlink()
    rebuilt = LocalStore(tmp_path / "agent.db")
    try:
        assert rebuilt.stream_id() not in (first, "")
    finally:
        rebuilt.close()


async def test_a_queue_predating_stream_ids_keeps_the_legacy_label(tmp_path):
    """An upgrade must not re-label sequence numbers already in flight.

    Events still waiting for an ACK were ingested as (agent, '', seq).  Minting
    a label for them on upgrade would move them to a stream the server has
    never seen, so a replay of an event whose ACK was lost would apply twice.
    """
    store = LocalStore(tmp_path / "agent.db")
    try:
        store.append_event("sms_in", {"device": "a"})
        # What an older agent's file looks like: sequence history, no label.
        store._db.execute("DELETE FROM kv WHERE key = 'stream_id'")
    finally:
        store.close()

    upgraded = LocalStore(tmp_path / "agent.db")
    try:
        assert upgraded.stream_id() == ""
    finally:
        upgraded.close()


async def test_acked_backlog_drains_across_batch_boundaries(tmp_path, monkeypatch):
    """A large outage queue must not pause five seconds after every batch."""
    store = LocalStore(tmp_path / "agent.db")
    total = BATCH + 1
    for index in range(total):
        store.append_event("status", {"device": "a", "rssi": index})
    socket = _Socket(event_count=total, acknowledge=True)

    try:
        await _connect_once(monkeypatch, store, socket)
        assert store.unacked_count() == 0
    finally:
        store.close()

    events = [frame for frame in socket.frames if "seq" in frame]
    assert len(events) == total
    assert [frame["seq"] for frame in events] == list(range(1, total + 1))


async def test_long_scan_does_not_block_ack_or_another_device(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / "agent.db")
    total = BATCH + 1
    for index in range(total):
        store.append_event("status", {"device": "a", "rssi": index})
    scan_started = asyncio.Event()
    release_scan = asyncio.Event()
    scan_done = asyncio.Event()
    other_done = asyncio.Event()

    async def on_command(frame):
        if frame["type"] == "scan_operators":
            scan_started.set()
            await release_scan.wait()
            scan_done.set()
        else:
            other_done.set()

    socket = _Socket(event_count=-1, acknowledge=True)
    socket.push({"type": "scan_operators", "device": "a", "cmd_id": "scan"})
    monkeypatch.setattr(link_module, "connect", lambda *a, **kw: _Connection(socket))
    link = _make_link(store, on_command)
    connection = asyncio.create_task(link._connect_once())
    try:
        async with asyncio.timeout(3):
            await scan_started.wait()
            await socket.wait_for_events(total)
            socket.push({"type": "query", "device": "b", "cmd_id": "other"})
            await other_done.wait()
            assert store.unacked_count() == 0
            assert not scan_done.is_set()
            assert [frame["seq"] for frame in socket.frames if "seq" in frame] == list(
                range(1, total + 1)
            )
            release_scan.set()
            await scan_done.wait()
            await socket.close()
            await connection
    finally:
        connection.cancel()
        await asyncio.gather(connection, return_exceptions=True)
        await link.stop()
        store.close()


async def test_reconnect_keeps_running_command_and_replays_its_result(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / "agent.db")
    scan_started = asyncio.Event()
    release_scan = asyncio.Event()
    other_done = asyncio.Event()
    new_done = asyncio.Event()
    executed = []

    async def on_command(frame):
        cmd_id = frame["cmd_id"]
        if cmd_id == "scan":
            scan_started.set()
            await release_scan.wait()
        executed.append(cmd_id)
        store.append_event("cmd_result", {"cmd_id": cmd_id, "ok": True, "data": {}})
        link.wake()
        if cmd_id == "other":
            other_done.set()
        if cmd_id == "new":
            new_done.set()

    first = _Socket(event_count=-1)
    second = _Socket(event_count=-1, acknowledge=True)
    sockets = iter([first, second])
    monkeypatch.setattr(link_module, "connect", lambda *a, **kw: _Connection(next(sockets)))
    first.push({"type": "scan_operators", "device": "a", "cmd_id": "scan"})
    first.push({"type": "send_sms", "device": "a", "cmd_id": "queued"})
    link = _make_link(store, on_command)
    connection = asyncio.create_task(link._connect_once())
    try:
        async with asyncio.timeout(3):
            await scan_started.wait()
            await first.close()
            await connection
            assert executed == []
            dropped = store.unacked_events()
            assert len(dropped) == 1
            assert dropped[0].payload["cmd_id"] == "queued"
            assert dropped[0].payload["error"] == "server disconnected before command started"

            connection = asyncio.create_task(link._connect_once())
            second.push({"type": "query", "device": "a", "cmd_id": "new"})
            second.push({"type": "query", "device": "b", "cmd_id": "other"})
            await other_done.wait()
            assert executed == ["other"]
            release_scan.set()
            await new_done.wait()
            await second.wait_for_events(4)
            assert executed == ["other", "scan", "new"]
            results = [frame for frame in second.frames if frame["type"] == "cmd_result"]
            assert [(frame["cmd_id"], frame["ok"]) for frame in results] == [
                ("queued", False), ("other", True), ("scan", True), ("new", True),
            ]
            await second.close()
            await connection
    finally:
        connection.cancel()
        await asyncio.gather(connection, return_exceptions=True)
        await link.stop()
        store.close()


async def test_full_command_queue_still_processes_ack_and_resend(tmp_path):
    store = LocalStore(tmp_path / "agent.db")
    event = store.append_event("sms_in", {"device": "a", "body": "test"})
    started = asyncio.Event()
    other_done = asyncio.Event()
    executed = []

    async def on_command(frame):
        executed.append(frame["cmd_id"])
        if frame["cmd_id"] == "running":
            started.set()
            await asyncio.Event().wait()
        else:
            other_done.set()

    link = _make_link(store, on_command)
    link._commands.max_per_device = 1
    link._sent_through = 10
    socket = _Socket(event_count=-1)
    socket.push({"type": "scan_operators", "device": "a", "cmd_id": "running"})
    receiver = asyncio.create_task(link._receiver(socket))
    try:
        async with asyncio.timeout(3):
            await started.wait()
            socket.push({"type": "send_sms", "device": "a", "cmd_id": "rejected"})
            socket.push({"type": "ack", "seq": event.seq})
            socket.push({"type": "resend_from", "seq": event.seq + 1})
            socket.push({"type": "query", "device": "b", "cmd_id": "other"})
            await other_done.wait()
            assert executed == ["running", "other"]
            assert link._sent_through == event.seq
            pending = store.unacked_events()
            assert len(pending) == 1
            assert pending[0].kind == "cmd_result"
            assert pending[0].payload == {
                "cmd_id": "rejected", "ok": False, "data": None,
                "error": "command queue is full; command was not started",
            }
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        await link.stop()
        store.close()


async def test_full_queue_reconnects_to_retry_unacknowledged_task_sync(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / "agent.db")
    started = asyncio.Event()
    executed = []

    async def on_command(frame):
        executed.append(frame["type"])
        started.set()
        await asyncio.Event().wait()

    link = _make_link(store, on_command)
    link._commands.max_pending = 1
    socket = _Socket(event_count=-1)
    monkeypatch.setattr(link_module, "connect", lambda *a, **kw: _Connection(socket))
    socket.push({"type": "scan_operators", "device": "a", "cmd_id": "scan"})
    connection = asyncio.create_task(link._connect_once())
    try:
        async with asyncio.timeout(3):
            await started.wait()
            socket.push({"type": "sync_tasks", "tasks": []})
            await connection
        assert socket.closed == (1013, "command queue is full")
        assert executed == ["scan_operators"]
    finally:
        connection.cancel()
        await asyncio.gather(connection, return_exceptions=True)
        await link.stop()
        store.close()


async def test_cancelling_link_run_finishes_commands_before_returning(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / "agent.db")
    started = asyncio.Event()
    finished = asyncio.Event()

    async def on_command(_frame):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    socket = _Socket(event_count=-1)
    socket.push({"type": "scan_operators", "device": "a", "cmd_id": "scan"})
    monkeypatch.setattr(link_module, "connect", lambda *a, **kw: _Connection(socket))
    link = _make_link(store, on_command)
    runner = asyncio.create_task(link.run())
    try:
        async with asyncio.timeout(3):
            await started.wait()
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        assert finished.is_set()
        assert not link.connected
        results = store.unacked_events()
        assert len(results) == 1
        assert results[0].payload["cmd_id"] == "scan"
        assert results[0].payload["error"] == (
            "agent stopped while command was running; execution result is unknown"
        )
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        await link.stop()
        store.close()


async def test_lost_ack_replays_after_agent_restart(
    tmp_path, monkeypatch, fault_cycles
):
    """Disconnect before ACK, reopen SQLite, then replay the exact sequence."""
    path = tmp_path / "agent.db"
    store = LocalStore(path)
    previous_seq = 0
    try:
        for cycle in range(fault_cycles):
            event = store.append_event(
                "sms_in",
                {"device": "a", "peer": "10086", "body": f"cycle-{cycle}"},
            )
            assert event.seq > previous_seq

            dropped = _Socket(event_count=1, acknowledge=False)
            await _connect_once(monkeypatch, store, dropped)
            assert store.unacked_count() == 1
            assert [frame.get("seq") for frame in dropped.frames[1:]] == [event.seq]

            # This is the Agent process boundary: only the SQLite file carries
            # the event and sequence into the next connection.
            store.close()
            store = LocalStore(path)

            replayed = _Socket(event_count=1, acknowledge=True)
            await _connect_once(monkeypatch, store, replayed)
            assert [frame.get("seq") for frame in replayed.frames[1:]] == [event.seq]
            assert store.unacked_count() == 0
            previous_seq = event.seq
    finally:
        store.close()
