"""Today's live picture of the shop.

Two shapes of the same computation: a flat block of display strings injected into the model's
prompt, and the full :class:`DashboardOut` the companion screen renders. Both come from one pass
over the analytics frames so the number MunshiJi speaks and the number on screen can never disagree.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from munshiji.clock import ist_date_of, now_ist, to_ist, weekday_name
from munshiji.db.enums import ActionStatus, InsightKind, KhataStatus
from munshiji.db.models import ActionRequest, Insight, KhataEntry, Merchant, Product, Transaction
from munshiji.insights.stats import robust_z
from munshiji.money import fmt_inr, fmt_pct, pct_change
from munshiji.repositories import analytics
from munshiji.schemas.common import money
from munshiji.schemas.merchant import (
    DashboardOut,
    MerchantOut,
    PaymentMixSlice,
    SparkPoint,
    TodaySnapshot,
)

__all__ = ["SPARKLINE_DAYS", "build_dashboard", "build_prompt_snapshot"]

SPARKLINE_DAYS = 14

#: Payment methods are shown in this order so the stacked bar stays stable between refreshes.
_MIX_ORDER = ("upi", "soundbox", "cash", "card", "wallet")


def _today_frame(session: Session, merchant_id: str, as_of: datetime) -> tuple[int, int]:
    """Collection (paise) and sale count for ``as_of``'s IST day, up to this moment."""
    today = ist_date_of(as_of)
    frame = analytics.daily_revenue(session, merchant_id, today, today, zero_fill=False)
    day = frame.get(today)
    return (day.revenue_paise, day.txn_count) if day else (0, 0)


def _unique_customers_today(session: Session, merchant_id: str, day: date) -> int:
    from munshiji.clock import day_bounds_ist

    start, end = day_bounds_ist(day)
    return int(
        session.scalar(
            select(func.count(func.distinct(Transaction.customer_id))).where(
                Transaction.merchant_id == merchant_id,
                Transaction.occurred_at >= start,
                Transaction.occurred_at < end,
                Transaction.customer_id.is_not(None),
                Transaction.is_return.is_(False),
            )
        )
        or 0
    )


def _customer_split_today(session: Session, merchant_id: str, day: date) -> tuple[int, int]:
    """(repeat, new) among today's named buyers.

    New means first-ever recorded visit is today; everyone else who bought today is a repeat.
    The merchant's own instinct for this number is exact — a wrong split would be noticed in
    one glance, so it is computed from the transaction table, not estimated.
    """
    from munshiji.clock import day_bounds_ist

    start, end = day_bounds_ist(day)
    today_ids = set(
        session.scalars(
            select(func.distinct(Transaction.customer_id)).where(
                Transaction.merchant_id == merchant_id,
                Transaction.occurred_at >= start,
                Transaction.occurred_at < end,
                Transaction.customer_id.is_not(None),
                Transaction.is_return.is_(False),
            )
        ).all()
    )
    if not today_ids:
        return (0, 0)
    seen_before = set(
        session.scalars(
            select(func.distinct(Transaction.customer_id)).where(
                Transaction.merchant_id == merchant_id,
                Transaction.occurred_at < start,
                Transaction.customer_id.in_(today_ids),
                Transaction.is_return.is_(False),
            )
        ).all()
    )
    repeat = len(today_ids & seen_before)
    return (repeat, len(today_ids) - repeat)


def _open_udhaar(session: Session, merchant_id: str) -> tuple[int, int]:
    """Total outstanding credit (paise) and the number of open entries."""
    entries = session.scalars(
        select(KhataEntry).where(
            KhataEntry.merchant_id == merchant_id,
            KhataEntry.status.in_([KhataStatus.OPEN, KhataStatus.PARTIAL]),
        )
    ).all()
    return sum(entry.outstanding_paise for entry in entries), len(entries)


def _low_stock_count(session: Session, merchant_id: str) -> int:
    return int(
        session.scalar(
            select(func.count(Product.id)).where(
                Product.merchant_id == merchant_id,
                Product.reorder_level > 0,
                Product.stock_qty <= Product.reorder_level,
            )
        )
        or 0
    )


def _pending_action_count(session: Session, merchant_id: str) -> int:
    return int(
        session.scalar(
            select(func.count(ActionRequest.id)).where(
                ActionRequest.merchant_id == merchant_id,
                ActionRequest.status == ActionStatus.PENDING_APPROVAL,
            )
        )
        or 0
    )


def _dormant_count(session: Session, merchant_id: str) -> int:
    """Read the dormancy figure off the stored insight, so screen and voice agree.

    Recomputing it here would duplicate the per-customer cadence rule and risk drifting from it.
    """
    insight = session.scalars(
        select(Insight)
        .where(
            Insight.merchant_id == merchant_id,
            Insight.kind == InsightKind.DORMANT_CUSTOMERS,
            Insight.status == "open",
        )
        .order_by(Insight.created_at.desc())
        .limit(1)
    ).first()
    if insight is None:
        return 0
    return int((insight.metrics or {}).get("dormant_count", 0))


def _collect(session: Session, merchant: Merchant, as_of: datetime) -> dict[str, Any]:
    """One pass over the analytics frames; everything else is formatting."""
    today = ist_date_of(as_of)
    collected_paise, txn_count = _today_frame(session, merchant.id, as_of)

    baseline = analytics.weekday_baseline(session, merchant.id, as_of=as_of)
    baseline_paise = int(round(baseline.median_paise)) if baseline.sample_count else None
    delta = (
        pct_change(collected_paise, baseline.median_paise)
        if baseline.sample_count and baseline.median_paise
        else None
    )
    z_score = (
        robust_z(float(collected_paise), baseline.median_paise, baseline.mad_paise)
        if baseline.sample_count >= 4
        else None
    )

    profile = analytics.intraday_profile(session, merchant.id, as_of=as_of)
    projection = analytics.project_close(profile, collected_paise, at=as_of)

    first_spark_day = today - timedelta(days=SPARKLINE_DAYS - 1)
    spark_frame = analytics.daily_revenue(session, merchant.id, first_spark_day, today)
    mix = analytics.payment_mix(
        session, merchant.id, first_day=today - timedelta(days=13), last_day=today
    )

    udhaar_paise, udhaar_count = _open_udhaar(session, merchant.id)

    return {
        "today": today,
        "collected_paise": collected_paise,
        "txn_count": txn_count,
        "unique_customers": _unique_customers_today(session, merchant.id, today),
        "customer_split": _customer_split_today(session, merchant.id, today),
        "baseline_paise": baseline_paise,
        "baseline_samples": baseline.sample_count,
        "delta_pct": delta,
        "robust_z": z_score,
        "projection": projection,
        "spark_frame": spark_frame,
        "mix": mix,
        "udhaar_paise": udhaar_paise,
        "udhaar_count": udhaar_count,
        "low_stock": _low_stock_count(session, merchant.id),
        "dormant": _dormant_count(session, merchant.id),
        "pending_actions": _pending_action_count(session, merchant.id),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Prompt view
# ─────────────────────────────────────────────────────────────────────────────


def build_prompt_snapshot(
    session: Session, merchant: Merchant, as_of: datetime | None = None
) -> dict[str, Any]:
    """Flat, pre-formatted facts for the system prompt.

    Values are strings because the model must quote them verbatim — handing it raw integers invites
    it to do arithmetic of its own, which is exactly what we forbid.
    """
    as_of = as_of or now_ist()
    frame = _collect(session, merchant, as_of)
    projection = frame["projection"]
    today: date = frame["today"]

    snapshot: dict[str, Any] = {
        "date": today.isoformat(),
        "weekday": f"{weekday_name(today)} / {weekday_name(today, hindi=True)}",
        "time_now_ist": to_ist(as_of).strftime("%H:%M"),
        "collection_so_far": fmt_inr(frame["collected_paise"]),
        "sales_count_today": str(frame["txn_count"]),
        "named_customers_today": str(frame["unique_customers"]),
    }

    if frame["baseline_paise"] is not None:
        snapshot["same_weekday_baseline"] = (
            f"{fmt_inr(frame['baseline_paise'])} "
            f"(median of last {frame['baseline_samples']} {weekday_name(today)}s)"
        )
    if frame["delta_pct"] is not None:
        snapshot["vs_baseline"] = fmt_pct(frame["delta_pct"])
    if frame["robust_z"] is not None:
        snapshot["robust_z_score"] = f"{frame['robust_z']:.2f}"

    if projection.too_early:
        snapshot["projected_close"] = (
            "too early to project — only "
            f"{projection.elapsed_share * 100:.0f}% of the trading day has elapsed"
        )
    elif projection.projected_paise is not None:
        snapshot["projected_close"] = fmt_inr(projection.projected_paise)
        if projection.band_low_paise is not None and projection.band_high_paise is not None:
            snapshot["projected_close_range"] = (
                f"{fmt_inr(projection.band_low_paise)} – {fmt_inr(projection.band_high_paise)}"
            )

    if frame["udhaar_count"]:
        snapshot["open_udhaar"] = (
            f"{fmt_inr(frame['udhaar_paise'])} across {frame['udhaar_count']} khata entries"
        )
    if frame["dormant"]:
        snapshot["dormant_regulars"] = str(frame["dormant"])
    if frame["low_stock"]:
        snapshot["items_at_or_below_reorder_level"] = str(frame["low_stock"])
    if frame["pending_actions"]:
        snapshot["actions_awaiting_your_yes"] = str(frame["pending_actions"])

    return snapshot


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard view
# ─────────────────────────────────────────────────────────────────────────────


def build_dashboard(
    session: Session, merchant: Merchant, as_of: datetime | None = None
) -> DashboardOut:
    """The full companion-screen payload."""
    as_of = as_of or now_ist()
    frame = _collect(session, merchant, as_of)
    today: date = frame["today"]
    projection = frame["projection"]
    collected = frame["collected_paise"]
    txn_count = frame["txn_count"]

    average_ticket = int(round(collected / txn_count)) if txn_count else 0

    sparkline = [
        SparkPoint(
            day=day.day,
            weekday=weekday_name(day.day),
            collection=money(day.revenue_paise),
            transactions=day.txn_count,
            is_today=day.day == today,
        )
        for day in sorted(frame["spark_frame"].values(), key=lambda d: d.day)
    ]

    mix = frame["mix"]
    payment_mix = [
        PaymentMixSlice(
            method=method,
            share_pct=round(mix.revenue_share(method) * 100, 1),
            amount=money(mix.revenue_paise.get(method, 0)),
        )
        for method in _MIX_ORDER
        if mix.revenue_paise.get(method)
    ]

    snapshot = TodaySnapshot(
        day=today,
        weekday_en=weekday_name(today),
        weekday_hi=weekday_name(today, hindi=True),
        collected=money(collected),
        transactions=txn_count,
        unique_customers=frame["unique_customers"],
        repeat_customers=frame["customer_split"][0],
        new_customers=frame["customer_split"][1],
        average_ticket=money(average_ticket),
        projected_close=money(projection.projected_paise)
        if projection.projected_paise is not None
        else None,
        projection_confidence=round(min(1.0, projection.sample_days / 8.0), 2)
        if not projection.too_early
        else 0.0,
        baseline=money(frame["baseline_paise"]) if frame["baseline_paise"] is not None else None,
        delta_pct=round(frame["delta_pct"], 2) if frame["delta_pct"] is not None else None,
        robust_z=round(frame["robust_z"], 2) if frame["robust_z"] is not None else None,
        day_progress=round(projection.elapsed_share, 4),
        is_too_early_to_project=projection.too_early,
    )

    return DashboardOut(
        merchant=MerchantOut.model_validate(merchant),
        today=snapshot,
        sparkline=sparkline,
        payment_mix=payment_mix,
        open_udhaar=money(frame["udhaar_paise"]),
        open_udhaar_count=frame["udhaar_count"],
        low_stock_count=frame["low_stock"],
        dormant_customer_count=frame["dormant"],
        pending_action_count=frame["pending_actions"],
        generated_at=to_ist(as_of),
    )
