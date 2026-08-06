"""When a keep-alive task runs next.

Pure time arithmetic, deliberately separate from the runner: scheduling is
where the subtle bugs live (a cron field that means something slightly
different from Vixie cron's, an interval measured from the wrong instant), and
none of it needs a modem, an event loop or a clock to test.

Two rules worth stating because they are load-bearing:

* An ``interval`` schedule counts from the **last run**, not from now.  A card
  that must see traffic every 30 days needs its next message 25 days after the
  last one, whatever else happened in between.
* Jitter is ``±jitter_seconds``, applied to the computed time.  Carriers see a
  lot of automated keep-alive traffic land exactly on the hour; drifting off
  the mark is the point of the setting.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

# How far ahead next_cron is willing to search before calling an expression
# unsatisfiable (e.g. "0 0 30 2 *" — the 30th of February).
SEARCH_LIMIT_DAYS = 400

DEFAULT_INTERVAL_DAYS = 25.0


class ScheduleError(ValueError):
    """A schedule expression that cannot be honoured."""


@dataclass(frozen=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    doms: frozenset[int]
    months: frozenset[int]
    dows: frozenset[int]
    dom_any: bool
    dow_any: bool

    def day_matches(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        # Python counts Monday as 0; cron counts Sunday as 0.
        dow_ok = ((day.weekday() + 1) % 7) in self.dows
        dom_ok = day.day in self.doms
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow_ok
        if self.dow_any:
            return dom_ok
        # Vixie cron's rule: with both fields restricted, either one firing is
        # enough ("1st of the month, and every Monday").
        return dom_ok or dow_ok


def _parse_field(raw: str, low: int, high: int) -> tuple[frozenset[int], bool]:
    raw = raw.strip()
    if not raw:
        raise ScheduleError("empty cron field")
    if raw == "*":
        return frozenset(range(low, high + 1)), True

    values: set[int] = set()
    for part in raw.split(","):
        step = 1
        body = part
        if "/" in part:
            body, _, step_raw = part.partition("/")
            try:
                step = int(step_raw)
            except ValueError:
                raise ScheduleError(f"bad step in {part!r}") from None
            if step < 1:
                raise ScheduleError(f"step must be positive in {part!r}")

        if body in ("*", ""):
            start, end = low, high
        elif "-" in body:
            start_raw, _, end_raw = body.partition("-")
            start, end = _int(start_raw, part), _int(end_raw, part)
        else:
            start = _int(body, part)
            # "5/15" is read as "from 5 to the end of the range, every 15" —
            # the same reading crontab gives it.
            end = high if step > 1 else start

        if start > end:
            raise ScheduleError(f"reversed range in {part!r}")
        if start < low or end > high:
            raise ScheduleError(f"{part!r} is outside {low}-{high}")
        values.update(range(start, end + 1, step))

    if not values:
        raise ScheduleError(f"{raw!r} matches nothing")
    return frozenset(values), False


def _int(raw: str, context: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise ScheduleError(f"{context!r} is not a number") from None


def parse_cron(expression: str) -> Cron:
    """Parse a 5-field cron expression: minute hour day-of-month month day-of-week."""
    fields = (expression or "").split()
    if len(fields) != 5:
        raise ScheduleError(
            f"cron needs 5 fields (minute hour day month weekday), got {len(fields)}"
        )

    minutes, _ = _parse_field(fields[0], 0, 59)
    hours, _ = _parse_field(fields[1], 0, 23)
    doms, dom_any = _parse_field(fields[2], 1, 31)
    months, _ = _parse_field(fields[3], 1, 12)
    dows, dow_any = _parse_field(fields[4], 0, 7)
    # Both 0 and 7 mean Sunday.
    dows = frozenset(0 if value == 7 else value for value in dows)

    return Cron(
        minutes=minutes, hours=hours, doms=doms, months=months, dows=dows,
        dom_any=dom_any, dow_any=dow_any,
    )


def next_cron(expression: str, after: datetime) -> datetime:
    """The first cron firing strictly after ``after``, in ``after``'s timezone."""
    cron = parse_cron(expression)
    moment = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    hours = sorted(cron.hours)
    minutes = sorted(cron.minutes)

    day = moment.date()
    for offset in range(SEARCH_LIMIT_DAYS):
        if cron.day_matches(day):
            floor = moment if offset == 0 else None
            for hour in hours:
                for minute in minutes:
                    at = datetime.combine(
                        day, time(hour, minute), tzinfo=after.tzinfo
                    )
                    if floor is None or at >= floor:
                        return at
        day += timedelta(days=1)

    raise ScheduleError(
        f"{expression!r} has no run within {SEARCH_LIMIT_DAYS} days"
    )


def next_interval(expression: str, after: datetime) -> datetime:
    """``expression`` is a number of days, counted from the last run."""
    try:
        days = float(expression)
    except (TypeError, ValueError) as exc:
        raise ScheduleError(
            f"interval must be a number of days, got {expression!r}"
        ) from exc
    if days <= 0:
        raise ScheduleError("interval must be greater than zero days")
    return after + timedelta(days=days)


def next_run(
    task: dict[str, Any],
    *,
    after: datetime,
    floor: datetime | None = None,
    rng: random.Random | None = None,
) -> datetime:
    """When ``task`` should run next.

    ``after`` is what the schedule counts from — the last run for intervals,
    the current time for cron.  ``floor`` keeps a negative jitter (or a long
    overdue interval) from landing in the past, which would make the task fire
    again the moment it finished.
    """
    kind = task.get("schedule_type") or "interval"
    expression = str(task.get("schedule_expr") or "").strip()

    if kind == "cron":
        moment = next_cron(expression, after)
    elif kind == "interval":
        moment = next_interval(expression or str(DEFAULT_INTERVAL_DAYS), after)
    else:
        raise ScheduleError(f"unknown schedule type {kind!r}")

    jitter = int(task.get("jitter_seconds") or 0)
    if jitter:
        generator = rng or random
        moment += timedelta(seconds=generator.uniform(-jitter, jitter))

    if floor is not None and moment < floor:
        return floor
    return moment
