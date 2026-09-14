"""Tests for the outbound copy — wording, length, and the §2.4 safety boundary.

Everything here is pure: no database, no clock, no network.
"""

from __future__ import annotations

import re

import pytest

from munshiji.db.enums import Tone
from munshiji.messaging import (
    MAX_MESSAGE_CHARS,
    TONE_RULES,
    contains_forbidden_terms,
    followup_note,
    payment_link,
    render_for_tool,
    restock_order,
    select_tone,
    udhaar_reminder,
    winback_offer,
)

# A deliberately long South Indian name — the worst realistic case for the SMS budget.
LONG_NAME = "Venkataraman Subrahmanya Krishnamurthy Iyer"
LONG_SHOP = "Shri Balaji Kirana & General Store"

DEVANAGARI = re.compile(r"[ऀ-ॿ]")
EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿]")

ITEMS = [
    {"name": "Toor Dal", "name_hi": "तूर दाल", "qty": 12, "unit": "kg"},
    {"name": "Fortune Sunflower Oil 1L", "name_hi": "फॉर्च्यून तेल 1L", "qty": 24, "unit": "pc"},
    {"name": "Aashirvaad Atta 10kg", "name_hi": "आशीर्वाद आटा 10kg", "qty": 15, "unit": "bag"},
]


def all_messages() -> dict[str, dict[str, str]]:
    """Every template, rendered with the worst-case name and shop."""
    rendered = {
        "winback": winback_offer(LONG_NAME, LONG_SHOP, "20% off up to Rs.200", 7, 34),
        "payment_link": payment_link(
            LONG_NAME, LONG_SHOP, 1_234_567, "https://paytm.me/pay/7KQ4M2XD"
        ),
        "restock": restock_order(LONG_SHOP, ITEMS),
        "followup": followup_note("Sharma ji ko naya rate confirm karna hai", "kal shaam 6 baje"),
    }
    for tone in Tone:
        rendered[f"udhaar_{tone.value}"] = udhaar_reminder(
            LONG_NAME, LONG_SHOP, 1_234_567, 63, tone
        )
    return rendered


# ─────────────────────────────────────────────────────────────────────────────
# Shape: bilingual, SMS-safe, Devanagari where promised
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", sorted(all_messages()))
def test_every_template_is_bilingual(key: str) -> None:
    message = all_messages()[key]
    assert set(message) == {"hi", "en"}
    assert message["hi"].strip()
    assert message["en"].strip()
    assert message["hi"] != message["en"]


@pytest.mark.parametrize("key", sorted(all_messages()))
def test_every_template_fits_the_sms_budget(key: str) -> None:
    for lang, text in all_messages()[key].items():
        assert len(text) <= MAX_MESSAGE_CHARS, f"{key}/{lang} is {len(text)} chars"


@pytest.mark.parametrize("key", sorted(all_messages()))
def test_hindi_is_actually_devanagari(key: str) -> None:
    assert DEVANAGARI.search(all_messages()[key]["hi"]), f"{key}/hi has no Devanagari"


@pytest.mark.parametrize("key", sorted(all_messages()))
def test_at_most_one_emoji(key: str) -> None:
    for lang, text in all_messages()[key].items():
        assert len(EMOJI.findall(text)) <= 1, f"{key}/{lang} is emoji-heavy"


def test_customer_messages_name_the_customer_and_the_shop() -> None:
    for key in ("winback", "payment_link", "udhaar_gentle", "udhaar_standard", "udhaar_firm"):
        for text in all_messages()[key].values():
            assert LONG_NAME in text
            assert LONG_SHOP in text


def test_free_text_longer_than_the_budget_is_clamped_not_dropped() -> None:
    message = followup_note("naya supplier rate poochna hai " * 40, "kal")
    for text in message.values():
        assert len(text) <= MAX_MESSAGE_CHARS
        assert "naya supplier rate" in text
        assert text.endswith("…")


def test_restock_trims_a_long_item_list_and_says_how_many_were_dropped() -> None:
    many = [{"name": f"Long Product Name Number {n}", "qty": n, "unit": "kg"} for n in range(1, 30)]
    message = restock_order(LONG_SHOP, many)
    assert len(message["en"]) <= MAX_MESSAGE_CHARS
    assert len(message["hi"]) <= MAX_MESSAGE_CHARS
    assert "more" in message["en"]
    assert "और" in message["hi"]


def test_restock_is_english_primary_but_localises_item_names() -> None:
    message = restock_order(LONG_SHOP, ITEMS)
    assert "Restock order from" in message["en"]
    assert "12 kg Toor Dal" in message["en"]
    assert "तूर दाल" in message["hi"]


# ─────────────────────────────────────────────────────────────────────────────
# The tone ladder
# ─────────────────────────────────────────────────────────────────────────────


def test_exactly_three_tones_exist() -> None:
    """There is deliberately no rung above FIRM (SPEC.md §2.4)."""
    assert [tone.value for tone in Tone] == ["gentle", "standard", "firm"]


def test_the_three_tones_are_genuinely_different_registers() -> None:
    rendered = {
        tone: udhaar_reminder("Ramesh Gupta", "Sharma General Store", 250_000, 40, tone)
        for tone in Tone
    }
    for lang in ("hi", "en"):
        texts = [rendered[tone][lang] for tone in Tone]
        assert len(set(texts)) == 3, f"{lang} tones are not distinct"
        # Not merely a swapped adjective: the sentences differ substantially.
        for first in range(3):
            for second in range(first + 1, 3):
                shared = set(texts[first].split()) & set(texts[second].split())
                longest = max(len(texts[first].split()), len(texts[second].split()))
                assert len(shared) / longest < 0.8, f"{lang} tones are near-identical"


def test_gentle_offers_no_deadline_and_firm_makes_a_clear_ask() -> None:
    gentle = udhaar_reminder("Ramesh Gupta", "Sharma General Store", 250_000, 40, Tone.GENTLE)
    firm = udhaar_reminder("Ramesh Gupta", "Sharma General Store", 250_000, 40, Tone.FIRM)
    assert "जब सुविधा हो" in gentle["hi"]
    assert "Whenever it suits you" in gentle["en"]
    assert "कृपया" in firm["hi"] and "settle" in firm["hi"]
    assert "Please settle it" in firm["en"]
    # FIRM stays warm: it offers to help rather than naming a consequence.
    assert "बता दीजिए" in firm["hi"]
    assert "sort it out together" in firm["en"]


def test_tone_accepts_the_enum_or_its_string_value() -> None:
    assert udhaar_reminder("A", "B", 100, 1, "firm") == udhaar_reminder("A", "B", 100, 1, Tone.FIRM)


# ── Forbidden vocabulary ────────────────────────────────────────────────────

#: Written out here on purpose rather than imported: the test states the boundary
#: independently of the module it guards.
THREATENING = (
    # English
    "legal",
    "lawyer",
    "court",
    "police",
    "penalty",
    "blacklist",
    "defaulter",
    "recovery agent",
    "consequence",
    "warning",
    "final notice",
    "last chance",
    "immediately",
    "must pay",
    "interest",
    "seize",
    "shame",
    "threat",
    "no more credit",
    "action will be taken",
    # Hindi
    "पुलिस",
    "अदालत",
    "कानूनी",
    "वकील",
    "जुर्माना",
    "ब्याज",
    "चेतावनी",
    "कार्रवाई",
    "वसूली",
    "धमकी",
    "बदनाम",
    "मजबूर",
    "सख्त",
    "अंतिम सूचना",
    "आखिरी मौका",
    "उधार बंद",
)


@pytest.mark.parametrize("tone", list(Tone))
@pytest.mark.parametrize("days", [1, 20, 45, 120, 400])
@pytest.mark.parametrize("amount", [500, 250_000, 12_500_000])
def test_no_reminder_in_any_tone_uses_threatening_language(
    tone: Tone, days: int, amount: int
) -> None:
    message = udhaar_reminder(LONG_NAME, LONG_SHOP, amount, days, tone)
    for lang, text in message.items():
        lowered = text.lower()
        hits = [term for term in THREATENING if term.lower() in lowered]
        assert not hits, f"{tone.value}/{lang} contains {hits}"
        assert not contains_forbidden_terms(text), f"{tone.value}/{lang} trips the module guard"


@pytest.mark.parametrize("key", sorted(all_messages()))
def test_no_outbound_template_at_all_uses_threatening_language(key: str) -> None:
    for lang, text in all_messages()[key].items():
        assert not contains_forbidden_terms(text), f"{key}/{lang}"


def test_the_guard_itself_actually_catches_something() -> None:
    """A guard that never fires is not a guard."""
    assert contains_forbidden_terms("We will take legal action and inform the police")
    assert contains_forbidden_terms("कल तक नहीं दिया तो कानूनी कार्रवाई होगी")
    assert contains_forbidden_terms("") == []


# ── select_tone rule table ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("days", "score", "paise", "expected", "why"),
    [
        (0, 0.10, 1_000_000, Tone.GENTLE, "R1 fresh: even a big balance starts soft"),
        (15, 0.00, 1_000_000, Tone.GENTLE, "R1 upper boundary"),
        (16, 0.50, 1_000_000, Tone.STANDARD, "R1 just expired, nothing else matches"),
        (16, 0.75, 1_000_000, Tone.GENTLE, "R2 reliable grace begins"),
        (30, 0.75, 1_000_000, Tone.GENTLE, "R2 upper boundary"),
        (31, 0.75, 1_000_000, Tone.STANDARD, "R2 just expired"),
        (30, 0.39, 500_000, Tone.FIRM, "R4 all three conditions exactly at boundary"),
        (30, 0.40, 500_000, Tone.STANDARD, "R4 score boundary is exclusive"),
        (30, 0.39, 499_999, Tone.STANDARD, "R4 amount boundary is inclusive"),
        (29, 0.39, 500_000, Tone.STANDARD, "R4 day boundary"),
        (44, 0.74, 10_000, Tone.STANDARD, "just short of R3"),
        (45, 0.74, 10_000, Tone.FIRM, "R3 day boundary"),
        (45, 0.75, 10_000, Tone.STANDARD, "R3 score boundary is exclusive"),
        (-5, 0.00, 0, Tone.GENTLE, "negative days clamp to 0"),
        (90, 5.00, 9_000_000, Tone.STANDARD, "out-of-range score clamps to 1.0"),
    ],
)
def test_select_tone_rule_table(
    days: int, score: float, paise: int, expected: Tone, why: str
) -> None:
    assert select_tone(days, score, paise) is expected, why


def test_a_reliable_payer_is_never_escalated_to_firm() -> None:
    for days in (0, 30, 60, 120, 365):
        for amount in (100, 500_000, 50_000_000):
            assert select_tone(days, 0.95, amount) is not Tone.FIRM


def test_the_rule_table_is_total() -> None:
    assert TONE_RULES[-1].tone is Tone.STANDARD
    assert TONE_RULES[-1].matches(0, 0.0, 0)
    assert all(rule.why for rule in TONE_RULES)


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch-time rendering
# ─────────────────────────────────────────────────────────────────────────────


def test_render_for_tool_personalises_per_recipient() -> None:
    params = {"discount_label": "15% off", "valid_days": 5}
    first = render_for_tool(
        "send_winback_offer",
        shop_name="Sharma General Store",
        params=params,
        target={"name": "Ramesh Gupta", "last_visit_days": 28},
    )
    second = render_for_tool(
        "send_winback_offer",
        shop_name="Sharma General Store",
        params=params,
        target={"name": "Priya Nair", "last_visit_days": 41},
    )
    assert "Ramesh Gupta" in first["hi"] and "28" in first["hi"]
    assert "Priya Nair" in second["hi"] and "41" in second["hi"]
    assert first != second


def test_render_for_tool_picks_a_tone_when_none_was_supplied() -> None:
    rendered = render_for_tool(
        "send_udhaar_reminder",
        shop_name="Sharma General Store",
        params={},
        target={"name": "Ramesh Gupta", "amount_paise": 250_000, "days_overdue": 3},
    )
    # 3 days overdue → R1 → GENTLE.
    assert "जब सुविधा हो" in rendered["hi"]


def test_render_for_tool_is_silent_about_unknown_tools() -> None:
    assert render_for_tool("save_merchant_note", shop_name="X") == {"hi": "", "en": ""}
