"""Task configuration and its acknowledgement share one durable boundary."""

from __future__ import annotations

import sqlite3

import pytest

from air780e_agent.app import AgentApp
from air780e_agent.config import AgentConfig
from air780e_agent.store import TASK_REVISION_KEY, LocalStore, task_revision


def task(task_id=1, **changes):
    return {
        "id": task_id, "device": "a", "name": "keep-alive", "enabled": True,
        "action": "send_sms", "target_number": "10086", "content": "1",
        "schedule_type": "interval", "schedule_expr": "25", "jitter_seconds": 0,
        "random_suffix": False, "retry_max": 2, "notify_on_result": True, **changes,
    }


def snapshot(tasks, **changes):
    return {
        "type": "sync_tasks", "tasks": tasks, "revision": task_revision(tasks),
        "sync_id": "a" * 32, **changes,
    }


@pytest.fixture
async def app(tmp_path):
    config = AgentConfig.parse(b'[[devices]]\nname="a"\nport="/dev/fake-a"\n')
    config.db_path = tmp_path / "agent.db"
    application = AgentApp(config)
    try:
        yield application
    finally:
        await application.stop()


async def test_applied_configuration_and_receipt_survive_restart(app):
    frame = snapshot([task()])
    await app.handle_command(frame)
    await app.stop()
    reopened = LocalStore(app.config.db_path)
    try:
        assert reopened.task(1)["content"] == "1"
        assert reopened.get(TASK_REVISION_KEY) == frame["revision"]
        events = reopened.unacked_events()
        assert len(events) == 1
        assert events[0].kind == "tasks_applied"
        assert events[0].payload == {
            "sync_id": frame["sync_id"], "revision": frame["revision"], "ok": True, "count": 1,
        }
    finally:
        reopened.close()


async def test_repeated_snapshot_keeps_the_local_schedule_and_answers_again(app):
    frame = snapshot([task()])
    await app.handle_command(frame)
    app.store.mark_task_run(1, last_run="2026-09-01", next_run="2026-09-26")
    await app.handle_command(frame)
    row = app.store.task(1)
    assert row["last_run_at"] == "2026-09-01"
    assert row["next_run_at"] == "2026-09-26"
    receipts = app.store.unacked_events()
    assert len(receipts) == 2
    assert all(event.payload["ok"] is True for event in receipts)
    assert receipts[0].seq < receipts[1].seq


async def test_bad_hash_cannot_replace_tasks_and_has_a_failure_receipt(app):
    app.store.replace_tasks([task()])
    frame = snapshot([task(content="changed")], revision="0" * 64)
    await app.handle_command(frame)
    assert app.store.task(1)["content"] == "1"
    assert app.store.unacked_events()[0].payload == {
        "sync_id": frame["sync_id"], "revision": frame["revision"],
        "ok": False, "error": "revision_mismatch",
    }


async def test_partial_replacement_rolls_back_before_reporting_failure(app):
    original = snapshot([task()])
    await app.handle_command(original)
    original_events = app.store.unacked_events()
    app.store.ack_through(original_events[-1].seq)
    frame = snapshot([task(2), task(3, name=None)])
    await app.handle_command(frame)
    assert [row["id"] for row in app.store.all_tasks()] == [1]
    assert app.store.get(TASK_REVISION_KEY) == original["revision"]
    events = app.store.unacked_events()
    assert len(events) == 1
    assert events[0].payload["ok"] is False
    assert events[0].payload["error"] == "apply_failed"


async def test_receipt_write_failure_rolls_back_tasks_and_revision(app, monkeypatch):
    app.store.replace_tasks([task()], revision="previous")

    def fail_receipt(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected full disk")

    monkeypatch.setattr(app.store, "append_event", fail_receipt)
    frame = snapshot([task(2)])
    with pytest.raises(sqlite3.OperationalError, match="full disk"):
        app.store.replace_tasks(
            frame["tasks"], revision=frame["revision"], sync_id=frame["sync_id"]
        )
    assert [row["id"] for row in app.store.all_tasks()] == [1]
    assert app.store.get(TASK_REVISION_KEY) == "previous"
    assert app.store.unacked_count() == 0


@pytest.mark.parametrize("tasks", [[task(True)], [task(1), task(1)], [task(-1)], [None]])
async def test_invalid_task_identity_leaves_existing_configuration_intact(app, tasks):
    app.store.replace_tasks([task()])
    await app.handle_command(snapshot(tasks))
    assert [row["id"] for row in app.store.all_tasks()] == [1]
    assert app.store.unacked_events()[0].payload["ok"] is False


async def test_manual_run_requires_the_requested_configuration(app, monkeypatch):
    applied = snapshot([task()])
    await app.handle_command(applied)
    started = []

    def run(task_id):
        started.append(task_id)
        return {"task_id": task_id, "status": "started"}

    monkeypatch.setattr(app.scheduler, "run_now", run)
    await app.handle_command({
        "type": "run_task", "task_id": 1, "revision": "0" * 64, "cmd_id": "stale",
    })
    assert started == []
    failed = app.store.unacked_events()[-1].payload
    assert failed["ok"] is False
    assert failed["error"] == "task configuration has not been applied"
    await app.handle_command({
        "type": "run_task", "task_id": 1, "revision": applied["revision"], "cmd_id": "current",
    })
    assert started == [1]
    assert app.store.unacked_events()[-1].payload["ok"] is True
