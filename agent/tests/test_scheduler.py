"""Keep-alive scheduler.

The core contract is that the agent keeps its schedule with the server
unreachable and queues receipts until connectivity returns, so these
tests never build a link.  A fake worker stands in for the modem; the clock is
injected, because a test that waits 25 days is not a test.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

import pytest

from air780e_agent.scheduler import KeepAliveScheduler
from air780e_agent.store import LocalStore
from air780e_agent.worker import DeviceOffline

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 2, 10, 0, 0, tzinfo=CST)


class Clock:
    def __init__(self, moment: datetime = NOW) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **kwargs) -> None:
        self.moment += timedelta(**kwargs)


class FakeWorker:
    """A modem that answers instantly and can be told to fail."""

    def __init__(
        self,
        *,
        fail_times: int = 0,
        ping_ok: bool = True,
        call_reaches_network: bool = True,
    ) -> None:
        self.sent: list[tuple[str, str]] = []
        self.pings: list[str] = []
        self.commands: list[str] = []
        self.called: list[str] = []
        self.fail_times = fail_times
        self.attempts = 0
        self.ping_ok = ping_ok
        self.call_reaches_network = call_reaches_network
        self.radio_enabled: bool | None = True
        self.data_enabled = True
        self.gate: asyncio.Event | None = None

    async def send_sms(self, number: str, body: str) -> list[int]:
        self.attempts += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.attempts <= self.fail_times:
            raise DeviceOffline("device a is offline: not connected")
        self.sent.append((number, body))
        return [1]

    async def ping(self, host: str) -> bool:
        self.pings.append(host)
        return self.ping_ok

    async def raw_at(self, command: str) -> list[str]:
        self.commands.append(command)
        return ["+CSQ: 24,99", "OK"]

    async def call_keepalive(self, number: str) -> dict:
        self.called.append(number)
        reached = self.call_reaches_network
        return {
            "outcome": "alerting" if reached else "no_progress",
            "reached_network": reached,
            "ring_seconds": 8.0,
            "detail": "far end rang after 8.0s" if reached else "never left the module",
        }


@pytest.fixture
def store(tmp_path):
    s = LocalStore(tmp_path / "agent.db")
    yield s
    s.close()


def make_task(**overrides):
    base = {
        "id": 1,
        "device": "a",
        "name": "移动卡保号",
        "enabled": True,
        "action": "send_sms",
        "target_number": "10086",
        "content": "1",
        "schedule_type": "interval",
        "schedule_expr": "25",
        "jitter_seconds": 0,
        "random_suffix": False,
        "retry_max": 0,
        "notify_on_result": True,
    }
    return {**base, **overrides}


def build(store, worker=None, *, tasks=None, clock=None, emit=None, **kwargs):
    store.replace_tasks(tasks if tasks is not None else [make_task()])
    events: list[tuple[str, dict]] = []
    scheduler = KeepAliveScheduler(
        store,
        {"a": worker} if worker is not None else {},
        emit or (lambda kind, payload: events.append((kind, payload))),
        clock=clock or Clock(),
        retry_delay=0.0,
        rng=random.Random(4242),
        **kwargs,
    )
    return scheduler, events


def due_now(store, task_id: int = 1, *, minutes_ago: int = 5) -> None:
    moment = NOW - timedelta(minutes=minutes_ago)
    store.set_task_next_run(task_id, moment.isoformat(timespec="seconds"))


def results(events) -> list[dict]:
    return [payload for kind, payload in events if kind == "task_result"]


# --------------------------------------------------------------------------
# when tasks run
# --------------------------------------------------------------------------


async def test_a_due_task_sends_and_reports(store):
    worker = FakeWorker()
    scheduler, events = build(store, worker)
    due_now(store)

    assert await scheduler.tick_once() == 1
    await scheduler.drain()

    assert worker.sent == [("10086", "1")]
    receipt = results(events)[0]
    assert receipt["status"] == "ok"
    assert receipt["attempts"] == 1
    assert receipt["task_id"] == 1
    assert "10086" in receipt["detail"]

    row = store.all_tasks()[0]
    assert row["last_run_at"] == NOW.isoformat(timespec="seconds")
    assert row["next_run_at"] == (NOW + timedelta(days=25)).isoformat(timespec="seconds")


async def test_a_task_that_is_not_due_is_left_alone(store):
    worker = FakeWorker()
    scheduler, events = build(store, worker)
    store.set_task_next_run(1, (NOW + timedelta(days=1)).isoformat())

    assert await scheduler.tick_once() == 0
    assert worker.sent == []
    assert events == []


async def test_a_disabled_task_never_runs(store):
    worker = FakeWorker()
    scheduler, _ = build(store, worker, tasks=[make_task(enabled=False)])
    due_now(store)

    assert await scheduler.tick_once() == 0
    assert worker.sent == []


async def test_a_disabled_task_can_be_started_manually(store):
    worker = FakeWorker()
    scheduler, events = build(store, worker, tasks=[make_task(enabled=False)])

    assert scheduler.run_now(1) == {"task_id": 1, "status": "started"}
    await scheduler.drain()

    assert worker.sent == [("10086", "1")]
    assert results(events)[0]["status"] == "ok"


async def test_a_running_task_rejects_a_second_manual_start(store):
    worker = FakeWorker()
    worker.gate = asyncio.Event()
    scheduler, _ = build(store, worker)

    scheduler.run_now(1)
    with pytest.raises(RuntimeError, match="already running"):
        scheduler.run_now(1)

    worker.gate.set()
    await scheduler.drain()


async def test_a_fresh_task_waits_a_full_interval(store):
    """Creating a keep-alive task must not immediately send an SMS."""
    worker = FakeWorker()
    scheduler, events = build(store, worker)

    assert await scheduler.tick_once() == 0
    assert worker.sent == []
    planned = store.all_tasks()[0]["next_run_at"]
    assert planned == (NOW + timedelta(days=25)).isoformat(timespec="seconds")


async def test_a_run_missed_while_the_agent_was_down_happens_once(store):
    """Being off for a month owes the carrier one message, not thirty."""
    worker = FakeWorker()
    scheduler, events = build(store, worker)
    store.mark_task_run(
        1,
        last_run=(NOW - timedelta(days=30)).isoformat(timespec="seconds"),
        next_run=(NOW - timedelta(days=5)).isoformat(timespec="seconds"),
    )

    assert await scheduler.tick_once() == 1
    await scheduler.drain()
    assert await scheduler.tick_once() == 0, "the catch-up must not repeat"
    assert len(worker.sent) == 1


async def test_a_running_task_is_not_started_twice(store):
    worker = FakeWorker()
    worker.gate = asyncio.Event()
    scheduler, _ = build(store, worker)
    due_now(store)

    assert await scheduler.tick_once() == 1
    assert await scheduler.tick_once() == 0, "still running from the first tick"

    worker.gate.set()
    await scheduler.drain()
    assert len(worker.sent) == 1


# --------------------------------------------------------------------------
# failure handling
# --------------------------------------------------------------------------


async def test_a_transient_failure_is_retried(store):
    worker = FakeWorker(fail_times=2)
    scheduler, events = build(store, worker, tasks=[make_task(retry_max=3)])
    due_now(store)

    await scheduler.tick_once()
    await scheduler.drain()

    receipt = results(events)[0]
    assert receipt["status"] == "ok"
    assert receipt["attempts"] == 3
    assert worker.sent == [("10086", "1")]


async def test_exhausted_retries_report_the_reason(store):
    worker = FakeWorker(fail_times=99)
    scheduler, events = build(store, worker, tasks=[make_task(retry_max=1)])
    due_now(store)

    await scheduler.tick_once()
    await scheduler.drain()

    receipt = results(events)[0]
    assert receipt["status"] == "failed"
    assert receipt["attempts"] == 2
    assert "offline" in receipt["error"]

    row = store.all_tasks()[0]
    assert row["next_run_at"], "a failing card must still be rescheduled"


async def test_an_unknown_device_is_skipped_without_retrying(store):
    """Retrying cannot conjure a module that this agent does not have."""
    scheduler, events = build(store, None, tasks=[make_task(retry_max=5)])
    due_now(store)

    await scheduler.tick_once()
    await scheduler.drain()

    receipt = results(events)[0]
    assert receipt["status"] == "skipped"
    assert receipt["attempts"] == 1
    assert store.all_tasks()[0]["last_run_at"] is None, "nothing was attempted"


async def test_flight_mode_skips_without_retrying_or_advancing_last_run(store):
    worker = FakeWorker()
    worker.radio_enabled = False
    scheduler, events = build(store, worker, tasks=[make_task(retry_max=5)])
    due_now(store)

    await scheduler.tick_once()
    await scheduler.drain()

    receipt = results(events)[0]
    assert receipt["status"] == "skipped"
    assert receipt["attempts"] == 1
    assert "radio is disabled" in receipt["error"]
    assert worker.sent == []
    assert store.all_tasks()[0]["last_run_at"] is None


async def test_a_broken_schedule_is_reported_and_not_retried_every_tick(store):
    worker = FakeWorker()
    scheduler, events = build(store, worker, tasks=[make_task(schedule_expr="每周")])

    assert await scheduler.tick_once() == 0
    receipt = results(events)[0]
    assert receipt["status"] == "skipped"
    assert "schedule" in receipt["detail"] or "schedule" in (receipt["error"] or "")
    assert store.all_tasks()[0]["next_run_at"] is None
    assert worker.sent == []


async def test_a_task_with_no_target_number_is_skipped(store):
    worker = FakeWorker()
    scheduler, events = build(store, worker, tasks=[make_task(target_number="")])
    due_now(store)

    await scheduler.tick_once()
    await scheduler.drain()
    assert results(events)[0]["status"] == "skipped"


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------


async def test_random_suffix_changes_the_body_every_time(store):
    """Carriers drop repeated identical messages to the same number."""
    worker = FakeWorker()
    scheduler, _ = build(store, worker, tasks=[make_task(random_suffix=True)])

    for _ in range(3):
        due_now(store)
        await scheduler.tick_once()
        await scheduler.drain()

    bodies = [body for _, body in worker.sent]
    assert len(bodies) == 3
    assert len(set(bodies)) == 3, "identical texts are what get filtered"
    assert all(body.startswith("1 ") for body in bodies)


async def test_without_the_suffix_the_content_goes_out_verbatim(store):
    worker = FakeWorker()
    scheduler, _ = build(store, worker, tasks=[make_task(content="CXHF")])
    due_now(store)

    await scheduler.tick_once()
    await scheduler.drain()
    assert worker.sent == [("10086", "CXHF")]


async def test_ping_action(store):
    worker = FakeWorker()
    scheduler, events = build(
        store, worker, tasks=[make_task(action="ping", content="")]
    )
    due_now(store)

    await scheduler.tick_once()
    await scheduler.drain()

    assert worker.pings == ["www.baidu.com"]
    assert results(events)[0]["status"] == "ok"


async def test_ping_that_gets_no_reply_is_a_failure(store):
    worker = FakeWorker(ping_ok=False)
    scheduler, events = build(store, worker, tasks=[make_task(action="ping")])
    due_now(store)

    await scheduler.tick_once()
    await scheduler.drain()
    assert results(events)[0]["status"] == "failed"


async def test_ping_is_skipped_when_packet_data_policy_is_off(store):
    worker = FakeWorker()
    worker.data_enabled = False
    scheduler, events = build(store, worker, tasks=[make_task(action="ping")])
    due_now(store)

    await scheduler.tick_once()
    await scheduler.drain()

    assert worker.pings == []
    receipt = results(events)[0]
    assert receipt["status"] == "skipped"
    assert "移动数据已关闭" in receipt["error"]


async def test_raw_at_action(store):
    worker = FakeWorker()
    scheduler, events = build(
        store, worker, tasks=[make_task(action="raw_at", content="AT+CSQ")]
    )
    due_now(store)

    await scheduler.tick_once()
    await scheduler.drain()

    assert worker.commands == ["AT+CSQ"]
    assert "+CSQ: 24,99" in results(events)[0]["detail"]


async def test_voice_call_action(store):
    worker = FakeWorker()
    scheduler, events = build(
        store, worker, tasks=[make_task(action="voice_call", target_number="10086")]
    )
    due_now(store)

    await scheduler.tick_once()
    await scheduler.drain()

    assert worker.called == ["10086"]
    assert results(events)[0]["status"] == "ok"


async def test_voice_call_that_never_reaches_the_network_is_a_failure(store):
    """A dial the carrier ignored keeps nothing alive, so it must not read ok.

    The modem reports success for the dial itself in this case, which is
    exactly how a roaming card with no working CS path behaves — the whole
    point of judging the call on `reached_network` rather than on the absence
    of an exception.
    """
    worker = FakeWorker(call_reaches_network=False)
    scheduler, events = build(
        store, worker, tasks=[make_task(action="voice_call", target_number="10086")]
    )
    due_now(store)

    await scheduler.tick_once()
    await scheduler.drain()

    assert results(events)[0]["status"] == "failed"


async def test_voice_call_without_a_number_is_skipped(store):
    worker = FakeWorker()
    scheduler, events = build(
        store, worker, tasks=[make_task(action="voice_call", target_number="")]
    )
    due_now(store)

    await scheduler.tick_once()
    await scheduler.drain()

    assert worker.called == []
    assert results(events)[0]["status"] == "skipped"


async def test_raw_at_without_a_command_is_skipped(store):
    worker = FakeWorker()
    scheduler, events = build(
        store, worker, tasks=[make_task(action="raw_at", content="")]
    )
    due_now(store)

    await scheduler.tick_once()
    await scheduler.drain()
    assert results(events)[0]["status"] == "skipped"


# --------------------------------------------------------------------------
# receipts survive an outage
# --------------------------------------------------------------------------


async def test_the_receipt_is_queued_when_the_server_is_unreachable(store):
    """No link, no problem: the task runs and the receipt waits its turn."""
    worker = FakeWorker()
    scheduler, _ = build(
        store, worker,
        emit=lambda kind, payload: store.append_event(kind, payload),
    )
    due_now(store)

    await scheduler.tick_once()
    await scheduler.drain()

    queued = store.unacked_events()
    assert [event.kind for event in queued] == ["task_result"]
    assert queued[0].payload["status"] == "ok"
    assert queued[0].payload["next_run_at"], "the server learns when we run next"


async def test_a_cron_task_reschedules_to_the_next_firing(store):
    worker = FakeWorker()
    clock = Clock()
    scheduler, events = build(
        store, worker, clock=clock,
        tasks=[make_task(schedule_type="cron", schedule_expr="0 3 * * *")],
    )
    due_now(store)

    await scheduler.tick_once()
    await scheduler.drain()

    # NOW is 10:00 on the 2nd, so the next 03:00 is on the 3rd.
    assert store.all_tasks()[0]["next_run_at"].startswith("2026-08-03T03:00")
