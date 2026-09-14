"""Action unit economics.

The point of this module is that MunshiJi never proposes spending money without saying what the
spend is and what has to happen for it to be worth it. These tests hold that promise in place.
"""

from __future__ import annotations

import pytest

from munshiji.economics import (
    MESSAGE_COST_PAISE,
    TOOL_TEMPLATE,
    TemplateCategory,
    estimate_action,
)


def test_a_reminder_is_utility_and_an_offer_is_marketing() -> None:
    """The distinction that drives everything else: a balance reminder is not promotion.

    It relates to a transaction the customer already entered into, so it is billed as utility -
    several times cheaper than marketing. That is why chasing udhaar is the best-value outbound
    action in the product, which is the opposite of most people's intuition.
    """
    assert TOOL_TEMPLATE["send_udhaar_reminder"] is TemplateCategory.UTILITY
    assert TOOL_TEMPLATE["send_winback_offer"] is TemplateCategory.MARKETING
    assert (
        MESSAGE_COST_PAISE[TemplateCategory.MARKETING]
        > MESSAGE_COST_PAISE[TemplateCategory.UTILITY] * 3
    )


def test_send_cost_is_recipients_times_the_rate() -> None:
    economics = estimate_action(
        "send_winback_offer",
        recipients=14,
        expected_return_paise=400_000,
        value_per_response_paise=30_000,
    )
    assert economics.send_cost_paise == 14 * MESSAGE_COST_PAISE[TemplateCategory.MARKETING]
    assert economics.net_paise == 400_000 - economics.send_cost_paise


def test_breakeven_is_the_number_a_shopkeeper_actually_decides_on() -> None:
    """'How many have to come back before this pays for itself' - rounded up, never down."""
    economics = estimate_action(
        "send_winback_offer",
        recipients=100,
        expected_return_paise=500_000,
        value_per_response_paise=3_000,  # ₹30 per response
    )
    # ₹78 of sending against ₹30 a head: three responses, because two would not cover it.
    assert economics.send_cost_paise == 7_800
    assert economics.breakeven_responses == 3


def test_chasing_udhaar_breaks_even_on_a_single_customer() -> None:
    """The comparison worth putting on a slide."""
    reminder = estimate_action(
        "send_udhaar_reminder",
        recipients=20,
        expected_return_paise=1_500_000,
        value_per_response_paise=75_000,
    )
    assert reminder.breakeven_responses == 1
    assert reminder.roi is not None and reminder.roi > 100


def test_zero_recipients_does_not_divide_by_zero() -> None:
    economics = estimate_action(
        "send_winback_offer", recipients=0, expected_return_paise=0, value_per_response_paise=0
    )
    assert economics.send_cost_paise == 0
    assert economics.breakeven_responses == 0
    assert economics.roi is None


def test_missing_per_response_value_falls_back_to_the_average() -> None:
    economics = estimate_action(
        "send_winback_offer",
        recipients=10,
        expected_return_paise=50_000,
        value_per_response_paise=0,
    )
    assert economics.value_per_response_paise == 5_000


def test_an_unknown_tool_is_costed_conservatively_as_utility() -> None:
    economics = estimate_action(
        "some_future_tool", recipients=5, expected_return_paise=1_000, value_per_response_paise=200
    )
    assert economics.template is TemplateCategory.UTILITY


@pytest.mark.parametrize("hindi", [True, False])
def test_the_sentence_names_cost_return_and_breakeven(hindi: bool) -> None:
    economics = estimate_action(
        "send_winback_offer",
        recipients=14,
        expected_return_paise=400_000,
        value_per_response_paise=30_000,
    )
    sentence = economics.sentence(hindi=hindi)
    assert "₹10.92" in sentence or "₹11" in sentence  # the send cost
    assert "₹4,000" in sentence
    assert str(economics.breakeven_responses) in sentence


def test_payload_money_is_pre_formatted_for_the_ui() -> None:
    payload = estimate_action(
        "send_udhaar_reminder",
        recipients=7,
        expected_return_paise=90_000,
        value_per_response_paise=13_000,
    ).as_dict()
    for key in ("send_cost_display", "expected_return_display", "net_display"):
        assert payload[key].startswith("₹")
    assert payload["unit_cost_display"] == "₹0.12"
