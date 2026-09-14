"""Speech-to-text provider protocol.

Live implementation targets Sarvam ``saaras:v3`` (22 Indian languages + English, with
``transcribe`` / ``translate`` / ``code-mixed`` output modes). The local implementation accepts
text passed through the audio envelope so the whole voice pipeline is exercisable offline —
and the frontend can fall back to the browser's own Web Speech API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from munshiji.providers.base import ProviderHealth, ProviderMode

__all__ = ["STTProvider", "TranscriptResult"]


@dataclass(slots=True)
class TranscriptResult:
    """A recognised utterance."""

    text: str
    language: str = "hi-IN"
    is_final: bool = True
    confidence: float = 1.0
    provider: ProviderMode = "local"
    model: str = ""
    latency_ms: int = 0
    raw: dict[str, Any] | None = None


@runtime_checkable
class STTProvider(Protocol):
    """Transcribe merchant speech, code-mixed Hindi/English included."""

    name: str
    mode: ProviderMode

    async def transcribe(
        self,
        audio: bytes,
        *,
        language: str = "hi-IN",
        sample_rate: int = 16_000,
        mime_type: str = "audio/wav",
    ) -> TranscriptResult:
        """Transcribe a complete utterance."""
        ...

    async def health(self) -> ProviderHealth: ...
