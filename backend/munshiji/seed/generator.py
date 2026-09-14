"""The synthetic-history generator.

This is the ground truth the whole product reasons over, so it is built to be *statistically*
honest rather than merely plausible: day-of-week and salary-cycle seasonality, a bimodal intraday
curve, per-customer visit cadence, basket co-occurrence, a stock ledger driven by real consumption,
and a payment mix that drifts as the soundbox takes over.

The load-bearing part is the **planted signals**. Every insight the demo shows must be discoverable
from the data by an independent query — never asserted by a constant somewhere downstream. So the
generator deliberately creates them (customers who stop coming, SKUs that go quiet, a soft week)
and reports back exactly what it planted, with ids and numbers, in :class:`SeedResult`.

Determinism: one :class:`random.Random` is threaded through everything, identifiers are derived
from it, and every timestamp is computed from ``as_of`` — never from the wall clock. Same seed,
same bytes.

Money is int paise. Timestamps are built in IST and stored timezone-aware UTC.
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from random import Random
from time import perf_counter

from sqlalchemy.orm import Session

from munshiji.clock import IST, to_utc, today_ist
from munshiji.compliance import CONSENT_TAG, OPT_OUT_TAG
from munshiji.config import get_settings
from munshiji.db.enums import Channel, CustomerSegment, KhataStatus, PaymentMethod
from munshiji.db.models import (
    Customer,
    KhataEntry,
    Merchant,
    Product,
    Transaction,
    TransactionItem,
)
from munshiji.logging import get_logger
from munshiji.money import fmt_inr
from munshiji.seed import festivals
from munshiji.seed.catalog import CATALOG, CATEGORIES, CATEGORY_SHARES, CO_OCCURRENCE, CatalogItem
from munshiji.seed.profiles import (
    DEFAULT_PROFILE,
    FIRST_NAMES_FEMALE,
    FIRST_NAMES_MALE,
    SURNAMES,
    CohortSpec,
    SeedProfile,
)

__all__ = [
    "DOW_MULTIPLIER",
    "HOUR_WEIGHTS",
    "RECENT_WINDOW_DAYS",
    "PlantedCollectionDip",
    "PlantedDeadStock",
    "PlantedDormant",
    "PlantedExpiry",
    "PlantedMarginLeak",
    "PlantedStockout",
    "SeedProfile",
    "SeedResult",
    "current_cost_paise",
    "generate",
    "month_multiplier",
]

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Seasonality tables
# ─────────────────────────────────────────────────────────────────────────────

#: Day-of-week demand multipliers, Monday first. Saturday is the weekly grocery run; Tuesday is
#: the dead day in a North Indian neighbourhood market.
DOW_MULTIPLIER: tuple[float, ...] = (0.95, 0.85, 0.92, 0.98, 1.08, 1.35, 1.25)

#: Intraday shape, 07:00–21:00 IST. Bimodal: the morning milk-and-bread run, a dead afternoon,
#: then the heavy evening peak when the neighbourhood comes home. Weights sum to 1.0.
HOUR_WEIGHTS: tuple[tuple[int, float], ...] = (
    (7, 0.030),
    (8, 0.070),
    (9, 0.095),
    (10, 0.100),
    (11, 0.085),
    (12, 0.055),
    (13, 0.040),
    (14, 0.028),
    (15, 0.026),
    (16, 0.038),
    (17, 0.070),
    (18, 0.095),
    (19, 0.110),
    (20, 0.095),
    (21, 0.063),
)

#: Number of lines on a bill, 1 through 6. Mean ≈ 2.75 at a neutral day multiplier.
LINE_COUNT_WEIGHTS: tuple[float, ...] = (0.24, 0.27, 0.20, 0.14, 0.09, 0.06)

#: How sharply an overdue customer's chance of walking in rises. Higher = tighter cadence.
DUE_EXPONENT: float = 3.2

#: Cap on "overdue-ness" so a long-absent customer never becomes a certainty.
DUE_RATIO_CAP: float = 4.0

#: Trailing window used for the "current consumption rate" behind stockout and expiry cover.
RECENT_WINDOW_DAYS: int = 21

#: Channel mix: a kirana is overwhelmingly counter trade.
CHANNEL_WEIGHTS: tuple[tuple[Channel, float], ...] = (
    (Channel.SHOP, 0.93),
    (Channel.ONLINE, 0.05),
    (Channel.PHONE, 0.02),
)

#: Payment methods in the order their shares are laid out each day.
PAYMENT_ORDER: tuple[PaymentMethod, ...] = (
    PaymentMethod.UPI,
    PaymentMethod.SOUNDBOX,
    PaymentMethod.CASH,
    PaymentMethod.CARD,
    PaymentMethod.WALLET,
)

_ID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ID_EPOCH_MS = 1_760_000_000_000
_HOUR_VALUES = tuple(hour for hour, _ in HOUR_WEIGHTS)
_WALKIN_TICKET_BIAS = 0.78


def month_multiplier(day_of_month: int) -> float:
    """Salary-cycle multiplier: flush in the first week, tight at month end."""
    if day_of_month <= 7:
        return 1.20
    if day_of_month <= 15:
        return 1.02
    if day_of_month <= 25:
        return 0.97
    return 0.90


def current_cost_paise(item: CatalogItem, profile: SeedProfile) -> int:
    """What the merchant pays for ``item`` *today*, in paise.

    Background cost drift for everything, plus the planted step increase on the leak category.
    """
    value = item.cost_paise * (1.0 + profile.cost_drift)
    if item.category == profile.margin_leak_category:
        value *= profile.margin_leak_cost_uplift
    return int(round(value))


# ─────────────────────────────────────────────────────────────────────────────
# Result payloads — what the generator promises is true in the data
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class PlantedDormant:
    """A previously-regular customer who stopped coming."""

    customer_id: str
    name: str
    cohort: str
    visits: int
    median_gap_days: float
    last_seen_on: date
    days_since_last: int
    avg_ticket_paise: int


@dataclass(slots=True)
class PlantedDeadStock:
    """A SKU that has not sold in a long time, with real money sitting in it."""

    product_id: str
    sku: str
    name: str
    last_sold_on: date | None
    days_since_last_sale: int
    stock_qty: float
    capital_locked_paise: int


@dataclass(slots=True)
class PlantedStockout:
    """A fast mover about to run out."""

    product_id: str
    sku: str
    name: str
    stock_qty: float
    daily_rate: float
    days_of_cover: float


@dataclass(slots=True)
class PlantedExpiry:
    """A perishable stocked deeper than its remaining shelf life."""

    product_id: str
    sku: str
    name: str
    stock_qty: float
    daily_rate: float
    days_of_cover: float
    remaining_shelf_life_days: int


@dataclass(slots=True)
class PlantedMarginLeak:
    """A category whose cost rose while the counter price did not."""

    category: str
    window_days: int
    margin_pct_before: float
    margin_pct_after: float
    revenue_paise: int
    lost_margin_paise: int


@dataclass(slots=True)
class PlantedCollectionDip:
    """The soft week — the opening line of the live demo."""

    window_days: int
    start_day: date
    end_day: date
    observed_paise: int
    baseline_paise: int
    dip_pct: float


@dataclass(slots=True)
class SeedResult:
    """Counts plus every planted signal, so tests and the demo can assert against the data."""

    merchant_id: str
    profile_key: str
    seed: int
    as_of: date
    first_day: date
    days: int
    customers: int
    customers_without_visits: int
    products: int
    transactions: int
    transaction_items: int
    walkin_transactions: int
    gross_collection_paise: int
    khata_entries: int
    open_khata_entries: int
    open_khata_paise: int
    khata_over_60_days: int
    payment_mix: dict[str, float]
    upi_share_first_month: float
    upi_share_last_month: float
    dormant_customers: list[PlantedDormant] = field(default_factory=list)
    dead_stock: list[PlantedDeadStock] = field(default_factory=list)
    stockout_risks: list[PlantedStockout] = field(default_factory=list)
    expiry_risks: list[PlantedExpiry] = field(default_factory=list)
    margin_leak: PlantedMarginLeak | None = None
    collection_dip: PlantedCollectionDip | None = None
    elapsed_seconds: float = 0.0

    @property
    def avg_ticket_paise(self) -> int:
        """Mean bill value across the whole window, in paise."""
        if self.transactions == 0:
            return 0
        return self.gross_collection_paise // self.transactions

    def summary(self) -> str:
        """Human-readable digest — what ``print(result)`` shows."""
        mix = ", ".join(f"{k} {v * 100:.0f}%" for k, v in sorted(self.payment_mix.items()))
        stopped = [signal.days_since_last for signal in self.dormant_customers]
        dead_value = sum(signal.capital_locked_paise for signal in self.dead_stock)
        cover = ", ".join(f"{s.days_of_cover:.1f}d" for s in self.stockout_risks)
        shelf = ", ".join(
            f"{e.days_of_cover:.0f}d cover / {e.remaining_shelf_life_days}d left"
            for e in self.expiry_risks
        )
        lines = [
            f"SeedResult · {self.profile_key} · seed={self.seed}",
            f"  window        {self.first_day} → {self.as_of}  ({self.days} days)",
            f"  merchant      {self.merchant_id}",
            f"  customers     {self.customers}" f" ({self.customers_without_visits} never bought)",
            f"  products      {self.products}",
            f"  transactions  {self.transactions}"
            f" ({self.transactions / max(1, self.days):.1f}/day,"
            f" {self.walkin_transactions} walk-ins)",
            f"  items         {self.transaction_items}",
            f"  collection    {fmt_inr(self.gross_collection_paise)}"
            f"  (avg bill {fmt_inr(self.avg_ticket_paise)})",
            f"  payment mix   {mix}",
            f"  upi drift     {self.upi_share_first_month * 100:.0f}% →"
            f" {self.upi_share_last_month * 100:.0f}%",
            f"  khata         {self.khata_entries} entries,"
            f" {self.open_khata_entries} open ({fmt_inr(self.open_khata_paise)}),"
            f" {self.khata_over_60_days} over 60d",
            "  planted signals:",
            f"    dormant     {len(self.dormant_customers)} regulars stopped"
            f" {min(stopped, default=0)}–{max(stopped, default=0)} days ago",
            f"    dead stock  {len(self.dead_stock)} SKUs, {fmt_inr(dead_value)} locked",
            f"    stockout    {len(self.stockout_risks)} SKUs, cover {cover}",
            f"    expiry      {len(self.expiry_risks)} SKUs, {shelf}",
        ]
        dip = self.collection_dip
        if dip is not None:
            lines.append(
                f"    collection  last {dip.window_days}d {fmt_inr(dip.observed_paise)}"
                f" vs weekday baseline {fmt_inr(dip.baseline_paise)} ({dip.dip_pct:+.1f}%)"
            )
        leak = self.margin_leak
        if leak is not None:
            lines.append(
                f"    margin leak {leak.category}: {leak.margin_pct_before:.1f}% →"
                f" {leak.margin_pct_after:.1f}% over {leak.window_days}d"
                f" (−{fmt_inr(leak.lost_margin_paise)})"
            )
        lines.append(f"  generated in  {self.elapsed_seconds:.2f}s")
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - presentation
        return self.summary()


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────


def _encode_id(value: int, length: int) -> str:
    chars: list[str] = []
    for _ in range(length):
        value, remainder = divmod(value, 32)
        chars.append(_ID_ALPHABET[remainder])
    return "".join(reversed(chars))


class _IdFactory:
    """Deterministic stand-in for :func:`munshiji.ids.new_id`.

    Same shape — sortable prefix plus randomness — but seeded, because a seeded run has to be
    byte-identical and the production generator uses the wall clock plus ``secrets``.
    """

    __slots__ = ("_counter", "_rng")

    def __init__(self, rng: Random) -> None:
        self._rng = rng
        self._counter = 0

    def new(self, prefix: str) -> str:
        self._counter += 1
        head = _encode_id(_ID_EPOCH_MS + self._counter * 37, 10)
        tail = _encode_id(self._rng.getrandbits(40), 8)
        return f"{prefix}_{head}{tail}"


def _cumulative(weights: Iterable[float]) -> list[float]:
    running = 0.0
    out: list[float] = []
    for weight in weights:
        running += max(0.0, weight)
        out.append(running)
    return out


def _pick(rng: Random, cumulative: Sequence[float]) -> int:
    """Index sampled proportionally to a pre-built cumulative weight array; -1 if all zero."""
    total = cumulative[-1] if cumulative else 0.0
    if total <= 0.0:
        return -1
    return min(bisect_right(cumulative, rng.random() * total), len(cumulative) - 1)


def _ist(day: date, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute, second), tzinfo=IST)


def _median(values: Sequence[float]) -> float:
    return statistics.median(values) if values else 0.0


def _trailing_weekday_median(
    series: Sequence[float],
    days_list: Sequence[date],
    index: int,
    upto: int,
    weeks: int,
) -> float:
    """Median of the last ``weeks`` same-weekday values strictly before index ``upto``."""
    weekday = days_list[index].weekday()
    peers = [series[j] for j in range(upto) if days_list[j].weekday() == weekday]
    return _median(peers[-weeks:])


@dataclass(slots=True)
class _CustomerPlan:
    """Mutable per-customer state carried through the simulation."""

    id: str
    name: str
    phone: str
    cohort: CohortSpec
    mean_gap_days: float
    propensity: float
    ticket_bias: float
    active_from_ord: int
    active_until_ord: int
    scheduled: list[int]
    is_planted_dormant: bool
    last_visit_ord: int
    visits: list[tuple[int, datetime, int]] = field(default_factory=list)
    is_khata: bool = False

    @property
    def txn_count(self) -> int:
        return len(self.visits)

    @property
    def total_spend_paise(self) -> int:
        return sum(amount for _, _, amount in self.visits)


@dataclass(slots=True)
class _DayContext:
    """Per-day sampling tables, so the inner transaction loop stays cheap."""

    anchor_cum: list[float]
    follow_cum: dict[str, list[float]]
    product_idx: dict[str, list[int]]
    product_cum: dict[str, list[float]]
    costs: list[int]
    payment_cum: list[float]
    ticket_mult: float


@dataclass(slots=True)
class _Simulation:
    """Generated sales plus every rollup derived from them.

    The rollups are always *recomputed* from ``transactions`` and ``items`` rather than
    accumulated inline, so trimming bills (see :func:`_tighten_dip`) can never leave a total
    disagreeing with the rows.
    """

    transactions: list[Transaction]
    items: list[TransactionItem]
    #: ``(day_index, customer_id)`` aligned with ``transactions``.
    meta: list[tuple[int, str | None]]
    consumption: list[list[float]]
    last_sale_index: list[int]
    daily_collection: list[int]
    category_revenue: dict[str, list[int]]
    category_cost: dict[str, list[int]]
    payment_counts: dict[str, int]
    upi_by_day: list[int]
    txns_by_day: list[int]
    walkins: int


@dataclass(slots=True)
class _StockState:
    """Closing position of the stock ledger, per catalogue index."""

    closing: list[float]
    reorder: list[float]
    last_restock_index: list[int]
    #: Units per day over the trailing :data:`RECENT_WINDOW_DAYS` — what cover is sized against.
    rate_recent: list[float]


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────


def generate(
    session: Session,
    profile: SeedProfile | None = None,
    *,
    seed: int | None = None,
    days: int = 180,
    as_of: date | None = None,
) -> SeedResult:
    """Populate ``session`` with one merchant's synthetic history.

    Args:
        session: a session over an **empty** schema for this merchant. The caller owns the
            transaction: ``generate`` flushes in batches but never commits.
        profile: the shop's shape; defaults to Sharma General Store (SPEC.md §6).
        seed: RNG seed. Defaults to ``Settings.seed`` (20260919). Same seed ⇒ same bytes.
        days: length of the history in IST calendar days, ending on ``as_of`` inclusive.
            Floored at ``baseline_weeks × 7 + dip_days + 1`` so the planted dip stays measurable.
        as_of: last day of history (IST calendar date). Defaults to today IST.

    Returns:
        A :class:`SeedResult` with counts and every planted signal — ids and the values an
        independent query should find.
    """
    started = perf_counter()
    profile = profile or DEFAULT_PROFILE
    seed_value = int(seed if seed is not None else get_settings().seed)
    rng = Random(seed_value)
    ids = _IdFactory(rng)
    as_of = as_of or today_ist()
    floor_days = profile.baseline_weeks * 7 + profile.collection_dip_days + 1
    days = max(floor_days, int(days))
    first_day = as_of - timedelta(days=days - 1)
    day_list = [first_day + timedelta(days=offset) for offset in range(days)]

    items = list(CATALOG)
    merchant = _build_merchant(profile, ids, as_of)
    products, category_members = _build_products(merchant.id, items, ids, as_of, profile)
    dead_idx, blocked_from = _choose_dead_stock(rng, items, day_list, profile)
    customers = _build_customers(profile, rng, ids, day_list)
    dormant_schedule = _plan_dormant(rng, customers, day_list, profile)

    weights = _plan_day_weights(rng, day_list, profile)
    txn_counts = _plan_txn_counts(weights, profile)

    sim = _simulate_sales(
        merchant_id=merchant.id,
        rng=rng,
        ids=ids,
        profile=profile,
        items=items,
        products=products,
        category_members=category_members,
        blocked_from=blocked_from,
        customers=customers,
        dormant_schedule=dormant_schedule,
        day_list=day_list,
        weights=weights,
        txn_counts=txn_counts,
    )
    product_index = {product.id: index for index, product in enumerate(products)}
    customers_by_id = {plan.id: plan for plan in customers}
    _rebuild_totals(sim, items, product_index, customers_by_id, days)
    _tighten_dip(rng, sim, day_list, profile)
    _rebuild_totals(sim, items, product_index, customers_by_id, days)

    stock = _run_stock_ledger(rng, items, sim.consumption, day_list, blocked_from, profile)
    dead_signals = _apply_dead_stock(rng, items, products, stock, sim, day_list, dead_idx, profile)
    stockout_signals, expiry_signals = _apply_inventory_risks(
        rng, items, products, stock, day_list, dead_idx, profile
    )
    _write_stock_to_products(products, stock, day_list)

    khata_rows, khata_stats = _build_khata(merchant.id, rng, ids, customers, day_list, profile)
    dormant_signals = _describe_dormant(customers, day_list, as_of)
    dip_signal = _measure_dip(sim.daily_collection, day_list, profile)
    leak_signal = _measure_margin_leak(sim, day_list, profile)

    _persist(session, merchant, products, customers, sim, khata_rows)

    result = SeedResult(
        merchant_id=merchant.id,
        profile_key=profile.key,
        seed=seed_value,
        as_of=as_of,
        first_day=first_day,
        days=days,
        customers=len(customers),
        customers_without_visits=sum(1 for plan in customers if not plan.visits),
        products=len(products),
        transactions=len(sim.transactions),
        transaction_items=len(sim.items),
        walkin_transactions=sim.walkins,
        gross_collection_paise=sum(sim.daily_collection),
        khata_entries=len(khata_rows),
        open_khata_entries=khata_stats["open"],
        open_khata_paise=khata_stats["open_paise"],
        khata_over_60_days=khata_stats["over_60"],
        payment_mix=_payment_mix(sim.payment_counts, len(sim.transactions)),
        upi_share_first_month=_upi_share(sim, 0, 30),
        upi_share_last_month=_upi_share(sim, days - 30, days),
        dormant_customers=dormant_signals,
        dead_stock=dead_signals,
        stockout_risks=stockout_signals,
        expiry_risks=expiry_signals,
        margin_leak=leak_signal,
        collection_dip=dip_signal,
        elapsed_seconds=perf_counter() - started,
    )
    logger.info(
        "seeded %s: %d txns / %d items / %d customers in %.2fs",
        profile.shop_name,
        result.transactions,
        result.transaction_items,
        result.customers,
        result.elapsed_seconds,
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Merchant, products, customers
# ─────────────────────────────────────────────────────────────────────────────


def _build_merchant(profile: SeedProfile, ids: _IdFactory, as_of: date) -> Merchant:
    opened = to_utc(_ist(as_of - timedelta(days=profile.years_open * 365), 9, 0))
    return Merchant(
        id=ids.new("mer"),
        owner_name=profile.owner_name,
        shop_name=profile.shop_name,
        category=profile.category,
        city=profile.city,
        locality=profile.locality,
        language=profile.language,
        phone=profile.phone,
        soundbox_id=profile.soundbox_id,
        opened_at=opened,
        monthly_rent_paise=profile.monthly_rent_paise,
        business_hours_start=profile.business_hours_start,
        business_hours_end=profile.business_hours_end,
        created_at=opened,
    )


def _build_products(
    merchant_id: str,
    items: Sequence[CatalogItem],
    ids: _IdFactory,
    as_of: date,
    profile: SeedProfile,
) -> tuple[list[Product], dict[str, list[int]]]:
    """One ``Product`` per catalogue SKU. Stock is filled in later from the ledger."""
    created = to_utc(_ist(as_of - timedelta(days=profile.years_open * 365), 9, 0))
    products: list[Product] = []
    members: dict[str, list[int]] = {category: [] for category in CATEGORIES}
    for index, item in enumerate(items):
        products.append(
            Product(
                id=ids.new("prd"),
                merchant_id=merchant_id,
                sku=item.sku,
                name=item.name,
                name_hi=item.name_hi,
                category=item.category,
                unit=item.unit,
                cost_price_paise=current_cost_paise(item, profile),
                sell_price_paise=item.sell_paise,
                stock_qty=0.0,
                reorder_level=0.0,
                is_perishable=item.is_perishable,
                shelf_life_days=item.shelf_life_days,
                last_restocked_at=None,
                created_at=created,
            )
        )
        members[item.category].append(index)
    return products, members


def _build_customers(
    profile: SeedProfile,
    rng: Random,
    ids: _IdFactory,
    day_list: Sequence[date],
) -> list[_CustomerPlan]:
    """Name the customer book and give every person their own visit cadence."""
    first_ord = day_list[0].toordinal()
    last_ord = day_list[-1].toordinal()
    total = profile.customer_count

    cohort_slots: list[CohortSpec] = []
    for cohort in profile.cohorts:
        cohort_slots.extend([cohort] * round(cohort.share * total))
    while len(cohort_slots) < total:
        cohort_slots.append(profile.cohorts[-1])
    cohort_slots = cohort_slots[:total]
    rng.shuffle(cohort_slots)

    late_count = round(total * profile.late_acquisition_share)
    late_positions = set(rng.sample(range(total), late_count)) if late_count else set()

    plans: list[_CustomerPlan] = []
    used_names: set[str] = set()
    for position, cohort in enumerate(cohort_slots):
        name = ""
        for _ in range(40):
            pool = FIRST_NAMES_MALE if rng.random() < 0.55 else FIRST_NAMES_FEMALE
            name = f"{rng.choice(pool)} {rng.choice(SURNAMES)}"
            if name not in used_names:
                break
        used_names.add(name)

        mean_gap = max(
            2.0, rng.gauss(cohort.mean_gap_days, cohort.mean_gap_days * cohort.gap_jitter)
        )
        if position in late_positions:
            active_from_ord = rng.randint(first_ord + 5, last_ord - 3)
            scheduled = [active_from_ord]
        else:
            active_from_ord = first_ord
            scheduled = []

        plans.append(
            _CustomerPlan(
                id=ids.new("cus"),
                name=name,
                phone=f"+9198{rng.randint(10_000_000, 99_999_999)}",
                cohort=cohort,
                mean_gap_days=mean_gap,
                propensity=(1.0 / mean_gap) * rng.uniform(0.82, 1.18),
                ticket_bias=cohort.ticket_bias * rng.uniform(0.88, 1.14),
                active_from_ord=active_from_ord,
                active_until_ord=last_ord,
                scheduled=scheduled,
                is_planted_dormant=False,
                last_visit_ord=active_from_ord - round(mean_gap * rng.uniform(0.2, 1.2)) - 1,
            )
        )
    return plans


def _plan_dormant(
    rng: Random,
    customers: Sequence[_CustomerPlan],
    day_list: Sequence[date],
    profile: SeedProfile,
) -> dict[int, list[_CustomerPlan]]:
    """Pick the regulars who will stop coming, and hand-schedule their tight visit history.

    They leave the weighted draw entirely and get an explicit cadence, so the shape the dormancy
    engine needs — many visits, low gap variance, then silence — is guaranteed, not hoped for.
    """
    first_ord = day_list[0].toordinal()
    last_ord = day_list[-1].toordinal()
    eligible = [
        plan
        for plan in customers
        if plan.cohort.name in {"champion", "loyal", "regular"}
        and plan.active_from_ord == first_ord
    ]
    chosen = rng.sample(eligible, min(profile.dormant_count, len(eligible)))
    schedule: dict[int, list[_CustomerPlan]] = {}

    for plan in chosen:
        quiet_for = rng.randint(profile.dormant_stop_min_days, profile.dormant_stop_max_days)
        stop_ord = last_ord - quiet_for
        gap = rng.uniform(profile.dormant_gap_min, profile.dormant_gap_max)
        plan.is_planted_dormant = True
        plan.mean_gap_days = gap
        plan.active_until_ord = stop_ord
        plan.scheduled = []

        # Walk backwards from the day they stopped, so "days since last visit" is exactly the
        # quiet stretch we asked for rather than that minus a random partial gap.
        cursor = stop_ord
        while cursor >= first_ord:
            plan.scheduled.append(cursor)
            schedule.setdefault(cursor, []).append(plan)
            cursor -= max(2, round(rng.gauss(gap, gap * 0.16)))
        plan.scheduled.reverse()
    return schedule


# ─────────────────────────────────────────────────────────────────────────────
# Demand planning
# ─────────────────────────────────────────────────────────────────────────────


def _expected_units(weight: float, profile: SeedProfile) -> float:
    """Expected collection for a day of this weight, in arbitrary units.

    Bills and basket size each take a share of the day multiplier, and the bill count is clamped
    to the profile's daily band — so a very strong day collects less than its raw weight implies.
    The dip has to be calibrated against *this*, not against the weight.
    """
    raw = profile.base_txns_per_day * (weight**profile.count_exponent)
    count = min(profile.max_txns_per_day, max(profile.min_txns_per_day, round(raw)))
    return count * (weight ** (1.0 - profile.count_exponent))


def _weight_for_units(target: float, profile: SeedProfile) -> float:
    """Invert :func:`_expected_units` — it is monotone in weight, so bisect."""
    low, high = 0.02, 8.0
    for _ in range(48):
        mid = (low + high) / 2.0
        if _expected_units(mid, profile) < target:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def _plan_day_weights(rng: Random, day_list: Sequence[date], profile: SeedProfile) -> list[float]:
    """Per-day demand weight, then the planted soft week.

    The dip is *calibrated against the series itself*: each of the last N days is set to the
    median expected collection of its own weekday over the trailing baseline window, times a
    shortfall factor. The shortfall is therefore genuinely in the data and survives whatever
    festival happens to be running, instead of being a multiplier a festival uplift could
    quietly cancel out.
    """
    count = len(day_list)
    weights: list[float] = []
    for index, day in enumerate(day_list):
        progress = index / (count - 1) if count > 1 else 1.0
        weight = DOW_MULTIPLIER[day.weekday()]
        weight *= month_multiplier(day.day)
        weight *= festivals.day_uplift(day, CATEGORY_SHARES)
        weight *= profile.trend_start + (profile.trend_end - profile.trend_start) * progress
        weight *= math.exp(rng.gauss(0.0, profile.daily_noise_sigma))
        weights.append(weight)

    units = [_expected_units(weight, profile) for weight in weights]
    dip_ratio = rng.uniform(*profile.dip_ratio_range)
    dip_start = count - profile.collection_dip_days
    for index in range(dip_start, count):
        baseline = _trailing_weekday_median(
            units, day_list, index, dip_start, profile.baseline_weeks
        )
        if baseline > 0.0:
            target = baseline * dip_ratio * rng.uniform(0.975, 1.025)
            weights[index] = _weight_for_units(target, profile)
    return weights


def _plan_txn_counts(weights: Sequence[float], profile: SeedProfile) -> list[int]:
    """Bill counts. Only part of the day multiplier lands on count; the rest grows the basket."""
    counts: list[int] = []
    for weight in weights:
        raw = profile.base_txns_per_day * (weight**profile.count_exponent)
        counts.append(int(min(profile.max_txns_per_day, max(profile.min_txns_per_day, round(raw)))))
    return counts


# ─────────────────────────────────────────────────────────────────────────────
# Simulation
# ─────────────────────────────────────────────────────────────────────────────


def _simulate_sales(
    *,
    merchant_id: str,
    rng: Random,
    ids: _IdFactory,
    profile: SeedProfile,
    items: Sequence[CatalogItem],
    products: Sequence[Product],
    category_members: dict[str, list[int]],
    blocked_from: Sequence[int | None],
    customers: Sequence[_CustomerPlan],
    dormant_schedule: dict[int, list[_CustomerPlan]],
    day_list: Sequence[date],
    weights: Sequence[float],
    txn_counts: Sequence[int],
) -> _Simulation:
    """Walk the window day by day, bill by bill."""
    day_count = len(day_list)
    hour_cum = _cumulative(weight for _, weight in HOUR_WEIGHTS)
    channel_cum = _cumulative(weight for _, weight in CHANNEL_WEIGHTS)
    channels = [channel for channel, _ in CHANNEL_WEIGHTS]

    sim = _Simulation(
        transactions=[],
        items=[],
        meta=[],
        consumption=[],
        last_sale_index=[],
        daily_collection=[],
        category_revenue={},
        category_cost={},
        payment_counts={},
        upi_by_day=[],
        txns_by_day=[],
        walkins=0,
    )

    pool = [plan for plan in customers if not plan.is_planted_dormant]
    first_visits: dict[int, list[_CustomerPlan]] = {}
    for plan in pool:
        for day_ord in plan.scheduled:
            first_visits.setdefault(day_ord, []).append(plan)

    for index, day in enumerate(day_list):
        day_ord = day.toordinal()
        count = txn_counts[index]
        context = _day_context(
            index, day, day_count, items, category_members, blocked_from, profile, weights
        )

        forced = (list(dormant_schedule.get(day_ord, ())) + list(first_visits.get(day_ord, ())))[
            :count
        ]
        walkins = sum(1 for _ in range(count) if rng.random() < profile.walkin_share)
        named_target = max(len(forced), count - walkins)
        drawn = _draw_customers(rng, pool, day_ord, named_target - len(forced), forced)

        assigned: list[_CustomerPlan | None] = [*forced, *drawn]
        assigned.extend([None] * max(0, count - len(assigned)))
        assigned = assigned[:count]
        rng.shuffle(assigned)

        times = sorted(
            _ist(day, _HOUR_VALUES[_pick(rng, hour_cum)], rng.randrange(60), rng.randrange(60))
            for _ in range(count)
        )

        for moment, plan in zip(times, assigned, strict=True):
            bias = plan.ticket_bias if plan is not None else _WALKIN_TICKET_BIAS
            lines = _build_basket(rng, context, items, bias)
            if not lines:
                continue
            txn_id = ids.new("txn")
            amount = 0
            for item_index, qty in lines:
                item = items[item_index]
                unit_price = item.sell_paise
                unit_cost = context.costs[item_index]
                line_total = int(round(qty * unit_price))
                amount += line_total
                sim.items.append(
                    TransactionItem(
                        id=ids.new("tif"),
                        transaction_id=txn_id,
                        product_id=products[item_index].id,
                        qty=qty,
                        unit_price_paise=unit_price,
                        unit_cost_paise=unit_cost,
                        line_total_paise=line_total,
                    )
                )

            stored_at = to_utc(moment)
            sim.transactions.append(
                Transaction(
                    id=txn_id,
                    merchant_id=merchant_id,
                    customer_id=plan.id if plan is not None else None,
                    amount_paise=amount,
                    occurred_at=stored_at,
                    payment_method=PAYMENT_ORDER[_pick(rng, context.payment_cum)],
                    channel=channels[_pick(rng, channel_cum)],
                    is_return=False,
                    created_at=stored_at,
                )
            )
            sim.meta.append((index, plan.id if plan is not None else None))
            if plan is not None:
                plan.last_visit_ord = day_ord
    return sim


def _rebuild_totals(
    sim: _Simulation,
    items: Sequence[CatalogItem],
    product_index: dict[str, int],
    customers_by_id: dict[str, _CustomerPlan],
    day_count: int,
) -> None:
    """Recompute every rollup from the surviving rows. Idempotent; safe to run twice."""
    sim.daily_collection = [0] * day_count
    sim.txns_by_day = [0] * day_count
    sim.upi_by_day = [0] * day_count
    sim.payment_counts = {method.value: 0 for method in PaymentMethod}
    sim.consumption = [[0.0] * day_count for _ in items]
    sim.last_sale_index = [-1] * len(items)
    sim.category_revenue = {category: [0] * day_count for category in CATEGORIES}
    sim.category_cost = {category: [0] * day_count for category in CATEGORIES}
    sim.walkins = 0
    for plan in customers_by_id.values():
        plan.visits.clear()

    day_of_txn: dict[str, int] = {}
    for txn, (day_index, customer_id) in zip(sim.transactions, sim.meta, strict=True):
        day_of_txn[txn.id] = day_index
        sim.daily_collection[day_index] += txn.amount_paise
        sim.txns_by_day[day_index] += 1
        sim.payment_counts[txn.payment_method.value] += 1
        if txn.payment_method is PaymentMethod.UPI:
            sim.upi_by_day[day_index] += 1
        if customer_id is None:
            sim.walkins += 1
        else:
            customers_by_id[customer_id].visits.append(
                (day_index, txn.occurred_at, txn.amount_paise)
            )

    for line in sim.items:
        day_index = day_of_txn[line.transaction_id]
        index = product_index[line.product_id]
        category = items[index].category
        sim.consumption[index][day_index] += line.qty
        if day_index > sim.last_sale_index[index]:
            sim.last_sale_index[index] = day_index
        sim.category_revenue[category][day_index] += line.line_total_paise
        sim.category_cost[category][day_index] += int(round(line.qty * line.unit_cost_paise))

    for plan in customers_by_id.values():
        plan.visits.sort(key=lambda visit: visit[1])


def _tighten_dip(
    rng: Random,
    sim: _Simulation,
    day_list: Sequence[date],
    profile: SeedProfile,
) -> None:
    """Trim bills from the soft week until the *observed* shortfall hits the target band.

    The weight-level dip already softens the last few days, but a day's collection carries
    roughly 14% sampling noise, so the shortfall an analyst actually measures wanders by several
    points. The demo opens on this number, so it is pinned: bills are dropped whole (never
    edited), which keeps ``amount_paise == Σ line_total_paise`` intact and simply means those
    customers did not come in that week.
    """
    count = len(day_list)
    start = count - profile.collection_dip_days
    if start <= 0:
        return
    baseline = sum(
        _trailing_weekday_median(
            sim.daily_collection, day_list, index, start, profile.baseline_weeks
        )
        for index in range(start, count)
    )
    if baseline <= 0:
        return

    target = baseline * (1.0 - rng.uniform(profile.observed_dip_min, profile.observed_dip_max))
    surplus = sum(sim.daily_collection[start:]) - target
    if surplus <= 0:
        return  # already at or below target; never invent sales to prop it back up

    room = [max(0, sim.txns_by_day[index] - profile.min_txns_per_day) for index in range(count)]
    candidates = [index for index, (day_index, _) in enumerate(sim.meta) if day_index >= start]
    rng.shuffle(candidates)

    dropped: set[int] = set()
    for position in candidates:
        if surplus <= 0:
            break
        day_index, _ = sim.meta[position]
        amount = sim.transactions[position].amount_paise
        if room[day_index] <= 0 or amount > surplus:
            continue
        dropped.add(position)
        room[day_index] -= 1
        surplus -= amount
    if not dropped:
        return

    dropped_ids = {sim.transactions[position].id for position in dropped}
    sim.transactions = [
        txn for position, txn in enumerate(sim.transactions) if position not in dropped
    ]
    sim.meta = [entry for position, entry in enumerate(sim.meta) if position not in dropped]
    sim.items = [line for line in sim.items if line.transaction_id not in dropped_ids]


def _day_context(
    index: int,
    day: date,
    day_count: int,
    items: Sequence[CatalogItem],
    category_members: dict[str, list[int]],
    blocked_from: Sequence[int | None],
    profile: SeedProfile,
    weights: Sequence[float],
) -> _DayContext:
    """Festival-tilted category and product weights, today's costs, today's payment mix."""
    uplift = {category: festivals.uplift_for(day, category) for category in CATEGORIES}
    festive = any(value > 1.0 for value in uplift.values())

    product_idx: dict[str, list[int]] = {}
    product_cum: dict[str, list[float]] = {}
    available: dict[str, bool] = {}
    for category in CATEGORIES:
        idxs: list[int] = []
        category_weights: list[float] = []
        for item_index in category_members[category]:
            blocked = blocked_from[item_index]
            if blocked is not None and index >= blocked:
                continue
            weight = items[item_index].popularity
            if festive and uplift[category] > 1.0:
                weight *= items[item_index].festival_affinity
            idxs.append(item_index)
            category_weights.append(weight)
        product_idx[category] = idxs
        product_cum[category] = _cumulative(category_weights)
        available[category] = bool(idxs)

    anchor_cum = _cumulative(
        CATEGORY_SHARES[category] * uplift[category] if available[category] else 0.0
        for category in CATEGORIES
    )
    follow_cum = {
        anchor: _cumulative(
            CO_OCCURRENCE[anchor][category] * uplift[category] if available[category] else 0.0
            for category in CATEGORIES
        )
        for anchor in CATEGORIES
    }

    progress = index / (day_count - 1) if day_count > 1 else 1.0
    drift = 1.0 + profile.cost_drift * progress
    leak_from = day_count - profile.margin_leak_days
    costs: list[int] = []
    for item in items:
        value = item.cost_paise * drift
        if item.category == profile.margin_leak_category and index >= leak_from:
            value *= profile.margin_leak_cost_uplift
        costs.append(int(round(value)))

    upi = profile.upi_share_start + (profile.upi_share_end - profile.upi_share_start) * progress
    soundbox = (
        profile.soundbox_share_start
        + (profile.soundbox_share_end - profile.soundbox_share_start) * progress
    )
    remainder = max(0.0, 1.0 - upi - soundbox)
    wallet_share = max(0.0, 1.0 - profile.cash_of_remainder - profile.card_of_remainder)
    payment_cum = _cumulative(
        (
            upi,
            soundbox,
            remainder * profile.cash_of_remainder,
            remainder * profile.card_of_remainder,
            remainder * wallet_share,
        )
    )

    return _DayContext(
        anchor_cum=anchor_cum,
        follow_cum=follow_cum,
        product_idx=product_idx,
        product_cum=product_cum,
        costs=costs,
        payment_cum=payment_cum,
        ticket_mult=weights[index] ** (1.0 - profile.count_exponent),
    )


def _draw_customers(
    rng: Random,
    pool: Sequence[_CustomerPlan],
    day_ord: int,
    wanted: int,
    already: Sequence[_CustomerPlan],
) -> list[_CustomerPlan]:
    """Pick today's named buyers, weighted by how overdue each one's personal cadence is.

    ``weight ∝ propensity × (days_since_last / mean_gap) ** DUE_EXPONENT`` concentrates each
    customer's gaps around their own cadence, which is what makes a per-customer dormancy test
    (median gap + 1.5·IQR) meaningful rather than a blanket 30-day rule.
    """
    if wanted <= 0:
        return []
    candidates: list[_CustomerPlan] = []
    weights: list[float] = []
    for plan in pool:
        if day_ord < plan.active_from_ord or day_ord > plan.active_until_ord:
            continue
        gap = day_ord - plan.last_visit_ord
        if gap <= 0:
            continue
        ratio = min(DUE_RATIO_CAP, gap / plan.mean_gap_days)
        candidates.append(plan)
        weights.append(plan.propensity * ratio**DUE_EXPONENT)

    cumulative = _cumulative(weights)
    taken = {id(plan) for plan in already}
    picked: list[_CustomerPlan] = []
    attempts = 0
    limit = wanted * 8 + 24
    while len(picked) < wanted and attempts < limit:
        attempts += 1
        index = _pick(rng, cumulative)
        if index < 0:
            break
        plan = candidates[index]
        if id(plan) in taken:
            continue
        taken.add(id(plan))
        picked.append(plan)
    return picked


def _build_basket(
    rng: Random,
    context: _DayContext,
    items: Sequence[CatalogItem],
    ticket_bias: float,
) -> list[tuple[int, float]]:
    """1–6 lines with category-appropriate co-occurrence, sized by the day and by the buyer."""
    tilt = context.ticket_mult * ticket_bias
    line_tilt = min(1.5, max(0.75, tilt))
    line_cum = _cumulative(
        weight * (line_tilt**position) for position, weight in enumerate(LINE_COUNT_WEIGHTS)
    )
    line_count = _pick(rng, line_cum) + 1

    anchor_index = _pick(rng, context.anchor_cum)
    if anchor_index < 0:
        return []
    anchor = CATEGORIES[anchor_index]
    follow_cum = context.follow_cum[anchor]

    chosen: list[tuple[int, float]] = []
    seen: set[int] = set()
    for position in range(line_count):
        category = anchor
        if position:
            next_index = _pick(rng, follow_cum)
            if next_index < 0:
                break
            category = CATEGORIES[next_index]
        idxs = context.product_idx[category]
        if not idxs:
            continue
        item_index = -1
        for _ in range(4):
            candidate = idxs[_pick(rng, context.product_cum[category])]
            if candidate not in seen:
                item_index = candidate
                break
        if item_index < 0:
            continue
        seen.add(item_index)
        chosen.append((item_index, _pick_qty(rng, items[item_index], tilt)))
    return chosen


def _pick_qty(rng: Random, item: CatalogItem, tilt: float) -> float:
    """A sensible quantity for this SKU, nudged by how flush the day and the buyer are."""
    qty = rng.choice(item.qty_choices)
    smallest = min(item.qty_choices)
    nudge = max(-0.45, min(0.45, (tilt - 1.0) * 1.1))
    if nudge > 0 and rng.random() < nudge:
        qty += 1.0
    elif nudge < 0 and qty > smallest and rng.random() < -nudge:
        qty = max(smallest, qty - 1.0)
    return round(qty, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Customer aggregates
# ─────────────────────────────────────────────────────────────────────────────


def _segment_for(plan: _CustomerPlan) -> CustomerSegment | None:
    """Frequency/monetary segment derived from what the customer *actually* bought.

    Deliberately recency-free: AT_RISK / DORMANT / LOST are verdicts for the insight engine to
    reach on its own, not facts the seed hands it.
    """
    visits = plan.txn_count
    if visits == 0:
        return None
    if visits <= 2:
        return CustomerSegment.NEW
    if visits >= 35 and plan.total_spend_paise >= 1_500_000:
        return CustomerSegment.CHAMPION
    if visits >= 22:
        return CustomerSegment.LOYAL
    if visits >= 11:
        return CustomerSegment.REGULAR
    return CustomerSegment.OCCASIONAL


def _describe_dormant(
    customers: Sequence[_CustomerPlan], day_list: Sequence[date], as_of: date
) -> list[PlantedDormant]:
    signals: list[PlantedDormant] = []
    for plan in customers:
        if not plan.is_planted_dormant or not plan.visits:
            continue
        day_indices = [index for index, _, _ in plan.visits]
        gaps = [later - earlier for earlier, later in pairwise(day_indices)]
        last_day = day_list[day_indices[-1]]
        signals.append(
            PlantedDormant(
                customer_id=plan.id,
                name=plan.name,
                cohort=plan.cohort.name,
                visits=len(plan.visits),
                median_gap_days=round(_median(gaps), 2),
                last_seen_on=last_day,
                days_since_last=(as_of - last_day).days,
                avg_ticket_paise=plan.total_spend_paise // len(plan.visits),
            )
        )
    signals.sort(key=lambda signal: signal.days_since_last, reverse=True)
    return signals


# ─────────────────────────────────────────────────────────────────────────────
# Inventory
# ─────────────────────────────────────────────────────────────────────────────


def _choose_dead_stock(
    rng: Random,
    items: Sequence[CatalogItem],
    day_list: Sequence[date],
    profile: SeedProfile,
) -> tuple[list[int], list[int | None]]:
    """Pick slow, high-value, non-perishable SKUs and switch their sales off mid-window."""
    day_count = len(day_list)
    blocked: list[int | None] = [None] * len(items)
    candidates = [
        index
        for index, item in enumerate(items)
        if not item.is_perishable and item.popularity <= 0.60 and item.cost_paise >= 6_000
    ]
    chosen = rng.sample(candidates, min(profile.dead_stock_count, len(candidates)))
    for index in chosen:
        quiet = rng.randint(profile.dead_stock_min_quiet_days, profile.dead_stock_max_quiet_days)
        blocked[index] = max(10, day_count - quiet)
    return chosen, blocked


def _run_stock_ledger(
    rng: Random,
    items: Sequence[CatalogItem],
    consumption: Sequence[Sequence[float]],
    day_list: Sequence[date],
    blocked_from: Sequence[int | None],
    profile: SeedProfile,
) -> _StockState:
    """Replay purchases against actual sales, so closing stock is something you could defend."""
    day_count = len(day_list)
    state = _StockState(
        closing=[0.0] * len(items),
        reorder=[0.0] * len(items),
        last_restock_index=[0] * len(items),
        rate_recent=[0.0] * len(items),
    )
    for index in range(len(items)):
        series = consumption[index]
        blocked = blocked_from[index]
        sellable_until = blocked if blocked is not None else day_count
        rate = sum(series) / max(1, sellable_until)
        recent_from = max(0, sellable_until - RECENT_WINDOW_DAYS)
        recent_days = max(1, sellable_until - recent_from)
        state.rate_recent[index] = sum(series[recent_from:sellable_until]) / recent_days

        reorder = round(max(2.0, rate * profile.reorder_cover_days), 1)
        stock = round(max(rate * profile.restock_cover_max_days, reorder * 2.0), 1)
        last_restock = 0
        for day_index in range(day_count):
            if day_index < sellable_until and stock < reorder:
                cover = rng.uniform(profile.restock_cover_min_days, profile.restock_cover_max_days)
                stock += round(max(rate * cover, reorder * 2.0), 1)
                last_restock = day_index
            stock = max(0.0, round(stock - series[day_index], 2))
        state.closing[index] = stock
        state.reorder[index] = reorder
        state.last_restock_index[index] = last_restock
    return state


def _apply_dead_stock(
    rng: Random,
    items: Sequence[CatalogItem],
    products: Sequence[Product],
    stock: _StockState,
    sim: _Simulation,
    day_list: Sequence[date],
    dead_idx: Sequence[int],
    profile: SeedProfile,
) -> list[PlantedDeadStock]:
    """Leave a defensible amount of capital sitting in the SKUs that went quiet."""
    as_of = day_list[-1]
    signals: list[PlantedDeadStock] = []
    for index in dead_idx:
        item = items[index]
        target_value = rng.randint(
            profile.dead_stock_value_min_paise, profile.dead_stock_value_max_paise
        )
        qty = max(1.0, round(target_value / item.cost_paise, 1))
        stock.closing[index] = qty
        last_index = sim.last_sale_index[index]
        last_day = day_list[last_index] if last_index >= 0 else None
        signals.append(
            PlantedDeadStock(
                product_id=products[index].id,
                sku=item.sku,
                name=item.name,
                last_sold_on=last_day,
                days_since_last_sale=(as_of - last_day).days if last_day else len(day_list),
                stock_qty=qty,
                capital_locked_paise=int(round(qty * products[index].cost_price_paise)),
            )
        )
    signals.sort(key=lambda signal: signal.capital_locked_paise, reverse=True)
    return signals


def _apply_inventory_risks(
    rng: Random,
    items: Sequence[CatalogItem],
    products: Sequence[Product],
    stock: _StockState,
    day_list: Sequence[date],
    dead_idx: Sequence[int],
    profile: SeedProfile,
) -> tuple[list[PlantedStockout], list[PlantedExpiry]]:
    """Draw the fast movers down to the wire, and over-buy a couple of perishables."""
    excluded = set(dead_idx)
    day_count = len(day_list)

    expiry_candidates = [
        index
        for index, item in enumerate(items)
        if index not in excluded
        and item.is_perishable
        and item.shelf_life_days is not None
        and 20 <= item.shelf_life_days <= 90
        and stock.rate_recent[index] > 0.15
    ]
    expiry_candidates.sort(key=lambda index: stock.rate_recent[index], reverse=True)
    expiry_chosen = expiry_candidates[: profile.expiry_count]
    excluded.update(expiry_chosen)

    stockout_candidates = [
        index
        for index in range(len(items))
        if index not in excluded and stock.rate_recent[index] >= 1.0
    ]
    stockout_candidates.sort(key=lambda index: stock.rate_recent[index], reverse=True)

    stockouts: list[PlantedStockout] = []
    for index in stockout_candidates[: profile.stockout_count]:
        rate = stock.rate_recent[index]
        cover = rng.uniform(profile.stockout_cover_min_days, profile.stockout_cover_max_days)
        qty = max(0.5, round(rate * cover, 1))
        stock.closing[index] = qty
        stock.last_restock_index[index] = max(0, day_count - rng.randint(4, 9))
        stockouts.append(
            PlantedStockout(
                product_id=products[index].id,
                sku=items[index].sku,
                name=items[index].name,
                stock_qty=qty,
                daily_rate=round(rate, 3),
                days_of_cover=round(qty / rate, 2),
            )
        )

    expiries: list[PlantedExpiry] = []
    for index in expiry_chosen:
        item = items[index]
        shelf_life = int(item.shelf_life_days or 30)
        remaining = rng.randint(
            profile.expiry_remaining_shelf_min, profile.expiry_remaining_shelf_max
        )
        rate = stock.rate_recent[index]
        cover = rng.uniform(profile.expiry_cover_min_days, profile.expiry_cover_max_days)
        qty = max(1.0, round(rate * cover, 1))
        stock.closing[index] = qty
        stock.last_restock_index[index] = max(0, day_count - 1 - (shelf_life - remaining))
        expiries.append(
            PlantedExpiry(
                product_id=products[index].id,
                sku=item.sku,
                name=item.name,
                stock_qty=qty,
                daily_rate=round(rate, 3),
                days_of_cover=round(qty / rate, 2),
                remaining_shelf_life_days=remaining,
            )
        )
    return stockouts, expiries


def _write_stock_to_products(
    products: Sequence[Product], stock: _StockState, day_list: Sequence[date]
) -> None:
    last_index = len(day_list) - 1
    for index, product in enumerate(products):
        product.stock_qty = round(max(0.0, stock.closing[index]), 2)
        product.reorder_level = stock.reorder[index]
        restock_day = day_list[min(stock.last_restock_index[index], last_index)]
        product.last_restocked_at = to_utc(_ist(restock_day, 8, 30))


# ─────────────────────────────────────────────────────────────────────────────
# Khata (udhaar)
# ─────────────────────────────────────────────────────────────────────────────


def _build_khata(
    merchant_id: str,
    rng: Random,
    ids: _IdFactory,
    customers: Sequence[_CustomerPlan],
    day_list: Sequence[date],
    profile: SeedProfile,
) -> tuple[list[KhataEntry], dict[str, int]]:
    """Open and settled udhaar, anchored on real bills so the ledger ties back to the sales."""
    day_count = len(day_list)
    as_of = day_list[-1]
    eligible = [plan for plan in customers if len(plan.visits) >= 6]
    ranked = sorted(eligible, key=lambda plan: (-plan.cohort.khata_propensity, plan.id))
    book = ranked[: max(profile.open_khata_count + 16, 30)]
    rng.shuffle(book)

    buckets: list[tuple[int, int]] = [(62, 95)] * profile.khata_over_60_count
    spread = ((31, 58), (16, 30), (2, 15))
    for position in range(profile.open_khata_count - profile.khata_over_60_count):
        buckets.append(spread[position % len(spread)])
    partial_count = min(profile.khata_partial_count, len(buckets))
    partial_slots = set(rng.sample(range(len(buckets)), partial_count))

    rows: list[KhataEntry] = []
    open_count = 0
    over_60 = 0
    open_paise = 0

    for slot, (plan, (low, high)) in enumerate(zip(book, buckets, strict=False)):
        plan.is_khata = True
        anchor = _visit_in_age_window(plan, day_count, low, high)
        if anchor is None:
            opened_day = day_list[max(0, day_count - 1 - rng.randint(low, high))]
            opened_at = to_utc(_ist(opened_day, rng.randint(17, 20), rng.randrange(60)))
            amount = rng.randint(30_000, 450_000)
        else:
            opened_at, amount = _tab_total(plan, anchor)
        amount = max(20_000, amount)
        paid = int(amount * rng.uniform(0.2, 0.6)) if slot in partial_slots else 0
        age_days = (as_of - opened_at.astimezone(IST).date()).days
        reminders = 0 if age_days < 20 else rng.randint(1, 3)
        rows.append(
            KhataEntry(
                id=ids.new("kht"),
                merchant_id=merchant_id,
                customer_id=plan.id,
                amount_paise=amount,
                paid_paise=paid,
                opened_at=opened_at,
                due_at=opened_at + timedelta(days=profile.khata_due_days),
                settled_at=None,
                status=KhataStatus.PARTIAL if paid else KhataStatus.OPEN,
                reminders_sent=reminders,
                last_reminder_at=(
                    opened_at + timedelta(days=rng.randint(16, 30)) if reminders else None
                ),
                note="उधार — काउंटर पर",
                created_at=opened_at,
            )
        )
        open_count += 1
        open_paise += amount - paid
        if age_days >= 60:
            over_60 += 1

    # Settled history — what a credit-reliability score is actually computed from.
    for _ in range(profile.settled_khata_count):
        plan = book[rng.randrange(len(book))]
        anchor = _visit_in_age_window(plan, day_count, 35, day_count - 5)
        if anchor is None:
            continue
        opened_at, amount = _tab_total(plan, anchor)
        settled_at = opened_at + timedelta(days=rng.randint(4, 26), hours=rng.randint(0, 9))
        if settled_at.astimezone(IST).date() > as_of:
            continue
        plan.is_khata = True
        amount = max(20_000, amount)
        rows.append(
            KhataEntry(
                id=ids.new("kht"),
                merchant_id=merchant_id,
                customer_id=plan.id,
                amount_paise=amount,
                paid_paise=amount,
                opened_at=opened_at,
                due_at=opened_at + timedelta(days=profile.khata_due_days),
                settled_at=settled_at,
                status=KhataStatus.SETTLED,
                reminders_sent=0 if (settled_at - opened_at).days <= 15 else 1,
                last_reminder_at=None,
                note="उधार चुकता",
                created_at=opened_at,
            )
        )
    return rows, {"open": open_count, "over_60": over_60, "open_paise": open_paise}


def _visit_in_age_window(
    plan: _CustomerPlan, day_count: int, low_days: int, high_days: int
) -> tuple[int, datetime, int] | None:
    """A bill of this customer whose age falls inside ``[low_days, high_days]``."""
    high_index = day_count - 1 - low_days
    low_index = day_count - 1 - high_days
    candidates = [visit for visit in plan.visits if low_index <= visit[0] <= high_index]
    return candidates[len(candidates) // 2] if candidates else None


#: How many days of shopping a khata entry rolls up, either side of the anchor bill.
#:
#: A tab is not a few days of shopping — a regular buys on credit through the month and settles
#: when the salary lands. A narrow window produced a book worth ~4% of monthly revenue, which
#: made udhaar look like a rounding error; widening it puts the ledger where a real kirana's
#: sits, in the low-to-mid teens as a share of turnover.
TAB_WINDOW_DAYS: int = 10

#: Most bills a single khata entry rolls up.
TAB_MAX_BILLS: int = 9


def _tab_total(plan: _CustomerPlan, anchor: tuple[int, datetime, int]) -> tuple[datetime, int]:
    """Roll the anchor bill and its neighbours into one entry.

    A khata line is not one bill — it is the week's shopping written down together and settled
    later. Rolling up the surrounding bills keeps the entry tied to real sales while landing the
    amounts where real udhaar sits (a few hundred to a few thousand rupees).
    """
    index, opened_at, _ = anchor
    window = [visit for visit in plan.visits if abs(visit[0] - index) <= TAB_WINDOW_DAYS][
        :TAB_MAX_BILLS
    ]
    if not window:
        return opened_at, anchor[2]
    # Dated from the *first* bill on the tab, so an entry never ages backwards out of its bucket.
    return window[0][1], sum(amount for _, _, amount in window)


# ─────────────────────────────────────────────────────────────────────────────
# Measuring the planted signals
# ─────────────────────────────────────────────────────────────────────────────


def _measure_dip(
    daily: Sequence[int], day_list: Sequence[date], profile: SeedProfile
) -> PlantedCollectionDip | None:
    """Measure the soft week the way an analyst would: same weekday, trailing median."""
    count = len(daily)
    start = count - profile.collection_dip_days
    if start <= 0:
        return None
    observed = sum(daily[start:])
    baseline = sum(
        _trailing_weekday_median(daily, day_list, index, start, profile.baseline_weeks)
        for index in range(start, count)
    )
    if baseline <= 0:
        return None
    return PlantedCollectionDip(
        window_days=profile.collection_dip_days,
        start_day=day_list[start],
        end_day=day_list[-1],
        observed_paise=observed,
        baseline_paise=int(round(baseline)),
        dip_pct=round((observed - baseline) / baseline * 100.0, 2),
    )


def _measure_margin_leak(
    sim: _Simulation, day_list: Sequence[date], profile: SeedProfile
) -> PlantedMarginLeak | None:
    """Gross margin on the leak category after the cost step vs the matching window before it."""
    category = profile.margin_leak_category
    revenue = sim.category_revenue.get(category)
    cost = sim.category_cost.get(category)
    if revenue is None or cost is None:
        return None
    count = len(day_list)
    after_start = count - profile.margin_leak_days
    before_start = max(0, after_start - profile.margin_leak_days)
    if after_start <= before_start:
        return None

    def margin_pct(low: int, high: int) -> tuple[float, int]:
        earned = sum(revenue[low:high])
        spent = sum(cost[low:high])
        return (0.0 if earned == 0 else (earned - spent) / earned * 100.0), earned

    before_pct, _ = margin_pct(before_start, after_start)
    after_pct, after_revenue = margin_pct(after_start, count)
    lost = int(round(after_revenue * (before_pct - after_pct) / 100.0))
    return PlantedMarginLeak(
        category=category,
        window_days=profile.margin_leak_days,
        margin_pct_before=round(before_pct, 2),
        margin_pct_after=round(after_pct, 2),
        revenue_paise=after_revenue,
        lost_margin_paise=max(0, lost),
    )


def _payment_mix(counts: dict[str, int], total: int) -> dict[str, float]:
    if total <= 0:
        return dict.fromkeys(counts, 0.0)
    return {method: round(value / total, 4) for method, value in counts.items()}


def _upi_share(sim: _Simulation, low: int, high: int) -> float:
    """Share of bills settled on UPI across day range ``[low, high)`` (soundbox counted apart)."""
    low = max(0, low)
    total = sum(sim.txns_by_day[low:high])
    if total <= 0:
        return 0.0
    return round(sum(sim.upi_by_day[low:high]) / total, 4)


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────

#: Rows per bulk flush. Large enough to amortise, small enough to keep memory flat.
BATCH_SIZE: int = 5_000


def _persist(
    session: Session,
    merchant: Merchant,
    products: Sequence[Product],
    customers: Sequence[_CustomerPlan],
    sim: _Simulation,
    khata_rows: Sequence[KhataEntry],
) -> None:
    """Write everything in foreign-key order, in bulk. No per-row commits."""
    session.add(merchant)
    session.flush()

    session.bulk_save_objects(list(products))
    session.bulk_save_objects([_customer_row(merchant.id, plan) for plan in customers])
    session.flush()

    _bulk(session, sim.transactions)
    _bulk(session, sim.items)
    session.bulk_save_objects(list(khata_rows))
    session.flush()


def _bulk(session: Session, rows: Sequence[object]) -> None:
    for start in range(0, len(rows), BATCH_SIZE):
        session.bulk_save_objects(list(rows[start : start + BATCH_SIZE]))
        session.flush()


def _consent_tags(customer_id: str) -> dict[str, bool]:
    """Marketing consent state for a seeded customer.

    A real shop does not hold consent for everyone whose number it has: some were never asked,
    and a few have asked to be left alone. Seeding everyone as consented would hide the rule that
    matters, because the offer would never have anyone to refuse. Derived from the id rather than
    the rng so the same demo always drops the same people.
    """
    total = sum(ord(char) * (index + 1) for index, char in enumerate(customer_id))
    bucket = total % 100
    if bucket < 4:
        return {CONSENT_TAG: False, OPT_OUT_TAG: True}
    return {CONSENT_TAG: bucket >= 22, OPT_OUT_TAG: False}


def _customer_row(merchant_id: str, plan: _CustomerPlan) -> Customer:
    first_at = plan.visits[0][1] if plan.visits else None
    last_at = plan.visits[-1][1] if plan.visits else None
    joined = first_at or to_utc(_ist(date.fromordinal(plan.active_from_ord), 9, 0))
    return Customer(
        id=plan.id,
        merchant_id=merchant_id,
        name=plan.name,
        phone=plan.phone,
        first_seen_at=first_at,
        last_seen_at=last_at,
        txn_count=plan.txn_count,
        total_spend_paise=plan.total_spend_paise,
        is_khata_customer=plan.is_khata,
        segment=_segment_for(plan),
        tags={"cohort": plan.cohort.name, **_consent_tags(plan.id)},
        created_at=joined,
    )
