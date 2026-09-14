"""Deterministic Hindi / Hinglish / English intent and slot parser.

Indian merchants do not pick a language and stay in it. The same sentence arrives as
``"आज कितना आया?"``, ``"aaj ka dhandha kaisa raha?"`` or ``"what's today's collection?"`` — often
mixed inside one utterance. This module turns any of those into a typed :class:`Intent` with
slots, **without an LLM**: it is pure, fast (microseconds), fully offline and unit-testable, which
is exactly what ``LocalLLM`` needs to be genuinely useful rather than fake (SPEC.md §2.2).

How it works:

1. :func:`normalise` folds an utterance into a comparable form — Devanagari is kept (with light
   vowel/nukta folding), Latin is lowercased and folded through common romanisation variants
   (``dhandha`` / ``dhandaa`` / ``dhanda`` all become ``danda``).
2. Every intent owns a lexicon of weighted surface forms in all three scripts. Forms are matched
   as whole words; a shorter form fully covered by a longer one does not score twice.
3. Scores are summed, diluted by utterance length, and squashed into a calibrated confidence with
   a margin term. Below :data:`INTENT_THRESHOLD` the result is ``unknown``.
4. Slots (money, percentages, counts, day references, tone) are extracted by regex from the same
   normalised text.

Nothing here touches the database, the network or the clock.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "INTENT_NAMES",
    "INTENT_THRESHOLD",
    "Intent",
    "detect_affirmation",
    "detect_intent",
    "detect_language",
    "extract_slots",
    "normalise",
]

# ── Script ranges ───────────────────────────────────────────────────────────────────────────
# Written as escapes, not literals: most code points below are combining or invisible, and
# a literal form would be unreadable in review and easy to corrupt in an editor.
#: The Devanagari block. Python's ``\w`` does *not* include Devanagari vowel signs (category Mc),
#: so every pattern in this module names the range explicitly instead of relying on ``\w``/``\b``.
DEVANAGARI = "\u0900-\u097f"
_DEVA_RE = re.compile(f"[{DEVANAGARI}]")
#: Devanagari letters (independent vowels + consonants). Used as a word boundary: a matra may
#: follow a matched form, a fresh letter may not ("आज" must not fire inside "आजकल").
_DEVA_LETTERS = "\u0904-\u0939\u0958-\u0961"

_ZERO_WIDTH = "\u200c\u200d"  # ZWNJ, ZWJ
_NUKTA = "\u093c"

#: Devanagari digits → ASCII, so "50" written either way extracts identically.
_DIGIT_MAP = str.maketrans(
    "\u0966\u0967\u0968\u0969\u096a\u096b\u096c\u096d\u096e\u096f",
    "0123456789",
)

#: Length/nasal folds so "karun" spelt with either vowel sign, or either nasal mark, compares equal.
_DEVA_FOLD = str.maketrans(
    {
        "\u0940": "\u093f",  # vowel sign II → vowel sign I
        "\u0942": "\u0941",  # vowel sign UU → vowel sign U
        "\u0908": "\u0907",  # letter II     → letter I
        "\u090a": "\u0909",  # letter UU     → letter U
        "\u0901": "\u0902",  # candrabindu   → anusvara
    }
)

_APOSTROPHES = ("'", "\u2019", "\u02bc")
_PUNCT_RE = re.compile(f"[^\\w\\s{DEVANAGARI}]|_")
_SPACE_RE = re.compile(r"\s+")

# ── Romanisation folding ────────────────────────────────────────────────────────────────────
# Applied in order. Deliberately conservative: "ee" is left alone so "teen" (3) never collapses
# into "ten" (10), and only the eight genuinely aspirated digraphs lose their "h".
_LATIN_FOLDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"z"), "j"),  # zyada / jyada
    (re.compile(r"w"), "v"),  # wapas / vapas
    (re.compile(r"q"), "k"),  # qeemat / keemat
    (re.compile(r"aa+"), "a"),  # dhandaa → dhanda
    (re.compile(r"ii+"), "i"),
    (re.compile(r"uu+"), "u"),
    (re.compile(r"oo+"), "o"),  # bhejoo → bhejo
    (re.compile(r"([bcdfghjklmnpqrstvxy])\1+"), r"\1"),  # pichhli → pichli, offer → ofer
    (re.compile(r"([bcdgjkpst])h"), r"\1"),  # dhandha → danda, khata → kata
)


def normalise(text: str) -> str:
    """Fold an utterance into the comparable form every lexicon entry is stored in.

    Lowercases Latin, strips punctuation, collapses whitespace, converts Devanagari
    digits to ASCII, folds Devanagari length/nasal variants, and folds common Hinglish
    romanisation variants (``dhanda`` / ``dhandha`` / ``dhandaa`` → ``danda``).
    """
    if not text:
        return ""
    # Decompose, drop nukta and zero-width joiners, recompose: "ज़्यादा" → "ज्यादा".
    folded = unicodedata.normalize("NFKD", text)
    for char in (_NUKTA, *_ZERO_WIDTH):
        folded = folded.replace(char, "")
    folded = unicodedata.normalize("NFC", folded)
    folded = folded.translate(_DIGIT_MAP).translate(_DEVA_FOLD).lower()

    for apostrophe in _APOSTROPHES:
        folded = folded.replace(apostrophe, "")
    folded = folded.replace("₹", " rs ").replace("%", " percent ")
    folded = _PUNCT_RE.sub(" ", folded)

    for pattern, replacement in _LATIN_FOLDS:
        folded = pattern.sub(replacement, folded)
    return _SPACE_RE.sub(" ", folded).strip()


def detect_language(text: str) -> str:
    """``"hi-IN"`` when the utterance contains Devanagari, else ``"en-IN"``.

    Romanised Hinglish reads as ``en-IN`` here on purpose: it is Latin script, and that is
    what the browser's speech stack needs to know. Spoken *register* is decided separately.
    """
    return "hi-IN" if _DEVA_RE.search(text or "") else "en-IN"


# ── Surface-form matching ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Form:
    """One compiled surface form: how it was authored, how it matches, what it is worth."""

    surface: str
    normalised: str
    pattern: re.Pattern[str]
    weight: float


#: Inside a lexicon form, a bare ``0`` stands for "any number here" — it survives
#: :func:`normalise` (unlike ``#``) and never occurs as a real word. So ``"top 0 customers"``
#: matches "top 5 customers" and "top 10 grahak"; spelled-out numbers have their own forms.
_NUMBER_SLOT = "0"


def _form_pattern(normalised_form: str) -> re.Pattern[str]:
    """Whole-word matcher for a normalised form, script-aware.

    Devanagari is agglutinative in practice, so a matra may follow a match but a fresh
    letter may not. Latin forms require a hard boundary on both sides.
    """
    body = r"\s+".join(
        r"\d+" if token == _NUMBER_SLOT else re.escape(token) for token in normalised_form.split()
    )
    if _DEVA_RE.search(normalised_form):
        return re.compile(f"(?<![{DEVANAGARI}]){body}(?![{_DEVA_LETTERS}])")
    return re.compile(f"(?<![0-9a-z{DEVANAGARI}]){body}(?![0-9a-z{DEVANAGARI}])")


def _compile_forms(entries: tuple[tuple[str, float], ...]) -> tuple[_Form, ...]:
    """Normalise, de-duplicate (keeping the highest weight) and sort longest-first."""
    best: dict[str, _Form] = {}
    for surface, weight in entries:
        key = normalise(surface)
        if not key:
            continue
        existing = best.get(key)
        if existing is None or weight > existing.weight:
            best[key] = _Form(surface, key, _form_pattern(key), weight)
    return tuple(sorted(best.values(), key=lambda form: len(form.normalised), reverse=True))


# ── Intent lexicon ──────────────────────────────────────────────────────────────────────────
# Weights:  1.0 = the phrase *is* the intent   0.8 = distinctive keyword
#           0.5 = supporting keyword           0.3 = weak hint (never fires alone)

_LEXICON: dict[str, tuple[tuple[str, float], ...]] = {
    "sales_summary": (
        ("aaj ka dhandha", 1.0),
        ("aaj ka dhanda", 1.0),
        ("dhandha kaisa", 1.0),
        ("dhanda kaisa", 1.0),
        ("dhandha kaisa raha", 1.0),
        ("aaj kitna aaya", 1.0),
        ("kitna aaya", 1.0),
        ("kitna kamaya", 1.0),
        ("kitne kamaye", 1.0),
        ("kitna kamaye", 1.0),
        ("aaj ki bikri", 1.0),
        ("kitni bikri", 1.0),
        ("aaj ka collection", 1.0),
        ("aaj ka hisab", 1.0),
        ("kitna paisa aaya", 1.0),
        ("kitne ka becha", 1.0),
        ("kitna business hua", 1.0),
        ("total kitna", 0.8),
        ("kul kitna", 0.8),
        ("dhandha", 0.8),
        ("dhanda", 0.8),
        ("bikri", 0.8),
        ("kamai", 0.8),
        ("collection", 0.8),
        ("galla", 0.5),
        ("gallah", 0.5),
        ("golak", 0.5),
        ("hisab", 0.5),
        ("sale", 0.5),
        ("kitna hua", 0.5),
        ("आज कितना", 1.0),
        ("कितना आया", 1.0),
        ("कितना कमाया", 1.0),
        ("आज का धंधा", 1.0),
        ("आज की बिक्री", 1.0),
        ("आज का हिसाब", 1.0),
        ("कितना पैसा आया", 1.0),
        ("कुल कितना", 0.8),
        ("धंधा", 0.8),
        ("बिक्री", 0.8),
        ("कमाई", 0.8),
        ("कलेक्शन", 0.8),
        ("गल्ला", 0.5),
        ("todays sales", 1.0),
        ("sales today", 1.0),
        ("todays collection", 1.0),
        ("todays revenue", 1.0),
        ("revenue today", 1.0),
        ("total sales", 1.0),
        ("daily sales", 1.0),
        ("how much did i make", 1.0),
        ("how much did we make", 1.0),
        ("how much have i made", 1.0),
        ("how much have we sold", 1.0),
        ("how is business", 1.0),
        ("hows business", 1.0),
        ("business today", 0.8),
        ("takings", 0.5),
        ("turnover", 0.5),
    ),
    "compare_sales": (
        ("pichle hafte se", 1.0),
        ("pichhle hafte se", 1.0),
        ("pichle hafte ke mukable", 1.0),
        ("pichle mahine se", 1.0),
        ("pichle saal se", 1.0),
        ("kal se zyada", 1.0),
        ("kal se kam", 1.0),
        ("kal ke mukable", 1.0),
        ("kam hua ya zyada", 1.0),
        ("zyada hua ya kam", 1.0),
        ("mukable", 0.8),
        ("mukabla", 0.8),
        ("tulna", 0.8),
        ("compare", 0.8),
        ("comparison", 0.8),
        ("se behtar", 0.8),
        ("se zyada", 0.5),
        ("se kam", 0.5),
        ("kal se", 0.5),
        ("पिछले हफ्ते से", 1.0),
        ("पिछले महीने से", 1.0),
        ("कल से ज्यादा", 1.0),
        ("कल से कम", 1.0),
        ("के मुकाबले", 1.0),
        ("मुकाबले", 0.8),
        ("तुलना", 0.8),
        ("last week comparison", 1.0),
        ("compared to last week", 1.0),
        ("compared to", 1.0),
        ("versus last week", 1.0),
        ("than last week", 1.0),
        ("than yesterday", 1.0),
        ("week over week", 1.0),
        ("vs last week", 1.0),
        ("versus", 0.5),
        ("difference", 0.3),
    ),
    "top_customers": (
        ("sabse zyada kaun", 1.0),
        ("kaun sabse zyada", 1.0),
        ("sabse zyada kisne", 1.0),
        ("sabse zyada kharidta", 1.0),
        ("sabse zyada kharidte", 1.0),
        ("top grahak", 1.0),
        ("top customer", 1.0),
        ("top customers", 1.0),
        ("best customer", 1.0),
        ("best customers", 1.0),
        ("best buyers", 1.0),
        ("sabse achhe customer", 1.0),
        ("sabse bade customer", 1.0),
        ("sabse bade grahak", 1.0),
        ("biggest customers", 1.0),
        ("who spends the most", 1.0),
        ("my best customers", 1.0),
        ("vip customer", 0.8),
        ("top spenders", 1.0),
        ("top 0 customers", 1.0),
        ("top 0 grahak", 1.0),
        ("best 0 customers", 1.0),
        ("sabse zyada 0 customers", 1.0),
        ("pehle 0 customers", 1.0),
        ("सबसे ज्यादा कौन", 1.0),
        ("कौन सबसे ज्यादा", 1.0),
        ("टॉप ग्राहक", 1.0),
        ("सबसे बड़े ग्राहक", 1.0),
        ("सबसे अच्छे ग्राहक", 1.0),
        ("सबसे ज्यादा खरीदते", 1.0),
    ),
    "dormant_customers": (
        ("kaun nahi aaya", 1.0),
        ("kaun nahi aa raha", 1.0),
        ("kaun nahi aa rahe", 1.0),
        ("nahi aa raha", 1.0),
        ("nahi aa rahe", 1.0),
        ("nahi aa rahe hain", 1.0),
        ("purane customer", 1.0),
        ("purane grahak", 1.0),
        ("purane customers", 1.0),
        ("chhut gaye", 1.0),
        ("kho gaye", 1.0),
        ("aana band", 1.0),
        ("band kar diya aana", 1.0),
        ("nahi dikh rahe", 1.0),
        ("nahi dikhe", 1.0),
        ("gayab", 0.8),
        ("dormant", 0.8),
        ("lapsed", 0.8),
        ("कौन नहीं आया", 1.0),
        ("कौन नहीं आ रहा", 1.0),
        ("नहीं आ रहे", 1.0),
        ("पुराने ग्राहक", 1.0),
        ("गायब", 0.8),
        ("छूट गए", 1.0),
        ("regulars missing", 1.0),
        ("missing customers", 1.0),
        ("who stopped coming", 1.0),
        ("stopped coming", 1.0),
        ("stopped visiting", 1.0),
        ("havent come", 1.0),
        ("havent visited", 1.0),
        ("inactive customers", 1.0),
        ("customers we lost", 1.0),
        ("churn", 0.5),
    ),
    "inventory_alerts": (
        ("saman khatam", 1.0),
        ("samaan khatam", 1.0),
        ("saman khatm", 1.0),
        ("maal khatam", 1.0),
        ("kya mangwana hai", 1.0),
        ("kya mangwana", 1.0),
        ("kya mangana hai", 1.0),
        ("kya khatam ho gaya", 1.0),
        ("khatam ho gaya", 1.0),
        ("khatm ho raha", 1.0),
        ("kitna stock", 1.0),
        ("stock kitna", 1.0),
        ("stock alert", 1.0),
        ("stock khatam", 1.0),
        ("stock", 0.8),
        ("restock", 0.8),
        ("inventory", 0.8),
        ("godown", 0.5),
        ("maal", 0.5),
        ("dead stock", 1.0),
        ("स्टॉक", 0.8),
        ("सामान खत्म", 1.0),
        ("माल खत्म", 1.0),
        ("क्या मंगवाना", 1.0),
        ("कितना स्टॉक", 1.0),
        ("रीस्टॉक", 0.8),
        ("what to order", 1.0),
        ("what should i order", 1.0),
        ("running low", 1.0),
        ("out of stock", 1.0),
        ("low stock", 1.0),
        ("reorder", 0.8),
        ("inventory alerts", 1.0),
        ("stock levels", 1.0),
    ),
    "udhaar_summary": (
        ("kitna baki hai", 1.0),
        ("kitna baki", 1.0),
        ("udhaar kitna", 1.0),
        ("kitna udhaar", 1.0),
        ("udhaar khata", 1.0),
        ("bahi khata", 1.0),
        ("udhaar wale", 1.0),
        ("kisko dena hai", 1.0),
        ("kisne paisa dena hai", 1.0),
        ("kisne paise dene hain", 1.0),
        ("udhaar", 0.8),
        ("udhaari", 0.8),
        ("baki", 0.8),
        ("baqi", 0.8),
        ("khata", 0.8),
        ("कितना बाकी", 1.0),
        ("कितना उधार", 1.0),
        ("बही खाता", 1.0),
        ("किसने पैसे देने", 1.0),
        ("उधार", 0.8),
        ("उधारी", 0.8),
        ("बाकी", 0.8),
        ("खाता", 0.8),
        ("who owes me", 1.0),
        ("who owes money", 1.0),
        ("how much is owed", 1.0),
        ("outstanding credit", 1.0),
        ("pending payments", 1.0),
        ("receivables", 0.8),
        ("outstanding", 0.8),
        ("dues", 0.8),
    ),
    "merchant_health": (
        ("dukaan ki sehat", 1.0),
        ("dukan ki sehat", 1.0),
        ("dukaan kaisi chal rahi", 1.0),
        ("dukan kaisi chal rahi", 1.0),
        ("business kaisa chal raha", 1.0),
        ("shop health", 1.0),
        ("health score", 1.0),
        ("merchant health", 1.0),
        ("loan mil sakta", 1.0),
        ("loan milega", 1.0),
        ("karza mil sakta", 1.0),
        ("credit score", 1.0),
        ("bank ko kaisa dikhta", 1.0),
        ("overall kaisa hai", 1.0),
        ("dukaan ka score", 1.0),
        ("दुकान की सेहत", 1.0),
        ("दुकान कैसी चल", 1.0),
        ("लोन मिल", 1.0),
        ("कर्ज़ा मिल", 1.0),
        ("सेहत", 0.7),
        ("sehat", 0.7),
        ("loan", 0.7),
    ),
    "insights": (
        ("kya karun", 1.0),
        ("kya karoon", 1.0),
        ("main kya karun", 1.0),
        ("aaj kya karna chahiye", 1.0),
        ("kya karna chahiye", 1.0),
        ("suggestion do", 1.0),
        ("koi suggestion", 1.0),
        ("kya suggest", 1.0),
        ("salah do", 1.0),
        ("koi salah", 1.0),
        ("koi sujhav", 1.0),
        ("koi dikkat", 1.0),
        ("kya dhyan dena", 1.0),
        ("business kaise badhe", 1.0),
        ("kaise badhau", 1.0),
        ("kuch batao", 0.8),
        ("salah", 0.5),
        ("sujhav", 0.8),
        ("advice", 0.8),
        ("क्या करूं", 1.0),
        ("क्या करना चाहिए", 1.0),
        ("कोई सलाह", 1.0),
        ("कोई सुझाव", 1.0),
        ("कोई दिक्कत", 1.0),
        ("क्या ध्यान", 1.0),
        ("सलाह", 0.5),
        ("सुझाव", 0.8),
        ("what should i do", 1.0),
        ("what do you suggest", 1.0),
        ("any suggestions", 1.0),
        ("any recommendations", 1.0),
        ("give me advice", 1.0),
        ("what needs attention", 1.0),
        ("anything i should know", 1.0),
        ("any issues", 1.0),
        ("whats wrong", 1.0),
        ("insights", 0.8),
    ),
    "recall_memory": (
        ("pichli baar", 1.0),
        ("pichhli baar", 1.0),
        ("pichle baar", 1.0),
        ("last baar", 1.0),
        ("uska kya hua", 1.0),
        ("us ka kya hua", 1.0),
        ("kya hua tha", 1.0),
        ("kya hua uska", 1.0),
        ("jo bheja tha", 1.0),
        ("jo offer bheja tha", 1.0),
        ("jo reminder bheja tha", 1.0),
        ("pehle jo", 1.0),
        ("pahle jo", 1.0),
        ("yaad hai", 0.8),
        ("us din", 0.8),
        ("पिछली बार", 1.0),
        ("पिछले बार", 1.0),
        ("उसका क्या हुआ", 1.0),
        ("क्या हुआ था", 1.0),
        ("जो भेजा था", 1.0),
        ("याद है", 0.8),
        ("last time", 1.0),
        ("what happened to", 1.0),
        ("what happened with", 1.0),
        ("the offer i sent", 1.0),
        ("did it work", 1.0),
        ("how did it go", 1.0),
        ("previously", 0.8),
        ("earlier you", 1.0),
        ("do you remember", 1.0),
        ("recall", 0.5),
    ),
    "send_offer": (
        ("offer bhejo", 1.0),
        ("offer bhej do", 1.0),
        ("offer bhej de", 1.0),
        ("offer bhej dijiye", 1.0),
        ("bhej do offer", 1.0),
        ("discount bhejo", 1.0),
        ("discount bhej do", 1.0),
        ("discount de do", 1.0),
        ("chhut do", 1.0),
        ("coupon bhejo", 1.0),
        ("promo bhejo", 1.0),
        ("wapas bulao", 1.0),
        ("unhe wapas bulao", 1.0),
        ("offer send karo", 1.0),
        ("winback", 1.0),
        ("win back", 1.0),
        ("ऑफर भेजो", 1.0),
        ("ऑफर भेज दो", 1.0),
        ("डिस्काउंट भेज दो", 1.0),
        ("छूट दो", 1.0),
        ("वापस बुलाओ", 1.0),
        ("send an offer", 1.0),
        ("send them an offer", 1.0),
        ("send a discount", 1.0),
        ("send the discount", 1.0),
        ("give them a discount", 1.0),
        ("win them back", 1.0),
        ("offer", 0.3),
        ("discount", 0.3),
    ),
    "send_reminder": (
        ("reminder bhejo", 1.0),
        ("reminder bhej do", 1.0),
        ("yaad dila do", 1.0),
        ("yaad dilao", 1.0),
        ("yaad dila dijiye", 1.0),
        ("udhaar maango", 1.0),
        ("udhaar mango", 1.0),
        ("paise maango", 1.0),
        ("paisa mango", 1.0),
        ("paise wapas mango", 1.0),
        ("taqaza karo", 1.0),
        ("takaza karo", 1.0),
        ("रिमाइंडर भेजो", 1.0),
        ("रिमाइंडर भेज दो", 1.0),
        ("याद दिला दो", 1.0),
        ("याद दिलाओ", 1.0),
        ("उधार मांगो", 1.0),
        ("पैसे मांगो", 1.0),
        ("send a reminder", 1.0),
        ("send reminders", 1.0),
        ("send the reminder", 1.0),
        ("remind them", 1.0),
        ("remind the customers", 1.0),
        ("chase the payment", 1.0),
        ("payment reminder", 1.0),
        ("ask for the money", 1.0),
        ("follow up on udhaar", 1.0),
    ),
    "restock": (
        ("order kar do", 1.0),
        ("order karo", 1.0),
        ("order de do", 1.0),
        ("order laga do", 1.0),
        ("order kar dijiye", 1.0),
        ("mangwa do", 1.0),
        ("manga do", 1.0),
        ("stock mangwa do", 1.0),
        ("supplier ko bolo", 1.0),
        ("supplier ko bol do", 1.0),
        ("ऑर्डर कर दो", 1.0),
        ("ऑर्डर लगा दो", 1.0),
        ("मंगवा दो", 1.0),
        ("सप्लायर को बोलो", 1.0),
        ("place the order", 1.0),
        ("place an order", 1.0),
        ("draft the order", 1.0),
        ("order it", 1.0),
        ("reorder it", 1.0),
    ),
    "greeting": (
        ("namaste", 1.0),
        ("namaskar", 1.0),
        ("ram ram", 1.0),
        ("salaam", 0.8),
        ("kaise ho", 0.8),
        ("kya haal", 0.8),
        ("munshi ji", 0.8),
        ("munshiji", 0.8),
        ("नमस्ते", 1.0),
        ("नमस्कार", 1.0),
        ("राम राम", 1.0),
        ("कैसे हो", 0.8),
        ("मुंशी जी", 0.8),
        ("hello", 0.8),
        ("hey there", 0.8),
        ("good morning", 0.8),
        ("good evening", 0.8),
        ("hi", 0.5),
        ("hey", 0.5),
    ),
    "thanks": (
        ("shukriya", 1.0),
        ("dhanyavaad", 1.0),
        ("dhanyawad", 1.0),
        ("bahut badhiya", 1.0),
        ("shabash", 0.8),
        ("badhiya", 0.5),
        ("शुक्रिया", 1.0),
        ("धन्यवाद", 1.0),
        ("बहुत बढ़िया", 1.0),
        ("शाबाश", 0.8),
        ("thank you", 1.0),
        ("thanks", 1.0),
        ("well done", 0.8),
        ("perfect", 0.5),
    ),
}

#: Every intent this parser can return. ``unknown`` is the fallback, not a lexicon entry.
INTENT_NAMES: tuple[str, ...] = (*_LEXICON.keys(), "unknown")

#: Tiebreak when two intents score identically: act > recall > read > social.
_PRIORITY: dict[str, int] = {
    "send_offer": 9,
    "send_reminder": 9,
    "restock": 9,
    "recall_memory": 8,
    "compare_sales": 7,
    "dormant_customers": 7,
    "udhaar_summary": 6,
    "inventory_alerts": 6,
    "top_customers": 6,
    "sales_summary": 5,
    "insights": 4,
    "thanks": 2,
    "greeting": 1,
}

_COMPILED: dict[str, tuple[_Form, ...]] = {
    name: _compile_forms(entries) for name, entries in _LEXICON.items()
}

# ── Scoring calibration ─────────────────────────────────────────────────────────────────────
#: Below this confidence ``detect_intent`` returns ``unknown`` rather than guessing.
INTENT_THRESHOLD = 0.55
_SATURATION = 1.15  # how fast extra evidence stops helping
_RAW_CAP = 3.5  # a pile of matches is not more certain than three good ones
_FREE_TOKENS = 6  # utterances longer than this dilute each match
_DILUTION = 0.06


def _confidence(best: float, runner_up: float) -> float:
    """Squash a raw score into 0–1, rewarding a clear margin over the second-best intent."""
    base = 1.0 - math.exp(-_SATURATION * best)
    margin = 0.0 if best <= 0 else max(0.0, (best - runner_up) / best)
    return round(min(0.99, 0.15 + 0.75 * base + 0.12 * margin), 3)


def _score_intent(norm: str, forms: tuple[_Form, ...]) -> tuple[float, list[str]]:
    """Sum weights of matching forms, ignoring a form already covered by a longer match."""
    total = 0.0
    matched: list[str] = []
    covered: list[tuple[int, int]] = []
    for form in forms:  # longest first
        found = form.pattern.search(norm)
        if found is None:
            continue
        span = found.span()
        if any(start <= span[0] and span[1] <= end for start, end in covered):
            continue
        covered.append(span)
        total += form.weight
        matched.append(form.surface)
    return total, matched


# ── Slot extraction ─────────────────────────────────────────────────────────────────────────

# Indian number words, authored naturally and folded at import.
# "saath" (60) is deliberately absent: it folds onto "saat" (7) and merchants overwhelmingly
# say the digit for larger amounts anyway.
_NUMBER_WORDS_RAW: dict[str, int] = {
    "ek": 1, "do": 2, "teen": 3, "tin": 3, "char": 4, "chaar": 4,
    "panch": 5, "paanch": 5, "chhe": 6, "che": 6, "chah": 6,
    "saat": 7, "aath": 8, "nau": 9, "das": 10, "dus": 10,
    "gyarah": 11, "barah": 12, "terah": 13, "chaudah": 14, "pandrah": 15,
    "bees": 20, "bis": 20, "pachchis": 25, "pachis": 25, "tees": 30, "tis": 30,
    "chalis": 40, "chaalis": 40, "pachas": 50, "pachaas": 50, "panchas": 50,
    "sattar": 70, "assi": 80, "nabbe": 90, "sau": 100, "hazar": 1000, "hazaar": 1000,
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5, "छह": 6,
    "सात": 7, "आठ": 8, "नौ": 9, "दस": 10, "बीस": 20, "पचीस": 25, "तीस": 30,
    "चालीस": 40, "पचास": 50, "सत्तर": 70, "अस्सी": 80, "नब्बे": 90,
    "सौ": 100, "हजार": 1000,
}  # fmt: skip

NUMBER_WORDS: dict[str, int] = {}
for _word, _value in _NUMBER_WORDS_RAW.items():
    NUMBER_WORDS.setdefault(normalise(_word), _value)

_NUMBER_WORD_ALT = "|".join(sorted((re.escape(k) for k in NUMBER_WORDS), key=len, reverse=True))
#: Left boundary shared by every slot pattern — ``\b`` is unusable because Devanagari matras are
#: not word characters in Python's ``re``.
_LEFT = f"(?<![0-9a-z{DEVANAGARI}])"
# Longest alternative first so "rupees" is not consumed as "rupee".
_MONEY_UNIT = r"(?:rupees|rupaye|rupaya|rupay|rupee|rs|रुपये|रुपए|रुपय|रु)"
_PERCENT_UNIT = r"(?:percent|pct|fisadi|प्रतिशत|फिसदि)"
_PEOPLE_UNIT = (
    f"(?:customers?|clients?|grahak[a-z]*|log[a-z]*|banda[a-z]*|"
    f"ग्राहक[{DEVANAGARI}]*|लोग[{DEVANAGARI}]*)"
)

_MONEY_PATTERNS = (
    re.compile(rf"{_LEFT}{_MONEY_UNIT}\s*(\d+)"),
    re.compile(rf"{_LEFT}(\d+)\s*{_MONEY_UNIT}"),
    re.compile(rf"{_LEFT}(\d+)\s*ka\s+(?:ofer|discount|kupan|coupon)"),
)
_MONEY_WORD_PATTERNS = (
    re.compile(rf"{_LEFT}{_MONEY_UNIT}\s+({_NUMBER_WORD_ALT})"),
    re.compile(rf"{_LEFT}({_NUMBER_WORD_ALT})\s+{_MONEY_UNIT}"),
)
_PERCENT_PATTERNS = (
    re.compile(rf"{_LEFT}(\d+)\s*{_PERCENT_UNIT}"),
    re.compile(rf"{_LEFT}{_PERCENT_UNIT}\s*(\d+)"),
)
_PERCENT_WORD_PATTERNS = (re.compile(rf"{_LEFT}({_NUMBER_WORD_ALT})\s+{_PERCENT_UNIT}"),)
_COUNT_PATTERNS = (
    re.compile(rf"{_LEFT}(\d+)\s*{_PEOPLE_UNIT}"),
    re.compile(rf"{_LEFT}(?:top|pehle|pahle|sabse)\s*(\d+)"),
    re.compile(rf"{_LEFT}{_PEOPLE_UNIT}\s*(\d+)"),
)
_COUNT_WORD_PATTERNS = (re.compile(rf"{_LEFT}({_NUMBER_WORD_ALT})\s+{_PEOPLE_UNIT}"),)

#: Day / period references → canonical tokens the tools understand.
_PERIOD_FORMS_RAW: tuple[tuple[str, str], ...] = (
    ("pichle hafte", "last_week"),
    ("pichhle hafte", "last_week"),
    ("pichle hafta", "last_week"),
    ("last week", "last_week"),
    ("पिछले हफ्ते", "last_week"),
    ("is hafte", "this_week"),
    ("this week", "this_week"),
    ("इस हफ्ते", "this_week"),
    ("pichle mahine", "last_month"),
    ("last month", "last_month"),
    ("पिछले महीने", "last_month"),
    ("is mahine", "this_month"),
    ("this month", "this_month"),
    ("इस महीने", "this_month"),
    ("pichle saal", "last_year"),
    ("last year", "last_year"),
    ("पिछले साल", "last_year"),
    ("is saal", "this_year"),
    ("this year", "this_year"),
    ("parso", "day_before"),
    ("parson", "day_before"),
    ("परसों", "day_before"),
    ("aajkal", "recent"),
    ("aaj kal", "recent"),
    ("आजकल", "recent"),
    ("these days", "recent"),
    ("lately", "recent"),
    ("recently", "recent"),
    ("yesterday", "yesterday"),
    ("kal", "yesterday"),
    ("कल", "yesterday"),
    ("today", "today"),
    ("aaj", "today"),
    ("आज", "today"),
    ("abhi", "now"),
    ("अभी", "now"),
    ("right now", "now"),
)


def _compile_labelled(
    entries: tuple[tuple[str, str], ...],
) -> tuple[tuple[re.Pattern[str], str], ...]:
    """Compile ``(surface, label)`` pairs longest-first so specific phrases win."""
    compiled = [
        (key, _form_pattern(key), label)
        for surface, label in entries
        if (key := normalise(surface))
    ]
    compiled.sort(key=lambda item: len(item[0]), reverse=True)
    return tuple((pattern, label) for _, pattern, label in compiled)


_PERIOD_FORMS: tuple[tuple[re.Pattern[str], str], ...] = _compile_labelled(_PERIOD_FORMS_RAW)

_TONE_FORMS_RAW: tuple[tuple[str, str], ...] = (
    ("pyaar se", "gentle"),
    ("pyar se", "gentle"),
    ("narmi se", "gentle"),
    ("narmi", "gentle"),
    ("halke se", "gentle"),
    ("aram se", "gentle"),
    ("politely", "gentle"),
    ("gently", "gentle"),
    ("softly", "gentle"),
    ("प्यार से", "gentle"),
    ("नरमी से", "gentle"),
    ("धीरे से", "gentle"),
    ("sakhti se", "firm"),
    ("sakhti", "firm"),
    ("sakht", "firm"),
    ("zor se", "firm"),
    ("seedha", "firm"),
    ("strictly", "firm"),
    ("strict", "firm"),
    ("firmly", "firm"),
    ("firm", "firm"),
    ("सख्ती से", "firm"),
    ("सख्ती", "firm"),
    ("सीधे", "firm"),
    ("normal", "standard"),
    ("standard", "standard"),
    ("सामान्य", "standard"),
)
_TONE_FORMS: tuple[tuple[re.Pattern[str], str], ...] = _compile_labelled(_TONE_FORMS_RAW)

#: Intents that always carry a ``tone`` slot, defaulting to STANDARD (SPEC.md §2.4).
_TONE_INTENTS = frozenset({"send_reminder"})
#: When only one period is named in a comparison, this is what it is compared against.
_COMPARISON_ANCHOR = {
    "last_week": "this_week",
    "last_month": "this_month",
    "last_year": "this_year",
}


def _first_number(
    norm: str,
    digit_patterns: tuple[re.Pattern[str], ...],
    word_patterns: tuple[re.Pattern[str], ...],
) -> int | None:
    """First digit match wins; number words are only read next to an explicit unit."""
    for pattern in digit_patterns:
        found = pattern.search(norm)
        if found is not None:
            return int(found.group(1))
    for pattern in word_patterns:
        found = pattern.search(norm)
        if found is not None:
            return NUMBER_WORDS[found.group(1)]
    return None


def _periods_in_order(norm: str) -> list[str]:
    """Canonical period tokens in the order the merchant said them, without duplicates."""
    hits: list[tuple[int, str]] = []
    claimed: list[tuple[int, int]] = []
    for pattern, canonical in _PERIOD_FORMS:  # longest first
        for found in pattern.finditer(norm):
            span = found.span()
            if any(start <= span[0] and span[1] <= end for start, end in claimed):
                continue
            claimed.append(span)
            hits.append((span[0], canonical))
    ordered: list[str] = []
    for _, canonical in sorted(hits):
        if canonical not in ordered:
            ordered.append(canonical)
    return ordered


def extract_slots(text: str, intent_name: str = "unknown") -> dict[str, Any]:
    """Pull money, percentage, count, period and tone slots out of an utterance.

    Money is returned as **rupees** (``discount_amount_rupees``) because that is what
    merchants say out loud; the tool layer converts to paise. Percentages are whole numbers.
    """
    norm = normalise(text)
    slots: dict[str, Any] = {}
    if not norm:
        return slots

    percent = _first_number(norm, _PERCENT_PATTERNS, _PERCENT_WORD_PATTERNS)
    if percent is not None:
        slots["discount_pct"] = percent

    amount = _first_number(norm, _MONEY_PATTERNS, _MONEY_WORD_PATTERNS)
    if amount is not None:
        slots["discount_amount_rupees"] = amount

    count = _first_number(norm, _COUNT_PATTERNS, _COUNT_WORD_PATTERNS)
    if count is not None:
        slots["count"] = count

    periods = _periods_in_order(norm)
    if periods:
        slots["period"] = periods[0]
    if intent_name == "compare_sales":
        if len(periods) >= 2:
            slots["period_a"], slots["period_b"] = periods[0], periods[1]
        elif len(periods) == 1:
            baseline = periods[0]
            slots["period_a"] = _COMPARISON_ANCHOR.get(baseline, "today")
            slots["period_b"] = baseline
        else:
            slots["period_a"], slots["period_b"] = "today", "yesterday"

    for pattern, tone in _TONE_FORMS:
        if pattern.search(norm):
            slots["tone"] = tone
            break
    if intent_name in _TONE_INTENTS:
        slots.setdefault("tone", "standard")
    return slots


# ── Affirmation ─────────────────────────────────────────────────────────────────────────────

# Compact on purpose: these are word lists, and one word per line buries the pattern.
# fmt: off
_AFFIRMATIVE_RAW: tuple[str, ...] = (
    "haan", "han", "haa", "ha", "haanji", "ji", "ji haan", "jee", "yes", "yeah", "yep", "yup",
    "ok", "okay", "okey", "theek hai", "thik hai", "theek", "thik", "sahi hai", "sahi", "bilkul",
    "zaroor", "zarur", "jarur", "kar do", "kardo", "kar dijiye", "kar de", "bhej do", "bhejdo",
    "bhej dijiye", "bhejo", "chalo", "chaliye", "go ahead", "do it", "send it", "sure",
    "please do", "haan bhai", "hanji", "हाँ", "हां", "जी", "जी हाँ", "ठीक", "ठीक है", "सही है",
    "बिलकुल", "ज़रूर", "कर दो", "भेज दो", "भेजो", "चलो", "हाँ जी",
)
_NEGATIVE_RAW: tuple[str, ...] = (
    "nahi", "nahin", "naa", "na", "nako", "mat", "mat bhejo", "mat karo", "rehne do", "rahne do",
    "chhod do", "chod do", "chhodo", "ruko", "ruk jao", "abhi nahi", "baad mein", "baad me",
    "nahi chahiye", "zarurat nahi", "no", "nope", "not now", "dont", "do not", "cancel", "stop",
    "skip it", "leave it", "नहीं", "नही", "ना", "मत", "रहने दो", "छोड़ दो", "अभी नहीं", "बाद में",
    "रुको",
)
#: "kyun nahi" / "why not" is agreement wearing a negation's clothes.
_RHETORICAL_YES_RAW: tuple[str, ...] = (
    "kyun nahi", "kyon nahi", "kyu nahi", "why not", "क्यों नहीं", "क्यूं नहीं",
)

# fmt: on

_AFFIRMATIVE = _compile_forms(tuple((form, 1.0) for form in _AFFIRMATIVE_RAW))
_NEGATIVE = _compile_forms(tuple((form, 1.0) for form in _NEGATIVE_RAW))
_RHETORICAL_YES = tuple(
    _form_pattern(normalise(form)) for form in _RHETORICAL_YES_RAW if normalise(form)
)


def _earliest(norm: str, forms: tuple[_Form, ...]) -> int | None:
    positions = [found.start() for form in forms if (found := form.pattern.search(norm))]
    return min(positions) if positions else None


def detect_affirmation(text: str) -> bool | None:
    """``True`` for yes, ``False`` for no, ``None`` when the utterance is neither.

    When both a yes and a no are present the **no wins**: this gates outbound messages,
    and a false "yes" is the expensive mistake (SPEC.md §2.4).
    """
    norm = normalise(text)
    if not norm:
        return None
    for pattern in _RHETORICAL_YES:
        norm = pattern.sub(" yes ", norm)

    if _earliest(norm, _NEGATIVE) is not None:
        return False
    if _earliest(norm, _AFFIRMATIVE) is not None:
        return True
    return None


# ── Public entry point ──────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Intent:
    """What the merchant asked for, how sure we are, and what fired."""

    name: str
    confidence: float
    slots: dict[str, Any] = field(default_factory=dict)
    matched: list[str] = field(default_factory=list)

    @property
    def is_known(self) -> bool:
        return self.name != "unknown"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "confidence": self.confidence,
            "slots": dict(self.slots),
            "matched": list(self.matched),
        }


def detect_intent(text: str, *, language: str = "hi-IN") -> Intent:
    """Classify an utterance into one of :data:`INTENT_NAMES` with slots and provenance.

    ``language`` is the caller's declared conversation language; it is only used to decide
    the ``language`` slot when the text itself carries no script signal (romanised
    Hinglish). An ``unknown`` result reports how confident we are that *nothing* matched,
    and keeps the rejected runner-up in ``slots["best_guess"]`` so the UI can still show
    its reasoning.
    """
    norm = normalise(text)
    resolved_language = "hi-IN" if _DEVA_RE.search(text or "") else language

    if not norm:
        return Intent("unknown", 0.99, {"language": resolved_language}, [])

    token_count = len(norm.split())
    dilution = 1.0 + _DILUTION * max(0, token_count - _FREE_TOKENS)

    scored: list[tuple[float, int, str, list[str]]] = []
    for name, forms in _COMPILED.items():
        raw, matched = _score_intent(norm, forms)
        if raw <= 0:
            continue
        scored.append((min(raw, _RAW_CAP) / dilution, _PRIORITY.get(name, 0), name, matched))

    if not scored:
        return Intent("unknown", 0.99, {"language": resolved_language}, [])

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best_score, _, best_name, best_matched = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    confidence = _confidence(best_score, runner_up)

    if confidence < INTENT_THRESHOLD:
        slots = {"language": resolved_language, "best_guess": best_name}
        return Intent("unknown", round(1.0 - confidence, 3), slots, best_matched)

    slots = extract_slots(text, best_name)
    slots["language"] = resolved_language
    return Intent(best_name, confidence, slots, best_matched)
