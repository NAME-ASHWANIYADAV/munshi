"""``SarvamTTS`` — live speech synthesis via ``bulbul`` (11 Indian languages)."""

from __future__ import annotations

import time

from munshiji.config import Settings, get_settings
from munshiji.integrations.sarvam.client import SarvamClient
from munshiji.integrations.sarvam.speech import synthesise
from munshiji.logging import get_logger
from munshiji.providers.base import ProviderHealth, ProviderMode
from munshiji.providers.tts import SpeechResult

__all__ = ["SarvamTTS"]

_log = get_logger(__name__)


class SarvamTTS:
    """Live :class:`~munshiji.providers.tts.TTSProvider`.

    ``client_should_synthesise`` stays ``False`` here: real audio came back, so the browser must
    play it rather than fall back to ``speechSynthesis``.
    """

    name = "sarvam-bulbul"
    mode: ProviderMode = "live"
    kind = "tts"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: SarvamClient | None = None,
        model: str | None = None,
        speaker: str | None = None,
    ) -> None:
        # ``settings`` is positional so ``providers/factory.py`` can call ``SarvamTTS(settings)``.
        resolved = settings or get_settings()
        self.model = model or resolved.sarvam_tts_model
        self.speaker = speaker or resolved.sarvam_tts_speaker
        self._client = client or SarvamClient(settings=resolved)
        self._owns_client = client is None

    async def speak(
        self,
        text: str,
        *,
        language: str = "hi-IN",
        speaker: str | None = None,
        pace: float = 1.0,
    ) -> SpeechResult:
        """Synthesise ``text`` to WAV bytes in the merchant's language."""
        started = time.perf_counter()
        result = await synthesise(
            self._client,
            text,
            model=self.model,
            language=language,
            speaker=speaker or self.speaker,
            pace=pace,
        )
        return SpeechResult(
            audio=result.audio,
            mime_type="audio/wav",
            sample_rate=result.sample_rate,
            duration_ms=result.duration_ms,
            provider="live",
            model=result.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            client_should_synthesise=False,
            text=text,
            raw=result.raw,
        )

    async def health(self) -> ProviderHealth:
        """Cheap reachability probe; never raises."""
        try:
            ok, detail, latency_ms = await self._client.probe()
        except Exception as exc:
            _log.warning("sarvam tts health probe failed: %s", exc)
            ok, detail, latency_ms = False, f"{type(exc).__name__}: {exc}", 0
        return ProviderHealth(
            name=self.name,
            kind="tts",
            mode=self.mode,
            ok=ok,
            detail=f"{self.model}/{self.speaker}: {detail}",
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
