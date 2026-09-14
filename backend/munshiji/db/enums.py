"""Domain enumerations.

All are ``str``-valued so they serialise directly to JSON and store readably in SQLite.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "SEVERITY_WEIGHT",
    "TERMINAL_ACTION_STATUSES",
    "ActionStatus",
    "Channel",
    "ConversationChannel",
    "CustomerSegment",
    "InsightKind",
    "KhataStatus",
    "MemoryKind",
    "PaymentMethod",
    "Severity",
    "Tone",
    "TurnRole",
]


class PaymentMethod(str, Enum):
    UPI = "upi"
    CARD = "card"
    CASH = "cash"
    SOUNDBOX = "soundbox"
    WALLET = "wallet"


class Channel(str, Enum):
    SHOP = "shop"
    ONLINE = "online"
    PHONE = "phone"


class ConversationChannel(str, Enum):
    VOICE = "voice"
    TEXT = "text"
    WHATSAPP = "whatsapp"


class KhataStatus(str, Enum):
    """Lifecycle of one udhaar (credit) entry."""

    OPEN = "open"
    PARTIAL = "partial"
    SETTLED = "settled"
    WRITTEN_OFF = "written_off"


class InsightKind(str, Enum):
    COLLECTION_ANOMALY = "collection_anomaly"
    DORMANT_CUSTOMERS = "dormant_customers"
    DEAD_STOCK = "dead_stock"
    STOCKOUT_RISK = "stockout_risk"
    EXPIRY_RISK = "expiry_risk"
    UDHAAR_OVERDUE = "udhaar_overdue"
    FESTIVAL_PREP = "festival_prep"
    PEAK_HOUR = "peak_hour"
    MARGIN_LEAK = "margin_leak"
    PAYMENT_MIX = "payment_mix"
    NEW_CUSTOMER_DROP = "new_customer_drop"


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


#: Multiplier applied to an insight's impact when ranking the feed.
SEVERITY_WEIGHT: dict[Severity, float] = {
    Severity.INFO: 0.4,
    Severity.LOW: 0.6,
    Severity.MEDIUM: 1.0,
    Severity.HIGH: 1.5,
    Severity.CRITICAL: 2.2,
}


class ActionStatus(str, Enum):
    """State machine for an outbound action. Transitions enforced in ``agent/approval.py``."""

    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"
    EXPIRED = "expired"


TERMINAL_ACTION_STATUSES: frozenset[ActionStatus] = frozenset(
    {
        ActionStatus.REJECTED,
        ActionStatus.EXECUTED,
        ActionStatus.FAILED,
        ActionStatus.EXPIRED,
    }
)


class TurnRole(str, Enum):
    MERCHANT = "merchant"
    MUNSHI = "munshi"
    TOOL = "tool"
    SYSTEM = "system"


class Tone(str, Enum):
    """Reminder register. There is deliberately no tier harsher than FIRM (SPEC.md §2.4)."""

    GENTLE = "gentle"
    STANDARD = "standard"
    FIRM = "firm"


class MemoryKind(str, Enum):
    MERCHANT = "merchant"
    CUSTOMER = "customer"
    PRODUCT = "product"
    DAY = "day"
    ACTION = "action"
    INSIGHT = "insight"
    NOTE = "note"
    CONVERSATION = "conversation"


class CustomerSegment(str, Enum):
    """RFM-derived segments used across insights and win-back targeting."""

    CHAMPION = "champion"
    LOYAL = "loyal"
    REGULAR = "regular"
    OCCASIONAL = "occasional"
    NEW = "new"
    AT_RISK = "at_risk"
    DORMANT = "dormant"
    LOST = "lost"
