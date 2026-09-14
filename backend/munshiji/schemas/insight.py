"""Ranked insight feed."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from munshiji.db.enums import InsightKind, Severity
from munshiji.db.models import Insight
from munshiji.schemas.common import ApiModel, Meta, Money, money

__all__ = ["InsightListOut", "InsightOut"]


class InsightOut(ApiModel):
    id: str
    kind: InsightKind
    severity: Severity
    title_en: str
    title_hi: str
    body_en: str
    body_hi: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    suggested_tool: str | None = None
    suggested_params: dict[str, Any] = Field(default_factory=dict)
    impact: Money
    confidence: float = 0.0
    score: float = 0.0
    status: str = "open"
    created_at: datetime

    @classmethod
    def from_model(cls, insight: Insight) -> InsightOut:
        return cls(
            id=insight.id,
            kind=insight.kind,
            severity=insight.severity,
            title_en=insight.title_en,
            title_hi=insight.title_hi,
            body_en=insight.body_en,
            body_hi=insight.body_hi,
            metrics=insight.metrics or {},
            suggested_tool=insight.suggested_tool,
            suggested_params=insight.suggested_params or {},
            impact=money(insight.impact_paise),
            confidence=insight.confidence,
            score=insight.score,
            status=insight.status,
            created_at=insight.created_at,
        )


class InsightListOut(ApiModel):
    insights: list[InsightOut] = Field(default_factory=list)
    total_impact: Money
    meta: Meta = Field(default_factory=Meta)
