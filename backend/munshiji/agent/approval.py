"""The human-in-the-loop gate.

Nothing leaves the building without the merchant saying yes. This module owns the
:class:`ActionStatus` state machine, the outbound rate limits, and the audit trail — deliberately
separated from the agent loop so the safety rules cannot be bypassed by a prompt (SPEC.md §2.4).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from munshiji.clock import now_utc
from munshiji.config import get_settings
from munshiji.db.enums import TERMINAL_ACTION_STATUSES, ActionStatus
from munshiji.db.models import ActionOutcome, ActionRequest, KhataEntry
from munshiji.errors import InvalidStateTransitionError, NotFoundError, RateLimitedError
from munshiji.logging import get_logger
from munshiji.providers.actions import ActionDispatch, ActionProvider

__all__ = [
    "ALLOWED_TRANSITIONS",
    "OUTBOUND_TOOLS",
    "WRITE_TOOLS",
    "approve",
    "check_outbound_budget",
    "create_request",
    "execute_action",
    "expire_stale",
    "filter_reminder_cooldown",
    "latest_pending",
    "pending_for_conversation",
    "reject",
]

logger = get_logger(__name__)

#: Tools that mutate the world and therefore require explicit approval.
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "send_winback_offer",
        "send_udhaar_reminder",
        "create_payment_link",
        "draft_restock_order",
        "schedule_followup",
        "save_merchant_note",
    }
)

#: The subset that actually contacts a customer — these consume the daily outbound budget.
OUTBOUND_TOOLS: frozenset[str] = frozenset(
    {"send_winback_offer", "send_udhaar_reminder", "create_payment_link"}
)

ALLOWED_TRANSITIONS: dict[ActionStatus, frozenset[ActionStatus]] = {
    ActionStatus.DRAFT: frozenset({ActionStatus.PENDING_APPROVAL, ActionStatus.EXPIRED}),
    ActionStatus.PENDING_APPROVAL: frozenset(
        {ActionStatus.APPROVED, ActionStatus.REJECTED, ActionStatus.EXPIRED}
    ),
    ActionStatus.APPROVED: frozenset({ActionStatus.EXECUTING, ActionStatus.EXPIRED}),
    ActionStatus.EXECUTING: frozenset({ActionStatus.EXECUTED, ActionStatus.FAILED}),
    ActionStatus.EXECUTED: frozenset(),
    ActionStatus.REJECTED: frozenset(),
    ActionStatus.FAILED: frozenset(),
    ActionStatus.EXPIRED: frozenset(),
}


def _transition(action: ActionRequest, to_status: ActionStatus) -> None:
    """Move an action to ``to_status`` or refuse, loudly."""
    allowed = ALLOWED_TRANSITIONS.get(action.status, frozenset())
    if to_status not in allowed:
        raise InvalidStateTransitionError(
            f"cannot move action {action.id} from {action.status.value} to {to_status.value}",
            action_id=action.id,
            current=action.status.value,
            requested=to_status.value,
        )
    action.status = to_status


# ── Creation ────────────────────────────────────────────────────────────────


def create_request(
    session: Session,
    *,
    merchant_id: str,
    tool_name: str,
    params: dict[str, Any],
    summary_en: str = "",
    summary_hi: str = "",
    target_count: int = 0,
    estimated_impact_paise: int = 0,
    estimated_cost_paise: int = 0,
    insight_id: str | None = None,
    conversation_id: str | None = None,
) -> ActionRequest:
    """Record a proposed action awaiting the merchant's yes.

    This is the *only* way a write tool may come into existence.
    """
    action = ActionRequest(
        merchant_id=merchant_id,
        tool_name=tool_name,
        params=params,
        summary_en=summary_en,
        summary_hi=summary_hi,
        status=ActionStatus.PENDING_APPROVAL,
        target_count=target_count,
        estimated_impact_paise=estimated_impact_paise,
        estimated_cost_paise=estimated_cost_paise,
        insight_id=insight_id,
        conversation_id=conversation_id,
    )
    session.add(action)
    session.flush()
    logger.info("action proposed id=%s tool=%s targets=%d", action.id, tool_name, target_count)
    return action


# ── Decisions ───────────────────────────────────────────────────────────────


def approve(session: Session, action_id: str, *, approved_by: str = "merchant") -> ActionRequest:
    """Record the merchant's consent. Does not execute — see :func:`execute_action`."""
    action = session.get(ActionRequest, action_id)
    if action is None:
        raise NotFoundError(f"action {action_id!r} not found", action_id=action_id)
    _transition(action, ActionStatus.APPROVED)
    action.decided_at = now_utc()
    action.result = {**(action.result or {}), "approved_by": approved_by}
    session.flush()
    logger.info("action approved id=%s by=%s", action.id, approved_by)
    return action


def reject(session: Session, action_id: str, *, reason: str = "") -> ActionRequest:
    """Record a refusal. The agent must not re-pitch a rejected action in the same conversation."""
    action = session.get(ActionRequest, action_id)
    if action is None:
        raise NotFoundError(f"action {action_id!r} not found", action_id=action_id)
    _transition(action, ActionStatus.REJECTED)
    action.decided_at = now_utc()
    if reason:
        action.result = {**(action.result or {}), "reason": reason}
    session.flush()
    logger.info("action rejected id=%s reason=%s", action.id, reason or "-")
    return action


def latest_pending(session: Session, merchant_id: str) -> ActionRequest | None:
    """The most recent action still awaiting a decision."""
    stmt = (
        select(ActionRequest)
        .where(
            ActionRequest.merchant_id == merchant_id,
            ActionRequest.status == ActionStatus.PENDING_APPROVAL,
        )
        .order_by(ActionRequest.requested_at.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def pending_for_conversation(session: Session, conversation_id: str) -> ActionRequest | None:
    """The pending action raised inside one conversation — what 'haan' refers to."""
    stmt = (
        select(ActionRequest)
        .where(
            ActionRequest.conversation_id == conversation_id,
            ActionRequest.status == ActionStatus.PENDING_APPROVAL,
        )
        .order_by(ActionRequest.requested_at.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def expire_stale(session: Session, merchant_id: str, *, older_than_hours: int = 12) -> int:
    """Expire approvals the merchant never answered, so they cannot fire much later."""
    cutoff = now_utc() - timedelta(hours=older_than_hours)
    stale = session.scalars(
        select(ActionRequest).where(
            ActionRequest.merchant_id == merchant_id,
            ActionRequest.status == ActionStatus.PENDING_APPROVAL,
            ActionRequest.requested_at < cutoff,
        )
    ).all()
    for action in stale:
        _transition(action, ActionStatus.EXPIRED)
        action.decided_at = now_utc()
    if stale:
        session.flush()
        logger.info("expired %d stale approvals for merchant=%s", len(stale), merchant_id)
    return len(stale)


# ── Rate limits ─────────────────────────────────────────────────────────────


def outbound_sent_today(session: Session, merchant_id: str) -> int:
    """How many customer messages this merchant has already sent today (UTC-day window)."""
    since = now_utc() - timedelta(hours=24)
    total = session.scalar(
        select(func.coalesce(func.sum(ActionOutcome.value_num), 0))
        .select_from(ActionOutcome)
        .join(ActionRequest, ActionRequest.id == ActionOutcome.action_id)
        .where(
            ActionRequest.merchant_id == merchant_id,
            ActionRequest.tool_name.in_(list(OUTBOUND_TOOLS)),
            ActionOutcome.metric == "messages_sent",
            ActionOutcome.observed_at >= since,
        )
    )
    return int(total or 0)


def check_outbound_budget(session: Session, merchant_id: str, requested: int) -> None:
    """Refuse a send that would exceed the daily outbound cap."""
    settings = get_settings()
    already = outbound_sent_today(session, merchant_id)
    if already + requested > settings.max_outbound_per_day:
        raise RateLimitedError(
            "daily outbound message limit reached",
            merchant_id=merchant_id,
            already_sent=already,
            requested=requested,
            limit=settings.max_outbound_per_day,
        )


def filter_reminder_cooldown(
    session: Session, khata_ids: Sequence[str], *, cooldown_days: int | None = None
) -> tuple[list[str], list[str]]:
    """Split khata entries into (may remind, in cooldown).

    A customer must not be chased about the same entry more than once a week.
    """
    if not khata_ids:
        return [], []
    days = cooldown_days if cooldown_days is not None else get_settings().reminder_cooldown_days
    cutoff = now_utc() - timedelta(days=days)
    entries = session.scalars(select(KhataEntry).where(KhataEntry.id.in_(list(khata_ids)))).all()
    allowed: list[str] = []
    blocked: list[str] = []
    for entry in entries:
        if entry.last_reminder_at is not None and entry.last_reminder_at >= cutoff:
            blocked.append(entry.id)
        else:
            allowed.append(entry.id)
    return allowed, blocked


# ── Execution ───────────────────────────────────────────────────────────────


async def execute_action(
    session: Session,
    action: ActionRequest,
    provider: ActionProvider,
    dispatch: ActionDispatch,
) -> ActionRequest:
    """Execute an approved action through the provider, recording the outcome either way.

    Raises :class:`InvalidStateTransitionError` if the action was never approved — the gate is
    enforced here, not in the caller.

    Each state change is committed before control leaves for the provider. Two reasons: the audit
    trail must survive a crash mid-send, and the action providers open their own sessions, so
    holding this transaction open across the call would deadlock SQLite against itself.
    """
    _transition(action, ActionStatus.EXECUTING)
    session.commit()

    try:
        result = await provider.dispatch(dispatch)
    except Exception as exc:
        action.status = ActionStatus.FAILED
        action.error = f"{type(exc).__name__}: {exc}"[:400]
        action.executed_at = now_utc()
        session.commit()
        logger.exception("action execution failed id=%s", action.id)
        return action

    action.provider = result.provider
    action.result = {**(action.result or {}), **result.as_dict()}
    action.executed_at = now_utc()
    _transition(action, ActionStatus.EXECUTED if result.ok else ActionStatus.FAILED)
    if not result.ok:
        action.error = result.message[:400]
    session.commit()
    logger.info(
        "action executed id=%s ok=%s delivered=%d failed=%d via=%s",
        action.id,
        result.ok,
        result.delivered_count,
        result.failed_count,
        result.provider,
    )
    return action


def is_terminal(action: ActionRequest) -> bool:
    """Whether an action can no longer change state."""
    return action.status in TERMINAL_ACTION_STATUSES
