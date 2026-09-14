"""Merchant profile and the live dashboard."""

from __future__ import annotations

from fastapi import APIRouter

from munshiji.agent.snapshot import build_dashboard
from munshiji.api.deps import CurrentMerchant, DbSession
from munshiji.clock import now_ist
from munshiji.insights.health import assess_health
from munshiji.schemas.merchant import DashboardOut, HealthScoreOut, MerchantOut

router = APIRouter(tags=["merchant"])


@router.get("/merchant/{merchant_id}", response_model=MerchantOut, summary="Merchant profile")
async def get_merchant(merchant: CurrentMerchant) -> MerchantOut:
    """Profile for one merchant. Pass ``default`` to get the only merchant in a seeded database."""
    return MerchantOut.model_validate(merchant)


@router.get(
    "/merchant/{merchant_id}/dashboard",
    response_model=DashboardOut,
    summary="Today's numbers, projection and headline counts",
)
async def get_dashboard(merchant: CurrentMerchant, session: DbSession) -> DashboardOut:
    """Everything the companion screen shows above the fold.

    The projection is computed from this shop's own historical intraday curve and refuses to
    project before enough of the trading day has elapsed.
    """
    return build_dashboard(session, merchant, now_ist())


@router.get(
    "/merchant/{merchant_id}/health",
    response_model=HealthScoreOut,
    summary="Explainable merchant-health signal",
)
async def get_health(merchant: CurrentMerchant, session: DbSession) -> HealthScoreOut:
    """Score the shop across revenue, credit, retention, inventory and digital maturity.

    This is the by-product that answers "why would Paytm care": a merchant who uses MunshiJi is
    continuously producing the evidence a lender needs about a shop with no audited accounts.
    Every dimension carries its own number and a sentence explaining it, because a score a credit
    officer cannot interrogate is one they will not use.

    A signal, not a credit decision.
    """
    return HealthScoreOut.model_validate(
        assess_health(session, merchant.id, as_of=now_ist()).as_dict()
    )
