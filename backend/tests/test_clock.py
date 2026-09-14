"""IST handling — 'aaj' must mean the Indian calendar day, not the server's."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from munshiji.clock import (
    IST,
    UTC,
    business_day_progress,
    day_bounds_ist,
    days_between,
    ist_date_of,
    now_ist,
    range_bounds_ist,
    to_ist,
    to_utc,
    weekday_name,
)


def test_ist_is_fixed_plus_530() -> None:
    """India has no DST; a fixed offset avoids depending on the OS tz database."""
    assert IST.utcoffset(None) == timedelta(hours=5, minutes=30)


def test_day_bounds_span_exactly_one_day_in_utc() -> None:
    start, end = day_bounds_ist(date(2026, 9, 14))
    assert start == datetime(2026, 9, 13, 18, 30, tzinfo=UTC)
    assert end == datetime(2026, 9, 14, 18, 30, tzinfo=UTC)
    assert end - start == timedelta(days=1)


def test_late_evening_utc_is_already_tomorrow_in_ist() -> None:
    """19:00 UTC on the 13th is 00:30 IST on the 14th — the bug this function exists to prevent."""
    moment = datetime(2026, 9, 13, 19, 0, tzinfo=UTC)
    assert ist_date_of(moment) == date(2026, 9, 14)


def test_naive_datetimes_are_assumed_utc() -> None:
    naive = datetime(2026, 9, 14, 6, 0)
    assert to_ist(naive) == datetime(2026, 9, 14, 11, 30, tzinfo=IST)
    assert to_utc(naive).tzinfo == UTC


def test_range_bounds_are_inclusive_of_both_days() -> None:
    start, end = range_bounds_ist(date(2026, 9, 1), date(2026, 9, 7))
    assert end - start == timedelta(days=7)


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (time(6, 0), 0.0),
        (time(7, 0), 0.0),
        (time(22, 0), 1.0),
        (time(23, 30), 1.0),
        (time(14, 30), 0.5),
    ],
)
def test_business_day_progress(at: time, expected: float) -> None:
    moment = datetime.combine(date(2026, 9, 14), at, tzinfo=IST)
    assert business_day_progress(moment) == pytest.approx(expected, abs=0.01)


def test_progress_is_computed_in_ist_not_server_time() -> None:
    """Same instant, expressed in UTC, must give the same progress."""
    ist_moment = datetime(2026, 9, 14, 14, 30, tzinfo=IST)
    utc_moment = ist_moment.astimezone(timezone.utc)
    assert business_day_progress(ist_moment) == pytest.approx(business_day_progress(utc_moment))


def test_weekday_names() -> None:
    tuesday = date(2026, 9, 15)
    assert weekday_name(tuesday) == "Tuesday"
    assert weekday_name(tuesday, hindi=True) == "मंगलवार"


def test_days_between_uses_calendar_days() -> None:
    assert days_between(date(2026, 9, 1), date(2026, 9, 14)) == 13
    assert days_between(datetime(2026, 9, 1, 23, 0, tzinfo=IST), date(2026, 9, 2)) == 1
    assert days_between(date(2026, 9, 14), date(2026, 9, 1)) == -13


def test_now_ist_is_aware_and_in_ist() -> None:
    moment = now_ist()
    assert moment.tzinfo is not None
    assert moment.utcoffset() == timedelta(hours=5, minutes=30)
