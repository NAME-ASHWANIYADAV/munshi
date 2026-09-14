"""Shared transport for every Sarvam AI call.

One ``httpx.AsyncClient`` wrapper handles auth, timeouts, a single retry, and — most
importantly — turns *any* failure into :class:`~munshiji.errors.ProviderUnavailableError` so the
provider factory can fall back to the offline implementation without a crash (SPEC.md §2.1).

Two deliberate defensive choices, because this code has to survive a hackathon venue and a
vendor API that may have moved since it was written:

* **Endpoint paths are module-level constants** with a comment naming what each one is.
  If Sarvam renames a route, this file is the only place to edit.
* **Response parsing is tolerant.** :func:`find_value` searches a decoded payload for the first
  plausible key, at any reasonable nesting depth, so a response wrapped in ``{"data": …}`` or
  ``{"results": [...]}`` still parses.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from munshiji.config import Settings, get_settings
from munshiji.errors import ProviderUnavailableError
from munshiji.logging import get_logger

__all__ = [
    "AUTH_HEADER",
    "AUTH_HEADER_FALLBACK",
    "RETRYABLE_STATUS",
    "SARVAM_CHAT_PATH",
    "SARVAM_MODELS_PATH",
    "SARVAM_STT_PATH",
    "SARVAM_STT_TRANSLATE_PATH",
    "SARVAM_TTS_PATH",
    "SarvamClient",
    "find_value",
]

_log = get_logger(__name__)

# ── Endpoints ───────────────────────────────────────────────────────────────────────────────
# Paths are relative to ``settings.sarvam_base_url`` (default ``https://api.sarvam.ai``) and
# follow Sarvam's documented REST surface. Kept as constants so a rename is a one-line change.

#: Speech-to-text: multipart upload (``file``) + ``model``/``language_code`` form fields.
SARVAM_STT_PATH = "/speech-to-text"
#: Same upload shape, but the transcript comes back translated into English.
SARVAM_STT_TRANSLATE_PATH = "/speech-to-text-translate"
#: Text-to-speech: JSON ``{text, target_language_code, speaker, model}`` → base64 WAV.
SARVAM_TTS_PATH = "/text-to-speech"
#: Chat completions, OpenAI-compatible (``model``/``messages``/``tools``/``tool_choice``).
SARVAM_CHAT_PATH = "/v1/chat/completions"
#: Cheap GET used only by the health probe; a 404/405 here still proves reachability.
SARVAM_MODELS_PATH = "/v1/models"

# ── Auth ────────────────────────────────────────────────────────────────────────────────────
#: Sarvam's documented scheme: the raw subscription key in its own header.
AUTH_HEADER = "api-subscription-key"
#: Accepted fallback — the OpenAI-compatible surface also honours bearer auth.
AUTH_HEADER_FALLBACK = "Authorization"

#: Statuses worth one retry: the vendor is up but wobbling.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

_RETRYABLE_EXCEPTIONS = (httpx.TimeoutException, httpx.TransportError)
_MAX_BODY_IN_ERROR = 400


class SarvamClient:
    """Authenticated, retrying, failure-translating HTTP client for ``api.sarvam.ai``.

    ``transport`` exists so the test-suite can drive every code path with
    ``httpx.MockTransport`` — nothing in this package ever touches the real network in CI.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 1,
        retry_backoff: float = 0.25,
        use_bearer: bool = False,
        settings: Settings | None = None,
    ) -> None:
        resolved = settings or get_settings()
        self.api_key = (api_key if api_key is not None else resolved.sarvam_api_key).strip()
        self.base_url = (base_url or resolved.sarvam_base_url).rstrip("/")
        self.timeout = timeout if timeout is not None else resolved.http_timeout_seconds
        self.max_retries = max(0, max_retries)
        self.retry_backoff = max(0.0, retry_backoff)
        self.use_bearer = use_bearer
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    # ── Plumbing ────────────────────────────────────────────────────────────────────────
    def auth_headers(self) -> dict[str, str]:
        """Auth header for the configured scheme. Empty when no key is configured."""
        if not self.api_key:
            return {}
        if self.use_bearer:
            return {AUTH_HEADER_FALLBACK: f"Bearer {self.api_key}"}
        return {AUTH_HEADER: self.api_key}

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout),
                headers={"Accept": "application/json", **self.auth_headers()},
                transport=self._transport,
            )
        return self._client

    async def aclose(self) -> None:
        """Release the connection pool. Safe to call more than once."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def __aenter__(self) -> SarvamClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # ── Requests ────────────────────────────────────────────────────────────────────────
    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        require_key: bool = True,
    ) -> dict[str, Any]:
        """Perform one call and return the decoded JSON body.

        Retries once on a connect/timeout error or a retryable status, with a short backoff.
        Every other failure — including a non-JSON body — surfaces as
        :class:`ProviderUnavailableError` so the caller can fall back to local.
        """
        if require_key and not self.api_key:
            raise ProviderUnavailableError(
                "SARVAM_API_KEY is not configured", provider="sarvam", path=path
            )

        client = self._ensure_client()
        last_detail = "unknown error"
        for attempt in range(self.max_retries + 1):
            try:
                response = await client.request(method, path, json=json, data=data, files=files)
            except _RETRYABLE_EXCEPTIONS as exc:
                last_detail = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code < 400:
                    return _decode_json(response, path)
                last_detail = _describe(response)
                if response.status_code not in RETRYABLE_STATUS:
                    raise ProviderUnavailableError(
                        f"Sarvam {path} failed: {last_detail}",
                        provider="sarvam",
                        path=path,
                        status=response.status_code,
                    )

            if attempt < self.max_retries:
                _log.warning(
                    "sarvam retry path=%s attempt=%s detail=%s", path, attempt, last_detail
                )
                if self.retry_backoff:
                    await asyncio.sleep(self.retry_backoff * (attempt + 1))

        raise ProviderUnavailableError(
            f"Sarvam {path} unavailable: {last_detail}", provider="sarvam", path=path
        )

    async def probe(self) -> tuple[bool, str, int]:
        """Cheap reachability + credential check for ``health()``. Never raises.

        Deliberately *not* a real inference call: ``GET /api/health`` is polled by the dashboard,
        and spending a transcription or a completion on every poll is not acceptable. Any
        response below 500 proves the host is up and the key was not rejected outright.
        """
        if not self.api_key:
            return False, "no SARVAM_API_KEY configured", 0
        started = time.perf_counter()
        try:
            client = self._ensure_client()
            response = await client.get(SARVAM_MODELS_PATH)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}", _elapsed_ms(started)
        latency = _elapsed_ms(started)
        if response.status_code in (401, 403):
            return False, f"auth rejected ({response.status_code})", latency
        if response.status_code >= 500:
            return False, f"upstream {response.status_code}", latency
        return True, f"reachable ({response.status_code})", latency


# ── Response helpers ────────────────────────────────────────────────────────────────────────


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _describe(response: httpx.Response) -> str:
    body = response.text or ""
    if len(body) > _MAX_BODY_IN_ERROR:
        body = f"{body[:_MAX_BODY_IN_ERROR]}…"
    return f"HTTP {response.status_code} {body}".strip()


def _decode_json(response: httpx.Response, path: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderUnavailableError(
            f"Sarvam {path} returned a non-JSON body", provider="sarvam", path=path
        ) from exc
    if isinstance(payload, dict):
        return payload
    # A bare list is legal JSON; wrap it so downstream parsing has one shape to reason about.
    return {"data": payload}


def find_value(
    payload: Any,
    keys: Iterable[str],
    *,
    accept: Callable[[Any], bool] | None = None,
    max_depth: int = 5,
) -> Any:
    """First value matching any of ``keys``, searched breadth-first, in key priority order.

    Tolerance is the point: the same transcript may arrive as ``{"transcript": …}``,
    ``{"text": …}``, ``{"data": {"transcript": …}}`` or ``{"results": [{"transcript": …}]}``,
    and this finds all four. ``accept`` rejects structurally-present but useless values
    (an empty string, the wrong type). Returns ``None`` when nothing matches.
    """
    for key in keys:
        queue: deque[tuple[Any, int]] = deque([(payload, 0)])
        while queue:
            node, depth = queue.popleft()
            if depth > max_depth:
                continue
            if isinstance(node, dict):
                if key in node:
                    value = node[key]
                    if accept is None or accept(value):
                        return value
                queue.extend((child, depth + 1) for child in node.values())
            elif isinstance(node, list):
                queue.extend((child, depth + 1) for child in node)
    return None
