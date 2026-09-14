"""Custom column types.

SQLite has no native timestamp type: ``DateTime(timezone=True)`` stores an ISO string and hands
back a **naive** ``datetime`` on load. Any arithmetic against an aware "now" then raises
``TypeError: can't subtract offset-naive and offset-aware datetimes`` — and only at runtime, in
whichever code path happened to compare a stored timestamp with the clock.

:class:`UtcDateTime` closes that hole at the boundary: values are normalised to UTC on the way in
and re-tagged as UTC on the way out, so every timestamp in the application is timezone-aware no
matter which database served it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from munshiji.clock import UTC

__all__ = ["UtcDateTime"]


class UtcDateTime(TypeDecorator[datetime]):
    """A timezone-aware UTC timestamp that stays aware across a SQLite round trip."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Normalise to UTC before storing; a naive value is assumed to already be UTC."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        """Re-attach UTC to whatever the driver returned."""
        if value is None:
            return None
        if isinstance(value, str):  # pragma: no cover - only some drivers do this
            value = datetime.fromisoformat(value)
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
