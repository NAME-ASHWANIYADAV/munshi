"""What MunshiJi is allowed to send, and when.

The approval gate already asks the merchant before anything goes out. This module asks a
different question first: *may this be sent at all?* It is the difference between a system that
does what it is told and one that is safe to point at real customers.

Three rules, each from a different place:

**Quiet hours.** Reminders about money owed are collection contact, and the RBI's fair-practices
expectations for recovery are unambiguous that it happens at reasonable hours, not at night, so
reminders are refused outside :data:`QUIET_HOURS_START`-:data:`QUIET_HOURS_END` IST. The rule is
deliberately narrow: it binds the tools in :data:`QUIET_HOURS_TOOLS` and nothing else. A win-back
offer is ordinary shop marketing rather than debt collection, and is governed by consent instead.
Applying a recovery-conduct rule to every message would look thorough and be wrong.

**Consent.** Under the DPDP Act a shopkeeper holding customer purchase history may not simply
repurpose it into a marketing list. Promotional contact needs consent the customer gave and can
withdraw. A reminder about that customer's own outstanding balance stands on different ground -
it concerns a transaction they entered into - so it is gated on opt-out rather than opt-in.
Either way, a withdrawn consent stops everything.

**Template category.** WhatsApp requires an outbound message to be categorised honestly, and
dressing marketing up as a utility message to pay the cheaper rate is a policy violation, not a
clever optimisation. :mod:`munshiji.economics` assigns the category; this module refuses to let a
tool send under a category it is not entitled to.

None of this is legal advice and the thresholds are configurable. The point is that the refusals
are real, the merchant is told about them in words, and they are recorded.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time

from munshiji.clock import IST, now_ist
from munshiji.db.models import Customer
from munshiji.economics import TOOL_TEMPLATE, TemplateCategory

__all__ = [
    "CONSENT_TAG",
    "OPT_OUT_TAG",
    "QUIET_HOURS_END",
    "QUIET_HOURS_START",
    "QUIET_HOURS_TOOLS",
    "ComplianceDecision",
    "Refusal",
    "check_send_window",
    "consented_ids",
    "screen_recipients",
]

#: Collection contact is confined to these IST hours.
QUIET_HOURS_START: time = time(8, 0)
QUIET_HOURS_END: time = time(19, 0)

#: Tools whose messages are collection contact, and therefore bound by the hours above.
#:
#: Deliberately narrow. The fair-practices expectation being honoured here is about chasing money
#: owed, so it applies to reminders; a win-back offer is ordinary shop marketing and is governed
#: by consent instead. Applying the window to everything would look thorough and be wrong.
QUIET_HOURS_TOOLS: frozenset[str] = frozenset({"send_udhaar_reminder"})

#: Human-readable form of the window, for the sentence the merchant hears. Written out rather
#: than formatted, because platform strftime does not agree on how to drop a leading zero.
WINDOW_LABEL_EN = "8am-7pm IST"
WINDOW_LABEL_HI = "subah 8 se shaam 7"

#: Tag keys on :class:`~munshiji.db.models.Customer` carrying consent state. Tags rather than
#: columns because consent is evidence about a person, not a property of the shop's schema: a
#: real deployment keeps it in a ledger with a timestamp and a source, and this is the seam.
CONSENT_TAG = "marketing_consent"
OPT_OUT_TAG = "opted_out"


@dataclass(slots=True, frozen=True)
class Refusal:
    """One recipient MunshiJi declined to contact, and why."""

    customer_id: str
    name: str
    rule: str
    reason_en: str
    reason_hi: str


@dataclass(slots=True)
class ComplianceDecision:
    """Who may be contacted, who may not, and what to tell the merchant."""

    allowed: list[Customer] = field(default_factory=list)
    refused: list[Refusal] = field(default_factory=list)
    #: Set when the whole send is blocked regardless of recipient (quiet hours, wrong category).
    blocked_reason_en: str | None = None
    blocked_reason_hi: str | None = None

    @property
    def is_blocked(self) -> bool:
        return self.blocked_reason_en is not None

    @property
    def allowed_ids(self) -> list[str]:
        return [customer.id for customer in self.allowed]

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed_count": len(self.allowed),
            "refused_count": len(self.refused),
            "blocked": self.is_blocked,
            "blocked_reason_en": self.blocked_reason_en,
            "blocked_reason_hi": self.blocked_reason_hi,
            "refusals": [
                {
                    "customer_id": refusal.customer_id,
                    "name": refusal.name,
                    "rule": refusal.rule,
                    "reason_en": refusal.reason_en,
                    "reason_hi": refusal.reason_hi,
                }
                for refusal in self.refused
            ],
            "rules_applied": ["send_window", "consent", "template_category"],
        }

    def sentence(self, *, hindi: bool = True) -> str:
        """What the merchant hears about the recipients that were dropped."""
        if self.is_blocked:
            return (self.blocked_reason_hi if hindi else self.blocked_reason_en) or ""
        if not self.refused:
            return ""
        count = len(self.refused)
        if hindi:
            return f"{count} ko chhod diya — unki anumati nahi hai."
        return f"{count} skipped — no consent on file."


def check_send_window(
    tool_name: str, *, category: TemplateCategory | None = None, at: datetime | None = None
) -> tuple[str, str] | None:
    """Refuse the whole send if it is the wrong hour or the wrong category.

    Returns ``(reason_en, reason_hi)`` when blocked, ``None`` when the send may proceed.
    """
    declared = TOOL_TEMPLATE.get(tool_name, TemplateCategory.UTILITY)
    if category is not None and category is not declared:
        return (
            f"{tool_name} may only send as a {declared.value} message.",
            "Is sandesh ki shreni galat hai.",
        )

    if tool_name in QUIET_HOURS_TOOLS:
        # Convert before reading the hour. The window is IST because the customer is in India;
        # taking .time() off whatever zone the caller passed would silently apply the rule to
        # server-local hours, which on a UTC host is five and a half hours wrong.
        moment = (at or now_ist()).astimezone(IST).time()
        if not (QUIET_HOURS_START <= moment < QUIET_HOURS_END):
            return (
                f"Reminders only go out between {WINDOW_LABEL_EN}.",
                f"Yaad dilane ka sandesh {WINDOW_LABEL_HI} ke beech hi jaata hai.",
            )
    return None


def _tag_is_set(customer: Customer, key: str) -> bool:
    tags = customer.tags or {}
    return bool(tags.get(key))


def screen_recipients(
    tool_name: str, customers: Iterable[Customer], *, at: datetime | None = None
) -> ComplianceDecision:
    """Apply every rule to a candidate recipient list.

    Marketing needs consent on file. Utility messages about a customer's own balance need only
    the absence of an opt-out. A withdrawn consent blocks both.
    """
    decision = ComplianceDecision()

    blocked = check_send_window(tool_name, at=at)
    if blocked is not None:
        decision.blocked_reason_en, decision.blocked_reason_hi = blocked
        return decision

    category = TOOL_TEMPLATE.get(tool_name, TemplateCategory.UTILITY)
    for customer in customers:
        if _tag_is_set(customer, OPT_OUT_TAG):
            decision.refused.append(
                Refusal(
                    customer_id=customer.id,
                    name=customer.name,
                    rule="opted_out",
                    reason_en=f"{customer.name} has opted out of messages.",
                    reason_hi=f"{customer.name} ne sandesh band karwa diye hain.",
                )
            )
            continue

        if category is TemplateCategory.MARKETING and not _tag_is_set(customer, CONSENT_TAG):
            decision.refused.append(
                Refusal(
                    customer_id=customer.id,
                    name=customer.name,
                    rule="no_marketing_consent",
                    reason_en=f"No marketing consent on file for {customer.name}.",
                    reason_hi=f"{customer.name} ki prachar ke liye anumati nahi hai.",
                )
            )
            continue

        decision.allowed.append(customer)

    return decision


def consented_ids(customers: Sequence[Customer]) -> list[str]:
    """Ids that may receive marketing - handy for sizing an audience before proposing."""
    return [
        customer.id
        for customer in customers
        if _tag_is_set(customer, CONSENT_TAG) and not _tag_is_set(customer, OPT_OUT_TAG)
    ]
