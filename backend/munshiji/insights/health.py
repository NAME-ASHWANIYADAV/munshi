"""Merchant health: the signal a lender would actually want.

Everything else in MunshiJi argues the merchant's case — here is what is wrong, here is what to
do about it. This module answers the other question: *why would Paytm care?*

Paytm does not make its money selling dashboards to kirana owners. It makes it on payments,
device subscriptions and distributing credit. The hardest part of lending to a shop with no
audited accounts is knowing whether it is a good shop. A merchant who talks to MunshiJi every
day is, as a side effect, producing exactly the evidence that question needs — revenue trend,
whether customers come back, how disciplined the shop is with its own credit book, whether stock
turns, and how much of the business is digitally observable at all.

So the score is a by-product, not a new feature: the same engines, read as an underwriting
signal. Five dimensions, each 0–100 and each explaining itself in one sentence, because a score
a credit officer cannot interrogate is a score they will not use.

**It is a signal, not a decision.** Nothing here approves or prices credit, and the bands are
deliberately named for attention rather than for risk grades.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from munshiji.clock import ist_date_of, now_ist
from munshiji.db.enums import KhataStatus
from munshiji.db.models import KhataEntry
from munshiji.insights.stats import safe_div
from munshiji.money import fmt_inr
from munshiji.repositories import analytics

__all__ = ["DIMENSION_WEIGHTS", "Dimension", "MerchantHealth", "assess_health", "band_for"]

#: How much each dimension counts. Revenue trend and credit discipline carry the most because
#: they are the two that actually move a repayment outcome; digital maturity counts least on its
#: own, but it is what makes any of the others measurable in the first place.
DIMENSION_WEIGHTS: dict[str, float] = {
    "revenue_trend": 0.28,
    "credit_discipline": 0.26,
    "customer_retention": 0.20,
    "inventory_efficiency": 0.14,
    "digital_maturity": 0.12,
}

#: Score bands. Named for what a human should do, not for a risk grade.
_BANDS: tuple[tuple[float, str, str], ...] = (
    (75.0, "strong", "मजबूत"),
    (60.0, "steady", "ठीक-ठाक"),
    (45.0, "watch", "ध्यान दें"),
    (0.0, "strained", "दबाव में"),
)


def band_for(score: float) -> tuple[str, str]:
    """The (english, hindi) band a score falls in."""
    for threshold, english, hindi in _BANDS:
        if score >= threshold:
            return english, hindi
    return _BANDS[-1][1], _BANDS[-1][2]


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


@dataclass(slots=True)
class Dimension:
    """One axis of the score, with the number behind it and why it landed there."""

    key: str
    label_en: str
    label_hi: str
    score: float
    weight: float
    #: The underlying measurement, already formatted for display.
    evidence: str
    reason_en: str
    reason_hi: str
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def contribution(self) -> float:
        return round(self.score * self.weight, 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label_en": self.label_en,
            "label_hi": self.label_hi,
            "score": round(self.score, 1),
            "weight": self.weight,
            "contribution": self.contribution,
            "evidence": self.evidence,
            "reason_en": self.reason_en,
            "reason_hi": self.reason_hi,
            "metrics": self.metrics,
        }


@dataclass(slots=True)
class MerchantHealth:
    """The composite, its parts, and how it has moved."""

    merchant_id: str
    as_of: datetime
    score: float
    band: str
    band_hi: str
    dimensions: list[Dimension]
    previous_score: float | None = None

    @property
    def delta(self) -> float | None:
        return None if self.previous_score is None else round(self.score - self.previous_score, 1)

    @property
    def weakest(self) -> Dimension | None:
        return min(self.dimensions, key=lambda d: d.score) if self.dimensions else None

    def as_dict(self) -> dict[str, Any]:
        weakest = self.weakest
        return {
            "merchant_id": self.merchant_id,
            "as_of": self.as_of.isoformat(),
            "score": round(self.score, 1),
            "band": self.band,
            "band_hi": self.band_hi,
            "previous_score": round(self.previous_score, 1) if self.previous_score else None,
            "delta": self.delta,
            "weakest_dimension": weakest.key if weakest else None,
            "dimensions": [dimension.as_dict() for dimension in self.dimensions],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Dimensions
# ─────────────────────────────────────────────────────────────────────────────


def _revenue_trend(session: Session, merchant_id: str, as_of: datetime) -> Dimension:
    """Is the shop growing? Recent 30 days against the 30 before it."""
    today = ist_date_of(as_of)
    recent = analytics.daily_revenue(session, merchant_id, today - timedelta(days=29), today)
    prior = analytics.daily_revenue(
        session, merchant_id, today - timedelta(days=59), today - timedelta(days=30)
    )
    recent_total = sum(day.revenue_paise for day in recent.values())
    prior_total = sum(day.revenue_paise for day in prior.values())
    change = safe_div(recent_total - prior_total, prior_total, 0.0)

    # Flat is a pass, not a failure: a steady kirana is a perfectly good credit. +20% saturates.
    score = _clamp(60.0 + change * 200.0)
    pct = change * 100
    return Dimension(
        key="revenue_trend",
        label_en="Revenue trend",
        label_hi="बिक्री का रुख",
        score=score,
        weight=DIMENSION_WEIGHTS["revenue_trend"],
        evidence=f"{fmt_inr(recent_total)} vs {fmt_inr(prior_total)} ({pct:+.1f}%)",
        reason_en=f"Last 30 days ran {pct:+.1f}% against the 30 before.",
        reason_hi=f"पिछले 30 दिन {pct:+.1f}% रहे।",
        metrics={
            "recent_paise": recent_total,
            "prior_paise": prior_total,
            "change_pct": round(pct, 2),
        },
    )


def _credit_discipline(session: Session, merchant_id: str, as_of: datetime) -> Dimension:
    """How the shop runs its own credit book — the closest proxy for how it would run a loan."""
    today = ist_date_of(as_of)
    entries = session.scalars(select(KhataEntry).where(KhataEntry.merchant_id == merchant_id)).all()
    open_entries = [e for e in entries if e.status in (KhataStatus.OPEN, KhataStatus.PARTIAL)]
    settled = [e for e in entries if e.status == KhataStatus.SETTLED and e.settled_at]

    outstanding = sum(e.outstanding_paise for e in open_entries)
    monthly = sum(
        day.revenue_paise
        for day in analytics.daily_revenue(
            session, merchant_id, today - timedelta(days=29), today
        ).values()
    )
    exposure = safe_div(outstanding, monthly, 0.0)
    aged = sum(1 for e in open_entries if (today - ist_date_of(e.due_at or e.opened_at)).days > 60)
    aged_share = safe_div(aged, len(open_entries), 0.0)
    settle_days = [
        (ist_date_of(e.settled_at) - ist_date_of(e.opened_at)).days
        for e in settled  # type: ignore[arg-type]
    ]
    average_settle = sum(settle_days) / len(settle_days) if settle_days else 0.0

    # A credit book is normal; an unmanaged one is not. Penalise exposure above ~15% of turnover,
    # entries left past 60 days, and slow settlement.
    score = _clamp(
        100.0
        - max(0.0, exposure - 0.15) * 220.0
        - aged_share * 45.0
        - max(0.0, average_settle - 21.0) * 0.8
    )
    return Dimension(
        key="credit_discipline",
        label_en="Credit discipline",
        label_hi="उधार का अनुशासन",
        score=score,
        weight=DIMENSION_WEIGHTS["credit_discipline"],
        evidence=(
            f"{fmt_inr(outstanding)} open ({exposure * 100:.0f}% of turnover), "
            f"{aged} past 60 days"
        ),
        reason_en=(
            f"Credit book is {exposure * 100:.0f}% of monthly turnover; "
            f"{aged} of {len(open_entries)} entries are over 60 days; "
            f"settled tabs close in {average_settle:.0f} days on average."
        ),
        reason_hi=(
            f"उधार महीने की " f"बिक्री का {exposure * 100:.0f}%, " f"{aged} खाते 60 दिन से " f"पुराने।"
        ),
        metrics={
            "outstanding_paise": outstanding,
            "exposure_pct": round(exposure * 100, 2),
            "open_entries": len(open_entries),
            "over_60_days": aged,
            "average_settle_days": round(average_settle, 1),
        },
    )


def _customer_retention(session: Session, merchant_id: str, as_of: datetime) -> Dimension:
    """Does the shop keep the people it wins? Repeat share is the whole question."""
    histories = analytics.visit_histories(session, merchant_id, as_of=as_of)
    if not histories:
        return Dimension(
            key="customer_retention",
            label_en="Customer retention",
            label_hi="ग्राहक टिकाव",
            score=50.0,
            weight=DIMENSION_WEIGHTS["customer_retention"],
            evidence="no named customers yet",
            reason_en="Not enough named-customer history to judge retention.",
            reason_hi="अभी पर्याप्त डेटा नहीं।",
        )

    today = ist_date_of(as_of)
    repeat = sum(1 for history in histories.values() if history.visit_count >= 3)
    repeat_share = safe_div(repeat, len(histories), 0.0)
    # A month without a visit is the point a neighbourhood regular has actually gone quiet -
    # the same horizon the dormancy engine works on, so the two cannot disagree.
    lapsed = sum(
        1
        for history in histories.values()
        if history.last_visit and (today - history.last_visit).days > 30
    )
    lapsed_share = safe_div(lapsed, len(histories), 0.0)

    # Winning repeat buyers is most of it; holding on to them is the rest. Deliberately not
    # scaled so that a good shop saturates - a dimension pinned at 100 tells a lender nothing.
    score = _clamp(repeat_share * 72.0 + (1.0 - lapsed_share) * 28.0)
    return Dimension(
        key="customer_retention",
        label_en="Customer retention",
        label_hi="ग्राहक टिकाव",
        score=score,
        weight=DIMENSION_WEIGHTS["customer_retention"],
        evidence=f"{repeat_share * 100:.0f}% repeat, {lapsed_share * 100:.0f}% lapsed",
        reason_en=(
            f"{repeat} of {len(histories)} named customers have bought three times or more; "
            f"{lapsed} have not been in for 45 days."
        ),
        reason_hi=(f"{len(histories)} में से {repeat} ग्राहक " f"बार-बार आते हैं।"),
        metrics={
            "named_customers": len(histories),
            "repeat_share_pct": round(repeat_share * 100, 1),
            "lapsed_share_pct": round(lapsed_share * 100, 1),
        },
    )


def _inventory_efficiency(session: Session, merchant_id: str, as_of: datetime) -> Dimension:
    """Is working capital moving, or sitting on a shelf?"""
    movements = analytics.product_movements(session, merchant_id, as_of=as_of)
    if not movements:
        return Dimension(
            key="inventory_efficiency",
            label_en="Inventory efficiency",
            label_hi="स्टॉक की चाल",
            score=50.0,
            weight=DIMENSION_WEIGHTS["inventory_efficiency"],
            evidence="no stock movement recorded",
            reason_en="No inventory movement to judge.",
            reason_hi="स्टॉक का डेटा नहीं।",
        )

    today = ist_date_of(as_of)
    stock_value = 0
    dead_value = 0
    for movement in movements.values():
        product = movement.product
        value = product.stock_value_paise
        stock_value += value
        quiet = (today - movement.last_sale_day).days if movement.last_sale_day else 999
        if quiet >= 45:
            dead_value += value
    dead_share = safe_div(dead_value, stock_value, 0.0)

    score = _clamp(100.0 - dead_share * 260.0)
    return Dimension(
        key="inventory_efficiency",
        label_en="Inventory efficiency",
        label_hi="स्टॉक की चाल",
        score=score,
        weight=DIMENSION_WEIGHTS["inventory_efficiency"],
        evidence=f"{fmt_inr(dead_value)} idle of {fmt_inr(stock_value)}",
        reason_en=(
            f"{dead_share * 100:.0f}% of stock value has not sold in 45 days "
            f"({fmt_inr(dead_value)} of capital)."
        ),
        reason_hi=(f"{dead_share * 100:.0f}% स्टॉक 45 दिन " f"से नहीं बिका।"),
        metrics={
            "stock_value_paise": stock_value,
            "dead_value_paise": dead_value,
            "dead_share_pct": round(dead_share * 100, 1),
        },
    )


def _digital_maturity(session: Session, merchant_id: str, as_of: datetime) -> Dimension:
    """How much of the business is visible at all — the precondition for everything above."""
    today = ist_date_of(as_of)
    mix = analytics.payment_mix(
        session, merchant_id, first_day=today - timedelta(days=29), last_day=today
    )
    digital = sum(
        mix.revenue_paise.get(method, 0) for method in ("upi", "soundbox", "card", "wallet")
    )
    digital_share = safe_div(digital, mix.total_paise, 0.0)

    # Straight through: the score *is* the observable share. Anything that flatters it would be
    # claiming visibility into a part of the business nobody can actually see.
    score = _clamp(digital_share * 100.0)
    return Dimension(
        key="digital_maturity",
        label_en="Digital maturity",
        label_hi="डिजिटल हिस्सा",
        score=score,
        weight=DIMENSION_WEIGHTS["digital_maturity"],
        evidence=f"{digital_share * 100:.0f}% of takings are digital",
        reason_en=(
            f"{digital_share * 100:.0f}% of the last 30 days came through digital rails, "
            "so that share of the business is independently observable."
        ),
        reason_hi=(f"पिछले 30 दिन की " f"{digital_share * 100:.0f}% वसूली डिजिटल थी।"),
        metrics={
            "digital_share_pct": round(digital_share * 100, 1),
            "digital_paise": digital,
            "total_paise": mix.total_paise,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────


def assess_health(
    session: Session, merchant_id: str, *, as_of: datetime | None = None
) -> MerchantHealth:
    """Score a merchant across every dimension, with the reasoning attached."""
    as_of = as_of or now_ist()
    dimensions = [
        _revenue_trend(session, merchant_id, as_of),
        _credit_discipline(session, merchant_id, as_of),
        _customer_retention(session, merchant_id, as_of),
        _inventory_efficiency(session, merchant_id, as_of),
        _digital_maturity(session, merchant_id, as_of),
    ]
    score = round(sum(dimension.contribution for dimension in dimensions), 1)

    # The same assessment a month back, so the score carries a direction and not just a level.
    previous: float | None = None
    month_ago = as_of - timedelta(days=30)
    try:
        previous = round(
            sum(
                dimension.contribution
                for dimension in (
                    _revenue_trend(session, merchant_id, month_ago),
                    _credit_discipline(session, merchant_id, month_ago),
                    _customer_retention(session, merchant_id, month_ago),
                    _inventory_efficiency(session, merchant_id, month_ago),
                    _digital_maturity(session, merchant_id, month_ago),
                )
            ),
            1,
        )
    except Exception:  # pragma: no cover - a short history simply has no prior score
        previous = None

    english, hindi = band_for(score)
    return MerchantHealth(
        merchant_id=merchant_id,
        as_of=as_of,
        score=score,
        band=english,
        band_hi=hindi,
        dimensions=dimensions,
        previous_score=previous,
    )
