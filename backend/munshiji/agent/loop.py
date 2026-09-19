"""The agent loop — one merchant utterance in, one spoken reply (and maybe an action) out.

Shape of a turn:

1. Resolve the conversation and persist what the merchant said.
2. If an approval is pending, check whether this utterance answers it. A yes executes the action
   and reports the result; a no closes it gracefully. Neither path consults the model for
   permission — the gate lives in :mod:`munshiji.agent.approval`.
3. Recall relevant long-term memory and assemble today's live snapshot.
4. Ask the model, giving it the tool schemas. Execute the tools it asks for, feeding results back,
   up to a bounded number of iterations.
5. Persist the reply, write the turn back into memory, and publish events for the live dashboard.

Write tools never execute here. They can only *propose*, which surfaces as ``pending_action``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from munshiji.agent.approval import approve as approve_action
from munshiji.agent.approval import (
    execute_action,
    expire_stale,
    pending_for_conversation,
)
from munshiji.agent.approval import reject as reject_action
from munshiji.agent.prompts import build_system_prompt
from munshiji.agent.registry import ToolRegistry, get_registry
from munshiji.agent.tools.base import ToolContext
from munshiji.clock import now_ist
from munshiji.db.enums import ActionStatus, MemoryKind, TurnRole
from munshiji.db.models import ActionRequest, Conversation, Insight, Merchant, Turn
from munshiji.events import EventName, get_event_bus
from munshiji.logging import get_logger
from munshiji.providers.factory import ProviderBundle
from munshiji.providers.llm import ChatMessage, ToolCall
from munshiji.repositories.core import add_turn, get_or_create_conversation

__all__ = ["MAX_TOOL_ITERATIONS", "AgentLoop", "ToolCallRecord", "TurnResult"]

logger = get_logger(__name__)

#: How many times the model may call tools before it must answer. Keeps a confused model bounded.
MAX_TOOL_ITERATIONS = 4

#: How much conversation history to replay into the prompt.
HISTORY_TURNS = 8

#: Conversation transcripts echo MunshiJi's own replies back into the index and crowd out facts,
#: so prompt context is recalled from what *happened* rather than what was said.
CONTEXT_MEMORY_KINDS = (
    MemoryKind.ACTION,
    MemoryKind.DAY,
    MemoryKind.CUSTOMER,
    MemoryKind.INSIGHT,
    MemoryKind.PRODUCT,
    MemoryKind.NOTE,
)


def _ids_from(rows: object, key: str) -> list[str]:
    """Pull ``key`` out of a list of dicts, preserving order and dropping blanks."""
    if not isinstance(rows, list):
        return []
    found: list[str] = []
    for row in rows:
        if isinstance(row, dict) and row.get(key):
            found.append(str(row[key]))
    return found


def _restock_items(rows: object) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    if not isinstance(rows, list):
        return items
    for row in rows:
        if isinstance(row, dict) and row.get("sku") and row.get("suggested_order_qty"):
            items.append(
                {"sku": row["sku"], "qty": row["suggested_order_qty"], "name": row.get("name")}
            )
    return items


#: "Unhe offer bhej do" — *whom*? The referent is whatever the previous tool returned. A live model
#: resolves this from the tool results already in its context; this table makes the behaviour
#: identical no matter which provider is reasoning, and means a dropped list is never silently
#: turned into an empty send.
REFERENT_RESOLVERS: dict[tuple[str, str], tuple] = {
    ("send_winback_offer", "customer_ids"): (
        lambda data: list(data.get("customer_ids") or []),
        lambda data: _ids_from(data.get("customers"), "customer_id"),
    ),
    ("send_udhaar_reminder", "khata_ids"): (
        lambda data: list(data.get("khata_ids") or []),
        lambda data: _ids_from(data.get("top_debtors"), "khata_entry_id"),
    ),
    ("draft_restock_order", "items"): (lambda data: _restock_items(data.get("running_out")),),
}


@dataclass(slots=True)
class ToolCallRecord:
    """One tool invocation, for the UI trace and the audit log."""

    name: str
    arguments: dict[str, Any]
    ok: bool
    summary: str
    latency_ms: int = 0
    #: Parameters filled in from an earlier tool result rather than supplied by the model.
    resolved_referents: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TurnResult:
    """Everything one conversational turn produced."""

    conversation_id: str
    reply: str
    language: str = "hi-IN"
    intent: str = ""
    intent_confidence: float = 0.0
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    pending_action: ActionRequest | None = None
    executed_action: ActionRequest | None = None
    memory_used: list[str] = field(default_factory=list)
    latency_ms: int = 0
    provider: str = "local"


def _detect_affirmation(text: str) -> bool | None:
    """Yes / no / neither, in Hindi, Hinglish and English.

    Prefers the shared NLU parser; falls back to a compact lexicon so the loop keeps working even
    if that module is unavailable.
    """
    try:
        from munshiji.nlu import detect_affirmation

        return detect_affirmation(text)
    except Exception:  # pragma: no cover - defensive fallback
        lowered = text.strip().lower()
        yes = {
            "haan",
            "han",
            "haa",
            "ha",
            "yes",
            "y",
            "ok",
            "okay",
            "theek",
            "thik",
            "bhej do",
            "kar do",
            "bhejo",
            "karo",
            "हाँ",
            "हां",
            "ठीक",
            "भेज दो",
        }
        no = {"nahi", "nai", "na", "no", "n", "rehne do", "mat", "cancel", "नहीं", "मत", "रहने दो"}
        if any(token in lowered for token in no):
            return False
        if any(token in lowered for token in yes):
            return True
        return None


def _detect_intent(text: str, language: str) -> tuple[str, float]:
    try:
        from munshiji.nlu import detect_intent

        intent = detect_intent(text, language=language)
        return intent.name, intent.confidence
    except Exception:  # pragma: no cover - defensive fallback
        return "", 0.0


def _detect_language(text: str, default: str) -> str:
    try:
        from munshiji.nlu import detect_language

        return detect_language(text)
    except Exception:  # pragma: no cover - defensive fallback
        return default


class AgentLoop:
    """Runs conversational turns for one merchant at a time."""

    def __init__(
        self,
        providers: ProviderBundle,
        registry: ToolRegistry | None = None,
        *,
        publish_events: bool = True,
    ) -> None:
        self.providers = providers
        self.registry = registry or get_registry()
        self.publish_events = publish_events
        self.bus = get_event_bus()

    # ── public entry point ──────────────────────────────────────────────────

    async def run_turn(
        self,
        session: Session,
        merchant: Merchant,
        text: str,
        *,
        conversation_id: str | None = None,
        language: str | None = None,
        as_of: datetime | None = None,
    ) -> TurnResult:
        """Process one merchant utterance."""
        started = now_ist()
        as_of = as_of or started
        text = (text or "").strip()

        conversation = get_or_create_conversation(
            session,
            merchant.id,
            conversation_id,
            language=language or merchant.language,
        )
        # Register is the merchant's, not the keyboard's. A kirana owner typing romanised
        # Hinglish ("aaj dhandha kaisa raha") is writing Latin script but speaking Hindi, and
        # answering him in English because his phone lacks a Devanagari keyboard would be the
        # single most tone-deaf thing this product could do. Script detection exists for the
        # speech stack; it does not choose the language of the reply.
        speak_language = language or conversation.language or merchant.language
        conversation.language = speak_language
        script = _detect_language(text, speak_language)

        add_turn(session, conversation.id, TurnRole.MERCHANT.value, text)
        intent_name, intent_confidence = _detect_intent(text, script)

        # Proposals the merchant never answered must not fire hours later.
        expire_stale(session, merchant.id)

        pending = pending_for_conversation(session, conversation.id)
        if pending is not None:
            answered = await self._resolve_pending(
                session, merchant, conversation, pending, text, speak_language, as_of
            )
            if answered is not None:
                answered.intent = intent_name or answered.intent
                answered.intent_confidence = intent_confidence
                answered.latency_ms = self._elapsed_ms(started)
                return answered

        return await self._reason(
            session,
            merchant,
            conversation,
            text,
            speak_language,
            as_of,
            intent_name,
            intent_confidence,
            started,
        )

    # ── approval branch ─────────────────────────────────────────────────────

    async def _resolve_pending(
        self,
        session: Session,
        merchant: Merchant,
        conversation: Conversation,
        pending: ActionRequest,
        text: str,
        language: str,
        as_of: datetime,
    ) -> TurnResult | None:
        """Handle 'haan' / 'nahi' against a pending proposal.

        Returns ``None`` when the utterance was not an answer to it at all.
        """
        answer = _detect_affirmation(text)
        if answer is None:
            return None

        if answer is False:
            reject_action(session, pending.id, reason=text[:200])
            reply = self._say_rejected(pending, language)
            self._persist_reply(session, conversation, reply, [], self.providers.llm.mode)
            self._emit(
                merchant.id,
                EventName.ACTION_DECIDED,
                {"action_id": pending.id, "status": "rejected"},
            )
            return TurnResult(
                conversation_id=conversation.id,
                reply=reply,
                language=language,
                provider=self.providers.llm.mode,
            )

        approve_action(session, pending.id)
        self._emit(
            merchant.id, EventName.ACTION_DECIDED, {"action_id": pending.id, "status": "approved"}
        )

        from munshiji.agent.dispatch import build_dispatch

        ctx = self._context(session, merchant, conversation, as_of, language)
        dispatch = build_dispatch(ctx, pending)
        executed = await execute_action(session, pending, self.providers.actions, dispatch)

        reply = self._say_executed(executed, language)
        self._persist_reply(session, conversation, reply, [], executed.provider)
        self._emit(
            merchant.id,
            EventName.ACTION_EXECUTED,
            {
                "action_id": executed.id,
                "status": executed.status.value,
                "delivered": executed.result.get("delivered_count", 0),
            },
        )
        await self._remember(session, merchant.id)

        return TurnResult(
            conversation_id=conversation.id,
            reply=reply,
            language=language,
            executed_action=executed,
            provider=executed.provider,
        )

    # ── reasoning branch ────────────────────────────────────────────────────

    async def _reason(
        self,
        session: Session,
        merchant: Merchant,
        conversation: Conversation,
        text: str,
        language: str,
        as_of: datetime,
        intent_name: str,
        intent_confidence: float,
        started: datetime,
    ) -> TurnResult:
        # This search only enriches the system prompt — the recall tool makes its own, fully
        # budgeted call when memory IS the answer. Eight seconds buys the graph's phrasing when
        # the tenant is quick and switches to the local mirror (same facts) when it is not,
        # instead of spending the whole transport timeout before the first model call.
        memory_context = await self.providers.memory.search(
            merchant.id, text, limit=5, hops=1, kinds=CONTEXT_MEMORY_KINDS, budget_seconds=8.0
        )
        snapshot = self._snapshot(session, merchant, as_of)

        system = build_system_prompt(
            merchant,
            snapshot=snapshot,
            memory=memory_context.rendered,
            pending_action=None,
            language=language,
        )
        messages: list[ChatMessage] = [ChatMessage(role="system", content=system)]
        messages.extend(self._history(session, conversation.id))
        messages.append(ChatMessage(role="user", content=text))

        ctx = self._context(session, merchant, conversation, as_of, language)
        specs = self.registry.specs()
        records: list[ToolCallRecord] = []
        pending_action: ActionRequest | None = None
        reply = ""
        provider_mode = self.providers.llm.mode

        for _ in range(MAX_TOOL_ITERATIONS):
            response = await self.providers.llm.complete(messages, specs, language=language)
            provider_mode = response.provider

            if not response.wants_tools:
                reply = response.text.strip()
                break

            messages.append(
                ChatMessage(role="assistant", content=response.text, tool_calls=response.tool_calls)
            )
            for call in response.tool_calls:
                record, action = await self._run_tool(ctx, call, messages)
                records.append(record)
                if action is not None:
                    pending_action = action
        else:
            logger.warning(
                "tool iteration limit reached for conversation=%s; answering from what we have",
                conversation.id,
            )
            final = await self.providers.llm.complete(messages, None, language=language)
            reply = final.text.strip()
            provider_mode = final.provider

        if not reply:
            reply = self._fallback_reply(language)

        # Honesty guard: if every tool this turn failed, the composer must not be allowed to
        # narrate a success over the top of it. Reporting the failure is always better than a
        # confident sentence built on nothing.
        if records and not any(record.ok for record in records):
            reply = self._say_tools_failed(records, language)

        self._persist_reply(session, conversation, reply, records, provider_mode)

        if pending_action is not None:
            self._emit(
                merchant.id,
                EventName.ACTION_PROPOSED,
                {
                    "action_id": pending_action.id,
                    "tool": pending_action.tool_name,
                    "targets": pending_action.target_count,
                },
            )

        await self._remember(session, merchant.id)

        return TurnResult(
            conversation_id=conversation.id,
            reply=reply,
            language=language,
            intent=intent_name,
            intent_confidence=intent_confidence,
            tool_calls=records,
            pending_action=pending_action,
            memory_used=[hit.ref for hit in memory_context.hits],
            latency_ms=self._elapsed_ms(started),
            provider=provider_mode,
        )

    # ── helpers ─────────────────────────────────────────────────────────────

    async def _run_tool(
        self, ctx: ToolContext, call: ToolCall, messages: list[ChatMessage]
    ) -> tuple[ToolCallRecord, ActionRequest | None]:
        """Execute one tool call and append its result to the conversation sent to the model."""
        began = now_ist()
        arguments, resolved = self._resolve_referents(
            ctx, call.name, dict(call.arguments or {}), messages
        )
        result = await self.registry.execute(call.name, arguments, ctx)
        latency = self._elapsed_ms(began)

        # Keep the payload on failure too: a refusal carries the evidence for it (who was
        # dropped and under which rule), and the screen needs to show that, not just an error.
        payload = dict(result.data or {})
        if not result.ok:
            payload["error"] = result.error
        messages.append(
            ChatMessage(
                role="tool",
                name=call.name,
                tool_call_id=call.id,
                content=json.dumps(payload, ensure_ascii=False, default=str),
            )
        )
        record = ToolCallRecord(
            name=call.name,
            arguments=arguments,
            ok=result.ok,
            summary=result.summary_hi or result.summary_en or ("ok" if result.ok else result.error),
            latency_ms=latency,
            resolved_referents=resolved,
        )
        return record, result.action

    @staticmethod
    def _resolve_referents(
        ctx: ToolContext,
        tool_name: str,
        arguments: dict[str, Any],
        messages: list[ChatMessage],
    ) -> tuple[dict[str, Any], list[str]]:
        """Fill empty parameters from what MunshiJi has already established.

        This is anaphora resolution, not a workaround: "unhe offer bhej do" names its referent
        only by pointing at something said earlier. Two sources, in order:

        1. Tool results from *this* turn, still in the message list.
        2. The open insight that suggested this very tool — the durable record of the list the
           merchant was told about a turn or two ago, which also links the action back to the
           finding it answers.

        Resolving here rather than in a provider keeps the behaviour identical whether Sarvam or
        the local model is reasoning, and guarantees a dropped list never becomes a silent no-op.
        """
        resolved: list[str] = []
        candidates = [
            (param, extractors)
            for (name, param), extractors in REFERENT_RESOLVERS.items()
            if name == tool_name and not arguments.get(param)
        ]

        def open_insight_for(tool: str) -> Insight | None:
            return ctx.session.scalars(
                select(Insight)
                .where(
                    Insight.merchant_id == ctx.merchant_id,
                    Insight.status == "open",
                    Insight.suggested_tool == tool,
                )
                .order_by(Insight.score.desc())
                .limit(1)
            ).first()

        insight = open_insight_for(tool_name)
        if insight is None and candidates:
            # The merchant can open with "unhe offer bhej do" without having asked who first.
            # Rather than refusing, work out who: the engines are the same ones that would have
            # answered the question he skipped.
            try:
                from munshiji.agent.tools.insights import ensure_insights

                ensure_insights(ctx)
                insight = open_insight_for(tool_name)
            except Exception as exc:  # pragma: no cover - a failed refresh is not fatal
                logger.warning("could not derive a referent for %s: %s", tool_name, exc)
        if insight is not None:
            for key, value in (insight.suggested_params or {}).items():
                if value and not arguments.get(key):
                    arguments[key] = value
                    resolved.append(f"{key}<-insight")
            ctx.insight_id = ctx.insight_id or insight.id

        candidates = [
            (param, extractors) for param, extractors in candidates if not arguments.get(param)
        ]
        if not candidates:
            if resolved:
                logger.info("resolved referents for %s: %s", tool_name, ", ".join(resolved))
            return arguments, resolved

        for message in reversed(messages):
            if message.role != "tool" or not message.content:
                continue
            try:
                data = json.loads(message.content)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            for param, extractors in candidates:
                if arguments.get(param):
                    continue
                for extract in extractors:
                    try:
                        value = extract(data)
                    except Exception:  # pragma: no cover - a malformed result is not fatal
                        continue
                    if value:
                        arguments[param] = value
                        resolved.append(f"{param}<-{message.name or 'tool'}")
                        break
            if all(arguments.get(param) for param, _ in candidates):
                break

        if resolved:
            logger.info("resolved referents for %s: %s", tool_name, ", ".join(resolved))
        return arguments, resolved

    def _context(
        self,
        session: Session,
        merchant: Merchant,
        conversation: Conversation,
        as_of: datetime,
        language: str,
    ) -> ToolContext:
        return ToolContext(
            session=session,
            merchant=merchant,
            providers=self.providers,
            as_of=as_of,
            language=language,
            conversation_id=conversation.id,
        )

    def _snapshot(self, session: Session, merchant: Merchant, as_of: datetime) -> dict[str, Any]:
        """Today's live numbers for the prompt.

        Never fatal: an empty snapshot just means the model has fewer facts to work from.
        """
        try:
            from munshiji.agent.snapshot import build_prompt_snapshot

            return build_prompt_snapshot(session, merchant, as_of)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("snapshot unavailable: %s", exc)
            return {}

    def _history(self, session: Session, conversation_id: str) -> list[ChatMessage]:
        """Replay recent turns so the model has conversational context."""
        turns = session.scalars(
            select(Turn)
            .where(Turn.conversation_id == conversation_id)
            .order_by(Turn.seq.desc())
            .limit(HISTORY_TURNS + 1)
        ).all()
        # Drop the utterance we just stored; it is appended explicitly by the caller.
        ordered = list(reversed(turns))[:-1]
        messages: list[ChatMessage] = []
        for turn in ordered:
            if turn.role is TurnRole.MERCHANT:
                messages.append(ChatMessage(role="user", content=turn.text))
            elif turn.role is TurnRole.MUNSHI:
                messages.append(ChatMessage(role="assistant", content=turn.text))
        return messages

    def _persist_reply(
        self,
        session: Session,
        conversation: Conversation,
        reply: str,
        records: list[ToolCallRecord],
        provider: str,
    ) -> Turn:
        turn = add_turn(
            session,
            conversation.id,
            TurnRole.MUNSHI.value,
            reply,
            tool_calls=[
                {
                    "name": record.name,
                    "arguments": record.arguments,
                    "ok": record.ok,
                    "summary": record.summary,
                    "latency_ms": record.latency_ms,
                }
                for record in records
            ],
            provider=provider,
        )
        self._emit(
            conversation.merchant_id,
            EventName.TURN,
            {"conversation_id": conversation.id, "reply": reply, "seq": turn.seq},
        )
        return turn

    async def _remember(self, session: Session, merchant_id: str) -> None:
        """Fold the latest state — including any action just executed — back into memory.

        The turn is committed first: the memory provider owns its own session, so leaving this
        transaction open would have the process contend with itself for the write lock.
        """
        try:
            from munshiji.memory.ingest import ingest_all

            session.commit()
            # Sessions are built with expire_on_commit=False, so rows written through a
            # provider's own session (outcomes, delivery counts) would otherwise be invisible to
            # the objects already cached here — and the memory would record a stale version of
            # what just happened.
            session.expire_all()
            facts = ingest_all(session, merchant_id)
            written = await self.providers.memory.ingest(merchant_id, facts)
            self._emit(merchant_id, EventName.MEMORY_UPDATED, {"nodes": written})
        except Exception as exc:  # pragma: no cover - memory must never break a conversation
            logger.warning("memory ingest skipped: %s", exc)

    def _emit(self, merchant_id: str, name: str, data: dict[str, Any]) -> None:
        if self.publish_events:
            self.bus.publish(merchant_id, name, data)

    @staticmethod
    def _elapsed_ms(since: datetime) -> int:
        return max(0, int((now_ist() - since).total_seconds() * 1000))

    @staticmethod
    def _say_rejected(action: ActionRequest, language: str) -> str:
        if language.startswith("hi"):
            return "ठीक है, रहने देते हैं। कुछ और बताऊँ?"
        return "Sure, we'll leave it. Anything else you'd like to look at?"

    @staticmethod
    def _say_executed(action: ActionRequest, language: str) -> str:
        """What the merchant hears once an approved action has been attempted.

        The status is load-bearing. A dispatch that raised leaves ``result`` with no delivery
        counts at all, and ``target_count`` as their fallback would quietly turn "nothing went
        out" into "sent to everyone" — the one lie this product cannot afford, because the
        merchant would then sit waiting on customers who were never contacted.
        """
        if action.status is not ActionStatus.EXECUTED:
            return AgentLoop._say_action_failed(action, language)

        delivered = int(action.result.get("delivered_count", action.target_count) or 0)
        failed = int(action.result.get("failed_count", 0) or 0)
        if language.startswith("hi"):
            text = f"भेज दिया — {delivered} ग्राहकों को।"
            if failed:
                text += f" {failed} नंबर नहीं लगे।"
            return text + " कौन लौटा, बताता रहूंगा।"
        text = f"Done — sent to {delivered} customers."
        if failed:
            text += f" {failed} numbers didn't go through."
        return text + " I'll keep you posted on who comes back."

    @staticmethod
    def _say_action_failed(action: ActionRequest, language: str) -> str:
        """An approved action that reached nobody. Say that plainly, and offer to retry.

        Nothing is half-sent here: the dispatch sits between two short transactions, so a
        failure means the whole batch stayed home. The merchant is told the count that is still
        waiting, so the number they hear is the number they can act on.
        """
        waiting = int(action.target_count or 0)
        if language.startswith("hi"):
            text = "भेज नहीं पाया — मैसेज वाली लाइन अभी बंद है। किसी के पास कुछ नहीं गया।"
            if waiting:
                text += f" {waiting} ग्राहक वैसे के वैसे हैं।"
            return text + " लाइन ठीक होते ही दोबारा पूछूँगा।"
        text = "Couldn't send it — the messaging line is down. Nothing went out to anyone."
        if waiting:
            text += f" All {waiting} customers are still waiting."
        return text + " I'll ask you again once it's back."

    @staticmethod
    def _say_tools_failed(records: list[ToolCallRecord], language: str) -> str:
        """Tell the merchant plainly that we could not get the number, rather than inventing one.

        When the tool refused on purpose — quiet hours, no consent, a cooldown, the daily cap —
        its own summary is already the right sentence in the right language, and it is the whole
        point that the merchant hears *why*. A refusal nobody can hear is not a guardrail. Only
        when there is nothing to quote do we fall back to the generic apology.
        """
        detail = records[0].summary.strip() if records else ""
        if detail:
            return detail
        if language.startswith("hi"):
            return "ये जानकारी अभी निकाल नहीं पाया। एक बार फिर पूछिए, या कुछ और पूछिए।"
        return "I couldn't pull that up just now. Ask me again, or ask something else."

    @staticmethod
    def _fallback_reply(language: str) -> str:
        if language.startswith("hi"):
            return "मैं ठीक से समझ़ नहीं पाया। क्या आप दोबारा बताएंगे?"
        return "I didn't quite catch that. Could you say it again?"
