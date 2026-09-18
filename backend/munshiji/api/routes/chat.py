"""Text conversation — the same agent loop the voice socket drives, over plain JSON.

This is deliberately a first-class path, not a debug aid: on a noisy demo floor, typing is the
fallback that always works.
"""

from __future__ import annotations

from fastapi import APIRouter

from munshiji.agent.loop import AgentLoop, TurnResult
from munshiji.api.deps import DbSession, Providers, resolve_merchant
from munshiji.api.routes.voice import audio_data_uri
from munshiji.errors import ProviderUnavailableError
from munshiji.schemas.action import ActionOut
from munshiji.schemas.common import Meta
from munshiji.schemas.conversation import ChatIn, ToolCallOut, TurnResultOut

router = APIRouter(tags=["chat"])


def _to_payload(
    result: TurnResult, *, audio_uri: str | None = None, client_tts: bool = False
) -> TurnResultOut:
    return TurnResultOut(
        conversation_id=result.conversation_id,
        reply=result.reply,
        reply_display=result.reply,
        language=result.language,
        intent=result.intent,
        intent_confidence=round(result.intent_confidence, 2),
        tool_calls=[
            ToolCallOut(
                name=call.name,
                arguments=call.arguments,
                ok=call.ok,
                latency_ms=call.latency_ms,
                summary=call.summary,
            )
            for call in result.tool_calls
        ],
        pending_action=ActionOut.from_model(result.pending_action)
        if result.pending_action
        else None,
        executed_action=(
            ActionOut.from_model(result.executed_action) if result.executed_action else None
        ),
        memory_used=result.memory_used,
        audio_data_uri=audio_uri,
        client_should_synthesise=client_tts,
        meta=Meta(provider=result.provider, latency_ms=result.latency_ms),
    )


@router.post("/chat", response_model=TurnResultOut, summary="One conversational turn")
async def chat(payload: ChatIn, session: DbSession, providers: Providers) -> TurnResultOut:
    """Send the merchant's message and get MunshiJi's reply.

    A turn may propose an action (``pending_action``) which the merchant then approves — either by
    answering *haan* in the next turn, or through ``POST /api/actions/{id}/approve``.
    """
    merchant = resolve_merchant(payload.merchant_id, session)
    loop = AgentLoop(providers)
    result = await loop.run_turn(
        session,
        merchant,
        payload.text,
        conversation_id=payload.conversation_id,
        language=payload.language,
    )

    audio_uri: str | None = None
    client_tts = False
    if payload.speak and result.reply:
        try:
            speech = await providers.tts.speak(result.reply, language=result.language)
            audio_uri = audio_data_uri(speech)
            client_tts = speech.client_should_synthesise
        except ProviderUnavailableError:
            # The reply is composed and PAID FOR by this point — a flaky TTS vendor must not
            # turn a finished answer into a 503. The client speaks it with its own voice.
            client_tts = True

    return _to_payload(result, audio_uri=audio_uri, client_tts=client_tts)
