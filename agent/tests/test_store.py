"""Durable local state: the queue must not lose a message, ever."""

from __future__ import annotations

import pytest

from air780e_agent.store import LocalStore


@pytest.fixture
def store(tmp_path):
    s = LocalStore(tmp_path / "agent.db")
    yield s
    s.close()


# --------------------------------------------------------------------------
# outbound queue
# --------------------------------------------------------------------------


def test_append_assigns_increasing_seq(store):
    a = store.append_event("sms_in", {"body": "one"})
    b = store.append_event("sms_in", {"body": "two"})
    assert b.seq > a.seq
    assert store.unacked_count() == 2


def test_unacked_events_round_trip_payload(store):
    store.append_event("sms_in", {"peer": "10086", "body": "验证码 123456"})
    events = store.unacked_events()
    assert events[0].kind == "sms_in"
    assert events[0].payload["body"] == "验证码 123456"
    assert events[0].to_frame()["type"] == "sms_in"
    assert events[0].to_frame()["seq"] == events[0].seq


def test_ack_is_cumulative(store):
    seqs = [store.append_event("status", {"n": i}).seq for i in range(5)]
    store.ack_through(seqs[2])
    remaining = [event.seq for event in store.unacked_events()]
    assert remaining == seqs[3:]


def test_seq_never_reused_after_ack(store):
    """A replayed seq must never collide with an old one on the server."""
    first = store.append_event("sms_in", {"body": "one"})
    store.ack_through(first.seq)
    assert store.unacked_count() == 0

    second = store.append_event("sms_in", {"body": "two"})
    assert second.seq > first.seq


def test_last_seq_survives_an_emptied_queue(store):
    event = store.append_event("sms_in", {"body": "one"})
    store.ack_through(event.seq)
    assert store.last_seq() == event.seq


def test_queue_survives_reopen(tmp_path):
    path = tmp_path / "agent.db"
    store = LocalStore(path)
    store.append_event("sms_in", {"body": "survives a restart"})
    store.close()

    reopened = LocalStore(path)
    events = reopened.unacked_events()
    assert len(events) == 1
    assert events[0].payload["body"] == "survives a restart"
    reopened.close()


def test_trim_drops_status_but_never_messages(store):
    for i in range(10):
        store.append_event("status", {"n": i})
    for i in range(5):
        store.append_event("sms_in", {"body": f"message {i}"})

    store.trim_events(keep=6)

    kinds = [event.kind for event in store.unacked_events()]
    assert kinds.count("sms_in") == 5, "messages must never be dropped"
    assert kinds.count("status") < 10


def test_trim_is_a_no_op_below_the_ceiling(store):
    store.append_event("status", {"n": 1})
    assert store.trim_events(keep=100) == 0
    assert store.unacked_count() == 1


def test_events_from_supports_resend(store):
    seqs = [store.append_event("status", {"n": i}).seq for i in range(5)]
    replayed = [event.seq for event in store.events_from(seqs[2])]
    assert replayed == seqs[2:]


# --------------------------------------------------------------------------
# message history
# --------------------------------------------------------------------------


def test_record_and_read_messages(store):
    store.record_message(
        device="a", direction="in", peer="10086",
        body="hello", ts="2026-08-02T18:00:00+08:00",
    )
    rows = store.recent_messages()
    assert len(rows) == 1
    assert rows[0]["peer"] == "10086"
    assert rows[0]["direction"] == "in"


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------


def _task(task_id: int, **overrides):
    base = {
        "id": task_id,
        "device": "a",
        "name": f"task {task_id}",
        "enabled": True,
        "action": "send_sms",
        "target_number": "10086",
        "content": "1",
        "schedule_type": "interval",
        "schedule_expr": "25",
        "jitter_seconds": 1800,
        "random_suffix": True,
        "retry_max": 3,
        "notify_on_result": True,
    }
    base.update(overrides)
    return base


def test_replace_tasks_stores_all_fields(store):
    store.replace_tasks([_task(1, target_number="10010", content="CXHF")])
    tasks = store.all_tasks()
    assert len(tasks) == 1
    assert tasks[0]["target_number"] == "10010"
    assert tasks[0]["content"] == "CXHF"
    assert tasks[0]["enabled"] == 1


def test_replace_tasks_deletes_absent_ones(store):
    store.replace_tasks([_task(1), _task(2), _task(3)])
    store.replace_tasks([_task(2)])
    assert [t["id"] for t in store.all_tasks()] == [2]


def test_replace_tasks_with_empty_list_clears_everything(store):
    store.replace_tasks([_task(1)])
    store.replace_tasks([])
    assert store.all_tasks() == []


def test_resync_preserves_run_times(store):
    """Re-syncing must not reset a keep-alive schedule."""
    store.replace_tasks([_task(1)])
    store.mark_task_run(1, last_run="2026-08-01T03:00:00+08:00",
                        next_run="2026-08-26T03:00:00+08:00")

    store.replace_tasks([_task(1, content="changed")])

    task = store.all_tasks()[0]
    assert task["content"] == "changed"
    assert task["last_run_at"] == "2026-08-01T03:00:00+08:00"
    assert task["next_run_at"] == "2026-08-26T03:00:00+08:00"


def test_changing_the_schedule_drops_the_planned_run(store):
    """Otherwise editing "every 25 days" to "every Tuesday" changes nothing
    until the old plan finally fires."""
    store.replace_tasks([_task(1)])
    store.mark_task_run(1, last_run="2026-08-01T03:00:00+08:00",
                        next_run="2026-08-26T03:00:00+08:00")

    store.replace_tasks([_task(1, schedule_type="cron", schedule_expr="0 3 * * 2")])

    task = store.all_tasks()[0]
    assert task["next_run_at"] is None, "the scheduler must re-plan"
    assert task["last_run_at"] == "2026-08-01T03:00:00+08:00", "history is kept"


def test_changing_only_the_jitter_also_replans(store):
    store.replace_tasks([_task(1)])
    store.set_task_next_run(1, "2026-08-26T03:00:00+08:00")
    store.replace_tasks([_task(1, jitter_seconds=0)])
    assert store.all_tasks()[0]["next_run_at"] is None


def test_kv(store):
    assert store.get("missing") is None
    assert store.get("missing", "fallback") == "fallback"
    store.set("k", "v")
    store.set("k", "v2")
    assert store.get("k") == "v2"
