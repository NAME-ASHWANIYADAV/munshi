"""Sales tools — what came in, how it compares, and who brought it."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from munshiji.agent.tools.base import Tool, ToolContext, ToolResult
from munshiji.clock import ist_date_of, weekday_name
from munshiji.db.models import Customer, Transaction
from munshiji.money import fmt_inr, fmt_pct, pct_change
from munshiji.repositories import analytics

__all__ = ["TOOLS"]

Period = Literal["today", "yesterday", "last_7_days", "this_week", "this_month", "last_month"]


def _period_bounds(period: str, today: date) -> tuple[date, date, str, str]:
    """Resolve a period name to an inclusive IST day range plus bilingual labels."""
    if period == "today":
        return today, today, "today", "आज"
    if period == "yesterday":
        day = today - timedelta(days=1)
        return day, day, "yesterday", "कल"
    if period == "last_7_days":
        return today - timedelta(days=6), today, "the last 7 days", "पिछले 7 दिन"
    if period == "this_week":
        return (
            today - timedelta(days=today.weekday()),
            today,
            "this week",
            "इस हफ़्ते",
        )
    if period == "this_month":
        return today.replace(day=1), today, "this month", "इस महीने"
    if period == "last_month":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return (
            last_prev.replace(day=1),
            last_prev,
            "last month",
            "पिछले महीने",
        )
    return today, today, "today", "आज"


def _totals(ctx: ToolContext, first_day: date, last_day: date) -> tuple[int, int]:
    frame = analytics.daily_revenue(
        ctx.session, ctx.merchant_id, first_day, last_day, zero_fill=False
    )
    return sum(d.revenue_paise for d in frame.values()), sum(d.txn_count for d in frame.values())


# ── get_sales_summary ───────────────────────────────────────────────────────


class SalesSummaryParams(BaseModel):
    period: Period = Field(default="today", description="Which window to summarise.")


async def _sales_summary(ctx: ToolContext, params: SalesSummaryParams) -> ToolResult:
    today = ist_date_of(ctx.as_of)
    first_day, last_day, label_en, label_hi = _period_bounds(params.period, today)
    total, count = _totals(ctx, first_day, last_day)
    average = int(round(total / count)) if count else 0

    data = {
        "period": params.period,
        "period_label_en": label_en,
        "period_label_hi": label_hi,
        "from": first_day.isoformat(),
        "to": last_day.isoformat(),
        "collection_paise": total,
        "collection_display": fmt_inr(total),
        "transactions": count,
        "average_ticket_paise": average,
        "average_ticket_display": fmt_inr(average),
    }

    # For today specifically, add the live projection and the weekday baseline — the numbers that
    # make an answer mid-afternoon useful rather than merely accurate.
    if params.period == "today":
        baseline = analytics.weekday_baseline(ctx.session, ctx.merchant_id, as_of=ctx.as_of)
        if baseline.sample_count:
            delta = pct_change(total, baseline.median_paise)
            data["baseline_paise"] = int(round(baseline.median_paise))
            data["baseline_display"] = fmt_inr(int(round(baseline.median_paise)))
            data["baseline_weekday"] = weekday_name(today)
            data["baseline_weekday_hi"] = weekday_name(today, hindi=True)
            data["baseline_samples"] = baseline.sample_count
            data["delta_pct"] = round(delta, 1) if delta is not None else None
            data["delta_display"] = fmt_pct(delta)

        profile = analytics.intraday_profile(ctx.session, ctx.merchant_id, as_of=ctx.as_of)
        projection = analytics.project_close(profile, total, at=ctx.as_of)
        data["day_progress_pct"] = round(projection.elapsed_share * 100, 1)
        if projection.too_early:
            data["projection"] = None
            data["projection_note_en"] = "too early in the day to project a close"
            data["projection_note_hi"] = "अभी दिन शुरू ही " "हुआ है, अनुमान " "लगाना जल्दी होगा"
        else:
            data["projected_close_paise"] = projection.projected_paise
            data["projected_close_display"] = fmt_inr(projection.projected_paise or 0)
            data["projected_range_display"] = (
                f"{fmt_inr(projection.band_low_paise or 0)} – "
                f"{fmt_inr(projection.band_high_paise or 0)}"
            )

    return ToolResult(
        data=data,
        summary_en=f"{label_en}: {fmt_inr(total)} across {count} sales",
        summary_hi=f"{label_hi}: {fmt_inr(total)}, {count} bikri",
    )


# ── compare_sales ───────────────────────────────────────────────────────────


class CompareSalesParams(BaseModel):
    period_a: Period = Field(default="today", description="The period being asked about.")
    period_b: Period = Field(default="yesterday", description="The period to compare against.")


async def _compare_sales(ctx: ToolContext, params: CompareSalesParams) -> ToolResult:
    today = ist_date_of(ctx.as_of)
    a_first, a_last, a_en, a_hi = _period_bounds(params.period_a, today)
    b_first, b_last, b_en, b_hi = _period_bounds(params.period_b, today)

    a_total, a_count = _totals(ctx, a_first, a_last)
    b_total, b_count = _totals(ctx, b_first, b_last)
    delta = pct_change(a_total, b_total)

    return ToolResult(
        data={
            "a": {
                "period": params.period_a,
                "label_en": a_en,
                "label_hi": a_hi,
                "collection_paise": a_total,
                "collection_display": fmt_inr(a_total),
                "transactions": a_count,
            },
            "b": {
                "period": params.period_b,
                "label_en": b_en,
                "label_hi": b_hi,
                "collection_paise": b_total,
                "collection_display": fmt_inr(b_total),
                "transactions": b_count,
            },
            "difference_paise": a_total - b_total,
            "difference_display": fmt_inr(abs(a_total - b_total)),
            "direction": "up" if a_total >= b_total else "down",
            "delta_pct": round(delta, 1) if delta is not None else None,
            "delta_display": fmt_pct(delta),
        },
        summary_en=f"{a_en} {fmt_inr(a_total)} vs {b_en} {fmt_inr(b_total)} ({fmt_pct(delta)})",
        summary_hi=f"{a_hi} {fmt_inr(a_total)}, {b_hi} {fmt_inr(b_total)} ({fmt_pct(delta)})",
    )


# ── get_top_customers ───────────────────────────────────────────────────────


class TopCustomersParams(BaseModel):
    limit: int = Field(default=5, ge=1, le=20, description="How many customers to return.")
    by: Literal["spend", "visits"] = Field(
        default="spend", description="Rank by lifetime spend or by number of visits."
    )
    days: int = Field(
        default=30, ge=1, le=365, description="Look back this many days; 0 uses lifetime totals."
    )


async def _top_customers(ctx: ToolContext, params: TopCustomersParams) -> ToolResult:
    today = ist_date_of(ctx.as_of)
    first_day = today - timedelta(days=params.days - 1)
    from munshiji.clock import range_bounds_ist

    start, end = range_bounds_ist(first_day, today)

    metric = (
        func.coalesce(func.sum(Transaction.amount_paise), 0)
        if params.by == "spend"
        else func.count(Transaction.id)
    )
    rows = ctx.session.execute(
        select(
            Customer.id,
            Customer.name,
            Customer.phone,
            func.coalesce(func.sum(Transaction.amount_paise), 0).label("spend"),
            func.count(Transaction.id).label("visits"),
        )
        .join(Transaction, Transaction.customer_id == Customer.id)
        .where(
            Customer.merchant_id == ctx.merchant_id,
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
            Transaction.is_return.is_(False),
        )
        .group_by(Customer.id)
        .order_by(metric.desc())
        .limit(params.limit)
    ).all()

    customers = [
        {
            "customer_id": row.id,
            "name": row.name,
            "phone": row.phone,
            "spend_paise": int(row.spend),
            "spend_display": fmt_inr(int(row.spend)),
            "visits": int(row.visits),
        }
        for row in rows
    ]
    total = sum(item["spend_paise"] for item in customers)

    top_names = ", ".join(item["name"] for item in customers[:3]) or "none"
    return ToolResult(
        data={
            "customers": customers,
            "window_days": params.days,
            "ranked_by": params.by,
            "combined_spend_paise": total,
            "combined_spend_display": fmt_inr(total),
        },
        summary_en=f"Top {len(customers)} by {params.by} ({params.days}d): {top_names}",
        summary_hi=f"{params.days} din ke top {len(customers)} grahak: {top_names}",
    )


TOOLS: list[Tool] = [
    Tool(
        name="get_sales_summary",
        description=(
            "Collection, sale count and average ticket for a period. For 'today' it also returns "
            "the same-weekday baseline, the percentage difference, and a projected closing total "
            "with a confidence range. Use this for any question about how the day or week is going."
        ),
        params_model=SalesSummaryParams,
        handler=_sales_summary,
        label_en="Sales summary",
        label_hi="Aaj ka hisaab",
    ),
    Tool(
        name="compare_sales",
        description=(
            "Compare collection between two periods, returning both totals, the difference and "
            "the percentage change. Use when the merchant asks 'vs yesterday' or 'vs last week'."
        ),
        params_model=CompareSalesParams,
        handler=_compare_sales,
        label_en="Compare periods",
        label_hi="Tulna",
    ),
    Tool(
        name="get_top_customers",
        description=(
            "The merchant's highest-value customers over a recent window, by spend or by visit "
            "count, with their phone numbers."
        ),
        params_model=TopCustomersParams,
        handler=_top_customers,
        label_en="Top customers",
        label_hi="Sabse bade grahak",
    ),
]
