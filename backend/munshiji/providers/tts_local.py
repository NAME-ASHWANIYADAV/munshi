"""``LocalTTS`` — offline speech synthesis that keeps the whole audio pipeline honest.

We cannot run a neural vocoder offline, and pretending to would be fake. What we *can* do is
emit a **real, playable WAV** of the right duration, so every downstream consumer — the browser's
``<audio>`` element, the latency badge, the "MunshiJi is speaking" UI state, the audio cache on
disk — is exercised exactly as it is in live mode. The file is silent; alongside it the result
carries ``client_should_synthesise=True`` and the text, so the browser voices the reply with
``speechSynthesis`` and the merchant still hears Hindi.
"""

from __future__ import annotations

import struct
import time

from munshiji.providers.base import ProviderHealth, ProviderMode
from munshiji.providers.tts import SpeechResult

__all__ = ["WAV_HEADER_BYTES", "LocalTTS", "build_wav", "estimate_duration_ms"]

#: A canonical PCM WAV header is exactly this long: RIFF(12) + fmt (24) + data(8).
WAV_HEADER_BYTES = 44

DEFAULT_SAMPLE_RATE = 22_050
_SAMPLE_WIDTH = 2  # 16-bit
_CHANNELS = 1  # mono

#: Measured pace of conversational Hindi/Hinglish; English runs faster.
WORDS_PER_MINUTE_HI = 85.0
WORDS_PER_MINUTE_EN = 120.0
#: Even one word needs a beat; nothing spoken aloud should run past twenty seconds.
MIN_DURATION_S = 0.6
MAX_DURATION_S = 20.0


def build_wav(
    pcm: bytes,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    channels: int = _CHANNELS,
    sample_width: int = _SAMPLE_WIDTH,
) -> bytes:
    """Wrap raw PCM in a canonical 44-byte RIFF/WAVE header."""
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    header = b"".join(
        (
            b"RIFF",
            struct.pack("<I", 36 + len(pcm)),
            b"WAVE",
            b"fmt ",
            struct.pack(
                "<IHHIIHH",
                16,  # PCM fmt chunk size
                1,  # format tag: PCM
                channels,
                sample_rate,
                byte_rate,
                block_align,
                sample_width * 8,
            ),
            b"data",
            struct.pack("<I", len(pcm)),
        )
    )
    return header + pcm


def estimate_duration_ms(text: str, *, language: str = "hi-IN", pace: float = 1.0) -> int:
    """How long ``text`` takes to say, in milliseconds, clamped to a sane speaking range."""
    words = max(1, len(text.split()))
    per_minute = WORDS_PER_MINUTE_HI if language.lower().startswith("hi") else WORDS_PER_MINUTE_EN
    seconds = words / (per_minute / 60.0) / max(0.25, pace)
    return int(min(MAX_DURATION_S, max(MIN_DURATION_S, seconds)) * 1000)


class LocalTTS:
    """Offline :class:`~munshiji.providers.tts.TTSProvider` producing real, timed WAV audio."""

    name = "munshiji-local-tts"
    mode: ProviderMode = "local"
    kind = "tts"

    def __init__(
        self, *, sample_rate: int = DEFAULT_SAMPLE_RATE, model: str = "local-silence-v1"
    ) -> None:
        self.sample_rate = sample_rate
        self.model = model

    async def speak(
        self,
        text: str,
        *,
        language: str = "hi-IN",
        speaker: str | None = None,
        pace: float = 1.0,
    ) -> SpeechResult:
        """Synthesise a silent but structurally valid WAV of proportionate duration.

        ``speaker`` is recorded, not used: the browser picks its own ``speechSynthesis`` voice.
        """
        started = time.perf_counter()
        duration_ms = estimate_duration_ms(text, language=language, pace=pace)
        frames = int(self.sample_rate * duration_ms / 1000)
        audio = build_wav(b"\x00" * (frames * _SAMPLE_WIDTH), sample_rate=self.sample_rate)
        return SpeechResult(
            audio=audio,
            mime_type="audio/wav",
            sample_rate=self.sample_rate,
            duration_ms=int(frames / self.sample_rate * 1000),
            provider="local",
            model=self.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            client_should_synthesise=True,
            text=text,
            raw={
                "language": language,
                "speaker": speaker,
                "pace": pace,
                "words": len(text.split()),
            },
        )

    async def health(self) -> ProviderHealth:
        """Always healthy: the encoder is ``struct.pack``."""
        return ProviderHealth(
            name=self.name,
            kind="tts",
            mode=self.mode,
            ok=True,
            detail=f"{self.model}: {self.sample_rate} Hz mono WAV, client speechSynthesis",
            latency_ms=0,
        )
