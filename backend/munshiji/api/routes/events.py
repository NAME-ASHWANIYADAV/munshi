"""Server-sent events: the companion screen updates while the merchant is still talking."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from munshiji.api.deps import DbSession, resolve_merchant
from munshiji.events import EventName, get_event_bus
from munshiji.logging import get_logger

router = APIRouter(tags=["events"])
logger = get_logger(__name__)


@router.get("/events/{merchant_id}", summary="Live dashboard event stream (SSE)")
async def stream_events(
    merchant_id: str, request: Request, session: DbSession
) -> StreamingResponse:
    """Subscribe to this merchant's live events.

    Emits ``turn``, ``action.proposed``, ``action.decided``, ``action.executed``,
    ``insights.refreshed``, ``dashboard``, ``memory.updated`` and periodic ``heartbeat`` frames.
    """
    merchant = resolve_merchant(merchant_id, session)
    bus = get_event_bus()

    async def publisher() -> AsyncIterator[str]:
        # Tell the client which merchant it is actually watching (handles the "default" alias).
        yield f'event: ready\ndata: {{"merchant_id": "{merchant.id}"}}\n\n'
        try:
            async for event in bus.subscribe(merchant.id):
                if await request.is_disconnected():
                    break
                yield event.to_sse()
        except Exception:  # pragma: no cover - client disconnects are normal
            logger.debug("sse stream ended for merchant=%s", merchant.id)

    return StreamingResponse(
        publisher(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # Vite's dev proxy and most reverse proxies buffer without this.
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/events/{merchant_id}/ping", summary="Emit a test event (development aid)")
async def ping(merchant_id: str, session: DbSession) -> dict[str, object]:
    """Publish a heartbeat so a developer can confirm the stream is wired end to end."""
    merchant = resolve_merchant(merchant_id, session)
    bus = get_event_bus()
    bus.publish(merchant.id, EventName.HEARTBEAT, {"source": "ping"})
    return {"ok": True, "subscribers": bus.subscriber_count(merchant.id)}
