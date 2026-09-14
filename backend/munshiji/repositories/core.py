"""Common lookups shared by every layer.

Heavier analytical queries live in ``repositories/analytics.py``; this module holds the plain
fetches that the agent, providers and API all need. Everything takes an explicit ``Session`` so
callers control the transaction boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from munshiji.clock import day_bounds_ist, ist_date_of, now_utc
from munshiji.db.enums import ActionStatus, KhataStatus
from munshiji.db.models import (
    ActionOutcome,
    ActionRequest,
    Conversation,
    Customer,
    Insight,
    KhataEntry,
    Merchant,
    Product,
    Transaction,
    Turn,
)
from munshiji.errors import NotFoundError

__all__ = [
    "add_turn",
    "day_collection_paise",
    "day_transaction_count",
    "first_merchant",
    "get_action",
    "get_customer",
    "get_customers",
    "get_insight",
    "get_merchant",
    "get_open_khata",
    "get_or_create_conversation",
    "get_product",
    "list_actions",
    "list_customers",
    "list_insights",
    "list_products",
    "next_turn_seq",
    "record_outcome",
    "require_action",
    "require_merchant",
]


# ── Merchant ────────────────────────────────────────────────────────────────


def get_merchant(session: Session, merchant_id: str) -> Merchant | None:
    return session.get(Merchant, merchant_id)


def require_merchant(session: Session, merchant_id: str) -> Merchant:
    merchant = session.get(Merchant, merchant_id)
    if merchant is None:
        raise NotFoundError(f"merchant {merchant_id!r} not found", merchant_id=merchant_id)
    return merchant


def first_merchant(session: Session) -> Merchant | None:
    """The demo database holds a single merchant; this is the convenience entry point."""
    return session.scalars(select(Merchant).order_by(Merchant.created_at).limit(1)).first()


# ── Customers ───────────────────────────────────────────────────────────────


def get_customer(session: Session, customer_id: str) -> Customer | None:
    return session.get(Customer, customer_id)


def get_customers(session: Session, customer_ids: Sequence[str]) -> list[Customer]:
    """Fetch many customers, preserving the order of ``customer_ids``."""
    if not customer_ids:
        return []
    found = {
        customer.id: customer
        for customer in session.scalars(
            select(Customer).where(Customer.id.in_(list(customer_ids)))
        ).all()
    }
    return [found[cid] for cid in customer_ids if cid in found]


def list_customers(
    session: Session,
    merchant_id: str,
    *,
    khata_only: bool = False,
    limit: int | None = None,
) -> list[Customer]:
    stmt = select(Customer).where(Customer.merchant_id == merchant_id)
    if khata_only:
        stmt = stmt.where(Customer.is_khata_customer.is_(True))
    stmt = stmt.order_by(Customer.total_spend_paise.desc())
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).all())


# ── Products ────────────────────────────────────────────────────────────────


def get_product(session: Session, product_id: str) -> Product | None:
    return session.get(Product, product_id)


def list_products(
    session: Session, merchant_id: str, *, category: str | None = None
) -> list[Product]:
    stmt = select(Product).where(Product.merchant_id == merchant_id)
    if category:
        stmt = stmt.where(Product.category == category)
    return list(session.scalars(stmt.order_by(Product.name)).all())


# ── Khata (udhaar) ──────────────────────────────────────────────────────────


def get_open_khata(session: Session, merchant_id: str) -> list[KhataEntry]:
    """Every unsettled credit entry, oldest first."""
    stmt = (
        select(KhataEntry)
        .where(
            KhataEntry.merchant_id == merchant_id,
            KhataEntry.status.in_([KhataStatus.OPEN, KhataStatus.PARTIAL]),
        )
        .order_by(KhataEntry.opened_at)
    )
    return list(session.scalars(stmt).all())


# ── Actions ─────────────────────────────────────────────────────────────────


def get_action(session: Session, action_id: str) -> ActionRequest | None:
    return session.get(ActionRequest, action_id)


def require_action(session: Session, action_id: str) -> ActionRequest:
    action = session.get(ActionRequest, action_id)
    if action is None:
        raise NotFoundError(f"action {action_id!r} not found", action_id=action_id)
    return action


def list_actions(
    session: Session,
    merchant_id: str,
    *,
    statuses: Sequence[ActionStatus] | None = None,
    limit: int = 50,
) -> list[ActionRequest]:
    stmt = select(ActionRequest).where(ActionRequest.merchant_id == merchant_id)
    if statuses:
        stmt = stmt.where(ActionRequest.status.in_(list(statuses)))
    stmt = stmt.order_by(ActionRequest.requested_at.desc()).limit(limit)
    return list(session.scalars(stmt).all())


def record_outcome(
    session: Session,
    action_id: str,
    metric: str,
    *,
    value_num: float | None = None,
    value_paise: int | None = None,
    note: str = "",
    observed_at: datetime | None = None,
) -> ActionOutcome:
    """Attach a measured result to an executed action."""
    outcome = ActionOutcome(
        action_id=action_id,
        metric=metric,
        value_num=value_num,
        value_paise=value_paise,
        note=note,
        observed_at=observed_at or now_utc(),
    )
    session.add(outcome)
    session.flush()
    return outcome


# ── Insights ────────────────────────────────────────────────────────────────


def get_insight(session: Session, insight_id: str) -> Insight | None:
    return session.get(Insight, insight_id)


def list_insights(
    session: Session,
    merchant_id: str,
    *,
    status: str | None = "open",
    limit: int = 12,
) -> list[Insight]:
    stmt = select(Insight).where(Insight.merchant_id == merchant_id)
    if status:
        stmt = stmt.where(Insight.status == status)
    stmt = stmt.order_by(Insight.score.desc(), Insight.created_at.desc()).limit(limit)
    return list(session.scalars(stmt).all())


# ── Conversation ────────────────────────────────────────────────────────────


def get_or_create_conversation(
    session: Session,
    merchant_id: str,
    conversation_id: str | None = None,
    *,
    channel: str = "voice",
    language: str = "hi-IN",
) -> Conversation:
    """Resume a conversation by id, or start a new one."""
    if conversation_id:
        existing = session.get(Conversation, conversation_id)
        if existing is not None:
            return existing
    conversation = Conversation(merchant_id=merchant_id, channel=channel, language=language)
    session.add(conversation)
    session.flush()
    return conversation


def next_turn_seq(session: Session, conversation_id: str) -> int:
    highest = session.scalar(
        select(func.max(Turn.seq)).where(Turn.conversation_id == conversation_id)
    )
    return int(highest or 0) + 1


def add_turn(
    session: Session,
    conversation_id: str,
    role: str,
    text: str,
    *,
    text_display: str = "",
    tool_calls: list[dict] | None = None,
    latency_ms: int = 0,
    provider: str = "local",
    audio_path: str = "",
) -> Turn:
    turn = Turn(
        conversation_id=conversation_id,
        seq=next_turn_seq(session, conversation_id),
        role=role,
        text=text,
        text_display=text_display or text,
        tool_calls=tool_calls or [],
        latency_ms=latency_ms,
        provider=provider,
        audio_path=audio_path,
    )
    session.add(turn)
    session.flush()
    return turn


# ── Small aggregates used in several places ─────────────────────────────────


def day_collection_paise(session: Session, merchant_id: str, day: datetime | object) -> int:
    """Total collected on one IST calendar day, in paise."""
    target = ist_date_of(day) if isinstance(day, datetime) else day
    start, end = day_bounds_ist(target)  # type: ignore[arg-type]
    total = session.scalar(
        select(func.coalesce(func.sum(Transaction.amount_paise), 0)).where(
            Transaction.merchant_id == merchant_id,
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
            Transaction.is_return.is_(False),
        )
    )
    return int(total or 0)


def day_transaction_count(session: Session, merchant_id: str, day: datetime | object) -> int:
    """Number of sales on one IST calendar day."""
    target = ist_date_of(day) if isinstance(day, datetime) else day
    start, end = day_bounds_ist(target)  # type: ignore[arg-type]
    count = session.scalar(
        select(func.count(Transaction.id)).where(
            Transaction.merchant_id == merchant_id,
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
            Transaction.is_return.is_(False),
        )
    )
    return int(count or 0)
