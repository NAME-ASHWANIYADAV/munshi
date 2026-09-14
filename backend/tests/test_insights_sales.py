"""Sales engines against fixtures whose answers were worked out by hand.

Every number asserted here was computed on paper first — the median and MAD of an eight-Monday
series, the exact robust z it implies, a projection whose arithmetic is a single division. If
the engine and the paper disagree, the engine is wrong.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from munshiji.clock import IST, to_utc
from munshiji.db.base import Base
from munshiji.db.enums import PaymentMethod, Severity
from munshiji.db.models import Merchant, Product, Transaction, TransactionItem
from munshiji.insights.base import InsightContext
from munshiji.insights.sales import (
    CollectionAnomalyEngine,
    MarginLeakEngine,
    PaymentMixEngine,
    PeakHourEngine,
    baseline_confidence,
    hour_label_en,
    hour_label_hi,
    severity_from_z,
)
from munshiji.insights.stats import chi2_sf, mad, median, robust_z

# 2026-09-14 is a Monday; the whole fixture set is anchored to it.
TODAY = date(2026, 9, 14)
RUPEE = 100  # paise per rupee


# ── fixtures plumbing ───────────────────────────────────────────────────────


def make_session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def at(day: date, hour: int = 12, minute: int = 0) -> datetime:
    """An IST wall-clock moment, converted to UTC the way the ORM stores it."""
    return to_utc(datetime.combine(day, time(hour, minute), tzinfo=IST))


def ist_at(day: date, hour: int = 12, minute: int = 0) -> datetime:
    """The same moment, left in IST — what an engine receives as ``as_of``."""
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


def make_merchant(session: Session) -> Merchant:
    merchant = Merchant(owner_name="Sharma ji", shop_name="Sharma General Store")
    session.add(merchant)
    session.flush()
    return merchant


def sale(
    session: Session,
    merchant_id: str,
    day: date,
    hour: int,
    paise: int,
    *,
    method: PaymentMethod = PaymentMethod.UPI,
    minute: int = 0,
) -> Transaction:
    txn = Transaction(
        merchant_id=merchant_id,
        amount_paise=paise,
        occurred_at=at(day, hour, minute),
        payment_method=method,
    )
    session.add(txn)
    session.flush()
    return txn


def ctx_for(session: Session, merchant: Merchant, moment: datetime) -> InsightContext:
    return InsightContext(session=session, merchant_id=merchant.id, as_of=moment)


# ── weekday baseline: median + MAD computed by hand ─────────────────────────

#: Eight Mondays. Sorted: 8000, 9000, 10000, 10000, 10000, 10000, 11000, 12000 (rupees).
#: median = (10000 + 10000) / 2 = 10000.
#: |deviations| = 2000, 1000, 0, 0, 0, 0, 1000, 2000 -> sorted 0,0,0,0,1000,1000,2000,2000
#: MAD = (0 + 1000) / 2 = 500.
MONDAY_BASELINE: dict[date, int] = {
    date(2026, 7, 20): 10_000,
    date(2026, 7, 27): 10_000,
    date(2026, 8, 3): 12_000,
    date(2026, 8, 10): 10_000,
    date(2026, 8, 17): 8_000,
    date(2026, 8, 24): 10_000,
    date(2026, 8, 31): 11_000,
    date(2026, 9, 7): 9_000,
}
EXPECTED_MEDIAN_RUPEES = 10_000
EXPECTED_MAD_RUPEES = 500


def test_hand_computed_median_and_mad() -> None:
    """Sanity-check the paper arithmetic the engine assertions depend on."""
    values = [float(value) for value in MONDAY_BASELINE.values()]
    assert median(values) == EXPECTED_MEDIAN_RUPEES
    assert mad(values) == EXPECTED_MAD_RUPEES
    # 0.6745 * (8500 - 10000) / 500
    assert robust_z(8_500, EXPECTED_MEDIAN_RUPEES, EXPECTED_MAD_RUPEES) == pytest.approx(
        -2.0235, abs=1e-4
    )


def seed_monday_baseline(session: Session, merchant: Merchant, today_rupees: int) -> None:
    """Eight historical Mondays plus today, each split 40% morning / 60% evening."""
    for day, rupees_total in MONDAY_BASELINE.items():
        paise = rupees_total * RUPEE
        sale(session, merchant.id, day, 10, int(paise * 0.4))
        sale(session, merchant.id, day, 18, int(paise * 0.6))
    paise = today_rupees * RUPEE
    sale(session, merchant.id, TODAY, 10, int(paise * 0.4))
    sale(session, merchant.id, TODAY, 18, int(paise * 0.6))
    session.commit()


def test_collection_anomaly_matches_hand_computed_z_and_delta() -> None:
    session = make_session()
    merchant = make_merchant(session)
    seed_monday_baseline(session, merchant, today_rupees=8_500)

    # 23:30 — after close, so the projection resolves to the actual takings.
    drafts = CollectionAnomalyEngine().run(ctx_for(session, merchant, ist_at(TODAY, 23, 30)))
    today = next(draft for draft in drafts if draft.dedupe_key == "collection_anomaly:today")
    metrics = today.metrics

    assert metrics["baseline_samples"] == 8
    assert metrics["baseline_paise"] == EXPECTED_MEDIAN_RUPEES * RUPEE
    assert metrics["baseline_mad_paise"] == EXPECTED_MAD_RUPEES * RUPEE
    assert metrics["collected_so_far_paise"] == 8_500 * RUPEE
    assert metrics["projected_close_paise"] == 8_500 * RUPEE
    assert metrics["robust_z"] == pytest.approx(-2.0235, abs=1e-3)
    assert metrics["delta_pct"] == pytest.approx(-15.0, abs=1e-6)
    assert metrics["delta_paise"] == -1_500 * RUPEE
    assert today.severity is Severity.MEDIUM  # 1.5 <= |z| < 3
    assert today.impact_paise == 1_500 * RUPEE
    assert today.suggested_tool == "schedule_followup"


def test_collection_anomaly_metric_keys_the_voice_layer_depends_on() -> None:
    session = make_session()
    merchant = make_merchant(session)
    seed_monday_baseline(session, merchant, today_rupees=8_500)

    drafts = CollectionAnomalyEngine().run(ctx_for(session, merchant, ist_at(TODAY, 19, 0)))
    metrics = next(
        draft for draft in drafts if draft.dedupe_key == "collection_anomaly:today"
    ).metrics
    for key in (
        "collected_so_far_paise",
        "projected_close_paise",
        "baseline_paise",
        "delta_pct",
        "robust_z",
        "band_low_paise",
        "band_high_paise",
        "hours_elapsed_share",
    ):
        assert key in metrics, key


@pytest.mark.parametrize(
    ("today_rupees", "expected"),
    [
        (10_000, Severity.INFO),  # z = 0
        (8_500, Severity.MEDIUM),  # z = -2.02
        (7_500, Severity.CRITICAL),  # z = -3.37, money missing
        (12_500, Severity.HIGH),  # z = +3.37, upside
    ],
)
def test_collection_anomaly_severity_ladder(today_rupees: int, expected: Severity) -> None:
    session = make_session()
    merchant = make_merchant(session)
    seed_monday_baseline(session, merchant, today_rupees=today_rupees)
    drafts = CollectionAnomalyEngine().run(ctx_for(session, merchant, ist_at(TODAY, 23, 30)))
    today = next(draft for draft in drafts if draft.dedupe_key == "collection_anomaly:today")
    assert today.severity is expected


def test_severity_never_exceeds_medium_on_a_thin_baseline() -> None:
    assert severity_from_z(-4.0, sample_count=8) is Severity.CRITICAL
    assert severity_from_z(-4.0, sample_count=3) is Severity.MEDIUM
    assert severity_from_z(-0.5, sample_count=3) is Severity.INFO
    assert baseline_confidence(3, degenerate_mad=False) < baseline_confidence(
        8, degenerate_mad=False
    )
    assert baseline_confidence(8, degenerate_mad=True) < baseline_confidence(
        8, degenerate_mad=False
    )


# ── partial-day projection ──────────────────────────────────────────────────


def seed_simple_intraday(
    session: Session,
    merchant: Merchant,
    *,
    morning_hour: int,
    morning_share: float,
    weeks: int = 6,
) -> None:
    """Historical Mondays of exactly ₹1,000: ``morning_share`` early, the rest at 18:00."""
    total = 1_000 * RUPEE
    for step in range(1, weeks + 1):
        day = TODAY - timedelta(days=7 * step)
        sale(session, merchant.id, day, morning_hour, int(total * morning_share))
        sale(session, merchant.id, day, 18, int(total * (1 - morning_share)))
    session.commit()


def test_projection_is_plain_division_by_the_elapsed_share() -> None:
    """40% of every past Monday is banked by noon; ₹300 by noon today projects to ₹750."""
    session = make_session()
    merchant = make_merchant(session)
    seed_simple_intraday(session, merchant, morning_hour=9, morning_share=0.4)
    sale(session, merchant.id, TODAY, 9, 300 * RUPEE)
    session.commit()

    drafts = CollectionAnomalyEngine().run(ctx_for(session, merchant, ist_at(TODAY, 12, 0)))
    metrics = next(
        draft for draft in drafts if draft.dedupe_key == "collection_anomaly:today"
    ).metrics

    assert metrics["hours_elapsed_share"] == pytest.approx(0.4)
    assert metrics["collected_so_far_paise"] == 300 * RUPEE
    assert metrics["projected_close_paise"] == 750 * RUPEE
    assert metrics["too_early"] is False
    # Every historical day has the identical curve, so the band collapses onto the estimate.
    assert metrics["band_low_paise"] == 750 * RUPEE
    assert metrics["band_high_paise"] == 750 * RUPEE
    assert metrics["projection_sample_days"] == 6


def test_projection_band_widens_with_historical_dispersion() -> None:
    """Mondays that bank between 25% and 55% by noon must produce a band, not a point."""
    session = make_session()
    merchant = make_merchant(session)
    total = 1_000 * RUPEE
    for step, share in enumerate([0.25, 0.30, 0.40, 0.45, 0.50, 0.55], start=1):
        day = TODAY - timedelta(days=7 * step)
        sale(session, merchant.id, day, 9, int(total * share))
        sale(session, merchant.id, day, 18, int(total * (1 - share)))
    sale(session, merchant.id, TODAY, 9, 400 * RUPEE)
    session.commit()

    drafts = CollectionAnomalyEngine().run(ctx_for(session, merchant, ist_at(TODAY, 12, 0)))
    metrics = next(
        draft for draft in drafts if draft.dedupe_key == "collection_anomaly:today"
    ).metrics

    # Median of the six shares is (0.40 + 0.45) / 2 = 0.425 -> 40000 / 0.425 = 94,118 paise.
    assert metrics["hours_elapsed_share"] == pytest.approx(0.425)
    assert metrics["projected_close_paise"] == pytest.approx(94_118, abs=1)
    assert metrics["band_low_paise"] < metrics["projected_close_paise"]
    assert metrics["band_high_paise"] > metrics["projected_close_paise"]


def test_too_early_to_project_below_the_share_threshold() -> None:
    """5% of the day banked is under the 8% floor: refuse to project, and say so."""
    session = make_session()
    merchant = make_merchant(session)
    seed_simple_intraday(session, merchant, morning_hour=8, morning_share=0.05)
    sale(session, merchant.id, TODAY, 8, 50 * RUPEE)
    session.commit()

    drafts = CollectionAnomalyEngine().run(ctx_for(session, merchant, ist_at(TODAY, 9, 0)))
    today = next(draft for draft in drafts if draft.dedupe_key == "collection_anomaly:today")

    assert today.metrics["hours_elapsed_share"] == pytest.approx(0.05)
    assert today.metrics["too_early"] is True
    assert today.metrics["projected_close_paise"] is None
    assert today.metrics["robust_z"] is None
    assert today.metrics["delta_pct"] is None
    assert today.metrics["collected_so_far_paise"] == 50 * RUPEE
    assert today.severity is Severity.INFO
    assert today.confidence <= 0.35
    assert today.impact_paise == 0


def test_the_threshold_is_the_only_difference_between_refusing_and_projecting() -> None:
    """Same clock, same shape: 5% banked refuses (above), 10% banked projects."""
    session = make_session()
    merchant = make_merchant(session)
    seed_simple_intraday(session, merchant, morning_hour=8, morning_share=0.10)
    sale(session, merchant.id, TODAY, 8, 100 * RUPEE)
    session.commit()

    drafts = CollectionAnomalyEngine().run(ctx_for(session, merchant, ist_at(TODAY, 9, 0)))
    metrics = next(
        draft for draft in drafts if draft.dedupe_key == "collection_anomaly:today"
    ).metrics
    assert metrics["hours_elapsed_share"] == pytest.approx(0.10)
    assert metrics["too_early"] is False
    assert metrics["projected_close_paise"] == 1_000 * RUPEE


def test_intraday_curve_interpolates_within_the_hour() -> None:
    """12:30 sits half way between the 12:00 and 13:00 cumulative shares."""
    session = make_session()
    merchant = make_merchant(session)
    total = 1_000 * RUPEE
    for step in range(1, 7):
        day = TODAY - timedelta(days=7 * step)
        sale(session, merchant.id, day, 9, int(total * 0.4))
        sale(session, merchant.id, day, 12, int(total * 0.2))  # lands in the 12:00-13:00 bucket
        sale(session, merchant.id, day, 18, int(total * 0.4))
    sale(session, merchant.id, TODAY, 9, 100 * RUPEE)
    session.commit()

    engine = CollectionAnomalyEngine()
    noon = next(
        draft
        for draft in engine.run(ctx_for(session, merchant, ist_at(TODAY, 12, 0)))
        if draft.dedupe_key == "collection_anomaly:today"
    )
    half_past = next(
        draft
        for draft in engine.run(ctx_for(session, merchant, ist_at(TODAY, 12, 30)))
        if draft.dedupe_key == "collection_anomaly:today"
    )
    assert noon.metrics["hours_elapsed_share"] == pytest.approx(0.40)
    assert half_past.metrics["hours_elapsed_share"] == pytest.approx(0.50)  # 0.40 + 0.20/2


# ── trailing-week anomaly ───────────────────────────────────────────────────


def test_soft_week_long_dip_is_caught_even_though_no_single_day_would_fire() -> None:
    """Every day 12% light — under the single-day bar, unmistakable across seven days."""
    session = make_session()
    merchant = make_merchant(session)
    base = 10_000 * RUPEE
    # 9 weeks of history so each of the last 7 days has a full same-weekday baseline.
    for offset in range(8, 70):
        day = TODAY - timedelta(days=offset)
        sale(session, merchant.id, day, 10, int(base * 0.4))
        sale(session, merchant.id, day, 18, int(base * 0.6))
    for offset in range(1, 8):
        day = TODAY - timedelta(days=offset)
        sale(session, merchant.id, day, 10, int(base * 0.88 * 0.4))
        sale(session, merchant.id, day, 18, int(base * 0.88 * 0.6))
    session.commit()

    drafts = CollectionAnomalyEngine().run(ctx_for(session, merchant, ist_at(TODAY, 12, 0)))
    week = next(draft for draft in drafts if draft.dedupe_key == "collection_anomaly:week")

    assert week.metrics["window_days"] == 7
    assert week.metrics["delta_pct"] == pytest.approx(-12.0, abs=0.5)
    assert week.metrics["robust_z"] < 0
    assert abs(week.metrics["stouffer_z"]) > abs(week.metrics["robust_z"])
    assert week.severity in (Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL)
    assert week.impact_paise == pytest.approx(7 * base * 0.12, rel=0.02)


def test_steady_week_produces_no_week_anomaly() -> None:
    session = make_session()
    merchant = make_merchant(session)
    base = 10_000 * RUPEE
    for offset in range(1, 70):
        day = TODAY - timedelta(days=offset)
        sale(session, merchant.id, day, 10, int(base * 0.4))
        sale(session, merchant.id, day, 18, int(base * 0.6))
    session.commit()

    drafts = CollectionAnomalyEngine().run(ctx_for(session, merchant, ist_at(TODAY, 12, 0)))
    assert not [draft for draft in drafts if draft.dedupe_key == "collection_anomaly:week"]


# ── peak hour ───────────────────────────────────────────────────────────────


def test_peak_hour_finds_both_windows_of_a_bimodal_day() -> None:
    session = make_session()
    merchant = make_merchant(session)
    for offset in range(1, 31):
        day = TODAY - timedelta(days=offset)
        sale(session, merchant.id, day, 9, 300 * RUPEE)
        sale(session, merchant.id, day, 10, 300 * RUPEE)
        sale(session, merchant.id, day, 19, 200 * RUPEE)
        sale(session, merchant.id, day, 20, 200 * RUPEE)
    session.commit()

    draft = PeakHourEngine().run(ctx_for(session, merchant, ist_at(TODAY, 12, 0)))[0]
    windows = draft.metrics["peak_windows"]

    assert len(windows) == 2
    assert windows[0]["start_hour"] == 9 and windows[0]["end_hour"] == 11
    assert windows[1]["start_hour"] == 19 and windows[1]["end_hour"] == 21
    assert windows[0]["share_pct"] == pytest.approx(60.0)
    assert windows[1]["share_pct"] == pytest.approx(40.0)
    assert draft.metrics["combined_share_pct"] == pytest.approx(100.0)
    assert draft.metrics["peak_daily_revenue_paise"] == 1_000 * RUPEE
    assert draft.severity is Severity.INFO


def test_hour_labels_are_bilingual() -> None:
    assert hour_label_en(9) == "9 AM"
    assert hour_label_en(19) == "7 PM"
    assert hour_label_hi(9) == "सुबह 9 बजे"
    assert hour_label_hi(19) == "शाम 7 बजे"


# ── payment mix ─────────────────────────────────────────────────────────────


def seed_mix(
    session: Session,
    merchant: Merchant,
    *,
    prior_upi: int,
    prior_cash: int,
    recent_upi: int,
    recent_cash: int,
) -> None:
    """Per-day transaction counts of ₹100 each, over the prior-28 and recent-14 windows."""
    for offset in range(15, 43):  # prior 28 complete days
        day = TODAY - timedelta(days=offset)
        for index in range(prior_upi):
            sale(session, merchant.id, day, 10, 100 * RUPEE, minute=index)
        for index in range(prior_cash):
            sale(
                session, merchant.id, day, 11, 100 * RUPEE, method=PaymentMethod.CASH, minute=index
            )
    for offset in range(1, 15):  # recent 14 complete days
        day = TODAY - timedelta(days=offset)
        for index in range(recent_upi):
            sale(session, merchant.id, day, 10, 100 * RUPEE, minute=index)
        for index in range(recent_cash):
            sale(
                session, merchant.id, day, 11, 100 * RUPEE, method=PaymentMethod.CASH, minute=index
            )
    session.commit()


def test_payment_mix_flags_a_swing_from_upi_to_cash() -> None:
    """UPI 2/3 -> 1/3 of revenue: a 33-point drift with a chi-square that leaves no doubt."""
    session = make_session()
    merchant = make_merchant(session)
    seed_mix(session, merchant, prior_upi=2, prior_cash=1, recent_upi=1, recent_cash=2)

    draft = PaymentMixEngine().run(ctx_for(session, merchant, ist_at(TODAY, 12, 0)))[0]
    metrics = draft.metrics
    biggest = metrics["biggest_shift"]

    assert metrics["prior_txns"] == 84 and metrics["recent_txns"] == 42
    assert biggest["method"] == "upi"
    assert biggest["prior_share_pct"] == pytest.approx(66.67, abs=0.01)
    assert biggest["recent_share_pct"] == pytest.approx(33.33, abs=0.01)
    assert biggest["delta_pp"] == pytest.approx(-33.33, abs=0.01)
    # expected upi 28 vs observed 14 -> 7.0; expected cash 14 vs observed 28 -> 14.0
    assert metrics["chi_square"] == pytest.approx(21.0, abs=1e-6)
    assert metrics["dof"] == 1
    assert metrics["p_value"] == pytest.approx(chi2_sf(21.0, 1), abs=1e-8)
    assert 0.0 < metrics["p_value"] < 0.05
    assert draft.severity is Severity.HIGH  # drift >= 15 points
    assert metrics["moved_paise"] == pytest.approx(
        0.3333 * metrics["recent_revenue_paise"], rel=0.01
    )


def test_payment_mix_silent_when_the_split_holds() -> None:
    session = make_session()
    merchant = make_merchant(session)
    seed_mix(session, merchant, prior_upi=2, prior_cash=1, recent_upi=2, recent_cash=1)
    assert PaymentMixEngine().run(ctx_for(session, merchant, ist_at(TODAY, 12, 0))) == []


def test_payment_mix_ignores_a_sample_too_small_to_test() -> None:
    session = make_session()
    merchant = make_merchant(session)
    for offset in range(1, 15):
        sale(session, merchant.id, TODAY - timedelta(days=offset), 10, 100 * RUPEE)
    session.commit()
    assert PaymentMixEngine().run(ctx_for(session, merchant, ist_at(TODAY, 12, 0))) == []


# ── margin leak ─────────────────────────────────────────────────────────────


def seed_margin(
    session: Session,
    merchant: Merchant,
    *,
    category: str = "staples",
    recent_unit_cost: int,
    prior_unit_cost: int,
) -> None:
    """One line a day: 10 units at ₹10, with the purchase cost changing 30 days ago."""
    product = Product(
        merchant_id=merchant.id,
        sku="ATTA5",
        name="Atta 5kg",
        name_hi="आटा 5 किलो",
        category=category,
        cost_price_paise=prior_unit_cost,
        sell_price_paise=1_000,
        stock_qty=500.0,
    )
    session.add(product)
    session.flush()

    def line(day: date, unit_cost: int) -> None:
        txn = sale(session, merchant.id, day, 11, 10 * 1_000)
        session.add(
            TransactionItem(
                transaction_id=txn.id,
                product_id=product.id,
                qty=10.0,
                unit_price_paise=1_000,
                unit_cost_paise=unit_cost,
                line_total_paise=10 * 1_000,
            )
        )

    for offset in range(1, 31):  # recent 30 complete days
        line(TODAY - timedelta(days=offset), recent_unit_cost)
    for offset in range(31, 91):  # prior 60 complete days
        line(TODAY - timedelta(days=offset), prior_unit_cost)
    session.commit()


def test_margin_leak_reports_the_exact_points_lost() -> None:
    """Cost 800 -> 860 on a ₹10 shelf price: 20% margin becomes 14%, a 6-point slip."""
    session = make_session()
    merchant = make_merchant(session)
    seed_margin(session, merchant, recent_unit_cost=860, prior_unit_cost=800)

    draft = MarginLeakEngine().run(ctx_for(session, merchant, ist_at(TODAY, 12, 0)))[0]
    metrics = draft.metrics

    assert metrics["category"] == "staples"
    assert metrics["prior_margin_pct"] == pytest.approx(20.0)
    assert metrics["recent_margin_pct"] == pytest.approx(14.0)
    assert metrics["drop_pp"] == pytest.approx(6.0)
    assert metrics["recent_revenue_paise"] == 30 * 10 * 1_000
    assert metrics["recent_lines"] == 30
    # 6% of ₹3,000 of recent sales
    assert draft.impact_paise == 18_000
    assert draft.severity is Severity.HIGH


def test_margin_leak_ignores_a_slip_under_two_points() -> None:
    session = make_session()
    merchant = make_merchant(session)
    seed_margin(session, merchant, recent_unit_cost=810, prior_unit_cost=800)  # 1pp
    assert MarginLeakEngine().run(ctx_for(session, merchant, ist_at(TODAY, 12, 0))) == []


def test_margin_leak_ignores_a_category_with_trivial_volume() -> None:
    session = make_session()
    merchant = make_merchant(session)
    product = Product(
        merchant_id=merchant.id,
        sku="RARE",
        name="Rare item",
        category="household",
        cost_price_paise=800,
        sell_price_paise=1_000,
        stock_qty=5.0,
    )
    session.add(product)
    session.flush()
    for offset, cost in ((5, 900), (40, 800)):
        txn = sale(session, merchant.id, TODAY - timedelta(days=offset), 11, 1_000)
        session.add(
            TransactionItem(
                transaction_id=txn.id,
                product_id=product.id,
                qty=1.0,
                unit_price_paise=1_000,
                unit_cost_paise=cost,
                line_total_paise=1_000,
            )
        )
    session.commit()
    assert MarginLeakEngine().run(ctx_for(session, merchant, ist_at(TODAY, 12, 0))) == []


# ── empty database ──────────────────────────────────────────────────────────


def test_every_sales_engine_is_silent_on_an_empty_database() -> None:
    session = make_session()
    merchant = make_merchant(session)
    session.commit()
    ctx = ctx_for(session, merchant, ist_at(TODAY, 12, 0))
    for engine in (
        CollectionAnomalyEngine(),
        PeakHourEngine(),
        PaymentMixEngine(),
        MarginLeakEngine(),
    ):
        assert engine.run(ctx) == [], type(engine).__name__
