"""Merchant profile and the live dashboard snapshot."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from munshiji.schemas.common import ApiModel, Meta, Money

__all__ = [
    "DashboardOut",
    "HealthDimensionOut",
    "HealthScoreOut",
    "MerchantOut",
    "PaymentMixSlice",
    "SparkPoint",
    "TodaySnapshot",
]


class MerchantOut(ApiModel):
    id: str
    owner_name: str
    shop_name: str
    category: str
    city: str
    locality: str
    language: str
    phone: str = ""
    soundbox_id: str = ""
    business_hours_start: int = 7
    business_hours_end: int = 22


class SparkPoint(ApiModel):
    """One point on the 14-day collection sparkline."""

    day: date
    weekday: str
    collection: Money
    transactions: int = 0
    is_today: bool = False


class PaymentMixSlice(ApiModel):
    method: str
    share_pct: float
    amount: Money


class TodaySnapshot(ApiModel):
    """Everything MunshiJi can say about today without calling a tool."""

    day: date
    weekday_en: str
    weekday_hi: str
    collected: Money
    transactions: int = 0
    unique_customers: int = 0
    #: Of today's named buyers, how many had bought before today vs never before.
    repeat_customers: int = 0
    new_customers: int = 0
    average_ticket: Money
    #: Projected close, from the historical intraday curve (None before the projection threshold).
    projected_close: Money | None = None
    projection_confidence: float = 0.0
    baseline: Money | None = None
    delta_pct: float | None = None
    robust_z: float | None = None
    day_progress: float = 0.0
    is_too_early_to_project: bool = False


class HealthDimensionOut(ApiModel):
    """One axis of the merchant health signal, with the evidence behind it."""

    key: str
    label_en: str
    label_hi: str
    score: float
    weight: float
    contribution: float
    evidence: str
    reason_en: str
    reason_hi: str
    metrics: dict[str, float | int | str] = Field(default_factory=dict)


class HealthScoreOut(ApiModel):
    """An explainable merchant-health signal.

    Produced as a by-product of the same engines that advise the merchant, and framed for the
    question a lender has to answer about a shop with no audited accounts. It is a signal, not a
    credit decision — nothing here approves or prices anything.
    """

    merchant_id: str
    score: float
    band: str
    band_hi: str
    previous_score: float | None = None
    delta: float | None = None
    weakest_dimension: str | None = None
    dimensions: list[HealthDimensionOut] = Field(default_factory=list)
    as_of: datetime


class DashboardOut(ApiModel):
    merchant: MerchantOut
    today: TodaySnapshot
    sparkline: list[SparkPoint] = Field(default_factory=list)
    payment_mix: list[PaymentMixSlice] = Field(default_factory=list)
    open_udhaar: Money
    open_udhaar_count: int = 0
    low_stock_count: int = 0
    dormant_customer_count: int = 0
    pending_action_count: int = 0
    generated_at: datetime
    meta: Meta = Field(default_factory=Meta)
