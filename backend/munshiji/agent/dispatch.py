"""Turn an approved :class:`ActionRequest` into something the action layer can execute.

Responsibility split: this module resolves *who* is being contacted and the facts about them
(name, phone, what they owe, how long they have been away). Rendering the actual copy is left to
the provider, which calls :mod:`munshiji.messaging` per recipient — personalised text cannot be
rendered once for a whole audience.

An action arrives here only after :mod:`munshiji.agent.approval` has recorded a yes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from munshiji.clock import days_between, ist_date_of, now_ist
from munshiji.db.enums import KhataStatus
from munshiji.db.models import ActionRequest, Customer, KhataEntry, Merchant
from munshiji.logging import get_logger
from munshiji.messaging import select_tone
from munshiji.providers.actions import ActionDispatch

__all__ = ["build_dispatch", "resolve_customer_targets", "resolve_khata_targets"]

logger = get_logger(__name__)


def _idempotency_key(action: ActionRequest) -> str:
    """Stable per action, so a retried dispatch cannot double-send."""
    return f"{action.merchant_id}:{action.id}:{action.tool_name}"


def _customers_by_id(session: Session, customer_ids: list[str]) -> dict[str, Customer]:
    if not customer_ids:
        return {}
    rows = session.scalars(select(Customer).where(Customer.id.in_(customer_ids))).all()
    return {row.id: row for row in rows}


def resolve_customer_targets(
    session: Session,
    customer_ids: list[str],
    *,
    as_of: datetime,
) -> list[dict[str, Any]]:
    """Facts about each recipient of a customer-facing message."""
    found = _customers_by_id(session, customer_ids)
    targets: list[dict[str, Any]] = []
    for customer_id in customer_ids:
        customer = found.get(customer_id)
        if customer is None:
            logger.warning("dropping unknown customer %s from dispatch", customer_id)
            continue
        last_visit_days = days_between(customer.last_seen_at, as_of) if customer.last_seen_at else 0
        targets.append(
            {
                "customer_id": customer.id,
                "name": customer.name,
                "phone": customer.phone,
                "last_visit_days": max(0, last_visit_days),
                "segment": customer.segment.value if customer.segment else "",
                "avg_ticket_paise": (
                    int(customer.total_spend_paise / customer.txn_count)
                    if customer.txn_count
                    else 0
                ),
            }
        )
    return targets


def resolve_khata_targets(
    session: Session,
    *,
    merchant_id: str,
    khata_ids: list[str] | None = None,
    customer_ids: list[str] | None = None,
    as_of: datetime,
) -> list[dict[str, Any]]:
    """Open credit entries to chase, with the tone each one warrants.

    Accepts either explicit entry ids or a customer list (in which case every open entry for those
    customers is included). Settled entries are silently skipped — chasing a paid khata is the
    single worst failure mode this feature has.
    """
    stmt = select(KhataEntry).where(
        KhataEntry.merchant_id == merchant_id,
        KhataEntry.status.in_([KhataStatus.OPEN, KhataStatus.PARTIAL]),
    )
    if khata_ids:
        stmt = stmt.where(KhataEntry.id.in_(khata_ids))
    elif customer_ids:
        stmt = stmt.where(KhataEntry.customer_id.in_(customer_ids))
    else:
        return []

    entries = list(session.scalars(stmt.order_by(KhataEntry.opened_at)).all())
    customers = _customers_by_id(session, [entry.customer_id for entry in entries])
    today = ist_date_of(as_of)

    targets: list[dict[str, Any]] = []
    for entry in entries:
        customer = customers.get(entry.customer_id)
        if customer is None:
            continue
        reference = entry.due_at or entry.opened_at
        days_overdue = max(0, (today - ist_date_of(reference)).days)
        outstanding = entry.outstanding_paise
        if outstanding <= 0:
            continue
        # Reliability is refined by the credit engine when it proposes the action; absent that,
        # a neutral prior keeps the tone in the middle of the ladder rather than guessing harshly.
        reliability = float((customer.tags or {}).get("reliability", 0.5))
        targets.append(
            {
                "customer_id": customer.id,
                "khata_entry_id": entry.id,
                "name": customer.name,
                "phone": customer.phone,
                "amount_paise": outstanding,
                "days_overdue": days_overdue,
                "tone": select_tone(days_overdue, reliability, outstanding).value,
            }
        )
    return targets


def build_dispatch(ctx: Any, action: ActionRequest) -> ActionDispatch:
    """Assemble the execution payload for an approved action.

    ``ctx`` is a :class:`munshiji.agent.tools.base.ToolContext`; it is typed loosely here only to
    avoid a circular import between the tool layer and the loop.
    """
    session: Session = ctx.session
    merchant: Merchant = ctx.merchant
    as_of: datetime = getattr(ctx, "as_of", None) or now_ist()
    params: dict[str, Any] = dict(action.params or {})
    tool = action.tool_name

    targets: list[dict[str, Any]] = []

    # An engine may have handed us fully-formed target rows; trust them when present.
    supplied = params.get("targets")
    if isinstance(supplied, list) and supplied:
        targets = [dict(row) for row in supplied if isinstance(row, dict)]

    elif tool == "send_winback_offer":
        targets = resolve_customer_targets(
            session, list(params.get("customer_ids") or []), as_of=as_of
        )

    elif tool == "send_udhaar_reminder":
        targets = resolve_khata_targets(
            session,
            merchant_id=merchant.id,
            khata_ids=list(params.get("khata_ids") or []) or None,
            customer_ids=list(params.get("customer_ids") or []) or None,
            as_of=as_of,
        )

    elif tool == "create_payment_link":
        customer_id = params.get("customer_id")
        if customer_id:
            targets = resolve_customer_targets(session, [str(customer_id)], as_of=as_of)
            if targets:
                targets[0]["amount_paise"] = int(params.get("amount_paise") or 0)

    # draft_restock_order, schedule_followup and save_merchant_note have no customer recipients;
    # their payload travels entirely in `params`.

    params.setdefault("shop_name", merchant.shop_name)
    params.setdefault("language", "hi" if ctx.language.startswith("hi") else "en")

    return ActionDispatch(
        action_id=action.id,
        merchant_id=merchant.id,
        tool_name=tool,
        params=params,
        targets=targets,
        messages={},
        idempotency_key=_idempotency_key(action),
    )
