"""``SarvamSTT`` — live transcription via ``saaras`` (22 Indian languages, code-mixed aware)."""

from __future__ import annotations

import time

from munshiji.config import Settings, get_settings
from munshiji.integrations.sarvam.client import SarvamClient
from munshiji.integrations.sarvam.speech import transcribe
from munshiji.logging import get_logger
from munshiji.providers.base import ProviderHealth, ProviderMode
from munshiji.providers.stt import TranscriptResult

__all__ = ["SarvamSTT"]

_log = get_logger(__name__)

#: Extension guessed from the upload's MIME type, so the API sees a sensible filename.
_EXTENSIONS = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "audio/mp4": "m4a",
}


class SarvamSTT:
    """Live :class:`~munshiji.providers.stt.STTProvider`.

    Confidence is reported as ``1.0`` when the API returns no score of its own: the model does
    not publish one, and inventing a plausible-looking number would violate SPEC.md §2.2.
    """

    name = "sarvam-saaras"
    mode: ProviderMode = "live"
    kind = "stt"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: SarvamClient | None = None,
        model: str | None = None,
    ) -> None:
        # ``settings`` is positional so ``providers/factory.py`` can call ``SarvamSTT(settings)``.
        resolved = settings or get_settings()
        self.model = model or resolved.sarvam_stt_model
        self._client = client or SarvamClient(settings=resolved)
        self._owns_client = client is None

    async def transcribe(
        self,
        audio: bytes,
        *,
        language: str = "hi-IN",
        sample_rate: int = 16_000,
        mime_type: str = "audio/wav",
    ) -> TranscriptResult:
        """Transcribe a complete utterance.

        ``sample_rate`` is not sent — the API reads it from the container — but it is echoed
        back in ``raw`` so the voice route can log what the browser actually captured.
        """
        started = time.perf_counter()
        extension = _EXTENSIONS.get(mime_type.split(";")[0].strip().lower(), "wav")
        result = await transcribe(
            self._client,
            audio,
            model=self.model,
            language=language,
            mime_type=mime_type,
            filename=f"utterance.{extension}",
        )
        return TranscriptResult(
            text=result.text,
            language=result.language,
            is_final=True,
            confidence=1.0,
            provider="live",
            model=result.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            raw={"sample_rate": sample_rate, "bytes": len(audio), "response": result.raw},
        )

    async def health(self) -> ProviderHealth:
        """Cheap reachability probe; never raises."""
        try:
            ok, detail, latency_ms = await self._client.probe()
        except Exception as exc:
            _log.warning("sarvam stt health probe failed: %s", exc)
            ok, detail, latency_ms = False, f"{type(exc).__name__}: {exc}", 0
        return ProviderHealth(
            name=self.name,
            kind="stt",
            mode=self.mode,
            ok=ok,
            detail=f"{self.model}: {detail}",
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
