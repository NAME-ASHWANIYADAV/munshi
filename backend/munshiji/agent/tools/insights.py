"""Insight tools — what MunshiJi thinks is worth the merchant's attention right now."""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, Field
from sqlalchemy import select

from munshiji.agent.tools.base import Tool, ToolContext, ToolResult
from munshiji.clock import days_between, now_utc
from munshiji.db.enums import InsightKind
from munshiji.db.models import Insight
from munshiji.logging import get_logger
from munshiji.money import fmt_inr
from munshiji.repositories.core import get_customers, list_insights

__all__ = ["TOOLS", "ensure_insights"]

logger = get_logger(__name__)

#: Recompute rather than serve findings older than this.
STALE_AFTER = timedelta(minutes=20)


def ensure_insights(ctx: ToolContext) -> list[Insight]:
    """Return the ranked feed, recomputing it when it is missing or stale.

    The engines live behind a defensive import so a failure in one analytical module degrades the
    feed rather than the conversation.
    """
    newest = ctx.session.scalars(
        select(Insight)
        .where(Insight.merchant_id == ctx.merchant_id, Insight.status == "open")
        .order_by(Insight.created_at.desc())
        .limit(1)
    ).first()

    fresh_enough = newest is not None and (now_utc() - newest.created_at) < STALE_AFTER
    if not fresh_enough:
        try:
            from munshiji.insights.registry import refresh

            refresh(ctx.session, ctx.merchant_id, as_of=ctx.as_of)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("insight refresh failed, serving what is stored: %s", exc)

    return list_insights(ctx.session, ctx.merchant_id, limit=12)


def _serialise(insight: Insight) -> dict[str, object]:
    return {
        "id": insight.id,
        "kind": insight.kind.value,
        "severity": insight.severity.value,
        "title_en": insight.title_en,
        "title_hi": insight.title_hi,
        "body_en": insight.body_en,
        "body_hi": insight.body_hi,
        "metrics": insight.metrics or {},
        "impact_paise": insight.impact_paise,
        "impact_display": fmt_inr(insight.impact_paise),
        "confidence": round(insight.confidence, 2),
        "score": round(insight.score, 1),
        "suggested_tool": insight.suggested_tool,
        "suggested_params": insight.suggested_params or {},
    }


class InsightsParams(BaseModel):
    limit: int = Field(default=3, ge=1, le=10, description="How many findings to return.")
    kind: InsightKind | None = Field(default=None, description="Restrict to one kind of finding.")


async def _get_insights(ctx: ToolContext, params: InsightsParams) -> ToolResult:
    insights = ensure_insights(ctx)
    if params.kind is not None:
        insights = [item for item in insights if item.kind == params.kind]
    selected = insights[: params.limit]

    total_impact = sum(item.impact_paise for item in selected)
    headline = selected[0].title_hi if selected else ""

    return ToolResult(
        data={
            "insights": [_serialise(item) for item in selected],
            "count": len(selected),
            "total_impact_paise": total_impact,
            "total_impact_display": fmt_inr(total_impact),
        },
        summary_en=(
            f"{len(selected)} findings worth {fmt_inr(total_impact)}"
            if selected
            else "Nothing needs attention right now"
        ),
        summary_hi=headline or "Abhi sab theek hai",
    )


class DormantParams(BaseModel):
    limit: int = Field(default=50, ge=1, le=100, description="Maximum customers to return.")


async def _find_dormant(ctx: ToolContext, params: DormantParams) -> ToolResult:
    """Customers who are overdue *by their own visiting rhythm*, not a global threshold.

    ``count`` is deliberately the number MunshiJi would actually contact, not the number found:
    it is the figure the merchant hears, and it must match what the offer then sends. The wider
    population is reported separately as ``dormant_count``.
    """
    insights = ensure_insights(ctx)
    dormant = next((item for item in insights if item.kind == InsightKind.DORMANT_CUSTOMERS), None)

    if dormant is None:
        return ToolResult(
            data={"customers": [], "count": 0, "dormant_count": 0, "winback_value_paise": 0},
            summary_en="No dormant regulars found",
            summary_hi="Koi purana grahak chhuta nahi hai",
        )

    metrics = dormant.metrics or {}
    suggested = dormant.suggested_params or {}
    customer_ids = list(suggested.get("customer_ids") or [])[: params.limit]
    recoverable = int(metrics.get("recoverable_paise", dormant.impact_paise))

    # Names come from the database rather than the insight payload, so the reply can address real
    # people and the list can never drift from who would actually be contacted.
    people = get_customers(ctx.session, customer_ids)
    customers = [
        {
            "customer_id": person.id,
            "name": person.name,
            "phone": person.phone,
            "last_seen_days": (
                days_between(person.last_seen_at, ctx.as_of) if person.last_seen_at else None
            ),
            "lifetime_spend_paise": person.total_spend_paise,
            "visits": person.txn_count,
        }
        for person in people
    ]

    return ToolResult(
        data={
            "count": len(customer_ids),
            "targeted_count": len(customer_ids),
            "dormant_count": int(metrics.get("dormant_count", len(customer_ids))),
            "customer_ids": customer_ids,
            "customers": customers,
            "winback_value_paise": recoverable,
            "winback_value_display": fmt_inr(recoverable),
            "rule": metrics.get("rule", ""),
            "insight_id": dormant.id,
            "suggested_discount_pct": suggested.get("discount_pct"),
            "suggested_valid_days": suggested.get("valid_days"),
        },
        summary_en=f"{len(customer_ids)} dormant regulars, {fmt_inr(recoverable)} recoverable",
        summary_hi=(
            f"{len(customer_ids)} purane grahak nahi aa rahe, "
            f"{fmt_inr(recoverable)} wapas aa sakta hai"
        ),
    )


class HealthParams(BaseModel):
    """No parameters: the score is always the whole shop, as of now."""


async def _merchant_health(ctx: ToolContext, _params: HealthParams) -> ToolResult:
    from munshiji.insights.health import assess_health

    health = assess_health(ctx.session, ctx.merchant_id, as_of=ctx.as_of)
    weakest = health.weakest

    return ToolResult(
        data={
            **health.as_dict(),
            # The composer speaks one number and one reason; the rest is for the screen.
            "count": int(round(health.score)),
            "headline": weakest.reason_hi
            if weakest and ctx.language.startswith("hi")
            else (weakest.reason_en if weakest else ""),
        },
        summary_en=f"Health {health.score:.0f}/100 ({health.band})",
        summary_hi=f"Dukaan ki sehat {health.score:.0f}/100 — {health.band_hi}",
    )


TOOLS: list[Tool] = [
    Tool(
        name="get_merchant_health",
        description=(
            "An explainable health score for the whole shop (0-100) across revenue trend, credit "
            "discipline, customer retention, inventory efficiency and digital maturity, with the "
            "evidence behind each. Use when the merchant asks how the shop is doing overall, how "
            "it looks to a lender, or whether they would qualify for a loan."
        ),
        params_model=HealthParams,
        handler=_merchant_health,
        label_en="Shop health",
        label_hi="Dukaan ki sehat",
    ),
    Tool(
        name="get_insights",
        description=(
            "The ranked list of what needs the merchant's attention right now — collection "
            "anomalies, dormant customers, stock and udhaar problems — each with its numbers and "
            "a suggested action. Use when the merchant asks what to do, or for a general check-in."
        ),
        params_model=InsightsParams,
        handler=_get_insights,
        label_en="What needs attention",
        label_hi="Kya dhyaan dena hai",
    ),
    Tool(
        name="find_dormant_customers",
        description=(
            "Regular customers who have stopped coming, judged against each customer's own visit "
            "rhythm rather than a fixed number of days, with the revenue a win-back could recover "
            "and the customer ids needed to send an offer."
        ),
        params_model=DormantParams,
        handler=_find_dormant,
        label_en="Dormant regulars",
        label_hi="Jo grahak nahi aa rahe",
    ),
]
