"""Inventory tools — what is about to run out, and what is sitting dead on the shelf."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from munshiji.agent.tools.base import Tool, ToolContext, ToolResult
from munshiji.clock import ist_date_of
from munshiji.money import fmt_inr
from munshiji.repositories import analytics
from munshiji.repositories.core import list_products

__all__ = ["TOOLS"]

#: Supplier lead time and safety buffer, in days. Kept here so the tool and the insight engine
#: answer the same question the same way.
LEAD_TIME_DAYS = 3.0
SAFETY_DAYS = 1.0
DEAD_STOCK_DAYS = 45
DEAD_STOCK_MIN_CAPITAL_PAISE = 50_000  # ₹500


class InventoryAlertsParams(BaseModel):
    limit: int = Field(default=5, ge=1, le=20, description="Maximum items per alert category.")


async def _inventory_alerts(ctx: ToolContext, params: InventoryAlertsParams) -> ToolResult:
    today = ist_date_of(ctx.as_of)
    movements = analytics.product_movements(ctx.session, ctx.merchant_id, as_of=ctx.as_of)

    running_out: list[dict[str, object]] = []
    dead_stock: list[dict[str, object]] = []
    expiring: list[dict[str, object]] = []

    for movement in movements.values():
        product = movement.product
        rate = movement.rate_units_per_day
        days_of_cover = product.stock_qty / rate if rate > 0.01 else None

        if days_of_cover is not None and days_of_cover < LEAD_TIME_DAYS + SAFETY_DAYS:
            suggested = max(0.0, rate * (LEAD_TIME_DAYS + 7.0) - product.stock_qty)
            running_out.append(
                {
                    "sku": product.sku,
                    "name": product.name,
                    "name_hi": product.name_hi,
                    "stock_qty": round(product.stock_qty, 2),
                    "unit": product.unit,
                    "daily_rate": round(rate, 2),
                    "days_of_cover": round(days_of_cover, 1),
                    "suggested_order_qty": round(suggested, 1),
                }
            )

        days_since_sale = (
            (today - movement.last_sale_day).days if movement.last_sale_day else DEAD_STOCK_DAYS + 1
        )
        capital = product.stock_value_paise
        if days_since_sale >= DEAD_STOCK_DAYS and capital >= DEAD_STOCK_MIN_CAPITAL_PAISE:
            dead_stock.append(
                {
                    "sku": product.sku,
                    "name": product.name,
                    "name_hi": product.name_hi,
                    "days_since_sale": days_since_sale,
                    "stock_qty": round(product.stock_qty, 2),
                    "capital_locked_paise": capital,
                    "capital_locked_display": fmt_inr(capital),
                }
            )

        if product.is_perishable and product.shelf_life_days and product.last_restocked_at:
            age = (today - ist_date_of(product.last_restocked_at)).days
            remaining_shelf = product.shelf_life_days - age
            if days_of_cover is not None and remaining_shelf < days_of_cover:
                at_risk_units = max(
                    0.0, product.stock_qty - max(0.0, rate * max(0, remaining_shelf))
                )
                value = int(round(at_risk_units * product.cost_price_paise))
                expiring.append(
                    {
                        "sku": product.sku,
                        "name": product.name,
                        "name_hi": product.name_hi,
                        "days_left": max(0, remaining_shelf),
                        "days_of_cover": round(days_of_cover, 1),
                        "units_at_risk": round(at_risk_units, 1),
                        "value_at_risk_paise": value,
                        "value_at_risk_display": fmt_inr(value),
                    }
                )

    running_out.sort(key=lambda row: row["days_of_cover"])
    dead_stock.sort(key=lambda row: row["capital_locked_paise"], reverse=True)
    expiring.sort(key=lambda row: row["value_at_risk_paise"], reverse=True)

    running_out = running_out[: params.limit]
    dead_stock = dead_stock[: params.limit]
    expiring = expiring[: params.limit]

    locked = sum(int(row["capital_locked_paise"]) for row in dead_stock)

    parts_en = []
    parts_hi = []
    if running_out:
        parts_en.append(f"{len(running_out)} running out")
        parts_hi.append(f"{len(running_out)} khatam hone wale")
    if dead_stock:
        parts_en.append(f"{len(dead_stock)} dead ({fmt_inr(locked)} locked)")
        parts_hi.append(f"{len(dead_stock)} pada hua ({fmt_inr(locked)})")
    if expiring:
        parts_en.append(f"{len(expiring)} at expiry risk")
        parts_hi.append(f"{len(expiring)} kharab hone wale")

    return ToolResult(
        data={
            "running_out": running_out,
            "dead_stock": dead_stock,
            "expiry_risk": expiring,
            "capital_locked_paise": locked,
            "capital_locked_display": fmt_inr(locked),
            "lead_time_days": LEAD_TIME_DAYS,
        },
        summary_en="; ".join(parts_en) or "No inventory alerts",
        summary_hi="; ".join(parts_hi) or "Stock theek hai",
    )


class ProductPerformanceParams(BaseModel):
    category: str | None = Field(
        default=None, description="Restrict to one product category, e.g. 'dairy'."
    )
    limit: int = Field(default=5, ge=1, le=20)
    order: Literal["best", "worst"] = Field(
        default="best", description="Best or worst sellers by revenue in the window."
    )
    days: int = Field(default=30, ge=7, le=180)


async def _product_performance(ctx: ToolContext, params: ProductPerformanceParams) -> ToolResult:
    movements = analytics.product_movements(
        ctx.session, ctx.merchant_id, as_of=ctx.as_of, window_days=params.days
    )
    wanted = None
    if params.category:
        wanted = {
            product.id
            for product in list_products(ctx.session, ctx.merchant_id, category=params.category)
        }

    rows = [
        movement
        for movement in movements.values()
        if wanted is None or movement.product.id in wanted
    ]
    rows.sort(key=lambda m: m.revenue_paise, reverse=params.order == "best")

    items = []
    for movement in rows[: params.limit]:
        product = movement.product
        margin_paise = int(round(movement.units_sold * product.margin_paise))
        items.append(
            {
                "sku": product.sku,
                "name": product.name,
                "name_hi": product.name_hi,
                "category": product.category,
                "units_sold": round(movement.units_sold, 1),
                "revenue_paise": movement.revenue_paise,
                "revenue_display": fmt_inr(movement.revenue_paise),
                "margin_paise": margin_paise,
                "margin_display": fmt_inr(margin_paise),
                "stock_qty": round(product.stock_qty, 2),
            }
        )

    label = "Best" if params.order == "best" else "Slowest"
    names = ", ".join(item["name"] for item in items[:3]) or "none"
    return ToolResult(
        data={
            "items": items,
            "window_days": params.days,
            "category": params.category,
            "order": params.order,
        },
        summary_en=f"{label} sellers ({params.days}d): {names}",
        summary_hi=(
            f"{params.days} din mein "
            f"{'top' if params.order == 'best' else 'sabse kam'}: {names}"
        ),
    )


TOOLS: list[Tool] = [
    Tool(
        name="get_inventory_alerts",
        description=(
            "Stock that needs attention: items about to run out (days of cover below supplier lead "
            "time), dead stock with capital locked in it, and perishables that will expire before "
            "they sell. Use for any question about saman, stock or what to order."
        ),
        params_model=InventoryAlertsParams,
        handler=_inventory_alerts,
        label_en="Stock alerts",
        label_hi="Stock ki haalat",
    ),
    Tool(
        name="get_product_performance",
        description=(
            "Best or slowest selling products over a recent window, with units, revenue and "
            "margin. "
            "Optionally restricted to one category."
        ),
        params_model=ProductPerformanceParams,
        handler=_product_performance,
        label_en="Product performance",
        label_hi="Kaunsa saman chal raha hai",
    ),
]
