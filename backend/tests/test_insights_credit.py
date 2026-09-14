"""Udhaar engine: aging boundaries, reliability, chase priority, tone and the cooldown.

Bucket boundaries are tested *at* 15, 30 and 60 days rather than near them, because that is where
an off-by-one silently reclassifies a customer's debt — and the tone they get.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from munshiji.clock import IST, to_utc
from munshiji.db.base import Base
from munshiji.db.enums import KhataStatus, Severity, Tone
from munshiji.db.models import Customer, KhataEntry, Merchant
from munshiji.insights.base import InsightContext
from munshiji.insights.credit import (
    BUCKET_RISK_WEIGHT,
    RELIABILITY_PRIOR,
    SETTLE_REFERENCE_DAYS,
    UdhaarOverdueEngine,
    bucket_for,
    recoverability,
    reliability_score,
    select_tone,
)
from munshiji.repositories.analytics import SettleStats, khata_settle_stats

TODAY = date(2026, 9, 14)
RUPEE = 100


def make_session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def at(day: date, hour: int = 11) -> datetime:
    return to_utc(datetime.combine(day, time(hour, 0), tzinfo=IST))


def ist_at(day: date, hour: int = 12) -> datetime:
    return datetime.combine(day, time(hour, 0), tzinfo=IST)


def make_merchant(session: Session) -> Merchant:
    merchant = Merchant(owner_name="Sharma ji", shop_name="Sharma General Store")
    session.add(merchant)
    session.flush()
    return merchant


def add_customer(session: Session, merchant: Merchant, name: str) -> Customer:
    customer = Customer(merchant_id=merchant.id, name=name, is_khata_customer=True)
    session.add(customer)
    session.flush()
    return customer


def add_khata(
    session: Session,
    merchant: Merchant,
    customer: Customer,
    *,
    rupees: int,
    age_days: int,
    paid_rupees: int = 0,
    status: KhataStatus = KhataStatus.OPEN,
    settled_days_ago: int | None = None,
    due_days_after_open: int | None = 30,
    last_reminder_days_ago: int | None = None,
) -> KhataEntry:
    opened = TODAY - timedelta(days=age_days)
    entry = KhataEntry(
        merchant_id=merchant.id,
        customer_id=customer.id,
        amount_paise=rupees * RUPEE,
        paid_paise=paid_rupees * RUPEE,
        opened_at=at(opened),
        due_at=(
            at(opened + timedelta(days=due_days_after_open))
            if due_days_after_open is not None
            else None
        ),
        settled_at=(
            at(TODAY - timedelta(days=settled_days_ago)) if settled_days_ago is not None else None
        ),
        status=status,
        last_reminder_at=(
            at(TODAY - timedelta(days=last_reminder_days_ago))
            if last_reminder_days_ago is not None
            else None
        ),
    )
    session.add(entry)
    session.flush()
    return entry


def ctx_for(session: Session, merchant: Merchant) -> InsightContext:
    return InsightContext(session=session, merchant_id=merchant.id, as_of=ist_at(TODAY))


# ── aging buckets ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("age_days", "bucket"),
    [
        (0, "0-15"),
        (15, "0-15"),  # inclusive upper edge
        (16, "16-30"),
        (30, "16-30"),  # inclusive upper edge
        (31, "31-60"),
        (60, "31-60"),  # inclusive upper edge
        (61, "60+"),
        (400, "60+"),
    ],
)
def test_aging_bucket_boundaries_are_exact(age_days: int, bucket: str) -> None:
    assert bucket_for(age_days) == bucket


def test_engine_buckets_entries_at_the_exact_boundaries() -> None:
    session = make_session()
    merchant = make_merchant(session)
    for index, age in enumerate((15, 30, 60, 61)):
        customer = add_customer(session, merchant, f"Customer {index}")
        add_khata(session, merchant, customer, rupees=1_000, age_days=age)
    session.commit()

    draft = UdhaarOverdueEngine().run(ctx_for(session, merchant))[0]
    buckets = draft.metrics["buckets"]

    assert [buckets[label]["count"] for label in ("0-15", "16-30", "31-60", "60+")] == [1, 1, 1, 1]
    assert draft.metrics["total_outstanding_paise"] == 4 * 1_000 * RUPEE
    assert draft.metrics["open_count"] == 4
    assert buckets["60+"]["total_paise"] == 1_000 * RUPEE
    assert buckets["60+"]["risk_weight"] == BUCKET_RISK_WEIGHT["60+"]


def test_partial_payments_count_only_the_balance() -> None:
    session = make_session()
    merchant = make_merchant(session)
    customer = add_customer(session, merchant, "Half paid")
    add_khata(
        session,
        merchant,
        customer,
        rupees=1_000,
        paid_rupees=600,
        age_days=40,
        status=KhataStatus.PARTIAL,
    )
    session.commit()

    draft = UdhaarOverdueEngine().run(ctx_for(session, merchant))[0]
    assert draft.metrics["total_outstanding_paise"] == 400 * RUPEE
    assert draft.metrics["chase"][0]["outstanding_paise"] == 400 * RUPEE


# ── reliability ─────────────────────────────────────────────────────────────


def test_no_settlement_history_gets_the_uninformative_prior() -> None:
    assert reliability_score(None) == RELIABILITY_PRIOR
    assert reliability_score(SettleStats("cus", 0, 0.0, 0.0)) == RELIABILITY_PRIOR


def test_reliability_is_shrunk_toward_the_prior() -> None:
    """Four on-time settlements in 2 days each: raw 0.96, shrunk to (4x0.96 + 2x0.5)/6."""
    stats = SettleStats(customer_id="cus", settled_count=4, avg_days_to_settle=2.0, late_share=0.0)
    promptness = 1.0 - 2.0 / SETTLE_REFERENCE_DAYS
    raw = 0.6 * promptness + 0.4 * 1.0
    assert reliability_score(stats) == pytest.approx((4 * raw + 2 * 0.5) / 6, abs=1e-4)

    # A single perfect settlement must not out-score a long clean record.
    one_shot = SettleStats("cus", 1, 0.0, 0.0)
    many = SettleStats("cus", 20, 2.0, 0.0)
    assert reliability_score(one_shot) < reliability_score(many)


def test_recoverability_has_a_floor_and_a_ceiling() -> None:
    assert recoverability(0.0) == pytest.approx(0.35)
    assert recoverability(1.0) == pytest.approx(1.0)
    assert recoverability(0.5) < recoverability(0.9)


def test_settle_stats_read_real_history_and_ignore_write_offs() -> None:
    session = make_session()
    merchant = make_merchant(session)
    good = add_customer(session, merchant, "Prompt")
    add_khata(
        session,
        merchant,
        good,
        rupees=500,
        age_days=50,
        status=KhataStatus.SETTLED,
        settled_days_ago=48,
    )
    add_khata(
        session,
        merchant,
        good,
        rupees=500,
        age_days=100,
        status=KhataStatus.SETTLED,
        settled_days_ago=96,
    )
    forgiven = add_customer(session, merchant, "Written off")
    add_khata(session, merchant, forgiven, rupees=900, age_days=300, status=KhataStatus.WRITTEN_OFF)
    session.commit()

    stats = khata_settle_stats(session, merchant.id)
    assert set(stats) == {good.id}
    assert stats[good.id].settled_count == 2
    assert stats[good.id].avg_days_to_settle == pytest.approx(3.0)  # 2 days and 4 days
    assert stats[good.id].late_share == 0.0


def test_a_prompt_payer_outranks_a_slow_one_on_the_same_debt() -> None:
    session = make_session()
    merchant = make_merchant(session)
    prompt = add_customer(session, merchant, "Prompt")
    for age in (120, 150, 180):
        add_khata(
            session,
            merchant,
            prompt,
            rupees=500,
            age_days=age,
            status=KhataStatus.SETTLED,
            settled_days_ago=age - 2,
        )
    slow = add_customer(session, merchant, "Slow")
    for age in (120, 150, 180):
        add_khata(
            session,
            merchant,
            slow,
            rupees=500,
            age_days=age,
            status=KhataStatus.SETTLED,
            settled_days_ago=age - 55,
        )
    add_khata(session, merchant, prompt, rupees=2_000, age_days=40)
    add_khata(session, merchant, slow, rupees=2_000, age_days=40)
    session.commit()

    chase = UdhaarOverdueEngine().run(ctx_for(session, merchant))[0].metrics["chase"]
    by_name = {row["customer_name"]: row for row in chase}
    assert by_name["Prompt"]["reliability"] > by_name["Slow"]["reliability"]
    assert by_name["Prompt"]["chase_priority"] > by_name["Slow"]["chase_priority"]
    assert chase[0]["customer_name"] == "Prompt"


# ── tone ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("bucket", "reliability", "expected"),
    [
        ("0-15", 0.9, Tone.GENTLE),
        ("0-15", 0.5, Tone.GENTLE),
        ("0-15", 0.1, Tone.GENTLE),
        ("16-30", 0.9, Tone.GENTLE),
        ("16-30", 0.5, Tone.STANDARD),
        ("16-30", 0.1, Tone.STANDARD),
        ("31-60", 0.9, Tone.STANDARD),
        ("31-60", 0.5, Tone.STANDARD),
        ("31-60", 0.1, Tone.FIRM),
        ("60+", 0.9, Tone.STANDARD),
        ("60+", 0.5, Tone.FIRM),
        ("60+", 0.1, Tone.FIRM),
    ],
)
def test_tone_table(bucket: str, reliability: float, expected: Tone) -> None:
    assert select_tone(bucket, reliability) is expected


def test_tone_boundaries_sit_where_documented() -> None:
    assert select_tone("16-30", 0.7) is Tone.GENTLE  # >= 0.7 is the reliable band
    assert select_tone("16-30", 0.6999) is Tone.STANDARD
    assert select_tone("60+", 0.4) is Tone.FIRM  # 0.4-0.7 middle band
    assert select_tone("60+", 0.3999) is Tone.FIRM


def test_no_tier_is_harsher_than_firm() -> None:
    tones = {
        select_tone(bucket, reliability)
        for bucket in ("0-15", "16-30", "31-60", "60+")
        for reliability in (0.0, 0.25, 0.5, 0.75, 1.0)
    }
    assert tones <= {Tone.GENTLE, Tone.STANDARD, Tone.FIRM}


# ── cooldown ────────────────────────────────────────────────────────────────


def test_cooldown_suppresses_recently_reminded_entries() -> None:
    session = make_session()
    merchant = make_merchant(session)
    recent = add_customer(session, merchant, "Just reminded")
    add_khata(session, merchant, recent, rupees=5_000, age_days=40, last_reminder_days_ago=2)
    stale = add_customer(session, merchant, "Due a nudge")
    add_khata(session, merchant, stale, rupees=1_000, age_days=40, last_reminder_days_ago=10)
    never = add_customer(session, merchant, "Never reminded")
    add_khata(session, merchant, never, rupees=1_000, age_days=40)
    session.commit()

    draft = UdhaarOverdueEngine(cooldown_days=7).run(ctx_for(session, merchant))[0]
    chased = {row["customer_name"] for row in draft.metrics["chase"]}

    assert chased == {"Due a nudge", "Never reminded"}
    assert draft.metrics["suppressed_by_cooldown"] == 1
    assert draft.metrics["cooldown_days"] == 7
    # The suppressed entry still counts toward the reported position.
    assert draft.metrics["total_outstanding_paise"] == 7_000 * RUPEE
    assert draft.metrics["open_count"] == 3


def test_cooldown_boundary_is_the_configured_window() -> None:
    session = make_session()
    merchant = make_merchant(session)
    customer = add_customer(session, merchant, "Edge")
    add_khata(session, merchant, customer, rupees=1_000, age_days=40, last_reminder_days_ago=7)
    session.commit()

    # Reminded exactly 7 days ago: outside a 7-day cooldown, inside an 8-day one.
    assert (
        UdhaarOverdueEngine(cooldown_days=7)
        .run(ctx_for(session, merchant))[0]
        .metrics["suppressed_by_cooldown"]
        == 0
    )
    assert (
        UdhaarOverdueEngine(cooldown_days=8)
        .run(ctx_for(session, merchant))[0]
        .metrics["suppressed_by_cooldown"]
        == 1
    )


def test_everything_in_cooldown_reports_the_position_but_proposes_nothing() -> None:
    session = make_session()
    merchant = make_merchant(session)
    customer = add_customer(session, merchant, "Recently nudged")
    add_khata(session, merchant, customer, rupees=9_000, age_days=70, last_reminder_days_ago=1)
    session.commit()

    draft = UdhaarOverdueEngine(cooldown_days=7).run(ctx_for(session, merchant))[0]
    assert draft.suggested_tool is None
    assert draft.metrics["chase"] == []
    assert draft.metrics["total_outstanding_paise"] == 9_000 * RUPEE
    assert draft.impact_paise == 0
    assert draft.severity is Severity.INFO


# ── the draft itself ────────────────────────────────────────────────────────


def test_chase_priority_is_amount_times_risk_weight_times_recoverability() -> None:
    session = make_session()
    merchant = make_merchant(session)
    customer = add_customer(session, merchant, "Solo")
    add_khata(session, merchant, customer, rupees=2_000, age_days=70)
    session.commit()

    row = UdhaarOverdueEngine().run(ctx_for(session, merchant))[0].metrics["chase"][0]
    expected = 2_000 * RUPEE * BUCKET_RISK_WEIGHT["60+"] * recoverability(RELIABILITY_PRIOR)
    assert row["reliability"] == pytest.approx(RELIABILITY_PRIOR)
    assert row["chase_priority"] == pytest.approx(expected, abs=0.5)
    assert row["bucket"] == "60+"
    assert row["age_days"] == 70
    assert row["days_overdue"] == 40  # opened 70 days ago on 30-day terms


def test_draft_carries_every_metric_the_voice_layer_speaks() -> None:
    session = make_session()
    merchant = make_merchant(session)
    for index, age in enumerate((10, 20, 45, 80)):
        customer = add_customer(session, merchant, f"Customer {index}")
        add_khata(session, merchant, customer, rupees=2_500, age_days=age)
    session.commit()

    draft = UdhaarOverdueEngine().run(ctx_for(session, merchant))[0]
    metrics = draft.metrics

    for key in (
        "total_outstanding_paise",
        "open_count",
        "buckets",
        "chase",
        "expected_recovery_paise",
        "suppressed_by_cooldown",
    ):
        assert key in metrics, key
    assert draft.suggested_tool == "send_udhaar_reminder"
    assert len(draft.suggested_params["entry_ids"]) == len(metrics["chase"])
    assert all(
        target["tone"] in {t.value for t in Tone} for target in draft.suggested_params["targets"]
    )
    assert draft.impact_paise == sum(row["expected_recovery_paise"] for row in metrics["chase"])
    assert draft.impact_paise < metrics["total_outstanding_paise"]  # expected, not hoped-for
    assert any("ऀ" <= char <= "ॿ" for char in draft.body_hi)


def test_settled_entries_are_not_chased() -> None:
    session = make_session()
    merchant = make_merchant(session)
    customer = add_customer(session, merchant, "All square")
    add_khata(
        session,
        merchant,
        customer,
        rupees=1_000,
        age_days=50,
        status=KhataStatus.SETTLED,
        settled_days_ago=45,
    )
    session.commit()
    assert UdhaarOverdueEngine().run(ctx_for(session, merchant)) == []


def test_udhaar_engine_is_silent_on_an_empty_database() -> None:
    session = make_session()
    merchant = make_merchant(session)
    session.commit()
    assert UdhaarOverdueEngine().run(ctx_for(session, merchant)) == []
