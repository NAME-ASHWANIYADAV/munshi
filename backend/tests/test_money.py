"""Money formatting — Indian numbering is where naive implementations quietly go wrong."""

from __future__ import annotations

from decimal import Decimal

import pytest

from munshiji.money import (
    fmt_inr,
    fmt_inr_short,
    fmt_pct,
    fmt_rupees_words_hi,
    group_indian,
    paise_from_rupees,
    pct_change,
    rupees,
)


@pytest.mark.parametrize(
    ("digits", "expected"),
    [
        ("1", "1"),
        ("99", "99"),
        ("999", "999"),
        ("1000", "1,000"),
        ("12345", "12,345"),
        ("123456", "1,23,456"),
        ("1234567", "12,34,567"),
        ("12345678", "1,23,45,678"),
        ("123456789", "12,34,56,789"),
    ],
)
def test_indian_grouping(digits: str, expected: str) -> None:
    """Groups of two after the first three — lakh and crore, not thousands all the way up."""
    assert group_indian(digits) == expected


@pytest.mark.parametrize(
    ("paise", "expected"),
    [
        (0, "₹0"),
        (1854000, "₹18,540"),
        (123456789, "₹12,34,567.89"),
        (50, "₹0.50"),
        (-1854000, "-₹18,540"),
    ],
)
def test_fmt_inr(paise: int, expected: str) -> None:
    assert fmt_inr(paise) == expected


def test_fmt_inr_decimals_are_suppressed_for_whole_rupees() -> None:
    assert fmt_inr(1854000) == "₹18,540"
    assert fmt_inr(1854000, decimals=True) == "₹18,540.00"
    assert fmt_inr(1854050, decimals=False) == "₹18,540"


@pytest.mark.parametrize(
    ("paise", "expected"),
    [
        (99900, "₹999"),
        (1854000, "₹18.5K"),
        (12345678, "₹1.23L"),
        (1230000000, "₹1.23Cr"),
        (12345678900, "₹12.35Cr"),
    ],
)
def test_fmt_inr_short(paise: int, expected: str) -> None:
    assert fmt_inr_short(paise) == expected


def test_paise_round_trip_is_exact() -> None:
    """No float drift: this is why money is stored as int paise."""
    assert paise_from_rupees("18540.50") == 1854050
    assert paise_from_rupees(18540.5) == 1854050
    assert rupees(1854050) == Decimal("18540.50")


def test_paise_rounds_half_up() -> None:
    assert paise_from_rupees("0.005") == 1
    assert paise_from_rupees("0.004") == 0


def test_spoken_hindi_magnitude() -> None:
    assert fmt_rupees_words_hi(1854000) == "18 हजार 540 रुपये"
    assert fmt_rupees_words_hi(45000) == "450 रुपये"


def test_pct_change_and_formatting() -> None:
    assert pct_change(112, 100) == pytest.approx(12.0)
    assert pct_change(88, 100) == pytest.approx(-12.0)
    assert pct_change(5, 0) is None
    assert fmt_pct(12.34) == "+12.3%"
    assert fmt_pct(-12.34) == "-12.3%"
    assert fmt_pct(None) == "—"
