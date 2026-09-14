"""Table-driven tests for the Hindi / Hinglish / English intent parser.

The table *is* the specification: every row is something a Lajpat Nagar shopkeeper could
plausibly say, in the script they would say it in. It doubles as the accuracy report — if a row
regresses, the number in the README is wrong.
"""

from __future__ import annotations

from typing import Any

import pytest

from munshiji.nlu import (
    INTENT_NAMES,
    INTENT_THRESHOLD,
    Intent,
    detect_affirmation,
    detect_intent,
    detect_language,
    normalise,
)

#: ``(utterance, expected intent, expected slot subset)``.
UTTERANCES: list[tuple[str, str, dict[str, Any]]] = [
    # ── sales_summary ────────────────────────────────────────────────────────────────
    ("aaj dhandha kaisa raha?", "sales_summary", {"period": "today"}),
    ("aaj ka dhandaa kitna hua", "sales_summary", {"period": "today"}),
    ("आज कितना आया?", "sales_summary", {"period": "today"}),
    ("आज की बिक्री बताओ", "sales_summary", {"period": "today"}),
    ("kitna kamaya aaj", "sales_summary", {}),
    ("today's collection please", "sales_summary", {}),
    ("how much did i make today", "sales_summary", {"period": "today"}),
    ("total sales dikhao", "sales_summary", {}),
    ("aaj ka hisab batao munshi", "sales_summary", {}),
    # ── compare_sales ────────────────────────────────────────────────────────────────
    (
        "pichle hafte se kitna raha",
        "compare_sales",
        {"period_a": "this_week", "period_b": "last_week"},
    ),
    ("kal se zyada hua kya", "compare_sales", {"period_a": "today", "period_b": "yesterday"}),
    ("कल से कम है क्या", "compare_sales", {"period_b": "yesterday"}),
    ("last week comparison chahiye", "compare_sales", {"period_b": "last_week"}),
    ("compared to last month kaisa hai", "compare_sales", {"period_b": "last_month"}),
    ("पिछले हफ्ते से तुलना करो", "compare_sales", {"period_b": "last_week"}),
    # ── top_customers ────────────────────────────────────────────────────────────────
    ("sabse zyada kaun kharidta hai", "top_customers", {}),
    ("top 5 customers batao", "top_customers", {"count": 5}),
    ("mere best customers kaun hain", "top_customers", {}),
    ("सबसे ज्यादा कौन खरीदते हैं", "top_customers", {}),
    ("टॉप ग्राहक दिखाओ", "top_customers", {}),
    ("who spends the most here", "top_customers", {}),
    # ── dormant_customers ────────────────────────────────────────────────────────────
    ("kaun kaun nahi aa raha aajkal", "dormant_customers", {"period": "recent"}),
    ("kaun nahi aaya is mahine", "dormant_customers", {"period": "this_month"}),
    ("purane customer wapas nahi aa rahe", "dormant_customers", {}),
    ("कौन नहीं आ रहा आजकल", "dormant_customers", {"period": "recent"}),
    ("पुराने ग्राहक गायब हैं", "dormant_customers", {}),
    ("which regulars missing these days", "dormant_customers", {"period": "recent"}),
    ("customers who stopped coming", "dormant_customers", {}),
    # ── inventory_alerts ─────────────────────────────────────────────────────────────
    ("kya saman khatam ho gaya", "inventory_alerts", {}),
    ("kya mangwana hai kal ke liye", "inventory_alerts", {}),
    ("stock kitna bacha hai", "inventory_alerts", {}),
    ("रीस्टॉक क्या करना है", "inventory_alerts", {}),
    ("सामान खत्म हो गया क्या", "inventory_alerts", {}),
    ("what should i order this week", "inventory_alerts", {"period": "this_week"}),
    ("anything running low", "inventory_alerts", {}),
    # ── udhaar_summary ───────────────────────────────────────────────────────────────
    ("kitna udhaar baki hai", "udhaar_summary", {}),
    ("udhaar khata dikhao", "udhaar_summary", {}),
    ("कितना बाकी है", "udhaar_summary", {}),
    ("उधार कितना चल रहा है", "udhaar_summary", {}),
    ("who owes me money", "udhaar_summary", {}),
    ("pending payments ka hisab", "udhaar_summary", {}),
    # ── merchant_health ──────────────────────────────────────────────────────────────
    ("dukaan ki sehat kaisi hai", "merchant_health", {}),
    ("business kaisa chal raha hai overall", "merchant_health", {}),
    ("mujhe loan mil sakta hai kya", "merchant_health", {}),
    ("दुकान की सेहत बताओ", "merchant_health", {}),
    ("क्या लोन मिल सकता है", "merchant_health", {}),
    ("what is my shop health score", "merchant_health", {}),
    # ── insights ─────────────────────────────────────────────────────────────────────
    ("aaj kya karna chahiye", "insights", {"period": "today"}),
    ("koi suggestion do", "insights", {}),
    ("main kya karun ab", "insights", {}),
    ("क्या करूँ आज", "insights", {}),
    ("कोई सलाह दो", "insights", {}),
    ("what should i do next", "insights", {}),
    # ── recall_memory ────────────────────────────────────────────────────────────────
    ("pichli baar jo offer bheja tha uska kya hua", "recall_memory", {}),
    ("last time ka kya result raha", "recall_memory", {}),
    ("पिछली बार क्या हुआ था", "recall_memory", {}),
    ("उसका क्या हुआ", "recall_memory", {}),
    ("what happened to the offer i sent", "recall_memory", {}),
    # ── send_offer ───────────────────────────────────────────────────────────────────
    ("unhe 50 rupaye ka offer bhej do", "send_offer", {"discount_amount_rupees": 50}),
    ("20% discount bhej do 12 customers ko", "send_offer", {"discount_pct": 20, "count": 12}),
    ("ऑफर भेज दो उनको", "send_offer", {}),
    ("send them a win back offer", "send_offer", {}),
    ("pachas rupaye ka discount de do", "send_offer", {"discount_amount_rupees": 50}),
    # ── send_reminder ────────────────────────────────────────────────────────────────
    ("reminder bhej do sabko", "send_reminder", {"tone": "standard"}),
    ("yaad dila do pyaar se", "send_reminder", {"tone": "gentle"}),
    ("udhaar maango sakhti se", "send_reminder", {"tone": "firm"}),
    ("याद दिला दो उनको", "send_reminder", {"tone": "standard"}),
    ("send a reminder to them", "send_reminder", {"tone": "standard"}),
    # ── restock ──────────────────────────────────────────────────────────────────────
    ("order kar do supplier ko", "restock", {}),
    ("mangwa do jaldi", "restock", {}),
    ("ऑर्डर कर दो", "restock", {}),
    ("place the order now", "restock", {}),
    # ── greeting / thanks ────────────────────────────────────────────────────────────
    ("namaste munshi ji", "greeting", {}),
    ("नमस्ते", "greeting", {}),
    ("good morning", "greeting", {}),
    ("shukriya bhai", "thanks", {}),
    ("धन्यवाद", "thanks", {}),
    ("thank you so much", "thanks", {}),
    # ── unknown ──────────────────────────────────────────────────────────────────────
    ("mausam kaisa hai dilli mein", "unknown", {}),
    ("cricket ka score kya hai", "unknown", {}),
    ("", "unknown", {}),
]


def test_table_covers_every_intent() -> None:
    """Regression guard: a new intent must arrive with test rows."""
    covered = {expected for _, expected, _ in UTTERANCES}
    assert covered == set(INTENT_NAMES)
    assert len(UTTERANCES) >= 45


@pytest.mark.parametrize(("utterance", "expected", "slots"), UTTERANCES, ids=lambda v: str(v)[:40])
def test_detect_intent(utterance: str, expected: str, slots: dict[str, Any]) -> None:
    intent = detect_intent(utterance)
    assert isinstance(intent, Intent)
    assert intent.name == expected, f"{utterance!r} -> {intent.name} ({intent.matched})"
    for key, value in slots.items():
        assert intent.slots.get(key) == value, f"{utterance!r} slot {key}: {intent.slots}"
    if expected != "unknown":
        assert intent.confidence >= INTENT_THRESHOLD
        assert intent.matched, "a recognised intent must say which surface form fired"


def test_unknown_keeps_its_best_guess_for_the_why_badge() -> None:
    intent = detect_intent("cricket ka score kya hai")
    assert intent.name == "unknown"
    assert 0.0 <= intent.confidence <= 1.0


def test_language_slot_follows_the_script_not_the_caller() -> None:
    assert detect_intent("आज कितना आया", language="en-IN").slots["language"] == "hi-IN"
    assert detect_intent("todays collection", language="en-IN").slots["language"] == "en-IN"
    # Romanised Hinglish has no script signal, so the caller's declared language stands.
    assert detect_intent("aaj ka dhandha", language="hi-IN").slots["language"] == "hi-IN"


# ── normalise ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Dhanda", "danda"),
        ("dhandha", "danda"),
        ("dhandaa", "danda"),
        ("DHANDHAA", "danda"),
        ("zyada", "jyada"),
        ("jyaada", "jyada"),
        ("Khaata", "kata"),
        ("khata", "kata"),
        ("pichhli", "picli"),
        ("pichli", "picli"),
        ("Aaj ka dhandha!", "aj ka danda"),
        ("today's collection", "todays colection"),
        ("₹50", "rs 50"),
        ("50%", "50 percent"),
        ("  spaced   out  ", "spaced out"),
        ("५०", "50"),
    ],
)
def test_normalise_folds_variants(raw: str, expected: str) -> None:
    assert normalise(raw) == expected


def test_normalise_preserves_devanagari_matras() -> None:
    # Devanagari vowel signs are not ``\w`` in Python's ``re``; a naive strip would eat them.
    assert normalise("कितना आया?") == "कितना आया"
    assert "\u093f" in normalise("पिछली बार")


def test_normalise_folds_devanagari_nukta_and_vowel_length() -> None:
    assert normalise("ज़्यादा") == normalise("ज्यादा")
    assert normalise("करूँ") == normalise("करूं")
    assert normalise("रूपये") == normalise("रुपये")


def test_teen_and_ten_do_not_collide() -> None:
    """The fold must not turn "teen" (3) into "ten" (10)."""
    assert normalise("teen") != normalise("ten")


# ── detect_affirmation ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("haan bhej do", True),
        ("haan", True),
        ("ji haan", True),
        ("हाँ", True),
        ("theek hai kar do", True),
        ("bilkul", True),
        ("yes please", True),
        ("go ahead", True),
        ("ठीक है", True),
        ("kyun nahi", True),
        ("nahi rehne do", False),
        ("nahi", False),
        ("नहीं", False),
        ("abhi nahi", False),
        ("baad mein dekhenge", False),
        ("rehne do", False),
        ("no", False),
        ("cancel karo", False),
        ("aaj ka dhandha kaisa raha", None),
        ("kitna udhaar baki hai", None),
        ("", None),
    ],
)
def test_detect_affirmation(utterance: str, expected: bool | None) -> None:
    assert detect_affirmation(utterance) is expected


def test_negation_wins_when_both_appear() -> None:
    """Gating outbound messages: a false yes is the expensive mistake (SPEC.md §2.4)."""
    assert detect_affirmation("haan nahi bhejna") is False


# ── detect_language ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("आज कितना आया", "hi-IN"),
        ("कल", "hi-IN"),
        ("aaj ka dhandha", "en-IN"),
        ("today's collection", "en-IN"),
        ("mixed आज today", "hi-IN"),
        ("", "en-IN"),
    ],
)
def test_detect_language(utterance: str, expected: str) -> None:
    assert detect_language(utterance) == expected


# ── accuracy report ─────────────────────────────────────────────────────────────────────────


def test_table_accuracy_is_total() -> None:
    """Every row must classify correctly — this is the number quoted in the README."""
    wrong = [
        (utterance, expected, detect_intent(utterance).name)
        for utterance, expected, _ in UTTERANCES
        if detect_intent(utterance).name != expected
    ]
    assert not wrong, f"{len(UTTERANCES) - len(wrong)}/{len(UTTERANCES)} correct; missed {wrong}"
