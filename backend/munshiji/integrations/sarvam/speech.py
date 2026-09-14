"""Sarvam speech endpoints: ``saaras`` transcription and ``bulbul`` synthesis.

Both calls are thin — the interesting part is the parsing, which is deliberately forgiving
about where in the payload the useful bytes live (see :func:`~…client.find_value`).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import io
import wave
from dataclasses import dataclass, field
from typing import Any

from munshiji.errors import ProviderUnavailableError
from munshiji.integrations.sarvam.client import (
    SARVAM_STT_PATH,
    SARVAM_TTS_PATH,
    SarvamClient,
    find_value,
)
from munshiji.logging import get_logger

__all__ = [
    "AUDIO_KEYS",
    "TRANSCRIPT_KEYS",
    "SarvamSpeech",
    "SarvamTranscript",
    "synthesise",
    "transcribe",
    "wav_duration_ms",
]

_log = get_logger(__name__)

#: Keys that have plausibly carried the transcript, most specific first.
TRANSCRIPT_KEYS = ("transcript", "transcription", "transcript_text", "text", "output")
#: Keys that have plausibly carried base64 audio, most specific first.
AUDIO_KEYS = ("audios", "audio", "audio_base64", "audio_content", "audio_data")
#: Keys that have plausibly carried the recognised language tag.
LANGUAGE_KEYS = ("language_code", "detected_language_code", "detected_language", "language")

#: Fallback when the returned audio is not a parseable WAV: Hindi TTS runs near this rate.
_FALLBACK_WORDS_PER_MINUTE = 130.0


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


@dataclass(slots=True)
class SarvamTranscript:
    """A transcript returned by ``/speech-to-text``."""

    text: str
    language: str
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SarvamSpeech:
    """Synthesised audio returned by ``/text-to-speech``."""

    audio: bytes
    sample_rate: int
    duration_ms: int
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


async def transcribe(
    client: SarvamClient,
    audio: bytes,
    *,
    model: str,
    language: str = "hi-IN",
    mime_type: str = "audio/wav",
    filename: str = "utterance.wav",
) -> SarvamTranscript:
    """POST an utterance as multipart form-data and pull the transcript out of the reply.

    ``language`` is Sarvam's ``language_code`` (``"hi-IN"``, ``"en-IN"``, …); passing
    ``"unknown"`` asks the model to auto-detect.
    """
    payload = await client.request_json(
        "POST",
        SARVAM_STT_PATH,
        data={"model": model, "language_code": language},
        files={"file": (filename, audio, mime_type)},
    )
    text = find_value(payload, TRANSCRIPT_KEYS, accept=_non_empty_str)
    if text is None:
        raise ProviderUnavailableError(
            "Sarvam speech-to-text returned no transcript",
            provider="sarvam",
            keys=sorted(payload)[:10],
        )
    detected = find_value(payload, LANGUAGE_KEYS, accept=_non_empty_str)
    returned_model = find_value(payload, ("model",), accept=_non_empty_str)
    return SarvamTranscript(
        text=str(text).strip(),
        language=str(detected) if detected else language,
        model=str(returned_model) if returned_model else model,
        raw=payload,
    )


async def synthesise(
    client: SarvamClient,
    text: str,
    *,
    model: str,
    language: str = "hi-IN",
    speaker: str = "anushka",
    pace: float = 1.0,
) -> SarvamSpeech:
    """POST text as JSON and decode the base64 WAV (or WAV chunks) that come back."""
    body: dict[str, Any] = {
        "text": text,
        "target_language_code": language,
        "speaker": speaker,
        "model": model,
        "pace": pace,
    }
    payload = await client.request_json("POST", SARVAM_TTS_PATH, json=body)
    audio = _decode_audio(payload)
    if not audio:
        raise ProviderUnavailableError(
            "Sarvam text-to-speech returned no audio",
            provider="sarvam",
            keys=sorted(payload)[:10],
        )
    sample_rate, duration = _describe_wav(audio, text)
    returned_model = find_value(payload, ("model",), accept=_non_empty_str)
    return SarvamSpeech(
        audio=audio,
        sample_rate=sample_rate,
        duration_ms=duration,
        model=str(returned_model) if returned_model else model,
        raw={key: value for key, value in payload.items() if key not in AUDIO_KEYS},
    )


# ── Parsing ─────────────────────────────────────────────────────────────────────────────────


def _accept_audio(value: Any) -> bool:
    if _non_empty_str(value):
        return True
    return isinstance(value, list) and any(_non_empty_str(item) for item in value)


def _decode_audio(payload: dict[str, Any]) -> bytes:
    """Decode ``audios`` / ``audio`` / ``audio_base64``, merging multi-chunk replies."""
    value = find_value(payload, AUDIO_KEYS, accept=_accept_audio)
    if value is None:
        return b""
    encoded = [value] if isinstance(value, str) else [c for c in value if _non_empty_str(c)]

    chunks: list[bytes] = []
    for item in encoded:
        try:
            chunks.append(base64.b64decode(item, validate=False))
        except (binascii.Error, ValueError):
            _log.warning("sarvam tts chunk was not valid base64; skipped")
    if not chunks:
        return b""
    if len(chunks) == 1:
        return chunks[0]
    return _merge_wavs(chunks)


def _merge_wavs(chunks: list[bytes]) -> bytes:
    """Concatenate several WAV chunks into one playable file.

    Raw byte concatenation would produce a file with several RIFF headers, which browsers stop
    playing after the first chunk — so the frames are re-muxed under a single header.
    """
    frames: list[bytes] = []
    params: Any = None
    for chunk in chunks:
        try:
            with wave.open(io.BytesIO(chunk), "rb") as handle:
                params = params or handle.getparams()
                frames.append(handle.readframes(handle.getnframes()))
        except (wave.Error, EOFError):
            return chunks[0]
    if params is None:
        return chunks[0]

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(params.nchannels)
        out.setsampwidth(params.sampwidth)
        out.setframerate(params.framerate)
        out.writeframes(b"".join(frames))
    return buffer.getvalue()


_UNREADABLE = (wave.Error, EOFError, ValueError)


def wav_duration_ms(data: bytes) -> int | None:
    """Duration of a WAV in milliseconds, or ``None`` if it is not parseable."""
    with (
        contextlib.suppress(*_UNREADABLE),
        wave.open(io.BytesIO(data), "rb") as handle,
    ):
        rate = handle.getframerate()
        if rate > 0:
            return int(handle.getnframes() / rate * 1000)
    return None


def _describe_wav(audio: bytes, text: str) -> tuple[int, int]:
    """``(sample_rate, duration_ms)`` from the audio itself, estimated from text if unreadable."""
    with (
        contextlib.suppress(*_UNREADABLE),
        wave.open(io.BytesIO(audio), "rb") as handle,
    ):
        rate = handle.getframerate() or 22_050
        return rate, int(handle.getnframes() / rate * 1000)
    words = max(1, len(text.split()))
    return 22_050, int(words / _FALLBACK_WORDS_PER_MINUTE * 60_000)
