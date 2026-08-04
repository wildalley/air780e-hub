"""Keep-alive scheduler.

Runs on the agent, not the server.  A missed keep-alive cannot be
made up for after the fact — a card that goes quiet long enough is simply
cancelled — so execution must not depend on the link being up.  The server
edits tasks and receives receipts; the clock that matters is this one.

Failure handling is deliberately patient rather than clever: a module that is
offline right now is usually a USB reset that will be over in a minute, so a
failed attempt waits and tries again instead of burning the whole schedule.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Any, Callable

from .schedule import ScheduleError, next_run
from .store import LocalStore
from .worker import DeviceWorker

log = logging.getLogger(__name__)

EmitCallback = Callable[[str, dict[str, Any]], None]

# Characters a carrier's SMS gateway will not mangle, minus the ones that are
# hard to tell apart in a delivery report.
SUFFIX_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
SUFFIX_LENGTH = 4

DEFAULT_PING_HOST = "www.baidu.com"

# Cap on what a receipt carries back; a raw AT dump can be long.
DETAIL_LIMIT = 200


class TaskSkipped(RuntimeError):
    """The task cannot run and retrying will not help."""


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


class KeepAliveScheduler:
    def __init__(
        self,
        store: LocalStore,
        workers: dict[str, DeviceWorker],
        emit: EmitCallback,
        *,
        tick: float = 30.0,
        retry_delay: float = 60.0,
        rng: random.Random | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.store = store
        self.workers = workers
        self.emit = emit
        self.tick = tick
        self.retry_delay = retry_delay
        self.rng = rng or random.Random()
        self.clock = clock
        self._running: dict[int, asyncio.Task] = {}
        self._stopped = False

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        log.info("keep-alive scheduler started (tick %.0fs)", self.tick)
        while not self._stopped:
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A scheduling bug must not stop the loop; the next tick may
                # well succeed, and the modules keep working either way.
                log.exception("scheduler tick failed")
            await asyncio.sleep(self.tick)

    async def stop(self) -> None:
        self._stopped = True
        for task in list(self._running.values()):
            task.cancel()
        await self.drain()

    async def drain(self) -> None:
        while self._running:
            await asyncio.gather(*list(self._running.values()), return_exceptions=True)

    # -- the tick ----------------------------------------------------------

    async def tick_once(self) -> int:
        """Plan unplanned tasks, start the due ones.  Returns how many started."""
        now = self.clock()
        started = 0

        for task in self.store.all_tasks():
            task_id = int(task["id"])
            if not task["enabled"] or task_id in self._running:
                continue

            due = _parse(task["next_run_at"])
            if due is None:
                due = self._plan(task, now)
                if due is None:
                    continue
            if due > now:
                continue

            handle = asyncio.create_task(
                self._execute(task), name=f"keepalive-{task_id}"
            )
            self._running[task_id] = handle
            handle.add_done_callback(lambda _, key=task_id: self._running.pop(key, None))
            started += 1

        return started

    def _plan(self, task: dict[str, Any], now: datetime) -> datetime | None:
        """Work out and store when a task with no plan should first run."""
        # An interval counts from the last run, so a task that has run before
        # keeps its cadence across an agent restart; a fresh one starts its
        # first full interval now rather than firing immediately.
        anchor = _parse(task["last_run_at"]) or now
        if (task["schedule_type"] or "interval") == "cron":
            anchor = now

        try:
            moment = next_run(task, after=anchor, floor=now, rng=self.rng)
        except ScheduleError as exc:
            log.warning("task %s has an unusable schedule: %s", task["id"], exc)
            self._report(task, "skipped", 0, "", f"bad schedule: {exc}", now)
            # Leave next_run_at NULL: fixing the expression on the server is
            # what should make it run, not another tick.
            return None

        self.store.set_task_next_run(int(task["id"]), _iso(moment))
        log.info("task %s scheduled for %s", task["id"], _iso(moment))
        return moment

    # -- execution ---------------------------------------------------------

    async def _execute(self, task: dict[str, Any]) -> None:
        retry_max = max(0, int(task["retry_max"] or 0))
        attempts = 0
        detail = ""
        error: str | None = None
        status = "failed"

        while attempts <= retry_max:
            attempts += 1
            try:
                detail = await self._perform(task)
            except asyncio.CancelledError:
                raise
            except TaskSkipped as exc:
                status, error = "skipped", str(exc)
                break
            except Exception as exc:
                # DeviceOffline lands here too: a module mid-USB-reset is the
                # single most likely reason a keep-alive fails, and it is
                # exactly the case worth retrying.
                error = str(exc) or type(exc).__name__
                log.warning(
                    "task %s attempt %d/%d failed: %s",
                    task["id"], attempts, retry_max + 1, error,
                )
                if attempts <= retry_max:
                    # Linear, not exponential: the common cause is a module
                    # that is mid-reset, which resolves in seconds.
                    await asyncio.sleep(self.retry_delay * attempts)
                    continue
                break
            else:
                status, error = "ok", None
                break

        finished = self.clock()
        planned = self._reschedule(task, finished, ran=status != "skipped")
        self._report(task, status, attempts, detail, error, finished, planned)

    def _reschedule(
        self, task: dict[str, Any], finished: datetime, *, ran: bool
    ) -> str | None:
        """Record the run and plan the next one."""
        try:
            moment = next_run(task, after=finished, floor=finished, rng=self.rng)
        except ScheduleError as exc:
            log.warning("task %s cannot be rescheduled: %s", task["id"], exc)
            self.store.mark_task_run(
                int(task["id"]),
                last_run=_iso(finished) if ran else task["last_run_at"],
                next_run=None,
            )
            return None

        self.store.mark_task_run(
            int(task["id"]),
            last_run=_iso(finished) if ran else task["last_run_at"],
            next_run=_iso(moment),
        )
        return _iso(moment)

    async def _perform(self, task: dict[str, Any]) -> str:
        worker = self.workers.get(task["device"])
        if worker is None:
            raise TaskSkipped(f"no device named {task['device']!r} on this agent")

        action = task["action"] or "send_sms"
        if action == "send_sms":
            number = str(task["target_number"] or "").strip()
            if not number:
                raise TaskSkipped("task has no target number")
            body = self._body(task)
            refs = await worker.send_sms(number, body)
            return f'sent to {number}: "{body}" ({len(refs)} segment(s))'

        if action == "ping":
            host = str(task["content"] or "").strip() or DEFAULT_PING_HOST
            if not await worker.ping(host):
                raise RuntimeError(f"ping {host} got no reply")
            return f"ping {host} ok"

        if action == "raw_at":
            command = str(task["content"] or "").strip()
            if not command:
                raise TaskSkipped("task has no AT command")
            lines = await worker.raw_at(command)
            return f"{command} -> {'; '.join(lines)}"[:DETAIL_LIMIT]

        raise TaskSkipped(f"unknown action {action!r}")

    def _body(self, task: dict[str, Any]) -> str:
        content = str(task["content"] or "")
        if not task["random_suffix"]:
            return content
        # Carriers drop repeated identical messages to the same number, which
        # is exactly what an unchanging keep-alive text looks like.
        suffix = "".join(
            self.rng.choice(SUFFIX_ALPHABET) for _ in range(SUFFIX_LENGTH)
        )
        return f"{content} {suffix}".strip()

    # -- receipts ----------------------------------------------------------

    def _report(
        self,
        task: dict[str, Any],
        status: str,
        attempts: int,
        detail: str,
        error: str | None,
        moment: datetime,
        next_at: str | None = None,
    ) -> None:
        log.info(
            "task %s (%s) %s after %d attempt(s)",
            task["id"], task["name"] or task["action"], status, attempts,
        )
        self.emit("task_result", {
            "task_id": int(task["id"]),
            "device": task["device"],
            "ts": _iso(moment),
            "status": status,
            "attempts": attempts,
            "detail": detail[:DETAIL_LIMIT],
            "error": error,
            "next_run_at": next_at,
            "notify": bool(task["notify_on_result"]),
        })
