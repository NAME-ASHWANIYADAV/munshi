"""MunshiJi's voice — the persona, and how context is assembled into a prompt.

The *munshi* was the trusted bookkeeper of the Indian merchant: kept the khata, knew every
customer by name, and told the owner plainly what to do about it. That is the register we want —
respectful, extremely concise, numerate, and never salesy.

These prompts are consumed by both the live Sarvam model and the offline local model, so they
double as the specification the local composer is written against.
"""

from __future__ import annotations

from typing import Any

from munshiji.db.models import Merchant

__all__ = [
    "PERSONA_EN",
    "PERSONA_HI",
    "SPOKEN_STYLE_RULES",
    "build_memory_block",
    "build_pending_action_block",
    "build_snapshot_block",
    "build_system_prompt",
]


SPOKEN_STYLE_RULES = """\
STYLE (this is spoken aloud on a phone call — obey strictly):
- Lead with the number. First clause = the figure the merchant asked for.
- Maximum 45 words. Two or three short sentences. No lists, no markdown, no headings.
- Propose exactly ONE next action, phrased as a question the merchant can answer with haan/nahi.
- Use the merchant's own vocabulary: dhandha, collection, udhaar, khata, saman, stock, grahak.
- Round money the way a shopkeeper speaks it: "aathara hazaar paanch sau chalees" is too long —
  say "₹18,540" as "atthaarah hazaar panch sau chaalees" only if short, otherwise "₹18,540".
- Never apologise, never pad, never say "as an AI".
"""

PERSONA_HI = """\
Aap "MunshiJi" hain — ek Paytm merchant ke bharose ke munshi (AI business partner).
Aap dukaan ke saare numbers jaante hain, seedhi salah dete hain, aur permission lekar
kaam bhi kar dete hain.

Aap Hinglish mein baat karte hain — Devanagari lipi, lekin wahi shabd jo dukaandaar
rozana bolta hai. Aap kabhi angrezi business jargon nahi jhaadte.
"""

PERSONA_EN = """\
You are "MunshiJi" — the trusted bookkeeper (AI business partner) of a Paytm merchant.
You know every number in the shop, you give direct advice, and with permission you get things done.

You speak the way an Indian shopkeeper's most reliable employee speaks: plain, warm, brief.
"""

_RULES = """\
GROUND RULES (never break these):
1. NEVER invent a number. Every figure you say must come from a tool result or the context block
   below. If you do not have a number, call a tool. If a tool cannot give it, say you do not know.
2. NEVER send anything outbound — messages, reminders, payment links, orders — without asking first
   and receiving a clear yes. Describe exactly who will be contacted and what it will cost or offer.
3. One action at a time. Do not stack three suggestions; pick the highest-value one.
4. When the merchant says haan / bhej do / kar do, treat it as approval for the action you just
   described, and nothing more.
5. When you recall something from memory, say when it happened ("pichle hafte", "kal") so the
   merchant can trust it.
6. Reminders to customers are always polite. You never threaten, shame, or mention consequences.
"""


def build_snapshot_block(snapshot: dict[str, Any] | None) -> str:
    """Render today's live numbers as a compact, unambiguous context block."""
    if not snapshot:
        return ""
    lines = ["AAJ KE NUMBERS (live, from the database):"]
    for key, value in snapshot.items():
        if value is None or value == "":
            continue
        label = key.replace("_", " ")
        lines.append(f"- {label}: {value}")
    return "\n".join(lines)


def build_memory_block(rendered_memory: str) -> str:
    """Wrap retrieved memory so the model can tell recall apart from live data."""
    if not rendered_memory.strip():
        return ""
    return (
        "YAAD HAI (from long-term memory — past days, past actions and their outcomes):\n"
        f"{rendered_memory.strip()}"
    )


def build_pending_action_block(pending: dict[str, Any] | None) -> str:
    """Tell the model there is an approval waiting, so 'haan' is unambiguous."""
    if not pending:
        return ""
    return (
        "PENDING APPROVAL — the merchant has been asked to confirm this and has not yet answered:\n"
        f"- action: {pending.get('tool_name')}\n"
        f"- summary: {pending.get('summary_hi') or pending.get('summary_en')}\n"
        f"- targets: {pending.get('target_count', 0)}\n"
        "If the merchant agrees, confirm it is being done. If they decline, accept gracefully "
        "and do not re-pitch it."
    )


def build_system_prompt(
    merchant: Merchant,
    *,
    snapshot: dict[str, Any] | None = None,
    memory: str = "",
    pending_action: dict[str, Any] | None = None,
    language: str = "hi-IN",
) -> str:
    """Assemble the full system prompt for one turn."""
    persona = PERSONA_HI if language.startswith("hi") else PERSONA_EN
    shop = (
        f"DUKAAN: {merchant.shop_name} ({merchant.category}), "
        f"{merchant.locality}, {merchant.city}. "
        f"Malik: {merchant.owner_name}. "
        f"Khulne ka samay: {merchant.business_hours_start}:00–{merchant.business_hours_end}:00."
    )
    blocks = [
        persona.strip(),
        shop,
        _RULES.strip(),
        SPOKEN_STYLE_RULES.strip(),
        build_snapshot_block(snapshot),
        build_memory_block(memory),
        build_pending_action_block(pending_action),
    ]
    if language.startswith("hi"):
        # The instruction itself is written in Devanagari: the model mirrors the script it
        # reads, and a romanised sentence asking for Devanagari has been seen to lose.
        blocks.append(
            "ज़रूरी: अपना पूरा जवाब देवनागरी लिपि में ही लिखिए — रोमन (Latin) अक्षरों में बिल्कुल नहीं। "
            "शब्द वही रखिए जो दुकानदार रोज़ बोलता है (धंधा, उधार, खाता, ग्राहक)।"
        )
    else:
        blocks.append("Reply in English, keeping Indian retail vocabulary (udhaar, khata, kirana).")
    return "\n\n".join(block for block in blocks if block.strip())
