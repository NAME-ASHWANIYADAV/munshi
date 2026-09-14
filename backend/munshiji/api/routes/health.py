"""Health and provider status.

The dashboard's status strip reads this to show, live, which of the three sponsor platforms is
serving each capability — and which are running on the offline fallback.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from munshiji import __version__
from munshiji.api.deps import AppSettings, DbSession, Providers
from munshiji.db.models import Merchant
from munshiji.logging import get_logger
from munshiji.schemas.health import HealthOut, ProviderStatusOut

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


@router.get("/health", response_model=HealthOut, summary="Service and provider status")
async def health(session: DbSession, settings: AppSettings, providers: Providers) -> HealthOut:
    """Probe every provider concurrently and report which implementation is serving each."""
    report = await providers.health()

    merchant_id: str | None = None
    database_ready = False
    try:
        merchant_id = session.scalar(select(Merchant.id).limit(1))
        database_ready = True
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("database probe failed: %s", exc)

    everything_ok = all(entry.ok for entry in report) and database_ready
    return HealthOut(
        status="ok" if everything_ok else "degraded",
        version=__version__,
        env=settings.env,
        provider_mode=settings.provider_mode,
        providers=[ProviderStatusOut(**entry.as_dict()) for entry in report],
        sponsors=providers.sponsor_status,
        database_ready=database_ready,
        merchant_id=merchant_id,
    )
