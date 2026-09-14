"""Bilingual message templates — the actual words that reach a customer's phone.

Everything MunshiJi sends out is authored here, in Hindi (Devanagari) and English, by hand.
Nothing is machine-translated at render time (SPEC.md §2.3) and nothing in this module touches
the database, the clock or the network: every function is **pure**, so the wording can be
reviewed, diffed and unit-tested on its own.

Product-safety boundary (SPEC.md §2.4) — read before editing
------------------------------------------------------------
Udhaar (credit) reminders ride a three-rung tone ladder — ``GENTLE`` → ``STANDARD`` → ``FIRM``
— and there is **deliberately no rung above FIRM**. ``FIRM`` means *clear and direct*
("kripya is hafte tak settle kar dijiye"), never coercive. No reminder in any tier may:

* mention consequences of non-payment, legal action, police, courts or recovery agents;
* threaten interest, penalties, blacklisting or withdrawal of credit;
* shame the customer, publicly or privately, or address anyone but the customer;
* manufacture urgency the merchant did not ask for.

The merchant has to sell this person groceries again tomorrow morning, and an automated
collection system operating at scale must not be the thing that makes small-ticket debt
frightening. :data:`FORBIDDEN_TERMS` encodes that boundary, :func:`contains_forbidden_terms`
is the reusable guard, and ``tests/test_messaging.py`` asserts that no template in any tone
trips it. If you add a tier or soften this rule, you are changing the product, not the copy.

Messages are kept under :data:`MAX_MESSAGE_CHARS` (SMS/WhatsApp-safe), name the customer and
the shop, and carry at most one emoji.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from munshiji.db.enums import Tone
from munshiji.money import fmt_inr

__all__ = [
    "FORBIDDEN_TERMS",
    "MAX_MESSAGE_CHARS",
    "MESSAGE_TOOLS",
    "TONE_RULES",
    "ToneRule",
    "contains_forbidden_terms",
    "followup_note",
    "payment_link",
    "render_for_tool",
    "restock_order",
    "select_tone",
    "udhaar_reminder",
    "winback_offer",
]

#: SMS/WhatsApp-safe ceiling. Every renderer in this module guarantees it for any input.
MAX_MESSAGE_CHARS = 320

Bilingual = dict[str, str]


# ─────────────────────────────────────────────────────────────────────────────
# Safety vocabulary
# ─────────────────────────────────────────────────────────────────────────────

#: Words and phrases that must never appear in an outbound MunshiJi message, in any tone.
#: Matched case-insensitively as substrings. Kept deliberately blunt — a false positive here
#: costs one rewritten sentence; a false negative costs a merchant their customer.
FORBIDDEN_TERMS: tuple[str, ...] = (
    # English — consequences, authority, coercion
    "legal",
    "lawyer",
    "advocate",
    "court",
    "police",
    "penalty",
    "penalties",
    "blacklist",
    "defaulter",
    "recovery agent",
    "consequence",
    "warning",
    "final notice",
    "last chance",
    "no more credit",
    "stop your credit",
    "report you",
    "action will be taken",
    "immediately",
    "threat",
    "seize",
    "must pay",
    "interest",
    "shame",
    # Hindi — the same boundary in Devanagari
    "कानूनी",
    "क़ानूनी",
    "वकील",
    "अदालत",
    "कोर्ट",
    "पुलिस",
    "थाने",
    "जुर्माना",
    "ब्याज",
    "चेतावनी",
    "आखिरी मौका",
    "आख़िरी मौका",
    "अंतिम सूचना",
    "कार्रवाई",
    "वसूली",
    "नतीजा भुगत",
    "बदनाम",
    "धमकी",
    "उधार बंद",
    "मजबूर",
    "सख्त",
    "सख़्त",
)


def contains_forbidden_terms(text: str) -> list[str]:
    """Return every :data:`FORBIDDEN_TERMS` entry present in ``text`` (case-insensitive).

    Empty list means the text respects the §2.4 boundary. Used by the tests and available to
    ``agent/approval.py`` as a last-line check before anything leaves the building.
    """
    lowered = text.lower()
    return [term for term in FORBIDDEN_TERMS if term.lower() in lowered]


# ─────────────────────────────────────────────────────────────────────────────
# Tone ladder
# ─────────────────────────────────────────────────────────────────────────────

#: Below this many days overdue the first nudge is always soft.
GENTLE_MAX_DAYS = 15
#: A customer with a strong settle history keeps the soft register this long.
RELIABLE_GRACE_DAYS = 30
#: Only past this age does the direct register come into play at all.
FIRM_MIN_DAYS = 45
#: Score at or above which a customer counts as a reliable payer (0.0–1.0).
RELIABLE_SCORE = 0.75
#: Score below which the settle history is genuinely weak.
WEAK_SCORE = 0.40
#: ₹5,000 — the point where an unpaid khata materially hurts a kirana's working capital.
HIGH_VALUE_PAISE = 500_000


@dataclass(frozen=True, slots=True)
class ToneRule:
    """One row of the tone rule table. First match wins."""

    name: str
    tone: Tone
    why: str
    matches: Callable[[int, float, int], bool]


#: The rule table, evaluated top-down by :func:`select_tone`.
#: Arguments to each predicate are ``(days_overdue, reliability_score, amount_paise)``.
TONE_RULES: tuple[ToneRule, ...] = (
    ToneRule(
        name="fresh",
        tone=Tone.GENTLE,
        why="The first nudge on a young balance is always soft, whatever the amount.",
        matches=lambda days, _score, _paise: days <= GENTLE_MAX_DAYS,
    ),
    ToneRule(
        name="reliable_grace",
        tone=Tone.GENTLE,
        why="A customer who has always settled gets an extra fortnight of benefit of the doubt.",
        matches=lambda days, score, _paise: (
            days <= RELIABLE_GRACE_DAYS and score >= RELIABLE_SCORE
        ),
    ),
    ToneRule(
        name="long_overdue_weak_history",
        tone=Tone.FIRM,
        why="Past 45 days with a patchy settle record, the ask has to be unambiguous.",
        matches=lambda days, score, _paise: days >= FIRM_MIN_DAYS and score < RELIABLE_SCORE,
    ),
    ToneRule(
        name="large_sum_weak_history",
        tone=Tone.FIRM,
        why="A month-old balance above ₹5,000 from a weak payer is real working capital at risk.",
        matches=lambda days, score, paise: (
            days >= RELIABLE_GRACE_DAYS and score < WEAK_SCORE and paise >= HIGH_VALUE_PAISE
        ),
    ),
    ToneRule(
        name="default",
        tone=Tone.STANDARD,
        why="Everything else: a plain, polite ask with a soft deadline.",
        matches=lambda _days, _score, _paise: True,
    ),
)


def select_tone(days_overdue: int, reliability_score: float, amount_paise: int) -> Tone:
    """Pick the reminder register from the khata's facts.

    Args:
        days_overdue: Whole days past the due date (IST calendar days). Negative treated as 0.
        reliability_score: 0.0–1.0, how dependably this customer has settled before
            (see ``insights/credit.py``). Values outside the range are clamped.
        amount_paise: Outstanding balance, in paise.

    Returns:
        The gentlest tone consistent with :data:`TONE_RULES`. Never harsher than
        :attr:`~munshiji.db.enums.Tone.FIRM` — see the module docstring.
    """
    days = max(0, int(days_overdue))
    score = min(1.0, max(0.0, float(reliability_score)))
    paise = max(0, int(amount_paise))
    for rule in TONE_RULES:
        if rule.matches(days, score, paise):
            return rule.tone
    return Tone.STANDARD  # pragma: no cover - the table ends in a catch-all


# ─────────────────────────────────────────────────────────────────────────────
# Small pure helpers
# ─────────────────────────────────────────────────────────────────────────────


def _pick(value: str | Mapping[str, str] | None, lang: str, *, default: str = "") -> str:
    """Resolve a value that may be plain text or a ``{"hi": …, "en": …}`` mapping."""
    if value is None:
        return default
    if isinstance(value, Mapping):
        chosen = value.get(lang) or value.get("en") or value.get("hi")
        return str(chosen) if chosen else default
    text = str(value).strip()
    return text or default


def _fit(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Hard-clamp ``text`` to ``limit`` characters, ending with an ellipsis when truncated."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _fmt_qty(value: Any) -> str:
    """``12.0`` -> ``'12'``, ``1.5`` -> ``'1.5'``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def _item_phrase(item: Mapping[str, Any], lang: str) -> str:
    """Render one restock line: ``'12 kg Toor Dal'``."""
    name = str(item.get("name_hi") or item.get("name") or "") if lang == "hi" else ""
    name = name or str(item.get("name") or item.get("sku") or "item")
    qty = item.get("qty", item.get("quantity", 0))
    unit = str(item.get("unit") or "").strip()
    parts = [p for p in (_fmt_qty(qty), unit, name) if p and p != "0"]
    return " ".join(parts)


def _join_items(items: Sequence[Mapping[str, Any]], lang: str, budget: int, more_word: str) -> str:
    """Join item phrases, dropping the tail (``'+3 more'``) until they fit ``budget``."""
    phrases = [_item_phrase(item, lang) for item in items if item]
    if not phrases:
        return ""
    for keep in range(len(phrases), 0, -1):
        shown = phrases[:keep]
        dropped = len(phrases) - keep
        text = ", ".join(shown)
        if dropped:
            text = f"{text} +{dropped} {more_word}"
        if len(text) <= budget:
            return text
    return _fit(phrases[0], budget)


# ─────────────────────────────────────────────────────────────────────────────
# Templates — one function per action type
# ─────────────────────────────────────────────────────────────────────────────


def winback_offer(
    customer_name: str,
    shop_name: str,
    discount_label: str | Mapping[str, str],
    valid_days: int,
    last_visit_days: int,
) -> Bilingual:
    """Win-back offer for a customer who has gone quiet.

    Args:
        customer_name: How the merchant addresses them.
        shop_name: The shop, so the message is never anonymous.
        discount_label: The offer as the merchant phrased it (``"10% off"``), plain or bilingual.
        valid_days: Days the offer stays open.
        last_visit_days: Days since the customer last bought something.
    """
    name = customer_name.strip() or "जी"
    shop = shop_name.strip()
    valid = max(1, int(valid_days))
    since = max(0, int(last_visit_days))
    offer_hi = _pick(discount_label, "hi", default="खास छूट")
    offer_en = _pick(discount_label, "en", default="a special discount")
    hi = (
        f"नमस्ते {name} जी! {shop} से मुंशीजी। {since} दिन से आप नहीं आए — "
        f"याद आ रही है। इस बार आइए तो {offer_hi} "
        f"आपके लिए, {valid} दिन तक। इंतज़ार रहेगा 🙏"
    )
    en = (
        f"Namaste {name} ji! {shop} here. It's been {since} days since your last visit "
        f"and we've missed you. Come by and {offer_en} "
        f"is yours, valid {valid} days. See you soon 🙏"
    )
    return {"hi": _fit(hi), "en": _fit(en)}


def udhaar_reminder(
    customer_name: str,
    shop_name: str,
    amount_paise: int,
    days_overdue: int,
    tone: Tone | str,
) -> Bilingual:
    """Khata (credit) reminder in one of the three permitted registers.

    Args:
        customer_name: The customer.
        shop_name: The shop.
        amount_paise: Outstanding balance, in paise.
        days_overdue: Whole days the balance has been open.
        tone: ``GENTLE`` | ``STANDARD`` | ``FIRM`` — usually from :func:`select_tone`.

    Every tier is polite and non-threatening; see the module docstring for why.
    """
    name = customer_name.strip() or "जी"
    shop = shop_name.strip()
    amount = fmt_inr(amount_paise)
    days = max(0, int(days_overdue))
    resolved = tone if isinstance(tone, Tone) else Tone(str(tone).strip().lower())

    if resolved is Tone.GENTLE:
        # Soft: no deadline at all, the balance is mentioned almost apologetically.
        hi = (
            f"नमस्ते {name} जी, {shop} से। छोटी सी याद-दिलाई — खाते में {amount} "
            f"बाकी है, {days} दिन हो गए। जब सुविधा हो तब दे दीजिएगा, कोई जल्दी नहीं। "
            f"धन्यवाद 🙏"
        )
        en = (
            f"Namaste {name} ji, {shop} here. Just a gentle note — {amount} is still open "
            f"on your khata ({days} days). Whenever it suits you is perfectly fine. Thank you 🙏"
        )
    elif resolved is Tone.STANDARD:
        # Plain: states the fact, asks for this week, offers the easy rail.
        hi = (
            f"नमस्ते {name} जी, {shop} से। आपका {amount} का उधार {days} दिन से बाकी है। "
            f"इस हफ़्ते निपटा दीजिए तो अच्छा रहेगा। UPI से भी भेज सकते हैं। धन्यवाद।"
        )
        en = (
            f"Namaste {name} ji, {shop} here. {amount} on your khata has been pending "
            f"{days} days. Could you please settle it this week? UPI works too. Thank you."
        )
    else:
        # Direct: an unambiguous ask, and an open door — never a consequence.
        hi = (
            f"नमस्ते {name} जी, {shop} से। आपका {amount} का उधार {days} दिन से बाकी है। "
            f"कृपया इस हफ़्ते तक settle कर दीजिए। UPI या नकद, जैसा ठीक लगे। "
            f"कोई दिक्कत हो तो बता दीजिए, मिलकर रास्ता निकाल लेंगे।"
        )
        en = (
            f"Namaste {name} ji, {shop} here. {amount} on your khata is {days} days old now. "
            f"Please settle it by the end of this week — UPI or cash, whichever is easier. "
            f"If something is holding it up, tell us and we'll sort it out together."
        )
    return {"hi": _fit(hi), "en": _fit(en)}


def payment_link(
    customer_name: str,
    shop_name: str,
    amount_paise: int,
    link: str,
) -> Bilingual:
    """Hand a customer a one-tap way to pay.

    Args:
        amount_paise: Amount being collected, in paise.
        link: The payment URL.
    """
    name = customer_name.strip() or "जी"
    shop = shop_name.strip()
    amount = fmt_inr(amount_paise)
    url = link.strip()
    hi = (
        f"नमस्ते {name} जी, {shop} से। {amount} का पेमेंट लिंक यह रहा: {url} — "
        f"UPI, कार्ड या वॉलेट, जो ठीक लगे। पैसे आते ही रसीद मिल जाएगी। धन्यवाद 🙏"
    )
    en = (
        f"Namaste {name} ji, {shop} here. Your payment link for {amount}: {url} — "
        f"pay by UPI, card or wallet. The receipt reaches you the moment it's done. Thank you 🙏"
    )
    return {"hi": _fit(hi), "en": _fit(en)}


def restock_order(shop_name: str, items: Sequence[Mapping[str, Any]]) -> Bilingual:
    """Supplier-facing restock request. English is the primary register here.

    Args:
        shop_name: The ordering shop.
        items: ``[{"name": "Toor Dal", "name_hi": "तूर दाल", "qty": 12, "unit": "kg"}, …]``.
            The list is trimmed with a ``+N more`` tail if it would breach the SMS ceiling.
    """
    shop = shop_name.strip()
    en_head = f"Restock order from {shop}: "
    en_tail = ". Please confirm availability and a delivery date. Thank you."
    hi_head = f"{shop} की तरफ़ से ऑर्डर: "
    hi_tail = "। उपलब्धता और डिलीवरी की तारीख़ बता दीजिए। धन्यवाद।"

    en_items = _join_items(items, "en", MAX_MESSAGE_CHARS - len(en_head) - len(en_tail), "more")
    hi_items = _join_items(items, "hi", MAX_MESSAGE_CHARS - len(hi_head) - len(hi_tail), "और")

    if not en_items:
        return {
            "hi": _fit(f"{shop} की तरफ़ से: ऑर्डर की लिस्ट अभी तैयार नहीं है।"),
            "en": _fit(f"Restock order from {shop}: no items listed yet."),
        }
    return {"hi": _fit(hi_head + hi_items + hi_tail), "en": _fit(en_head + en_items + en_tail)}


def followup_note(text: str, when: str | Mapping[str, str]) -> Bilingual:
    """A reminder MunshiJi will read back to the *merchant* later.

    Args:
        text: What to be reminded about, in the merchant's own words.
        when: Display label for the moment, e.g. ``"kal shaam 6 baje"``. Plain or bilingual.
            Kept as a string on purpose — this module never touches the clock.
    """
    when_hi = _pick(when, "hi", default="बाद में")
    when_en = _pick(when, "en", default="later")
    body = text.strip() or "—"
    hi_head = f"मुंशीजी की याद-दिलाई ({when_hi}): "
    en_head = f"MunshiJi reminder ({when_en}): "
    return {
        "hi": _fit(hi_head + _fit(body, MAX_MESSAGE_CHARS - len(hi_head))),
        "en": _fit(en_head + _fit(body, MAX_MESSAGE_CHARS - len(en_head))),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch-time rendering
# ─────────────────────────────────────────────────────────────────────────────


def _render_winback(shop: str, params: Mapping[str, Any], target: Mapping[str, Any]) -> Bilingual:
    return winback_offer(
        customer_name=str(target.get("name") or ""),
        shop_name=shop,
        discount_label=params.get("discount_label") or params.get("offer") or "10% off",
        valid_days=int(params.get("valid_days") or 7),
        last_visit_days=int(target.get("last_visit_days") or params.get("last_visit_days") or 0),
    )


def _render_reminder(shop: str, params: Mapping[str, Any], target: Mapping[str, Any]) -> Bilingual:
    amount = int(target.get("amount_paise") or params.get("amount_paise") or 0)
    days = int(target.get("days_overdue") or params.get("days_overdue") or 0)
    tone = params.get("tone") or target.get("tone")
    if tone is None:
        tone = select_tone(days, float(target.get("reliability_score") or 0.5), amount)
    return udhaar_reminder(
        customer_name=str(target.get("name") or ""),
        shop_name=shop,
        amount_paise=amount,
        days_overdue=days,
        tone=tone,
    )


def _render_payment(shop: str, params: Mapping[str, Any], target: Mapping[str, Any]) -> Bilingual:
    return payment_link(
        customer_name=str(target.get("name") or ""),
        shop_name=shop,
        amount_paise=int(target.get("amount_paise") or params.get("amount_paise") or 0),
        link=str(target.get("link") or params.get("link") or ""),
    )


def _render_restock(shop: str, params: Mapping[str, Any], _target: Mapping[str, Any]) -> Bilingual:
    items = params.get("items") or []
    return restock_order(shop_name=shop, items=list(items))


def _render_followup(shop: str, params: Mapping[str, Any], _target: Mapping[str, Any]) -> Bilingual:
    del shop
    return followup_note(
        text=str(params.get("text") or params.get("note") or ""),
        when=params.get("when_display") or params.get("when") or "",
    )


#: Tool name → renderer. Mirrors the write tools in SPEC.md §9.
MESSAGE_TOOLS: dict[str, Callable[[str, Mapping[str, Any], Mapping[str, Any]], Bilingual]] = {
    "send_winback_offer": _render_winback,
    "send_udhaar_reminder": _render_reminder,
    "create_payment_link": _render_payment,
    "draft_restock_order": _render_restock,
    "schedule_followup": _render_followup,
}


def render_for_tool(
    tool_name: str,
    *,
    shop_name: str,
    params: Mapping[str, Any] | None = None,
    target: Mapping[str, Any] | None = None,
) -> Bilingual:
    """Render the outbound text for one ``(tool, target)`` pair.

    The action providers call this when the agent handed them a list of recipients rather than
    pre-rendered copy — personalised text cannot be rendered once for a whole audience. Still
    pure: everything it needs arrives in ``params`` and ``target``.

    Unknown tools render an empty pair rather than raising; the provider decides what that means.
    """
    renderer = MESSAGE_TOOLS.get(tool_name)
    if renderer is None:
        return {"hi": "", "en": ""}
    return renderer(shop_name.strip(), params or {}, target or {})
