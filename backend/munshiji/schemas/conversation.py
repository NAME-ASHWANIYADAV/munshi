"""Chat and voice turns."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from munshiji.db.enums import TurnRole
from munshiji.db.models import Turn
from munshiji.schemas.action import ActionOut
from munshiji.schemas.common import ApiModel, Meta

__all__ = [
    "ChatIn",
    "ConversationOut",
    "SpeakIn",
    "SpeakOut",
    "ToolCallOut",
    "TranscribeOut",
    "TurnOut",
    "TurnResultOut",
]


class ChatIn(ApiModel):
    merchant_id: str
    text: str
    conversation_id: str | None = None
    language: str | None = None
    #: When true the reply is also synthesised to audio and returned as a data URI.
    speak: bool = False


class ToolCallOut(ApiModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    latency_ms: int = 0
    #: Compact, human-readable summary of what the tool returned — shown in the UI trace.
    summary: str = ""


class TurnOut(ApiModel):
    id: str
    seq: int
    role: TurnRole
    text: str
    text_display: str = ""
    created_at: datetime
    latency_ms: int = 0
    provider: str = "local"

    @classmethod
    def from_model(cls, turn: Turn) -> TurnOut:
        return cls(
            id=turn.id,
            seq=turn.seq,
            role=turn.role,
            text=turn.text,
            text_display=turn.text_display or turn.text,
            created_at=turn.created_at,
            latency_ms=turn.latency_ms,
            provider=turn.provider,
        )


class TurnResultOut(ApiModel):
    """Everything one conversational turn produced — the core API response."""

    conversation_id: str
    reply: str
    reply_display: str = ""
    language: str = "hi-IN"
    intent: str = ""
    intent_confidence: float = 0.0
    tool_calls: list[ToolCallOut] = Field(default_factory=list)
    #: Populated when this turn proposed something that needs a yes.
    pending_action: ActionOut | None = None
    #: Populated when this turn's approval caused an action to execute.
    executed_action: ActionOut | None = None
    memory_used: list[str] = Field(default_factory=list)
    audio_data_uri: str | None = None
    client_should_synthesise: bool = False
    meta: Meta = Field(default_factory=Meta)


class ConversationOut(ApiModel):
    id: str
    merchant_id: str
    channel: str
    language: str
    started_at: datetime
    ended_at: datetime | None = None
    summary: str = ""
    turns: list[TurnOut] = Field(default_factory=list)


class TranscribeOut(ApiModel):
    text: str
    language: str = "hi-IN"
    confidence: float = 1.0
    meta: Meta = Field(default_factory=Meta)


class SpeakIn(ApiModel):
    text: str
    language: str = "hi-IN"
    speaker: str | None = None
    pace: float = 1.0


class SpeakOut(ApiModel):
    audio_data_uri: str | None = None
    mime_type: str = "audio/wav"
    duration_ms: int = 0
    client_should_synthesise: bool = False
    text: str = ""
    meta: Meta = Field(default_factory=Meta)
