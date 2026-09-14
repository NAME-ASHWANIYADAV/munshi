"""Write tools — the things MunshiJi can *do*, once the merchant says yes.

Every handler here ends the same way: it creates a ``PENDING_APPROVAL`` :class:`ActionRequest` and
returns. None of them contacts anybody. Execution happens later, in the agent loop's approval
branch, through :func:`munshiji.agent.approval.execute_action` — which refuses anything that is not
in ``APPROVED``.

Budgets and cooldowns are checked *here*, at proposal time, so MunshiJi never offers to do
something it would then be blocked from doing.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from munshiji.agent.approval import check_outbound_budget, create_request, filter_reminder_cooldown
from munshiji.agent.dispatch import resolve_customer_targets, resolve_khata_targets
from munshiji.agent.tools.base import Tool, ToolContext, ToolResult
from munshiji.compliance import screen_recipients
from munshiji.db.enums import Tone
from munshiji.economics import estimate_action
from munshiji.errors import RateLimitedError
from munshiji.money import fmt_inr
from munshiji.repositories.core import get_customers

__all__ = ["TOOLS", "UDHAAR_RECOVERY_PRIOR", "WINBACK_RETURN_PRIOR"]

#: Share of contacted dormant customers expected to return within the offer window. Deliberately
#: conservative: over-promising on stage is worse than a modest, defensible number.
WINBACK_RETURN_PRIOR = 0.30

#: Share of outstanding credit typically recovered after a polite reminder.
UDHAAR_RECOVERY_PRIOR = 0.45


# ── send_winback_offer ──────────────────────────────────────────────────────


class WinbackParams(BaseModel):
    customer_ids: list[str] = Field(
        description="Customers to contact. Get these from find_dormant_customers."
    )
    discount_pct: int = Field(default=10, ge=1, le=50, description="Discount percentage to offer.")
    valid_days: int = Field(default=7, ge=1, le=30, description="How long the offer stays open.")
    insight_id: str | None = Field(default=None, description="The finding this action answers.")

    @field_validator("customer_ids")
    @classmethod
    def _non_empty(cls, value: list[str]) -> list[str]:
        cleaned = [item for item in dict.fromkeys(value) if item]
        if not cleaned:
            raise ValueError("at least one customer id is required")
        return cleaned


async def _send_winback_offer(ctx: ToolContext, params: WinbackParams) -> ToolResult:
    targets = resolve_customer_targets(ctx.session, params.customer_ids, as_of=ctx.as_of)
    if not targets:
        return ToolResult(
            ok=False,
            error="none of those customers exist",
            summary_en="No valid customers to contact",
            summary_hi="Koi valid grahak nahi mila",
        )

    # Before anything else: may this be sent at all? An offer is marketing, so it needs consent
    # the customer actually gave, and it waits for daylight.
    people = get_customers(ctx.session, [str(target["customer_id"]) for target in targets])
    screen = screen_recipients("send_winback_offer", people, at=ctx.as_of)
    if screen.is_blocked:
        return ToolResult(
            ok=False,
            error=screen.blocked_reason_en or "blocked",
            data={"compliance": screen.as_dict()},
            summary_en=screen.blocked_reason_en or "Blocked",
            summary_hi=screen.blocked_reason_hi or "Abhi nahi bhej sakte",
        )

    allowed_ids = set(screen.allowed_ids)
    targets = [target for target in targets if str(target["customer_id"]) in allowed_ids]
    if not targets:
        return ToolResult(
            ok=False,
            error="no consented recipients",
            data={"compliance": screen.as_dict()},
            summary_en="Nobody on that list has consented to offers",
            summary_hi="In mein se kisi ne bhi prachar ki anumati nahi di hai",
        )

    try:
        check_outbound_budget(ctx.session, ctx.merchant_id, len(targets))
    except RateLimitedError as exc:
        return ToolResult(
            ok=False,
            error=exc.message,
            data={
                "limit": exc.context.get("limit"),
                "already_sent": exc.context.get("already_sent"),
            },
            summary_en="Daily message limit reached",
            summary_hi="Aaj ka message limit poora ho gaya",
        )

    tickets = [int(target.get("avg_ticket_paise") or 0) for target in targets]
    expected = int(round(sum(tickets) * WINBACK_RETURN_PRIOR))
    average_ticket = int(sum(tickets) / len(tickets)) if tickets else 0
    economics = estimate_action(
        "send_winback_offer",
        recipients=len(targets),
        expected_return_paise=expected,
        # One response is worth that customer's usual basket, less the discount being given away.
        value_per_response_paise=int(average_ticket * (1 - params.discount_pct / 100)),
    )
    discount_label = f"{params.discount_pct}% off"

    action = create_request(
        ctx.session,
        merchant_id=ctx.merchant_id,
        tool_name="send_winback_offer",
        params={
            "customer_ids": [target["customer_id"] for target in targets],
            "discount_pct": params.discount_pct,
            "discount_label": discount_label,
            "valid_days": params.valid_days,
            "targets": targets,
        },
        summary_en=(
            f"Send a {discount_label} win-back offer to {len(targets)} customers "
            f"(valid {params.valid_days} days)"
        ),
        summary_hi=(
            f"{len(targets)} ग्राहकों को "
            f"{params.discount_pct}% का ऑफ़र, "
            f"{params.valid_days} दिन के लिए"
        ),
        target_count=len(targets),
        estimated_impact_paise=expected,
        estimated_cost_paise=economics.send_cost_paise,
        insight_id=params.insight_id or ctx.insight_id,
        conversation_id=ctx.conversation_id,
    )

    return ToolResult(
        action=action,
        data={
            "action_id": action.id,
            "status": action.status.value,
            "requires_approval": True,
            "target_count": len(targets),
            "discount_pct": params.discount_pct,
            "valid_days": params.valid_days,
            "estimated_recovery_paise": expected,
            "estimated_recovery_display": fmt_inr(expected),
            # The key the reply composer looks for when it wants one figure to say out loud.
            "estimated_impact_paise": expected,
            "economics": economics.as_dict(),
            "economics_sentence": economics.sentence(hindi=ctx.language.startswith("hi")),
            "compliance": screen.as_dict(),
            "compliance_sentence": screen.sentence(hindi=ctx.language.startswith("hi")),
            # Present only when somebody was actually dropped, so the reply composer can key a
            # clause off its existence rather than having to special-case a zero.
            **({"skipped_no_consent": len(screen.refused)} if screen.refused else {}),
            "customer_names": [target["name"] for target in targets[:5]],
        },
        summary_en=action.summary_en,
        summary_hi=action.summary_hi,
    )


# ── send_udhaar_reminder ────────────────────────────────────────────────────


class ReminderParams(BaseModel):
    customer_ids: list[str] = Field(
        default_factory=list, description="Customers whose open khata entries should be chased."
    )
    khata_ids: list[str] = Field(
        default_factory=list, description="Specific khata entry ids, from get_udhaar_summary."
    )
    tone: Tone | None = Field(
        default=None,
        description="Override the register. Omit to let each entry's history choose it.",
    )
    insight_id: str | None = None


async def _send_udhaar_reminder(ctx: ToolContext, params: ReminderParams) -> ToolResult:
    if not params.customer_ids and not params.khata_ids:
        return ToolResult(
            ok=False,
            error="specify customer_ids or khata_ids",
            summary_en="Nobody specified to remind",
            summary_hi="Kisko yaad dilana hai, ye nahi bataya",
        )

    targets = resolve_khata_targets(
        ctx.session,
        merchant_id=ctx.merchant_id,
        khata_ids=params.khata_ids or None,
        customer_ids=params.customer_ids or None,
        as_of=ctx.as_of,
    )
    if not targets:
        return ToolResult(
            data={"target_count": 0},
            summary_en="No open khata entries for those customers",
            summary_hi="Un grahakon ka koi udhaar baaki nahi hai",
        )

    # Chasing money owed is collection contact, so it keeps to daylight hours and never goes to
    # someone who has asked to be left alone.
    people = get_customers(ctx.session, [str(target["customer_id"]) for target in targets])
    screen = screen_recipients("send_udhaar_reminder", people, at=ctx.as_of)
    if screen.is_blocked:
        return ToolResult(
            ok=False,
            error=screen.blocked_reason_en or "blocked",
            data={"compliance": screen.as_dict()},
            summary_en=screen.blocked_reason_en or "Blocked",
            summary_hi=screen.blocked_reason_hi or "Abhi nahi bhej sakte",
        )

    contactable = set(screen.allowed_ids)
    targets = [target for target in targets if str(target["customer_id"]) in contactable]
    if not targets:
        return ToolResult(
            ok=False,
            error="every recipient has opted out",
            data={"compliance": screen.as_dict()},
            summary_en="Everyone on that list has opted out of messages",
            summary_hi="In sabhi ne sandesh band karwa diye hain",
        )

    allowed_ids, blocked_ids = filter_reminder_cooldown(
        ctx.session, [target["khata_entry_id"] for target in targets]
    )
    allowed = [target for target in targets if target["khata_entry_id"] in set(allowed_ids)]
    if not allowed:
        return ToolResult(
            data={"target_count": 0, "in_cooldown": len(blocked_ids)},
            summary_en=f"All {len(blocked_ids)} were reminded within the last week",
            summary_hi=f"{len(blocked_ids)} ko pichhle hafte hi yaad dilaya tha",
        )

    try:
        check_outbound_budget(ctx.session, ctx.merchant_id, len(allowed))
    except RateLimitedError as exc:
        return ToolResult(
            ok=False,
            error=exc.message,
            summary_en="Daily message limit reached",
            summary_hi="Aaj ka message limit poora ho gaya",
        )

    if params.tone is not None:
        for target in allowed:
            target["tone"] = params.tone.value

    outstanding = sum(int(target["amount_paise"]) for target in allowed)
    expected = int(round(outstanding * UDHAAR_RECOVERY_PRIOR))
    # A reminder about an existing balance is a utility message, not marketing - several times
    # cheaper per recipient, which is why chasing udhaar is the best-value thing MunshiJi does.
    economics = estimate_action(
        "send_udhaar_reminder",
        recipients=len(allowed),
        expected_return_paise=expected,
        value_per_response_paise=int(outstanding / len(allowed)) if allowed else 0,
    )

    action = create_request(
        ctx.session,
        merchant_id=ctx.merchant_id,
        tool_name="send_udhaar_reminder",
        params={
            "khata_ids": [target["khata_entry_id"] for target in allowed],
            "customer_ids": [target["customer_id"] for target in allowed],
            "tone": params.tone.value if params.tone else None,
            "targets": allowed,
        },
        summary_en=f"Remind {len(allowed)} customers about {fmt_inr(outstanding)} of udhaar",
        summary_hi=(f"{len(allowed)} ग्राहकों को " f"{fmt_inr(outstanding)} के उधार की " f"याद दिलाएं"),
        target_count=len(allowed),
        estimated_impact_paise=expected,
        estimated_cost_paise=economics.send_cost_paise,
        insight_id=params.insight_id or ctx.insight_id,
        conversation_id=ctx.conversation_id,
    )

    return ToolResult(
        action=action,
        data={
            "action_id": action.id,
            "requires_approval": True,
            "target_count": len(allowed),
            "skipped_in_cooldown": len(blocked_ids),
            "outstanding_paise": outstanding,
            "outstanding_display": fmt_inr(outstanding),
            "estimated_impact_paise": expected,
            "estimated_recovery_paise": expected,
            "estimated_recovery_display": fmt_inr(expected),
            "economics": economics.as_dict(),
            "economics_sentence": economics.sentence(hindi=ctx.language.startswith("hi")),
            "compliance": screen.as_dict(),
            "compliance_sentence": screen.sentence(hindi=ctx.language.startswith("hi")),
            "tones": sorted({str(target["tone"]) for target in allowed}),
            "customer_names": [target["name"] for target in allowed[:5]],
        },
        summary_en=action.summary_en,
        summary_hi=action.summary_hi,
    )


# ── create_payment_link ─────────────────────────────────────────────────────


class PaymentLinkParams(BaseModel):
    customer_id: str = Field(description="Who to send the payment link to.")
    amount_paise: int = Field(gt=0, description="Amount to collect, in paise (100 paise = ₹1).")


async def _create_payment_link(ctx: ToolContext, params: PaymentLinkParams) -> ToolResult:
    targets = resolve_customer_targets(ctx.session, [params.customer_id], as_of=ctx.as_of)
    if not targets:
        return ToolResult(
            ok=False,
            error=f"customer {params.customer_id} not found",
            summary_en="Customer not found",
            summary_hi="Grahak nahi mila",
        )
    targets[0]["amount_paise"] = params.amount_paise

    action = create_request(
        ctx.session,
        merchant_id=ctx.merchant_id,
        tool_name="create_payment_link",
        params={
            "customer_id": params.customer_id,
            "amount_paise": params.amount_paise,
            "targets": targets,
        },
        summary_en=f"Send {targets[0]['name']} a payment link for {fmt_inr(params.amount_paise)}",
        summary_hi=f"{targets[0]['name']} को {fmt_inr(params.amount_paise)} का payment link",
        target_count=1,
        estimated_impact_paise=params.amount_paise,
        conversation_id=ctx.conversation_id,
    )

    return ToolResult(
        action=action,
        data={
            "action_id": action.id,
            "requires_approval": True,
            "customer_name": targets[0]["name"],
            "amount_paise": params.amount_paise,
            "amount_display": fmt_inr(params.amount_paise),
        },
        summary_en=action.summary_en,
        summary_hi=action.summary_hi,
    )


# ── draft_restock_order ─────────────────────────────────────────────────────


class RestockItem(BaseModel):
    sku: str
    qty: float = Field(gt=0)
    name: str | None = None


class RestockParams(BaseModel):
    items: list[RestockItem] = Field(description="SKUs and quantities to order.")
    supplier: str | None = Field(default=None, description="Supplier name, if known.")
    insight_id: str | None = None

    @field_validator("items")
    @classmethod
    def _non_empty(cls, value: list[RestockItem]) -> list[RestockItem]:
        if not value:
            raise ValueError("at least one item is required")
        return value


async def _draft_restock_order(ctx: ToolContext, params: RestockParams) -> ToolResult:
    from munshiji.repositories.core import list_products

    catalogue = {product.sku: product for product in list_products(ctx.session, ctx.merchant_id)}
    rows: list[dict[str, object]] = []
    cost = 0
    for item in params.items:
        product = catalogue.get(item.sku)
        line_cost = int(round(item.qty * product.cost_price_paise)) if product else 0
        cost += line_cost
        rows.append(
            {
                "sku": item.sku,
                "name": item.name or (product.name if product else item.sku),
                "name_hi": product.name_hi if product else "",
                "qty": item.qty,
                "unit": product.unit if product else "pc",
                "cost_paise": line_cost,
            }
        )

    action = create_request(
        ctx.session,
        merchant_id=ctx.merchant_id,
        tool_name="draft_restock_order",
        params={"items": rows, "supplier": params.supplier, "estimated_cost_paise": cost},
        summary_en=f"Draft a restock order for {len(rows)} items ({fmt_inr(cost)})",
        summary_hi=f"{len(rows)} सामान का ऑर्डर ({fmt_inr(cost)})",
        target_count=len(rows),
        estimated_impact_paise=cost,
        insight_id=params.insight_id or ctx.insight_id,
        conversation_id=ctx.conversation_id,
    )

    return ToolResult(
        action=action,
        data={
            "action_id": action.id,
            "requires_approval": True,
            "items": rows,
            "estimated_cost_paise": cost,
            "estimated_cost_display": fmt_inr(cost),
            "supplier": params.supplier,
        },
        summary_en=action.summary_en,
        summary_hi=action.summary_hi,
    )


# ── schedule_followup ───────────────────────────────────────────────────────


class FollowupParams(BaseModel):
    what: str = Field(description="What MunshiJi should raise later, in the merchant's words.")
    when: Literal["tomorrow", "this_evening", "next_week", "month_end"] = Field(
        default="tomorrow", description="When to bring it up."
    )


async def _schedule_followup(ctx: ToolContext, params: FollowupParams) -> ToolResult:
    action = create_request(
        ctx.session,
        merchant_id=ctx.merchant_id,
        tool_name="schedule_followup",
        params={"what": params.what, "when": params.when},
        summary_en=f"Remind you {params.when.replace('_', ' ')}: {params.what}",
        summary_hi=f"{params.what} — याद दिलाऊंगा",
        target_count=0,
        conversation_id=ctx.conversation_id,
    )
    return ToolResult(
        action=action,
        data={"action_id": action.id, "requires_approval": True, "when": params.when},
        summary_en=action.summary_en,
        summary_hi=action.summary_hi,
    )


# ── save_merchant_note ──────────────────────────────────────────────────────


class NoteParams(BaseModel):
    text: str = Field(min_length=2, description="The note to remember, verbatim.")


async def _save_merchant_note(ctx: ToolContext, params: NoteParams) -> ToolResult:
    action = create_request(
        ctx.session,
        merchant_id=ctx.merchant_id,
        tool_name="save_merchant_note",
        params={"text": params.text},
        summary_en=f"Remember: {params.text[:80]}",
        summary_hi=f"याद रखूँगा: {params.text[:80]}",
        target_count=0,
        conversation_id=ctx.conversation_id,
    )
    return ToolResult(
        action=action,
        data={"action_id": action.id, "requires_approval": True, "text": params.text},
        summary_en=action.summary_en,
        summary_hi=action.summary_hi,
    )


TOOLS: list[Tool] = [
    Tool(
        name="send_winback_offer",
        description=(
            "PROPOSE sending a discount offer over WhatsApp to customers who have stopped coming. "
            "This does NOT send anything: it creates a proposal the merchant must approve "
            "out loud. "
            "Always tell the merchant how many people and what discount, then ask."
        ),
        params_model=WinbackParams,
        handler=_send_winback_offer,
        requires_approval=True,
        label_en="Win-back offer",
        label_hi="Win-back offer bhejein",
    ),
    Tool(
        name="send_udhaar_reminder",
        description=(
            "PROPOSE polite khata reminders to customers with open credit. Does NOT send: "
            "creates a proposal for approval. Entries reminded within the last week are "
            "skipped automatically."
        ),
        params_model=ReminderParams,
        handler=_send_udhaar_reminder,
        requires_approval=True,
        label_en="Udhaar reminder",
        label_hi="Udhaar ki yaad dilayein",
    ),
    Tool(
        name="create_payment_link",
        description=(
            "PROPOSE sending one customer a payment link for a specific amount. Does NOT send: "
            "creates a proposal for approval."
        ),
        params_model=PaymentLinkParams,
        handler=_create_payment_link,
        requires_approval=True,
        label_en="Payment link",
        label_hi="Payment link bhejein",
    ),
    Tool(
        name="draft_restock_order",
        description=(
            "PROPOSE a restock order for specific SKUs and quantities, with its estimated cost. "
            "Does NOT order anything: creates a proposal for approval."
        ),
        params_model=RestockParams,
        handler=_draft_restock_order,
        requires_approval=True,
        label_en="Restock order",
        label_hi="Saman mangwayein",
    ),
    Tool(
        name="schedule_followup",
        description=(
            "PROPOSE that MunshiJi raise something again later (tomorrow, this evening, next week, "
            "month end). Creates a proposal for approval."
        ),
        params_model=FollowupParams,
        handler=_schedule_followup,
        requires_approval=True,
        label_en="Follow up",
        label_hi="Baad mein yaad dilayein",
    ),
    Tool(
        name="save_merchant_note",
        description=(
            "PROPOSE remembering something the merchant said, so it can be recalled in later "
            "conversations. Creates a proposal for approval."
        ),
        params_model=NoteParams,
        handler=_save_merchant_note,
        requires_approval=True,
        label_en="Remember this",
        label_hi="Yaad rakhein",
    ),
]
