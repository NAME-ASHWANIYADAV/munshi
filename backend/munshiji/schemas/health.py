"""Health and provider-status payloads.

This is not boilerplate: the dashboard's status strip reads it to show judges, live, which of the
three sponsor platforms is serving each capability and which is running on the offline fallback.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from munshiji.clock import now_ist
from munshiji.schemas.common import ApiModel

__all__ = ["HealthOut", "ProviderStatusOut"]


class ProviderStatusOut(ApiModel):
    name: str
    kind: str
    mode: str
    ok: bool
    detail: str = ""
    latency_ms: int = 0


class HealthOut(ApiModel):
    status: str = "ok"
    version: str = "1.0.0"
    env: str = "dev"
    provider_mode: str = "auto"
    providers: list[ProviderStatusOut] = Field(default_factory=list)
    #: ``{"sarvam": "live"|"local", "cognee": …, "n8n": …}``
    sponsors: dict[str, str] = Field(default_factory=dict)
    database_ready: bool = False
    merchant_id: str | None = None
    checked_at: datetime = Field(default_factory=now_ist)
