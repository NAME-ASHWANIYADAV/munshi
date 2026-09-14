"""The human-in-the-loop gate.

These are the safety tests: nothing outbound may execute without an explicit approval, no action
may skip a state, and the rate limits must hold even when the agent asks nicely.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from munshiji.agent import approval
from munshiji.clock import now_utc
from munshiji.db.enums import ActionStatus, KhataStatus
from munshiji.db.models import ActionOutcome, ActionRequest, Customer, KhataEntry, Merchant
from munshiji.errors import InvalidStateTransitionError, NotFoundError, RateLimitedError
from munshiji.providers.actions import ActionDispatch, ActionResult


class _StubProvider:
    """Minimal ActionProvider double that records what it was asked to do."""

    name = "stub"
    mode = "local"

    def __init__(self, *, ok: bool = True, raises: BaseException | None = None) -> None:
        self.ok = ok
        self.raises = raises
        self.calls: list[ActionDispatch] = []

    async def dispatch(self, dispatch: ActionDispatch) -> ActionResult:
        self.calls.append(dispatch)
        if self.raises is not None:
            raise self.raises
        return ActionResult(
            ok=self.ok,
            provider="local",
            message="sent" if self.ok else "gateway refused",
            delivered_count=2 if self.ok else 0,
            failed_count=0 if self.ok else 2,
        )

    async def health(self):  # pragma: no cover - unused here
        raise NotImplementedError


def _pending(
    session: Session, merchant: Merchant, tool: str = "send_winback_offer"
) -> ActionRequest:
    return approval.create_request(
        session,
        merchant_id=merchant.id,
        tool_name=tool,
        params={"customer_ids": ["cus_a", "cus_b"]},
        summary_en="Send offer to 2 customers",
        summary_hi="2 ग्राहकों को ऑफ़र",
        target_count=2,
    )


# ── State machine ───────────────────────────────────────────────────────────


def test_new_requests_start_pending_not_approved(session: Session, merchant: Merchant) -> None:
    action = _pending(session, merchant)
    assert action.status is ActionStatus.PENDING_APPROVAL
    assert action.decided_at is None


def test_approve_then_execute_is_the_only_path(session: Session, merchant: Merchant) -> None:
    action = _pending(session, merchant)
    approval.approve(session, action.id)
    assert action.status is ActionStatus.APPROVED
    assert action.decided_at is not None


async def test_execution_without_approval_is_refused(session: Session, merchant: Merchant) -> None:
    """The gate is enforced in the execution path itself, not merely in the caller."""
    action = _pending(session, merchant)
    provider = _StubProvider()
    dispatch = ActionDispatch(
        action_id=action.id, merchant_id=merchant.id, tool_name=action.tool_name
    )

    with pytest.raises(InvalidStateTransitionError):
        await approval.execute_action(session, action, provider, dispatch)

    assert provider.calls == [], "provider must never be reached for an unapproved action"
    assert action.status is ActionStatus.PENDING_APPROVAL


async def test_full_happy_path(session: Session, merchant: Merchant) -> None:
    action = _pending(session, merchant)
    approval.approve(session, action.id)
    provider = _StubProvider()
    dispatch = ActionDispatch(
        action_id=action.id, merchant_id=merchant.id, tool_name=action.tool_name
    )

    result = await approval.execute_action(session, action, provider, dispatch)

    assert result.status is ActionStatus.EXECUTED
    assert result.executed_at is not None
    assert result.result["delivered_count"] == 2
    assert len(provider.calls) == 1


async def test_provider_failure_marks_failed_and_keeps_the_reason(
    session: Session, merchant: Merchant
) -> None:
    action = _pending(session, merchant)
    approval.approve(session, action.id)
    provider = _StubProvider(ok=False)
    dispatch = ActionDispatch(
        action_id=action.id, merchant_id=merchant.id, tool_name=action.tool_name
    )

    result = await approval.execute_action(session, action, provider, dispatch)

    assert result.status is ActionStatus.FAILED
    assert "gateway refused" in result.error


async def test_provider_exception_does_not_escape(session: Session, merchant: Merchant) -> None:
    """A vendor blowing up must fail the action, not the conversation."""
    action = _pending(session, merchant)
    approval.approve(session, action.id)
    provider = _StubProvider(raises=RuntimeError("connection reset"))
    dispatch = ActionDispatch(
        action_id=action.id, merchant_id=merchant.id, tool_name=action.tool_name
    )

    result = await approval.execute_action(session, action, provider, dispatch)

    assert result.status is ActionStatus.FAILED
    assert "connection reset" in result.error


def test_rejected_action_cannot_be_approved_afterwards(
    session: Session, merchant: Merchant
) -> None:
    action = _pending(session, merchant)
    approval.reject(session, action.id, reason="rehne do")
    assert action.status is ActionStatus.REJECTED
    assert action.result["reason"] == "rehne do"

    with pytest.raises(InvalidStateTransitionError):
        approval.approve(session, action.id)


def test_double_approval_is_refused(session: Session, merchant: Merchant) -> None:
    action = _pending(session, merchant)
    approval.approve(session, action.id)
    with pytest.raises(InvalidStateTransitionError):
        approval.approve(session, action.id)


def test_unknown_action_raises_not_found(session: Session) -> None:
    with pytest.raises(NotFoundError):
        approval.approve(session, "act_does_not_exist")


def test_every_status_has_a_transition_entry() -> None:
    """A new ActionStatus must not silently become a dead end."""
    for status in ActionStatus:
        assert status in approval.ALLOWED_TRANSITIONS


# ── Pending lookup ──────────────────────────────────────────────────────────


def test_latest_pending_returns_the_newest(session: Session, merchant: Merchant) -> None:
    first = _pending(session, merchant)
    second = _pending(session, merchant, tool="send_udhaar_reminder")
    # Make ordering unambiguous regardless of clock resolution.
    second.requested_at = first.requested_at + timedelta(seconds=5)
    session.flush()

    assert approval.latest_pending(session, merchant.id).id == second.id

    approval.reject(session, second.id)
    assert approval.latest_pending(session, merchant.id).id == first.id


def test_expire_stale_only_touches_old_pending_actions(
    session: Session, merchant: Merchant
) -> None:
    fresh = _pending(session, merchant)
    stale = _pending(session, merchant)
    stale.requested_at = now_utc() - timedelta(hours=30)
    session.flush()

    expired = approval.expire_stale(session, merchant.id, older_than_hours=12)

    assert expired == 1
    assert stale.status is ActionStatus.EXPIRED
    assert fresh.status is ActionStatus.PENDING_APPROVAL


# ── Rate limits ─────────────────────────────────────────────────────────────


def _record_sent(session: Session, merchant: Merchant, count: int) -> None:
    action = _pending(session, merchant)
    approval.approve(session, action.id)
    action.status = ActionStatus.EXECUTED
    session.add(ActionOutcome(action_id=action.id, metric="messages_sent", value_num=float(count)))
    session.flush()


def test_outbound_budget_blocks_the_send_that_would_exceed_it(
    session: Session, merchant: Merchant
) -> None:
    _record_sent(session, merchant, 45)
    assert approval.outbound_sent_today(session, merchant.id) == 45

    approval.check_outbound_budget(session, merchant.id, requested=5)  # exactly at the cap: fine

    with pytest.raises(RateLimitedError) as excinfo:
        approval.check_outbound_budget(session, merchant.id, requested=6)
    assert excinfo.value.context["limit"] == 50


def test_yesterdays_sends_do_not_count_against_today(session: Session, merchant: Merchant) -> None:
    action = _pending(session, merchant)
    session.add(
        ActionOutcome(
            action_id=action.id,
            metric="messages_sent",
            value_num=48.0,
            observed_at=now_utc() - timedelta(days=2),
        )
    )
    session.flush()
    assert approval.outbound_sent_today(session, merchant.id) == 0


def test_reminder_cooldown_splits_entries(session: Session, merchant: Merchant) -> None:
    customer = Customer(merchant_id=merchant.id, name="Sunita Devi", phone="9810000001")
    session.add(customer)
    session.flush()

    recent = KhataEntry(
        merchant_id=merchant.id,
        customer_id=customer.id,
        amount_paise=50000,
        opened_at=now_utc() - timedelta(days=40),
        status=KhataStatus.OPEN,
        last_reminder_at=now_utc() - timedelta(days=2),
    )
    old = KhataEntry(
        merchant_id=merchant.id,
        customer_id=customer.id,
        amount_paise=70000,
        opened_at=now_utc() - timedelta(days=70),
        status=KhataStatus.OPEN,
        last_reminder_at=now_utc() - timedelta(days=20),
    )
    never = KhataEntry(
        merchant_id=merchant.id,
        customer_id=customer.id,
        amount_paise=30000,
        opened_at=now_utc() - timedelta(days=20),
        status=KhataStatus.OPEN,
    )
    session.add_all([recent, old, never])
    session.flush()

    allowed, blocked = approval.filter_reminder_cooldown(
        session, [recent.id, old.id, never.id], cooldown_days=7
    )

    assert set(allowed) == {old.id, never.id}
    assert blocked == [recent.id]


def test_write_and_outbound_tool_sets_are_consistent() -> None:
    """Every outbound tool is a write tool — otherwise it would skip the gate."""
    assert approval.OUTBOUND_TOOLS <= approval.WRITE_TOOLS
