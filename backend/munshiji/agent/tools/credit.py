"""Udhaar (khata) tools — who owes what, for how long, and who to ask first."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from munshiji.agent.tools.base import Tool, ToolContext, ToolResult
from munshiji.clock import ist_date_of
from munshiji.db.models import Customer
from munshiji.messaging import select_tone
from munshiji.money import fmt_inr
from munshiji.repositories.core import get_open_khata

__all__ = ["AGING_BUCKETS", "TOOLS", "udhaar_ledger"]

#: Inclusive upper bounds, in days. The last bucket is open-ended.
AGING_BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("0-15", 15),
    ("16-30", 30),
    ("31-60", 60),
    ("60+", None),
)


def _bucket_for(days: int) -> str:
    for label, upper in AGING_BUCKETS:
        if upper is None or days <= upper:
            return label
    return AGING_BUCKETS[-1][0]


class UdhaarSummaryParams(BaseModel):
    limit: int = Field(
        default=5, ge=1, le=25, description="How many individual debtors to list, highest first."
    )


def udhaar_ledger(
    session: Session, merchant_id: str, *, today: date, limit: int | None = None
) -> dict[str, Any]:
    """Aging buckets and the prioritised debtor list.

    One computation shared by the conversational tool and ``GET /api/khata/{merchant_id}`` —
    the khata page and the spoken answer must never disagree about who owes what.
    """
    entries = get_open_khata(session, merchant_id)

    customer_ids = {entry.customer_id for entry in entries}
    customers = (
        {
            customer.id: customer
            for customer in session.query(Customer).filter(Customer.id.in_(customer_ids)).all()
        }
        if customer_ids
        else {}
    )

    buckets: dict[str, dict[str, int]] = {
        label: {"count": 0, "amount_paise": 0} for label, _ in AGING_BUCKETS
    }
    rows: list[dict[str, object]] = []
    total = 0

    for entry in entries:
        outstanding = entry.outstanding_paise
        if outstanding <= 0:
            continue
        reference = entry.due_at or entry.opened_at
        days_overdue = max(0, (today - ist_date_of(reference)).days)
        bucket = _bucket_for(days_overdue)
        buckets[bucket]["count"] += 1
        buckets[bucket]["amount_paise"] += outstanding
        total += outstanding

        customer = customers.get(entry.customer_id)
        reliability = float((customer.tags or {}).get("reliability", 0.5)) if customer else 0.5
        rows.append(
            {
                "khata_entry_id": entry.id,
                "customer_id": entry.customer_id,
                "name": customer.name if customer else "(unknown)",
                "phone": customer.phone if customer else "",
                "amount_paise": outstanding,
                "amount_display": fmt_inr(outstanding),
                "days_overdue": days_overdue,
                "bucket": bucket,
                "reminders_sent": entry.reminders_sent,
                "suggested_tone": select_tone(days_overdue, reliability, outstanding).value,
            }
        )

    # Chase priority: the biggest, oldest balances first — that is where the working capital is.
    rows.sort(
        key=lambda row: (int(row["amount_paise"]) * (1 + int(row["days_overdue"]) / 30)),
        reverse=True,
    )
    top = rows[:limit] if limit else rows

    for label in buckets:
        buckets[label]["amount_display"] = fmt_inr(buckets[label]["amount_paise"])  # type: ignore[assignment]

    over_60 = buckets["60+"]["count"]

    return {
        "total_outstanding_paise": total,
        "total_outstanding_display": fmt_inr(total),
        "entry_count": len(rows),
        "customer_count": len({row["customer_id"] for row in rows}),
        "buckets": buckets,
        "over_60_days_count": over_60,
        "top_debtors": top,
    }


async def _udhaar_summary(ctx: ToolContext, params: UdhaarSummaryParams) -> ToolResult:
    data = udhaar_ledger(
        ctx.session, ctx.merchant_id, today=ist_date_of(ctx.as_of), limit=params.limit
    )
    total = data["total_outstanding_paise"]
    count = data["entry_count"]
    over_60 = data["over_60_days_count"]
    return ToolResult(
        data=data,
        summary_en=f"{fmt_inr(total)} open across {count} entries ({over_60} over 60 days)",
        summary_hi=(f"{fmt_inr(total)} udhaar baaki, {count} entries, {over_60} 60 din se purani"),
    )


TOOLS: list[Tool] = [
    Tool(
        name="get_udhaar_summary",
        description=(
            "Outstanding khata (informal credit): the total owed, an aging breakdown "
            "(0-15 / 16-30 / 31-60 / 60+ days), and a prioritised list of debtors with the "
            "reminder tone each one warrants. Use for udhaar, khata, baki or 'who owes me' "
            "questions."
        ),
        params_model=UdhaarSummaryParams,
        handler=_udhaar_summary,
        label_en="Udhaar summary",
        label_hi="Udhaar ka hisaab",
    ),
]
