"""Provider selection — the switch between vendor-backed and offline implementations.

Policy (SPEC.md §2.1):

* ``MUNSHIJI_PROVIDER_MODE=local`` — always offline.
* ``=live``  — always vendor; a failing probe is surfaced, not hidden.
* ``=auto``  (default) — use a vendor when its credential is present *and* a health probe passes,
  otherwise fall back to the local implementation and log a warning.

Construction never touches the network, so importing this module is cheap and tests stay offline.
Probing is an explicit, awaited step (:func:`resolve`) run once at application startup.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from munshiji.config import Settings, get_settings
from munshiji.logging import get_logger
from munshiji.providers.base import ProviderHealth

if TYPE_CHECKING:  # pragma: no cover - typing only
    from munshiji.providers.actions import ActionProvider
    from munshiji.providers.llm import LLMProvider
    from munshiji.providers.memory import MemoryProvider
    from munshiji.providers.stt import STTProvider
    from munshiji.providers.tts import TTSProvider

__all__ = ["ProviderBundle", "build_providers", "get_providers", "reset_providers", "resolve"]

logger = get_logger(__name__)


@dataclass(slots=True)
class ProviderBundle:
    """Every external capability MunshiJi needs, already resolved to an implementation."""

    llm: LLMProvider
    stt: STTProvider
    tts: TTSProvider
    memory: MemoryProvider
    actions: ActionProvider

    @property
    def modes(self) -> dict[str, str]:
        """Which implementation is serving each capability — shown in the UI status strip."""
        return {
            "llm": self.llm.mode,
            "stt": self.stt.mode,
            "tts": self.tts.mode,
            "memory": self.memory.mode,
            "actions": self.actions.mode,
        }

    @property
    def sponsor_status(self) -> dict[str, str]:
        """Sponsor-facing view: which of the three partner platforms is live."""
        voice_live = "live" in {self.llm.mode, self.stt.mode, self.tts.mode}
        return {
            "sarvam": "live" if voice_live else "local",
            "cognee": self.memory.mode,
            "n8n": self.actions.mode,
        }

    async def health(self) -> list[ProviderHealth]:
        """Probe every provider concurrently. Never raises."""
        providers = [self.llm, self.stt, self.tts, self.memory, self.actions]
        results = await asyncio.gather(
            *(provider.health() for provider in providers), return_exceptions=True
        )
        report: list[ProviderHealth] = []
        for provider, result in zip(providers, results, strict=True):
            if isinstance(result, BaseException):
                report.append(
                    ProviderHealth(
                        name=getattr(provider, "name", type(provider).__name__),
                        kind="llm",
                        mode=getattr(provider, "mode", "local"),
                        ok=False,
                        detail=f"probe raised: {result!r}",
                    )
                )
            else:
                report.append(result)
        return report


def build_providers(settings: Settings | None = None) -> ProviderBundle:
    """Construct providers from configuration, without any network access.

    Imports are deferred so a missing optional module (or an in-progress one) cannot break
    unrelated code paths.
    """
    settings = settings or get_settings()

    from munshiji.providers.actions_local import LocalActions
    from munshiji.providers.llm_local import LocalLLM
    from munshiji.providers.memory_local import LocalGraphMemory
    from munshiji.providers.stt_local import LocalSTT
    from munshiji.providers.tts_local import LocalTTS

    llm: LLMProvider = LocalLLM()
    stt: STTProvider = LocalSTT()
    tts: TTSProvider = LocalTTS()
    memory: MemoryProvider = LocalGraphMemory()
    actions: ActionProvider = LocalActions()

    if settings.wants_live(settings.has_sarvam):
        try:
            from munshiji.providers.llm_sarvam import SarvamLLM
            from munshiji.providers.stt_sarvam import SarvamSTT
            from munshiji.providers.tts_sarvam import SarvamTTS

            llm, stt, tts = SarvamLLM(settings), SarvamSTT(settings), SarvamTTS(settings)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Sarvam providers unavailable, staying local: %s", exc)

    if settings.wants_live(settings.has_cognee):
        try:
            from munshiji.providers.memory_cognee import CogneeMemory

            # No settings argument: CogneeMemory's first positional is the CLIENT, and handing
            # it a Settings object survived until the first run with a real key — the probe
            # then died on Settings.api_key and silently benched the whole live path.
            memory = CogneeMemory()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Cognee memory unavailable, staying local: %s", exc)

    if settings.wants_live(settings.has_n8n):
        try:
            from munshiji.providers.actions_n8n import N8nActions

            actions = N8nActions(settings)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("n8n actions unavailable, staying local: %s", exc)

    return ProviderBundle(llm=llm, stt=stt, tts=tts, memory=memory, actions=actions)


async def resolve(
    bundle: ProviderBundle | None = None, settings: Settings | None = None
) -> ProviderBundle:
    """Probe live providers and downgrade any that fail. Returns a bundle safe to serve traffic.

    In ``live`` mode nothing is downgraded — a broken vendor should be loud, not silently masked.
    """
    settings = settings or get_settings()
    bundle = bundle or build_providers(settings)
    if settings.provider_mode == "live":
        return bundle

    from munshiji.providers.actions_local import LocalActions
    from munshiji.providers.llm_local import LocalLLM
    from munshiji.providers.memory_local import LocalGraphMemory
    from munshiji.providers.stt_local import LocalSTT
    from munshiji.providers.tts_local import LocalTTS

    fallbacks = {
        "llm": LocalLLM,
        "stt": LocalSTT,
        "tts": LocalTTS,
        "memory": LocalGraphMemory,
        "actions": LocalActions,
    }

    resolved = bundle
    for field_name, fallback_cls in fallbacks.items():
        provider = getattr(resolved, field_name)
        if getattr(provider, "mode", "local") != "live":
            continue
        try:
            health = await provider.health()
            ok = health.ok
            detail = health.detail
        except Exception as exc:  # pragma: no cover - defensive
            ok, detail = False, repr(exc)
        if not ok:
            logger.warning(
                "%s provider %s failed its probe (%s) — falling back to local",
                field_name,
                getattr(provider, "name", "?"),
                detail,
            )
            resolved = replace(resolved, **{field_name: fallback_cls()})

    return resolved


_bundle: ProviderBundle | None = None


def get_providers(settings: Settings | None = None) -> ProviderBundle:
    """Process-wide bundle, built on first use (unprobed — call :func:`resolve` at startup)."""
    global _bundle
    if _bundle is None:
        _bundle = build_providers(settings)
    return _bundle


def set_providers(bundle: ProviderBundle) -> None:
    """Install a bundle as the process-wide default (used by startup and by tests)."""
    global _bundle
    _bundle = bundle


def reset_providers() -> None:
    """Drop the cached bundle. Used between tests."""
    global _bundle
    _bundle = None
