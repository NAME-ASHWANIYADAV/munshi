"""Turn live database state into :class:`MemoryFact` records.

Nothing here invents a number (SPEC.md §2.2): every figure in every node's text is computed
from the tables it describes. The text is written in **English and Hindi** because retrieval
is lexical — a merchant who asks *"कल कितना आया?"* has to hit the same node as one who asks
*"kal ka collection kitna tha"*.

The demo-critical function is :func:`action_facts`. An action executed in conversation #1
becomes ``(action)-[targeted]->(customer)``, ``(action)-[addressed]->(insight)`` and
``(action)-[on_day]->(day)`` with its ``ActionOutcome`` rows spelled out in the node text, so
conversation #2 can answer *"pichli baar jo offer bheja tha uska kya hua?"* from memory alone.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from munshiji.clock import (
    day_bounds_ist,
    days_between,
    ist_date_of,
    now_utc,
    range_bounds_ist,
    to_ist,
    today_ist,
    weekday_name,
)
from munshiji.db.enums import ActionStatus, CustomerSegment, KhataStatus, MemoryKind
from munshiji.db.models import (
    ActionRequest,
    Conversation,
    Customer,
    Insight,
    KhataEntry,
    Merchant,
    Product,
    Transaction,
    TransactionItem,
)
from munshiji.logging import get_logger
from munshiji.money import fmt_inr
from munshiji.providers.memory import MemoryEdgeSpec, MemoryFact, node_ref

__all__ = [
    "action_facts",
    "conversation_facts",
    "customer_facts",
    "daily_rollup_facts",
    "day_key",
    "ingest_all",
    "insight_facts",
    "merchant_facts",
    "product_facts",
]

logger = get_logger(__name__)

#: Hindi month names, indexed 1–12. ``clock.py`` owns weekdays; months live here because this
#: is the only module that renders dates into prose.
MONTHS_HI = (
    "",
    "जनवरी",
    "फ़रवरी",
    "मार्च",
    "अप्रैल",
    "मई",
    "जून",
    "जुलाई",
    "अगस्त",
    "सितंबर",
    "अक्तूबर",
    "नवंबर",
    "दिसंबर",
)

SEGMENT_HI: dict[str, str] = {
    CustomerSegment.CHAMPION.value: "सबसे खास ग्राहक",
    CustomerSegment.LOYAL.value: "पक्का ग्राहक",
    CustomerSegment.REGULAR.value: "नियमित ग्राहक",
    CustomerSegment.OCCASIONAL.value: "कभी-कभी आने वाला",
    CustomerSegment.NEW.value: "नया ग्राहक",
    CustomerSegment.AT_RISK.value: "छूटने वाला ग्राहक",
    CustomerSegment.DORMANT.value: "बंद हो चुका ग्राहक",
    CustomerSegment.LOST.value: "खो चुका ग्राहक",
}

#: Weight scale for edges. Structural edges are deliberately weak so the merchant hub node
#: never dominates a multi-hop expansion; semantic edges (who an action targeted) are strong.
W_STRUCTURAL = 0.25
W_CONTEXT = 0.5
W_SEMANTIC = 0.9

#: Cap on ``targeted`` edges per action — a broadcast to the whole customer base should not
#: turn one node into a 250-way hub.
MAX_TARGET_EDGES = 60

_DEAD_STOCK_DAYS = 45
_DEAD_STOCK_MIN_CAPITAL_PAISE = 50_000  # ₹500, per SPEC.md §7
_TOP_SELLERS = 8
_ID_RE = re.compile(r"\b(cus|prd|ins|act)_[0-9A-Z]{8,}\b")


def _enum_value(value: Any) -> str:
    """``MemoryKind.DAY`` or the raw string it was stored as — both render the same."""
    return value.value if hasattr(value, "value") else str(value)


def _article(word: str) -> str:
    """``"a"`` or ``"an"`` — segment names are interpolated into English prose."""
    return "an" if word[:1].lower() in "aeiou" else "a"


def day_key(value: date | datetime) -> str:
    """Canonical key for a ``DAY`` node: the ISO **IST** calendar date."""
    day = ist_date_of(value) if isinstance(value, datetime) else value
    return day.isoformat()


def _date_en(day: date) -> str:
    return day.strftime("%d %b %Y").lstrip("0")


def _date_hi(day: date) -> str:
    return f"{day.day} {MONTHS_HI[day.month]} {day.year}"


def _extract_ids(payload: Any, prefix: str, *, depth: int = 0) -> list[str]:
    """Recursively pull ``prefix_…`` identifiers out of a JSON blob.

    Insight and action payloads are authored by other modules and their key names will drift;
    scanning for the id *shape* is far more robust than guessing ``customer_ids`` vs
    ``recipients`` vs ``targets``.
    """
    if depth > 4 or payload is None:
        return []
    found: list[str] = []
    if isinstance(payload, str):
        if payload.startswith(f"{prefix}_"):
            found.append(payload)
        else:
            found.extend(
                match.group(0)
                for match in _ID_RE.finditer(payload)
                if match.group(0).startswith(f"{prefix}_")
            )
    elif isinstance(payload, dict):
        for value in payload.values():
            found.extend(_extract_ids(value, prefix, depth=depth + 1))
    elif isinstance(payload, list | tuple):
        for value in payload:
            found.extend(_extract_ids(value, prefix, depth=depth + 1))
    return list(dict.fromkeys(found))


# ─────────────────────────────────────────────────────────────────────────────
# Merchant
# ─────────────────────────────────────────────────────────────────────────────


def merchant_facts(session: Session, merchant_id: str) -> list[MemoryFact]:
    """The shop itself: who runs it, what it sells, when it opens (SPEC.md §8)."""
    merchant = session.get(Merchant, merchant_id)
    if merchant is None:
        return []

    where = ", ".join(part for part in (merchant.locality, merchant.city) if part)
    hours = f"{merchant.business_hours_start:02d}:00–{merchant.business_hours_end:02d}:00 IST"
    opened = _date_en(to_ist(merchant.opened_at).date()) if merchant.opened_at else ""
    rent = fmt_inr(merchant.monthly_rent_paise) if merchant.monthly_rent_paise else ""

    english = (
        f"{merchant.shop_name} is a {merchant.category} shop owned by {merchant.owner_name}"
        f"{f' in {where}' if where else ''}. Business hours {hours}, open daily."
    )
    if opened:
        english += f" Shop opened {opened}."
    if rent:
        english += f" Monthly rent {rent}."

    hindi = (
        f"{merchant.shop_name} एक {merchant.category} दुकान है, मालिक {merchant.owner_name}"
        f"{f', {where} में' if where else ''}। दुकान रोज़ {hours} खुलती है।"
    )
    if rent:
        hindi += f" महीने का किराया {rent}।"

    return [
        MemoryFact(
            kind=MemoryKind.MERCHANT,
            key=merchant.id,
            label=merchant.shop_name,
            text=f"{english} {hindi}",
            attrs={
                "owner_name": merchant.owner_name,
                "shop_name": merchant.shop_name,
                "category": merchant.category,
                "city": merchant.city,
                "locality": merchant.locality,
                "language": merchant.language,
                "business_hours": hours,
                "business_hours_start": merchant.business_hours_start,
                "business_hours_end": merchant.business_hours_end,
                "monthly_rent_paise": merchant.monthly_rent_paise,
            },
            occurred_at=None,  # an always-true fact: no recency decay
        )
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Daily rollups
# ─────────────────────────────────────────────────────────────────────────────


def daily_rollup_facts(session: Session, merchant_id: str, *, days: int = 45) -> list[MemoryFact]:
    """One ``DAY`` node per IST calendar date: collection, sales count, top category, mix."""
    days = max(1, int(days))
    last_day = today_ist()
    first_day = last_day - timedelta(days=days - 1)
    start, end = range_bounds_ist(first_day, last_day)

    rows = session.execute(
        select(
            Transaction.id,
            Transaction.amount_paise,
            Transaction.occurred_at,
            Transaction.payment_method,
        ).where(
            Transaction.merchant_id == merchant_id,
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
            Transaction.is_return.is_(False),
        )
    ).all()
    if not rows:
        return []

    totals: dict[date, int] = defaultdict(int)
    counts: dict[date, int] = defaultdict(int)
    mix: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for _txn_id, amount, occurred_at, method in rows:
        day = ist_date_of(occurred_at)
        totals[day] += int(amount)
        counts[day] += 1
        mix[day][_enum_value(method)] += int(amount)

    category_rows = session.execute(
        select(Transaction.occurred_at, Product.category, TransactionItem.line_total_paise)
        .join(TransactionItem, TransactionItem.transaction_id == Transaction.id)
        .join(Product, Product.id == TransactionItem.product_id)
        .where(
            Transaction.merchant_id == merchant_id,
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
            Transaction.is_return.is_(False),
        )
    ).all()
    categories: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for occurred_at, category, line_total in category_rows:
        categories[ist_date_of(occurred_at)][category] += int(line_total or 0)

    facts: list[MemoryFact] = []
    merchant_target = node_ref(MemoryKind.MERCHANT, merchant_id)
    for day in sorted(totals):
        total = totals[day]
        count = counts[day]
        average = total // count if count else 0
        mix_pct: dict[str, float] = {}
        if total:
            mix_pct = {
                method: round(amount * 100.0 / total, 1) for method, amount in mix[day].items()
            }
        mix_text = ", ".join(
            f"{method.upper()} {share:.0f}%"
            for method, share in sorted(mix_pct.items(), key=lambda kv: -kv[1])
        )
        top_category, top_category_paise = "", 0
        if categories.get(day):
            top_category, top_category_paise = max(categories[day].items(), key=lambda kv: kv[1])

        weekday_en = weekday_name(day, hindi=False)
        weekday_hi = weekday_name(day, hindi=True)

        english = (
            f"{_date_en(day)} ({weekday_hi}): collection {fmt_inr(total)} " f"across {count} sales."
        )
        english += f" It was a {weekday_en}; average ticket {fmt_inr(average)}."
        if top_category:
            english += (
                f" Best-selling category was {top_category} at {fmt_inr(top_category_paise)}."
            )
        if mix_text:
            english += f" Payment mix: {mix_text}."

        hindi = (
            f"{_date_hi(day)} ({weekday_hi}): कुल वसूली {fmt_inr(total)}, "
            f"{count} बिक्री, औसत बिल {fmt_inr(average)}।"
        )
        if top_category:
            hindi += f" सबसे ज़्यादा बिक्री {top_category} में हुई।"
        if mix_text:
            hindi += f" भुगतान: {mix_text}।"

        facts.append(
            MemoryFact(
                kind=MemoryKind.DAY,
                key=day_key(day),
                label=f"{_date_en(day)} ({weekday_en})",
                text=f"{english} {hindi}",
                attrs={
                    "date": day.isoformat(),
                    "weekday": weekday_en,
                    "weekday_hi": weekday_hi,
                    "collection_paise": total,
                    "txn_count": count,
                    "avg_ticket_paise": average,
                    "top_category": top_category,
                    "top_category_paise": top_category_paise,
                    "payment_mix_pct": mix_pct,
                },
                occurred_at=day_bounds_ist(day)[0],
                edges=[MemoryEdgeSpec(rel="at_shop", target=merchant_target, weight=W_STRUCTURAL)],
            )
        )
    return facts


# ─────────────────────────────────────────────────────────────────────────────
# Customers
# ─────────────────────────────────────────────────────────────────────────────


def _khata_outstanding(session: Session, merchant_id: str) -> dict[str, int]:
    """Open udhaar per customer, in paise."""
    rows = session.execute(
        select(KhataEntry.customer_id, KhataEntry.amount_paise, KhataEntry.paid_paise).where(
            KhataEntry.merchant_id == merchant_id,
            KhataEntry.status.in_([KhataStatus.OPEN, KhataStatus.PARTIAL]),
        )
    ).all()
    outstanding: dict[str, int] = defaultdict(int)
    for customer_id, amount, paid in rows:
        outstanding[customer_id] += max(0, int(amount) - int(paid or 0))
    return dict(outstanding)


def _customer_fact(customer: Customer, *, outstanding_paise: int, as_of: datetime) -> MemoryFact:
    """One customer node. Cadence is derived from real first/last-seen and visit count."""
    segment = customer.segment.value if customer.segment else "unclassified"
    segment_hi = SEGMENT_HI.get(segment, "ग्राहक")
    visits = int(customer.txn_count or 0)
    spend = int(customer.total_spend_paise or 0)
    average = spend // visits if visits else 0

    cadence_days: float | None = None
    if customer.first_seen_at and customer.last_seen_at and visits > 1:
        span = days_between(customer.first_seen_at, customer.last_seen_at)
        if span > 0:
            cadence_days = round(span / (visits - 1), 1)

    since = days_between(customer.last_seen_at, as_of) if customer.last_seen_at else None
    last_seen_en = _date_en(to_ist(customer.last_seen_at).date()) if customer.last_seen_at else "—"
    last_seen_hi = _date_hi(to_ist(customer.last_seen_at).date()) if customer.last_seen_at else "—"

    segment_en = segment.replace("_", "-")
    english = (
        f"{customer.name} is {_article(segment_en)} {segment_en} customer: {visits} visits, "
        f"lifetime spend {fmt_inr(spend)}, average ticket {fmt_inr(average)}."
    )
    if cadence_days:
        english += f" Usually shops about every {cadence_days:g} days."
    english += f" Last seen {last_seen_en}"
    english += f" ({since} days ago)." if since is not None else "."
    if outstanding_paise:
        english += f" Khata outstanding {fmt_inr(outstanding_paise)}."

    hindi = (
        f"{customer.name} — {segment_hi}। {visits} बार आए, कुल खरीद {fmt_inr(spend)}, "
        f"औसत बिल {fmt_inr(average)}। आखिरी बार {last_seen_hi}"
    )
    hindi += f" ({since} दिन पहले)।" if since is not None else "।"
    if outstanding_paise:
        hindi += f" उधार बाकी {fmt_inr(outstanding_paise)}।"

    return MemoryFact(
        kind=MemoryKind.CUSTOMER,
        key=customer.id,
        label=customer.name,
        text=f"{english} {hindi}",
        attrs={
            "name": customer.name,
            "phone": customer.phone,
            "segment": segment,
            "segment_hi": segment_hi,
            "visits": visits,
            "lifetime_spend_paise": spend,
            "avg_ticket_paise": average,
            "cadence_days": cadence_days,
            "days_since_last_visit": since,
            "last_seen": customer.last_seen_at.isoformat() if customer.last_seen_at else None,
            "is_khata_customer": customer.is_khata_customer,
            "khata_outstanding_paise": outstanding_paise,
            "tags": customer.tags or {},
        },
        occurred_at=customer.last_seen_at,
        edges=[
            MemoryEdgeSpec(
                rel="shops_at",
                target=node_ref(MemoryKind.MERCHANT, customer.merchant_id),
                weight=W_STRUCTURAL,
            )
        ],
    )


def customer_facts(session: Session, merchant_id: str, *, limit: int = 250) -> list[MemoryFact]:
    """The merchant's customers, highest lifetime spend first."""
    customers = list(
        session.scalars(
            select(Customer)
            .where(Customer.merchant_id == merchant_id)
            .order_by(Customer.total_spend_paise.desc(), Customer.id)
            .limit(max(1, int(limit)))
        ).all()
    )
    if not customers:
        return []
    outstanding = _khata_outstanding(session, merchant_id)
    as_of = now_utc()
    return [
        _customer_fact(customer, outstanding_paise=outstanding.get(customer.id, 0), as_of=as_of)
        for customer in customers
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Products
# ─────────────────────────────────────────────────────────────────────────────


def _product_fact(
    product: Product,
    *,
    reasons: Sequence[str],
    sold_paise: int,
    sold_qty: float,
    days_since_sale: int | None,
) -> MemoryFact:
    cover = ""
    if product.reorder_level:
        cover = f" Reorder level {product.reorder_level:g} {product.unit}."

    english = (
        f"{product.name} (SKU {product.sku}, {product.category}) — stock "
        f"{product.stock_qty:g} {product.unit} worth {fmt_inr(product.stock_value_paise)} at cost, "
        f"sells at {fmt_inr(product.sell_price_paise)} with {fmt_inr(product.margin_paise)} margin."
        f"{cover}"
    )
    if sold_paise or sold_qty:
        english += (
            f" Sold {sold_qty:g} {product.unit} for {fmt_inr(sold_paise)} in the last "
            f"{_DEAD_STOCK_DAYS} days."
        )
    if days_since_sale is None:
        english += f" No sale recorded in the last {_DEAD_STOCK_DAYS} days."
    elif days_since_sale > 0:
        english += f" Last sold {days_since_sale} days ago."
    english += f" Flagged as: {', '.join(reasons)}."

    name_hi = product.name_hi or product.name
    hindi = (
        f"{name_hi} ({product.sku}): स्टॉक {product.stock_qty:g} {product.unit}, "
        f"कीमत {fmt_inr(product.stock_value_paise)}।"
    )
    if days_since_sale is None:
        hindi += f" पिछले {_DEAD_STOCK_DAYS} दिन में एक भी बिक्री नहीं।"
    elif sold_paise:
        hindi += f" पिछले {_DEAD_STOCK_DAYS} दिन में {fmt_inr(sold_paise)} की बिक्री।"

    return MemoryFact(
        kind=MemoryKind.PRODUCT,
        key=product.id,
        label=product.name,
        text=f"{english} {hindi}",
        attrs={
            "sku": product.sku,
            "name": product.name,
            "name_hi": product.name_hi,
            "category": product.category,
            "unit": product.unit,
            "stock_qty": product.stock_qty,
            "reorder_level": product.reorder_level,
            "stock_value_paise": product.stock_value_paise,
            "sell_price_paise": product.sell_price_paise,
            "cost_price_paise": product.cost_price_paise,
            "margin_paise": product.margin_paise,
            "sold_recent_paise": sold_paise,
            "sold_recent_qty": sold_qty,
            "days_since_sale": days_since_sale,
            "flags": list(reasons),
            "is_perishable": product.is_perishable,
        },
        occurred_at=product.last_restocked_at,
        edges=[
            MemoryEdgeSpec(
                rel="stocked_by",
                target=node_ref(MemoryKind.MERCHANT, product.merchant_id),
                weight=W_STRUCTURAL,
            )
        ],
    )


def product_facts(session: Session, merchant_id: str) -> list[MemoryFact]:
    """Notable SKUs only: top sellers, dead stock and anything at or below reorder level.

    Indexing all 200-odd SKUs would bury the interesting ones under shelf filler.
    """
    products = list(
        session.scalars(select(Product).where(Product.merchant_id == merchant_id)).all()
    )
    if not products:
        return []

    last_day = today_ist()
    start, end = range_bounds_ist(last_day - timedelta(days=_DEAD_STOCK_DAYS - 1), last_day)
    rows = session.execute(
        select(
            TransactionItem.product_id,
            TransactionItem.qty,
            TransactionItem.line_total_paise,
            Transaction.occurred_at,
        )
        .join(Transaction, Transaction.id == TransactionItem.transaction_id)
        .where(
            Transaction.merchant_id == merchant_id,
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
            Transaction.is_return.is_(False),
        )
    ).all()

    sold_paise: dict[str, int] = defaultdict(int)
    sold_qty: dict[str, float] = defaultdict(float)
    last_sold: dict[str, datetime] = {}
    for product_id, qty, line_total, occurred_at in rows:
        sold_paise[product_id] += int(line_total or 0)
        sold_qty[product_id] += float(qty or 0.0)
        if product_id not in last_sold or occurred_at > last_sold[product_id]:
            last_sold[product_id] = occurred_at

    reasons: dict[str, list[str]] = defaultdict(list)
    ranked = sorted(products, key=lambda p: -sold_paise.get(p.id, 0))
    for product in ranked[:_TOP_SELLERS]:
        if sold_paise.get(product.id, 0) > 0:
            reasons[product.id].append("top seller")
    for product in products:
        locked = product.stock_value_paise
        if product.id not in last_sold and locked >= _DEAD_STOCK_MIN_CAPITAL_PAISE:
            reasons[product.id].append("dead stock")
        if product.reorder_level > 0 and product.stock_qty <= product.reorder_level:
            reasons[product.id].append("at or below reorder level")

    as_of = today_ist()
    facts = []
    for product in products:
        if not reasons.get(product.id):
            continue
        seen = last_sold.get(product.id)
        facts.append(
            _product_fact(
                product,
                reasons=reasons[product.id],
                sold_paise=sold_paise.get(product.id, 0),
                sold_qty=round(sold_qty.get(product.id, 0.0), 2),
                days_since_sale=days_between(seen, as_of) if seen else None,
            )
        )
    return facts


# ─────────────────────────────────────────────────────────────────────────────
# Insights
# ─────────────────────────────────────────────────────────────────────────────


def insight_facts(session: Session, merchant_id: str, *, limit: int = 25) -> list[MemoryFact]:
    """Open insights, linked to the customers and products they are about."""
    insights = list(
        session.scalars(
            select(Insight)
            .where(Insight.merchant_id == merchant_id, Insight.status == "open")
            .order_by(Insight.score.desc(), Insight.created_at.desc())
            .limit(max(1, int(limit)))
        ).all()
    )
    facts: list[MemoryFact] = []
    for insight in insights:
        kind = _enum_value(insight.kind)
        severity = _enum_value(insight.severity)
        impact = fmt_inr(insight.impact_paise) if insight.impact_paise else ""
        raised = _date_en(to_ist(insight.created_at).date())

        english = (
            f"Insight ({kind}, {severity} severity) raised {raised}: {insight.title_en}. "
            f"{insight.body_en}"
        )
        if impact:
            english += f" Estimated impact {impact}."
        if insight.suggested_tool:
            english += f" Suggested next step: {insight.suggested_tool}."
        hindi = f"{insight.title_hi} — {insight.body_hi}"
        if impact:
            hindi += f" अनुमानित असर {impact}।"

        edges = [
            MemoryEdgeSpec(
                rel="for_shop",
                target=node_ref(MemoryKind.MERCHANT, merchant_id),
                weight=W_STRUCTURAL,
            )
        ]
        payload = [insight.metrics or {}, insight.suggested_params or {}]
        for customer_id in _extract_ids(payload, "cus")[:MAX_TARGET_EDGES]:
            edges.append(
                MemoryEdgeSpec(
                    rel="about",
                    target=node_ref(MemoryKind.CUSTOMER, customer_id),
                    weight=W_CONTEXT,
                )
            )
        for product_id in _extract_ids(payload, "prd")[:MAX_TARGET_EDGES]:
            edges.append(
                MemoryEdgeSpec(
                    rel="about",
                    target=node_ref(MemoryKind.PRODUCT, product_id),
                    weight=W_CONTEXT,
                )
            )

        facts.append(
            MemoryFact(
                kind=MemoryKind.INSIGHT,
                key=insight.id,
                label=insight.title_en,
                text=f"{english} {hindi}",
                attrs={
                    "insight_kind": kind,
                    "severity": severity,
                    "title_en": insight.title_en,
                    "title_hi": insight.title_hi,
                    "impact_paise": insight.impact_paise,
                    "confidence": insight.confidence,
                    "score": insight.score,
                    "status": insight.status,
                    "suggested_tool": insight.suggested_tool or "",
                    "metrics": insight.metrics or {},
                },
                occurred_at=insight.created_at,
                edges=edges,
            )
        )
    return facts


# ─────────────────────────────────────────────────────────────────────────────
# Actions — the demo-critical path
# ─────────────────────────────────────────────────────────────────────────────

#: Metric name → (English phrasing, Hindi phrasing). Anything unrecognised falls back to
#: ``"metric value"``, so a new outcome metric degrades gracefully instead of disappearing.
_OUTCOME_PHRASES: dict[str, tuple[str, str]] = {
    "sent": ("{n} messages sent", "{n} संदेश भेजे गए"),
    "delivered": ("{n} delivered", "{n} पहुँचे"),
    "opened": ("{n} opened", "{n} ने खोला"),
    "redeemed": ("{n} redeemed", "{n} ने इस्तेमाल किया"),
    "responded": ("{n} replied", "{n} ने जवाब दिया"),
    "returned": ("{n} came back to the shop", "{n} ग्राहक दोबारा आए"),
    "visits": ("{n} return visits", "{n} बार दोबारा आए"),
    "recovered": ("{money} recovered", "{money} वसूल हुए"),
    "revenue": ("{money} of revenue", "{money} की बिक्री"),
    "settled": ("{money} of udhaar settled", "{money} का उधार चुका"),
}


def _phrase_outcome(
    metric: str, value_num: float | None, value_paise: int | None
) -> tuple[str, str]:
    """Render one ``ActionOutcome`` row into English and Hindi clauses."""
    key = metric.lower().removesuffix("_paise").removesuffix("_count")
    number = ""
    if value_num is not None:
        number = f"{value_num:g}"
    money = fmt_inr(value_paise) if value_paise is not None else ""
    template = _OUTCOME_PHRASES.get(key)
    if template is None:
        tail = money or number or "recorded"
        return f"{key.replace('_', ' ')} {tail}", f"{key.replace('_', ' ')} {tail}"
    english, hindi = template
    if "{money}" in english and not money:
        money = number
    if "{n}" in english and not number:
        number = money
    return (
        english.format(n=number, money=money).strip(),
        hindi.format(n=number, money=money).strip(),
    )


def action_facts(session: Session, merchant_id: str, *, limit: int = 100) -> list[MemoryFact]:
    """Every action MunshiJi proposed, with what happened next.

    This is the node that makes cross-session recall work: text states what was sent, when,
    to how many, and every measured ``ActionOutcome``; edges tie it to the customers it
    targeted, the insight it addressed and the day it ran.
    """
    actions = list(
        session.scalars(
            select(ActionRequest)
            .options(selectinload(ActionRequest.outcomes))
            .where(ActionRequest.merchant_id == merchant_id)
            .order_by(ActionRequest.requested_at.desc())
            .limit(max(1, int(limit)))
        ).all()
    )

    facts: list[MemoryFact] = []
    for action in actions:
        status = _enum_value(action.status)
        when = action.executed_at or action.decided_at or action.requested_at
        when_ist = to_ist(when)
        recipients = _extract_ids(action.params or {}, "cus")
        targets = int(action.target_count or len(recipients))
        tool = action.tool_name.replace("_", " ")
        summary_en = action.summary_en or f"{tool} for {targets} customers"
        summary_hi = action.summary_hi or ""

        english = (
            f"Action {action.tool_name} ({status}): {summary_en}. "
            f"Sent to {targets} customers on {_date_en(when_ist.date())}"
            f" at {when_ist.strftime('%H:%M')} IST."
        )
        if action.estimated_impact_paise:
            english += f" Estimated impact was {fmt_inr(action.estimated_impact_paise)}."

        # Prefer the authored Hindi summary — it already names the audience, so the count
        # goes in parentheses rather than being stated twice. The romanised tool name only
        # appears as a fallback: "12 ग्राहकों को send winback offer भेजा गया" reads like a robot.
        when_hi = _date_hi(when_ist.date())
        if summary_hi:
            hindi = f"{when_hi} को {summary_hi} भेजा गया ({targets} ग्राहक)।"
        else:
            hindi = f"{when_hi} को {targets} ग्राहकों को {tool} भेजा गया।"

        outcome_en: list[str] = []
        outcome_hi: list[str] = []
        outcome_attrs: dict[str, Any] = {}
        for outcome in sorted(action.outcomes, key=lambda row: row.observed_at):
            phrase_en, phrase_hi = _phrase_outcome(
                outcome.metric, outcome.value_num, outcome.value_paise
            )
            outcome_en.append(phrase_en)
            outcome_hi.append(phrase_hi)
            outcome_attrs[outcome.metric] = (
                outcome.value_paise if outcome.value_paise is not None else outcome.value_num
            )
            if outcome.note:
                outcome_en.append(outcome.note)

        if outcome_en:
            english += f" Outcome: {'; '.join(outcome_en)}."
            hindi += f" नतीजा: {'; '.join(outcome_hi)}।"
        elif status == ActionStatus.EXECUTED.value:
            english += " No outcome measured yet."
            hindi += " अभी तक कोई नतीजा दर्ज नहीं हुआ।"

        edges = [
            MemoryEdgeSpec(
                rel="on_day",
                target=node_ref(MemoryKind.DAY, day_key(when)),
                weight=W_CONTEXT,
            )
        ]
        if action.insight_id:
            edges.append(
                MemoryEdgeSpec(
                    rel="addressed",
                    target=node_ref(MemoryKind.INSIGHT, action.insight_id),
                    weight=W_SEMANTIC,
                )
            )
        for customer_id in recipients[:MAX_TARGET_EDGES]:
            edges.append(
                MemoryEdgeSpec(
                    rel="targeted",
                    target=node_ref(MemoryKind.CUSTOMER, customer_id),
                    weight=W_SEMANTIC,
                )
            )

        facts.append(
            MemoryFact(
                kind=MemoryKind.ACTION,
                key=action.id,
                label=summary_en[:200],
                text=f"{english} {hindi}",
                attrs={
                    "tool_name": action.tool_name,
                    "status": status,
                    "target_count": targets,
                    "estimated_impact_paise": action.estimated_impact_paise,
                    "requested_at": action.requested_at.isoformat(),
                    "executed_at": action.executed_at.isoformat() if action.executed_at else None,
                    "provider": action.provider,
                    "params": action.params or {},
                    "outcomes": outcome_attrs,
                    "recipient_count": len(recipients),
                },
                occurred_at=when,
                edges=edges,
            )
        )
    return facts


# ─────────────────────────────────────────────────────────────────────────────
# Conversations
# ─────────────────────────────────────────────────────────────────────────────


def conversation_facts(session: Session, merchant_id: str, *, limit: int = 20) -> list[MemoryFact]:
    """Conversation summaries, linked to whatever actions they produced."""
    conversations = list(
        session.scalars(
            select(Conversation)
            .options(selectinload(Conversation.turns))
            .where(Conversation.merchant_id == merchant_id)
            .order_by(Conversation.started_at.desc())
            .limit(max(1, int(limit)))
        ).all()
    )
    if not conversations:
        return []

    action_rows = session.execute(
        select(ActionRequest.conversation_id, ActionRequest.id, ActionRequest.tool_name).where(
            ActionRequest.merchant_id == merchant_id,
            ActionRequest.conversation_id.is_not(None),
        )
    ).all()
    by_conversation: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for conversation_id, action_id, tool_name in action_rows:
        by_conversation[conversation_id].append((action_id, tool_name))

    facts: list[MemoryFact] = []
    for conversation in conversations:
        turns = list(conversation.turns)
        summary = conversation.summary.strip()
        if not summary:
            # Derive one from the real turns rather than storing a placeholder.
            asked = next(
                (t.text for t in turns if _enum_value(t.role) == "merchant" and t.text), ""
            )
            answered = next(
                (t.text for t in reversed(turns) if _enum_value(t.role) == "munshi" and t.text),
                "",
            )
            pieces = []
            if asked:
                pieces.append(f'merchant asked "{asked.strip()[:160]}"')
            if answered:
                pieces.append(f'MunshiJi answered "{answered.strip()[:160]}"')
            summary = "; ".join(pieces) or "no transcript recorded"

        started = to_ist(conversation.started_at)
        channel = _enum_value(conversation.channel)
        created = by_conversation.get(conversation.id, [])
        english = (
            f"Conversation on {_date_en(started.date())} at {started.strftime('%H:%M')} IST "
            f"({channel}, {conversation.language}), {len(turns)} turns: {summary}"
        )
        if created:
            english += f" Actions created here: {', '.join(tool for _, tool in created)}."
        hindi = f"{_date_hi(started.date())} की बातचीत ({channel}), {len(turns)} बार बात हुई।"
        if created:
            hindi += f" इसमें {len(created)} काम शुरू हुए।"

        edges = [
            MemoryEdgeSpec(
                rel="at_shop",
                target=node_ref(MemoryKind.MERCHANT, merchant_id),
                weight=W_STRUCTURAL,
            )
        ]
        edges.extend(
            MemoryEdgeSpec(
                rel="created", target=node_ref(MemoryKind.ACTION, action_id), weight=W_SEMANTIC
            )
            for action_id, _ in created
        )

        facts.append(
            MemoryFact(
                kind=MemoryKind.CONVERSATION,
                key=conversation.id,
                label=f"Conversation {_date_en(started.date())} {started.strftime('%H:%M')}",
                text=f"{english} {hindi}",
                attrs={
                    "channel": channel,
                    "language": conversation.language,
                    "turn_count": len(turns),
                    "summary": summary,
                    "action_ids": [action_id for action_id, _ in created],
                },
                occurred_at=conversation.started_at,
                edges=edges,
            )
        )
    return facts


# ─────────────────────────────────────────────────────────────────────────────
# Composition
# ─────────────────────────────────────────────────────────────────────────────


def _backfill_edge_targets(
    session: Session, merchant_id: str, facts: list[MemoryFact]
) -> list[MemoryFact]:
    """Create nodes for customers/products referenced by an edge but not otherwise ingested.

    An action can target a customer outside the top-250 by spend; without this, the
    demo-critical ``targeted`` edge would silently fail to resolve.
    """
    have = {fact.ref for fact in facts}
    wanted_customers: list[str] = []
    wanted_products: list[str] = []
    for fact in facts:
        for spec in fact.edges:
            if spec.target in have:
                continue
            kind_key = spec.target.split(":", 1)
            if len(kind_key) != 2:
                continue
            kind, key = kind_key
            if kind == MemoryKind.CUSTOMER.value:
                wanted_customers.append(key)
            elif kind == MemoryKind.PRODUCT.value:
                wanted_products.append(key)

    extra: list[MemoryFact] = []
    if wanted_customers:
        outstanding = _khata_outstanding(session, merchant_id)
        as_of = now_utc()
        rows = session.scalars(
            select(Customer).where(
                Customer.merchant_id == merchant_id,
                Customer.id.in_(list(dict.fromkeys(wanted_customers))),
            )
        ).all()
        extra.extend(
            _customer_fact(customer, outstanding_paise=outstanding.get(customer.id, 0), as_of=as_of)
            for customer in rows
        )
    if wanted_products:
        rows = session.scalars(
            select(Product).where(
                Product.merchant_id == merchant_id,
                Product.id.in_(list(dict.fromkeys(wanted_products))),
            )
        ).all()
        extra.extend(
            _product_fact(
                product,
                reasons=["referenced by an insight or action"],
                sold_paise=0,
                sold_qty=0.0,
                days_since_sale=None,
            )
            for product in rows
        )
    return extra


def _dedupe(facts: Iterable[MemoryFact]) -> list[MemoryFact]:
    """Last writer wins per ref; edges from every occurrence are merged."""
    merged: dict[str, MemoryFact] = {}
    for fact in facts:
        existing = merged.get(fact.ref)
        if existing is None:
            merged[fact.ref] = fact
            continue
        seen = {(spec.rel, spec.target) for spec in fact.edges}
        fact.edges = [
            *fact.edges,
            *(spec for spec in existing.edges if (spec.rel, spec.target) not in seen),
        ]
        merged[fact.ref] = fact
    return list(merged.values())


def ingest_all(session: Session, merchant_id: str) -> list[MemoryFact]:
    """Every fact worth remembering about one merchant, ready for ``MemoryProvider.ingest``."""
    facts: list[MemoryFact] = []
    facts.extend(merchant_facts(session, merchant_id))
    facts.extend(daily_rollup_facts(session, merchant_id))
    facts.extend(customer_facts(session, merchant_id))
    facts.extend(product_facts(session, merchant_id))
    facts.extend(insight_facts(session, merchant_id))
    facts.extend(action_facts(session, merchant_id))
    facts.extend(conversation_facts(session, merchant_id))
    facts = _dedupe(facts)
    facts = _dedupe([*facts, *_backfill_edge_targets(session, merchant_id, facts)])
    logger.debug("built %d memory facts for %s", len(facts), merchant_id)
    return facts
