"""Tests for the seed generator.

The contract these tests enforce is the one the rest of the product leans on: the generated
history is deterministic, internally consistent, and every "planted" signal reported in
:class:`SeedResult` is *actually findable in the data* by a query written here from scratch —
never by trusting the generator's own bookkeeping.

Every test runs against a throwaway SQLite file under ``tmp_path``; the real ``data/munshiji.db``
is never touched.
"""

from __future__ import annotations

import hashlib
import statistics
import time
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from munshiji.clock import ist_date_of, range_bounds_ist, to_ist
from munshiji.db.base import Base
from munshiji.db.enums import KhataStatus, PaymentMethod
from munshiji.db.models import (
    Customer,
    KhataEntry,
    Merchant,
    Product,
    Transaction,
    TransactionItem,
)
from munshiji.seed import festivals
from munshiji.seed.catalog import (
    CATALOG,
    CATEGORIES,
    CATEGORY_SHARES,
    CO_OCCURRENCE,
)
from munshiji.seed.generator import (
    DOW_MULTIPLIER,
    HOUR_WEIGHTS,
    RECENT_WINDOW_DAYS,
    SeedResult,
    generate,
    month_multiplier,
)
from munshiji.seed.profiles import DEFAULT_PROFILE

SEED = 20260919
#: Pinned so assertions do not drift with the wall clock. A default-``as_of`` run is exercised
#: separately in ``test_default_as_of_and_runtime``.
AS_OF = date(2026, 9, 14)

#: Window covering Sharad Navratri (11–19 Oct) and Diwali (8 Nov) 2026.
FESTIVE_AS_OF = date(2026, 11, 20)

DEAD_STOCK_QUIET_DAYS = 45
DEAD_STOCK_MIN_CAPITAL_PAISE = 50_000  # ₹500


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Seeded:
    session: Session
    result: SeedResult
    as_of: date


def _open_db(path: Path) -> tuple[Engine, sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _seed_into(path: Path, **kwargs: object) -> tuple[Engine, Session, SeedResult]:
    engine, factory = _open_db(path)
    session = factory()
    result = generate(session, **kwargs)  # type: ignore[arg-type]
    session.commit()
    return engine, session, result


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Seeded]:
    """One shared 180-day Sharma General Store history, generated once for the module."""
    path = tmp_path_factory.mktemp("seed-default") / "munshiji.db"
    engine, session, result = _seed_into(path, seed=SEED, as_of=AS_OF)
    yield Seeded(session=session, result=result, as_of=AS_OF)
    session.close()
    engine.dispose()


@pytest.fixture(scope="module")
def festive(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Seeded]:
    """A window that actually contains the Navratri → Diwali run-up."""
    path = tmp_path_factory.mktemp("seed-festive") / "munshiji.db"
    engine, session, result = _seed_into(path, seed=SEED, as_of=FESTIVE_AS_OF)
    yield Seeded(session=session, result=result, as_of=FESTIVE_AS_OF)
    session.close()
    engine.dispose()


# ─────────────────────────────────────────────────────────────────────────────
# Query helpers — written independently of the generator's own bookkeeping
# ─────────────────────────────────────────────────────────────────────────────


def daily_collection(session: Session) -> dict[date, int]:
    """Collection per IST calendar day, straight from the transaction rows."""
    totals: dict[date, int] = defaultdict(int)
    rows = session.execute(select(Transaction.occurred_at, Transaction.amount_paise))
    for occurred_at, amount in rows:
        totals[ist_date_of(occurred_at)] += amount
    return dict(totals)


def units_sold(session: Session, product_id: str, since: date, until: date) -> float:
    start, end = range_bounds_ist(since, until)
    total = session.scalar(
        select(func.sum(TransactionItem.qty))
        .join(Transaction, Transaction.id == TransactionItem.transaction_id)
        .where(
            TransactionItem.product_id == product_id,
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
        )
    )
    return float(total or 0.0)


def recent_daily_rate(session: Session, product_id: str, as_of: date) -> float:
    """Units per day over the trailing window the generator sizes cover against."""
    first = as_of - timedelta(days=RECENT_WINDOW_DAYS - 1)
    return units_sold(session, product_id, first, as_of) / RECENT_WINDOW_DAYS


def category_margin_pct(session: Session, category: str, since: date, until: date) -> float:
    start, end = range_bounds_ist(since, until)
    revenue, cost = session.execute(
        select(
            func.sum(TransactionItem.line_total_paise),
            func.sum(TransactionItem.qty * TransactionItem.unit_cost_paise),
        )
        .join(Transaction, Transaction.id == TransactionItem.transaction_id)
        .join(Product, Product.id == TransactionItem.product_id)
        .where(
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
            Product.category == category,
        )
    ).one()
    return 0.0 if not revenue else (revenue - cost) / revenue * 100.0


def visit_dates(session: Session, customer_id: str) -> list[date]:
    rows = session.scalars(
        select(Transaction.occurred_at)
        .where(Transaction.customer_id == customer_id)
        .order_by(Transaction.occurred_at)
    ).all()
    return [ist_date_of(value) for value in rows]


def gap_threshold(gaps: Sequence[int]) -> float:
    """The per-customer dormancy bar from SPEC.md §7: ``median gap + 1.5 × IQR``."""
    quartiles = statistics.quantiles(gaps, n=4)
    return statistics.median(gaps) + 1.5 * (quartiles[2] - quartiles[0])


def robust_z(value: float, peers: Sequence[float]) -> float:
    """``0.6745 (x − median) / MAD`` — the outlier test SPEC.md §7 mandates."""
    median = statistics.median(peers)
    mad = statistics.median([abs(peer - median) for peer in peers])
    return 0.0 if mad == 0 else 0.6745 * (value - median) / mad


def fingerprint(session: Session) -> str:
    """A hash over every generated row, in a stable order."""
    digest = hashlib.sha256()
    queries = (
        select(Merchant.id, Merchant.shop_name, Merchant.opened_at).order_by(Merchant.id),
        select(
            Product.id,
            Product.sku,
            Product.cost_price_paise,
            Product.stock_qty,
            Product.reorder_level,
            Product.last_restocked_at,
        ).order_by(Product.id),
        select(
            Customer.id,
            Customer.name,
            Customer.txn_count,
            Customer.total_spend_paise,
            Customer.first_seen_at,
            Customer.last_seen_at,
            Customer.segment,
        ).order_by(Customer.id),
        select(
            Transaction.id,
            Transaction.customer_id,
            Transaction.amount_paise,
            Transaction.occurred_at,
            Transaction.payment_method,
            Transaction.channel,
        ).order_by(Transaction.id),
        select(
            TransactionItem.id,
            TransactionItem.product_id,
            TransactionItem.qty,
            TransactionItem.unit_price_paise,
            TransactionItem.unit_cost_paise,
            TransactionItem.line_total_paise,
        ).order_by(TransactionItem.id),
        select(
            KhataEntry.id,
            KhataEntry.customer_id,
            KhataEntry.amount_paise,
            KhataEntry.paid_paise,
            KhataEntry.opened_at,
            KhataEntry.status,
        ).order_by(KhataEntry.id),
    )
    for query in queries:
        for row in session.execute(query):
            digest.update(repr(tuple(row)).encode("utf-8"))
    return digest.hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Pure tables — no DB needed
# ─────────────────────────────────────────────────────────────────────────────


def test_catalog_is_well_formed() -> None:
    assert len(CATALOG) >= 55
    skus = [item.sku for item in CATALOG]
    assert len(set(skus)) == len(skus), "duplicate SKU in the catalogue"
    assert {item.category for item in CATALOG} == set(CATEGORIES)
    for item in CATALOG:
        assert item.sell_paise > item.cost_paise > 0, f"{item.sku} does not make money"
        assert item.margin_pct < 40.0, f"{item.sku} margin is not kirana-realistic"
        assert item.qty_choices and min(item.qty_choices) > 0
        assert item.name_hi, f"{item.sku} has no Hindi name"
        if item.is_perishable:
            assert item.shelf_life_days and item.shelf_life_days > 0


def test_mix_tables_are_probability_distributions() -> None:
    assert set(CATEGORY_SHARES) == set(CATEGORIES)
    assert sum(CATEGORY_SHARES.values()) == pytest.approx(1.0)
    assert set(CO_OCCURRENCE) == set(CATEGORIES)
    for anchor, row in CO_OCCURRENCE.items():
        assert set(row) == set(CATEGORIES), f"{anchor} row is missing a category"
        assert sum(row.values()) == pytest.approx(1.0), f"{anchor} row does not sum to 1"


def test_seasonality_tables_match_the_spec() -> None:
    assert len(DOW_MULTIPLIER) == 7
    assert DOW_MULTIPLIER[5] == pytest.approx(1.35)  # Saturday
    assert DOW_MULTIPLIER[6] == pytest.approx(1.25)  # Sunday
    assert min(DOW_MULTIPLIER) == DOW_MULTIPLIER[1] == pytest.approx(0.85)  # Tuesday

    assert sum(weight for _, weight in HOUR_WEIGHTS) == pytest.approx(1.0)
    shares = dict(HOUR_WEIGHTS)
    morning = sum(shares[hour] for hour in (8, 9, 10))
    trough = sum(shares[hour] for hour in (14, 15, 16))
    evening = sum(shares[hour] for hour in (17, 18, 19, 20))
    assert morning > trough * 2, "no morning peak"
    assert evening > trough * 2, "no evening peak"
    assert min(shares[hour] for hour in (8, 9, 10, 18, 19, 20)) > max(
        shares[hour] for hour in (14, 15)
    )

    assert month_multiplier(3) > month_multiplier(12) > month_multiplier(28)


def test_festival_calendar_covers_2026() -> None:
    required = {
        "makar_sankranti",
        "holi",
        "eid_ul_fitr",
        "ram_navami",
        "raksha_bandhan",
        "janmashtami",
        "ganesh_chaturthi",
        "navratri",
        "dussehra",
        "karva_chauth",
        "diwali",
        "bhai_dooj",
        "christmas",
    }
    keys = {f.key.rsplit("_", 1)[0] for f in festivals.FESTIVALS_2026}
    assert required <= keys
    for festival in festivals.FESTIVALS_2026:
        assert festival.day.year == 2026
        assert festival.prep_window_days > 0
        assert set(festival.category_uplift) <= set(CATEGORIES)
        assert all(value > 1.0 for value in festival.category_uplift.values())


def test_festival_uplift_ramps_and_decays() -> None:
    diwali = next(f for f in festivals.FESTIVALS_2026 if f.key == "diwali_2026")
    quiet = festivals.uplift_for(date(2026, 7, 15), "confectionery")
    early = festivals.uplift_for(diwali.day - timedelta(days=16), "confectionery")
    late = festivals.uplift_for(diwali.day - timedelta(days=2), "confectionery")
    assert quiet == pytest.approx(1.0)
    assert 1.0 < early < late
    assert late > 2.0
    # A category Diwali does not move stays put.
    assert festivals.uplift_for(diwali.day, "spices") < festivals.uplift_for(
        diwali.day, "confectionery"
    )

    soon = festivals.upcoming(AS_OF, within_days=60)
    assert [f.key for f in soon][:2] == ["navratri_2026", "dussehra_2026"]
    assert all(f.day > AS_OF for f in soon)
    assert festivals.days_until(soon[0], AS_OF) == 27


# ─────────────────────────────────────────────────────────────────────────────
# Determinism
# ─────────────────────────────────────────────────────────────────────────────


def test_same_seed_is_byte_identical(seeded: Seeded, tmp_path: Path) -> None:
    engine, session, result = _seed_into(tmp_path / "repeat.db", seed=SEED, as_of=AS_OF)
    try:
        assert fingerprint(session) == fingerprint(seeded.session)
        assert result.merchant_id == seeded.result.merchant_id
        assert result.transactions == seeded.result.transactions
        assert result.gross_collection_paise == seeded.result.gross_collection_paise
        assert result.collection_dip.dip_pct == seeded.result.collection_dip.dip_pct
    finally:
        session.close()
        engine.dispose()


def test_different_seed_gives_different_data(seeded: Seeded, tmp_path: Path) -> None:
    engine, session, result = _seed_into(tmp_path / "other.db", seed=SEED + 1, as_of=AS_OF)
    try:
        assert fingerprint(session) != fingerprint(seeded.session)
        assert result.gross_collection_paise != seeded.result.gross_collection_paise
        # ...but the *shape* of the world is stable across seeds.
        assert result.customers == seeded.result.customers
        assert result.open_khata_entries == seeded.result.open_khata_entries
        assert len(result.dormant_customers) == len(seeded.result.dormant_customers)
    finally:
        session.close()
        engine.dispose()


def test_default_as_of_and_runtime(tmp_path: Path) -> None:
    """The default profile, ending today IST, in well under 20 seconds."""
    started = time.perf_counter()
    engine, session, result = _seed_into(tmp_path / "today.db", seed=SEED)
    elapsed = time.perf_counter() - started
    try:
        assert elapsed < 20.0, f"generation took {elapsed:.1f}s"
        assert result.elapsed_seconds < 20.0
        assert result.days == 180
        assert (result.as_of - result.first_day).days == 179
        assert result.collection_dip is not None
        assert result.margin_leak is not None
    finally:
        session.close()
        engine.dispose()


# ─────────────────────────────────────────────────────────────────────────────
# Structural invariants
# ─────────────────────────────────────────────────────────────────────────────


def test_transaction_amount_equals_sum_of_items(seeded: Seeded) -> None:
    """The invariant everything downstream depends on — checked for *every* transaction."""
    session = seeded.session
    mismatches = session.execute(
        select(Transaction.id, Transaction.amount_paise, func.sum(TransactionItem.line_total_paise))
        .join(TransactionItem, TransactionItem.transaction_id == Transaction.id)
        .group_by(Transaction.id)
        .having(Transaction.amount_paise != func.sum(TransactionItem.line_total_paise))
    ).all()
    assert mismatches == []

    total_transactions = session.scalar(select(func.count()).select_from(Transaction))
    with_items = session.scalar(select(func.count(func.distinct(TransactionItem.transaction_id))))
    assert total_transactions == with_items, "a transaction has no lines"
    assert total_transactions == seeded.result.transactions


def test_line_totals_are_qty_times_unit_price(seeded: Seeded) -> None:
    rows = seeded.session.execute(
        select(
            TransactionItem.qty,
            TransactionItem.unit_price_paise,
            TransactionItem.unit_cost_paise,
            TransactionItem.line_total_paise,
        )
    ).all()
    assert len(rows) == seeded.result.transaction_items
    for qty, unit_price, unit_cost, line_total in rows:
        assert qty > 0
        assert line_total == round(qty * unit_price)
        assert 0 < unit_cost < unit_price, "margin analysis needs a real cost on every line"


def test_customer_aggregates_match_recomputation(seeded: Seeded) -> None:
    session = seeded.session
    recomputed = {
        customer_id: (count, total, first, last)
        for customer_id, count, total, first, last in session.execute(
            select(
                Transaction.customer_id,
                func.count(),
                func.sum(Transaction.amount_paise),
                func.min(Transaction.occurred_at),
                func.max(Transaction.occurred_at),
            )
            .where(Transaction.customer_id.is_not(None))
            .group_by(Transaction.customer_id)
        )
    }
    customers = session.scalars(select(Customer)).all()
    assert len(customers) == seeded.result.customers

    for customer in customers:
        expected = recomputed.get(customer.id)
        if expected is None:
            assert customer.txn_count == 0
            assert customer.total_spend_paise == 0
            assert customer.first_seen_at is None and customer.last_seen_at is None
            continue
        count, total, first, last = expected
        assert customer.txn_count == count, customer.name
        assert customer.total_spend_paise == total, customer.name
        assert ist_date_of(customer.first_seen_at) == ist_date_of(first)
        assert ist_date_of(customer.last_seen_at) == ist_date_of(last)


def test_walkin_share_and_daily_volume(seeded: Seeded) -> None:
    session = seeded.session
    walkins = session.scalar(
        select(func.count()).select_from(Transaction).where(Transaction.customer_id.is_(None))
    )
    assert walkins == seeded.result.walkin_transactions
    share = walkins / seeded.result.transactions
    assert DEFAULT_PROFILE.walkin_share - 0.05 < share < DEFAULT_PROFILE.walkin_share + 0.05

    per_day = defaultdict(int)
    for occurred_at in session.scalars(select(Transaction.occurred_at)):
        per_day[ist_date_of(occurred_at)] += 1
    assert len(per_day) == seeded.result.days, "a day with no trade at all"
    assert min(per_day.values()) >= DEFAULT_PROFILE.min_txns_per_day - 1
    assert max(per_day.values()) <= DEFAULT_PROFILE.max_txns_per_day
    assert 40 <= statistics.mean(per_day.values()) <= 70


def test_stock_is_consistent_with_consumption(seeded: Seeded) -> None:
    session = seeded.session
    products = session.scalars(select(Product)).all()
    assert len(products) == seeded.result.products
    for product in products:
        assert product.stock_qty >= 0.0, product.sku
        assert product.last_restocked_at is not None
        assert ist_date_of(product.last_restocked_at) <= seeded.as_of
        assert product.cost_price_paise < product.sell_price_paise

        sold = units_sold(session, product.id, seeded.result.first_day, seeded.as_of)
        assert sold > 0, f"{product.sku} never sold once"
        # Nobody sits on two years of cover, and nothing is stocked past the window's sales.
        assert product.stock_qty <= sold


# ─────────────────────────────────────────────────────────────────────────────
# Planted signals — each verified by an independent query
# ─────────────────────────────────────────────────────────────────────────────


def test_dormant_customers_are_really_dormant(seeded: Seeded) -> None:
    signals = seeded.result.dormant_customers
    assert 12 <= len(signals) <= 15

    for signal in signals:
        dates = visit_dates(seeded.session, signal.customer_id)
        assert len(dates) == signal.visits >= 5, signal.name
        assert dates[-1] == signal.last_seen_on

        days_since = (seeded.as_of - dates[-1]).days
        assert days_since == signal.days_since_last
        assert 21 <= days_since <= 42, f"{signal.name} stopped {days_since}d ago, not 3–6 weeks"

        gaps = [(later - earlier).days for earlier, later in pairwise(dates)]
        assert statistics.median(gaps) == pytest.approx(signal.median_gap_days, abs=0.01)
        assert days_since > gap_threshold(gaps), f"{signal.name} is not detectably dormant"


def test_dead_stock_skus_have_not_sold_and_lock_capital(seeded: Seeded) -> None:
    signals = seeded.result.dead_stock
    assert 3 <= len(signals) <= 4

    quiet_from = seeded.as_of - timedelta(days=DEAD_STOCK_QUIET_DAYS - 1)
    for signal in signals:
        product = seeded.session.get(Product, signal.product_id)
        assert product is not None and product.sku == signal.sku
        assert units_sold(seeded.session, signal.product_id, quiet_from, seeded.as_of) == 0.0
        assert signal.days_since_last_sale >= DEAD_STOCK_QUIET_DAYS

        locked = round(product.stock_qty * product.cost_price_paise)
        assert locked >= DEAD_STOCK_MIN_CAPITAL_PAISE, f"{signal.sku} locks only {locked}p"
        assert abs(locked - signal.capital_locked_paise) <= 1
        # It used to sell — that is what makes it dead rather than merely new.
        before = units_sold(seeded.session, signal.product_id, seeded.result.first_day, quiet_from)
        assert before > 0, f"{signal.sku} never sold at all"


def test_fast_movers_are_about_to_stock_out(seeded: Seeded) -> None:
    signals = seeded.result.stockout_risks
    assert 2 <= len(signals) <= 3

    for signal in signals:
        product = seeded.session.get(Product, signal.product_id)
        rate = recent_daily_rate(seeded.session, signal.product_id, seeded.as_of)
        assert rate > 1.0, f"{signal.sku} is not a fast mover"
        assert rate == pytest.approx(signal.daily_rate, abs=0.01)
        cover = product.stock_qty / rate
        assert 0 < cover <= 3.5, f"{signal.sku} has {cover:.1f} days of cover"


def test_overstocked_perishables_outlive_their_shelf_life(seeded: Seeded) -> None:
    signals = seeded.result.expiry_risks
    assert 1 <= len(signals) <= 2

    for signal in signals:
        product = seeded.session.get(Product, signal.product_id)
        assert product.is_perishable and product.shelf_life_days
        rate = recent_daily_rate(seeded.session, signal.product_id, seeded.as_of)
        cover = product.stock_qty / rate
        remaining = (
            product.shelf_life_days - (seeded.as_of - ist_date_of(product.last_restocked_at)).days
        )
        assert remaining == signal.remaining_shelf_life_days
        assert 0 < remaining <= 7
        assert cover > remaining, f"{signal.sku}: {cover:.1f}d cover vs {remaining}d shelf life"


def test_open_khata_has_a_realistic_aging_spread(seeded: Seeded) -> None:
    session = seeded.session
    open_entries = session.scalars(
        select(KhataEntry).where(KhataEntry.status.in_([KhataStatus.OPEN, KhataStatus.PARTIAL]))
    ).all()
    expected = DEFAULT_PROFILE
    assert len(open_entries) == seeded.result.open_khata_entries == expected.open_khata_count

    ages = sorted((seeded.as_of - ist_date_of(entry.opened_at)).days for entry in open_entries)
    assert (
        sum(1 for age in ages if age >= 60)
        == seeded.result.khata_over_60_days
        == expected.khata_over_60_count
    )
    assert min(ages) < 16, "no fresh udhaar at all"
    assert sum(1 for age in ages if 16 <= age < 60) >= 8, "aging spread is too clumpy"

    # The share that matters. A kirana's credit book is working capital sitting on a shelf, and
    # at a few percent of turnover the whole udhaar feature would be arguing about loose change.
    monthly = seeded.result.gross_collection_paise / max(1, seeded.result.days) * 30
    share = seeded.result.open_khata_paise / monthly
    assert 0.08 <= share <= 0.30, f"udhaar is {share:.1%} of monthly turnover, outside kirana range"

    for entry in open_entries:
        assert entry.outstanding_paise > 0
        assert entry.due_at is not None and entry.due_at > entry.opened_at
        assert entry.settled_at is None
        assert ist_date_of(entry.opened_at) <= seeded.as_of
        assert session.get(Customer, entry.customer_id).is_khata_customer

    settled = session.scalars(
        select(KhataEntry).where(KhataEntry.status == KhataStatus.SETTLED)
    ).all()
    assert settled, "no repayment history to score reliability from"
    assert all(entry.settled_at is not None for entry in settled)
    assert all(entry.paid_paise == entry.amount_paise for entry in settled)


def test_last_week_collection_is_genuinely_soft(seeded: Seeded) -> None:
    """The opening line of the demo, recomputed here with a robust median/MAD test."""
    totals = daily_collection(seeded.session)
    days = sorted(totals)
    window = days[-DEFAULT_PROFILE.collection_dip_days :]

    observed = sum(totals[day] for day in window)
    baseline = 0.0
    z_scores: list[float] = []
    for day in window:
        peers = [
            totals[other]
            for other in days
            if other < window[0] and other.weekday() == day.weekday()
        ][-DEFAULT_PROFILE.baseline_weeks :]
        assert len(peers) == DEFAULT_PROFILE.baseline_weeks
        baseline += statistics.median(peers)
        z_scores.append(robust_z(totals[day], peers))

    dip_pct = (observed - baseline) / baseline * 100.0
    assert dip_pct == pytest.approx(seeded.result.collection_dip.dip_pct, abs=0.05)
    assert -16.0 <= dip_pct <= -9.0, f"dip of {dip_pct:.1f}% is outside the ~10–15% band"

    # A single day's collection carries ~14% sampling noise, so the *week* is the statistic with
    # power. Compare it against the eight preceding weekday-aligned weeks, median/MAD as above.
    prior_weeks = [
        sum(totals[day] for day in days[-7 * (week + 1) : -7 * week])
        for week in range(1, DEFAULT_PROFILE.baseline_weeks + 1)
    ]
    assert robust_z(observed, prior_weeks) < -0.6, "the soft week is not a robust outlier"

    # ...and day by day it is soft throughout, not one bad Tuesday dragging the total down.
    assert statistics.median(z_scores) < -0.25
    assert sum(1 for z in z_scores if z < 0) >= 4, "the soft week is not consistently soft"

    reported = seeded.result.collection_dip
    assert reported.observed_paise == observed
    assert reported.start_day == window[0] and reported.end_day == seeded.as_of


def test_one_category_has_a_slipping_margin(seeded: Seeded) -> None:
    leak = seeded.result.margin_leak
    assert leak is not None and leak.category == DEFAULT_PROFILE.margin_leak_category
    window = leak.window_days

    after = category_margin_pct(
        seeded.session, leak.category, seeded.as_of - timedelta(days=window - 1), seeded.as_of
    )
    before = category_margin_pct(
        seeded.session,
        leak.category,
        seeded.as_of - timedelta(days=2 * window - 1),
        seeded.as_of - timedelta(days=window),
    )
    assert before == pytest.approx(leak.margin_pct_before, abs=0.05)
    assert after == pytest.approx(leak.margin_pct_after, abs=0.05)
    assert after > 0.0, "the shop should still be making money, just less"
    assert before - after >= 3.0, "the margin slip is too small to notice"

    # ...and it is specific to that category, not a shop-wide artefact.
    control = next(c for c in CATEGORIES if c != leak.category)
    control_after = category_margin_pct(
        seeded.session, control, seeded.as_of - timedelta(days=window - 1), seeded.as_of
    )
    control_before = category_margin_pct(
        seeded.session,
        control,
        seeded.as_of - timedelta(days=2 * window - 1),
        seeded.as_of - timedelta(days=window),
    )
    assert abs(control_before - control_after) < 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Realism the insight engines depend on
# ─────────────────────────────────────────────────────────────────────────────


def test_day_of_week_seasonality_is_visible(seeded: Seeded) -> None:
    totals = daily_collection(seeded.session)
    # Exclude the planted soft week so it does not skew the weekday means.
    days = sorted(totals)[: -DEFAULT_PROFILE.collection_dip_days]
    by_weekday: dict[int, list[int]] = defaultdict(list)
    for day in days:
        by_weekday[day.weekday()].append(totals[day])
    means = {weekday: statistics.mean(values) for weekday, values in by_weekday.items()}

    assert max(means, key=lambda weekday: means[weekday]) == 5, "Saturday is not the best day"
    assert min(means, key=lambda weekday: means[weekday]) == 1, "Tuesday is not the worst day"
    assert means[5] / means[1] > 1.25
    assert means[6] > means[3], "Sunday should beat a mid-week day"


def test_intraday_curve_is_bimodal(seeded: Seeded) -> None:
    by_hour: dict[int, int] = defaultdict(int)
    for occurred_at in seeded.session.scalars(select(Transaction.occurred_at)):
        by_hour[to_ist(occurred_at).hour] += 1
    assert min(by_hour) >= DEFAULT_PROFILE.business_hours_start
    assert max(by_hour) < DEFAULT_PROFILE.business_hours_end

    morning = sum(by_hour[hour] for hour in (8, 9, 10))
    trough = sum(by_hour[hour] for hour in (14, 15, 16))
    evening = sum(by_hour[hour] for hour in (17, 18, 19, 20))
    assert morning > trough * 2, "morning peak missing"
    assert evening > morning, "evening should be the heavier peak"
    assert by_hour[13] > by_hour[15], "the trough should sit mid-afternoon, not at lunch"


def test_month_cycle_lifts_salary_week(seeded: Seeded) -> None:
    totals = daily_collection(seeded.session)
    days = sorted(totals)[: -DEFAULT_PROFILE.collection_dip_days]
    salary_week = [totals[day] for day in days if day.day <= 7]
    month_end = [totals[day] for day in days if day.day >= 26]
    assert statistics.mean(salary_week) > statistics.mean(month_end) * 1.15


def test_payment_mix_drifts_towards_upi(seeded: Seeded) -> None:
    session = seeded.session

    def upi_share(first: date, last: date) -> float:
        start, end = range_bounds_ist(first, last)
        window = (Transaction.occurred_at >= start, Transaction.occurred_at < end)
        total = session.scalar(select(func.count()).select_from(Transaction).where(*window))
        upi = session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(*window, Transaction.payment_method == PaymentMethod.UPI)
        )
        return upi / total

    first_month = upi_share(seeded.result.first_day, seeded.result.first_day + timedelta(days=29))
    last_month = upi_share(seeded.as_of - timedelta(days=29), seeded.as_of)
    assert first_month == pytest.approx(seeded.result.upi_share_first_month, abs=0.001)
    assert last_month == pytest.approx(seeded.result.upi_share_last_month, abs=0.001)
    assert 0.50 <= first_month <= 0.60
    assert 0.66 <= last_month <= 0.76
    assert last_month - first_month > 0.08

    mix = seeded.result.payment_mix
    assert set(mix) == {method.value for method in PaymentMethod}
    assert sum(mix.values()) == pytest.approx(1.0, abs=0.001)
    assert mix[PaymentMethod.SOUNDBOX.value] > 0.03
    assert mix[PaymentMethod.CASH.value] > 0.10, "an Indian kirana still takes cash"


def test_baskets_are_composed_not_random(seeded: Seeded) -> None:
    session = seeded.session
    sizes = (
        session.execute(
            select(func.count())
            .select_from(TransactionItem)
            .group_by(TransactionItem.transaction_id)
        )
        .scalars()
        .all()
    )
    assert min(sizes) >= 1
    assert max(sizes) <= 6
    assert 2.0 < statistics.mean(sizes) < 4.0

    # Every category actually moves, and staples/dairy dominate as they should in a kirana.
    revenue = dict(
        session.execute(
            select(Product.category, func.sum(TransactionItem.line_total_paise))
            .join(TransactionItem, TransactionItem.product_id == Product.id)
            .group_by(Product.category)
        ).all()
    )
    assert set(revenue) == set(CATEGORIES)
    assert min(revenue.values()) > 0
    assert max(revenue, key=lambda category: revenue[category]) == "staples"

    # No bill lists the same SKU twice.
    duplicates = session.execute(
        select(TransactionItem.transaction_id, TransactionItem.product_id, func.count())
        .group_by(TransactionItem.transaction_id, TransactionItem.product_id)
        .having(func.count() > 1)
    ).all()
    assert duplicates == []


def test_navratri_and_diwali_ramp_is_in_the_data(festive: Seeded) -> None:
    """A window that covers the festival season must *show* the festival season."""
    session = festive.session
    diwali = next(f for f in festivals.FESTIVALS_2026 if f.key == "diwali_2026")
    navratri = next(f for f in festivals.FESTIVALS_2026 if f.key == "navratri_2026")

    def confectionery_per_day(first: date, last: date) -> float:
        start, end = range_bounds_ist(first, last)
        total = session.scalar(
            select(func.sum(TransactionItem.line_total_paise))
            .join(Transaction, Transaction.id == TransactionItem.transaction_id)
            .join(Product, Product.id == TransactionItem.product_id)
            .where(
                Transaction.occurred_at >= start,
                Transaction.occurred_at < end,
                Product.category == "confectionery",
            )
        )
        return (total or 0) / ((last - first).days + 1)

    quiet = confectionery_per_day(date(2026, 7, 1), date(2026, 7, 31))
    navratri_run_up = confectionery_per_day(navratri.start_day, navratri.end_day)
    diwali_run_up = confectionery_per_day(diwali.day - timedelta(days=10), diwali.day)

    assert navratri_run_up > quiet * 1.2, "no Navratri ramp"
    assert diwali_run_up > quiet * 1.8, "no Diwali ramp"
    assert diwali_run_up > navratri_run_up

    # The whole shop lifts too, not just the mithai shelf.
    totals = daily_collection(session)
    festive_days = [
        totals[day] for day in totals if diwali.day - timedelta(days=10) <= day <= diwali.day
    ]
    ordinary = [totals[day] for day in totals if date(2026, 7, 1) <= day <= date(2026, 7, 31)]
    assert statistics.mean(festive_days) > statistics.mean(ordinary) * 1.15
