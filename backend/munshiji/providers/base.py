"""Shared vocabulary for providers.

Every external capability (LLM, STT, TTS, memory, actions) is defined as a Protocol with two
implementations: a ``live`` one that calls the vendor, and a ``local`` one that is fully
functional offline. See SPEC.md §2.1 — this is the rule the whole system is built on.

Providers are **async**; the database layer is sync. SQLite reads are sub-millisecond at this
scale, so repositories are called directly from async code rather than through a threadpool.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from munshiji.clock import now_utc

__all__ = [
    "ProviderHealth",
    "ProviderKind",
    "ProviderMode",
    "Timed",
    "atimed",
    "timed",
]

ProviderMode = Literal["live", "local"]
ProviderKind = Literal["llm", "stt", "tts", "memory", "actions"]

T = TypeVar("T")


@dataclass(slots=True)
class ProviderHealth:
    """Result of a provider readiness probe — surfaced on ``GET /api/health``."""

    name: str
    kind: ProviderKind
    mode: ProviderMode
    ok: bool
    detail: str = ""
    latency_ms: int = 0
    checked_at: datetime = field(default_factory=now_utc)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "mode": self.mode,
            "ok": self.ok,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            "checked_at": self.checked_at.isoformat(),
        }


@dataclass(slots=True)
class Timed(Generic[T]):
    """A value plus how long it took to produce, in milliseconds."""

    value: T
    latency_ms: int


def timed(fn: Callable[[], T]) -> Timed[T]:
    """Run ``fn`` and record elapsed milliseconds."""
    started = time.perf_counter()
    value = fn()
    return Timed(value=value, latency_ms=int((time.perf_counter() - started) * 1000))


async def atimed(awaitable: Awaitable[T]) -> Timed[T]:
    """Await ``awaitable`` and record elapsed milliseconds."""
    started = time.perf_counter()
    value = await awaitable
    return Timed(value=value, latency_ms=int((time.perf_counter() - started) * 1000))
