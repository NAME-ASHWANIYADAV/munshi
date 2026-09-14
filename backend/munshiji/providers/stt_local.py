"""``LocalSTT`` — offline transcription so the voice loop is testable without a vendor.

There is no offline ASR here and no pretence of one. Instead the transcript travels *inside* the
audio envelope, which keeps every other part of the pipeline real: the WebSocket carries bytes,
the route parses a WAV, the agent gets a :class:`TranscriptResult` with a provider badge, and the
browser's own Web Speech API supplies genuine recognition when a microphone is involved.

Two envelopes are accepted:

* a WAV whose RIFF ``LIST``/``INFO`` ``ICMT`` comment carries the text — build one with
  :func:`encode_text_wav` (used by the tests and by ``munshiji demo``);
* raw UTF-8 bytes that are not a WAV at all, taken as the transcript directly.
"""

from __future__ import annotations

import struct
import time

from munshiji.nlu import detect_language
from munshiji.providers.base import ProviderHealth, ProviderMode
from munshiji.providers.stt import TranscriptResult
from munshiji.providers.tts_local import build_wav

__all__ = ["TEXT_CHUNK_ID", "LocalSTT", "decode_text_wav", "encode_text_wav"]

#: RIFF ``INFO`` sub-chunk used for the transcript: ICMT is the standard "comment" field.
TEXT_CHUNK_ID = b"ICMT"
_RIFF, _WAVE, _LIST, _INFO = b"RIFF", b"WAVE", b"LIST", b"INFO"

DEFAULT_SAMPLE_RATE = 16_000
_SAMPLE_WIDTH = 2
_HEADER_SPLIT = 44  # canonical WAV header length produced by ``build_wav``
_MIN_FRAMES = 160  # 10 ms of silence, so the file is never zero-length audio


def encode_text_wav(
    text: str, *, sample_rate: int = DEFAULT_SAMPLE_RATE, duration_ms: int = 200
) -> bytes:
    """Build a valid WAV whose ``LIST``/``INFO``/``ICMT`` comment carries ``text``.

    The result is a real RIFF file: ``wave.open`` reads it, browsers play it (as silence), and
    :func:`decode_text_wav` gets the sentence back. Unknown chunks are skipped by every WAV
    parser, so the comment is invisible to everything that does not look for it.
    """
    frames = max(_MIN_FRAMES, int(sample_rate * max(0, duration_ms) / 1000))
    base = build_wav(b"\x00" * (frames * _SAMPLE_WIDTH), sample_rate=sample_rate)
    header, data_chunk = base[:_HEADER_SPLIT], base[_HEADER_SPLIT:]

    payload = text.encode("utf-8") + b"\x00"
    if len(payload) % 2:  # RIFF chunks are word-aligned
        payload += b"\x00"
    info = _INFO + TEXT_CHUNK_ID + struct.pack("<I", len(payload)) + payload
    list_chunk = _LIST + struct.pack("<I", len(info)) + info

    # ``header`` is RIFF(12) + fmt (24) + the 8-byte "data" header; splice LIST before "data".
    body = header[12:36] + list_chunk + header[36:] + data_chunk
    return _RIFF + struct.pack("<I", 4 + len(body)) + _WAVE + body


def decode_text_wav(data: bytes) -> str | None:
    """Recover the text written by :func:`encode_text_wav`, or ``None`` if there is none."""
    if len(data) < 12 or data[:4] != _RIFF or data[8:12] != _WAVE:
        return None
    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        (size,) = struct.unpack("<I", data[offset + 4 : offset + 8])
        body = data[offset + 8 : offset + 8 + size]
        if chunk_id == _LIST and body[:4] == _INFO:
            found = _find_info_field(body[4:])
            if found is not None:
                return found
        offset += 8 + size + (size % 2)
    return None


def _find_info_field(body: bytes) -> str | None:
    offset = 0
    while offset + 8 <= len(body):
        field_id = body[offset : offset + 4]
        (size,) = struct.unpack("<I", body[offset + 4 : offset + 8])
        value = body[offset + 8 : offset + 8 + size]
        if field_id == TEXT_CHUNK_ID:
            return value.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        offset += 8 + size + (size % 2)
    return None


class LocalSTT:
    """Offline :class:`~munshiji.providers.stt.STTProvider` over the text-carrying envelope."""

    name = "munshiji-local-stt"
    mode: ProviderMode = "local"
    kind = "stt"

    def __init__(self, *, model: str = "local-envelope-v1") -> None:
        self.model = model

    async def transcribe(
        self,
        audio: bytes,
        *,
        language: str = "hi-IN",
        sample_rate: int = 16_000,
        mime_type: str = "audio/wav",
    ) -> TranscriptResult:
        """Read the transcript out of the envelope and detect its language.

        Confidence is ``1.0`` when text was recovered and ``0.0`` when the envelope carried
        none — an honest signal, not a decorative one.
        """
        started = time.perf_counter()
        text, source = _read_envelope(audio)
        detected = detect_language(text) if text else language
        return TranscriptResult(
            text=text,
            language=detected,
            is_final=True,
            confidence=1.0 if text else 0.0,
            provider="local",
            model=self.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            raw={
                "source": source,
                "bytes": len(audio),
                "sample_rate": sample_rate,
                "mime_type": mime_type,
                "requested_language": language,
            },
        )

    async def health(self) -> ProviderHealth:
        """Always healthy: decoding is pure byte arithmetic."""
        return ProviderHealth(
            name=self.name,
            kind="stt",
            mode=self.mode,
            ok=True,
            detail=f"{self.model}: WAV LIST/INFO comment or raw UTF-8 envelope",
            latency_ms=0,
        )


def _read_envelope(audio: bytes) -> tuple[str, str]:
    """``(text, how_we_got_it)`` — a WAV comment, raw UTF-8 text, or nothing."""
    if not audio:
        return "", "empty"
    if audio[:4] == _RIFF and audio[8:12] == _WAVE:
        return (decode_text_wav(audio) or "").strip(), "wav_comment"
    try:
        decoded = audio.decode("utf-8").strip()
    except UnicodeDecodeError:
        return "", "opaque_audio"
    return decoded, "utf8_bytes"
