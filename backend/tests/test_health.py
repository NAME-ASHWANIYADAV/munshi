"""Merchant health scoring.

The score is the product's answer to "why would Paytm care", so it has to survive being
questioned: every dimension must explain itself, the weights must be honest, and the thing must
respond to the data rather than sitting at a flattering constant.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from munshiji.clock import now_utc
from munshiji.db.enums import KhataStatus
from munshiji.db.models import KhataEntry, Merchant
from munshiji.insights.health import (
    DIMENSION_WEIGHTS,
    assess_health,
    band_for,
)
from munshiji.seed.generator import generate


@pytest.fixture
def shop(session: Session) -> Merchant:
    generate(session, days=90)
    session.commit()
    return session.scalars(select(Merchant)).one()


def test_weights_sum_to_one() -> None:
    """Otherwise the composite is not on the 0-100 scale it claims to be."""
    assert sum(DIMENSION_WEIGHTS.values()) == pytest.approx(1.0)


def test_every_dimension_explains_itself(session: Session, shop: Merchant) -> None:
    health = assess_health(session, shop.id)

    assert set(DIMENSION_WEIGHTS) == {dimension.key for dimension in health.dimensions}
    for dimension in health.dimensions:
        assert 0.0 <= dimension.score <= 100.0
        assert dimension.evidence, f"{dimension.key} has no evidence"
        assert dimension.reason_en and dimension.reason_hi
        # A credit officer has to be able to see the number behind the score.
        assert dimension.metrics, f"{dimension.key} exposes no metrics"


def test_composite_equals_the_weighted_parts(session: Session, shop: Merchant) -> None:
    health = assess_health(session, shop.id)
    expected = sum(dimension.contribution for dimension in health.dimensions)
    assert health.score == pytest.approx(expected, abs=0.11)
    assert 0.0 <= health.score <= 100.0


def test_bands_are_ordered_and_cover_the_range() -> None:
    assert band_for(95.0)[0] == "strong"
    assert band_for(65.0)[0] == "steady"
    assert band_for(50.0)[0] == "watch"
    assert band_for(10.0)[0] == "strained"
    assert band_for(0.0)[0] == "strained"


def test_credit_discipline_falls_when_the_book_goes_bad(session: Session, shop: Merchant) -> None:
    """The dimension has to move with the evidence, or it is decoration."""
    before = next(
        d for d in assess_health(session, shop.id).dimensions if d.key == "credit_discipline"
    )

    # Pile on old, unsettled credit: a large book, most of it long past due.
    customer_id = session.scalars(
        select(KhataEntry.customer_id).where(KhataEntry.merchant_id == shop.id).limit(1)
    ).one()
    for _ in range(30):
        session.add(
            KhataEntry(
                merchant_id=shop.id,
                customer_id=customer_id,
                amount_paise=900_000,
                opened_at=now_utc() - timedelta(days=150),
                due_at=now_utc() - timedelta(days=120),
                status=KhataStatus.OPEN,
            )
        )
    session.commit()

    after = next(
        d for d in assess_health(session, shop.id).dimensions if d.key == "credit_discipline"
    )
    assert (
        after.score < before.score - 20
    ), f"credit discipline barely moved: {before.score:.1f} -> {after.score:.1f}"


def test_weakest_dimension_is_the_lowest_scoring_one(session: Session, shop: Merchant) -> None:
    health = assess_health(session, shop.id)
    weakest = health.weakest
    assert weakest is not None
    assert weakest.score == min(dimension.score for dimension in health.dimensions)


def test_an_empty_shop_scores_without_dividing_by_zero(
    session: Session, merchant: Merchant
) -> None:
    """No transactions at all must produce a score, not an exception."""
    health = assess_health(session, merchant.id)
    assert 0.0 <= health.score <= 100.0
    assert len(health.dimensions) == len(DIMENSION_WEIGHTS)


def test_payload_is_serialisable_and_labels_both_languages(
    session: Session, shop: Merchant
) -> None:
    payload = assess_health(session, shop.id).as_dict()
    assert payload["band"] and payload["band_hi"]
    assert payload["weakest_dimension"] in DIMENSION_WEIGHTS
    for dimension in payload["dimensions"]:
        assert dimension["label_hi"] != dimension["label_en"]
