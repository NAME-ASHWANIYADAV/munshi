"""Text-to-speech provider protocol.

Live implementation targets Sarvam ``bulbul:v3`` (11 Indian languages, streaming WebSocket for
low first-byte latency). The local implementation synthesises a valid, silent WAV of
proportionate duration so the client's audio pipeline, timing and UI states are all still
exercised offline — and the browser's own ``speechSynthesis`` can voice the text instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from munshiji.providers.base import ProviderHealth, ProviderMode

__all__ = ["SpeechResult", "TTSProvider"]


@dataclass(slots=True)
class SpeechResult:
    """Synthesised audio for one utterance."""

    audio: bytes
    mime_type: str = "audio/wav"
    sample_rate: int = 22_050
    duration_ms: int = 0
    provider: ProviderMode = "local"
    model: str = ""
    latency_ms: int = 0
    #: Set by the local provider so the browser can speak the text itself instead.
    client_should_synthesise: bool = False
    text: str = ""
    raw: dict[str, Any] | None = None


@runtime_checkable
class TTSProvider(Protocol):
    """Speak MunshiJi's reply in the merchant's language."""

    name: str
    mode: ProviderMode

    async def speak(
        self,
        text: str,
        *,
        language: str = "hi-IN",
        speaker: str | None = None,
        pace: float = 1.0,
    ) -> SpeechResult:
        """Synthesise ``text`` to audio bytes."""
        ...

    async def health(self) -> ProviderHealth: ...
