"""Live action execution via n8n.

Maps an approved :class:`~munshiji.providers.actions.ActionDispatch` onto the matching n8n
webhook, hands the workflow a fully resolved payload (every recipient, every rendered message),
parses whatever comes back, and writes **exactly the same** ``ActionOutcome`` rows and khata
updates as :class:`~munshiji.providers.actions_local.LocalActions`. Downstream — memory ingest,
the action feed, conversation #2 — cannot tell which provider served the action, only that one did.

Transaction discipline
----------------------
The HTTP call happens **between** two short database transactions, never inside one:

1. read the action, the merchant and the idempotency stamp, then close;
2. POST to n8n — if this raises :class:`~munshiji.errors.ProviderUnavailableError` it propagates
   untouched and *nothing has been written*, so the factory is free to fall back to local;
3. write every row the response implies in a single committed transaction, rolled back whole on
   any error.

That ordering is the reason a dead n8n can never leave a half-executed action behind.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sqlalchemy.orm import Session

from munshiji.clock import now_utc
from munshiji.config import Settings
from munshiji.errors import ValidationError
from munshiji.integrations.n8n_client import (
    WEBHOOK_FOLLOWUP,
    WEBHOOK_PAYMENT_LINK,
    WEBHOOK_REMINDER,
    WEBHOOK_RESTOCK,
    WEBHOOK_WINBACK,
    N8nClient,
    N8nResult,
)
from munshiji.logging import get_logger
from munshiji.money import fmt_inr
from munshiji.providers.actions import ActionDispatch, ActionResult
from munshiji.providers.actions_local import (
    ResolvedTarget,
    action_session_scope,
    bump_khata_reminders,
    default_targets,
    finalise_action,
    previous_result,
    primary_language,
    resolve_targets,
    write_delivery_outcomes,
)
from munshiji.providers.base import ProviderHealth, ProviderMode
from munshiji.repositories.core import require_action, require_merchant

__all__ = ["PAYLOAD_VERSION", "TOOL_WEBHOOKS", "N8nActions"]

logger = get_logger(__name__)

#: Write tool (SPEC.md §9) → n8n webhook path. A tool absent from this table has no live
#: channel; ``save_merchant_note`` is deliberately not here — it never leaves the machine.
TOOL_WEBHOOKS: dict[str, str] = {
    "send_winback_offer": WEBHOOK_WINBACK,
    "send_udhaar_reminder": WEBHOOK_REMINDER,
    "create_payment_link": WEBHOOK_PAYMENT_LINK,
    "draft_restock_order": WEBHOOK_RESTOCK,
    "schedule_followup": WEBHOOK_FOLLOWUP,
}

#: Bumped whenever the webhook payload shape changes, so workflows can branch on it.
PAYLOAD_VERSION = 1

#: Keys a workflow might use for a per-recipient result array.
_TARGET_LIST_KEYS = ("results", "targets", "deliveries", "recipients", "messages")
_FALSY = frozenset({"false", "0", "no", "error", "failed", "failure", "rejected", "skipped"})


def _truthy(value: Any, *, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in _FALSY
    return default


def _per_target_results(raw: Mapping[str, Any]) -> dict[str, bool]:
    """Extract ``{customer_id: delivered?}`` if the workflow reported per recipient."""
    for key in _TARGET_LIST_KEYS:
        value = raw.get(key)
        if not isinstance(value, list) or not value:
            continue
        statuses: dict[str, bool] = {}
        for entry in value:
            if not isinstance(entry, dict):
                continue
            handle = str(entry.get("customer_id") or entry.get("id") or entry.get("phone") or "")
            if not handle:
                continue
            verdict = entry.get("ok")
            if verdict is None:
                verdict = entry.get("delivered", entry.get("status"))
            statuses[handle] = _truthy(verdict)
        if statuses:
            return statuses
    return {}


class N8nActions:
    """Live :class:`~munshiji.providers.actions.ActionProvider` backed by n8n workflows."""

    name = "n8n-actions"
    mode: ProviderMode = "live"

    def __init__(
        self,
        client: N8nClient | None = None,
        session_factory: Callable[[], Session] | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        """Args:
        client: An :class:`~munshiji.integrations.n8n_client.N8nClient`. One is built lazily
            from settings when omitted; a supplied client is never closed by this provider.
        session_factory: Session factory, defaulting to
            :func:`munshiji.db.base.get_sessionmaker` (resolved lazily).
        settings: Override configuration for the lazily-built client.
        """
        self._client = client
        self._owns_client = client is None
        self._settings = settings
        self._session_factory = session_factory

    # ── Protocol ────────────────────────────────────────────────────────────

    async def dispatch(self, dispatch: ActionDispatch) -> ActionResult:
        """Send one approved action through its n8n workflow. Idempotent on ``idempotency_key``."""
        started = time.perf_counter()
        key = dispatch.idempotency_key or dispatch.action_id
        path = self.webhook_for(dispatch.tool_name)

        # ── 1. read-only pass ───────────────────────────────────────────────
        with action_session_scope(self._factory()) as session:
            action = require_action(session, dispatch.action_id)
            replay = previous_result(action, key)
            if replay is not None:
                logger.info("action %s replayed from idempotency key %s", action.id, key)
                return replay
            merchant = require_merchant(session, dispatch.merchant_id)
            language = primary_language(dispatch.params, merchant)
            shop_name = merchant.shop_name
            merchant_payload = {
                "id": merchant.id,
                "shop_name": merchant.shop_name,
                "owner_name": merchant.owner_name,
                "phone": merchant.phone,
                "language": merchant.language,
                "city": merchant.city,
            }
            summary = {"en": action.summary_en, "hi": action.summary_hi}
            raw_targets = [dict(t) for t in dispatch.targets] or default_targets(
                dispatch.tool_name, dispatch.params, merchant
            )

        targets = resolve_targets(
            tool_name=dispatch.tool_name,
            params=dispatch.params,
            targets=raw_targets,
            shop_name=shop_name,
            language=language,
        )
        sendable = [target for target in targets if target.deliverable]
        invalid = [target for target in targets if not target.deliverable]

        # ── 2. the network hop (outside every transaction) ──────────────────
        if sendable:
            payload = self._payload(
                dispatch,
                targets=sendable,
                language=language,
                merchant=merchant_payload,
                summary=summary,
                idempotency_key=key,
                webhook=path,
            )
            response = await self._require_client().post(
                path, payload, idempotency_key=key, expected=len(sendable)
            )
        else:
            logger.warning(
                "action %s has no deliverable recipients; skipping the n8n call",
                dispatch.action_id,
            )
            response = N8nResult(ok=not targets, delivered=0, failed=0, raw={})

        delivered_count = max(0, min(int(response.delivered), len(sendable)))
        failed_count = max(0, int(response.failed)) + len(invalid)
        delivered_targets = self._delivered_targets(response, sendable, delivered_count)
        delivered_indices = {target.index for target in delivered_targets}

        result = ActionResult(
            ok=bool(response.ok) and (delivered_count > 0 or not targets),
            provider=self.mode,
            message=self._summarise(path, targets, delivered_count, failed_count),
            detail={
                "tool": dispatch.tool_name,
                "mode": "n8n",
                "webhook": path,
                "language": language,
                "idempotency_key": key,
                "status_code": response.status_code,
                "response": response.raw,
                "targets": [
                    {
                        **target.as_payload(language),
                        "status": (
                            "delivered"
                            if target.index in delivered_indices
                            else ("failed" if target.deliverable else "invalid_phone")
                        ),
                    }
                    for target in targets
                ],
            },
            delivered_count=delivered_count,
            failed_count=failed_count,
            latency_ms=int((time.perf_counter() - started) * 1000),
            external_id=response.external_id,
        )

        # ── 3. one atomic write ─────────────────────────────────────────────
        with action_session_scope(self._factory()) as session:
            action = require_action(session, dispatch.action_id)
            replay = previous_result(action, key)
            if replay is not None:  # a concurrent dispatch landed first
                return replay
            write_delivery_outcomes(
                session,
                action.id,
                delivered=delivered_count,
                failed=failed_count,
                note=f"{self.name} · {path}",
            )
            extra: dict[str, Any] = {"webhook": path}
            if dispatch.tool_name == "send_udhaar_reminder":
                touched = bump_khata_reminders(session, dispatch.merchant_id, delivered_targets)
                extra["khata_entry_ids"] = [entry.id for entry in touched]
            finalise_action(
                session,
                action,
                result=result,
                targets=targets,
                idempotency_key=key,
                extra_result=extra,
            )
        return result

    async def health(self) -> ProviderHealth:
        """Probe n8n's health path. Never raises — a dead n8n is an unhealthy report."""
        started = time.perf_counter()
        ok = await self._require_client().ping()
        return ProviderHealth(
            name=self.name,
            kind="actions",
            mode=self.mode,
            ok=ok,
            detail="n8n reachable" if ok else "n8n unreachable — will fall back to local",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def aclose(self) -> None:
        """Release the lazily-built HTTP client. A caller-supplied client is left alone."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def webhook_for(tool_name: str) -> str:
        """The webhook path for a write tool.

        Raises:
            ValidationError: the tool has no live channel (the factory should use local).
        """
        path = TOOL_WEBHOOKS.get(tool_name)
        if path is None:
            raise ValidationError(f"tool {tool_name!r} has no n8n webhook", tool_name=tool_name)
        return path

    def _factory(self) -> Callable[[], Session]:
        if self._session_factory is not None:
            return self._session_factory
        from munshiji.db.base import get_sessionmaker

        return get_sessionmaker()

    def _require_client(self) -> N8nClient:
        if self._client is None:
            self._client = N8nClient(self._settings)
        return self._client

    @staticmethod
    def _payload(
        dispatch: ActionDispatch,
        *,
        targets: Sequence[ResolvedTarget],
        language: str,
        merchant: Mapping[str, Any],
        summary: Mapping[str, str],
        idempotency_key: str,
        webhook: str,
    ) -> dict[str, Any]:
        """The JSON body an n8n workflow receives. Complete — no lookups needed on that side."""
        return {
            "version": PAYLOAD_VERSION,
            "action_id": dispatch.action_id,
            "merchant_id": dispatch.merchant_id,
            "merchant": dict(merchant),
            "tool": dispatch.tool_name,
            "webhook": webhook,
            "idempotency_key": idempotency_key,
            "language": language,
            "params": dict(dispatch.params),
            "messages": dict(dispatch.messages),
            "summary": dict(summary),
            "target_count": len(targets),
            "targets": [target.as_payload(language) for target in targets],
            "meta": {
                "source": "munshiji",
                "requested_at": now_utc().isoformat(),
                "approved": True,
            },
        }

    @staticmethod
    def _delivered_targets(
        response: N8nResult,
        sendable: Sequence[ResolvedTarget],
        delivered_count: int,
    ) -> list[ResolvedTarget]:
        """Which recipients actually got the message.

        Uses the workflow's per-recipient array when it supplied one. Otherwise n8n reports only
        totals, and since the workflow fans out over ``targets`` in order, the first
        ``delivered_count`` are taken as the successes — a documented approximation that matters
        only for which khata rows get their reminder counter bumped.
        """
        statuses = _per_target_results(response.raw)
        if statuses:
            return [
                target
                for target in sendable
                if statuses.get(target.customer_id, statuses.get(target.phone, True))
            ]
        return list(sendable[:delivered_count])

    @staticmethod
    def _summarise(
        webhook: str,
        targets: Sequence[ResolvedTarget],
        delivered: int,
        failed: int,
    ) -> str:
        if not targets:
            return f"No recipients resolved; {webhook} was not called."
        amount = sum(target.amount_paise for target in targets)
        money = f" covering {fmt_inr(amount)}" if amount else ""
        text = f"{delivered}/{len(targets)} delivered via n8n ({webhook}){money}"
        if failed:
            text += f"; {failed} not delivered"
        return text
