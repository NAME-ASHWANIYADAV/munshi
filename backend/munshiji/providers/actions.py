"""Action provider protocol — how MunshiJi's decisions reach the outside world.

Live implementation POSTs to n8n webhooks, where the workflow fans out to WhatsApp/SMS, payment
links and supplier email, and returns a structured result. The local implementation runs the
same contract in-process so the approval gate, audit trail and outcome tracking are all
exercised offline.

An action only ever arrives here **after** the merchant has approved it (SPEC.md §2.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from munshiji.providers.base import ProviderHealth, ProviderMode

__all__ = ["ActionDispatch", "ActionProvider", "ActionResult"]


@dataclass(slots=True)
class ActionDispatch:
    """An approved action being handed to the execution layer."""

    action_id: str
    merchant_id: str
    tool_name: str
    params: dict[str, Any] = field(default_factory=dict)
    #: Resolved recipients: ``[{"customer_id":…, "name":…, "phone":…, "amount_paise":…}, …]``
    targets: list[dict[str, Any]] = field(default_factory=list)
    #: Rendered message body per language, keyed ``"hi"`` / ``"en"``.
    messages: dict[str, str] = field(default_factory=dict)
    idempotency_key: str = ""


@dataclass(slots=True)
class ActionResult:
    """Outcome of dispatching one action."""

    ok: bool
    provider: ProviderMode = "local"
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    delivered_count: int = 0
    failed_count: int = 0
    latency_ms: int = 0
    external_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "provider": self.provider,
            "message": self.message,
            "detail": self.detail,
            "delivered_count": self.delivered_count,
            "failed_count": self.failed_count,
            "latency_ms": self.latency_ms,
            "external_id": self.external_id,
        }


@runtime_checkable
class ActionProvider(Protocol):
    """Executes approved actions."""

    name: str
    mode: ProviderMode

    async def dispatch(self, dispatch: ActionDispatch) -> ActionResult:
        """Execute one approved action. Must be idempotent on ``idempotency_key``."""
        ...

    async def health(self) -> ProviderHealth: ...
