"""Command ordering, overload and lifecycle without real hardware or clocks."""

from __future__ import annotations

import asyncio

import pytest

from air780e_agent.commands import CommandDispatcher


async def test_same_device_is_serial_while_other_device_can_finish():
    started = asyncio.Event()
    release = asyncio.Event()
    other_done = asyncio.Event()
    all_done = asyncio.Event()
    order = []

    async def execute(frame):
        cmd_id = frame["cmd_id"]
        order.append((cmd_id, "start"))
        if cmd_id == "scan-a":
            started.set()
            await release.wait()
        order.append((cmd_id, "end"))
        if cmd_id == "query-b":
            other_done.set()
        if cmd_id == "send-a":
            all_done.set()

    rejected = []
    dispatcher = CommandDispatcher(execute, lambda *args: rejected.append(args))
    try:
        assert dispatcher.submit({"device": "a", "cmd_id": "scan-a"})
        assert dispatcher.submit({"device": "a", "cmd_id": "send-a"})
        async with asyncio.timeout(3):
            await started.wait()
            assert dispatcher.submit({"device": "b", "cmd_id": "query-b"})
            await other_done.wait()
            assert order == [
                ("scan-a", "start"), ("query-b", "start"), ("query-b", "end"),
            ]
            release.set()
            await all_done.wait()
        assert order[-3:] == [
            ("scan-a", "end"), ("send-a", "start"), ("send-a", "end"),
        ]
        assert rejected == []
    finally:
        await dispatcher.stop()


async def test_manual_run_cannot_overtake_task_configuration():
    sync_started = asyncio.Event()
    sync_ready = asyncio.Event()
    run_done = asyncio.Event()
    applied = False

    async def execute(frame):
        nonlocal applied
        if frame["type"] == "sync_tasks":
            sync_started.set()
            await sync_ready.wait()
            applied = True
        else:
            assert applied
            run_done.set()

    rejected = []
    dispatcher = CommandDispatcher(execute, lambda *args: rejected.append(args))
    try:
        assert dispatcher.submit({"type": "sync_tasks", "tasks": []})
        assert dispatcher.submit({"type": "run_task", "task_id": 1, "device": "a"})
        async with asyncio.timeout(3):
            await sync_started.wait()
            assert not run_done.is_set()
            sync_ready.set()
            await run_done.wait()
        assert rejected == []
    finally:
        await dispatcher.stop()


@pytest.mark.parametrize(("limits", "devices"), [
    ({"max_per_device": 2}, ["a", "a", "a"]),
    ({"max_pending": 2}, ["a", "b", "b"]),
    ({"max_workers": 2}, ["a", "b", "c"]),
])
async def test_overload_rejects_unstarted_command_and_capacity_recovers(limits, devices):
    started = asyncio.Event()
    release = asyncio.Event()
    done = asyncio.Event()
    executed = []

    async def execute(frame):
        started.set()
        await release.wait()
        executed.append(frame["cmd_id"])
        if len(executed) >= 2:
            done.set()

    rejected = []
    dispatcher = CommandDispatcher(execute, lambda *args: rejected.append(args), **limits)
    frames = [{"device": device, "cmd_id": str(i)} for i, device in enumerate(devices)]
    try:
        async with asyncio.timeout(3):
            assert dispatcher.submit(frames[0])
            await started.wait()
            assert dispatcher.submit(frames[1])
            assert not dispatcher.submit(frames[2])
            assert rejected == [
                (frames[2], "command queue is full; command was not started"),
            ]
            release.set()
            await done.wait()
            assert sorted(executed) == ["0", "1"]
            done.clear()
            assert dispatcher.submit({"device": devices[2], "cmd_id": "new"})
            await done.wait()
        assert sorted(executed[:2]) == ["0", "1"]
        assert executed[-1] == "new"
    finally:
        await dispatcher.stop()


async def test_disconnect_discards_waiting_commands_and_preserves_running_command():
    started = asyncio.Event()
    release = asyncio.Event()
    done = asyncio.Event()
    executed = []

    async def execute(frame):
        if frame["cmd_id"] == "running":
            started.set()
            await release.wait()
        executed.append(frame["cmd_id"])
        if frame["cmd_id"] == "reconnected":
            done.set()

    rejected = []
    dispatcher = CommandDispatcher(execute, lambda *args: rejected.append(args))
    try:
        async with asyncio.timeout(3):
            assert dispatcher.submit({"device": "a", "cmd_id": "running"})
            await started.wait()
            queued = {"device": "a", "cmd_id": "queued"}
            assert dispatcher.submit(queued)
            dispatcher.discard_queued()
            assert rejected == [(queued, "server disconnected before command started")]
            assert dispatcher.submit({"device": "a", "cmd_id": "reconnected"})
            release.set()
            await done.wait()
        assert executed == ["running", "reconnected"]
    finally:
        await dispatcher.stop()


async def test_handler_failure_does_not_strand_the_next_command():
    done = asyncio.Event()

    async def execute(frame):
        if frame["cmd_id"] == "broken":
            raise RuntimeError("injected handler failure")
        done.set()

    rejected = []
    dispatcher = CommandDispatcher(execute, lambda *args: rejected.append(args))
    broken = {"device": "a", "cmd_id": "broken"}
    try:
        assert dispatcher.submit(broken)
        assert dispatcher.submit({"device": "a", "cmd_id": "next"})
        async with asyncio.timeout(3):
            await done.wait()
        assert rejected == [
            (broken, "command handler failed; execution result is unknown"),
        ]
    finally:
        await dispatcher.stop()


async def test_stop_waits_for_running_command_cleanup_and_rejects_pending_work():
    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def execute(_frame):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    rejected = []
    dispatcher = CommandDispatcher(execute, lambda *args: rejected.append(args))
    running = {"device": "a", "cmd_id": "running"}
    queued = {"device": "a", "cmd_id": "queued"}
    try:
        assert dispatcher.submit(running)
        async with asyncio.timeout(3):
            await started.wait()
            assert dispatcher.submit(queued)
            await dispatcher.stop()
        assert cleaned_up.is_set()
        assert rejected == [
            (queued, "agent is stopping; command was not started"),
            (running, "agent stopped while command was running; execution result is unknown"),
        ]
        assert not dispatcher.submit(queued)
        assert rejected[-1] == (queued, "agent is stopping; command was not started")
    finally:
        await dispatcher.stop()


async def test_stop_before_worker_starts_never_executes_the_command():
    executed = []

    async def execute(frame):
        executed.append(frame)

    rejected = []
    dispatcher = CommandDispatcher(execute, lambda *args: rejected.append(args))
    frame = {"device": "a", "cmd_id": "queued"}
    assert dispatcher.submit(frame)
    await dispatcher.stop()
    await dispatcher.stop()
    assert executed == []
    assert rejected == [(frame, "agent is stopping; command was not started")]
