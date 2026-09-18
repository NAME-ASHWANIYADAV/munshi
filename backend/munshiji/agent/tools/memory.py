"""Memory tool - the one that makes MunshiJi a teammate rather than a chatbot.

Everything else answers from today's database. This answers from what MunshiJi *did*, in earlier
conversations, and how it turned out.

The tool distils; the composer speaks. A retrieval returns every matched fact with its provenance,
but a spoken reply carries one clause - so this module picks the single fact worth saying and the
vetted numbers that go with it, and hands nothing else to the language layer.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field

from munshiji.agent.tools.base import Tool, ToolContext, ToolResult
from munshiji.clock import days_between, to_ist
from munshiji.db.enums import MemoryKind
from munshiji.money import fmt_inr

__all__ = ["FACT_KINDS", "TOOLS"]

#: Conversation nodes hold verbatim transcripts, including MunshiJi's own replies. Left in the
#: default search they outrank the facts - the model ends up recalling its own sentences back at
#: the merchant instead of what actually happened. Recalling them stays possible, just opt-in.
FACT_KINDS: tuple[MemoryKind, ...] = (
    MemoryKind.ACTION,
    MemoryKind.DAY,
    MemoryKind.CUSTOMER,
    MemoryKind.PRODUCT,
    MemoryKind.INSIGHT,
    MemoryKind.NOTE,
    MemoryKind.MERCHANT,
)

#: Memory node text is authored bilingually as "<english> <hindi>" in one string, so the spoken
#: half has to be sliced back out rather than translated.
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")

#: Sentence terminators: the Devanagari danda, and the Latin full stop.
_SENTENCE_END = ("।", ".")

#: A spoken reply carries one clause, not a paragraph. A memory node's text is written for the
#: index and runs long; anything past this is dropped by the composer's word budget anyway, taking
#: the whole sentence with it - so the sentence handed over has to already be short.
_LEAD_CHARS = 120

#: Outcome metrics an action node may carry, in the order a merchant would want them spoken.
_RECOVERY_KEYS = ("revenue_recovered_paise", "recovered_paise", "collected_paise")
_COUNT_KEYS = ("redeemed", "messages_sent", "delivered_count", "messages_failed")


def _language_half(text: str, language: str) -> str:
    """The Hindi or English half of a bilingual node text."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    match = _DEVANAGARI.search(cleaned)
    if match is None:
        return cleaned
    if language.startswith("hi"):
        # Rewind to the start of the sentence the first Devanagari character sits in.
        start = max(
            (cleaned.rfind(end, 0, match.start()) for end in _SENTENCE_END),
            default=-1,
        )
        return cleaned[start + 1 :].strip() or cleaned
    return cleaned[: match.start()].strip() or cleaned


def _lead_sentence(text: str, language: str) -> str:
    """The first statement of a memory node: what happened, without the trailing analysis."""
    half = _language_half(text, language)
    if not half:
        return ""
    cut = min(
        (index for index in (half.find(end) for end in _SENTENCE_END) if index > 0),
        default=-1,
    )
    lead = half[: cut + 1] if cut > 0 else half
    if len(lead) > _LEAD_CHARS:
        lead = lead[:_LEAD_CHARS].rsplit(" ", 1)[0] + "…"
    return lead


def _spoken_date(when: datetime | None, as_of: datetime) -> str:
    """A date a person would say out loud, not an ISO timestamp."""
    if when is None:
        return ""
    gap = days_between(when, as_of)
    if gap <= 0:
        return "आज"  # aaj
    if gap == 1:
        return "कल"  # kal
    if gap <= 6:
        return f"{gap} दिन पहले"  # N din pehle
    return to_ist(when).strftime("%d %b")


def _outcomes_of(attrs: dict) -> dict:
    """Action nodes nest their measured results; everything else keeps them flat."""
    nested = attrs.get("outcomes")
    return nested if isinstance(nested, dict) else attrs


def _distil(hits: list, as_of: datetime, language: str) -> dict[str, object]:
    """Reduce a recall to the one fact worth saying, plus its vetted numbers.

    An action node wins when present - "what happened to the offer" is nearly always what is being
    asked - and its *measured* outcome supplies the figure, so the reply quotes what was recovered
    rather than what was once estimated.
    """
    if not hits:
        return {"summary": "", "when": "", "found": False}

    # "Who came back?" recalls several return notes at once. Answering with only the top note
    # would name one customer and stay silent about the rest — worse than useless to a merchant
    # deciding whether the offer worked. When two or more return notes match, the fact worth
    # saying is the roll-up: every name, and what they spent between them.
    returns = [
        hit
        for hit in hits
        if hit.kind is MemoryKind.NOTE and str(hit.ref).startswith("note:return-")
    ]
    if len(returns) >= 2:
        names: list[str] = []
        total = 0
        for hit in returns:
            name = (hit.label or "").split(" returned", 1)[0].strip()
            if name and name not in names:
                names.append(name)
            amount = (hit.attrs or {}).get("amount_paise")
            if isinstance(amount, int | float):
                total += int(amount)
        if len(names) >= 2:
            joiner = " और " if language.startswith("hi") else " and "
            spoken_names = f"{', '.join(names[:-1])}{joiner}{names[-1]}"
            if language.startswith("hi"):
                summary = f"{spoken_names} वापसी ऑफर के बाद दुकान लौटे"
                if total > 0:
                    summary += f" — करीब {fmt_inr(total)} की खरीदारी"
                summary += "।"
            else:
                summary = f"{spoken_names} came back after the win-back offer"
                if total > 0:
                    summary += f" — about {fmt_inr(total)} in purchases"
                summary += "."
            # The summary sentence already carries the figure and the timing; handing the
            # composer recovered_* or a "when" as well makes it say the same rupees twice.
            return {
                "found": True,
                "summary": summary,
                "top_ref": returns[0].ref,
                "top_kind": MemoryKind.NOTE.value,
                "when": "",
                "returned_count": len(names),
                "returned_names": names,
            }

    preferred = next((hit for hit in hits if hit.kind is MemoryKind.ACTION), hits[0])
    attrs = preferred.attrs or {}
    outcomes = _outcomes_of(attrs)

    distilled: dict[str, object] = {
        "found": True,
        "summary": _lead_sentence(preferred.text, language),
        "top_ref": preferred.ref,
        "top_kind": preferred.kind.value,
        "when": _spoken_date(preferred.occurred_at, as_of),
    }

    for key in _RECOVERY_KEYS:
        value = outcomes.get(key)
        if isinstance(value, int | float) and value > 0:
            distilled["recovered_paise"] = int(value)
            distilled["recovered_display"] = fmt_inr(int(value))
            break
    for key in _COUNT_KEYS:
        value = outcomes.get(key)
        if isinstance(value, int | float):
            distilled[key] = int(value)
    if isinstance(attrs.get("target_count"), int):
        distilled["target_count"] = attrs["target_count"]
    return distilled


class RecallParams(BaseModel):
    query: str = Field(
        description=(
            "What to remember, in the merchant's own words - e.g. 'pichla offer kya hua', "
            "'Sunita ji ka udhaar', 'last week ka collection'."
        )
    )
    limit: int = Field(default=5, ge=1, le=12)
    hops: int = Field(
        default=1,
        ge=0,
        le=2,
        description="How far to traverse the knowledge graph from the matched facts.",
    )
    kinds: list[MemoryKind] | None = Field(
        default=None,
        description=(
            "Restrict recall to certain kinds of memory. Defaults to facts (actions, days, "
            "customers, products, insights, notes); pass 'conversation' explicitly to search "
            "what was said."
        ),
    )


async def _recall(ctx: ToolContext, params: RecallParams) -> ToolResult:
    context = await ctx.providers.memory.search(
        ctx.merchant_id,
        params.query,
        limit=params.limit,
        hops=params.hops,
        kinds=params.kinds or FACT_KINDS,
    )

    # Raw node attributes are deliberately withheld from the payload the language layer sees.
    # They carry estimates and intermediate figures, and a composer that searches the payload for
    # "a number" would happily speak one of those as though it had been measured.
    hits = [
        {
            "ref": hit.ref,
            "kind": hit.kind.value,
            "label": hit.label,
            "text": hit.text,
            "hops": hit.hops,
            "path": hit.path,
        }
        for hit in context.hits
    ]

    data: dict[str, object] = {
        "query": params.query,
        "hits": hits,
        "hit_count": len(hits),
        "provider": context.provider,
    }
    data.update(_distil(context.hits, ctx.as_of, ctx.language))

    return ToolResult(
        ok=True,
        data=data,
        summary_en=(
            f"Recalled {len(hits)} facts via {context.provider} memory"
            if hits
            else "Nothing in memory about that yet"
        ),
        summary_hi=(
            f"{len(hits)} baatein yaad aayin" if hits else "Iske baare mein kuch yaad nahi hai"
        ),
    )


TOOLS: list[Tool] = [
    Tool(
        name="recall_memory",
        description=(
            "Search MunshiJi's long-term memory of this shop: past days, past conversations, and "
            "crucially past actions together with their measured outcomes. Use whenever the "
            "merchant refers to something earlier - 'pichli baar', 'last week', 'uska kya hua'."
        ),
        params_model=RecallParams,
        handler=_recall,
        label_en="Recall",
        label_hi="Yaad karo",
    ),
]
