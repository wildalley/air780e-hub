"""Schedule arithmetic.

Nothing here touches a modem or a clock: every case pins an exact expected
datetime, because "roughly the right day" is not a property a keep-alive
schedule can be trusted on — the card is cancelled or it is not.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from air780e_agent.schedule import (
    ScheduleError,
    next_cron,
    next_interval,
    next_run,
    parse_cron,
)

CST = timezone(timedelta(hours=8))


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=CST)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def test_every_field_wildcard():
    cron = parse_cron("* * * * *")
    assert len(cron.minutes) == 60
    assert len(cron.hours) == 24
    assert cron.dom_any and cron.dow_any


def test_lists_ranges_and_steps():
    cron = parse_cron("0,30 9-17 */10 * *")
    assert sorted(cron.minutes) == [0, 30]
    assert sorted(cron.hours) == list(range(9, 18))
    assert sorted(cron.doms) == [1, 11, 21, 31]


def test_sunday_is_both_zero_and_seven():
    assert parse_cron("0 0 * * 7").dows == parse_cron("0 0 * * 0").dows


@pytest.mark.parametrize("expression", [
    "",                 # nothing
    "0 3 * *",          # four fields
    "0 3 * * * *",      # six fields
    "60 3 * * *",       # minute out of range
    "0 24 * * *",       # hour out of range
    "0 3 * * mon",      # names are not supported
    "10-5 3 * * *",     # reversed range
    "0 3 * * */0",      # zero step
])
def test_bad_expressions_are_rejected(expression):
    with pytest.raises(ScheduleError):
        parse_cron(expression)


# --------------------------------------------------------------------------
# next firing
# --------------------------------------------------------------------------


def test_next_cron_is_strictly_after():
    """Standing exactly on a firing must not return that same minute again."""
    assert next_cron("0 3 * * *", at("2026-08-02T03:00:00")) == at("2026-08-03T03:00:00")


def test_next_cron_later_today():
    assert next_cron("30 3 * * *", at("2026-08-02T01:00:00")) == at("2026-08-02T03:30:00")


def test_next_cron_picks_the_weekday():
    # 2026-08-02 is a Sunday; the next Tuesday is the 4th.
    assert next_cron("0 3 * * 2", at("2026-08-02T12:00:00")) == at("2026-08-04T03:00:00")


def test_next_cron_crosses_a_month():
    assert next_cron("0 0 1 * *", at("2026-08-15T12:00:00")) == at("2026-09-01T00:00:00")


def test_day_of_month_and_weekday_are_or_not_and():
    """Vixie cron's rule: with both set, either one firing is enough."""
    # The 1st (a Tuesday) and every Friday.
    assert next_cron("0 0 1 * 5", at("2026-09-02T00:00:00")) == at("2026-09-04T00:00:00")
    assert next_cron("0 0 1 * 5", at("2026-09-30T12:00:00")) == at("2026-10-01T00:00:00")


def test_impossible_date_is_reported_not_hung():
    with pytest.raises(ScheduleError):
        next_cron("0 0 30 2 *", at("2026-08-02T00:00:00"))


def test_interval_counts_days_from_the_anchor():
    assert next_interval("25", at("2026-08-02T10:00:00")) == at("2026-08-27T10:00:00")


@pytest.mark.parametrize("expression", ["", "abc", "0", "-3"])
def test_bad_interval_is_rejected(expression):
    with pytest.raises(ScheduleError):
        next_interval(expression, at("2026-08-02T10:00:00"))


# --------------------------------------------------------------------------
# next_run: what the scheduler actually calls
# --------------------------------------------------------------------------


def task(**overrides):
    base = {
        "schedule_type": "interval", "schedule_expr": "25", "jitter_seconds": 0,
    }
    return {**base, **overrides}


def test_interval_runs_from_the_last_run_not_from_now():
    """A 25-day cadence must not drift every time the agent restarts."""
    last_run = at("2026-08-02T10:00:00")
    assert next_run(task(), after=last_run) == at("2026-08-27T10:00:00")


def test_jitter_stays_inside_its_band():
    now = at("2026-08-02T10:00:00")
    rng = random.Random(1234)
    for _ in range(50):
        moment = next_run(task(jitter_seconds=1800), after=now, rng=rng)
        drift = abs((moment - at("2026-08-27T10:00:00")).total_seconds())
        assert drift <= 1800


def test_jitter_actually_moves_the_time():
    """Otherwise every module in the country sends on the hour."""
    now = at("2026-08-02T10:00:00")
    rng = random.Random(7)
    moments = {next_run(task(jitter_seconds=1800), after=now, rng=rng) for _ in range(10)}
    assert len(moments) > 1
    assert at("2026-08-27T10:00:00") not in moments


def test_floor_keeps_a_negative_jitter_out_of_the_past():
    """A past due time would make the task fire again the moment it finished."""
    now = at("2026-08-02T10:00:00")
    overdue = task(schedule_expr="0.0001", jitter_seconds=3600)
    for _ in range(20):
        assert next_run(overdue, after=now, floor=now, rng=random.Random(3)) >= now


def test_cron_schedule_goes_through_next_run():
    now = at("2026-08-02T12:00:00")
    moment = next_run(
        task(schedule_type="cron", schedule_expr="0 3 * * 2"), after=now
    )
    assert moment == at("2026-08-04T03:00:00")


def test_unknown_schedule_type_is_rejected():
    with pytest.raises(ScheduleError):
        next_run(task(schedule_type="lunar"), after=at("2026-08-02T10:00:00"))
