"""``LocalLLM`` — MunshiJi's offline reasoning core.

This is not a stub. It does three real jobs, in the same shape the live model does (SPEC.md §2.2):

1. **Tool selection.** :func:`~munshiji.nlu.detect_intent` classifies the merchant's utterance,
   the intent maps onto one of the tools *actually advertised in this turn*, and slots plus the
   tool's own JSON schema fill the parameters.
2. **Composition.** Given ``role="tool"`` results, it composes a spoken Hinglish reply from the
   template table below — using **only numbers present in the tool result**. Templates contain no
   literal digits, so "did it invent a figure?" is a property you can test rather than trust.
3. **Approval language.** On a yes/no turn with a pending action in context, it produces the
   confirmation or the stand-down. The agent loop owns the state change; this owns the words.

Style rules baked into the templates (SPEC.md §9): lead with the number, one sentence of context,
then exactly one proposed next action phrased as a question. Under ~45 words — it is spoken aloud.
Variant selection is seeded by the turn index, so a demo does not repeat itself word for word,
but the numbers never vary.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from string import Formatter
from typing import Any

from munshiji.money import fmt_inr, paise_from_rupees
from munshiji.nlu import detect_affirmation, detect_intent
from munshiji.providers.base import ProviderHealth, ProviderMode
from munshiji.providers.llm import ChatMessage, LLMResponse, ToolCall, ToolSpec

__all__ = ["MAX_SPOKEN_WORDS", "TEMPLATES", "TOOL_FOR_INTENT", "LocalLLM", "ReplyTemplate"]

#: A spoken reply longer than this stops being a reply and starts being a lecture.
MAX_SPOKEN_WORDS = 45

# ── Intent → tool ───────────────────────────────────────────────────────────────────────────
#: Candidates in preference order. Only a tool actually present in ``tools`` is ever emitted, so
#: an agent that advertises a subset still gets a sensible (or no) call.
TOOL_FOR_INTENT: dict[str, tuple[str, ...]] = {
    "sales_summary": ("get_sales_summary", "get_insights"),
    "compare_sales": ("compare_sales", "get_sales_summary"),
    "top_customers": ("get_top_customers",),
    "dormant_customers": ("find_dormant_customers", "get_top_customers"),
    "inventory_alerts": ("get_inventory_alerts", "get_product_performance"),
    "udhaar_summary": ("get_udhaar_summary",),
    "merchant_health": ("get_merchant_health", "get_insights"),
    "insights": ("get_insights",),
    "recall_memory": ("recall_memory", "get_insights"),
    "send_offer": ("send_winback_offer", "find_dormant_customers"),
    "send_reminder": ("send_udhaar_reminder", "get_udhaar_summary"),
    "restock": ("draft_restock_order", "get_inventory_alerts"),
}

#: Slot name → parameter names it may satisfy, in order. Tool schemas are written by another
#: module, so the mapping is deliberately generous about synonyms.
SLOT_ALIASES: dict[str, tuple[str, ...]] = {
    "period": ("period", "range", "window", "timeframe", "day", "date_range"),
    "period_a": ("period_a", "current_period", "period"),
    "period_b": ("period_b", "baseline_period", "compare_to", "previous_period"),
    "discount_pct": ("discount_pct", "discount_percent", "percent_off"),
    "discount_amount_rupees": ("discount_amount_rupees", "amount_rupees", "discount_rupees"),
    "count": ("count", "limit", "top_n", "target_count", "max_targets", "n"),
    "tone": ("tone",),
    "language": ("language", "lang"),
}
_QUERY_PARAMS = ("query", "q", "text", "question", "search")
#: Used when a required integer parameter has no schema default and no slot filled it.
_COUNTISH = ("limit", "count", "top_n", "n", "max")
_DEFAULT_COUNT = 5
_DEFAULT_DAYS = 7

# ── Fact extraction ─────────────────────────────────────────────────────────────────────────

_MONEY, _RUPEES, _INT, _PCT, _TEXT, _NAMES = "money", "rupees", "int", "pct", "text", "names"


@dataclass(frozen=True, slots=True)
class _FactSpec:
    """Where one speakable value lives in a tool result, and how to render it."""

    name: str
    kind: str
    keys: tuple[str, ...]


#: Money that is *already* in paise, per SPEC.md §2.3 — never a float, never a bare rupee int.
FACT_SPECS: dict[str, tuple[_FactSpec, ...]] = {
    "get_sales_summary": (
        _FactSpec(
            "amount",
            _MONEY,
            (
                "total_paise",
                "collection_paise",
                "sales_paise",
                "amount_paise",
                "gross_paise",
                "today_paise",
            ),
        ),
        _FactSpec(
            "count",
            _INT,
            ("txn_count", "transaction_count", "sales_count", "bill_count", "orders", "count"),
        ),
        _FactSpec("delta", _PCT, ("vs_baseline_pct", "delta_pct", "change_pct", "pct_change")),
        _FactSpec("projected", _MONEY, ("projected_paise", "projection_paise", "forecast_paise")),
    ),
    "compare_sales": (
        _FactSpec("amount", _MONEY, ("current_paise", "period_a_paise", "total_paise")),
        _FactSpec(
            "baseline",
            _MONEY,
            ("baseline_paise", "previous_paise", "period_b_paise", "comparison_paise"),
        ),
        _FactSpec("delta", _PCT, ("delta_pct", "change_pct", "pct_change", "vs_baseline_pct")),
    ),
    "get_top_customers": (
        _FactSpec("names", _NAMES, ("customers", "top_customers", "top", "rows", "items")),
        _FactSpec("amount", _MONEY, ("total_paise", "spend_paise", "total_spend_paise")),
        _FactSpec("count", _INT, ("count", "customer_count", "total")),
    ),
    "find_dormant_customers": (
        _FactSpec("count", _INT, ("count", "customer_count", "dormant_count", "total")),
        _FactSpec(
            "amount",
            _MONEY,
            (
                "winback_value_paise",
                "value_paise",
                "at_risk_paise",
                "potential_paise",
                "impact_paise",
            ),
        ),
        _FactSpec("names", _NAMES, ("customers", "dormant", "rows", "items")),
    ),
    "get_inventory_alerts": (
        _FactSpec("count", _INT, ("count", "alert_count", "item_count", "total")),
        _FactSpec("names", _NAMES, ("products", "items", "alerts", "rows")),
        _FactSpec("amount", _MONEY, ("capital_locked_paise", "value_paise", "impact_paise")),
    ),
    "get_udhaar_summary": (
        _FactSpec(
            "amount", _MONEY, ("outstanding_paise", "udhaar_paise", "balance_paise", "total_paise")
        ),
        _FactSpec("count", _INT, ("count", "open_count", "entry_count", "customer_count")),
        _FactSpec("oldest_days", _INT, ("oldest_days", "max_age_days", "oldest_age_days")),
    ),
    "get_insights": (
        _FactSpec("headline", _TEXT, ("title_hi", "title_en", "title", "headline", "summary")),
        _FactSpec("amount", _MONEY, ("impact_paise", "value_paise", "total_paise")),
        _FactSpec("count", _INT, ("count", "insight_count", "total")),
    ),
    "get_product_performance": (
        _FactSpec("names", _NAMES, ("products", "items", "top", "rows")),
        _FactSpec("amount", _MONEY, ("revenue_paise", "total_paise", "margin_paise")),
        _FactSpec("count", _INT, ("count", "product_count", "total")),
    ),
    "get_merchant_health": (
        _FactSpec("count", _INT, ("score",)),
        _FactSpec("tone", _TEXT, ("band_hi", "band")),
        _FactSpec("headline", _TEXT, ("headline",)),
    ),
    "recall_memory": (
        _FactSpec("headline", _TEXT, ("summary", "rendered", "label", "text", "message")),
        _FactSpec("amount", _MONEY, ("recovered_paise", "value_paise", "impact_paise")),
        _FactSpec("when", _TEXT, ("when", "occurred_at", "date", "day")),
    ),
    "send_winback_offer": (
        _FactSpec("count", _INT, ("target_count", "count", "delivered_count", "customer_count")),
        _FactSpec("discount_pct", _PCT, ("discount_pct", "discount_percent")),
        _FactSpec("discount", _RUPEES, ("discount_amount_rupees", "amount_rupees")),
        _FactSpec("amount", _MONEY, ("estimated_impact_paise", "impact_paise", "value_paise")),
        # A shopkeeper decides on two hard numbers - what it costs, and how many have to come
        # back before that is covered - not on a modelled expected value.
        _FactSpec("cost", _MONEY, ("send_cost_paise",)),
        _FactSpec("breakeven", _INT, ("breakeven_responses",)),
        # Only set when somebody was actually dropped, so the clause disappears on a clean list.
        _FactSpec("dropped", _INT, ("skipped_no_consent",)),
    ),
    "send_udhaar_reminder": (
        _FactSpec("count", _INT, ("target_count", "count", "delivered_count", "entry_count")),
        _FactSpec("amount", _MONEY, ("outstanding_paise", "total_paise", "amount_paise")),
        _FactSpec("tone", _TEXT, ("tone",)),
        _FactSpec("cost", _MONEY, ("send_cost_paise",)),
        _FactSpec("breakeven", _INT, ("breakeven_responses",)),
    ),
    "draft_restock_order": (
        _FactSpec("count", _INT, ("item_count", "count", "line_count", "target_count")),
        _FactSpec("amount", _MONEY, ("order_value_paise", "total_paise", "cost_paise")),
        _FactSpec("names", _NAMES, ("products", "items", "lines", "rows")),
    ),
}

#: Tried for every tool, after its own specs, so an unmapped tool still speaks real numbers.
COMMON_FACT_SPECS: tuple[_FactSpec, ...] = (
    _FactSpec(
        "amount",
        _MONEY,
        ("amount_paise", "total_paise", "value_paise", "impact_paise", "estimated_impact_paise"),
    ),
    _FactSpec("count", _INT, ("count", "target_count", "delivered_count", "total")),
    _FactSpec("headline", _TEXT, ("message", "summary", "title_hi", "title", "status")),
)


# ── Template table ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReplyTemplate:
    """Spoken-reply blueprint for one tool, in both registers.

    Rules enforced by :meth:`LocalLLM._compose`:

    * **No literal digits anywhere.** Every figure is substituted from the tool result.
    * A placeholder ending in ``_clause`` is optional: it renders to ``""`` when its facts are
      missing. Any other placeholder is required, and a variant needing a missing fact is skipped.
    * ``ask_*`` is the single proposed next action, always a question.
    """

    hi: tuple[str, ...]
    en: tuple[str, ...]
    ask_hi: tuple[str, ...]
    ask_en: tuple[str, ...]


#: Optional clause fragments: ``tool → clause name → (Hindi, English)``. A clause is emitted only
#: when every fact it names is available, which is how the "invent nothing" rule is kept.
CLAUSES: dict[str, dict[str, tuple[str, str]]] = {
    "get_sales_summary": {
        "count_clause": (", {count} बिल", " across {count} bills"),
        "delta_clause": (
            " पिछले औसत से {delta_abs} {delta_word}।",
            " That is {delta_abs} {delta_word} the usual.",
        ),
        "projection_clause": (
            " पूरे दिन का अनुमान {projected}।",
            " Full-day projection {projected}.",
        ),
    },
    "compare_sales": {
        "delta_clause": (" यानी {delta_abs} {delta_word}।", " That is {delta_abs} {delta_word}."),
    },
    "get_top_customers": {
        "amount_clause": (" इनसे कुल {amount} आया।", " They have spent {amount} between them."),
    },
    "find_dormant_customers": {
        "amount_clause": (" — करीब {amount} का नुकसान", " — roughly {amount} at stake"),
        "names_clause": (" जैसे {names}।", " Names like {names}."),
    },
    "get_inventory_alerts": {
        "names_clause": (" — {names}", " — {names}"),
        "amount_clause": (", {amount} का माल फंसा है", ", {amount} of capital locked"),
    },
    "get_udhaar_summary": {
        "count_clause": (", {count} खातों में", " across {count} khata entries"),
        "age_clause": (
            " सबसे पुराना {oldest_days} दिन का है।",
            " The oldest is {oldest_days} days old.",
        ),
    },
    "get_insights": {
        "amount_clause": (" असर करीब {amount} का।", " Worth about {amount}."),
        "count_clause": (" कुल {count} बातें देखने लायक हैं।", " {count} findings in all."),
    },
    "get_product_performance": {
        "amount_clause": (" कुल {amount}।", " Worth {amount}."),
    },
    "recall_memory": {
        "amount_clause": (" उससे {amount} वापस आया।", " It brought back {amount}."),
        "when_clause": (" ({when})", " ({when})"),
    },
    "send_winback_offer": {
        "discount_clause": (", {discount_text} की छूट", " at {discount_text} off"),
        "amount_clause": (" — अनुमानित वापसी {amount}", " — about {amount} in play"),
        "cost_clause": (
            " {cost} खर्च, {breakeven} ग्राहक से निकल जाएगा।",
            " {cost} to send, covered by {breakeven}.",
        ),
        "dropped_clause": (
            " {dropped} को अनुमति नहीं थी।",
            " {dropped} had not consented.",
        ),
    },
    "send_udhaar_reminder": {
        "amount_clause": (", कुल {amount}", ", {amount} in total"),
        "tone_clause": (" लहजा {tone_word} रखा है।", " Tone kept {tone_word}."),
        "cost_clause": (
            " भेजने का खर्च सिर्फ {cost}।",
            " Sending costs just {cost}.",
        ),
    },
    "draft_restock_order": {
        "amount_clause": (", कीमत {amount}", ", {amount} in value"),
        "names_clause": (" — {names}", " — {names}"),
    },
    "get_merchant_health": {
        "headline_clause": (" {headline}", " {headline}"),
        "tone_clause": (" {tone_word}", " {tone_word}"),
    },
    "_generic": {
        "headline_clause": (" {headline}।", " {headline}."),
        "amount_clause": (" {amount}।", " {amount}."),
        "count_clause": (" {count} रिकॉर्ड।", " {count} records."),
    },
}

TEMPLATES: dict[str, ReplyTemplate] = {
    "get_sales_summary": ReplyTemplate(
        hi=(
            "अब तक {amount} का कलेक्शन{count_clause}।{delta_clause}{projection_clause}",
            "आज का गल्ला अभी {amount}{count_clause}।{delta_clause}{projection_clause}",
            "{amount} आ चुका है{count_clause}।{delta_clause}{projection_clause}",
        ),
        en=(
            "{amount} collected so far{count_clause}.{delta_clause}{projection_clause}",
            "Today's takings are {amount}{count_clause}.{delta_clause}{projection_clause}",
            "{amount} is in the till{count_clause}.{delta_clause}{projection_clause}",
        ),
        ask_hi=(
            "पिछले हफ्ते से तुलना कर दूं?",
            "टॉप ग्राहक दिखा दूं?",
            "कोई सुझाव बताऊं?",
        ),
        ask_en=(
            "Shall I compare it with last week?",
            "Want today's top customers?",
            "Should I flag what needs attention?",
        ),
    ),
    "compare_sales": ReplyTemplate(
        hi=("{amount} इस बार, {baseline} पिछली बार।{delta_clause}",),
        en=("{amount} this time against {baseline} before.{delta_clause}",),
        ask_hi=("वजह देख लूं?", "कोई सुझाव बताऊं?"),
        ask_en=("Want me to dig into why?", "Shall I suggest a fix?"),
    ),
    "get_top_customers": ReplyTemplate(
        hi=("सबसे ज्यादा खरीदने वाले: {names}।{amount_clause}",),
        en=("Your biggest buyers: {names}.{amount_clause}",),
        ask_hi=("इन्हें शुक्रिया वाला ऑफर भेजूं?", "इनकी पूरी लिस्ट निकाल दूं?"),
        ask_en=("Shall I send them a thank-you offer?", "Want the full list?"),
    ),
    "find_dormant_customers": ReplyTemplate(
        hi=(
            "{count} पुराने ग्राहक काफी दिन से नहीं आए{amount_clause}।{names_clause}",
            "{count} रेगुलर ग्राहक गायब हैं{amount_clause}।{names_clause}",
        ),
        en=(
            "{count} regulars have stopped coming{amount_clause}.{names_clause}",
            "{count} of your regulars have gone quiet{amount_clause}.{names_clause}",
        ),
        ask_hi=("इन्हें विनबैक ऑफर भेज दूं?", "इनके लिए छूट तैयार कर दूं?"),
        ask_en=("Shall I send them a win-back offer?", "Want me to draft a discount for them?"),
    ),
    "get_inventory_alerts": ReplyTemplate(
        hi=("{count} चीजों पर ध्यान चाहिए{names_clause}{amount_clause}।",),
        en=("{count} items need attention{names_clause}{amount_clause}.",),
        ask_hi=("सप्लायर का ऑर्डर ड्राफ्ट कर दूं?", "पूरी लिस्ट पढ़ दूं?"),
        ask_en=("Shall I draft the restock order?", "Want me to read the full list?"),
    ),
    "get_udhaar_summary": ReplyTemplate(
        hi=("{amount} उधार बाकी है{count_clause}।{age_clause}",),
        en=("{amount} is still on udhaar{count_clause}.{age_clause}",),
        ask_hi=("रिमाइंडर भेज दूं?", "सबसे पुराने वालों को याद दिला दूं?"),
        ask_en=("Shall I send reminders?", "Want me to nudge the oldest ones?"),
    ),
    "get_merchant_health": ReplyTemplate(
        # No literal digits, per the rule that every number in a reply comes from the data: the
        # scale lives in the score itself and the band word carries the meaning.
        hi=("दुकान की सेहत का स्कोर {count} — {tone_word}।{headline_clause}",),
        en=("Shop health scores {count} — {tone_word}.{headline_clause}",),
        ask_hi=("सबसे कमजोर हिस्सा ठीक करूं?",),
        ask_en=("Want me to work on the weakest part?",),
    ),
    "get_insights": ReplyTemplate(
        hi=("सबसे जरूरी बात — {headline}।{amount_clause}{count_clause}",),
        en=("The one that matters — {headline}.{amount_clause}{count_clause}",),
        ask_hi=("इस पर काम शुरू करूं?", "इसका हल बता दूं?"),
        ask_en=("Want me to act on it?", "Shall I walk you through the fix?"),
    ),
    "get_product_performance": ReplyTemplate(
        hi=("सबसे ज्यादा चलने वाले: {names}।{amount_clause}",),
        en=("Your fastest movers: {names}.{amount_clause}",),
        ask_hi=("इनका स्टॉक ऑर्डर कर दूं?",),
        ask_en=("Shall I reorder these?",),
    ),
    "recall_memory": ReplyTemplate(
        hi=("पिछली बार: {headline}।{amount_clause}{when_clause}",),
        en=("Last time: {headline}.{amount_clause}{when_clause}",),
        ask_hi=("फिर वैसा ही कर दूं?", "इस बार भी वही भेजूं?"),
        ask_en=("Shall I run the same play again?", "Want the same thing sent again?"),
    ),
    "send_winback_offer": ReplyTemplate(
        hi=("{count} ग्राहकों के लिए ऑफर तैयार है{discount_clause}।{dropped_clause}{cost_clause}",),
        en=(
            "The offer is ready for {count} customers{discount_clause}."
            "{dropped_clause}{cost_clause}",
        ),
        ask_hi=("भेज दूं?",),
        ask_en=("Shall I send it?",),
    ),
    "send_udhaar_reminder": ReplyTemplate(
        hi=("{count} खातों का रिमाइंडर तैयार है{amount_clause}।{cost_clause}",),
        en=("Reminders are ready for {count} khata entries{amount_clause}.{cost_clause}",),
        ask_hi=("भेज दूं?",),
        ask_en=("Shall I send them?",),
    ),
    "draft_restock_order": ReplyTemplate(
        hi=("{count} चीजों का ऑर्डर तैयार है{amount_clause}{names_clause}।",),
        en=("The order is drafted for {count} items{amount_clause}{names_clause}.",),
        ask_hi=("सप्लायर को भेज दूं?",),
        ask_en=("Shall I send it to the supplier?",),
    ),
}

#: Used for a tool with no entry of its own, and when no variant's required facts are available.
GENERIC_TEMPLATE = ReplyTemplate(
    hi=("हो गया।{headline_clause}{amount_clause}{count_clause}",),
    en=("Done.{headline_clause}{amount_clause}{count_clause}",),
    ask_hi=("और कुछ देखूं?",),
    ask_en=("Anything else?",),
)

# ── Non-tool replies ────────────────────────────────────────────────────────────────────────

SOCIAL_REPLIES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "greeting": (
        ("नमस्ते! आज का हिसाब देख लूं?", "राम राम! आज की बिक्री बता दूं?"),
        ("Namaste! Shall I pull up today's numbers?", "Hello! Want today's collection?"),
    ),
    "thanks": (
        ("जी, और कुछ देखूं?", "खुशी हुई। और कुछ बताऊं?"),
        ("Anytime. Anything else?", "Glad to help. What next?"),
    ),
    "unknown": (
        (
            "माफ कीजिए, ठीक से समझ नहीं आया। बिक्री, उधार, स्टॉक या ग्राहक — किसके बारे में बताऊं?",
            "जरा दोबारा कहिए। आज का हिसाब, उधार, स्टॉक या पुराने ग्राहक — क्या देखूं?",
        ),
        (
            "Sorry, I didn't catch that. Sales, udhaar, stock or customers — which one?",
            "Say that again for me. Today's numbers, udhaar, stock or lapsed customers?",
        ),
    ),
    "no_tool": (
        ("यह अभी मेरे पास नहीं है। बिक्री, उधार या स्टॉक में से क्या देखूं?",),
        ("I don't have that one wired up yet. Sales, udhaar or stock instead?",),
    ),
}

APPROVAL_YES: tuple[tuple[str, ...], tuple[str, ...]] = (
    ("ठीक है, भेज रहा हूं। हो जाने पर बता दूंगा।", "जी, अभी कर देता हूं। नतीजा आते ही बताऊंगा।"),
    ("Done — sending it now. I'll report back.", "On it. I'll tell you how it lands."),
)
APPROVAL_NO: tuple[tuple[str, ...], tuple[str, ...]] = (
    ("ठीक है, रहने देते हैं। और कुछ देखूं?",),
    ("Alright, leaving it. Anything else?",),
)

#: Markers the agent loop puts in the system/context message when an action awaits a yes/no.
PENDING_ACTION_MARKERS = (
    "pending_action",
    "pending action",
    "pending_approval",
    "awaiting approval",
    "मंजूरी का इंतजार",
)
#: Intents that may be re-read as a plain yes/no when an action is pending.
_APPROVAL_SAFE_INTENTS = frozenset({"unknown", "greeting", "thanks"})

_TONE_WORDS = {
    "gentle": ("नरम", "gentle"),
    "standard": ("सामान्य", "normal"),
    "firm": ("सीधा", "firm"),
}
_MAX_NAMES = 3


# ── Small helpers ───────────────────────────────────────────────────────────────────────────


class _Blank(dict):
    """``format_map`` backing store: an absent placeholder renders as an empty string."""

    def __missing__(self, key: str) -> str:
        return ""


def _dig(payload: Any, keys: tuple[str, ...], accept: Any = None, max_depth: int = 4) -> Any:
    """First value under any of ``keys``, breadth-first, in key priority order."""
    for key in keys:
        queue: deque[tuple[Any, int]] = deque([(payload, 0)])
        while queue:
            node, depth = queue.popleft()
            if depth > max_depth:
                continue
            if isinstance(node, dict):
                if key in node:
                    value = node[key]
                    if accept is None or accept(value):
                        return value
                queue.extend((child, depth + 1) for child in node.values())
            elif isinstance(node, list):
                queue.extend((child, depth + 1) for child in node)
    return None


def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name}


def _lang_key(language: str) -> str:
    """``"hi"`` or ``"en"`` — the two registers the templates are authored in."""
    return "hi" if (language or "").lower().startswith("hi") else "en"


def _join_names(names: list[str], lang: str) -> str:
    trimmed = [str(name).strip() for name in names if str(name).strip()][:_MAX_NAMES]
    if not trimmed:
        return ""
    if len(trimmed) == 1:
        return trimmed[0]
    joiner = " और " if lang == "hi" else " and "
    return ", ".join(trimmed[:-1]) + joiner + trimmed[-1]


def _names_from(value: Any) -> list[str]:
    """Names out of a list of strings *or* a list of ``{"name": …}`` rows."""
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            for key in ("name", "label", "customer_name", "product_name", "title"):
                if isinstance(item.get(key), str):
                    names.append(item[key])
                    break
    return names


#: Artefacts a dropped clause can leave behind — a danda or comma stranded after a space.
_STRAY_PUNCTUATION = (
    (" ।", "।"),
    (" .", "."),
    (" ,", ","),
    ("।।", "।"),
    ("..", "."),
    ("  ", " "),
)


def _tidy(text: str) -> str:
    """Collapse the gaps left by dropped clauses, without disturbing the numbers."""
    cleaned = " ".join(text.split())
    for stray, fixed in _STRAY_PUNCTUATION:
        cleaned = cleaned.replace(stray, fixed)
    return cleaned.strip()


def _clamp_words(lead: str, ask: str) -> str:
    """Keep the reply speakable: drop context sentences (never the number, never the ask)."""
    sentences = [part for part in lead.replace("।", "।\x00").split("\x00") if part.strip()]
    while sentences and len(f"{' '.join(sentences)} {ask}".split()) > MAX_SPOKEN_WORDS:
        sentences.pop()
    return _tidy(f"{' '.join(sentences)} {ask}") if sentences else _tidy(ask)


class LocalLLM:
    """Offline :class:`~munshiji.providers.llm.LLMProvider`.

    Real NLU, real templates, real numbers — no network, no credentials, no hardcoded answers.
    """

    name = "munshiji-local"
    mode: ProviderMode = "local"
    kind = "llm"

    def __init__(self, *, model: str = "munshiji-nlu-v1") -> None:
        self.model = model

    # ── Protocol ────────────────────────────────────────────────────────────────────────
    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.3,
        max_tokens: int = 800,
        language: str = "hi-IN",
    ) -> LLMResponse:
        """Select a tool, or compose the spoken answer from tool results already in hand.

        ``temperature`` and ``max_tokens`` are accepted for protocol parity and ignored: this
        provider is deterministic, and its variation comes from the turn index alone.
        """
        started = time.perf_counter()
        lang = _lang_key(language)
        turn = len(messages)
        advertised = {tool.name: tool for tool in (tools or [])}

        tool_results = _trailing_tool_messages(messages)
        if tool_results:
            text, source = self._compose(tool_results, lang=lang, turn=turn)
            return self._reply(text, started, language=language, why=f"compose:{source}")

        user = _last_user_message(messages)
        if user is None or not user.content.strip():
            return self._reply(
                _pick(SOCIAL_REPLIES["unknown"][0 if lang == "hi" else 1], turn),
                started,
                language=language,
                why="empty",
            )

        intent = detect_intent(user.content, language=language)

        pending = _pending_action(messages)
        if pending and intent.name in _APPROVAL_SAFE_INTENTS:
            verdict = detect_affirmation(user.content)
            if verdict is not None:
                table = APPROVAL_YES if verdict else APPROVAL_NO
                text = _pick(table[0 if lang == "hi" else 1], turn)
                return self._reply(
                    text, started, language=language, why=f"approval:{pending}:{verdict}"
                )

        tool_name = _select_tool(intent.name, advertised)
        if tool_name is None:
            # Understood but unserviceable ("no_tool") reads very differently from not understood.
            key = intent.name if intent.name in SOCIAL_REPLIES else "no_tool"
            text = _pick(SOCIAL_REPLIES[key][0 if lang == "hi" else 1], turn)
            return self._reply(text, started, language=language, why=f"clarify:{intent.name}")

        arguments = _fill_parameters(
            advertised[tool_name], intent.slots, query=user.content, language=language
        )
        call = ToolCall(id=f"call_{turn:02d}_{tool_name}", name=tool_name, arguments=arguments)
        return self._reply(
            "",
            started,
            language=language,
            why=f"intent:{intent.name}",
            tool_calls=[call],
            intent=intent,
        )

    async def health(self) -> ProviderHealth:
        """Always healthy: no network, no credentials, no I/O to fail."""
        return ProviderHealth(
            name=self.name,
            kind="llm",
            mode=self.mode,
            ok=True,
            detail=f"{self.model}: offline intent router + template composer",
            latency_ms=0,
        )

    # ── Internals ───────────────────────────────────────────────────────────────────────
    def _reply(
        self,
        text: str,
        started: float,
        *,
        language: str,
        why: str,
        tool_calls: list[ToolCall] | None = None,
        intent: Any = None,
    ) -> LLMResponse:
        raw: dict[str, Any] = {"why": why, "language": language}
        if intent is not None:
            raw["intent"] = intent.as_dict()
        return LLMResponse(
            text=text,
            tool_calls=tool_calls or [],
            provider="local",
            model=self.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            finish_reason="tool_calls" if tool_calls else "stop",
            raw=raw,
        )

    def _compose(self, results: list[ChatMessage], *, lang: str, turn: int) -> tuple[str, str]:
        """Turn tool results into one spoken sentence plus one question.

        Only the first result with a template is voiced — a 45-word utterance has room for one
        number and one next step, and stacking two findings is how a demo loses the room.
        """
        chosen_name, payload = _primary_result(results)
        facts = _collect_facts(chosen_name, payload, lang)
        template = TEMPLATES.get(chosen_name, GENERIC_TEMPLATE)
        clauses = CLAUSES.get(chosen_name, CLAUSES["_generic"])

        for name, (hi_form, en_form) in clauses.items():
            form = hi_form if lang == "hi" else en_form
            facts[name] = form.format_map(_Blank(facts)) if _has_all(form, facts) else ""

        variants = template.hi if lang == "hi" else template.en
        usable = [form for form in variants if _has_required(form, facts)]
        if not usable:
            generic = GENERIC_TEMPLATE.hi if lang == "hi" else GENERIC_TEMPLATE.en
            for name, (hi_form, en_form) in CLAUSES["_generic"].items():
                form = hi_form if lang == "hi" else en_form
                facts[name] = form.format_map(_Blank(facts)) if _has_all(form, facts) else ""
            usable = list(generic)
            asks = GENERIC_TEMPLATE.ask_hi if lang == "hi" else GENERIC_TEMPLATE.ask_en
        else:
            asks = template.ask_hi if lang == "hi" else template.ask_en

        lead = _tidy(_pick(tuple(usable), turn).format_map(_Blank(facts)))
        return _clamp_words(lead, _pick(asks, turn)), chosen_name


# ── Composition helpers ─────────────────────────────────────────────────────────────────────


def _pick(variants: tuple[str, ...] | list[str], turn: int) -> str:
    """Deterministic rotation, seeded by turn index — varied phrasing, identical numbers."""
    return variants[turn % len(variants)] if variants else ""


def _has_all(form: str, facts: dict[str, str]) -> bool:
    return all(facts.get(name) for name in _placeholders(form))


def _has_required(form: str, facts: dict[str, str]) -> bool:
    """Optional (``*_clause``) placeholders may be blank; anything else must resolve."""
    return all(facts.get(name) for name in _placeholders(form) if not name.endswith("_clause"))


def _trailing_tool_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    trailing: list[ChatMessage] = []
    for message in reversed(messages):
        if message.role != "tool":
            break
        trailing.append(message)
    return list(reversed(trailing))


def _last_user_message(messages: list[ChatMessage]) -> ChatMessage | None:
    for message in reversed(messages):
        if message.role == "user":
            return message
    return None


def _pending_action(messages: list[ChatMessage]) -> str:
    """Tool name of the action awaiting a yes/no, ``""`` when nothing is pending."""
    for message in reversed(messages):
        if message.role not in ("system", "assistant"):
            continue
        haystack = (message.content or "").lower()
        for marker in PENDING_ACTION_MARKERS:
            index = haystack.find(marker.lower())
            if index < 0:
                continue
            tail = haystack[index + len(marker) : index + len(marker) + 60].lstrip(" :=\"'")
            token = tail.split()[0].strip(",.\"'`") if tail.split() else ""
            return token or marker
    return ""


def _primary_result(results: list[ChatMessage]) -> tuple[str, dict[str, Any]]:
    """The tool result to speak: the first one we have a template for, else the first."""
    parsed = [(message.name or "", _as_payload(message.content)) for message in results]
    for name, payload in parsed:
        if name in TEMPLATES:
            return name, payload
    return parsed[0]


def _as_payload(content: str) -> dict[str, Any]:
    try:
        decoded = json.loads(content or "{}")
    except (ValueError, TypeError):
        return {"message": content}
    if isinstance(decoded, dict):
        return decoded
    if isinstance(decoded, list):
        return {"items": decoded}
    return {"message": str(decoded)}


def _collect_facts(tool_name: str, payload: dict[str, Any], lang: str) -> dict[str, str]:
    """Render every fact this tool can speak, as display strings. Missing facts stay absent."""
    facts: dict[str, str] = {}
    raw: dict[str, Any] = {}
    for spec in (*FACT_SPECS.get(tool_name, ()), *COMMON_FACT_SPECS):
        if spec.name in facts:
            continue
        value = _dig(payload, spec.keys, accept=_acceptor(spec.kind))
        if value is None:
            continue
        rendered = _render_fact(spec.kind, value, lang)
        if rendered:
            facts[spec.name] = rendered
            raw[spec.name] = value

    if "delta" in raw:
        magnitude = abs(float(raw["delta"]))
        facts["delta_abs"] = f"{magnitude:.0f}%"
        up = float(raw["delta"]) >= 0
        if lang == "hi":
            facts["delta_word"] = "ऊपर" if up else "नीचे"
        else:
            facts["delta_word"] = "above" if up else "below"
    if "tone" in facts:
        hi_word, en_word = _TONE_WORDS.get(facts["tone"].lower(), (facts["tone"], facts["tone"]))
        facts["tone_word"] = hi_word if lang == "hi" else en_word
    if "discount_pct" in facts:
        facts["discount_text"] = facts["discount_pct"]
    elif "discount" in facts:
        facts["discount_text"] = facts["discount"]
    return facts


def _acceptor(kind: str) -> Any:
    if kind in (_MONEY, _INT):
        return lambda value: isinstance(value, int) and not isinstance(value, bool)
    if kind in (_PCT, _RUPEES):
        return lambda value: isinstance(value, int | float) and not isinstance(value, bool)
    if kind == _TEXT:
        return lambda value: isinstance(value, str) and bool(value.strip())
    if kind == _NAMES:
        return lambda value: isinstance(value, list) and bool(_names_from(value))
    return None


def _render_fact(kind: str, value: Any, lang: str) -> str:
    if kind == _MONEY:
        return fmt_inr(int(value))
    if kind == _RUPEES:
        return fmt_inr(paise_from_rupees(value))
    if kind == _INT:
        return str(int(value))
    if kind == _PCT:
        return f"{abs(float(value)):.0f}%"
    if kind == _NAMES:
        return _join_names(_names_from(value), lang)
    return str(value).strip()


# ── Tool selection helpers ──────────────────────────────────────────────────────────────────


def _select_tool(intent_name: str, advertised: dict[str, ToolSpec]) -> str | None:
    """First advertised candidate for this intent — never a tool the caller did not offer."""
    for candidate in TOOL_FOR_INTENT.get(intent_name, ()):
        if candidate in advertised:
            return candidate
    return None


def _fill_parameters(
    spec: ToolSpec, slots: dict[str, Any], *, query: str, language: str
) -> dict[str, Any]:
    """Map slots onto the tool's declared parameters, then satisfy anything still required."""
    schema = spec.parameters or {}
    properties: dict[str, Any] = schema.get("properties") or {}
    required: list[str] = list(schema.get("required") or [])
    arguments: dict[str, Any] = {}

    for slot, value in slots.items():
        for candidate in SLOT_ALIASES.get(slot, (slot,)):
            if candidate in properties and candidate not in arguments:
                coerced = _coerce(value, properties[candidate])
                if coerced is not None:
                    arguments[candidate] = coerced
                break

    for candidate in _QUERY_PARAMS:
        if candidate in properties and candidate not in arguments:
            arguments[candidate] = query.strip()
            break

    for name in required:
        if name in arguments:
            continue
        arguments[name] = _default_for(name, properties.get(name) or {})
    return arguments


def _coerce(value: Any, prop: dict[str, Any]) -> Any:
    """Fit a slot value to a declared parameter type, honouring ``enum`` when present."""
    kind = prop.get("type")
    try:
        if kind == "integer":
            value = int(value)
        elif kind == "number":
            value = float(value)
        elif kind == "boolean":
            value = bool(value)
        elif kind == "string":
            value = str(value)
        elif kind == "array" and not isinstance(value, list):
            value = [value]
    except (TypeError, ValueError):
        return None

    enum = prop.get("enum")
    if isinstance(enum, list) and enum and value not in enum:
        lowered = {str(option).lower(): option for option in enum}
        match = lowered.get(str(value).lower())
        return match if match is not None else prop.get("default", enum[0])
    return value


def _default_for(name: str, prop: dict[str, Any]) -> Any:
    """A usable value for a required parameter nothing filled: schema first, then convention."""
    if "default" in prop:
        return prop["default"]
    enum = prop.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    kind = prop.get("type")
    if kind == "integer":
        if "days" in name:
            return _DEFAULT_DAYS
        return _DEFAULT_COUNT if any(token in name for token in _COUNTISH) else 0
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    if kind == "object":
        return {}
    return ""
