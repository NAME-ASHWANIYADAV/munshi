"""n8n webhook client — the wire between an approved decision and the outside world.

One webhook per action type. MunshiJi POSTs a fully resolved payload (recipients *and* the
exact text each one should receive) and n8n fans it out to WhatsApp / SMS / Paytm payment
links / supplier email, then answers with whatever the workflow author wired up.

Two deliberate design choices:

* **The payload is complete.** The workflow never has to look anything up. Every target already
  carries its phone number and its rendered message, so the n8n side is a loop and a send node.
* **Response parsing is tolerant.** n8n workflows return whatever the last node produced — an
  object, a one-element array, a nested ``{"data": …}``, or a bare ``"Workflow was started"``.
  :func:`parse_webhook_response` accepts all of those and falls back to sensible defaults rather
  than failing an action that actually went out.

Anything that stops a request reaching the workflow becomes
:class:`~munshiji.errors.ProviderUnavailableError`, so the provider factory can fall back to
``LocalActions`` instead of crashing the demo (SPEC.md §2.1).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

from munshiji.config import Settings, get_settings
from munshiji.errors import ProviderUnavailableError
from munshiji.logging import get_logger

__all__ = [
    "HEALTH_PATH",
    "IDEMPOTENCY_HEADER",
    "TOKEN_HEADER",
    "WEBHOOK_FOLLOWUP",
    "WEBHOOK_PATHS",
    "WEBHOOK_PAYMENT_LINK",
    "WEBHOOK_REMINDER",
    "WEBHOOK_RESTOCK",
    "WEBHOOK_WINBACK",
    "N8nClient",
    "N8nResult",
    "parse_webhook_response",
]

logger = get_logger(__name__)

# ── Webhook paths (one per action type; these are the n8n workflow names) ────
WEBHOOK_WINBACK = "munshiji-winback"
WEBHOOK_REMINDER = "munshiji-reminder"
WEBHOOK_PAYMENT_LINK = "munshiji-payment-link"
WEBHOOK_RESTOCK = "munshiji-restock"
WEBHOOK_FOLLOWUP = "munshiji-followup"

#: Every path this client is allowed to hit.
WEBHOOK_PATHS: tuple[str, ...] = (
    WEBHOOK_WINBACK,
    WEBHOOK_REMINDER,
    WEBHOOK_PAYMENT_LINK,
    WEBHOOK_RESTOCK,
    WEBHOOK_FOLLOWUP,
)

#: n8n's own liveness endpoint, relative to the base URL.
HEALTH_PATH = "/healthz"

TOKEN_HEADER = "X-Munshiji-Token"
IDEMPOTENCY_HEADER = "Idempotency-Key"

_USER_AGENT = "munshiji/1.0 (+n8n-actions)"
_CONNECT_TIMEOUT = 5.0
_HEALTH_TIMEOUT = 3.0
_RETRY_DELAY_SECONDS = 0.25


# ─────────────────────────────────────────────────────────────────────────────
# Response parsing
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class N8nResult:
    """A workflow's answer, normalised."""

    ok: bool
    delivered: int = 0
    failed: int = 0
    external_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    status_code: int = 200


_TRUTHY_STRINGS = frozenset(
    {"ok", "true", "yes", "1", "success", "succeeded", "sent", "delivered", "completed", "done"}
)
_FALSY_STRINGS = frozenset({"error", "failed", "failure", "false", "0", "no", "rejected"})

_CONTAINER_KEYS = ("data", "json", "result", "body", "response", "payload", "output")
_OK_KEYS = ("ok", "success", "succeeded", "delivered_all")
_STATUS_KEYS = ("status", "state", "result", "outcome")
_DELIVERED_KEYS = (
    "delivered",
    "delivered_count",
    "sent",
    "sent_count",
    "messages_sent",
    "success_count",
    "successful",
    "count",
)
_FAILED_KEYS = (
    "failed",
    "failed_count",
    "failures",
    "errors",
    "error_count",
    "messages_failed",
    "skipped",
)
_ID_KEYS = (
    "id",
    "external_id",
    "externalId",
    "execution_id",
    "executionId",
    "message_id",
    "messageId",
    "batch_id",
    "batchId",
    "workflow_id",
    "workflowId",
)


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in _TRUTHY_STRINGS:
            return True
        if cleaned in _FALSY_STRINGS:
            return False
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, list | tuple):
        return len(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.lstrip("-").isdigit():
            return int(cleaned)
    return None


def _first_int(data: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if key in data:
            found = _as_int(data[key])
            if found is not None:
                return max(0, found)
    return None


def _is_informative(payload: dict[str, Any]) -> bool:
    """Whether this level already carries a scalar verdict, so unwrapping would lose it."""
    return any(
        key in payload and not isinstance(payload[key], dict | list)
        for key in (*_OK_KEYS, *_STATUS_KEYS, *_DELIVERED_KEYS, *_FAILED_KEYS)
    )


def _unwrap(payload: Any, depth: int = 0) -> dict[str, Any]:
    """Peel n8n's habitual wrappers off until the level holding the verdict is in view.

    Handles ``[{...}]`` (the classic n8n item array), ``{"data": {...}}`` and
    ``{"json": {...}}``. Stops as soon as a level carries a usable scalar, so a sibling
    per-recipient array is never mistaken for the summary.
    """
    if depth > 3:
        return {}
    if isinstance(payload, list):
        for entry in payload:
            if isinstance(entry, dict | list):
                return _unwrap(entry, depth + 1)
        return {}
    if not isinstance(payload, dict):
        return {}
    if _is_informative(payload):
        return payload
    for key in _CONTAINER_KEYS:
        inner = payload.get(key)
        if isinstance(inner, dict):
            merged = _unwrap(inner, depth + 1)
            if merged:
                return {**{k: v for k, v in payload.items() if k != key}, **merged}
    return payload


def parse_webhook_response(payload: Any, *, expected: int = 0, status_code: int = 200) -> N8nResult:
    """Normalise whatever the workflow returned into an :class:`N8nResult`.

    Args:
        payload: Decoded JSON body (dict, list, scalar) or ``None`` for an empty body.
        expected: How many recipients we sent, used to fill in counts the workflow omitted.
        status_code: HTTP status, recorded for the audit trail.

    A 2xx with an unreadable body is treated as success with ``delivered = expected`` — the
    workflow accepted the batch and we have nothing better to go on.
    """
    data = _unwrap(payload)
    raw: dict[str, Any] = dict(payload) if isinstance(payload, dict) else {}
    if not raw:
        raw = dict(data) if data else ({"body": payload} if payload is not None else {})

    ok: bool | None = None
    for key in _OK_KEYS:
        if key in data:
            ok = _as_bool(data[key])
            if ok is not None:
                break
    if ok is None:
        for key in _STATUS_KEYS:
            candidate = data.get(key)
            if isinstance(candidate, str):
                ok = _as_bool(candidate)
                if ok is not None:
                    break
    if ok is None and data.get("error"):
        ok = False
    if ok is None:
        ok = True

    delivered = _first_int(data, _DELIVERED_KEYS)
    failed = _first_int(data, _FAILED_KEYS)

    if delivered is None and failed is None:
        delivered, failed = (expected, 0) if ok else (0, expected)
    elif delivered is None:
        delivered = max(expected - (failed or 0), 0)
    elif failed is None:
        failed = max(expected - delivered, 0)

    if delivered == 0 and failed > 0:
        ok = False

    external_id = ""
    for key in _ID_KEYS:
        value = data.get(key)
        if isinstance(value, str | int) and not isinstance(value, bool) and str(value).strip():
            external_id = str(value).strip()
            break

    return N8nResult(
        ok=bool(ok),
        delivered=int(delivered or 0),
        failed=int(failed or 0),
        external_id=external_id,
        raw=raw,
        status_code=status_code,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────────────


class N8nClient:
    """Thin, retrying ``httpx.AsyncClient`` wrapper around MunshiJi's n8n webhooks."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_delay_seconds: float = _RETRY_DELAY_SECONDS,
    ) -> None:
        """Args:
        settings: Defaults to the cached :func:`~munshiji.config.get_settings`.
        client: Supply your own client (tests, connection pooling); it is not closed by
            :meth:`aclose`.
        transport: Alternative to ``client`` — e.g. ``httpx.MockTransport`` in tests.
        retry_delay_seconds: Pause before the single retry. Tests pass ``0``.
        """
        self._settings = settings or get_settings()
        self._retry_delay = max(0.0, float(retry_delay_seconds))
        self._base_url = self._settings.n8n_base_url.rstrip("/")
        prefix = self._settings.n8n_webhook_prefix.strip()
        self._prefix = "/" + prefix.strip("/") if prefix.strip("/") else ""
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._settings.http_timeout_seconds, connect=_CONNECT_TIMEOUT),
            transport=transport,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )

    # ── URLs & headers ──────────────────────────────────────────────────────

    def webhook_url(self, path: str) -> str:
        """``{n8n_base_url}{n8n_webhook_prefix}/{path}``."""
        return f"{self._base_url}{self._prefix}/{path.strip('/')}"

    def _headers(self, idempotency_key: str) -> dict[str, str]:
        headers = {IDEMPOTENCY_HEADER: idempotency_key or "none"}
        token = self._settings.n8n_webhook_token.strip()
        if token:
            headers[TOKEN_HEADER] = token
        return headers

    # ── Calls ───────────────────────────────────────────────────────────────

    async def post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str = "",
        expected: int = 0,
    ) -> N8nResult:
        """POST to one webhook, retrying once on a connect/timeout/5xx failure.

        Raises:
            ProviderUnavailableError: the workflow could not be reached or refused the call.
                Nothing has been written to the database at this point, by construction.
        """
        url = self.webhook_url(path)
        headers = self._headers(idempotency_key)
        last_detail = ""

        for attempt in (0, 1):
            try:
                response = await self._client.post(url, json=payload, headers=headers)
            except httpx.TransportError as exc:  # connect errors, timeouts, broken pipes
                last_detail = f"{type(exc).__name__}: {exc}"
                logger.warning("n8n %s attempt %d failed: %s", path, attempt + 1, last_detail)
                if attempt == 0:
                    await self._pause()
                    continue
                raise ProviderUnavailableError(
                    f"n8n webhook {path!r} unreachable: {last_detail}",
                    path=path,
                    url=url,
                ) from exc

            if response.status_code >= 500:
                last_detail = f"HTTP {response.status_code}"
                logger.warning("n8n %s attempt %d returned %s", path, attempt + 1, last_detail)
                if attempt == 0:
                    await self._pause()
                    continue
                raise ProviderUnavailableError(
                    f"n8n webhook {path!r} failed: {last_detail}",
                    path=path,
                    url=url,
                    status_code=response.status_code,
                )

            if response.status_code >= 400:
                # 4xx is configuration (wrong token, workflow not active) — retrying cannot help.
                raise ProviderUnavailableError(
                    f"n8n webhook {path!r} rejected the call: HTTP {response.status_code}",
                    path=path,
                    url=url,
                    status_code=response.status_code,
                    body=response.text[:400],
                )

            return parse_webhook_response(
                self._decode(response), expected=expected, status_code=response.status_code
            )

        raise ProviderUnavailableError(  # pragma: no cover - loop always returns or raises
            f"n8n webhook {path!r} failed: {last_detail}", path=path, url=url
        )

    async def ping(self) -> bool:
        """Probe n8n's health path. Never raises — a dead n8n is a ``False``, not an exception."""
        url = f"{self._base_url}{HEALTH_PATH}"
        try:
            response = await self._client.get(url, timeout=_HEALTH_TIMEOUT)
        except Exception as exc:  # health probes must never propagate
            logger.debug("n8n ping failed: %s", exc)
            return False
        return response.status_code < 500

    async def aclose(self) -> None:
        """Close the underlying client, unless the caller supplied their own."""
        if self._owns_client:
            await self._client.aclose()

    # ── Internals ───────────────────────────────────────────────────────────

    async def _pause(self) -> None:
        if self._retry_delay > 0:
            await asyncio.sleep(self._retry_delay)

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        """Best-effort JSON decode; an unreadable body is not an error (see module docstring)."""
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            text = response.text.strip()
            return {"message": text[:400]} if text else None
