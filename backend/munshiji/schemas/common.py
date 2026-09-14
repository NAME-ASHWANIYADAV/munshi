"""Shared response pieces.

Money crosses the wire twice — as ``*_paise`` for arithmetic and ``*_display`` already formatted —
so the frontend never re-implements Indian currency rules (SPEC.md §5).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from munshiji.clock import now_ist
from munshiji.money import fmt_inr, fmt_inr_short

__all__ = ["ApiModel", "Envelope", "Meta", "Money", "money"]


class ApiModel(BaseModel):
    """Base for every DTO: immutable-ish, populated from ORM attributes."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class Money(ApiModel):
    """One monetary value, ready for both maths and display."""

    paise: int = 0
    display: str = "₹0"
    short: str = "₹0"


def money(amount_paise: int | None) -> Money:
    """Build a :class:`Money` from paise, tolerating ``None``."""
    value = int(amount_paise or 0)
    return Money(paise=value, display=fmt_inr(value), short=fmt_inr_short(value))


class Meta(ApiModel):
    """Per-response diagnostics — powers the provider badges in the UI."""

    provider: str = "local"
    latency_ms: int = 0
    as_of: datetime = Field(default_factory=now_ist)
    extra: dict[str, Any] = Field(default_factory=dict)


class Envelope(ApiModel):
    """Generic wrapper used where a route returns a bare list."""

    data: Any
    meta: Meta = Field(default_factory=Meta)
