"""In-process event bus for live dashboard updates.

The companion screen subscribes over SSE so the numbers, insights and action queue move while the
merchant is mid-sentence, rather than on a polling timer. Scope is one process, which is exactly
right for a single-merchant demo; swapping in Redis pub/sub later would not change this interface.

Publishing is non-blocking and never raises: a slow or dead subscriber is dropped rather than
allowed to stall an agent turn.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from munshiji.clock import now_ist
from munshiji.logging import get_logger

__all__ = ["Event", "EventBus", "EventName", "get_event_bus"]

logger = get_logger(__name__)

#: Queue depth per subscriber before we start dropping the oldest frames.
_QUEUE_SIZE = 64


class EventName:
    """Event names the frontend switches on."""

    TURN = "turn"
    ACTION_PROPOSED = "action.proposed"
    ACTION_DECIDED = "action.decided"
    ACTION_EXECUTED = "action.executed"
    INSIGHTS_REFRESHED = "insights.refreshed"
    DASHBOARD = "dashboard"
    MEMORY_UPDATED = "memory.updated"
    PROVIDER_CHANGED = "provider.changed"
    HEARTBEAT = "heartbeat"


@dataclass(slots=True)
class Event:
    """One server-sent frame."""

    name: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        """Render in the text/event-stream wire format."""
        payload = json.dumps(
            {"event": self.name, "data": self.data, "at": now_ist().isoformat()},
            ensure_ascii=False,
            default=str,
        )
        return f"event: {self.name}\ndata: {payload}\n\n"


class EventBus:
    """Fan-out of events to every subscriber of a merchant."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[Event]]] = {}

    def publish(self, merchant_id: str, name: str, data: dict[str, Any] | None = None) -> None:
        """Broadcast an event. Safe to call from anywhere; never raises, never blocks."""
        queues = self._subscribers.get(merchant_id)
        if not queues:
            return
        event = Event(name=name, data=data or {})
        for queue in list(queues):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest frame rather than the newest: dashboards care about *now*.
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover - race
                    logger.debug("dropped event %s for a saturated subscriber", name)

    async def subscribe(
        self, merchant_id: str, *, heartbeat_seconds: float = 20.0
    ) -> AsyncIterator[Event]:
        """Yield events for a merchant until the client disconnects.

        A heartbeat keeps proxies from closing an idle stream.
        """
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._subscribers.setdefault(merchant_id, set()).add(queue)
        logger.debug("sse subscriber added merchant=%s", merchant_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
                except TimeoutError:
                    yield Event(name=EventName.HEARTBEAT)
                    continue
                yield event
        finally:
            subscribers = self._subscribers.get(merchant_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(merchant_id, None)
            logger.debug("sse subscriber removed merchant=%s", merchant_id)

    def subscriber_count(self, merchant_id: str) -> int:
        """How many dashboards are currently watching this merchant."""
        return len(self._subscribers.get(merchant_id, ()))


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Process-wide event bus."""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
