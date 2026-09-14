"""Voice endpoints: speech in, speech out, and a streaming socket for the live call panel."""

from __future__ import annotations

import base64
import contextlib
import json

from fastapi import APIRouter, File, Form, UploadFile, WebSocket, WebSocketDisconnect

from munshiji.api.deps import Providers
from munshiji.config import get_settings
from munshiji.db.base import get_sessionmaker
from munshiji.logging import get_logger
from munshiji.providers.factory import get_providers
from munshiji.providers.tts import SpeechResult
from munshiji.repositories.core import first_merchant, get_merchant
from munshiji.schemas.common import Meta
from munshiji.schemas.conversation import SpeakIn, SpeakOut, TranscribeOut

router = APIRouter(tags=["voice"])
logger = get_logger(__name__)

#: Refuse absurd uploads rather than buffering them; a merchant utterance is seconds long.
MAX_AUDIO_BYTES = 12 * 1024 * 1024


def audio_data_uri(result: SpeechResult) -> str | None:
    """Base64 data URI for the synthesised audio, or ``None`` when the client should speak it.

    The offline TTS returns a real but silent WAV and sets ``client_should_synthesise``, so the
    browser voices the text with ``speechSynthesis`` instead of playing silence.
    """
    if result.client_should_synthesise or not result.audio:
        return None
    encoded = base64.b64encode(result.audio).decode("ascii")
    return f"data:{result.mime_type};base64,{encoded}"


@router.post(
    "/voice/transcribe",
    response_model=TranscribeOut,
    summary="Transcribe a recorded utterance",
)
async def transcribe(
    providers: Providers,
    file: UploadFile = File(description="Recorded audio (WAV/WebM/OGG)."),
    language: str = Form(default=""),
) -> TranscribeOut:
    """Speech to text, code-mixed Hindi/English included."""
    audio = await file.read()
    if len(audio) > MAX_AUDIO_BYTES:
        from munshiji.errors import ValidationError

        raise ValidationError("audio upload too large", size=len(audio), limit=MAX_AUDIO_BYTES)

    settings = get_settings()
    result = await providers.stt.transcribe(
        audio,
        language=language or settings.default_language,
        mime_type=file.content_type or "audio/wav",
    )
    return TranscribeOut(
        text=result.text,
        language=result.language,
        confidence=result.confidence,
        meta=Meta(provider=result.provider, latency_ms=result.latency_ms),
    )


@router.post("/voice/speak", response_model=SpeakOut, summary="Synthesise a reply")
async def speak(payload: SpeakIn, providers: Providers) -> SpeakOut:
    """Text to speech in the merchant's language."""
    result = await providers.tts.speak(
        payload.text,
        language=payload.language,
        speaker=payload.speaker,
        pace=payload.pace,
    )
    return SpeakOut(
        audio_data_uri=audio_data_uri(result),
        mime_type=result.mime_type,
        duration_ms=result.duration_ms,
        client_should_synthesise=result.client_should_synthesise,
        text=payload.text,
        meta=Meta(provider=result.provider, latency_ms=result.latency_ms),
    )


@router.websocket("/voice/stream")
async def voice_stream(websocket: WebSocket) -> None:
    """Full-duplex call socket for the live panel.

    Protocol (JSON text frames, plus binary audio frames from the client):

    * client → ``{"type":"start","merchant_id":…,"conversation_id":…,"language":…}``
    * client → binary audio chunks, then ``{"type":"end"}`` to close the utterance
    * client → ``{"type":"text","text":…}`` to skip speech entirely (the reliable demo path)
    * server → ``{"type":"transcript",…}``, ``{"type":"reply",…}``, ``{"type":"audio",…}``,
      ``{"type":"error",…}``
    """
    await websocket.accept()
    providers = get_providers()
    settings = get_settings()
    session_factory = get_sessionmaker()

    merchant_id: str | None = None
    conversation_id: str | None = None
    language = settings.default_language
    buffer = bytearray()

    async def send(kind: str, **payload: object) -> None:
        await websocket.send_text(json.dumps({"type": kind, **payload}, default=str))

    try:
        while True:
            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":
                break

            if (raw := message.get("bytes")) is not None:
                buffer.extend(raw)
                if len(buffer) > MAX_AUDIO_BYTES:
                    await send("error", message="audio stream too large")
                    buffer.clear()
                continue

            text_frame = message.get("text")
            if not text_frame:
                continue

            try:
                frame = json.loads(text_frame)
            except json.JSONDecodeError:
                await send("error", message="expected a JSON control frame")
                continue

            kind = frame.get("type")

            if kind == "start":
                merchant_id = frame.get("merchant_id") or merchant_id
                conversation_id = frame.get("conversation_id") or conversation_id
                language = frame.get("language") or language
                buffer.clear()
                await send("ready", merchant_id=merchant_id, language=language)
                continue

            if kind in {"end", "text"}:
                if kind == "text":
                    utterance = str(frame.get("text") or "").strip()
                else:
                    if not buffer:
                        await send("error", message="no audio received")
                        continue
                    transcript = await providers.stt.transcribe(bytes(buffer), language=language)
                    buffer.clear()
                    utterance = transcript.text
                    language = transcript.language or language
                    await send(
                        "transcript",
                        text=utterance,
                        language=language,
                        provider=transcript.provider,
                        latency_ms=transcript.latency_ms,
                    )

                if not utterance:
                    await send("error", message="nothing to process")
                    continue

                result = await _run_agent_turn(
                    session_factory, merchant_id, utterance, conversation_id, language
                )
                if result is None:
                    await send("error", message="merchant not found")
                    continue

                conversation_id = result["conversation_id"]
                await send("reply", **result)

                speech = await providers.tts.speak(result["reply"], language=language)
                await send(
                    "audio",
                    audio_data_uri=audio_data_uri(speech),
                    client_should_synthesise=speech.client_should_synthesise,
                    text=result["reply"],
                    duration_ms=speech.duration_ms,
                    provider=speech.provider,
                )
                continue

            if kind == "close":
                break

    except WebSocketDisconnect:
        logger.debug("voice socket disconnected")
    except Exception:  # pragma: no cover - never leak a stack trace onto the wire
        logger.exception("voice socket failed")
        with contextlib.suppress(Exception):
            await send("error", message="internal error")
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()


async def _run_agent_turn(
    session_factory,
    merchant_id: str | None,
    text: str,
    conversation_id: str | None,
    language: str,
) -> dict[str, object] | None:
    """Run one turn against its own session, so a socket never holds a transaction open."""
    from munshiji.agent.loop import AgentLoop

    session = session_factory()
    try:
        merchant = (
            get_merchant(session, merchant_id)
            if merchant_id and merchant_id not in {"default", "me", "-"}
            else first_merchant(session)
        )
        if merchant is None:
            return None

        loop = AgentLoop(get_providers())
        result = await loop.run_turn(
            session,
            merchant,
            text,
            conversation_id=conversation_id,
            language=language,
        )
        session.commit()
        return {
            "conversation_id": result.conversation_id,
            "reply": result.reply,
            "language": result.language,
            "intent": result.intent,
            "provider": result.provider,
            "latency_ms": result.latency_ms,
            "tool_calls": [
                {
                    "name": call.name,
                    "ok": call.ok,
                    "summary": call.summary,
                    "latency_ms": call.latency_ms,
                }
                for call in result.tool_calls
            ],
            "pending_action_id": result.pending_action.id if result.pending_action else None,
            "executed_action_id": result.executed_action.id if result.executed_action else None,
            "memory_used": result.memory_used,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
