"""Sarvam chat completions — the OpenAI-compatible surface used for tool calling.

``sarvam-105b`` speaks the OpenAI wire format, so the payload is built straight from
:meth:`ChatMessage.as_openai_dict` / :meth:`ToolSpec.as_openai_dict` with exactly one fix-up:
OpenAI requires ``tool_calls[].function.arguments`` to be a **JSON string**, while our dataclass
carries it as a dict. The same asymmetry shows up on the way back — some deployments return the
arguments as an object, others as a string — so :func:`parse_chat_response` accepts both.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from munshiji.integrations.sarvam.client import SARVAM_CHAT_PATH, SarvamClient, find_value
from munshiji.logging import get_logger
from munshiji.providers.llm import ChatMessage, ToolCall, ToolSpec

__all__ = [
    "ParsedCompletion",
    "build_chat_payload",
    "chat_completion",
    "parse_chat_response",
    "parse_tool_arguments",
]

_log = get_logger(__name__)

#: Where the assistant's prose has been found, most specific first.
TEXT_KEYS = ("content", "text", "output_text", "message")
#: Where the stop reason has been found.
FINISH_KEYS = ("finish_reason", "stop_reason", "finishReason")


@dataclass(slots=True)
class ParsedCompletion:
    """A chat completion reduced to the three things the agent loop cares about."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def build_chat_payload(
    messages: list[ChatMessage],
    tools: list[ToolSpec] | None,
    *,
    model: str,
    temperature: float = 0.3,
    max_tokens: int = 800,
    tool_choice: str = "auto",
) -> dict[str, Any]:
    """Serialise a turn into the OpenAI-compatible request body Sarvam accepts."""
    serialised = [_with_string_arguments(message.as_openai_dict()) for message in messages]
    payload: dict[str, Any] = {
        "model": model,
        "messages": serialised,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = [tool.as_openai_dict() for tool in tools]
        payload["tool_choice"] = tool_choice
    return payload


def _with_string_arguments(message: dict[str, Any]) -> dict[str, Any]:
    """Re-encode ``tool_calls[].function.arguments`` as a JSON string, as the API expects."""
    calls = message.get("tool_calls")
    if not calls:
        return message
    fixed = []
    for call in calls:
        function = dict(call.get("function", {}))
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            function["arguments"] = json.dumps(arguments or {}, ensure_ascii=False)
        fixed.append({**call, "function": function})
    return {**message, "tool_calls": fixed}


async def chat_completion(
    client: SarvamClient,
    messages: list[ChatMessage],
    tools: list[ToolSpec] | None = None,
    *,
    model: str,
    temperature: float = 0.3,
    max_tokens: int = 800,
    tool_choice: str = "auto",
) -> ParsedCompletion:
    """Call ``/v1/chat/completions`` and parse the reply into a :class:`ParsedCompletion`."""
    body = build_chat_payload(
        messages,
        tools,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        tool_choice=tool_choice,
    )
    payload = await client.request_json("POST", SARVAM_CHAT_PATH, json=body)
    return parse_chat_response(payload, fallback_model=model)


# ── Parsing ─────────────────────────────────────────────────────────────────────────────────


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """Coerce a tool call's ``arguments`` into a dict.

    Handles the three shapes seen in the wild: an object, a JSON string, and (rarely) a
    double-encoded JSON string. Anything else degrades to ``{}`` with a warning rather than
    taking the turn down — the agent loop can still ask the merchant to rephrase.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        for _ in range(2):  # tolerate a double-encoded string
            try:
                decoded = json.loads(text)
            except (ValueError, TypeError):
                break
            if isinstance(decoded, dict):
                return decoded
            if isinstance(decoded, str):
                text = decoded
                continue
            break
    _log.warning("sarvam tool_call arguments were not decodable: %r", raw)
    return {}


def _extract_text(message: Any) -> str:
    """Assistant prose, whether it arrived as a string or as a list of content parts."""
    value = find_value(message, TEXT_KEYS, accept=lambda item: isinstance(item, str | list))
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [
            part.get("text", "")
            for part in value
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        return "".join(parts).strip()
    return ""


def _is_list(value: Any) -> bool:
    return isinstance(value, list)


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _extract_tool_calls(message: Any) -> list[ToolCall]:
    raw_calls = find_value(message, ("tool_calls", "toolCalls"), accept=_is_list)
    if not raw_calls:
        return []
    calls: list[ToolCall] = []
    for index, entry in enumerate(raw_calls):
        if not isinstance(entry, dict):
            continue
        function = entry.get("function") if isinstance(entry.get("function"), dict) else entry
        name = function.get("name") or entry.get("name")
        if not name:
            continue
        calls.append(
            ToolCall(
                id=str(entry.get("id") or f"call_{index}"),
                name=str(name),
                arguments=parse_tool_arguments(function.get("arguments")),
            )
        )
    return calls


def parse_chat_response(payload: dict[str, Any], *, fallback_model: str = "") -> ParsedCompletion:
    """Reduce a completion body to text + tool calls, tolerating several response shapes."""
    choices = payload.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else payload
    message = first.get("message") if isinstance(first, dict) else None
    if not isinstance(message, dict):
        message = first.get("delta") if isinstance(first, dict) else None
    scope: Any = message if isinstance(message, dict) else first

    tool_calls = _extract_tool_calls(scope)
    text = _extract_text(scope)
    finish = find_value(first, FINISH_KEYS, accept=_is_text)
    model = find_value(payload, ("model",), accept=_is_text)

    default_finish = "tool_calls" if tool_calls else "stop"
    return ParsedCompletion(
        text=text,
        tool_calls=tool_calls,
        finish_reason=str(finish) if finish else default_finish,
        model=str(model) if model else fallback_model,
        raw=payload,
    )
