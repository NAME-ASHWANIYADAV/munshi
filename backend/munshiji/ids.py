"""Prefixed, lexicographically sortable identifiers.

``new_id("cus")`` -> ``"cus_01K4W8YQ7M3ZB9XKD2"``

Layout after the prefix: 10 Crockford-base32 characters of millisecond timestamp (sortable by
creation time) followed by 8 characters of randomness. Readable in logs, stable in URLs, and
collision-safe well past hackathon scale — without pulling in a ULID dependency.
"""

from __future__ import annotations

import secrets
import time

__all__ = ["ID_PREFIXES", "new_id", "timestamp_of"]

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U
_TIME_CHARS = 10
_RANDOM_CHARS = 8

ID_PREFIXES = {
    "merchant": "mer",
    "customer": "cus",
    "product": "prd",
    "transaction": "txn",
    "transaction_item": "tif",
    "khata": "kht",
    "insight": "ins",
    "action": "act",
    "outcome": "out",
    "conversation": "cnv",
    "turn": "trn",
    "memory_node": "mnd",
    "memory_edge": "med",
}


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, remainder = divmod(value, 32)
        chars.append(_ALPHABET[remainder])
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    """Generate a sortable identifier with the given short prefix (e.g. ``"cus"``)."""
    millis = int(time.time() * 1000)
    random_bits = secrets.randbits(_RANDOM_CHARS * 5)
    return f"{prefix}_{_encode(millis, _TIME_CHARS)}{_encode(random_bits, _RANDOM_CHARS)}"


def timestamp_of(identifier: str) -> int:
    """Recover the millisecond creation timestamp encoded in an identifier."""
    _, _, body = identifier.partition("_")
    value = 0
    for char in body[:_TIME_CHARS]:
        value = value * 32 + _ALPHABET.index(char)
    return value
