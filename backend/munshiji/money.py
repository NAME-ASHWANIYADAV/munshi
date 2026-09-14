"""Money handling for MunshiJi.

Every monetary value in this system is an ``int`` number of **paise**. Floats are never
used for money. Rendering follows the Indian numbering system (thousand, lakh, crore).

    >>> fmt_inr(1854000)
    '₹18,540'
    >>> fmt_inr(123456789)
    '₹12,34,567.89'
    >>> fmt_inr_short(123456789)
    '₹12.3L'
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

__all__ = [
    "RUPEE",
    "fmt_inr",
    "fmt_inr_short",
    "fmt_pct",
    "fmt_rupees_words_hi",
    "group_indian",
    "paise_from_rupees",
    "pct_change",
    "rupees",
]

RUPEE = "₹"

CRORE_PAISE = 10_000_000_00
LAKH_PAISE = 100_000_00
THOUSAND_PAISE = 1_000_00


def paise_from_rupees(amount: float | int | str | Decimal) -> int:
    """Convert a rupee amount to integer paise, rounding half-up."""
    quantum = Decimal("0.01")
    value = Decimal(str(amount)).quantize(quantum, rounding=ROUND_HALF_UP)
    return int(value * 100)


def rupees(amount_paise: int) -> Decimal:
    """Return the exact rupee value of ``amount_paise`` as a Decimal."""
    return (Decimal(int(amount_paise)) / Decimal(100)).quantize(Decimal("0.01"))


def group_indian(digits: str) -> str:
    """Group a string of digits Indian-style: ``'1234567'`` -> ``'12,34,567'``."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    chunks: list[str] = []
    while len(head) > 2:
        chunks.insert(0, head[-2:])
        head = head[:-2]
    if head:
        chunks.insert(0, head)
    return ",".join(chunks) + "," + tail


def fmt_inr(amount_paise: int, *, decimals: bool | None = None) -> str:
    """Format paise as ``₹12,34,567.89``.

    ``decimals=None`` (default) shows paise only when the amount is not a whole rupee.
    """
    amount_paise = int(amount_paise)
    sign = "-" if amount_paise < 0 else ""
    magnitude = abs(amount_paise)
    whole, remainder = divmod(magnitude, 100)
    show_decimals = remainder != 0 if decimals is None else decimals
    body = group_indian(str(whole))
    if show_decimals:
        body = f"{body}.{remainder:02d}"
    return f"{sign}{RUPEE}{body}"


def fmt_inr_short(amount_paise: int) -> str:
    """Compact Indian format for dashboards: ``₹12.3L``, ``₹1.24Cr``, ``₹18.5K``."""
    amount_paise = int(amount_paise)
    sign = "-" if amount_paise < 0 else ""
    magnitude = abs(amount_paise)

    def trim(value: float, suffix: str, digits: int) -> str:
        text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
        return f"{sign}{RUPEE}{text}{suffix}"

    if magnitude >= CRORE_PAISE:
        return trim(magnitude / CRORE_PAISE, "Cr", 2)
    if magnitude >= LAKH_PAISE:
        return trim(magnitude / LAKH_PAISE, "L", 2)
    if magnitude >= THOUSAND_PAISE:
        # Thousands are read aloud constantly ("atthaarah hazaar"); one decimal keeps it speakable.
        return trim(magnitude / THOUSAND_PAISE, "K", 1)
    return fmt_inr(amount_paise)


_HI_UNITS = [
    (10_000_000, "करोड़"),  # crore
    (100_000, "लाख"),  # lakh
    (1_000, "हजार"),  # hazaar
]


def fmt_rupees_words_hi(amount_paise: int) -> str:
    """Speakable Hindi magnitude, e.g. ``'अठारह हजार पाँच सौ चालीस रुपये'`` is overkill for TTS;
    we return the compact spoken form merchants actually use: ``'18 हजार 540 रुपये'``."""
    whole = abs(int(amount_paise)) // 100
    for threshold, unit in _HI_UNITS:
        if whole >= threshold:
            major, minor = divmod(whole, threshold)
            text = f"{major} {unit}"
            if minor:
                text = f"{text} {minor}"
            return f"{text} रुपये"
    return f"{whole} रुपये"


def pct_change(current: float, baseline: float) -> float | None:
    """Percent change from ``baseline`` to ``current``; ``None`` when baseline is zero."""
    if baseline == 0:
        return None
    return (current - baseline) / abs(baseline) * 100.0


def fmt_pct(value: float | None, *, signed: bool = True, digits: int = 1) -> str:
    """Format a percentage; ``None`` renders as ``'—'``."""
    if value is None:
        return "—"
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{value:.{digits}f}%"
