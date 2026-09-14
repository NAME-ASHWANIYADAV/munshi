"""Customer engines.

The centrepiece is :func:`test_per_customer_cadence_beats_a_global_threshold`, which builds four
customers a global 30-day rule gets *backwards* in two of the four cases, and shows the cadence
rule getting all four right.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from munshiji.clock import IST, to_utc
from munshiji.db.base import Base
from munshiji.db.enums import CustomerSegment, InsightKind
from munshiji.db.models import Customer, Merchant, Transaction
from munshiji.insights.base import InsightContext
from munshiji.insights.customers import (
    ABSOLUTE_DORMANCY_FLOOR_DAYS,
    RETURN_PRIOR_BY_SEGMENT,
    WINBACK_WINDOW_DAYS,
    DormantCustomerEngine,
    NewCustomerDropEngine,
)
from munshiji.insights.stats import trend_slope
from munshiji.repositories.analytics import compute_rfm, segment_for, visit_histories

TODAY = date(2026, 9, 14)
RUPEE = 100
GLOBAL_RULE_DAYS = 30  # the naive threshold this engine deliberately does not use


def make_session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def at(day: date, hour: int = 12) -> datetime:
    return to_utc(datetime.combine(day, time(hour, 0), tzinfo=IST))


def ist_at(day: date, hour: int = 12) -> datetime:
    return datetime.combine(day, time(hour, 0), tzinfo=IST)


def make_merchant(session: Session) -> Merchant:
    merchant = Merchant(owner_name="Sharma ji", shop_name="Sharma General Store")
    session.add(merchant)
    session.flush()
    return merchant


def add_customer(
    session: Session,
    merchant: Merchant,
    name: str,
    *,
    last_visit_days_ago: int,
    gap_days: int,
    visits: int,
    ticket_rupees: int = 500,
) -> Customer:
    """A customer with a perfectly regular cadence, last seen ``last_visit_days_ago`` ago."""
    customer = Customer(merchant_id=merchant.id, name=name)
    session.add(customer)
    session.flush()
    last = TODAY - timedelta(days=last_visit_days_ago)
    for index in range(visits):
        day = last - timedelta(days=gap_days * index)
        session.add(
            Transaction(
                merchant_id=merchant.id,
                customer_id=customer.id,
                amount_paise=ticket_rupees * RUPEE,
                occurred_at=at(day, 11),
            )
        )
    session.flush()
    return customer


def ctx_for(session: Session, merchant: Merchant) -> InsightContext:
    return InsightContext(session=session, merchant_id=merchant.id, as_of=ist_at(TODAY, 12))


# ── the headline: cadence vs a global threshold ─────────────────────────────


def build_cadence_fixture(session: Session, merchant: Merchant) -> dict[str, Customer]:
    """Four customers chosen so a global 30-day rule is wrong about half of them.

    =========  =========  ==================  =========  ==============  ==============
    Customer   Cadence    Days since visit    Own fence  Cadence rule    30-day rule
    =========  =========  ==================  =========  ==============  ==============
    Anita      3 days     4                   10         not dormant     not dormant
    Bhavna     30 days    60                  30         DORMANT         dormant
    Chandni    45 days    40                  45         not dormant     dormant  (wrong)
    Deepa      3 days     25                  10         DORMANT         not dormant (wrong)
    =========  =========  ==================  =========  ==============  ==============
    """
    return {
        "anita": add_customer(
            session, merchant, "Anita", last_visit_days_ago=4, gap_days=3, visits=12
        ),
        "bhavna": add_customer(
            session, merchant, "Bhavna", last_visit_days_ago=60, gap_days=30, visits=5
        ),
        "chandni": add_customer(
            session, merchant, "Chandni", last_visit_days_ago=40, gap_days=45, visits=4
        ),
        "deepa": add_customer(
            session, merchant, "Deepa", last_visit_days_ago=25, gap_days=3, visits=6
        ),
    }


def test_per_customer_cadence_beats_a_global_threshold() -> None:
    session = make_session()
    merchant = make_merchant(session)
    people = build_cadence_fixture(session, merchant)
    session.commit()

    found = DormantCustomerEngine().candidates(ctx_for(session, merchant))
    dormant_ids = {candidate.customer_id for candidate in found}

    assert people["bhavna"].id in dormant_ids
    assert people["deepa"].id in dormant_ids
    assert people["anita"].id not in dormant_ids
    assert people["chandni"].id not in dormant_ids

    # And explicitly: the global rule would have got Chandni and Deepa backwards.
    histories = visit_histories(session, merchant.id, as_of=ist_at(TODAY, 12))
    global_rule = {
        customer_id
        for customer_id, history in histories.items()
        if history.days_since_last(TODAY) > GLOBAL_RULE_DAYS
    }
    assert people["chandni"].id in global_rule and people["chandni"].id not in dormant_ids
    assert people["deepa"].id not in global_rule and people["deepa"].id in dormant_ids


def test_cadence_thresholds_are_the_documented_formula() -> None:
    session = make_session()
    merchant = make_merchant(session)
    build_cadence_fixture(session, merchant)
    session.commit()

    by_name = {
        candidate.name: candidate
        for candidate in DormantCustomerEngine().candidates(ctx_for(session, merchant))
    }
    # Perfectly regular gaps give IQR = 0, so the fence is the median gap itself,
    # floored at ABSOLUTE_DORMANCY_FLOOR_DAYS.
    assert by_name["Bhavna"].median_gap_days == 30.0
    assert by_name["Bhavna"].gap_iqr_days == 0.0
    assert by_name["Bhavna"].threshold_days == 30.0
    assert by_name["Bhavna"].days_since_last == 60

    assert by_name["Deepa"].median_gap_days == 3.0
    assert by_name["Deepa"].threshold_days == float(ABSOLUTE_DORMANCY_FLOOR_DAYS)
    assert by_name["Deepa"].days_since_last == 25


def test_a_daily_shopper_is_never_flagged_by_the_floor() -> None:
    """Someone who buys every day and missed a long weekend must not be 'dormant'."""
    session = make_session()
    merchant = make_merchant(session)
    add_customer(session, merchant, "Everyday", last_visit_days_ago=4, gap_days=1, visits=30)
    session.commit()
    assert DormantCustomerEngine().candidates(ctx_for(session, merchant)) == []


def test_two_visits_is_too_little_history_to_judge() -> None:
    session = make_session()
    merchant = make_merchant(session)
    add_customer(session, merchant, "Barely", last_visit_days_ago=200, gap_days=5, visits=2)
    session.commit()
    assert DormantCustomerEngine().candidates(ctx_for(session, merchant)) == []


# ── win-back value ──────────────────────────────────────────────────────────


def test_winback_value_is_prior_times_ticket_times_expected_visits() -> None:
    session = make_session()
    merchant = make_merchant(session)
    build_cadence_fixture(session, merchant)
    session.commit()

    by_name = {
        candidate.name: candidate
        for candidate in DormantCustomerEngine().candidates(ctx_for(session, merchant))
    }
    for candidate in by_name.values():
        assert candidate.avg_ticket_paise == 500 * RUPEE
        assert candidate.return_prior == RETURN_PRIOR_BY_SEGMENT[candidate.segment]
        assert candidate.recoverable_paise == round(
            candidate.return_prior * candidate.avg_ticket_paise * candidate.expected_visits
        )

    # expected_visits = clamp(30 / median_gap, 1, 4)
    assert by_name["Bhavna"].expected_visits == pytest.approx(WINBACK_WINDOW_DAYS / 30)
    assert by_name["Deepa"].expected_visits == pytest.approx(4.0)  # 30/3 clipped at 4


def test_dormant_draft_targets_and_sums_the_cohort() -> None:
    session = make_session()
    merchant = make_merchant(session)
    build_cadence_fixture(session, merchant)
    session.commit()

    ctx = ctx_for(session, merchant)
    engine = DormantCustomerEngine()
    draft = engine.run(ctx)[0]

    assert draft.kind is InsightKind.DORMANT_CUSTOMERS
    assert draft.suggested_tool == "send_winback_offer"
    assert len(draft.suggested_params["customer_ids"]) == 2
    assert draft.suggested_params["valid_days"] > 0
    assert 0 < draft.suggested_params["discount_pct"] <= 50
    assert draft.impact_paise == sum(row["recoverable_paise"] for row in draft.metrics["customers"])
    assert draft.metrics["dormant_count"] == 2
    assert draft.title_hi and draft.body_hi
    assert any("ऀ" <= char <= "ॿ" for char in draft.body_hi)  # Devanagari


def test_ranking_is_by_recoverable_value() -> None:
    session = make_session()
    merchant = make_merchant(session)
    add_customer(
        session,
        merchant,
        "Small",
        last_visit_days_ago=60,
        gap_days=20,
        visits=5,
        ticket_rupees=100,
    )
    add_customer(
        session,
        merchant,
        "Big",
        last_visit_days_ago=60,
        gap_days=20,
        visits=5,
        ticket_rupees=2_000,
    )
    session.commit()

    found = DormantCustomerEngine().candidates(ctx_for(session, merchant))
    assert [candidate.name for candidate in found] == ["Big", "Small"]
    assert found[0].recoverable_paise > found[1].recoverable_paise


# ── RFM ─────────────────────────────────────────────────────────────────────


def test_rfm_grid_maps_the_documented_corners() -> None:
    assert segment_for(5, 5, 5, frequency=20) is CustomerSegment.CHAMPION
    assert segment_for(1, 1, 1, frequency=2) is CustomerSegment.LOST
    assert segment_for(1, 5, 5, frequency=20) is CustomerSegment.AT_RISK  # valuable, gone quiet
    assert segment_for(2, 1, 1, frequency=3) is CustomerSegment.DORMANT
    assert segment_for(5, 1, 1, frequency=1) is CustomerSegment.NEW  # one visit override


def test_rfm_scores_every_customer_and_inverts_recency() -> None:
    session = make_session()
    merchant = make_merchant(session)
    people = build_cadence_fixture(session, merchant)
    session.commit()

    scores = compute_rfm(ctx_for(session, merchant))
    assert set(scores) == {customer.id for customer in people.values()}
    # Anita came in 4 days ago, Bhavna 60: the more recent buyer must score the higher R.
    assert scores[people["anita"].id].r > scores[people["bhavna"].id].r
    assert scores[people["anita"].id].recency_days == 4
    assert scores[people["bhavna"].id].recency_days == 60
    assert all(1 <= score.r <= 5 for score in scores.values())
    assert all(1 <= score.f <= 5 for score in scores.values())
    assert all(1 <= score.m <= 5 for score in scores.values())


def test_rfm_frame_is_cached_on_the_context() -> None:
    session = make_session()
    merchant = make_merchant(session)
    build_cadence_fixture(session, merchant)
    session.commit()
    ctx = ctx_for(session, merchant)
    assert compute_rfm(ctx) is compute_rfm(ctx)


# ── new customer drop ───────────────────────────────────────────────────────

#: First-time buyers per week, oldest first. Hand-computed slope: numerator -35, denominator 42,
#: so trend_slope == -35/42 == -0.8333 per week.
WEEKLY_NEW = [6, 6, 5, 4, 3, 2, 1, 1]


def test_hand_computed_trend_slope() -> None:
    assert trend_slope([float(value) for value in WEEKLY_NEW]) == pytest.approx(-35 / 42)


def test_new_customer_drop_reads_the_weekly_series() -> None:
    session = make_session()
    merchant = make_merchant(session)
    for bucket, count in enumerate(WEEKLY_NEW):
        day = TODAY - timedelta(days=7 * (8 - bucket) - 1)
        for index in range(count):
            customer = Customer(merchant_id=merchant.id, name=f"New {bucket}-{index}")
            session.add(customer)
            session.flush()
            session.add(
                Transaction(
                    merchant_id=merchant.id,
                    customer_id=customer.id,
                    amount_paise=200 * RUPEE,
                    occurred_at=at(day, 11),
                )
            )
    session.commit()

    draft = NewCustomerDropEngine().run(ctx_for(session, merchant))[0]
    metrics = draft.metrics

    assert metrics["weekly_counts"] == WEEKLY_NEW
    assert metrics["early_mean"] == pytest.approx(5.25)
    assert metrics["late_mean"] == pytest.approx(1.75)
    assert metrics["drop_pct"] == pytest.approx(66.67, abs=0.01)
    assert metrics["trend_slope_per_week"] == pytest.approx(-0.833, abs=1e-3)
    assert metrics["avg_first_ticket_paise"] == 200 * RUPEE
    # shortfall 3.5/week x 4 weeks x ₹200
    assert draft.impact_paise == round(3.5 * 4 * 200 * RUPEE)


def test_new_customer_drop_silent_on_a_steady_funnel() -> None:
    session = make_session()
    merchant = make_merchant(session)
    for bucket in range(8):
        day = TODAY - timedelta(days=7 * (8 - bucket) - 1)
        for index in range(4):
            customer = Customer(merchant_id=merchant.id, name=f"Steady {bucket}-{index}")
            session.add(customer)
            session.flush()
            session.add(
                Transaction(
                    merchant_id=merchant.id,
                    customer_id=customer.id,
                    amount_paise=200 * RUPEE,
                    occurred_at=at(day, 11),
                )
            )
    session.commit()
    assert NewCustomerDropEngine().run(ctx_for(session, merchant)) == []


# ── walk-ins and empty database ─────────────────────────────────────────────


def test_walk_ins_have_no_cadence_and_are_ignored() -> None:
    session = make_session()
    merchant = make_merchant(session)
    for offset in (5, 40, 80, 120):
        session.add(
            Transaction(
                merchant_id=merchant.id,
                customer_id=None,
                amount_paise=300 * RUPEE,
                occurred_at=at(TODAY - timedelta(days=offset), 11),
            )
        )
    session.commit()
    assert DormantCustomerEngine().run(ctx_for(session, merchant)) == []
    assert compute_rfm(ctx_for(session, merchant)) == {}


def test_every_customer_engine_is_silent_on_an_empty_database() -> None:
    session = make_session()
    merchant = make_merchant(session)
    session.commit()
    ctx = ctx_for(session, merchant)
    for engine in (DormantCustomerEngine(), NewCustomerDropEngine()):
        assert engine.run(ctx) == [], type(engine).__name__
