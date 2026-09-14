"""The ranked insight feed."""

from __future__ import annotations

from fastapi import APIRouter, Query

from munshiji.api.deps import CurrentMerchant, DbSession
from munshiji.clock import now_ist
from munshiji.events import EventName, get_event_bus
from munshiji.logging import get_logger
from munshiji.repositories.core import list_insights
from munshiji.schemas.common import Meta, money
from munshiji.schemas.insight import InsightListOut, InsightOut

router = APIRouter(tags=["insights"])
logger = get_logger(__name__)


def _to_payload(insights: list, latency_ms: int = 0) -> InsightListOut:
    items = [InsightOut.from_model(insight) for insight in insights]
    total = sum(insight.impact_paise for insight in insights)
    return InsightListOut(
        insights=items,
        total_impact=money(total),
        meta=Meta(latency_ms=latency_ms, extra={"count": len(items)}),
    )


@router.get(
    "/insights/{merchant_id}",
    response_model=InsightListOut,
    summary="Ranked findings for this merchant",
)
async def get_insights(
    merchant: CurrentMerchant,
    session: DbSession,
    limit: int = Query(default=12, ge=1, le=50),
) -> InsightListOut:
    """Stored findings, highest score first. Use the refresh endpoint to recompute."""
    return _to_payload(list_insights(session, merchant.id, limit=limit))


@router.post(
    "/insights/{merchant_id}/refresh",
    response_model=InsightListOut,
    summary="Recompute every insight engine",
)
async def refresh_insights(
    merchant: CurrentMerchant,
    session: DbSession,
    limit: int = Query(default=12, ge=1, le=50),
) -> InsightListOut:
    """Run all engines against current data, superseding previous findings of the same kind."""
    started = now_ist()
    try:
        from munshiji.insights.registry import refresh

        refresh(session, merchant.id, as_of=started)
    except Exception as exc:  # pragma: no cover - a broken engine degrades the feed, not the API
        logger.warning("insight refresh failed: %s", exc)

    insights = list_insights(session, merchant.id, limit=limit)
    latency = int((now_ist() - started).total_seconds() * 1000)

    get_event_bus().publish(merchant.id, EventName.INSIGHTS_REFRESHED, {"count": len(insights)})
    return _to_payload(insights, latency)
