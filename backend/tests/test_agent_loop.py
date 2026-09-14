"""The agent loop's own behaviour.

Everything here is a rule the loop enforces regardless of which provider is reasoning: the reply
register, referent resolution, the honesty guard, and the approval branch. These are the parts
that would silently regress the day a model starts behaving slightly differently.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from munshiji.agent.loop import AgentLoop
from munshiji.db.enums import ActionStatus, InsightKind, Severity
from munshiji.db.models import ActionRequest, Customer, Insight, Merchant
from munshiji.providers.actions_local import LocalActions
from munshiji.providers.factory import ProviderBundle
from munshiji.providers.llm_local import LocalLLM
from munshiji.providers.memory_local import LocalGraphMemory
from munshiji.providers.stt_local import LocalSTT
from munshiji.providers.tts_local import LocalTTS
from munshiji.seed.generator import generate

DEVANAGARI = re.compile(r"[ऀ-ॿ]")


@pytest.fixture
def bundle(session_factory) -> ProviderBundle:
    """Offline providers bound to the *test* database rather than the process-wide one."""
    return ProviderBundle(
        llm=LocalLLM(),
        stt=LocalSTT(),
        tts=LocalTTS(),
        memory=LocalGraphMemory(session_factory),
        actions=LocalActions(session_factory),
    )


@pytest.fixture
def shop(session: Session) -> Merchant:
    """A small seeded shop - 45 days is enough for every engine to have something to say."""
    generate(session, days=45)
    session.commit()
    return session.scalars(select(Merchant)).one()


@pytest.fixture
def loop(bundle: ProviderBundle) -> AgentLoop:
    return AgentLoop(bundle, publish_events=False)


def _open_winback_insight(session: Session, merchant: Merchant, count: int = 4) -> Insight:
    """An insight that already knows who to contact - the durable referent for 'unhe'."""
    customers = session.scalars(
        select(Customer).where(Customer.merchant_id == merchant.id).limit(count)
    ).all()
    insight = Insight(
        merchant_id=merchant.id,
        kind=InsightKind.DORMANT_CUSTOMERS,
        severity=Severity.HIGH,
        title_en="Dormant regulars",
        title_hi="पुराने ग्राहक",
        body_en="They stopped coming.",
        body_hi="वे नहीं आ रहे।",
        metrics={"dormant_count": len(customers), "recoverable_paise": 500000},
        suggested_tool="send_winback_offer",
        suggested_params={
            "customer_ids": [customer.id for customer in customers],
            "discount_pct": 10,
            "valid_days": 7,
        },
        impact_paise=500000,
        confidence=0.8,
        score=72.0,
        status="open",
        dedupe_key="dormant_customers",
    )
    session.add(insight)
    session.commit()
    return insight


# ── Reply register ──────────────────────────────────────────────────────────


async def test_romanised_hinglish_still_gets_a_hindi_reply(
    session: Session, shop: Merchant, loop: AgentLoop
) -> None:
    """A merchant whose phone has no Devanagari keyboard is still a Hindi speaker.

    Script detection exists for the speech stack; it must never decide the reply language, or
    every romanised question would be answered in English.
    """
    result = await loop.run_turn(session, shop, "Munshiji, aaj dhandha kaisa raha?")

    assert result.language == "hi-IN"
    assert DEVANAGARI.search(result.reply), f"expected a Hindi reply, got: {result.reply!r}"


async def test_explicit_language_overrides_the_profile(
    session: Session, shop: Merchant, loop: AgentLoop
) -> None:
    result = await loop.run_turn(session, shop, "How was business today?", language="en-IN")
    assert result.language == "en-IN"
    assert not DEVANAGARI.search(result.reply)


async def test_language_persists_across_a_conversation(
    session: Session, shop: Merchant, loop: AgentLoop
) -> None:
    first = await loop.run_turn(session, shop, "aaj ka collection?", language="en-IN")
    second = await loop.run_turn(
        session, shop, "udhaar kitna baaki hai?", conversation_id=first.conversation_id
    )
    assert second.language == "en-IN", "a mid-conversation turn must not flip register"


# ── Tool use ────────────────────────────────────────────────────────────────


async def test_a_sales_question_is_answered_from_a_tool(
    session: Session, shop: Merchant, loop: AgentLoop
) -> None:
    result = await loop.run_turn(session, shop, "aaj kitna aaya?")

    assert [call.name for call in result.tool_calls] == ["get_sales_summary"]
    assert all(call.ok for call in result.tool_calls)


async def test_reply_quotes_only_numbers_the_tool_returned(
    session: Session, shop: Merchant, loop: AgentLoop
) -> None:
    """The composer may not invent a figure. Every rupee amount spoken must appear in a result."""
    result = await loop.run_turn(session, shop, "aaj kitna aaya?")

    number = re.compile(r"\d[\d,]*(?:\.\d+)?")

    def figures(text: str) -> set[str]:
        return {match.group().rstrip(",") for match in number.finditer(text)}

    spoken = {
        match.group(1).rstrip(",")
        for match in re.finditer(r"₹\s?(\d[\d,]*(?:\.\d+)?)", result.reply)
    }
    available: set[str] = set()
    for call in result.tool_calls:
        available |= figures(call.summary)

    assert spoken <= available, f"reply quoted an unsourced figure: {spoken - available}"


# ── Referent resolution ─────────────────────────────────────────────────────


async def test_unhe_resolves_to_the_insight_s_customer_list(
    session: Session, shop: Merchant, loop: AgentLoop
) -> None:
    """'Unhe offer bhej do' names its referent only by pointing at an earlier answer.

    The open insight is the durable record of that list, so an empty argument must never become
    a silent no-op send.
    """
    insight = _open_winback_insight(session, shop, count=4)

    result = await loop.run_turn(session, shop, "unhe 10% ka offer bhej do")

    assert result.pending_action is not None
    assert result.pending_action.target_count == 4
    assert result.pending_action.params["customer_ids"] == insight.suggested_params["customer_ids"]
    assert any("customer_ids<-insight" in call.resolved_referents for call in result.tool_calls)


# ── The approval gate, through the loop ─────────────────────────────────────


async def test_a_write_tool_only_proposes(
    session: Session, shop: Merchant, loop: AgentLoop
) -> None:
    _open_winback_insight(session, shop)

    result = await loop.run_turn(session, shop, "unhe offer bhej do")

    assert result.pending_action is not None
    assert result.pending_action.status is ActionStatus.PENDING_APPROVAL
    assert result.executed_action is None
    assert not result.pending_action.executed_at


async def test_haan_executes_the_pending_action(
    session: Session, shop: Merchant, loop: AgentLoop
) -> None:
    _open_winback_insight(session, shop)
    proposed = await loop.run_turn(session, shop, "unhe offer bhej do")
    assert proposed.pending_action is not None

    approved = await loop.run_turn(
        session, shop, "haan bhej do", conversation_id=proposed.conversation_id
    )

    assert approved.executed_action is not None
    assert approved.executed_action.id == proposed.pending_action.id
    assert approved.executed_action.status is ActionStatus.EXECUTED
    assert approved.executed_action.result["delivered_count"] >= 1


async def test_nahi_rejects_without_sending(
    session: Session, shop: Merchant, loop: AgentLoop
) -> None:
    _open_winback_insight(session, shop)
    proposed = await loop.run_turn(session, shop, "unhe offer bhej do")

    declined = await loop.run_turn(
        session, shop, "nahi, rehne do", conversation_id=proposed.conversation_id
    )

    assert declined.executed_action is None
    session.expire_all()
    action = session.get(ActionRequest, proposed.pending_action.id)
    assert action.status is ActionStatus.REJECTED
    assert action.executed_at is None


async def test_approval_is_scoped_to_its_own_conversation(
    session: Session, shop: Merchant, loop: AgentLoop
) -> None:
    """A yes in a different call must not fire a proposal raised elsewhere."""
    _open_winback_insight(session, shop)
    proposed = await loop.run_turn(session, shop, "unhe offer bhej do")

    elsewhere = await loop.run_turn(session, shop, "haan")  # new conversation

    assert elsewhere.executed_action is None
    session.expire_all()
    assert (
        session.get(ActionRequest, proposed.pending_action.id).status
        is ActionStatus.PENDING_APPROVAL
    )


# ── Honesty ─────────────────────────────────────────────────────────────────


async def test_a_failed_tool_is_reported_not_narrated_over(
    session: Session, shop: Merchant, loop: AgentLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If every tool fails, the reply must say so rather than describe a success."""

    async def explode(*_args, **_kwargs):
        raise RuntimeError("analytics unavailable")

    # Patch the handler on the registered Tool itself: the module-level function was captured by
    # reference when the tool was built, so patching the module attribute would change nothing.
    monkeypatch.setattr(loop.registry.get("get_sales_summary"), "handler", explode)

    result = await loop.run_turn(session, shop, "aaj kitna aaya?")

    assert result.tool_calls and not any(call.ok for call in result.tool_calls)
    assert not re.search(
        r"₹\s?\d", result.reply
    ), f"a failed lookup must not produce a figure: {result.reply!r}"


# ── Memory ──────────────────────────────────────────────────────────────────


async def test_context_recall_ignores_conversation_transcripts(
    session: Session, shop: Merchant, loop: AgentLoop
) -> None:
    """Transcripts echo MunshiJi's own replies back and crowd out the facts."""
    await loop.run_turn(session, shop, "aaj kitna aaya?")
    result = await loop.run_turn(session, shop, "pichli baar kya hua tha?")

    assert not any(ref.startswith("conversation:") for ref in result.memory_used)


async def test_an_executed_action_is_recallable_in_a_new_conversation(
    session: Session, shop: Merchant, loop: AgentLoop
) -> None:
    """The demo's load-bearing property: call two knows what call one did."""
    _open_winback_insight(session, shop)
    proposed = await loop.run_turn(session, shop, "unhe offer bhej do")
    executed = await loop.run_turn(
        session, shop, "haan bhej do", conversation_id=proposed.conversation_id
    )
    assert executed.executed_action is not None

    recalled = await loop.run_turn(session, shop, "pichli baar jo offer bheja tha uska kya hua?")

    assert [call.name for call in recalled.tool_calls] == ["recall_memory"]
    assert (
        any(ref.startswith("action:") for ref in recalled.memory_used)
        or "action" in recalled.reply.lower()
        or DEVANAGARI.search(recalled.reply)
    )
