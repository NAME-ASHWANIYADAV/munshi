"""What an action costs, and what it is worth.

MunshiJi proposes things that cost real money to do. A shopkeeper deciding whether to send an
offer does not think in engagement rates — he thinks "what will this cost me, and how many people
have to walk back in before it has paid for itself". This module answers exactly that, so every
proposal carries its own arithmetic instead of asking the merchant to take it on faith.

The non-obvious part is the template category. WhatsApp charges very differently depending on
*why* you are messaging someone: a promotional offer is marketing and costs several times more
than a utility message, which is what a reminder about an outstanding balance qualifies as
because it relates to a transaction the customer already entered into. So chasing udhaar is
roughly an order of magnitude cheaper per recipient than winning a lapsed customer back — which
inverts the intuition that collections is the expensive, awkward thing to automate.

Rates are indicative and configurable: per-conversation and per-message pricing in India has
moved more than once, and any real deployment should pull the current rate card rather than trust
a constant compiled into a demo.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from munshiji.money import fmt_inr

__all__ = [
    "MESSAGE_COST_PAISE",
    "TOOL_TEMPLATE",
    "ActionEconomics",
    "TemplateCategory",
    "estimate_action",
]


class TemplateCategory(str, Enum):
    """How WhatsApp classifies an outbound business message, which is what sets its price."""

    MARKETING = "marketing"
    UTILITY = "utility"
    SERVICE = "service"


#: Indicative per-message cost in paise for India. Marketing carries the premium; utility covers
#: messages tied to an existing transaction; service replies inside an open customer-initiated
#: window are not billed.
MESSAGE_COST_PAISE: dict[TemplateCategory, int] = {
    TemplateCategory.MARKETING: 78,
    TemplateCategory.UTILITY: 12,
    TemplateCategory.SERVICE: 0,
}

#: Which category each outbound tool falls into.
TOOL_TEMPLATE: dict[str, TemplateCategory] = {
    "send_winback_offer": TemplateCategory.MARKETING,
    "send_udhaar_reminder": TemplateCategory.UTILITY,
    "create_payment_link": TemplateCategory.UTILITY,
}


@dataclass(slots=True, frozen=True)
class ActionEconomics:
    """The cost, the expected return, and the number that actually decides it."""

    tool_name: str
    template: TemplateCategory
    recipients: int
    unit_cost_paise: int
    send_cost_paise: int
    expected_return_paise: int
    #: Average value of one customer responding — what a single "yes" is worth.
    value_per_response_paise: int
    #: How many people must respond before the send has paid for itself. The shopkeeper's number.
    breakeven_responses: int

    @property
    def net_paise(self) -> int:
        return self.expected_return_paise - self.send_cost_paise

    @property
    def roi(self) -> float | None:
        """Return per rupee spent. ``None`` when nothing was spent."""
        if self.send_cost_paise <= 0:
            return None
        return self.expected_return_paise / self.send_cost_paise

    def as_dict(self) -> dict[str, object]:
        """Everything the agent may quote and the UI may render."""
        return {
            "template": self.template.value,
            "recipients": self.recipients,
            "unit_cost_paise": self.unit_cost_paise,
            "unit_cost_display": fmt_inr(self.unit_cost_paise, decimals=True),
            "send_cost_paise": self.send_cost_paise,
            "send_cost_display": fmt_inr(self.send_cost_paise),
            "expected_return_paise": self.expected_return_paise,
            "expected_return_display": fmt_inr(self.expected_return_paise),
            "net_paise": self.net_paise,
            "net_display": fmt_inr(self.net_paise),
            "breakeven_responses": self.breakeven_responses,
            "value_per_response_paise": self.value_per_response_paise,
            "value_per_response_display": fmt_inr(self.value_per_response_paise),
            "roi": round(self.roi, 1) if self.roi is not None else None,
        }

    def sentence(self, *, hindi: bool = True) -> str:
        """One line a merchant can decide on."""
        if hindi:
            return (
                f"{fmt_inr(self.send_cost_paise)} kharch, "
                f"{fmt_inr(self.expected_return_paise)} wapas aane ki ummeed — "
                f"{self.breakeven_responses} grahak laut aaye to kharcha nikal jaata hai."
            )
        return (
            f"{fmt_inr(self.send_cost_paise)} to send, about "
            f"{fmt_inr(self.expected_return_paise)} back — it pays for itself at "
            f"{self.breakeven_responses} response{'s' if self.breakeven_responses != 1 else ''}."
        )


def estimate_action(
    tool_name: str,
    *,
    recipients: int,
    expected_return_paise: int,
    value_per_response_paise: int,
) -> ActionEconomics:
    """Cost this action out.

    Args:
        tool_name: The write tool being proposed.
        recipients: How many people would actually be contacted.
        expected_return_paise: Modelled revenue back, across all recipients.
        value_per_response_paise: What one responding customer is worth — used for break-even.
            Falls back to the per-recipient average when zero, so the number is never nonsense.
    """
    template = TOOL_TEMPLATE.get(tool_name, TemplateCategory.UTILITY)
    unit = MESSAGE_COST_PAISE[template]
    recipients = max(0, int(recipients))
    send_cost = unit * recipients

    per_response = int(value_per_response_paise)
    if per_response <= 0 and recipients:
        per_response = max(1, int(expected_return_paise / recipients))

    # Ceiling division: two-thirds of a customer does not pay the bill.
    breakeven = 0 if send_cost <= 0 or per_response <= 0 else -(-send_cost // per_response)

    return ActionEconomics(
        tool_name=tool_name,
        template=template,
        recipients=recipients,
        unit_cost_paise=unit,
        send_cost_paise=send_cost,
        expected_return_paise=max(0, int(expected_return_paise)),
        value_per_response_paise=per_response,
        breakeven_responses=int(breakeven),
    )
