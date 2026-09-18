"""ORM models - the merchant's world as MunshiJi sees it.

Conventions (SPEC.md section 4):
* Primary keys are prefixed sortable strings from :mod:`munshiji.ids`.
* Money is ``int`` paise, never float. Column names end in ``_paise``.
* Timestamps are timezone-aware and stored in UTC; display happens in IST. The
  :class:`~munshiji.db.types.UtcDateTime` column type guarantees they come back aware even from
  SQLite, which has no native timestamp type.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from munshiji.clock import now_utc
from munshiji.db.base import Base
from munshiji.db.enums import (
    ActionStatus,
    Channel,
    ConversationChannel,
    CustomerSegment,
    InsightKind,
    KhataStatus,
    MemoryKind,
    PaymentMethod,
    Severity,
    TurnRole,
)
from munshiji.db.types import UtcDateTime
from munshiji.ids import new_id

__all__ = [
    "ActionOutcome",
    "ActionRequest",
    "Conversation",
    "Customer",
    "Insight",
    "KhataEntry",
    "MemoryEdge",
    "MemoryNode",
    "Merchant",
    "Product",
    "Transaction",
    "TransactionItem",
    "Turn",
]


def _enum(enum_cls: type, **kwargs: Any) -> SAEnum:
    """Store enums by their string *value* (not member name) for readable rows."""
    return SAEnum(
        enum_cls,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
        **kwargs,
    )


def _pk(prefix: str) -> Mapped[str]:
    return mapped_column(String(40), primary_key=True, default=lambda: new_id(prefix))


def _created() -> Mapped[datetime]:
    return mapped_column(UtcDateTime, default=now_utc, nullable=False, index=True)


# ---------------------------------------------------------------------------
# Merchant and catalogue
# ---------------------------------------------------------------------------


class Merchant(Base):
    """The shop owner MunshiJi works for."""

    __tablename__ = "merchants"

    id: Mapped[str] = _pk("mer")
    owner_name: Mapped[str] = mapped_column(String(120), nullable=False)
    shop_name: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str] = mapped_column(String(60), default="kirana", nullable=False)
    city: Mapped[str] = mapped_column(String(80), default="Delhi", nullable=False)
    locality: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    language: Mapped[str] = mapped_column(String(12), default="hi-IN", nullable=False)
    phone: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    #: Salted digest of the demo login password (see ``munshiji.security``). Empty = no login.
    password_hash: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    soundbox_id: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    opened_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    monthly_rent_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    business_hours_start: Mapped[int] = mapped_column(Integer, default=7, nullable=False)
    business_hours_end: Mapped[int] = mapped_column(Integer, default=22, nullable=False)
    created_at: Mapped[datetime] = _created()

    customers: Mapped[list[Customer]] = relationship(
        back_populates="merchant", cascade="all, delete-orphan"
    )
    products: Mapped[list[Product]] = relationship(
        back_populates="merchant", cascade="all, delete-orphan"
    )
    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="merchant", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Merchant {self.id} {self.shop_name!r}>"


class Customer(Base):
    """A named buyer. Walk-ins are represented by transactions with ``customer_id = NULL``."""

    __tablename__ = "customers"
    __table_args__ = (
        Index("ix_customers_merchant_last_seen", "merchant_id", "last_seen_at"),
        Index("ix_customers_merchant_khata", "merchant_id", "is_khata_customer"),
    )

    id: Mapped[str] = _pk("cus")
    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    first_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True, index=True)
    txn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_spend_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_khata_customer: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    segment: Mapped[CustomerSegment | None] = mapped_column(_enum(CustomerSegment), nullable=True)
    tags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = _created()

    merchant: Mapped[Merchant] = relationship(back_populates="customers")
    transactions: Mapped[list[Transaction]] = relationship(back_populates="customer")
    khata_entries: Mapped[list[KhataEntry]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Customer {self.id} {self.name!r}>"


class Product(Base):
    """One SKU on the merchant's shelf."""

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("merchant_id", "sku", name="uq_products_merchant_sku"),
        Index("ix_products_merchant_category", "merchant_id", "category"),
    )

    id: Mapped[str] = _pk("prd")
    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sku: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    name_hi: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    unit: Mapped[str] = mapped_column(String(20), default="pc", nullable=False)
    cost_price_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    sell_price_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    stock_qty: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reorder_level: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_perishable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    shelf_life_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_restocked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    created_at: Mapped[datetime] = _created()

    merchant: Mapped[Merchant] = relationship(back_populates="products")

    @property
    def margin_paise(self) -> int:
        """Gross margin per unit, in paise."""
        return self.sell_price_paise - self.cost_price_paise

    @property
    def stock_value_paise(self) -> int:
        """Capital tied up in this SKU at cost."""
        return int(round(self.stock_qty * self.cost_price_paise))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Product {self.sku} {self.name!r} stock={self.stock_qty}>"


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------


class Transaction(Base):
    """One sale at the counter."""

    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_txn_merchant_occurred", "merchant_id", "occurred_at"),
        Index("ix_txn_customer_occurred", "customer_id", "occurred_at"),
    )

    id: Mapped[str] = _pk("txn")
    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[str | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    payment_method: Mapped[PaymentMethod] = mapped_column(
        _enum(PaymentMethod), default=PaymentMethod.UPI, nullable=False
    )
    channel: Mapped[Channel] = mapped_column(_enum(Channel), default=Channel.SHOP, nullable=False)
    is_return: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = _created()

    merchant: Mapped[Merchant] = relationship(back_populates="transactions")
    customer: Mapped[Customer | None] = relationship(back_populates="transactions")
    items: Mapped[list[TransactionItem]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Transaction {self.id} {self.amount_paise}p at {self.occurred_at}>"


class TransactionItem(Base):
    """A line on a sale - the basis for all inventory analytics."""

    __tablename__ = "transaction_items"
    __table_args__ = (Index("ix_items_product", "product_id"),)

    id: Mapped[str] = _pk("tif")
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    qty: Mapped[float] = mapped_column(Float, nullable=False)
    unit_price_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cost_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    line_total_paise: Mapped[int] = mapped_column(Integer, nullable=False)

    transaction: Mapped[Transaction] = relationship(back_populates="items")
    product: Mapped[Product] = relationship()


class KhataEntry(Base):
    """One udhaar (informal credit) entry in the merchant's ledger."""

    __tablename__ = "khata_entries"
    __table_args__ = (
        Index("ix_khata_merchant_status", "merchant_id", "status"),
        Index("ix_khata_customer_status", "customer_id", "status"),
    )

    id: Mapped[str] = _pk("kht")
    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[str] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    paid_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    status: Mapped[KhataStatus] = mapped_column(
        _enum(KhataStatus), default=KhataStatus.OPEN, nullable=False
    )
    reminders_sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_reminder_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    note: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    created_at: Mapped[datetime] = _created()

    customer: Mapped[Customer] = relationship(back_populates="khata_entries")

    @property
    def outstanding_paise(self) -> int:
        """Amount still owed on this entry."""
        return max(0, self.amount_paise - self.paid_paise)


# ---------------------------------------------------------------------------
# Intelligence and actions
# ---------------------------------------------------------------------------


class Insight(Base):
    """A ranked, bilingual finding produced by an insight engine."""

    __tablename__ = "insights"
    __table_args__ = (
        Index("ix_insights_merchant_status_score", "merchant_id", "status", "score"),
        Index("ix_insights_merchant_kind", "merchant_id", "kind"),
    )

    id: Mapped[str] = _pk("ins")
    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[InsightKind] = mapped_column(_enum(InsightKind), nullable=False)
    severity: Mapped[Severity] = mapped_column(
        _enum(Severity), default=Severity.MEDIUM, nullable=False
    )
    title_en: Mapped[str] = mapped_column(String(200), nullable=False)
    title_hi: Mapped[str] = mapped_column(String(200), nullable=False)
    body_en: Mapped[str] = mapped_column(Text, nullable=False)
    body_hi: Mapped[str] = mapped_column(Text, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    suggested_tool: Mapped[str | None] = mapped_column(String(60), nullable=True)
    suggested_params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    impact_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.7, nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), default="open", nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(120), default="", nullable=False, index=True)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Insight {self.kind} score={self.score:.1f} {self.title_en!r}>"


class ActionRequest(Base):
    """An outbound action proposed by the agent, gated on merchant approval."""

    __tablename__ = "action_requests"
    __table_args__ = (Index("ix_actions_merchant_status", "merchant_id", "status"),)

    id: Mapped[str] = _pk("act")
    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    insight_id: Mapped[str | None] = mapped_column(
        ForeignKey("insights.id", ondelete="SET NULL"), nullable=True
    )
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    tool_name: Mapped[str] = mapped_column(String(60), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    summary_en: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    summary_hi: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    status: Mapped[ActionStatus] = mapped_column(
        _enum(ActionStatus), default=ActionStatus.PENDING_APPROVAL, nullable=False
    )
    target_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_impact_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: What sending this actually costs (see :mod:`munshiji.economics`). Stored alongside the
    #: expected return so the audit trail records the trade the merchant was offered, not just
    #: the upside we pitched.
    estimated_cost_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    requested_at: Mapped[datetime] = _created()
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str] = mapped_column(String(400), default="", nullable=False)
    provider: Mapped[str] = mapped_column(String(16), default="local", nullable=False)

    outcomes: Mapped[list[ActionOutcome]] = relationship(
        back_populates="action", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ActionRequest {self.id} {self.tool_name} {self.status}>"


class ActionOutcome(Base):
    """A measured result of an executed action - what makes MunshiJi accountable."""

    __tablename__ = "action_outcomes"

    id: Mapped[str] = _pk("out")
    action_id: Mapped[str] = mapped_column(
        ForeignKey("action_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metric: Mapped[str] = mapped_column(String(60), nullable=False)
    value_num: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    observed_at: Mapped[datetime] = _created()

    action: Mapped[ActionRequest] = relationship(back_populates="outcomes")


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------


class Conversation(Base):
    """One session between the merchant and MunshiJi."""

    __tablename__ = "conversations"

    id: Mapped[str] = _pk("cnv")
    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[ConversationChannel] = mapped_column(
        _enum(ConversationChannel), default=ConversationChannel.VOICE, nullable=False
    )
    language: Mapped[str] = mapped_column(String(12), default="hi-IN", nullable=False)
    started_at: Mapped[datetime] = _created()
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)

    turns: Mapped[list[Turn]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Turn.seq",
    )


class Turn(Base):
    """One utterance in a conversation (merchant, MunshiJi, or a tool result)."""

    __tablename__ = "turns"
    __table_args__ = (Index("ix_turns_conversation_seq", "conversation_id", "seq"),)

    id: Mapped[str] = _pk("trn")
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[TurnRole] = mapped_column(_enum(TurnRole), nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    text_display: Mapped[str] = mapped_column(Text, default="", nullable=False)
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    audio_path: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider: Mapped[str] = mapped_column(String(16), default="local", nullable=False)
    created_at: Mapped[datetime] = _created()

    conversation: Mapped[Conversation] = relationship(back_populates="turns")


# ---------------------------------------------------------------------------
# Memory graph (local Cognee-equivalent store)
# ---------------------------------------------------------------------------


class MemoryNode(Base):
    """An entity in the merchant's knowledge graph."""

    __tablename__ = "memory_nodes"
    __table_args__ = (
        UniqueConstraint("merchant_id", "kind", "key", name="uq_memory_node_identity"),
        Index("ix_memory_nodes_merchant_kind", "merchant_id", "kind"),
    )

    id: Mapped[str] = _pk("mnd")
    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[MemoryKind] = mapped_column(_enum(MemoryKind), nullable=False)
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    attrs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=now_utc, onupdate=now_utc, nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MemoryNode {self.kind}:{self.key}>"


class MemoryEdge(Base):
    """A typed relationship between two memory nodes."""

    __tablename__ = "memory_edges"
    __table_args__ = (
        Index("ix_memory_edges_src", "merchant_id", "src_id"),
        Index("ix_memory_edges_dst", "merchant_id", "dst_id"),
        UniqueConstraint("src_id", "dst_id", "rel", name="uq_memory_edge_identity"),
    )

    id: Mapped[str] = _pk("med")
    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    src_id: Mapped[str] = mapped_column(
        ForeignKey("memory_nodes.id", ondelete="CASCADE"), nullable=False
    )
    dst_id: Mapped[str] = mapped_column(
        ForeignKey("memory_nodes.id", ondelete="CASCADE"), nullable=False
    )
    rel: Mapped[str] = mapped_column(String(40), nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    attrs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = _created()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MemoryEdge {self.src_id} -{self.rel}-> {self.dst_id}>"
