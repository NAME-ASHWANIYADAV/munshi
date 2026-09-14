"""LLM provider protocol — the reasoning core of the agent.

Live implementation targets Sarvam's Chat Completions API (``sarvam-105b``); the local
implementation is a deterministic intent router + bilingual response composer that works from
real tool results. Both return the same :class:`LLMResponse`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from munshiji.providers.base import ProviderHealth, ProviderMode

__all__ = [
    "ChatMessage",
    "LLMProvider",
    "LLMResponse",
    "Role",
    "ToolCall",
    "ToolSpec",
]

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class ToolCall:
    """A model's request to invoke one tool."""

    id: str
    name: str
    arguments: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(slots=True)
class ChatMessage:
    """One message in the conversation sent to the model."""

    role: Role
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)

    def as_openai_dict(self) -> dict[str, Any]:
        """Serialise in the OpenAI-compatible shape Sarvam's chat API accepts."""
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            payload["name"] = self.name
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return payload


@dataclass(slots=True)
class ToolSpec:
    """A tool advertised to the model, in JSON-schema form."""

    name: str
    description: str
    parameters: dict[str, Any]

    def as_openai_dict(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(slots=True)
class LLMResponse:
    """What the model produced: prose, tool calls, or both."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider: ProviderMode = "local"
    model: str = ""
    latency_ms: int = 0
    finish_reason: str = "stop"
    raw: dict[str, Any] | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@runtime_checkable
class LLMProvider(Protocol):
    """Chat completion with tool calling."""

    name: str
    mode: ProviderMode

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.3,
        max_tokens: int = 800,
        language: str = "hi-IN",
    ) -> LLMResponse:
        """Produce the next assistant message, optionally requesting tool calls."""
        ...

    async def health(self) -> ProviderHealth:
        """Probe readiness without side effects."""
        ...
