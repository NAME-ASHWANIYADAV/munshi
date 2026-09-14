"""The action queue — propose, approve, reject, and see what it earned.

Approving here is the same gate the voice path uses: it records consent, then executes through
:func:`munshiji.agent.approval.execute_action`, which refuses anything not in ``APPROVED``.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from munshiji.agent.approval import approve as approve_action
from munshiji.agent.approval import execute_action
from munshiji.agent.approval import reject as reject_action
from munshiji.agent.dispatch import build_dispatch
from munshiji.agent.tools.base import ToolContext
from munshiji.api.deps import CurrentMerchant, DbSession, Providers, resolve_merchant
from munshiji.clock import now_ist
from munshiji.db.enums import ActionStatus
from munshiji.events import EventName, get_event_bus
from munshiji.repositories.core import list_actions, require_action
from munshiji.schemas.action import ActionListOut, ActionOut, ApproveIn, RejectIn
from munshiji.schemas.common import Meta

router = APIRouter(tags=["actions"])


@router.get(
    "/actions/{merchant_id}",
    response_model=ActionListOut,
    summary="Action feed, newest first",
)
async def get_actions(
    merchant: CurrentMerchant,
    session: DbSession,
    limit: int = Query(default=30, ge=1, le=100),
    status: ActionStatus | None = Query(default=None),
) -> ActionListOut:
    """Everything MunshiJi has proposed, with decisions and measured outcomes."""
    actions = list_actions(session, merchant.id, statuses=[status] if status else None, limit=limit)
    pending = sum(1 for action in actions if action.status is ActionStatus.PENDING_APPROVAL)
    return ActionListOut(
        actions=[ActionOut.from_model(action) for action in actions],
        pending_count=pending,
        meta=Meta(extra={"count": len(actions)}),
    )


@router.post(
    "/actions/{action_id}/approve",
    response_model=ActionOut,
    summary="Approve a pending action and execute it",
)
async def approve(
    action_id: str, payload: ApproveIn, session: DbSession, providers: Providers
) -> ActionOut:
    """Record the merchant's consent, then run the action through the action provider."""
    action = require_action(session, action_id)
    merchant = resolve_merchant(action.merchant_id, session)

    approve_action(session, action.id, approved_by=payload.approved_by)
    get_event_bus().publish(
        merchant.id, EventName.ACTION_DECIDED, {"action_id": action.id, "status": "approved"}
    )

    ctx = ToolContext(
        session=session,
        merchant=merchant,
        providers=providers,
        as_of=now_ist(),
        language=merchant.language,
        conversation_id=action.conversation_id,
    )
    executed = await execute_action(session, action, providers.actions, build_dispatch(ctx, action))

    get_event_bus().publish(
        merchant.id,
        EventName.ACTION_EXECUTED,
        {
            "action_id": executed.id,
            "status": executed.status.value,
            "delivered": executed.result.get("delivered_count", 0),
        },
    )
    return ActionOut.from_model(executed)


@router.post(
    "/actions/{action_id}/reject",
    response_model=ActionOut,
    summary="Decline a pending action",
)
async def reject(action_id: str, payload: RejectIn, session: DbSession) -> ActionOut:
    """Decline a proposal. MunshiJi will not re-pitch it in the same conversation."""
    action = require_action(session, action_id)
    rejected = reject_action(session, action.id, reason=payload.reason)
    get_event_bus().publish(
        action.merchant_id,
        EventName.ACTION_DECIDED,
        {"action_id": action.id, "status": "rejected"},
    )
    return ActionOut.from_model(rejected)
