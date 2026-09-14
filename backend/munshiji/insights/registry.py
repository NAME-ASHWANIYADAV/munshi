"""Runs every engine, deduplicates, ranks and persists.

Two properties matter more than anything clever here:

* **One broken engine must never silence the others.** A KeyError in the margin code at 9 a.m. on
  demo day must cost one card in the feed, not the feed.
* **Refreshing must not duplicate.** The same finding seen twice is one insight with a new
  timestamp, not two rows fighting for the merchant's attention — so a refresh supersedes the
  previous open row carrying the same ``dedupe_key``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from munshiji.clock import now_ist
from munshiji.db.models import Insight
from munshiji.insights.base import InsightContext, InsightDraft, InsightEngine
from munshiji.insights.credit import UdhaarOverdueEngine
from munshiji.insights.customers import DormantCustomerEngine, NewCustomerDropEngine
from munshiji.insights.inventory import DeadStockEngine, ExpiryRiskEngine, StockoutRiskEngine
from munshiji.insights.sales import (
    CollectionAnomalyEngine,
    MarginLeakEngine,
    PaymentMixEngine,
    PeakHourEngine,
)
from munshiji.insights.seasonal import FestivalPrepEngine
from munshiji.logging import get_logger

__all__ = [
    "ALL_ENGINES",
    "OPEN",
    "SUPERSEDED",
    "build_context",
    "refresh",
    "run_all",
]

logger = get_logger(__name__)

OPEN = "open"
SUPERSEDED = "superseded"

#: Every lens MunshiJi looks through, in a stable order so a tie in score ranks deterministically.
ALL_ENGINES: list[InsightEngine] = [
    CollectionAnomalyEngine(),
    UdhaarOverdueEngine(),
    DormantCustomerEngine(),
    StockoutRiskEngine(),
    ExpiryRiskEngine(),
    DeadStockEngine(),
    MarginLeakEngine(),
    FestivalPrepEngine(),
    PaymentMixEngine(),
    NewCustomerDropEngine(),
    PeakHourEngine(),
]


def build_context(
    session: Session, merchant_id: str, *, as_of: datetime | None = None
) -> InsightContext:
    """An :class:`InsightContext` anchored at ``as_of`` (default: now, IST)."""
    return InsightContext(session=session, merchant_id=merchant_id, as_of=as_of or now_ist())


def run_all(
    ctx: InsightContext, *, engines: list[InsightEngine] | None = None
) -> list[InsightDraft]:
    """Run every engine and return a deduplicated, score-ranked list of drafts.

    Per-engine exceptions are logged with a traceback and swallowed — the remaining engines still
    produce their findings. Duplicate ``dedupe_key`` values keep the higher-scoring draft.
    """
    selected = ALL_ENGINES if engines is None else engines
    collected: list[InsightDraft] = []
    for engine in selected:
        name = type(engine).__name__
        try:
            produced = engine.run(ctx) or []
        except Exception:
            logger.exception("insight engine %s failed; continuing without it", name)
            continue
        logger.debug("engine %s produced %d draft(s)", name, len(produced))
        collected.extend(produced)

    best: dict[str, InsightDraft] = {}
    for draft in collected:
        current = best.get(draft.dedupe_key)
        if current is None or draft.score > current.score:
            best[draft.dedupe_key] = draft

    return sorted(best.values(), key=lambda draft: (-draft.score, draft.dedupe_key))


def refresh(
    session: Session,
    merchant_id: str,
    *,
    as_of: datetime | None = None,
    engines: list[InsightEngine] | None = None,
) -> list[Insight]:
    """Run every engine and persist the result, superseding the previous generation.

    Any open insight sharing a ``dedupe_key`` with a new draft is moved to ``superseded`` rather
    than deleted, so the history of what MunshiJi said — and when — stays auditable. Open rows
    whose problem no longer reproduces are left alone to age out via their ``expires_at``.
    """
    ctx = build_context(session, merchant_id, as_of=as_of)
    drafts = run_all(ctx, engines=engines)
    if not drafts:
        session.commit()
        return []

    keys = [draft.dedupe_key for draft in drafts]
    previous = session.scalars(
        select(Insight).where(
            Insight.merchant_id == merchant_id,
            Insight.status == OPEN,
            Insight.dedupe_key.in_(keys),
        )
    ).all()
    for stale in previous:
        stale.status = SUPERSEDED

    rows = [draft.to_model(merchant_id) for draft in drafts]
    session.add_all(rows)
    session.commit()
    logger.info(
        "insights refreshed merchant=%s new=%d superseded=%d", merchant_id, len(rows), len(previous)
    )
    return sorted(rows, key=lambda insight: (-insight.score, insight.dedupe_key))
