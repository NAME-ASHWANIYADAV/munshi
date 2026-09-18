"""The khata page: open udhaar, aged and prioritised.

One route, no reshaping — it serves exactly what :func:`udhaar_ledger` computes, which is the
same computation the conversational tool answers from. The page and the spoken answer can
never disagree about who owes what.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from munshiji.agent.tools.credit import udhaar_ledger
from munshiji.api.deps import CurrentMerchant, DbSession
from munshiji.clock import ist_date_of, now_ist

router = APIRouter(tags=["khata"])


@router.get(
    "/khata/{merchant_id}",
    summary="Open udhaar: total, aging buckets and the prioritised debtor list",
)
async def get_khata(merchant: CurrentMerchant, session: DbSession) -> dict[str, Any]:
    """Aging buckets (0-15 / 16-30 / 31-60 / 60+ days) plus every open debtor, chase-ordered."""
    return {
        "merchant_id": merchant.id,
        **udhaar_ledger(session, merchant.id, today=ist_date_of(now_ist())),
    }
