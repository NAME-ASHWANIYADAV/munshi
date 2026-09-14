"""Time handling for MunshiJi — everything the merchant sees is Asia/Kolkata.

India has no DST, so IST is modelled as a fixed +05:30 offset. This avoids depending on
the OS tz database (notably absent on some Windows Python installs).

Storage rule: timestamps are persisted as timezone-aware UTC. Display and all business-day
arithmetic happen in IST.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

__all__ = [
    "IST",
    "UTC",
    "WEEKDAY_NAMES_HI",
    "business_day_progress",
    "day_bounds_ist",
    "days_between",
    "ensure_aware",
    "ist_date_of",
    "now_ist",
    "now_utc",
    "range_bounds_ist",
    "to_ist",
    "to_utc",
    "today_ist",
    "weekday_name",
]

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
UTC = timezone.utc

WEEKDAY_NAMES_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAY_NAMES_HI = [
    "सोमवार",  # Somvaar
    "मंगलवार",  # Mangalvaar
    "बुधवार",  # Budhvaar
    "गुरुवार",  # Guruvaar
    "शुक्रवार",  # Shukravaar
    "शनिवार",  # Shanivaar
    "रविवार",  # Ravivaar
]

#: Default kirana trading window, used for intraday projection.
BUSINESS_START = time(7, 0)
BUSINESS_END = time(22, 0)


def now_utc() -> datetime:
    """Current instant, timezone-aware UTC."""
    return datetime.now(UTC)


def now_ist() -> datetime:
    """Current instant, expressed in IST."""
    return datetime.now(UTC).astimezone(IST)


def today_ist() -> date:
    """Today's calendar date in IST."""
    return now_ist().date()


def ensure_aware(value: datetime, *, assume: timezone = UTC) -> datetime:
    """Attach ``assume`` to a naive datetime; pass aware datetimes through unchanged."""
    if value.tzinfo is None:
        return value.replace(tzinfo=assume)
    return value


def to_ist(value: datetime) -> datetime:
    """Convert any datetime to IST (naive values are assumed UTC)."""
    return ensure_aware(value).astimezone(IST)


def to_utc(value: datetime) -> datetime:
    """Convert any datetime to UTC (naive values are assumed UTC)."""
    return ensure_aware(value).astimezone(UTC)


def ist_date_of(value: datetime) -> date:
    """The IST calendar date a timestamp falls on."""
    return to_ist(value).date()


def day_bounds_ist(day: date) -> tuple[datetime, datetime]:
    """UTC-aware ``[start, end)`` bounds of one IST calendar day.

    Use for ``WHERE occurred_at >= start AND occurred_at < end`` queries.
    """
    start_ist = datetime.combine(day, time.min, tzinfo=IST)
    return start_ist.astimezone(UTC), (start_ist + timedelta(days=1)).astimezone(UTC)


def range_bounds_ist(first_day: date, last_day: date) -> tuple[datetime, datetime]:
    """UTC-aware ``[start, end)`` bounds covering ``first_day`` through ``last_day`` inclusive."""
    start, _ = day_bounds_ist(first_day)
    _, end = day_bounds_ist(last_day)
    return start, end


def weekday_name(day: date, *, hindi: bool = False) -> str:
    """Weekday name for a date; ``hindi=True`` returns Devanagari."""
    return (WEEKDAY_NAMES_HI if hindi else WEEKDAY_NAMES_EN)[day.weekday()]


def business_day_progress(
    at: datetime | None = None,
    *,
    start: time = BUSINESS_START,
    end: time = BUSINESS_END,
) -> float:
    """Fraction (0.0–1.0) of the trading day elapsed in IST at ``at`` (default: now).

    Returns 0.0 before opening and 1.0 after closing. Drives the partial-day sales projection.
    """
    moment = to_ist(at) if at is not None else now_ist()
    open_at = datetime.combine(moment.date(), start, tzinfo=IST)
    close_at = datetime.combine(moment.date(), end, tzinfo=IST)
    if moment <= open_at:
        return 0.0
    if moment >= close_at:
        return 1.0
    return (moment - open_at).total_seconds() / (close_at - open_at).total_seconds()


def days_between(earlier: datetime | date, later: datetime | date) -> int:
    """Whole IST calendar days from ``earlier`` to ``later`` (negative if reversed)."""
    a = ist_date_of(earlier) if isinstance(earlier, datetime) else earlier
    b = ist_date_of(later) if isinstance(later, datetime) else later
    return (b - a).days
