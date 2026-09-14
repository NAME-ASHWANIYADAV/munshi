"""Voice + reasoning provider tests. Fully offline — Sarvam is driven by ``httpx.MockTransport``.

Two properties matter most here and are asserted rather than assumed:

* ``LocalLLM`` only ever calls a tool that was advertised to it, and
* ``LocalLLM`` speaks **only** numbers that were in the tool result (SPEC.md §2.2).
"""

from __future__ import annotations

import base64
import io
import json
import re
import wave
from typing import Any

import httpx
import pytest

from munshiji.errors import ProviderUnavailableError
from munshiji.integrations.sarvam.chat import parse_chat_response, parse_tool_arguments
from munshiji.integrations.sarvam.client import AUTH_HEADER, SARVAM_CHAT_PATH, SarvamClient
from munshiji.providers.llm import ChatMessage, LLMProvider, ToolCall, ToolSpec
from munshiji.providers.llm_local import MAX_SPOKEN_WORDS, TEMPLATES, LocalLLM
from munshiji.providers.llm_sarvam import SarvamLLM
from munshiji.providers.stt import STTProvider
from munshiji.providers.stt_local import LocalSTT, decode_text_wav, encode_text_wav
from munshiji.providers.stt_sarvam import SarvamSTT
from munshiji.providers.tts import TTSProvider
from munshiji.providers.tts_local import WAV_HEADER_BYTES, LocalTTS, build_wav
from munshiji.providers.tts_sarvam import SarvamTTS

BASE_URL = "https://api.sarvam.test"
#: A rupee figure ends on a digit — a trailing comma belongs to the sentence, not the number.
RUPEE_FIGURE = re.compile(r"₹[\d,]*\d(?:\.\d+)?")
#: Any number spoken in a reply, grouping separators included.
ANY_NUMBER = re.compile(r"\d[\d,]*\d|\d")

# ── Fixtures / helpers ──────────────────────────────────────────────────────────────────────

TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="get_sales_summary",
        description="Today's collection so far.",
        parameters={
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": ["today", "yesterday", "last_week"],
                    "default": "today",
                },
                "language": {"type": "string"},
            },
            "required": ["period"],
        },
    ),
    ToolSpec(
        name="find_dormant_customers",
        description="Regulars who stopped coming.",
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer"}, "min_days": {"type": "integer"}},
            "required": ["limit"],
        },
    ),
    ToolSpec(
        name="send_winback_offer",
        description="Send a win-back offer (requires approval).",
        parameters={
            "type": "object",
            "properties": {
                "discount_pct": {"type": "integer"},
                "discount_amount_rupees": {"type": "integer"},
                "count": {"type": "integer"},
                "tone": {"type": "string", "enum": ["gentle", "standard", "firm"]},
            },
            "required": ["count"],
        },
    ),
]


def mock_client(handler: Any, **kwargs: Any) -> SarvamClient:
    """A Sarvam client wired to an in-memory transport; no socket is ever opened."""
    options: dict[str, Any] = {
        "api_key": "test-key",
        "base_url": BASE_URL,
        "timeout": 5.0,
        "retry_backoff": 0.0,
        "transport": httpx.MockTransport(handler),
    }
    options.update(kwargs)
    return SarvamClient(**options)


def json_response(payload: dict[str, Any], status: int = 200) -> Any:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


def tool_message(name: str, payload: dict[str, Any]) -> ChatMessage:
    return ChatMessage(role="tool", content=json.dumps(payload), name=name, tool_call_id="c1")


# ── LocalLLM: tool selection ────────────────────────────────────────────────────────────────


async def test_local_llm_emits_a_tool_call_for_a_sales_question() -> None:
    llm = LocalLLM()
    messages = [
        ChatMessage("system", "MunshiJi"),
        ChatMessage("user", "aaj ka dhandha kaisa raha?"),
    ]

    response = await llm.complete(messages, TOOLS)

    assert response.provider == "local"
    assert response.finish_reason == "tool_calls"
    assert response.text == ""
    assert [call.name for call in response.tool_calls] == ["get_sales_summary"]
    assert response.tool_calls[0].arguments["period"] == "today"
    assert response.raw is not None and response.raw["intent"]["name"] == "sales_summary"


@pytest.mark.parametrize(
    ("utterance", "tool"),
    [
        ("आज कितना आया?", "get_sales_summary"),
        ("kaun kaun nahi aa raha aajkal", "find_dormant_customers"),
        ("unhe 50 rupaye ka offer bhej do", "send_winback_offer"),
        ("today's collection", "get_sales_summary"),
    ],
)
async def test_local_llm_maps_intents_onto_advertised_tools(utterance: str, tool: str) -> None:
    response = await LocalLLM().complete([ChatMessage("user", utterance)], TOOLS)
    assert [call.name for call in response.tool_calls] == [tool]


async def test_local_llm_never_invents_a_tool() -> None:
    """A sales question with only an unrelated tool advertised must not fabricate one."""
    only_udhaar = [ToolSpec("get_udhaar_summary", "Udhaar", {"type": "object", "properties": {}})]
    response = await LocalLLM().complete([ChatMessage("user", "aaj ka dhandha")], only_udhaar)

    assert response.tool_calls == []
    assert response.text, "an unserviceable request must still get an answer"


async def test_local_llm_asks_a_clarifying_question_when_the_intent_is_unknown() -> None:
    llm = LocalLLM()
    hindi = await llm.complete([ChatMessage("user", "cricket ka score")], TOOLS)
    english = await llm.complete([ChatMessage("user", "cricket ka score")], TOOLS, language="en-IN")

    assert hindi.tool_calls == [] and english.tool_calls == []
    assert hindi.text.endswith("?") and english.text.endswith("?")
    assert re.search(r"[\u0900-\u097f]", hindi.text), "hi-IN replies must be Devanagari"
    assert not re.search(r"[\u0900-\u097f]", english.text), "en-IN replies must not be"


async def test_local_llm_fills_slots_and_schema_defaults() -> None:
    response = await LocalLLM().complete(
        [ChatMessage("user", "20% discount bhej do 12 customers ko")], TOOLS
    )
    arguments = response.tool_calls[0].arguments

    assert arguments["discount_pct"] == 20
    assert arguments["count"] == 12
    assert set(arguments) <= set(TOOLS[2].parameters["properties"]), "no undeclared parameters"


async def test_local_llm_supplies_required_parameters_nothing_filled() -> None:
    response = await LocalLLM().complete([ChatMessage("user", "kaun nahi aa raha")], TOOLS)
    assert "limit" in response.tool_calls[0].arguments


# ── LocalLLM: composition ───────────────────────────────────────────────────────────────────


async def test_local_llm_composition_speaks_only_the_tool_result_numbers() -> None:
    """The reply must contain the formatted figure — and no rupee figure that is not in it."""
    messages = [
        ChatMessage("user", "aaj ka dhandha"),
        tool_message("get_sales_summary", {"total_paise": 1_854_000, "txn_count": 63}),
    ]

    reply = (await LocalLLM().complete(messages, TOOLS)).text

    assert RUPEE_FIGURE.findall(reply) == ["₹18,540"]
    assert "63" in reply
    # Every number spoken traces back to the payload (SPEC.md §2.2).
    assert set(ANY_NUMBER.findall(reply)) == {"18,540", "63"}


async def test_local_llm_composition_reports_every_figure_it_was_given() -> None:
    messages = [
        ChatMessage("user", "aaj ka dhandha"),
        tool_message(
            "get_sales_summary",
            {
                "total_paise": 1_854_000,
                "txn_count": 63,
                "vs_baseline_pct": -12.4,
                "projected_paise": 4_205_000,
            },
        ),
    ]

    reply = (await LocalLLM().complete(messages, TOOLS)).text

    assert set(RUPEE_FIGURE.findall(reply)) == {"₹18,540", "₹42,050"}
    assert "12%" in reply


async def test_local_llm_composition_drops_clauses_whose_facts_are_missing() -> None:
    messages = [
        ChatMessage("user", "aaj ka dhandha"),
        tool_message("get_sales_summary", {"total_paise": 1_854_000}),
    ]
    reply = (await LocalLLM().complete(messages, TOOLS)).text

    assert RUPEE_FIGURE.findall(reply) == ["₹18,540"]
    assert "बिल" not in reply, "the bill-count clause must vanish without a count"
    assert "  " not in reply and " ।" not in reply


@pytest.mark.parametrize("language", ["hi-IN", "en-IN"])
async def test_local_llm_composition_is_short_and_ends_in_one_question(language: str) -> None:
    messages = [
        ChatMessage("user", "kaun nahi aa raha"),
        tool_message(
            "find_dormant_customers",
            {
                "count": 14,
                "winback_value_paise": 2_840_000,
                "customers": [{"name": "Ramesh"}, {"name": "Sunita"}, {"name": "Kapil"}],
            },
        ),
    ]

    reply = (await LocalLLM().complete(messages, TOOLS, language=language)).text

    assert len(reply.split()) <= MAX_SPOKEN_WORDS
    assert reply.count("?") == 1 and reply.endswith("?")
    assert reply.startswith("14")
    assert "₹28,400" in reply


async def test_local_llm_composition_varies_wording_but_never_the_numbers() -> None:
    """Deterministic variation seeded by turn index keeps repeat demos from sounding canned."""
    payload = {"total_paise": 1_854_000, "txn_count": 63}
    replies = set()
    for filler in range(3):
        messages = [ChatMessage("system", "x")] * filler
        messages += [ChatMessage("user", "aaj"), tool_message("get_sales_summary", payload)]
        reply = (await LocalLLM().complete(messages, TOOLS)).text
        assert RUPEE_FIGURE.findall(reply) == ["₹18,540"]
        replies.add(reply)
    assert len(replies) > 1


async def test_local_llm_falls_back_to_a_generic_template_for_an_unmapped_tool() -> None:
    messages = [
        ChatMessage("user", "note save karo"),
        tool_message("save_merchant_note", {"message": "note saved", "count": 1}),
    ]
    reply = (await LocalLLM().complete(messages, TOOLS)).text
    assert reply and reply.endswith("?")


def test_no_template_contains_a_literal_digit() -> None:
    """If a template hardcoded a number, a spoken figure could stop matching the database."""
    for name, template in TEMPLATES.items():
        for form in (*template.hi, *template.en, *template.ask_hi, *template.ask_en):
            assert not re.search(r"\d", form), f"{name}: {form!r}"


# ── LocalLLM: approval turns ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("utterance", "expect_yes"),
    [("haan bhej do", True), ("ji bilkul", True), ("nahi rehne do", False), ("abhi nahi", False)],
)
async def test_local_llm_answers_an_approval_turn(utterance: str, expect_yes: bool) -> None:
    messages = [
        ChatMessage("system", "pending_action: send_winback_offer for 14 customers"),
        ChatMessage("user", utterance),
    ]
    response = await LocalLLM().complete(messages, TOOLS)

    assert response.tool_calls == [], "the agent loop owns the state change, not the model"
    assert response.raw is not None
    assert response.raw["why"] == f"approval:send_winback_offer:{expect_yes}"
    assert response.text


async def test_local_llm_ignores_affirmation_without_a_pending_action() -> None:
    response = await LocalLLM().complete([ChatMessage("user", "haan")], TOOLS)
    assert response.raw is not None and response.raw["why"].startswith("clarify")


# ── LocalTTS ────────────────────────────────────────────────────────────────────────────────


async def test_local_tts_produces_a_valid_playable_wav() -> None:
    text = "आज अब तक अठारह हजार पांच सौ चालीस रुपये का कलेक्शन हुआ है"
    result = await LocalTTS().speak(text)

    assert result.audio[:4] == b"RIFF" and result.audio[8:12] == b"WAVE"
    with wave.open(io.BytesIO(result.audio), "rb") as handle:
        assert handle.getframerate() == 22_050
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        frames = handle.getnframes()

    assert len(result.audio) == WAV_HEADER_BYTES + frames * 2
    assert result.sample_rate == 22_050
    assert result.duration_ms == pytest.approx(frames / 22_050 * 1000, abs=2)
    # 12 words at ~85 wpm is roughly 8.5 s; allow a wide but real band.
    assert 5_000 < result.duration_ms < 12_000
    assert result.provider == "local"
    assert result.client_should_synthesise is True
    assert result.text == text


@pytest.mark.parametrize(
    ("text", "low", "high"),
    [("हाँ", 500, 1_500), ("शब्द " * 400, 19_000, 20_100)],
)
async def test_local_tts_clamps_duration(text: str, low: int, high: int) -> None:
    result = await LocalTTS().speak(text)
    assert low <= result.duration_ms <= high


async def test_local_tts_speaks_english_faster_than_hindi() -> None:
    text = "one two three four five six seven eight nine ten"
    hindi = await LocalTTS().speak(text, language="hi-IN")
    english = await LocalTTS().speak(text, language="en-IN")
    assert english.duration_ms < hindi.duration_ms


def test_build_wav_header_is_exactly_44_bytes() -> None:
    assert len(build_wav(b"")) == WAV_HEADER_BYTES


# ── LocalSTT ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "language"),
    [("आज कितना आया?", "hi-IN"), ("aaj ka dhandha kaisa raha", "en-IN")],
)
async def test_local_stt_round_trips_encode_text_wav(text: str, language: str) -> None:
    envelope = encode_text_wav(text)

    with wave.open(io.BytesIO(envelope), "rb") as handle:  # a real, parseable WAV
        assert handle.getframerate() == 16_000
        assert handle.getnframes() > 0

    result = await LocalSTT().transcribe(envelope)
    assert result.text == text
    assert result.language == language
    assert result.provider == "local"
    assert result.confidence == 1.0
    assert result.is_final is True


async def test_local_stt_accepts_raw_utf8_bytes() -> None:
    result = await LocalSTT().transcribe(b"kitna udhaar baki hai")
    assert result.text == "kitna udhaar baki hai"
    assert result.raw is not None and result.raw["source"] == "utf8_bytes"


async def test_local_stt_is_honest_about_an_empty_envelope() -> None:
    silence = build_wav(b"\x00" * 320, sample_rate=16_000)
    result = await LocalSTT().transcribe(silence, language="hi-IN")

    assert result.text == ""
    assert result.confidence == 0.0
    assert result.language == "hi-IN", "falls back to the requested language"


def test_decode_text_wav_returns_none_for_plain_audio() -> None:
    assert decode_text_wav(build_wav(b"\x00" * 100)) is None
    assert decode_text_wav(b"not a wav") is None


# ── Protocol conformance ────────────────────────────────────────────────────────────────────


def test_local_providers_satisfy_their_protocols() -> None:
    assert isinstance(LocalLLM(), LLMProvider)
    assert isinstance(LocalSTT(), STTProvider)
    assert isinstance(LocalTTS(), TTSProvider)


def test_live_providers_satisfy_their_protocols() -> None:
    handler = json_response({})
    assert isinstance(SarvamLLM(client=mock_client(handler)), LLMProvider)
    assert isinstance(SarvamSTT(client=mock_client(handler)), STTProvider)
    assert isinstance(SarvamTTS(client=mock_client(handler)), TTSProvider)


async def test_every_provider_reports_its_mode() -> None:
    local = [LocalLLM(), LocalSTT(), LocalTTS()]
    assert {provider.mode for provider in local} == {"local"}
    for provider in local:
        assert (await provider.health()).ok is True


# ── SarvamLLM ───────────────────────────────────────────────────────────────────────────────


async def test_sarvam_llm_parses_prose() -> None:
    payload = {
        "model": "sarvam-105b",
        "choices": [
            {
                "message": {"role": "assistant", "content": "आज ₹18,540 आया है।"},
                "finish_reason": "stop",
            }
        ],
    }
    llm = SarvamLLM(client=mock_client(json_response(payload)), model="sarvam-105b")

    response = await llm.complete([ChatMessage("user", "aaj ka dhandha")])

    assert response.provider == "live"
    assert response.model == "sarvam-105b"
    assert response.text == "आज ₹18,540 आया है।"
    assert response.tool_calls == []
    assert response.finish_reason == "stop"
    assert response.latency_ms >= 0


async def test_sarvam_llm_parses_tool_calls_with_object_arguments() -> None:
    payload = {
        "model": "sarvam-105b",
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "get_sales_summary",
                                "arguments": {"period": "today"},
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    llm = SarvamLLM(client=mock_client(json_response(payload)))

    response = await llm.complete([ChatMessage("user", "aaj")], TOOLS)

    assert response.wants_tools
    call = response.tool_calls[0]
    assert call.id == "call_abc"
    assert call.name == "get_sales_summary"
    assert call.arguments == {"period": "today"}


async def test_sarvam_llm_parses_tool_call_arguments_delivered_as_a_json_string() -> None:
    """The OpenAI wire format sends ``arguments`` as a string; some deployments send an object."""
    payload = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "send_winback_offer",
                                "arguments": '{"count": 14, "discount_pct": 20}',
                            },
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    llm = SarvamLLM(client=mock_client(json_response(payload)))

    response = await llm.complete([ChatMessage("user", "bhej do")], TOOLS)

    assert response.tool_calls[0].arguments == {"count": 14, "discount_pct": 20}
    assert response.finish_reason == "tool_calls"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"a": 1}, {"a": 1}),
        ('{"a": 1}', {"a": 1}),
        ('"{\\"a\\": 1}"', {"a": 1}),  # double-encoded
        ("", {}),
        ("not json", {}),
        (None, {}),
    ],
)
def test_parse_tool_arguments_is_tolerant(raw: Any, expected: dict[str, Any]) -> None:
    assert parse_tool_arguments(raw) == expected


def test_parse_chat_response_accepts_content_parts_and_a_flat_shape() -> None:
    parts = parse_chat_response(
        {"choices": [{"message": {"content": [{"type": "text", "text": "नमस्ते"}]}}]}
    )
    assert parts.text == "नमस्ते"

    flat = parse_chat_response({"text": "hello", "finish_reason": "stop"})
    assert flat.text == "hello"


async def test_sarvam_llm_sends_the_openai_shape_with_stringified_arguments() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        assert request.headers[AUTH_HEADER] == "test-key"
        assert request.url.path == SARVAM_CHAT_PATH
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    history = [
        ChatMessage("system", "MunshiJi"),
        ChatMessage("user", "aaj ka dhandha"),
        ChatMessage(
            "assistant",
            "",
            tool_calls=[ToolCall("call_1", "get_sales_summary", {"period": "today"})],
        ),
        ChatMessage(
            "tool",
            '{"total_paise": 1854000}',
            name="get_sales_summary",
            tool_call_id="call_1",
        ),
    ]
    await SarvamLLM(client=mock_client(handler), model="sarvam-105b").complete(history, TOOLS)

    body = captured[0]
    assert body["model"] == "sarvam-105b"
    assert body["tool_choice"] == "auto"
    assert [tool["function"]["name"] for tool in body["tools"]] == [t.name for t in TOOLS]
    assistant = body["messages"][2]
    arguments = assistant["tool_calls"][0]["function"]["arguments"]
    assert isinstance(arguments, str), "the API requires arguments as a JSON string"
    assert json.loads(arguments) == {"period": "today"}


# ── SarvamSTT ───────────────────────────────────────────────────────────────────────────────


async def test_sarvam_stt_parses_a_transcript() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/speech-to-text"
        assert b"multipart/form-data" in request.headers["content-type"].encode()
        return httpx.Response(
            200, json={"transcript": "आज कितना आया", "language_code": "hi-IN", "model": "saaras:v3"}
        )

    stt = SarvamSTT(client=mock_client(handler), model="saaras:v3")
    result = await stt.transcribe(build_wav(b"\x00" * 320, sample_rate=16_000))

    assert result.text == "आज कितना आया"
    assert result.language == "hi-IN"
    assert result.provider == "live"
    assert result.model == "saaras:v3"


async def test_sarvam_stt_tolerates_a_nested_response_shape() -> None:
    payload = {"data": {"results": [{"text": "kitna udhaar baki hai", "language": "en-IN"}]}}
    stt = SarvamSTT(client=mock_client(json_response(payload)))
    result = await stt.transcribe(b"audio")
    assert result.text == "kitna udhaar baki hai"


async def test_sarvam_stt_raises_when_no_transcript_is_present() -> None:
    stt = SarvamSTT(client=mock_client(json_response({"request_id": "abc"})))
    with pytest.raises(ProviderUnavailableError):
        await stt.transcribe(b"audio")


# ── SarvamTTS ───────────────────────────────────────────────────────────────────────────────


async def test_sarvam_tts_decodes_base64_audio() -> None:
    reference = build_wav(b"\x00" * 22_050 * 2, sample_rate=22_050)  # one second
    payload = {"audios": [base64.b64encode(reference).decode()], "model": "bulbul:v3"}

    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=payload)

    tts = SarvamTTS(client=mock_client(handler), model="bulbul:v3", speaker="anushka")
    result = await tts.speak("आज ₹18,540 आया", language="hi-IN")

    assert result.audio == reference
    assert result.sample_rate == 22_050
    assert result.duration_ms == pytest.approx(1000, abs=5)
    assert result.provider == "live"
    assert result.client_should_synthesise is False
    assert captured[0] == {
        "text": "आज ₹18,540 आया",
        "target_language_code": "hi-IN",
        "speaker": "anushka",
        "model": "bulbul:v3",
        "pace": 1.0,
    }


async def test_sarvam_tts_merges_multi_chunk_audio_into_one_playable_wav() -> None:
    chunk = build_wav(b"\x01\x00" * 11_025, sample_rate=22_050)  # half a second
    payload = {"audios": [base64.b64encode(chunk).decode()] * 2}
    tts = SarvamTTS(client=mock_client(json_response(payload)))

    result = await tts.speak("do chunk")

    with wave.open(io.BytesIO(result.audio), "rb") as handle:
        assert handle.getnframes() == 22_050  # both chunks under one header
    assert result.duration_ms == pytest.approx(1000, abs=5)


async def test_sarvam_tts_raises_when_no_audio_comes_back() -> None:
    tts = SarvamTTS(client=mock_client(json_response({"status": "queued"})))
    with pytest.raises(ProviderUnavailableError):
        await tts.speak("kuch bhi")


# ── Failure handling ────────────────────────────────────────────────────────────────────────


async def test_a_500_is_retried_once_then_surfaces_as_provider_unavailable() -> None:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, json={"error": "upstream exploded"})

    llm = SarvamLLM(client=mock_client(handler))
    with pytest.raises(ProviderUnavailableError) as excinfo:
        await llm.complete([ChatMessage("user", "aaj")])

    assert len(calls) == 2, "one retry, then give up"
    assert excinfo.value.status_code == 503


async def test_a_timeout_surfaces_as_provider_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(ProviderUnavailableError):
        await SarvamSTT(client=mock_client(handler)).transcribe(b"audio")

    with pytest.raises(ProviderUnavailableError):
        await SarvamTTS(client=mock_client(handler)).speak("kuch bhi")


async def test_a_client_error_is_not_retried() -> None:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(ProviderUnavailableError):
        await SarvamLLM(client=mock_client(handler)).complete([ChatMessage("user", "aaj")])
    assert len(calls) == 1


async def test_a_missing_api_key_fails_fast_without_a_request() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("no request should be attempted without a key")

    client = mock_client(handler, api_key="")
    with pytest.raises(ProviderUnavailableError):
        await SarvamLLM(client=client).complete([ChatMessage("user", "aaj")])


async def test_a_non_json_body_surfaces_as_provider_unavailable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway</html>")

    with pytest.raises(ProviderUnavailableError):
        await SarvamLLM(client=mock_client(handler)).complete([ChatMessage("user", "aaj")])


# ── Live health probes never raise ──────────────────────────────────────────────────────────


async def test_live_health_reports_ok_when_the_host_answers() -> None:
    handler = json_response({"models": []})
    for provider in (
        SarvamLLM(client=mock_client(handler)),
        SarvamSTT(client=mock_client(handler)),
        SarvamTTS(client=mock_client(handler)),
    ):
        health = await provider.health()
        assert health.ok is True
        assert health.mode == "live"


async def test_live_health_never_raises_on_failure() -> None:
    def exploding(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    for provider, kind in (
        (SarvamLLM(client=mock_client(exploding)), "llm"),
        (SarvamSTT(client=mock_client(exploding)), "stt"),
        (SarvamTTS(client=mock_client(exploding)), "tts"),
    ):
        health = await provider.health()
        assert health.ok is False
        assert health.kind == kind
        assert health.detail

    without_key = await SarvamLLM(client=mock_client(json_response({}), api_key="")).health()
    assert without_key.ok is False
    assert "SARVAM_API_KEY" in without_key.detail
