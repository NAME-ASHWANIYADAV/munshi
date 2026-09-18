"""Password hashing for the demo login, plus phone normalisation.

Deliberately boring: a salted SHA-256 digest, not bcrypt. These are seeded demo accounts whose
passwords are printed on the login screen — the point of hashing them at all is discipline
(the DB never holds a plain password, and the verify path is real), not resistance to offline
cracking. If real accounts ever land here, swap ``hash_password`` for a KDF and reseed.
"""

from __future__ import annotations

import hashlib
import hmac

__all__ = ["hash_password", "normalise_phone", "session_token", "verify_password"]

#: Application salt. Fixed so seeding stays deterministic across machines and boots.
_SALT = "munshiji::demo::v1"


def normalise_phone(raw: str) -> str:
    """The last 10 digits of whatever the user typed.

    Merchants know their number as "98110 34572"; the seed stores "+919811034572"; a judge may
    type either, with spaces, dashes or a +91. Comparing the trailing 10 digits accepts all of
    them without a parsing library.
    """
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[-10:]


def hash_password(phone: str, password: str) -> str:
    """Digest bound to the phone, so two shops with the same password hash differently."""
    material = f"{_SALT}:{normalise_phone(phone)}:{password}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def verify_password(phone: str, password: str, stored_hash: str) -> bool:
    """Constant-time comparison; an empty stored hash never matches."""
    if not stored_hash:
        return False
    return hmac.compare_digest(hash_password(phone, password), stored_hash)


def session_token(merchant_id: str, password_hash: str) -> str:
    """An opaque, stable token the client can hold as proof of a completed login.

    Nothing server-side checks it yet — every read endpoint of this demo is open — but issuing
    one keeps the login flow shaped like the real thing, so gating endpoints later is a
    one-decorator change rather than a client rewrite.
    """
    return hashlib.sha256(f"{_SALT}:token:{merchant_id}:{password_hash}".encode()).hexdigest()[:40]
