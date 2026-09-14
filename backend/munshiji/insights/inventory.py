"""Inventory engines: stockout risk, dead stock, expiry risk.

All three hang off one number — the **EWMA consumption rate** — because a kirana's demand is
neither stationary nor smooth. A plain 60-day average says a SKU sells 2/day even if it has sold
nothing for a fortnight; an exponentially weighted rate (alpha = 0.3, so the last week carries
most of the weight) notices. The same rate then answers three different questions: will it run
out, has it stopped moving, and will it rot before it sells.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from munshiji.clock import ist_date_of
from munshiji.db.enums import InsightKind, Severity
from munshiji.db.models import Product
from munshiji.insights.base import InsightContext, InsightDraft
from munshiji.insights.seasonal import festival_multiplier
from munshiji.insights.stats import clamp, percentile, safe_div
from munshiji.logging import get_logger
from munshiji.money import fmt_inr
from munshiji.repositories import analytics

__all__ = [
    "CONSUMPTION_WINDOW_DAYS",
    "COVER_TARGET_DAYS",
    "DEAD_STOCK_DAYS",
    "DEAD_STOCK_MIN_CAPITAL_PAISE",
    "EWMA_ALPHA",
    "LEAD_TIME_DAYS",
    "RATE_EPSILON",
    "SAFETY_DAYS",
    "DeadStockEngine",
    "ExpiryRiskEngine",
    "StockPosition",
    "StockoutRiskEngine",
]

logger = get_logger(__name__)


def _inr(amount_paise: float) -> str:
    """Whole-rupee rendering for spoken and displayed copy.

    ``fmt_inr`` shows paise whenever an amount is not a round rupee, and almost every number an
    engine derives (a projection, an expected recovery, a margin loss) has a fractional tail.
    "आठ हज़ार छह सौ सड़सठ दशमलव पाँच आठ रुपये" is not how a merchant hears money, so all copy is
    rounded to the rupee. ``metrics`` keeps the exact paise.
    """
    return fmt_inr(int(round(amount_paise)), decimals=False)


def _qty(units: float) -> str:
    """Quantity for spoken and displayed copy: whole numbers stay whole, the rest get one place.

    An EWMA rate multiplied out lands on values like ``48.6424``, which is not a number anyone
    says about packets of butter. ``metrics`` keeps the full precision.
    """
    rounded = round(float(units), 1)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:.1f}"


# ── Module constants (SPEC.md §7) ───────────────────────────────────────────

#: Decay for the consumption rate. 0.3 puts ~83% of the weight on the last 7 days, which matches
#: how quickly a neighbourhood's buying actually turns.
EWMA_ALPHA = 0.3

#: History fed to the EWMA. Long enough to survive a slow fortnight, short enough that last
#: quarter's demand does not haunt the number.
CONSUMPTION_WINDOW_DAYS = 60

#: Days between placing an order with the distributor and the stock arriving on the shelf.
LEAD_TIME_DAYS = 3

#: Buffer on top of the lead time, so a one-day delivery slip is not a stockout.
SAFETY_DAYS = 1

#: Cover a restock aims to leave on the shelf, over and above the lead time.
COVER_TARGET_DAYS = 7

#: Horizon over which the festival uplift is averaged: exactly the window a restock has to
#: cover, which is the lead time plus the cover target.
FESTIVAL_HORIZON_DAYS = LEAD_TIME_DAYS + COVER_TARGET_DAYS

#: Dead stock: nothing sold for this long...
DEAD_STOCK_DAYS = 45

#: ...and at least this much capital sitting in it. Below ₹500 it is not worth a conversation.
DEAD_STOCK_MIN_CAPITAL_PAISE = 50_000

#: Floor on the consumption rate so ``days_of_cover`` never divides by zero.
RATE_EPSILON = 1e-6

#: Catalogue size below which the top-decile severity escalation is switched off.
MIN_SKUS_FOR_DECILE = 5

#: Caps on how many single-SKU findings reach the feed.
MAX_STOCKOUT_DRAFTS = 6
MAX_EXPIRY_DRAFTS = 4
MAX_DEAD_STOCK_ITEMS = 8

#: A markdown must still clear cost by this margin to be worth suggesting.
CLEARANCE_MIN_MARGIN = 1.05
CLEARANCE_MAX_DISCOUNT_PCT = 40


@dataclass(slots=True, frozen=True)
class StockPosition:
    """One SKU's demand and cover, the shared frame behind all three inventory engines."""

    product: Product
    rate_units_per_day: float
    stock_qty: float
    days_of_cover: float
    units_60d: float
    revenue_60d_paise: int
    last_sale_day: date | None
    days_since_sale: int | None

    @property
    def capital_locked_paise(self) -> int:
        return int(round(self.stock_qty * self.product.cost_price_paise))


def _positions(ctx: InsightContext) -> dict[str, StockPosition]:
    """Compute (and cache) every SKU's consumption rate and days of cover."""

    def build() -> dict[str, StockPosition]:
        today = ist_date_of(ctx.as_of)
        movements = analytics.product_movements(
            ctx.session,
            ctx.merchant_id,
            as_of=ctx.as_of,
            window_days=CONSUMPTION_WINDOW_DAYS,
        )
        positions: dict[str, StockPosition] = {}
        for product_id, movement in movements.items():
            rate = movement.rate_units_per_day
            stock = float(movement.product.stock_qty)
            positions[product_id] = StockPosition(
                product=movement.product,
                rate_units_per_day=rate,
                stock_qty=stock,
                days_of_cover=safe_div(stock, max(rate, RATE_EPSILON), 0.0),
                units_60d=movement.units_sold,
                revenue_60d_paise=movement.revenue_paise,
                last_sale_day=movement.last_sale_day,
                days_since_sale=(
                    (today - movement.last_sale_day).days if movement.last_sale_day else None
                ),
            )
        return positions

    return ctx.cached("stock_positions", build)


def _revenue_cutoff(positions: dict[str, StockPosition], pct: float = 90.0) -> float:
    """Revenue level above which a SKU counts as a top earner for severity escalation.

    Returns ``0.0`` (escalation disabled) for a catalogue too small for a decile to mean
    anything — with two SKUs the 90th percentile is simply "the bigger one", which would
    escalate half the shelf.
    """
    if len(positions) < MIN_SKUS_FOR_DECILE:
        return 0.0
    values = [float(position.revenue_60d_paise) for position in positions.values()]
    return percentile(values, pct) if values else 0.0


def restock_quantity(
    position: StockPosition,
    *,
    multiplier: float = 1.0,
    lead_time_days: int = LEAD_TIME_DAYS,
    cover_target_days: int = COVER_TARGET_DAYS,
) -> float:
    """Units to order: ``rate x (lead_time + cover_target) x festival_multiplier - stock``.

    Rounded up to a whole unit — distributors do not ship 3.4 packets — and never negative.
    """
    target = position.rate_units_per_day * (lead_time_days + cover_target_days) * multiplier
    return max(0.0, math.ceil(target - position.stock_qty))


class StockoutRiskEngine:
    """SKUs that will run dry before a replacement can land.

    ``days_of_cover = stock_qty / max(rate, eps)``; risk when that is below
    ``LEAD_TIME_DAYS + SAFETY_DAYS``. Severity tracks urgency first (how many days are left) and
    is then escalated one step for a top-decile earner, because running out of the shop's best
    seller costs far more than running out of a slow one.
    """

    kind = InsightKind.STOCKOUT_RISK

    def __init__(
        self,
        *,
        lead_time_days: int = LEAD_TIME_DAYS,
        safety_days: int = SAFETY_DAYS,
        max_drafts: int = MAX_STOCKOUT_DRAFTS,
    ) -> None:
        self.lead_time_days = lead_time_days
        self.safety_days = safety_days
        self.max_drafts = max_drafts

    @property
    def threshold_days(self) -> int:
        return self.lead_time_days + self.safety_days

    def run(self, ctx: InsightContext) -> list[InsightDraft]:
        positions = _positions(ctx)
        if not positions:
            return []
        top_earner_cutoff = _revenue_cutoff(positions)

        at_risk = [
            position
            for position in positions.values()
            if position.rate_units_per_day > 0 and position.days_of_cover < self.threshold_days
        ]
        at_risk.sort(key=lambda position: (position.days_of_cover, -position.revenue_60d_paise))

        drafts: list[InsightDraft] = []
        for position in at_risk[: self.max_drafts]:
            product = position.product
            shortfall_days = max(0.0, self.threshold_days - position.days_of_cover)
            units_missed = position.rate_units_per_day * shortfall_days
            impact = int(round(units_missed * product.sell_price_paise))
            if impact <= 0:
                continue

            multiplier = festival_multiplier(
                product.category, ctx.as_of, horizon_days=FESTIVAL_HORIZON_DAYS
            )
            order_qty = restock_quantity(position, multiplier=multiplier)

            if position.days_of_cover < 1.0:
                severity = Severity.CRITICAL
            elif position.days_of_cover < 2.0:
                severity = Severity.HIGH
            else:
                severity = Severity.MEDIUM
            if (
                float(position.revenue_60d_paise) >= top_earner_cutoff > 0
                and severity is not Severity.CRITICAL
            ):
                severity = Severity.CRITICAL if severity is Severity.HIGH else Severity.HIGH

            # A rate estimated from a handful of units is weaker than one built from steady
            # movement, so confidence rises with observed volume and then plateaus.
            confidence = round(clamp(0.6 + 0.02 * min(position.units_60d, 15.0), 0.6, 0.9), 3)

            name_hi = product.name_hi or product.name
            cover_text = f"{position.days_of_cover:.1f}"
            drafts.append(
                InsightDraft(
                    kind=self.kind,
                    severity=severity,
                    title_en=f"{product.name} runs out in {cover_text} days",
                    title_hi=f"{name_hi} {cover_text} दिन में ख़त्म हो जाएगा",
                    body_en=(
                        f"{_qty(position.stock_qty)} {product.unit} left and selling "
                        f"{position.rate_units_per_day:.1f} {product.unit}/day, so cover is "
                        f"{cover_text} days against a {self.lead_time_days}-day lead time plus "
                        f"{self.safety_days} day of safety. About {_inr(impact)} of sales are "
                        f"at risk. Order {_qty(order_qty)} {product.unit}"
                        + (f" (festival uplift {multiplier:.2f}x)." if multiplier > 1.0 else ".")
                    ),
                    body_hi=(
                        f"{name_hi} सिर्फ़ {_qty(position.stock_qty)} {product.unit} बचा है और रोज़ "
                        f"{position.rate_units_per_day:.1f} बिक रहा है — {cover_text} दिन का "
                        f"स्टॉक। माल आने में {self.lead_time_days} दिन लगते हैं, यानी लगभग "
                        f"{_inr(impact)} की बिक्री हाथ से जा सकती है। {_qty(order_qty)} "
                        f"{product.unit} का ऑर्डर कर दीजिए।"
                    ),
                    metrics={
                        "product_id": product.id,
                        "sku": product.sku,
                        "name": product.name,
                        "name_hi": name_hi,
                        "category": product.category,
                        "unit": product.unit,
                        "stock_qty": round(position.stock_qty, 3),
                        "rate_units_per_day": round(position.rate_units_per_day, 4),
                        "ewma_alpha": EWMA_ALPHA,
                        "window_days": CONSUMPTION_WINDOW_DAYS,
                        "days_of_cover": round(position.days_of_cover, 2),
                        "lead_time_days": self.lead_time_days,
                        "safety_days": self.safety_days,
                        "threshold_days": self.threshold_days,
                        "units_60d": position.units_60d,
                        "revenue_60d_paise": position.revenue_60d_paise,
                        "units_at_risk": round(units_missed, 3),
                        "revenue_at_risk_paise": impact,
                        "restock_qty": order_qty,
                        "festival_multiplier": round(multiplier, 3),
                        "sell_price_paise": product.sell_price_paise,
                        "cost_price_paise": product.cost_price_paise,
                    },
                    suggested_tool="draft_restock_order",
                    suggested_params={
                        "items": [
                            {
                                "product_id": product.id,
                                "sku": product.sku,
                                "name": product.name,
                                "qty": order_qty,
                                "unit": product.unit,
                                "estimated_cost_paise": int(
                                    round(order_qty * product.cost_price_paise)
                                ),
                            }
                        ],
                        "reason": "stockout_risk",
                        "needed_by": (
                            ist_date_of(ctx.as_of) + timedelta(days=self.lead_time_days)
                        ).isoformat(),
                    },
                    impact_paise=impact,
                    confidence=confidence,
                    dedupe_key=f"stockout_risk:{product.sku}",
                    expires_in_days=2,
                )
            )
        return drafts


class DeadStockEngine:
    """Capital that stopped moving.

    A SKU qualifies when it has not sold for :data:`DEAD_STOCK_DAYS` **and** holds at least
    :data:`DEAD_STOCK_MIN_CAPITAL_PAISE` of cost. Both conditions matter: a dusty ₹40 packet is
    not a business problem, and a slow-but-moving line is not dead. Findings are aggregated into
    one insight because the merchant's decision ("run a clearance") is a single decision.
    """

    kind = InsightKind.DEAD_STOCK

    def __init__(
        self,
        *,
        idle_days: int = DEAD_STOCK_DAYS,
        min_capital_paise: int = DEAD_STOCK_MIN_CAPITAL_PAISE,
    ) -> None:
        self.idle_days = idle_days
        self.min_capital_paise = min_capital_paise

    def run(self, ctx: InsightContext) -> list[InsightDraft]:
        positions = _positions(ctx)
        if not positions:
            return []
        today = ist_date_of(ctx.as_of)

        stale: list[tuple[StockPosition, int]] = []
        for position in positions.values():
            capital = position.capital_locked_paise
            if capital < self.min_capital_paise or position.stock_qty <= 0:
                continue
            if position.last_sale_day is None:
                # Never sold at all: age it from when the SKU entered the catalogue, so a
                # product added yesterday is not branded dead on day one.
                idle = (today - ist_date_of(position.product.created_at)).days
            else:
                idle = (today - position.last_sale_day).days
            if idle >= self.idle_days:
                stale.append((position, idle))

        if not stale:
            return []
        stale.sort(key=lambda item: -item[0].capital_locked_paise)
        stale = stale[:MAX_DEAD_STOCK_ITEMS]
        total_capital = sum(position.capital_locked_paise for position, _ in stale)

        if total_capital >= 500_000:
            severity = Severity.HIGH
        elif total_capital >= 200_000:
            severity = Severity.MEDIUM
        else:
            severity = Severity.LOW

        items = [
            {
                "product_id": position.product.id,
                "sku": position.product.sku,
                "name": position.product.name,
                "name_hi": position.product.name_hi or position.product.name,
                "category": position.product.category,
                "stock_qty": round(position.stock_qty, 3),
                "unit": position.product.unit,
                "cost_price_paise": position.product.cost_price_paise,
                "capital_locked_paise": position.capital_locked_paise,
                "last_sale_day": (
                    position.last_sale_day.isoformat() if position.last_sale_day else None
                ),
                "days_idle": idle,
                "suggested_discount_pct": _clearance_discount(position.product),
            }
            for position, idle in stale
        ]
        names = ", ".join(str(item["name"]) for item in items[:3])
        oldest = max(int(item["days_idle"] or self.idle_days) for item in items)
        # Use the deepest markdown any of these SKUs can carry; if none has room above cost,
        # propose a bundle rather than a meaningless "0% off".
        best_discount = max(int(item["suggested_discount_pct"] or 0) for item in items)
        clearance_en = (
            f"bundle {names} at {best_discount}% off"
            if best_discount > 0
            else f"bundle {names} as a combo"
        )
        clearance_hi = (
            f"{names} पर {best_discount}% छूट लगाकर"
            if best_discount > 0
            else f"{names} का कॉम्बो बनाकर"
        )

        return [
            InsightDraft(
                kind=self.kind,
                severity=severity,
                title_en=f"{_inr(total_capital)} locked in {len(items)} unsold items",
                title_hi=f"{len(items)} बिना बिके सामान में {_inr(total_capital)} फँसा है",
                body_en=(
                    f"{names} have not sold in at least {self.idle_days} days — the longest for "
                    f"{oldest} days. That is {_inr(total_capital)} of purchase cost sitting on "
                    "the shelf. A combo or a marked-down corner turns it back into working "
                    "capital; the stock will not improve with age."
                ),
                body_hi=(
                    f"{names} पिछले {self.idle_days} दिनों से बिका ही नहीं — सबसे पुराना "
                    f"{oldest} दिन से। इसमें {_inr(total_capital)} की लागत फँसी है। कॉम्बो या "
                    "छूट लगाकर निकालिए, रखे रहने से हालत और ख़राब होगी।"
                ),
                metrics={
                    "idle_days_threshold": self.idle_days,
                    "min_capital_paise": self.min_capital_paise,
                    "item_count": len(items),
                    "capital_locked_paise": total_capital,
                    "oldest_days_idle": oldest,
                    "items": items,
                },
                suggested_tool="save_merchant_note",
                suggested_params={
                    "text_en": (
                        f"Clearance idea: {clearance_en} to release {_inr(total_capital)}."
                    ),
                    "text_hi": f"{clearance_hi} {_inr(total_capital)} निकालिए।",
                    "suggested_discount_pct": best_discount,
                    "tags": ["dead_stock", "clearance"],
                    "product_ids": [item["product_id"] for item in items],
                },
                impact_paise=total_capital,
                confidence=0.85,
                dedupe_key="dead_stock",
                expires_in_days=7,
            )
        ]


class ExpiryRiskEngine:
    """Perishables that will spoil before they sell.

    ``remaining_shelf_life = shelf_life_days - days_since_restock``. When days of cover exceeds
    it, the surplus is not slow stock — it is a write-off with a date on it, so the impact is
    valued at **cost** (money already spent and about to be binned), not at sell price.
    """

    kind = InsightKind.EXPIRY_RISK

    def __init__(self, *, max_drafts: int = MAX_EXPIRY_DRAFTS) -> None:
        self.max_drafts = max_drafts

    def run(self, ctx: InsightContext) -> list[InsightDraft]:
        positions = _positions(ctx)
        if not positions:
            return []
        today = ist_date_of(ctx.as_of)

        risky: list[tuple[StockPosition, float, float, int]] = []
        for position in positions.values():
            product = position.product
            if not product.is_perishable or not product.shelf_life_days:
                continue
            if product.last_restocked_at is None or position.stock_qty <= 0:
                continue
            days_since_restock = (today - ist_date_of(product.last_restocked_at)).days
            remaining = float(product.shelf_life_days - days_since_restock)
            if position.days_of_cover <= remaining:
                continue
            units_at_risk = max(
                0.0, position.stock_qty - position.rate_units_per_day * max(remaining, 0.0)
            )
            loss = int(round(units_at_risk * product.cost_price_paise))
            if loss <= 0:
                continue
            risky.append((position, remaining, units_at_risk, loss))

        if not risky:
            return []
        risky.sort(key=lambda item: (item[1], -item[3]))

        drafts: list[InsightDraft] = []
        for position, remaining, units_at_risk, loss in risky[: self.max_drafts]:
            product = position.product
            if remaining <= 1:
                severity = Severity.CRITICAL
            elif remaining <= 3:
                severity = Severity.HIGH
            else:
                severity = Severity.MEDIUM
            discount = _clearance_discount(product)
            name_hi = product.name_hi or product.name
            remaining_text = f"{max(remaining, 0):.0f}"
            # On a thin-margin perishable there is no room to discount above cost. Saying
            # "a 0% markdown" is nonsense; the real advice is that selling at cost still beats
            # binning it, because a write-off returns nothing at all.
            if discount > 0:
                advice_en = f"a {discount}% markdown today still clears cost."
                advice_hi = f"आज ही {discount}% छूट लगा दीजिए।"
                action_en = f"Mark {product.name} down {discount}% today"
                action_hi = f"{name_hi} पर आज {discount}% छूट लगाइए"
            else:
                advice_en = (
                    "the margin is too thin to discount, so move it at cost — "
                    "throwing it away returns nothing."
                )
                advice_hi = (
                    "मार्जिन इतना कम है कि छूट की गुंजाइश नहीं — लागत पर ही निकाल दीजिए, "
                    "फेंकने से तो कुछ भी नहीं मिलेगा।"
                )
                action_en = f"Move {product.name} at cost today"
                action_hi = f"{name_hi} आज लागत पर निकाल दीजिए"

            drafts.append(
                InsightDraft(
                    kind=self.kind,
                    severity=severity,
                    title_en=(
                        f"{product.name}: {_qty(units_at_risk)} {product.unit} will expire unsold"
                    ),
                    title_hi=f"{name_hi}: {_qty(units_at_risk)} {product.unit} ख़राब हो जाएगा",
                    body_en=(
                        f"{_qty(position.stock_qty)} {product.unit} in stock with {remaining_text} "
                        f"days of shelf life left, but at {position.rate_units_per_day:.1f} "
                        f"{product.unit}/day only "
                        f"{position.rate_units_per_day * max(remaining, 0.0):.1f} will sell. "
                        f"{_inr(loss)} of cost is at risk — {advice_en}"
                    ),
                    body_hi=(
                        f"{name_hi} का {_qty(position.stock_qty)} {product.unit} पड़ा है और सिर्फ़ "
                        f"{remaining_text} दिन की शेल्फ़ लाइफ़ बची है। रोज़ "
                        f"{position.rate_units_per_day:.1f} की रफ़्तार से सब नहीं बिकेगा — "
                        f"{_inr(loss)} का नुक़सान हो सकता है। {advice_hi}"
                    ),
                    metrics={
                        "product_id": product.id,
                        "sku": product.sku,
                        "name": product.name,
                        "name_hi": name_hi,
                        "unit": product.unit,
                        "stock_qty": round(position.stock_qty, 3),
                        "rate_units_per_day": round(position.rate_units_per_day, 4),
                        "days_of_cover": round(position.days_of_cover, 2),
                        "shelf_life_days": product.shelf_life_days,
                        "last_restocked_at": ist_date_of(product.last_restocked_at).isoformat(),
                        "remaining_shelf_life_days": round(remaining, 2),
                        "units_at_risk": round(units_at_risk, 3),
                        "loss_paise": loss,
                        "cost_price_paise": product.cost_price_paise,
                        "sell_price_paise": product.sell_price_paise,
                        "suggested_discount_pct": discount,
                    },
                    suggested_tool="save_merchant_note",
                    suggested_params={
                        "text_en": (
                            f"{action_en} — {_qty(units_at_risk)} {product.unit} expire in "
                            f"{remaining_text} days ({_inr(loss)} at risk)."
                        ),
                        "text_hi": (
                            f"{action_hi} — {remaining_text} दिन में "
                            f"{_qty(units_at_risk)} {product.unit} ख़राब हो जाएगा।"
                        ),
                        "tags": ["expiry", product.sku],
                        "product_ids": [product.id],
                    },
                    impact_paise=loss,
                    confidence=0.8,
                    dedupe_key=f"expiry_risk:{product.sku}",
                    expires_in_days=1,
                )
            )
        return drafts


def _clearance_discount(product: Product) -> int:
    """Largest round markdown that still leaves a small margin over cost, capped sensibly."""
    if product.sell_price_paise <= 0:
        return 0
    floor = product.cost_price_paise * CLEARANCE_MIN_MARGIN
    headroom = 1.0 - safe_div(floor, product.sell_price_paise, 1.0)
    return int(clamp(math.floor(headroom * 100 / 5) * 5, 0, CLEARANCE_MAX_DISCOUNT_PCT))
