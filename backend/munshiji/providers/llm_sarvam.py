"""``SarvamLLM`` — the live reasoning core, backed by ``sarvam-105b`` chat completions."""

from __future__ import annotations

import time

from munshiji.config import Settings, get_settings
from munshiji.errors import ProviderUnavailableError
from munshiji.integrations.sarvam.chat import chat_completion
from munshiji.integrations.sarvam.client import SarvamClient
from munshiji.logging import get_logger
from munshiji.providers.base import ProviderHealth, ProviderMode
from munshiji.providers.llm import ChatMessage, LLMProvider, LLMResponse, ToolSpec

__all__ = ["SarvamLLM"]

_log = get_logger(__name__)


class SarvamLLM:
    """Live :class:`~munshiji.providers.llm.LLMProvider` over Sarvam's chat API.

    Every vendor failure — transport, status, malformed body — is absorbed *per call*: the
    turn is composed by the ``LocalLLM`` twin instead, with the response's ``provider`` field
    flipped to ``"local"`` (SPEC.md §2.1). A flaky vendor may cost the merchant the live
    phrasing, never the conversation. ``health()`` still reports the vendor's real state, which
    is how the factory decides who starts the day.
    """

    name = "sarvam-chat"
    mode: ProviderMode = "live"
    kind = "llm"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: SarvamClient | None = None,
        model: str | None = None,
        fallback: LLMProvider | None = None,
    ) -> None:
        # ``settings`` is positional so ``providers/factory.py`` can call ``SarvamLLM(settings)``.
        resolved = settings or get_settings()
        self.model = model or resolved.sarvam_llm_model
        self._client = client or SarvamClient(settings=resolved)
        self._owns_client = client is None
        if fallback is None:
            from munshiji.providers.llm_local import LocalLLM

            fallback = LocalLLM()
        self._fallback = fallback

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        language: str = "hi-IN",
    ) -> LLMResponse:
        """Produce the next assistant message, optionally requesting tool calls.

        ``language`` is not a wire parameter — Sarvam's model follows the system prompt — so it
        is recorded on the response for the UI rather than sent.

        ``sarvam-105b`` thinks before it speaks, and the thinking bills against ``max_tokens``:
        with the default effort a two-line question can spend the entire budget inside
        ``reasoning_content`` and return ``content: null`` — a silent turn. Low effort plus a
        roomy budget keeps the answer inside the window; the one retry below covers the model
        occasionally overthinking anyway.
        """
        started = time.perf_counter()
        try:
            parsed = await chat_completion(
                self._client,
                messages,
                tools,
                model=self.model,
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning_effort="low",
            )
            if not parsed.text and not parsed.tool_calls:
                _log.warning(
                    "sarvam returned neither text nor tool calls (finish=%s); retrying with double budget",
                    parsed.finish_reason,
                )
                parsed = await chat_completion(
                    self._client,
                    messages,
                    tools,
                    model=self.model,
                    temperature=temperature,
                    max_tokens=max_tokens * 2,
                    reasoning_effort="low",
                )
        except ProviderUnavailableError as exc:
            # The dual-provider rule, applied per call: a slow or flaky vendor must never cost
            # the merchant the turn. The offline twin answers, and the response's provider
            # field flips to "local" so the UI stays honest about who spoke.
            _log.warning("sarvam chat failed mid-turn (%s); composing with the local twin", exc)
            return await self._fallback.complete(
                messages, tools, temperature=temperature, language=language
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        return LLMResponse(
            text=parsed.text,
            tool_calls=parsed.tool_calls,
            provider="live",
            model=parsed.model or self.model,
            latency_ms=latency_ms,
            finish_reason=parsed.finish_reason,
            raw={"language": language, "response": parsed.raw},
        )

    async def health(self) -> ProviderHealth:
        """Cheap reachability probe.

        Never raises: an unhealthy provider is a fact the dashboard shows, not an error.
        """
        try:
            ok, detail, latency_ms = await self._client.probe()
        except Exception as exc:
            _log.warning("sarvam llm health probe failed: %s", exc)
            ok, detail, latency_ms = False, f"{type(exc).__name__}: {exc}", 0
        return ProviderHealth(
            name=self.name,
            kind="llm",
            mode=self.mode,
            ok=ok,
            detail=f"{self.model}: {detail}",
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        """Release the HTTP pool if this provider created it."""
        if self._owns_client:
            await self._client.aclose()
