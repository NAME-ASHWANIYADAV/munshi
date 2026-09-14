"""Derived analytical frames over the merchant's raw rows.

``repositories/core.py`` holds the plain fetches; everything here is a *frame* — a small,
immutable, JSON-friendly summary that an insight engine can reason about without touching SQL.

Three conventions run through the module:

* **All bucketing happens in IST.** Timestamps come back from SQLite naive (the dialect drops
  ``tzinfo``); :func:`munshiji.clock.to_ist` re-attaches UTC and converts, so a transaction at
  23:30 UTC lands on the *next* IST day, which is what the merchant means by "kal".
* **Returns are excluded** from every revenue series, matching ``core.day_collection_paise``.
* **Money stays in integer paise**; only shares, rates and percentages are floats.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from munshiji.clock import (
    day_bounds_ist,
    ist_date_of,
    range_bounds_ist,
    to_ist,
)
from munshiji.db.enums import CustomerSegment, KhataStatus, PaymentMethod
from munshiji.db.models import Customer, KhataEntry, Product, Transaction, TransactionItem
from munshiji.insights.stats import (
    clamp,
    ewma,
    mad,
    median,
    percentile,
    quintile_rank,
    quintiles,
    safe_div,
    share_of,
)

if TYPE_CHECKING:  # pragma: no cover - import kept lazy so analytics never imports the engines
    from munshiji.insights.base import InsightContext

__all__ = [
    "IMPLIED_CREDIT_TERM_DAYS",
    "MIN_PROJECTION_SHARE",
    "RFM_SEGMENT_GRID",
    "CategoryMargin",
    "DaySales",
    "IntradayProfile",
    "MixFrame",
    "ProductMovement",
    "Projection",
    "RFMScore",
    "SettleStats",
    "VisitHistory",
    "WeekdayBaseline",
    "category_margins",
    "compute_rfm",
    "daily_revenue",
    "day_window",
    "first_transaction_day",
    "hour_histogram",
    "intraday_profile",
    "khata_settle_stats",
    "new_customers_per_week",
    "payment_mix",
    "product_movements",
    "project_close",
    "revenue_between",
    "segment_for",
    "visit_histories",
    "weekday_baseline",
]

#: Below this share of the day's revenue the projection denominator is too small to trust
#: (``projected = collected / share`` amplifies noise by ``1/share``). ~8% is roughly the first
#: 45 minutes of a kirana's morning peak.
MIN_PROJECTION_SHARE = 0.08

#: Percentile pair used for the projection's empirical confidence band.
PROJECTION_BAND_PCT = (20.0, 80.0)


# ─────────────────────────────────────────────────────────────────────────────
# Frames
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class DaySales:
    """One IST calendar day of trading."""

    day: date
    revenue_paise: int
    txn_count: int


@dataclass(slots=True, frozen=True)
class WeekdayBaseline:
    """Trailing same-weekday history: the reference a single day is judged against.

    Same-weekday rather than same-day-of-month because a kirana's dominant seasonality is weekly
    (Sat/Sun ~1.35x, Tue lowest) — comparing Monday to Sunday would drown any real signal.
    """

    weekday: int
    days: tuple[DaySales, ...]
    median_paise: float
    mad_paise: float

    @property
    def sample_count(self) -> int:
        return len(self.days)

    @property
    def values(self) -> list[float]:
        return [float(day.revenue_paise) for day in self.days]


@dataclass(slots=True, frozen=True)
class IntradayProfile:
    """Per-day cumulative revenue shares for one weekday, the basis of the live projection.

    ``day_cum[d][h]`` is the share of day ``d``'s revenue banked *strictly before* hour ``h``,
    so ``day_cum[d][0] == 0.0`` and ``day_cum[d][24] == 1.0``.
    """

    weekday: int
    day_cum: dict[date, tuple[float, ...]] = field(default_factory=dict)

    @property
    def sample_count(self) -> int:
        return len(self.day_cum)

    def cum_by_hour(self) -> list[float]:
        """Median cumulative share at each hour boundary — the published intraday curve."""
        if not self.day_cum:
            return [0.0] * 25
        columns = list(zip(*self.day_cum.values(), strict=True))
        return [round(median(column), 6) for column in columns]

    def shares_at(self, at: datetime) -> list[float]:
        """Each historical day's cumulative share at ``at``'s IST time-of-day.

        Linear interpolation inside the hour bucket, so 12:30 sits half way between the 12:00
        and 13:00 cumulative shares rather than snapping to a whole hour.
        """
        moment = to_ist(at)
        hour = moment.hour
        frac = (moment.minute * 60 + moment.second) / 3600.0
        shares: list[float] = []
        for cum in self.day_cum.values():
            low = cum[hour]
            high = cum[min(hour + 1, 24)]
            shares.append(low + (high - low) * frac)
        return shares


@dataclass(slots=True, frozen=True)
class Projection:
    """Today's projected close from partial collections."""

    collected_paise: int
    elapsed_share: float
    projected_paise: int | None
    band_low_paise: int | None
    band_high_paise: int | None
    too_early: bool
    sample_days: int


@dataclass(slots=True, frozen=True)
class MixFrame:
    """Payment-method split over one window, by revenue and by transaction count."""

    revenue_paise: dict[str, int]
    counts: dict[str, int]

    @property
    def total_paise(self) -> int:
        return sum(self.revenue_paise.values())

    @property
    def total_count(self) -> int:
        return sum(self.counts.values())

    def revenue_share(self, method: str) -> float:
        return share_of(self.revenue_paise.get(method, 0), self.total_paise)


@dataclass(slots=True, frozen=True)
class CategoryMargin:
    """Realised gross margin for one product category over a window."""

    category: str
    revenue_paise: int
    cost_paise: int
    units: float
    line_count: int

    @property
    def margin_paise(self) -> int:
        return self.revenue_paise - self.cost_paise

    @property
    def margin_pct(self) -> float:
        """Gross margin as a percentage of revenue (0 when nothing sold)."""
        return safe_div(self.margin_paise, self.revenue_paise, 0.0) * 100.0


@dataclass(slots=True)
class VisitHistory:
    """One customer's purchase rhythm. A *visit* is an IST day on which they bought anything."""

    customer_id: str
    name: str
    visit_days: list[date]
    amounts_paise: list[int]

    @property
    def txn_count(self) -> int:
        return len(self.amounts_paise)

    @property
    def visit_count(self) -> int:
        return len(self.visit_days)

    @property
    def total_paise(self) -> int:
        return sum(self.amounts_paise)

    @property
    def first_visit(self) -> date | None:
        return self.visit_days[0] if self.visit_days else None

    @property
    def last_visit(self) -> date | None:
        return self.visit_days[-1] if self.visit_days else None

    @property
    def avg_ticket_paise(self) -> int:
        return int(round(safe_div(self.total_paise, self.txn_count, 0.0)))

    def gaps_days(self) -> list[int]:
        """Inter-visit gaps in whole days; ``n_visits - 1`` entries."""
        return [
            (later - earlier).days
            for earlier, later in zip(self.visit_days, self.visit_days[1:], strict=False)
        ]

    def days_since_last(self, as_of: date) -> int:
        last = self.last_visit
        return (as_of - last).days if last else 10**6


@dataclass(slots=True, frozen=True)
class RFMScore:
    """Recency / Frequency / Monetary quintiles and the segment they map to."""

    customer_id: str
    name: str
    recency_days: int
    frequency: int
    monetary_paise: int
    avg_ticket_paise: int
    r: int
    f: int
    m: int
    segment: CustomerSegment
    last_visit: date | None

    @property
    def cell(self) -> str:
        """The classic ``RFM`` cell string, e.g. ``"544"``."""
        return f"{self.r}{self.f}{self.m}"


@dataclass(slots=True, frozen=True)
class ProductMovement:
    """Demand history for one SKU."""

    product: Product
    daily_units: tuple[float, ...]
    window_days: int
    units_sold: float
    revenue_paise: int
    last_sale_day: date | None

    @property
    def rate_units_per_day(self) -> float:
        """EWMA(alpha=0.3) consumption rate — see :mod:`munshiji.insights.inventory`."""
        return ewma(self.daily_units, 0.3)

    @property
    def mean_units_per_day(self) -> float:
        return safe_div(self.units_sold, self.window_days, 0.0)


@dataclass(slots=True, frozen=True)
class SettleStats:
    """How one customer has historically behaved on closed khata entries."""

    customer_id: str
    settled_count: int
    avg_days_to_settle: float
    late_share: float


# ─────────────────────────────────────────────────────────────────────────────
# Raw fetch helpers
# ─────────────────────────────────────────────────────────────────────────────


def _txn_rows(
    session: Session,
    merchant_id: str,
    start: datetime,
    end: datetime,
) -> Sequence[Any]:
    """Sale rows in ``[start, end)`` (UTC bounds), returns excluded, oldest first."""
    stmt = (
        select(
            Transaction.id,
            Transaction.customer_id,
            Transaction.amount_paise,
            Transaction.occurred_at,
            Transaction.payment_method,
        )
        .where(
            Transaction.merchant_id == merchant_id,
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
            Transaction.is_return.is_(False),
        )
        .order_by(Transaction.occurred_at)
    )
    return session.execute(stmt).all()


def first_transaction_day(session: Session, merchant_id: str) -> date | None:
    """IST date of the merchant's earliest recorded sale — the floor for any zero-filled series."""
    earliest = session.scalar(
        select(func.min(Transaction.occurred_at)).where(Transaction.merchant_id == merchant_id)
    )
    return ist_date_of(earliest) if earliest else None


def revenue_between(session: Session, merchant_id: str, start: datetime, end: datetime) -> int:
    """Total collected in ``[start, end)``, in paise. Bounds may be IST- or UTC-aware."""
    total = session.scalar(
        select(func.coalesce(func.sum(Transaction.amount_paise), 0)).where(
            Transaction.merchant_id == merchant_id,
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
            Transaction.is_return.is_(False),
        )
    )
    return int(total or 0)


def daily_revenue(
    session: Session,
    merchant_id: str,
    first_day: date,
    last_day: date,
    *,
    zero_fill: bool = True,
) -> dict[date, DaySales]:
    """Revenue and sale count per IST day across ``[first_day, last_day]`` inclusive.

    With ``zero_fill`` (default) days without sales become explicit zeros — a shut Sunday is a
    real observation and must pull the baseline down — but only from the merchant's first
    recorded sale onwards, so pre-history is never invented.
    """
    if last_day < first_day:
        return {}
    start, end = range_bounds_ist(first_day, last_day)
    revenue: dict[date, int] = defaultdict(int)
    counts: dict[date, int] = defaultdict(int)
    for row in _txn_rows(session, merchant_id, start, end):
        day = ist_date_of(row.occurred_at)
        revenue[day] += int(row.amount_paise)
        counts[day] += 1

    frame: dict[date, DaySales] = {}
    if zero_fill:
        floor = first_transaction_day(session, merchant_id)
        if floor is None:
            # No trading history at all: there is nothing for a zero to be a zero *of*, and
            # inventing 56 blank days would hand every engine a fake all-zero baseline.
            return {}
        cursor = max(first_day, floor)
        while cursor <= last_day:
            frame[cursor] = DaySales(cursor, revenue.get(cursor, 0), counts.get(cursor, 0))
            cursor += timedelta(days=1)
    else:
        for day in sorted(revenue):
            frame[day] = DaySales(day, revenue[day], counts[day])
    return frame


# ─────────────────────────────────────────────────────────────────────────────
# Sales frames
# ─────────────────────────────────────────────────────────────────────────────


def weekday_baseline(
    session: Session,
    merchant_id: str,
    *,
    as_of: datetime,
    weeks: int = 8,
) -> WeekdayBaseline:
    """Median + MAD of the trailing ``weeks`` occurrences of ``as_of``'s weekday.

    ``as_of``'s own day is excluded — you cannot judge today against itself.
    """
    today = ist_date_of(as_of)
    candidates = [today - timedelta(days=7 * step) for step in range(1, weeks + 1)]
    earliest = min(candidates)
    frame = daily_revenue(session, merchant_id, earliest, today - timedelta(days=1))
    days = tuple(frame[day] for day in sorted(candidates) if day in frame)
    values = [float(day.revenue_paise) for day in days]
    return WeekdayBaseline(
        weekday=today.weekday(),
        days=days,
        median_paise=median(values),
        mad_paise=mad(values),
    )


def intraday_profile(
    session: Session,
    merchant_id: str,
    *,
    as_of: datetime,
    weeks: int = 8,
) -> IntradayProfile:
    """Cumulative-share curve per historical occurrence of ``as_of``'s weekday.

    Days with zero revenue are skipped: a share of a zero total is undefined, and including them
    would bias the curve rather than widen the band honestly.
    """
    today = ist_date_of(as_of)
    wanted = {today - timedelta(days=7 * step) for step in range(1, weeks + 1)}
    if not wanted:
        return IntradayProfile(weekday=today.weekday())
    start, end = range_bounds_ist(min(wanted), max(wanted))
    hourly: dict[date, list[int]] = {}
    for row in _txn_rows(session, merchant_id, start, end):
        moment = to_ist(row.occurred_at)
        day = moment.date()
        if day not in wanted:
            continue
        bucket = hourly.setdefault(day, [0] * 24)
        bucket[moment.hour] += int(row.amount_paise)

    day_cum: dict[date, tuple[float, ...]] = {}
    for day, buckets in hourly.items():
        total = sum(buckets)
        if total <= 0:
            continue
        cumulative = [0.0]
        running = 0
        for amount in buckets:
            running += amount
            cumulative.append(running / total)
        cumulative[-1] = 1.0
        day_cum[day] = tuple(cumulative)
    return IntradayProfile(weekday=today.weekday(), day_cum=dict(sorted(day_cum.items())))


def project_close(
    profile: IntradayProfile,
    collected_paise: int,
    *,
    at: datetime,
    min_share: float = MIN_PROJECTION_SHARE,
) -> Projection:
    """Project today's closing total from what is banked so far.

    ``projected = collected_so_far / cum_share(now)`` where ``cum_share`` is the *median* share
    across historical same-weekday days. The band inverts the 20th/80th percentile of that same
    share distribution — a day that historically banks less by now implies a *higher* close, so
    the percentiles swap sides. Below ``min_share`` the ratio is unstable and we refuse to
    project rather than shout a number we cannot stand behind.
    """
    shares = profile.shares_at(at)
    if not shares:
        return Projection(collected_paise, 0.0, None, None, None, True, 0)

    elapsed = median(shares)
    if elapsed < min_share:
        return Projection(collected_paise, round(elapsed, 6), None, None, None, True, len(shares))

    low_pct, high_pct = PROJECTION_BAND_PCT
    share_low = max(percentile(shares, low_pct), min_share)
    share_high = max(percentile(shares, high_pct), share_low)
    projected = int(round(collected_paise / elapsed))
    band_high = int(round(collected_paise / share_low))
    band_low = int(round(collected_paise / share_high))
    return Projection(
        collected_paise=collected_paise,
        elapsed_share=round(elapsed, 6),
        projected_paise=projected,
        band_low_paise=min(band_low, projected),
        band_high_paise=max(band_high, projected),
        too_early=False,
        sample_days=len(shares),
    )


def hour_histogram(
    session: Session,
    merchant_id: str,
    *,
    as_of: datetime,
    days: int = 30,
) -> dict[int, int]:
    """Revenue by IST hour-of-day over the trailing ``days`` complete days."""
    last_day = ist_date_of(as_of) - timedelta(days=1)
    first_day = last_day - timedelta(days=days - 1)
    start, end = range_bounds_ist(first_day, last_day)
    histogram = dict.fromkeys(range(24), 0)
    for row in _txn_rows(session, merchant_id, start, end):
        histogram[to_ist(row.occurred_at).hour] += int(row.amount_paise)
    return histogram


def payment_mix(
    session: Session,
    merchant_id: str,
    *,
    first_day: date,
    last_day: date,
) -> MixFrame:
    """Revenue and transaction counts per payment method over an inclusive IST day range."""
    start, end = range_bounds_ist(first_day, last_day)
    revenue = {method.value: 0 for method in PaymentMethod}
    counts = {method.value: 0 for method in PaymentMethod}
    for row in _txn_rows(session, merchant_id, start, end):
        method = row.payment_method
        key = method.value if isinstance(method, PaymentMethod) else str(method)
        revenue[key] = revenue.get(key, 0) + int(row.amount_paise)
        counts[key] = counts.get(key, 0) + 1
    return MixFrame(revenue_paise=revenue, counts=counts)


def category_margins(
    session: Session,
    merchant_id: str,
    *,
    first_day: date,
    last_day: date,
) -> dict[str, CategoryMargin]:
    """Realised gross margin per category from line items.

    Uses ``TransactionItem.unit_price_paise`` against ``unit_cost_paise`` — the cost *as sold*,
    which is the only way to see a margin slip caused by a supplier price rise. Rows written
    before per-line costing existed (``unit_cost_paise == 0``) fall back to the product's
    current cost price so one bad row cannot report a 100% margin.
    """
    start, end = range_bounds_ist(first_day, last_day)
    stmt = (
        select(
            Product.category,
            TransactionItem.qty,
            TransactionItem.unit_price_paise,
            TransactionItem.unit_cost_paise,
            Product.cost_price_paise,
        )
        .join(Transaction, TransactionItem.transaction_id == Transaction.id)
        .join(Product, TransactionItem.product_id == Product.id)
        .where(
            Transaction.merchant_id == merchant_id,
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
            Transaction.is_return.is_(False),
        )
    )
    revenue: dict[str, int] = defaultdict(int)
    cost: dict[str, int] = defaultdict(int)
    units: dict[str, float] = defaultdict(float)
    lines: dict[str, int] = defaultdict(int)
    for row in session.execute(stmt).all():
        qty = float(row.qty)
        unit_cost = int(row.unit_cost_paise) or int(row.cost_price_paise)
        revenue[row.category] += int(round(qty * int(row.unit_price_paise)))
        cost[row.category] += int(round(qty * unit_cost))
        units[row.category] += qty
        lines[row.category] += 1
    return {
        category: CategoryMargin(
            category=category,
            revenue_paise=revenue[category],
            cost_paise=cost[category],
            units=round(units[category], 4),
            line_count=lines[category],
        )
        for category in revenue
    }


# ─────────────────────────────────────────────────────────────────────────────
# Customer frames
# ─────────────────────────────────────────────────────────────────────────────


def visit_histories(
    session: Session,
    merchant_id: str,
    *,
    as_of: datetime,
    lookback_days: int = 365,
) -> dict[str, VisitHistory]:
    """Per-customer purchase rhythm over the trailing ``lookback_days``.

    Two purchases on the same IST day count as **one visit** — otherwise a customer who splits a
    ration order across two bills looks like they have a zero-day cadence.
    """
    today = ist_date_of(as_of)
    start, end = range_bounds_ist(today - timedelta(days=lookback_days), today)
    names = dict(
        session.execute(
            select(Customer.id, Customer.name).where(Customer.merchant_id == merchant_id)
        ).all()
    )
    days: dict[str, set[date]] = defaultdict(set)
    amounts: dict[str, list[int]] = defaultdict(list)
    for row in _txn_rows(session, merchant_id, start, end):
        customer_id = row.customer_id
        if not customer_id:
            continue  # walk-in: no identity, no cadence
        days[customer_id].add(ist_date_of(row.occurred_at))
        amounts[customer_id].append(int(row.amount_paise))
    return {
        customer_id: VisitHistory(
            customer_id=customer_id,
            name=names.get(customer_id, customer_id),
            visit_days=sorted(days[customer_id]),
            amounts_paise=amounts[customer_id],
        )
        for customer_id in days
    }


#: R x FM segment grid (SPEC.md §7). Rows are the recency quintile 5→1 (5 = most recent), columns
#: the combined frequency/monetary quintile 1→5. The shape is the standard RFM grid: recent but
#: thin buyers are NEW, high-value buyers who have gone quiet are AT_RISK (worth spending a
#: win-back on), low-value quiet buyers are DORMANT then LOST (not worth the message).
RFM_SEGMENT_GRID: dict[int, tuple[CustomerSegment, ...]] = {
    5: (
        CustomerSegment.NEW,
        CustomerSegment.NEW,
        CustomerSegment.REGULAR,
        CustomerSegment.LOYAL,
        CustomerSegment.CHAMPION,
    ),
    4: (
        CustomerSegment.NEW,
        CustomerSegment.REGULAR,
        CustomerSegment.REGULAR,
        CustomerSegment.LOYAL,
        CustomerSegment.CHAMPION,
    ),
    3: (
        CustomerSegment.OCCASIONAL,
        CustomerSegment.REGULAR,
        CustomerSegment.REGULAR,
        CustomerSegment.LOYAL,
        CustomerSegment.LOYAL,
    ),
    2: (
        CustomerSegment.DORMANT,
        CustomerSegment.OCCASIONAL,
        CustomerSegment.AT_RISK,
        CustomerSegment.AT_RISK,
        CustomerSegment.AT_RISK,
    ),
    1: (
        CustomerSegment.LOST,
        CustomerSegment.LOST,
        CustomerSegment.DORMANT,
        CustomerSegment.AT_RISK,
        CustomerSegment.AT_RISK,
    ),
}


def segment_for(r: int, f: int, m: int, *, frequency: int) -> CustomerSegment:
    """Map an RFM cell to a :class:`CustomerSegment` via :data:`RFM_SEGMENT_GRID`.

    One override on top of the grid: a customer with a single recorded purchase is NEW when they
    are recent, regardless of how large that one basket was — one visit is not yet loyalty.
    """
    if frequency <= 1 and r >= 4:
        return CustomerSegment.NEW
    fm = int(round((f + m) / 2))
    row = RFM_SEGMENT_GRID[clamp(r, 1, 5)]
    return row[int(clamp(fm, 1, 5)) - 1]


def compute_rfm(ctx: InsightContext) -> dict[str, RFMScore]:
    """Quintile RFM scores for every customer with at least one purchase.

    Recency is inverted before ranking (fewer days since the last visit is *better*), so ``r=5``
    always means "came in recently" in the grid above. Cached on the context so the several
    engines that need segments pay for the scan once.
    """

    def build() -> dict[str, RFMScore]:
        today = ist_date_of(ctx.as_of)
        histories = visit_histories(ctx.session, ctx.merchant_id, as_of=ctx.as_of)
        if not histories:
            return {}
        recency = [float(history.days_since_last(today)) for history in histories.values()]
        frequency = [float(history.visit_count) for history in histories.values()]
        monetary = [float(history.total_paise) for history in histories.values()]
        r_cuts, f_cuts, m_cuts = quintiles(recency), quintiles(frequency), quintiles(monetary)

        scores: dict[str, RFMScore] = {}
        for customer_id, history in histories.items():
            days_since = history.days_since_last(today)
            r = 6 - quintile_rank(float(days_since), r_cuts)
            f = quintile_rank(float(history.visit_count), f_cuts)
            m = quintile_rank(float(history.total_paise), m_cuts)
            scores[customer_id] = RFMScore(
                customer_id=customer_id,
                name=history.name,
                recency_days=days_since,
                frequency=history.visit_count,
                monetary_paise=history.total_paise,
                avg_ticket_paise=history.avg_ticket_paise,
                r=r,
                f=f,
                m=m,
                segment=segment_for(r, f, m, frequency=history.visit_count),
                last_visit=history.last_visit,
            )
        return scores

    return ctx.cached("rfm", build)


def new_customers_per_week(
    session: Session,
    merchant_id: str,
    *,
    as_of: datetime,
    weeks: int = 8,
) -> list[int]:
    """First-time buyers in each of the trailing ``weeks`` complete 7-day windows, oldest first.

    "First time" is derived from the earliest transaction we hold for that customer, not from the
    denormalised ``Customer.first_seen_at``, so the series stays correct on partial fixtures.
    Windows end at the start of ``as_of``'s day — today is still in progress and would read as a
    fake collapse.
    """
    today = ist_date_of(as_of)
    stmt = (
        select(Transaction.customer_id, func.min(Transaction.occurred_at))
        .where(
            Transaction.merchant_id == merchant_id,
            Transaction.customer_id.is_not(None),
            Transaction.is_return.is_(False),
        )
        .group_by(Transaction.customer_id)
    )
    firsts = [ist_date_of(first) for _, first in session.execute(stmt).all() if first is not None]

    buckets: list[int] = []
    for step in range(weeks, 0, -1):
        window_end = today - timedelta(days=7 * (step - 1))
        window_start = today - timedelta(days=7 * step)
        buckets.append(sum(1 for day in firsts if window_start <= day < window_end))
    return buckets


# ─────────────────────────────────────────────────────────────────────────────
# Inventory frames
# ─────────────────────────────────────────────────────────────────────────────


def product_movements(
    session: Session,
    merchant_id: str,
    *,
    as_of: datetime,
    window_days: int = 60,
) -> dict[str, ProductMovement]:
    """Daily unit sales per SKU over the trailing ``window_days`` **complete** days.

    Today is excluded: a day that is only half over would drag every consumption rate down and
    make the 4 p.m. restock alert quieter than the 10 a.m. one. Days without a sale are filled
    with explicit zeros so the EWMA sees the gaps, which is the whole point of a decay-weighted
    rate. ``last_sale_day`` looks across *all* history, since dead stock is defined by an absence
    that can be far older than the window.
    """
    today = ist_date_of(as_of)
    last_day = today - timedelta(days=1)
    first_day = last_day - timedelta(days=window_days - 1)
    start, end = range_bounds_ist(first_day, last_day)

    products = {
        product.id: product
        for product in session.scalars(
            select(Product).where(Product.merchant_id == merchant_id)
        ).all()
    }
    if not products:
        return {}

    item_stmt = (
        select(
            TransactionItem.product_id,
            Transaction.occurred_at,
            TransactionItem.qty,
            TransactionItem.line_total_paise,
        )
        .join(Transaction, TransactionItem.transaction_id == Transaction.id)
        .where(
            Transaction.merchant_id == merchant_id,
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
            Transaction.is_return.is_(False),
        )
    )
    units: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    revenue: dict[str, int] = defaultdict(int)
    for row in session.execute(item_stmt).all():
        day = ist_date_of(row.occurred_at)
        units[row.product_id][day] += float(row.qty)
        revenue[row.product_id] += int(row.line_total_paise)

    last_stmt = (
        select(TransactionItem.product_id, func.max(Transaction.occurred_at))
        .join(Transaction, TransactionItem.transaction_id == Transaction.id)
        .where(
            Transaction.merchant_id == merchant_id,
            Transaction.is_return.is_(False),
        )
        .group_by(TransactionItem.product_id)
    )
    last_sale = {
        product_id: ist_date_of(moment)
        for product_id, moment in session.execute(last_stmt).all()
        if moment is not None
    }

    calendar = [first_day + timedelta(days=offset) for offset in range(window_days)]
    movements: dict[str, ProductMovement] = {}
    for product_id, product in products.items():
        per_day = units.get(product_id, {})
        series = tuple(per_day.get(day, 0.0) for day in calendar)
        movements[product_id] = ProductMovement(
            product=product,
            daily_units=series,
            window_days=window_days,
            units_sold=round(sum(series), 4),
            revenue_paise=revenue.get(product_id, 0),
            last_sale_day=last_sale.get(product_id),
        )
    return movements


# ─────────────────────────────────────────────────────────────────────────────
# Credit frames
# ─────────────────────────────────────────────────────────────────────────────

#: A settled entry with no explicit ``due_at`` is considered late past this many days.
IMPLIED_CREDIT_TERM_DAYS = 30


def khata_settle_stats(session: Session, merchant_id: str) -> dict[str, SettleStats]:
    """Per-customer settlement history over closed khata entries.

    Only ``SETTLED`` entries inform the score. ``WRITTEN_OFF`` entries are deliberately *not*
    counted as "settled late" — they are a merchant decision, not a customer behaviour, and
    folding them in would double-punish a customer the merchant has already forgiven.
    """
    stmt = select(KhataEntry).where(
        KhataEntry.merchant_id == merchant_id,
        KhataEntry.status == KhataStatus.SETTLED,
        KhataEntry.settled_at.is_not(None),
    )
    durations: dict[str, list[float]] = defaultdict(list)
    late: dict[str, list[int]] = defaultdict(list)
    for entry in session.scalars(stmt).all():
        opened = to_ist(entry.opened_at)
        settled = to_ist(entry.settled_at)  # type: ignore[arg-type]
        days = max(0.0, (settled - opened).total_seconds() / 86_400)
        durations[entry.customer_id].append(days)
        if entry.due_at is not None:
            late[entry.customer_id].append(1 if settled > to_ist(entry.due_at) else 0)
        else:
            late[entry.customer_id].append(1 if days > IMPLIED_CREDIT_TERM_DAYS else 0)

    return {
        customer_id: SettleStats(
            customer_id=customer_id,
            settled_count=len(values),
            avg_days_to_settle=round(sum(values) / len(values), 3),
            late_share=round(sum(late[customer_id]) / len(late[customer_id]), 4),
        )
        for customer_id, values in durations.items()
        if values
    }


def day_window(as_of: datetime) -> tuple[datetime, datetime]:
    """UTC ``[start, end)`` bounds of ``as_of``'s IST calendar day."""
    return day_bounds_ist(ist_date_of(as_of))
