"""Contract between the insight engines and everything that consumes them.

An engine turns raw shop data into an :class:`InsightDraft`: a bilingual, scored finding that
already knows which tool would act on it. That last part is what lets the agent move from
"here is a problem" to "shall I fix it?" in a single conversational turn.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.orm import Session

from munshiji.clock import now_ist
from munshiji.db.enums import SEVERITY_WEIGHT, InsightKind, Severity
from munshiji.db.models import Insight
from munshiji.money import rupees

__all__ = [
    "SCORE_SCALE",
    "InsightContext",
    "InsightDraft",
    "InsightEngine",
    "compute_score",
    "score_stored",
]

#: Tuned so a ₹5,000 medium-severity finding lands near 50, and a ₹50,000 high-severity
#: one saturates the scale.
SCORE_SCALE = 8.0


def compute_score(
    severity: Severity,
    confidence: float,
    impact_paise: int,
    *,
    age_days: float = 0.0,
) -> float:
    """Rank a finding on 0–100.

    ``severity_weight × confidence × log1p(impact_in_rupees) × SCORE_SCALE``, decayed ~5% per day
    of age so a stale insight sinks beneath a fresh one of equal size.
    """
    impact_rupees = max(0.0, float(rupees(max(0, impact_paise))))
    magnitude = math.log1p(impact_rupees)
    weight = SEVERITY_WEIGHT.get(severity, 1.0)
    raw = weight * max(0.0, min(1.0, confidence)) * magnitude * SCORE_SCALE
    decayed = raw * (0.95 ** max(0.0, age_days))
    return round(max(0.0, min(100.0, decayed)), 2)


def score_stored(insight: Insight, *, at: datetime | None = None) -> float:
    """Re-rank a persisted insight, applying recency decay from its creation time."""
    moment = at or now_ist()
    age_days = max(0.0, (moment - insight.created_at).total_seconds() / 86_400)
    return compute_score(
        insight.severity, insight.confidence, insight.impact_paise, age_days=age_days
    )


@dataclass(slots=True)
class InsightDraft:
    """A finding, before it is persisted.

    ``metrics`` must contain every number the agent might speak, so the voice layer never has to
    re-query. ``suggested_tool`` / ``suggested_params`` turn the finding into a one-step action.
    """

    kind: InsightKind
    severity: Severity
    title_en: str
    title_hi: str
    body_en: str
    body_hi: str
    metrics: dict[str, Any] = field(default_factory=dict)
    suggested_tool: str | None = None
    suggested_params: dict[str, Any] = field(default_factory=dict)
    impact_paise: int = 0
    confidence: float = 0.7
    dedupe_key: str = ""
    expires_in_days: int | None = 3

    def __post_init__(self) -> None:
        if not self.dedupe_key:
            self.dedupe_key = self.kind.value

    @property
    def score(self) -> float:
        return compute_score(self.severity, self.confidence, self.impact_paise)

    def to_model(self, merchant_id: str) -> Insight:
        """Materialise as an ORM row (not added to a session)."""
        from datetime import timedelta

        from munshiji.clock import now_utc

        expires_at = (
            now_utc() + timedelta(days=self.expires_in_days) if self.expires_in_days else None
        )
        return Insight(
            merchant_id=merchant_id,
            kind=self.kind,
            severity=self.severity,
            title_en=self.title_en,
            title_hi=self.title_hi,
            body_en=self.body_en,
            body_hi=self.body_hi,
            metrics=self.metrics,
            suggested_tool=self.suggested_tool,
            suggested_params=self.suggested_params,
            impact_paise=self.impact_paise,
            confidence=self.confidence,
            score=self.score,
            dedupe_key=self.dedupe_key,
            status="open",
            expires_at=expires_at,
        )


@dataclass(slots=True)
class InsightContext:
    """Everything an engine needs, plus a scratch cache so engines can share expensive frames.

    ``as_of`` is IST-aware and is the *only* notion of "now" an engine may use — this is what
    makes the suite reproducible against a seeded database.
    """

    session: Session
    merchant_id: str
    as_of: datetime
    cache: dict[str, Any] = field(default_factory=dict)

    def cached(self, key: str, factory: Any) -> Any:
        """Memoise a derived frame for the duration of one insight run."""
        if key not in self.cache:
            self.cache[key] = factory()
        return self.cache[key]


@runtime_checkable
class InsightEngine(Protocol):
    """One analytical lens over the merchant's data."""

    kind: InsightKind

    def run(self, ctx: InsightContext) -> list[InsightDraft]:
        """Produce zero or more findings. Must not mutate the database."""
        ...
