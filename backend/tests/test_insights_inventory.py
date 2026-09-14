"""Inventory engines.

Rates are made arithmetically obvious on purpose: a SKU that sells the same number of units every
day has an EWMA equal to that number, whatever alpha is, so ``days_of_cover`` is a division you
can do in your head and the boundaries (45 days idle, ₹500 locked, shelf life vs cover) can be
probed on both sides.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from munshiji.clock import IST, to_utc
from munshiji.db.base import Base
from munshiji.db.enums import Severity
from munshiji.db.models import Merchant, Product, Transaction, TransactionItem
from munshiji.insights.base import InsightContext
from munshiji.insights.inventory import (
    COVER_TARGET_DAYS,
    DEAD_STOCK_DAYS,
    DEAD_STOCK_MIN_CAPITAL_PAISE,
    LEAD_TIME_DAYS,
    SAFETY_DAYS,
    DeadStockEngine,
    ExpiryRiskEngine,
    StockoutRiskEngine,
)
from munshiji.insights.stats import ewma

TODAY = date(2026, 9, 14)
RUPEE = 100

#: A category no festival in the calendar touches, so the restock multiplier stays exactly 1.0
#: and the expected quantity is pure arithmetic.
NEUTRAL_CATEGORY = "general"


def make_session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def at(day: date, hour: int = 11) -> datetime:
    return to_utc(datetime.combine(day, time(hour, 0), tzinfo=IST))


def ist_at(day: date, hour: int = 12) -> datetime:
    return datetime.combine(day, time(hour, 0), tzinfo=IST)


def make_merchant(session: Session) -> Merchant:
    merchant = Merchant(owner_name="Sharma ji", shop_name="Sharma General Store")
    session.add(merchant)
    session.flush()
    return merchant


def make_product(
    session: Session,
    merchant: Merchant,
    sku: str,
    *,
    stock: float,
    cost: int,
    sell: int,
    category: str = NEUTRAL_CATEGORY,
    perishable: bool = False,
    shelf_life_days: int | None = None,
    restocked_days_ago: int | None = None,
    created_days_ago: int = 200,
) -> Product:
    product = Product(
        merchant_id=merchant.id,
        sku=sku,
        name=sku.title(),
        name_hi=f"{sku} हिंदी",
        category=category,
        cost_price_paise=cost,
        sell_price_paise=sell,
        stock_qty=stock,
        is_perishable=perishable,
        shelf_life_days=shelf_life_days,
        last_restocked_at=(
            at(TODAY - timedelta(days=restocked_days_ago))
            if restocked_days_ago is not None
            else None
        ),
        created_at=at(TODAY - timedelta(days=created_days_ago)),
    )
    session.add(product)
    session.flush()
    return product


def sell_units(
    session: Session, merchant: Merchant, product: Product, day: date, qty: float
) -> None:
    total = int(round(qty * product.sell_price_paise))
    txn = Transaction(merchant_id=merchant.id, amount_paise=total, occurred_at=at(day))
    session.add(txn)
    session.flush()
    session.add(
        TransactionItem(
            transaction_id=txn.id,
            product_id=product.id,
            qty=qty,
            unit_price_paise=product.sell_price_paise,
            unit_cost_paise=product.cost_price_paise,
            line_total_paise=total,
        )
    )


def sell_every_day(
    session: Session, merchant: Merchant, product: Product, qty: float, *, days: int = 60
) -> None:
    """``qty`` units on each of the ``days`` complete days before today."""
    for offset in range(1, days + 1):
        sell_units(session, merchant, product, TODAY - timedelta(days=offset), qty)


def ctx_for(session: Session, merchant: Merchant) -> InsightContext:
    return InsightContext(session=session, merchant_id=merchant.id, as_of=ist_at(TODAY))


# ── the rate itself ─────────────────────────────────────────────────────────


def test_ewma_of_a_constant_series_is_that_constant() -> None:
    assert ewma([2.0] * 60, 0.3) == pytest.approx(2.0)
    # And the recursion is the documented one: s0=1, then 0.5*2+0.5*1, then 0.5*3+0.5*1.5.
    assert ewma([1.0, 2.0, 3.0], 0.5) == pytest.approx(2.25)


# ── stockout risk ───────────────────────────────────────────────────────────


def test_stockout_risk_days_of_cover_and_restock_quantity() -> None:
    """2 units/day, 5 in stock -> 2.5 days of cover against a 4-day threshold."""
    session = make_session()
    merchant = make_merchant(session)
    rice = make_product(session, merchant, "RICE", stock=5.0, cost=1_500, sell=2_000)
    other = make_product(session, merchant, "SALT", stock=500.0, cost=100, sell=200)
    sell_every_day(session, merchant, rice, 2.0)
    sell_every_day(session, merchant, other, 1.0)
    session.commit()

    drafts = StockoutRiskEngine().run(ctx_for(session, merchant))
    assert len(drafts) == 1
    metrics = drafts[0].metrics

    assert metrics["sku"] == "RICE"
    assert metrics["rate_units_per_day"] == pytest.approx(2.0)
    assert metrics["days_of_cover"] == pytest.approx(2.5)
    assert metrics["threshold_days"] == LEAD_TIME_DAYS + SAFETY_DAYS == 4
    assert metrics["festival_multiplier"] == pytest.approx(1.0)
    # rate x (lead_time + cover_target) - stock = 2 x 10 - 5
    assert metrics["restock_qty"] == 2.0 * (LEAD_TIME_DAYS + COVER_TARGET_DAYS) - 5
    # units missed over the (4 - 2.5) uncovered days, at the shelf price
    assert metrics["units_at_risk"] == pytest.approx(3.0)
    assert drafts[0].impact_paise == 3 * 2_000
    assert drafts[0].severity is Severity.MEDIUM  # 2 <= cover < 4
    assert drafts[0].suggested_tool == "draft_restock_order"
    assert drafts[0].suggested_params["items"][0]["qty"] == metrics["restock_qty"]


@pytest.mark.parametrize(
    ("stock", "expected"),
    [
        (1.0, Severity.CRITICAL),  # 0.5 days of cover
        (3.0, Severity.HIGH),  # 1.5 days
        (7.0, Severity.MEDIUM),  # 3.5 days
    ],
)
def test_stockout_severity_scales_with_urgency(stock: float, expected: Severity) -> None:
    session = make_session()
    merchant = make_merchant(session)
    rice = make_product(session, merchant, "RICE", stock=stock, cost=1_500, sell=2_000)
    filler = make_product(session, merchant, "SALT", stock=500.0, cost=100, sell=200)
    sell_every_day(session, merchant, rice, 2.0)
    sell_every_day(session, merchant, filler, 1.0)
    session.commit()

    draft = StockoutRiskEngine().run(ctx_for(session, merchant))[0]
    assert draft.severity is expected


def test_well_stocked_sku_raises_nothing() -> None:
    session = make_session()
    merchant = make_merchant(session)
    rice = make_product(session, merchant, "RICE", stock=200.0, cost=1_500, sell=2_000)
    sell_every_day(session, merchant, rice, 2.0)
    session.commit()
    assert StockoutRiskEngine().run(ctx_for(session, merchant)) == []


def test_a_sku_that_never_sells_is_not_a_stockout_risk() -> None:
    """Zero rate means infinite cover, not a divide-by-zero."""
    session = make_session()
    merchant = make_merchant(session)
    make_product(session, merchant, "DUST", stock=0.0, cost=1_500, sell=2_000)
    session.commit()
    assert StockoutRiskEngine().run(ctx_for(session, merchant)) == []


# ── dead stock ──────────────────────────────────────────────────────────────


def test_dead_stock_boundaries_on_both_idle_days_and_capital() -> None:
    """45 days idle and ₹500 locked are both inclusive; 44 days and ₹499 are both out."""
    session = make_session()
    merchant = make_merchant(session)

    # Exactly on both boundaries: 45 days since the last sale, 10 x ₹50 = ₹500 locked.
    on_boundary = make_product(session, merchant, "EXACT", stock=10.0, cost=5_000, sell=8_000)
    sell_units(session, merchant, on_boundary, TODAY - timedelta(days=DEAD_STOCK_DAYS), 1.0)

    # One day short of idle.
    fresher = make_product(session, merchant, "FRESHER", stock=10.0, cost=5_000, sell=8_000)
    sell_units(session, merchant, fresher, TODAY - timedelta(days=DEAD_STOCK_DAYS - 1), 1.0)

    # Idle for ages but one paise short of the capital floor.
    cheap = make_product(session, merchant, "CHEAP", stock=1.0, cost=49_999, sell=60_000)
    sell_units(session, merchant, cheap, TODAY - timedelta(days=90), 1.0)
    session.commit()

    draft = DeadStockEngine().run(ctx_for(session, merchant))[0]
    skus = {item["sku"] for item in draft.metrics["items"]}

    assert skus == {"EXACT"}
    assert draft.metrics["capital_locked_paise"] == DEAD_STOCK_MIN_CAPITAL_PAISE == 50_000
    assert draft.impact_paise == 50_000
    assert draft.metrics["items"][0]["days_idle"] == DEAD_STOCK_DAYS
    assert draft.suggested_tool == "save_merchant_note"
    assert 0 < draft.metrics["items"][0]["suggested_discount_pct"] <= 40


def test_dead_stock_aggregates_and_sums_locked_capital() -> None:
    session = make_session()
    merchant = make_merchant(session)
    for index, (stock, cost) in enumerate([(15.0, 20_000), (4.0, 30_000), (20.0, 7_000)]):
        product = make_product(
            session, merchant, f"OLD{index}", stock=stock, cost=cost, sell=cost * 2
        )
        sell_units(session, merchant, product, TODAY - timedelta(days=70 + index), 1.0)
    session.commit()

    draft = DeadStockEngine().run(ctx_for(session, merchant))[0]
    expected = 15 * 20_000 + 4 * 30_000 + 20 * 7_000  # ₹3,000 + ₹1,200 + ₹1,400 = ₹5,600
    assert draft.metrics["item_count"] == 3
    assert draft.metrics["capital_locked_paise"] == expected
    assert draft.impact_paise == expected
    assert draft.severity is Severity.HIGH  # >= ₹5,000 locked
    # Ranked by the capital each one ties up.
    assert [item["sku"] for item in draft.metrics["items"]] == ["OLD0", "OLD2", "OLD1"]


def test_a_never_sold_sku_is_aged_from_when_it_entered_the_catalogue() -> None:
    session = make_session()
    merchant = make_merchant(session)
    make_product(
        session, merchant, "NEWLINE", stock=20.0, cost=5_000, sell=8_000, created_days_ago=10
    )
    make_product(
        session, merchant, "OLDLINE", stock=20.0, cost=5_000, sell=8_000, created_days_ago=100
    )
    session.commit()

    draft = DeadStockEngine().run(ctx_for(session, merchant))[0]
    assert {item["sku"] for item in draft.metrics["items"]} == {"OLDLINE"}


# ── expiry risk ─────────────────────────────────────────────────────────────


def test_expiry_risk_values_the_units_that_cannot_sell_in_time() -> None:
    """20 litres, 4 days of shelf life left, selling 2/day: 12 litres will be binned."""
    session = make_session()
    merchant = make_merchant(session)
    milk = make_product(
        session,
        merchant,
        "MILK",
        stock=20.0,
        cost=3_000,
        sell=4_000,
        perishable=True,
        shelf_life_days=10,
        restocked_days_ago=6,
    )
    sell_every_day(session, merchant, milk, 2.0)
    session.commit()

    draft = ExpiryRiskEngine().run(ctx_for(session, merchant))[0]
    metrics = draft.metrics

    assert metrics["rate_units_per_day"] == pytest.approx(2.0)
    assert metrics["days_of_cover"] == pytest.approx(10.0)
    assert metrics["remaining_shelf_life_days"] == pytest.approx(4.0)
    assert metrics["units_at_risk"] == pytest.approx(12.0)  # 20 - 2 x 4
    assert metrics["loss_paise"] == 12 * 3_000  # valued at cost, not shelf price
    assert draft.impact_paise == 12 * 3_000
    assert draft.severity is Severity.MEDIUM  # remaining > 3 days


def test_expiry_boundary_cover_equal_to_shelf_life_is_safe() -> None:
    """Cover 10 days against 10 days of shelf life: it sells out exactly in time."""
    session = make_session()
    merchant = make_merchant(session)
    milk = make_product(
        session,
        merchant,
        "MILK",
        stock=20.0,
        cost=3_000,
        sell=4_000,
        perishable=True,
        shelf_life_days=10,
        restocked_days_ago=0,
    )
    sell_every_day(session, merchant, milk, 2.0)
    session.commit()
    assert ExpiryRiskEngine().run(ctx_for(session, merchant)) == []


def test_expiry_is_critical_when_it_goes_off_tomorrow() -> None:
    session = make_session()
    merchant = make_merchant(session)
    paneer = make_product(
        session,
        merchant,
        "PANEER",
        stock=20.0,
        cost=3_000,
        sell=4_000,
        perishable=True,
        shelf_life_days=8,
        restocked_days_ago=7,
    )
    sell_every_day(session, merchant, paneer, 2.0)
    session.commit()

    draft = ExpiryRiskEngine().run(ctx_for(session, merchant))[0]
    assert draft.metrics["remaining_shelf_life_days"] == pytest.approx(1.0)
    assert draft.severity is Severity.CRITICAL
    assert draft.metrics["units_at_risk"] == pytest.approx(18.0)


def test_a_thin_margin_perishable_is_never_told_to_discount_by_zero_percent() -> None:
    """Cost ₹19 against a ₹20 shelf price leaves no room above cost: advise a sale, not '0% off'."""
    session = make_session()
    merchant = make_merchant(session)
    dahi = make_product(
        session,
        merchant,
        "DAHI",
        stock=30.0,
        cost=1_900,
        sell=2_000,
        perishable=True,
        shelf_life_days=5,
        restocked_days_ago=4,
    )
    sell_every_day(session, merchant, dahi, 2.0)
    session.commit()

    draft = ExpiryRiskEngine().run(ctx_for(session, merchant))[0]
    assert draft.metrics["suggested_discount_pct"] == 0
    assert "0%" not in draft.body_en
    assert "0%" not in draft.body_hi
    assert "0%" not in draft.suggested_params["text_en"]
    assert "at cost" in draft.body_en


def test_dead_stock_with_no_markdown_room_suggests_a_combo() -> None:
    session = make_session()
    merchant = make_merchant(session)
    thin = make_product(session, merchant, "THIN", stock=30.0, cost=1_900, sell=2_000)
    sell_units(session, merchant, thin, TODAY - timedelta(days=90), 1.0)
    session.commit()

    draft = DeadStockEngine().run(ctx_for(session, merchant))[0]
    assert draft.suggested_params["suggested_discount_pct"] == 0
    assert "0%" not in draft.suggested_params["text_en"]
    assert "combo" in draft.suggested_params["text_en"]


def test_non_perishables_are_never_an_expiry_risk() -> None:
    session = make_session()
    merchant = make_merchant(session)
    soap = make_product(
        session, merchant, "SOAP", stock=200.0, cost=2_000, sell=3_000, restocked_days_ago=100
    )
    sell_every_day(session, merchant, soap, 1.0)
    session.commit()
    assert ExpiryRiskEngine().run(ctx_for(session, merchant)) == []


# ── empty database ──────────────────────────────────────────────────────────


def test_every_inventory_engine_is_silent_on_an_empty_database() -> None:
    session = make_session()
    merchant = make_merchant(session)
    session.commit()
    ctx = ctx_for(session, merchant)
    for engine in (StockoutRiskEngine(), DeadStockEngine(), ExpiryRiskEngine()):
        assert engine.run(ctx) == [], type(engine).__name__


def test_a_catalogue_with_no_sales_at_all_does_not_divide_by_zero() -> None:
    session = make_session()
    merchant = make_merchant(session)
    make_product(session, merchant, "A", stock=10.0, cost=100, sell=200, created_days_ago=3)
    session.commit()
    ctx = ctx_for(session, merchant)
    assert StockoutRiskEngine().run(ctx) == []
    assert ExpiryRiskEngine().run(ctx) == []
