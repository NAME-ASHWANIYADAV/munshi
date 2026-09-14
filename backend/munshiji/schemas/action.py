"""Action queue: what MunshiJi proposed, what the merchant decided, what happened."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from munshiji.db.enums import ActionStatus
from munshiji.db.models import ActionOutcome, ActionRequest
from munshiji.schemas.common import ApiModel, Meta, Money, money

__all__ = ["ActionListOut", "ActionOut", "ApproveIn", "OutcomeOut", "RejectIn"]


class OutcomeOut(ApiModel):
    metric: str
    value_num: float | None = None
    value: Money | None = None
    note: str = ""
    observed_at: datetime

    @classmethod
    def from_model(cls, outcome: ActionOutcome) -> OutcomeOut:
        return cls(
            metric=outcome.metric,
            value_num=outcome.value_num,
            value=money(outcome.value_paise) if outcome.value_paise is not None else None,
            note=outcome.note,
            observed_at=outcome.observed_at,
        )


class ActionOut(ApiModel):
    id: str
    tool_name: str
    status: ActionStatus
    summary_en: str = ""
    summary_hi: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    target_count: int = 0
    #: The finding this action answers, when it came from one. The screen uses it to show the
    #: chain — a proposal that cannot point back at why it exists is just a demand.
    insight_id: str | None = None
    estimated_impact: Money
    estimated_cost: Money
    #: Expected return less what it costs to send. Negative means do not do it.
    net_expected: Money
    requested_at: datetime
    decided_at: datetime | None = None
    executed_at: datetime | None = None
    provider: str = "local"
    error: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    outcomes: list[OutcomeOut] = Field(default_factory=list)
    requires_approval: bool = True

    @classmethod
    def from_model(cls, action: ActionRequest) -> ActionOut:
        return cls(
            id=action.id,
            tool_name=action.tool_name,
            status=action.status,
            summary_en=action.summary_en,
            summary_hi=action.summary_hi,
            params=action.params or {},
            target_count=action.target_count,
            insight_id=action.insight_id,
            estimated_impact=money(action.estimated_impact_paise),
            estimated_cost=money(action.estimated_cost_paise),
            net_expected=money(action.estimated_impact_paise - action.estimated_cost_paise),
            requested_at=action.requested_at,
            decided_at=action.decided_at,
            executed_at=action.executed_at,
            provider=action.provider,
            error=action.error,
            result=action.result or {},
            outcomes=[OutcomeOut.from_model(outcome) for outcome in action.outcomes],
            requires_approval=action.status == ActionStatus.PENDING_APPROVAL,
        )


class ActionListOut(ApiModel):
    actions: list[ActionOut] = Field(default_factory=list)
    pending_count: int = 0
    meta: Meta = Field(default_factory=Meta)


class ApproveIn(ApiModel):
    approved_by: str = "merchant"


class RejectIn(ApiModel):
    reason: str = ""
