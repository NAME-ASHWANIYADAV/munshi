"""Tests for the action execution layer — local and n8n.

Every test runs against a throwaway SQLite file under ``tmp_path``; the real
``data/munshiji.db`` is never opened. The n8n tests use ``httpx.MockTransport``, so the suite
is fully offline.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from munshiji.clock import ensure_aware, now_utc
from munshiji.config import Settings
from munshiji.db.base import Base
from munshiji.db.enums import ActionStatus, CustomerSegment, KhataStatus
from munshiji.db.models import (
    ActionOutcome,
    ActionRequest,
    Customer,
    KhataEntry,
    Merchant,
    Transaction,
)
from munshiji.errors import ProviderUnavailableError, ValidationError
from munshiji.integrations.n8n_client import (
    IDEMPOTENCY_HEADER,
    TOKEN_HEADER,
    WEBHOOK_WINBACK,
    N8nClient,
    parse_webhook_response,
)
from munshiji.providers.actions import ActionDispatch, ActionProvider, ActionResult
from munshiji.providers.actions_local import (
    METRIC_MESSAGES_FAILED,
    METRIC_MESSAGES_SENT,
    METRIC_REDEEMED,
    METRIC_REVENUE_RECOVERED,
    PAYMENT_LINK_BASE,
    LocalActions,
    is_valid_phone,
    normalise_phone,
)
from munshiji.providers.actions_n8n import N8nActions

MERCHANT_ID = "mer_test0001"

#: Fixed ids everywhere: the simulator's draws are SHA-256 of (action_id, customer_id), so
#: stable ids make the whole suite reproducible across runs and machines.
CUSTOMERS: tuple[tuple[str, CustomerSegment, str], ...] = (
    ("cus_champion", CustomerSegment.CHAMPION, "9810000001"),
    ("cus_loyal", CustomerSegment.LOYAL, "98100 00002"),
    ("cus_regular", CustomerSegment.REGULAR, "+91 9810000003"),
    ("cus_occasional", CustomerSegment.OCCASIONAL, "09810000004"),
    ("cus_atrisk", CustomerSegment.AT_RISK, "919810000005"),
    ("cus_dormant", CustomerSegment.DORMANT, "9810000006"),
    ("cus_nophone", CustomerSegment.REGULAR, ""),
    ("cus_badphone", CustomerSegment.LOYAL, "12345"),
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class World:
    """A tiny but real merchant world, seeded with known numbers."""

    factory: Callable[[], Session]
    merchant_id: str = MERCHANT_ID
    customer_ids: tuple[str, ...] = tuple(cid for cid, _, _ in CUSTOMERS)


@pytest.fixture()
def factory(tmp_path: Any) -> Iterator[Callable[[], Session]]:
    engine = create_engine(f"sqlite:///{(tmp_path / 'actions.db').as_posix()}", future=True)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    yield maker
    engine.dispose()


@pytest.fixture()
def world(factory: Callable[[], Session]) -> World:
    """Merchant, eight customers with distinct histories, and two khata entries."""
    session = factory()
    try:
        session.add(
            Merchant(
                id=MERCHANT_ID,
                owner_name="Rajesh Sharma",
                shop_name="Sharma General Store",
                locality="Lajpat Nagar",
                phone="9810099999",
                language="hi-IN",
            )
        )
        opened = now_utc() - timedelta(days=40)
        for index, (customer_id, segment, phone) in enumerate(CUSTOMERS):
            # Four transactions each, with amounts that do not divide evenly — the average
            # ticket has to be rounded, so the rounding rule is exercised for real.
            amounts = [25_000 + index * 7_300 + delta for delta in (0, 37, 101, 2)]
            session.add(
                Customer(
                    id=customer_id,
                    merchant_id=MERCHANT_ID,
                    name=customer_id.removeprefix("cus_").title(),
                    phone=phone,
                    segment=segment,
                    txn_count=len(amounts),
                    total_spend_paise=sum(amounts),
                    last_seen_at=now_utc() - timedelta(days=30 + index),
                    is_khata_customer=index < 2,
                )
            )
            for offset, amount in enumerate(amounts):
                session.add(
                    Transaction(
                        id=f"txn_{customer_id}_{offset}",
                        merchant_id=MERCHANT_ID,
                        customer_id=customer_id,
                        amount_paise=amount,
                        occurred_at=now_utc() - timedelta(days=20 + offset),
                    )
                )
            # A return must never pollute the average ticket.
            session.add(
                Transaction(
                    id=f"txn_{customer_id}_ret",
                    merchant_id=MERCHANT_ID,
                    customer_id=customer_id,
                    amount_paise=-5_000,
                    occurred_at=now_utc() - timedelta(days=19),
                    is_return=True,
                )
            )
        session.add(
            KhataEntry(
                id="kht_champion",
                merchant_id=MERCHANT_ID,
                customer_id="cus_champion",
                amount_paise=250_000,
                opened_at=opened,
                status=KhataStatus.OPEN,
            )
        )
        session.add(
            KhataEntry(
                id="kht_nophone",
                merchant_id=MERCHANT_ID,
                customer_id="cus_nophone",
                amount_paise=180_000,
                opened_at=opened,
                status=KhataStatus.OPEN,
            )
        )
        session.commit()
    finally:
        session.close()
    return World(factory=factory)


def make_action(
    world: World,
    *,
    action_id: str,
    tool_name: str,
    params: dict[str, Any] | None = None,
    status: ActionStatus = ActionStatus.APPROVED,
) -> str:
    session = world.factory()
    try:
        session.add(
            ActionRequest(
                id=action_id,
                merchant_id=world.merchant_id,
                tool_name=tool_name,
                params=params or {},
                status=status,
                summary_en="test action",
                summary_hi="परीक्षण",
            )
        )
        session.commit()
    finally:
        session.close()
    return action_id


def targets_for(*aliases: str, **overrides: Any) -> list[dict[str, Any]]:
    lookup = {cid: phone for cid, _, phone in CUSTOMERS}
    return [
        {
            "customer_id": alias,
            "name": alias.removeprefix("cus_").title(),
            "phone": lookup[alias],
            **overrides,
        }
        for alias in aliases
    ]


def outcomes_of(world: World, action_id: str) -> dict[str, ActionOutcome]:
    session = world.factory()
    try:
        rows = session.scalars(
            select(ActionOutcome).where(ActionOutcome.action_id == action_id)
        ).all()
        return {row.metric: row for row in rows}
    finally:
        session.close()


def outcome_count(world: World, action_id: str) -> int:
    session = world.factory()
    try:
        return int(
            session.scalar(
                select(func.count(ActionOutcome.id)).where(ActionOutcome.action_id == action_id)
            )
            or 0
        )
    finally:
        session.close()


def reload_action(world: World, action_id: str) -> ActionRequest:
    session = world.factory()
    try:
        return session.get(ActionRequest, action_id)  # type: ignore[return-value]
    finally:
        session.close()


# ─────────────────────────────────────────────────────────────────────────────
# Phone normalisation (what decides delivery in local mode)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9810000001", "+919810000001"),
        ("98100 00002", "+919810000002"),
        ("+91 98100-00003", "+919810000003"),
        ("09810000004", "+919810000004"),
        ("919810000005", "+919810000005"),
        ("+919810000006", "+919810000006"),
        ("", ""),
        ("12345", ""),
        ("1234567890", ""),  # Indian mobiles never start below 6
        ("01126543210", ""),  # landline
        ("not-a-number", ""),
    ],
)
def test_phone_normalisation(raw: str, expected: str) -> None:
    assert normalise_phone(raw) == expected
    assert is_valid_phone(raw) is bool(expected)


# ─────────────────────────────────────────────────────────────────────────────
# LocalActions — dispatch
# ─────────────────────────────────────────────────────────────────────────────


async def test_dispatch_writes_outcome_rows_and_real_counts(world: World) -> None:
    action_id = make_action(
        world,
        action_id="act_winback1",
        tool_name="send_winback_offer",
        params={"discount_label": "10% off", "valid_days": 7},
    )
    provider = LocalActions(world.factory)

    result = await provider.dispatch(
        ActionDispatch(
            action_id=action_id,
            merchant_id=world.merchant_id,
            tool_name="send_winback_offer",
            params={"discount_label": "10% off", "valid_days": 7},
            targets=targets_for("cus_champion", "cus_loyal", "cus_regular"),
            idempotency_key="key-winback-1",
        )
    )

    assert result.ok is True
    assert result.provider == "local"
    assert (result.delivered_count, result.failed_count) == (3, 0)

    outcomes = outcomes_of(world, action_id)
    assert set(outcomes) == {METRIC_MESSAGES_SENT, METRIC_MESSAGES_FAILED}
    assert outcomes[METRIC_MESSAGES_SENT].value_num == 3.0
    assert outcomes[METRIC_MESSAGES_FAILED].value_num == 0.0

    action = reload_action(world, action_id)
    assert action.status is ActionStatus.EXECUTED
    assert action.provider == "local"
    assert action.target_count == 3
    assert action.executed_at is not None
    assert action.result["target_customer_ids"] == [
        "cus_champion",
        "cus_loyal",
        "cus_regular",
    ]
    # The text really was rendered per recipient, not broadcast.
    rendered = [entry["message"] for entry in result.detail["targets"]]
    assert len(set(rendered)) == 3
    assert "Champion" in rendered[0]
    assert "Sharma General Store" in rendered[0]


async def test_dispatch_is_idempotent_on_the_key(world: World) -> None:
    action_id = make_action(world, action_id="act_winback2", tool_name="send_winback_offer")
    provider = LocalActions(world.factory)
    dispatch = ActionDispatch(
        action_id=action_id,
        merchant_id=world.merchant_id,
        tool_name="send_winback_offer",
        params={"discount_label": "10% off"},
        targets=targets_for("cus_champion", "cus_loyal"),
        idempotency_key="key-replay",
    )

    first = await provider.dispatch(dispatch)
    after_first = outcome_count(world, action_id)
    second = await provider.dispatch(dispatch)

    assert after_first == 2
    assert outcome_count(world, action_id) == 2, "a replay must not double-write outcomes"
    assert second.as_dict() == first.as_dict()


async def test_a_different_key_is_a_new_dispatch(world: World) -> None:
    action_id = make_action(world, action_id="act_winback3", tool_name="send_winback_offer")
    provider = LocalActions(world.factory)

    def dispatch(key: str) -> ActionDispatch:
        return ActionDispatch(
            action_id=action_id,
            merchant_id=world.merchant_id,
            tool_name="send_winback_offer",
            targets=targets_for("cus_champion"),
            idempotency_key=key,
        )

    await provider.dispatch(dispatch("first"))
    await provider.dispatch(dispatch("second"))
    assert outcome_count(world, action_id) == 4


async def test_invalid_phone_is_counted_as_failed_not_raised(world: World) -> None:
    action_id = make_action(world, action_id="act_winback4", tool_name="send_winback_offer")
    provider = LocalActions(world.factory)

    result = await provider.dispatch(
        ActionDispatch(
            action_id=action_id,
            merchant_id=world.merchant_id,
            tool_name="send_winback_offer",
            targets=targets_for("cus_champion", "cus_nophone", "cus_badphone"),
            idempotency_key="key-mixed",
        )
    )

    assert result.ok is True, "a partial failure is still a successful dispatch"
    assert (result.delivered_count, result.failed_count) == (1, 2)
    assert outcomes_of(world, action_id)[METRIC_MESSAGES_FAILED].value_num == 2.0
    statuses = [entry["status"] for entry in result.detail["targets"]]
    assert statuses == ["delivered", "failed", "failed"]
    assert result.detail["targets"][1]["reason"] == "missing or invalid phone number"


async def test_every_target_undeliverable_marks_the_action_failed(world: World) -> None:
    action_id = make_action(world, action_id="act_winback5", tool_name="send_winback_offer")
    provider = LocalActions(world.factory)

    result = await provider.dispatch(
        ActionDispatch(
            action_id=action_id,
            merchant_id=world.merchant_id,
            tool_name="send_winback_offer",
            targets=targets_for("cus_nophone", "cus_badphone"),
            idempotency_key="key-allbad",
        )
    )

    assert result.ok is False
    assert reload_action(world, action_id).status is ActionStatus.FAILED


async def test_dispatch_is_deterministic_across_identical_runs(world: World) -> None:
    """Same key, same everything — the demo replays byte-identically."""
    provider = LocalActions(world.factory)
    refs = []
    for action_id in ("act_det_a", "act_det_b"):
        make_action(world, action_id=action_id, tool_name="send_winback_offer")
        result = await provider.dispatch(
            ActionDispatch(
                action_id=action_id,
                merchant_id=world.merchant_id,
                tool_name="send_winback_offer",
                targets=targets_for("cus_champion"),
                idempotency_key="same-key-both-times",
            )
        )
        refs.append(result.detail["targets"][0]["ref"])
    assert refs[0] == refs[1] != ""


async def test_pending_approval_actions_are_never_laundered_into_executed(world: World) -> None:
    action_id = make_action(
        world,
        action_id="act_pending",
        tool_name="send_winback_offer",
        status=ActionStatus.PENDING_APPROVAL,
    )
    await LocalActions(world.factory).dispatch(
        ActionDispatch(
            action_id=action_id,
            merchant_id=world.merchant_id,
            tool_name="send_winback_offer",
            targets=targets_for("cus_champion"),
            idempotency_key="key-pending",
        )
    )
    assert reload_action(world, action_id).status is ActionStatus.PENDING_APPROVAL


# ── Udhaar reminders ────────────────────────────────────────────────────────


async def test_udhaar_reminder_bumps_reminders_sent_and_last_reminder_at(world: World) -> None:
    action_id = make_action(
        world,
        action_id="act_reminder1",
        tool_name="send_udhaar_reminder",
        params={"tone": "gentle"},
    )
    provider = LocalActions(world.factory)

    before = now_utc()
    result = await provider.dispatch(
        ActionDispatch(
            action_id=action_id,
            merchant_id=world.merchant_id,
            tool_name="send_udhaar_reminder",
            params={"tone": "gentle"},
            targets=[
                {
                    "customer_id": "cus_champion",
                    "name": "Ramesh Gupta",
                    "phone": "9810000001",
                    "amount_paise": 250_000,
                    "days_overdue": 40,
                    "khata_entry_id": "kht_champion",
                },
                {
                    "customer_id": "cus_nophone",
                    "name": "Suresh",
                    "phone": "",
                    "amount_paise": 180_000,
                    "days_overdue": 40,
                    "khata_entry_id": "kht_nophone",
                },
            ],
            idempotency_key="key-reminder-1",
        )
    )

    assert (result.delivered_count, result.failed_count) == (1, 1)
    session = world.factory()
    try:
        delivered = session.get(KhataEntry, "kht_champion")
        skipped = session.get(KhataEntry, "kht_nophone")
        assert delivered.reminders_sent == 1
        assert delivered.last_reminder_at is not None
        # SQLite hands timestamps back naive; they were stored as UTC.
        assert ensure_aware(delivered.last_reminder_at) >= before
        # A message that never left must not consume the 7-day cooldown.
        assert skipped.reminders_sent == 0
        assert skipped.last_reminder_at is None
    finally:
        session.close()
    stored = reload_action(world, action_id).result
    assert stored["khata_entry_ids"] == ["kht_champion"]
    assert stored["detail"]["khata_entries_updated"] == 1


async def test_udhaar_reminder_bumps_open_entries_when_no_entry_id_is_given(world: World) -> None:
    action_id = make_action(world, action_id="act_reminder2", tool_name="send_udhaar_reminder")
    await LocalActions(world.factory).dispatch(
        ActionDispatch(
            action_id=action_id,
            merchant_id=world.merchant_id,
            tool_name="send_udhaar_reminder",
            targets=targets_for("cus_champion", amount_paise=250_000, days_overdue=50),
            idempotency_key="key-reminder-2",
        )
    )
    session = world.factory()
    try:
        assert session.get(KhataEntry, "kht_champion").reminders_sent == 1
    finally:
        session.close()


async def test_reminder_replay_does_not_double_bump_the_khata(world: World) -> None:
    action_id = make_action(world, action_id="act_reminder3", tool_name="send_udhaar_reminder")
    provider = LocalActions(world.factory)
    dispatch = ActionDispatch(
        action_id=action_id,
        merchant_id=world.merchant_id,
        tool_name="send_udhaar_reminder",
        targets=targets_for("cus_champion", amount_paise=250_000, days_overdue=50),
        idempotency_key="key-reminder-3",
    )
    await provider.dispatch(dispatch)
    await provider.dispatch(dispatch)
    session = world.factory()
    try:
        assert session.get(KhataEntry, "kht_champion").reminders_sent == 1
    finally:
        session.close()


# ── Payment links ───────────────────────────────────────────────────────────


async def test_payment_link_is_deterministic_and_declared_as_simulated(world: World) -> None:
    action_id = make_action(world, action_id="act_link1", tool_name="create_payment_link")
    provider = LocalActions(world.factory)

    result = await provider.dispatch(
        ActionDispatch(
            action_id=action_id,
            merchant_id=world.merchant_id,
            tool_name="create_payment_link",
            params={"amount_paise": 125_000},
            targets=targets_for("cus_champion", amount_paise=125_000),
            idempotency_key="key-link-1",
        )
    )

    link = result.detail["payment_links"]["cus_champion"]
    assert link.startswith(PAYMENT_LINK_BASE)
    assert "simulated" in result.message.lower()
    assert "not a live" in result.message.lower()
    assert link in result.detail["targets"][0]["message"]
    assert "payment_link_created" in outcomes_of(world, action_id)

    # Same key on a fresh action mints the same link.
    other = make_action(world, action_id="act_link2", tool_name="create_payment_link")
    again = await provider.dispatch(
        ActionDispatch(
            action_id=other,
            merchant_id=world.merchant_id,
            tool_name="create_payment_link",
            params={"amount_paise": 125_000},
            targets=targets_for("cus_champion", amount_paise=125_000),
            idempotency_key="key-link-1",
        )
    )
    assert again.detail["payment_links"]["cus_champion"] == link


async def test_restock_and_followup_resolve_their_own_recipients(world: World) -> None:
    provider = LocalActions(world.factory)

    restock_id = make_action(world, action_id="act_restock", tool_name="draft_restock_order")
    restock = await provider.dispatch(
        ActionDispatch(
            action_id=restock_id,
            merchant_id=world.merchant_id,
            tool_name="draft_restock_order",
            params={
                "supplier_name": "Gupta Traders",
                "supplier_phone": "9811122233",
                "items": [{"name": "Toor Dal", "qty": 12, "unit": "kg"}],
            },
            idempotency_key="key-restock",
        )
    )
    assert restock.delivered_count == 1
    assert "Toor Dal" in restock.detail["targets"][0]["message_en"]
    assert "restock_items" in outcomes_of(world, restock_id)

    followup_id = make_action(world, action_id="act_followup", tool_name="schedule_followup")
    followup = await provider.dispatch(
        ActionDispatch(
            action_id=followup_id,
            merchant_id=world.merchant_id,
            tool_name="schedule_followup",
            params={
                "text": "Gupta Traders ko rate confirm karna hai",
                "when_display": "kal 6 baje",
            },
            idempotency_key="key-followup",
        )
    )
    # The follow-up comes back to the merchant, not to a customer.
    assert followup.detail["targets"][0]["name"] == "Rajesh Sharma"
    assert "rate confirm" in followup.detail["targets"][0]["message"]
    assert "followup_scheduled" in outcomes_of(world, followup_id)


# ─────────────────────────────────────────────────────────────────────────────
# The outcome simulator
# ─────────────────────────────────────────────────────────────────────────────


async def dispatch_winback(world: World, action_id: str) -> ActionResult:
    make_action(
        world,
        action_id=action_id,
        tool_name="send_winback_offer",
        params={"discount_label": "10% off", "valid_days": 7},
    )
    return await LocalActions(world.factory).dispatch(
        ActionDispatch(
            action_id=action_id,
            merchant_id=world.merchant_id,
            tool_name="send_winback_offer",
            params={"discount_label": "10% off", "valid_days": 7},
            targets=targets_for(*world.customer_ids),
            idempotency_key=f"key-{action_id}",
        )
    )


def expected_revenue(world: World, customer_ids: list[str]) -> int:
    """Recompute recovered revenue independently, straight from the transaction rows."""
    session = world.factory()
    try:
        total = 0
        for customer_id in customer_ids:
            amounts = list(
                session.scalars(
                    select(Transaction.amount_paise).where(
                        Transaction.customer_id == customer_id,
                        Transaction.is_return.is_(False),
                    )
                ).all()
            )
            assert amounts, f"{customer_id} has no history to average"
            # round-half-up of sum/count, in integer arithmetic
            total += (2 * sum(amounts) + len(amounts)) // (2 * len(amounts))
        return total
    finally:
        session.close()


async def test_simulate_outcomes_derives_revenue_from_real_customer_history(world: World) -> None:
    action_id = "act_sim_revenue"
    await dispatch_winback(world, action_id)
    provider = LocalActions(world.factory)

    outcomes = await provider.simulate_outcomes(action_id, days_elapsed=30)
    metrics = {outcome.metric: outcome for outcome in outcomes}
    assert set(metrics) == {METRIC_REDEEMED, METRIC_REVENUE_RECOVERED, "redemption_rate"}

    action = reload_action(world, action_id)
    redeemers = action.result["redeemed_customer_ids"]
    assert redeemers, "the priors should produce at least one redemption over 30 days"
    assert set(redeemers) <= set(world.customer_ids)

    assert metrics[METRIC_REDEEMED].value_num == float(len(redeemers))
    assert metrics[METRIC_REVENUE_RECOVERED].value_paise == expected_revenue(world, redeemers)
    assert metrics[METRIC_REVENUE_RECOVERED].value_paise > 0
    assert metrics["redemption_rate"].value_num == pytest.approx(
        len(redeemers) / len(world.customer_ids), abs=1e-4
    )


async def test_simulate_outcomes_is_deterministic_and_replaces_rather_than_appends(
    world: World,
) -> None:
    action_id = "act_sim_determinism"
    await dispatch_winback(world, action_id)
    provider = LocalActions(world.factory)

    first = await provider.simulate_outcomes(action_id, days_elapsed=7)
    first_ids = list(reload_action(world, action_id).result["redeemed_customer_ids"])
    second = await provider.simulate_outcomes(action_id, days_elapsed=7)
    second_ids = list(reload_action(world, action_id).result["redeemed_customer_ids"])

    assert first_ids == second_ids
    assert [o.value_num for o in first] == [o.value_num for o in second]
    assert [o.value_paise for o in first] == [o.value_paise for o in second]
    # 2 delivery metrics + 3 simulated ones, not 2 + 6.
    assert outcome_count(world, action_id) == 5


async def test_redemptions_accumulate_as_days_pass(world: World) -> None:
    action_id = "act_sim_ramp"
    await dispatch_winback(world, action_id)
    provider = LocalActions(world.factory)

    await provider.simulate_outcomes(action_id, days_elapsed=1)
    day_one = set(reload_action(world, action_id).result["redeemed_customer_ids"])
    await provider.simulate_outcomes(action_id, days_elapsed=30)
    day_thirty = set(reload_action(world, action_id).result["redeemed_customer_ids"])

    assert day_one <= day_thirty, "a customer who redeemed on day 1 cannot un-redeem"
    assert len(day_thirty) >= len(day_one)


async def test_simulate_outcomes_ignores_tools_it_cannot_model(world: World) -> None:
    action_id = make_action(world, action_id="act_sim_other", tool_name="send_udhaar_reminder")
    assert await LocalActions(world.factory).simulate_outcomes(action_id) == []


# ─────────────────────────────────────────────────────────────────────────────
# n8n client — tolerant parsing, no network
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("payload", "expected", "want"),
    [
        ({"ok": True, "delivered": 3, "failed": 1, "id": "b-1"}, 4, (True, 3, 1, "b-1")),
        ({"success": "true", "sent": 2, "errors": 0}, 2, (True, 2, 0, "")),
        ([{"json": {"status": "success", "messages_sent": 5}}], 5, (True, 5, 0, "")),
        ({"data": {"ok": True, "delivered_count": 2, "executionId": 77}}, 2, (True, 2, 0, "77")),
        ({"message": "Workflow was started"}, 6, (True, 6, 0, "")),
        (None, 3, (True, 3, 0, "")),
        ({"ok": False, "error": "no whatsapp credentials"}, 3, (False, 0, 3, "")),
        ({"delivered": 0, "failed": 2}, 2, (False, 0, 2, "")),
        ({"success": True, "errors": ["bad number"]}, 4, (True, 3, 1, "")),
    ],
)
def test_tolerant_response_parsing(payload: Any, expected: int, want: tuple) -> None:
    result = parse_webhook_response(payload, expected=expected)
    assert (result.ok, result.delivered, result.failed, result.external_id) == want


def n8n_settings() -> Settings:
    """n8n config pinned for the test, independent of ambient environment variables.

    ``model_copy`` rather than ``Settings(...)`` on purpose: this project's fields carry
    ``validation_alias``, so a ``N8N_*`` environment variable (the shared conftest blanks the
    token) would otherwise win over an init keyword.
    """
    return Settings().model_copy(
        update={
            "n8n_base_url": "http://n8n.test",
            "n8n_webhook_prefix": "/webhook",
            "n8n_webhook_token": "test-token",
            "http_timeout_seconds": 1.0,
        }
    )


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> N8nClient:
    return N8nClient(
        n8n_settings(), transport=httpx.MockTransport(handler), retry_delay_seconds=0.0
    )


async def test_client_builds_the_url_and_sends_both_headers() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get(TOKEN_HEADER)
        seen["key"] = request.headers.get(IDEMPOTENCY_HEADER)
        return httpx.Response(200, json={"ok": True, "delivered": 1})

    client = mock_client(handler)
    await client.post(WEBHOOK_WINBACK, {"hello": "ji"}, idempotency_key="idem-9", expected=1)
    await client.aclose()

    assert seen["url"] == "http://n8n.test/webhook/munshiji-winback"
    assert seen["token"] == "test-token"
    assert seen["key"] == "idem-9"


async def test_client_retries_once_on_5xx_then_gives_up() -> None:
    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(ProviderUnavailableError):
        await mock_client(handler).post(WEBHOOK_WINBACK, {}, idempotency_key="k")
    assert attempts["n"] == 2


async def test_client_retries_once_on_timeout_then_gives_up() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectTimeout("n8n did not answer", request=request)

    with pytest.raises(ProviderUnavailableError):
        await mock_client(handler).post(WEBHOOK_WINBACK, {}, idempotency_key="k")
    assert attempts["n"] == 2


async def test_client_recovers_when_the_retry_succeeds() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"ok": True, "delivered": 2})

    result = await mock_client(handler).post(WEBHOOK_WINBACK, {}, idempotency_key="k", expected=2)
    assert (attempts["n"], result.ok, result.delivered) == (2, True, 2)


async def test_client_does_not_retry_a_4xx() -> None:
    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(404, text="workflow not registered")

    with pytest.raises(ProviderUnavailableError):
        await mock_client(handler).post(WEBHOOK_WINBACK, {}, idempotency_key="k")
    assert attempts["n"] == 1


async def test_ping_never_raises() -> None:
    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nothing listening", request=request)

    assert await mock_client(dead).ping() is False
    assert await mock_client(lambda _r: httpx.Response(200, text="ok")).ping() is True


# ─────────────────────────────────────────────────────────────────────────────
# N8nActions
# ─────────────────────────────────────────────────────────────────────────────


async def test_n8n_dispatch_parses_a_well_behaved_response(world: World) -> None:
    action_id = make_action(world, action_id="act_n8n1", tool_name="send_winback_offer")
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = __import__("json").loads(request.content)
        return httpx.Response(200, json={"ok": True, "delivered": 3, "failed": 0, "id": "wa-77"})

    provider = N8nActions(mock_client(handler), world.factory)
    result = await provider.dispatch(
        ActionDispatch(
            action_id=action_id,
            merchant_id=world.merchant_id,
            tool_name="send_winback_offer",
            params={"discount_label": "10% off"},
            targets=targets_for("cus_champion", "cus_loyal", "cus_regular"),
            idempotency_key="key-n8n-1",
        )
    )

    assert (result.ok, result.provider, result.external_id) == (True, "live", "wa-77")
    assert (result.delivered_count, result.failed_count) == (3, 0)

    # Same rows as the local provider would have written.
    outcomes = outcomes_of(world, action_id)
    assert outcomes[METRIC_MESSAGES_SENT].value_num == 3.0
    assert outcomes[METRIC_MESSAGES_FAILED].value_num == 0.0
    action = reload_action(world, action_id)
    assert action.status is ActionStatus.EXECUTED
    assert action.provider == "live"

    # The payload is self-contained: phones and rendered text travel with it.
    body = captured["body"]
    assert body["tool"] == "send_winback_offer"
    assert body["merchant"]["shop_name"] == "Sharma General Store"
    assert body["target_count"] == 3
    assert body["targets"][0]["phone"] == "+919810000001"
    assert "Sharma General Store" in body["targets"][0]["message"]


async def test_n8n_dispatch_tolerates_a_variant_response_shape(world: World) -> None:
    action_id = make_action(world, action_id="act_n8n2", tool_name="send_udhaar_reminder")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "data": {
                        "status": "success",
                        "messages_sent": 1,
                        "messages_failed": 1,
                        "executionId": "exec-42",
                    }
                }
            ],
        )

    provider = N8nActions(mock_client(handler), world.factory)
    result = await provider.dispatch(
        ActionDispatch(
            action_id=action_id,
            merchant_id=world.merchant_id,
            tool_name="send_udhaar_reminder",
            targets=targets_for("cus_champion", "cus_loyal", amount_paise=250_000)
            + targets_for("cus_nophone"),
            idempotency_key="key-n8n-2",
        )
    )

    assert result.ok is True
    assert result.external_id == "exec-42"
    # 1 failure reported by n8n + 1 recipient we never even attempted.
    assert (result.delivered_count, result.failed_count) == (1, 2)
    # n8n reported totals only, so the first delivered target is the one credited.
    assert reload_action(world, action_id).result["khata_entry_ids"] == ["kht_champion"]


async def test_n8n_dispatch_is_idempotent(world: World) -> None:
    action_id = make_action(world, action_id="act_n8n3", tool_name="send_winback_offer")
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True, "delivered": 1})

    provider = N8nActions(mock_client(handler), world.factory)
    dispatch = ActionDispatch(
        action_id=action_id,
        merchant_id=world.merchant_id,
        tool_name="send_winback_offer",
        targets=targets_for("cus_champion"),
        idempotency_key="key-n8n-replay",
    )
    first = await provider.dispatch(dispatch)
    second = await provider.dispatch(dispatch)

    assert calls["n"] == 1, "a replay must not hit the webhook again"
    assert second.as_dict() == first.as_dict()
    assert outcome_count(world, action_id) == 2


@pytest.mark.parametrize("failure", ["500", "timeout"])
async def test_n8n_failure_propagates_and_writes_nothing(world: World, failure: str) -> None:
    action_id = make_action(
        world, action_id=f"act_n8n_fail_{failure}", tool_name="send_winback_offer"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("n8n never answered", request=request)
        return httpx.Response(500, text="workflow error")

    provider = N8nActions(mock_client(handler), world.factory)
    with pytest.raises(ProviderUnavailableError):
        await provider.dispatch(
            ActionDispatch(
                action_id=action_id,
                merchant_id=world.merchant_id,
                tool_name="send_winback_offer",
                targets=targets_for("cus_champion", "cus_loyal"),
                idempotency_key="key-n8n-fail",
            )
        )

    # Nothing half-written: no outcomes, no state change, no stamped result.
    assert outcome_count(world, action_id) == 0
    action = reload_action(world, action_id)
    assert action.status is ActionStatus.APPROVED
    assert action.executed_at is None
    assert action.result == {}


async def test_n8n_refuses_a_tool_with_no_webhook() -> None:
    with pytest.raises(ValidationError):
        N8nActions.webhook_for("save_merchant_note")


async def test_n8n_uses_per_recipient_results_when_the_workflow_supplies_them(
    world: World,
) -> None:
    action_id = make_action(world, action_id="act_n8n4", tool_name="send_udhaar_reminder")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "delivered": 1,
                "failed": 1,
                "results": [
                    {"customer_id": "cus_loyal", "ok": False},
                    {"customer_id": "cus_champion", "ok": True},
                ],
            },
        )

    provider = N8nActions(mock_client(handler), world.factory)
    await provider.dispatch(
        ActionDispatch(
            action_id=action_id,
            merchant_id=world.merchant_id,
            tool_name="send_udhaar_reminder",
            targets=targets_for("cus_loyal", "cus_champion", amount_paise=250_000),
            idempotency_key="key-n8n-4",
        )
    )
    session = world.factory()
    try:
        assert session.get(KhataEntry, "kht_champion").reminders_sent == 1
    finally:
        session.close()


# ─────────────────────────────────────────────────────────────────────────────
# Protocol conformance and health
# ─────────────────────────────────────────────────────────────────────────────


def test_both_providers_satisfy_the_action_provider_protocol() -> None:
    assert isinstance(LocalActions(), ActionProvider)
    assert isinstance(N8nActions(), ActionProvider)


async def test_local_health_reports_the_database(world: World) -> None:
    health = await LocalActions(world.factory).health()
    assert (health.ok, health.kind, health.mode) == (True, "actions", "local")


async def test_n8n_health_reflects_reachability() -> None:
    up = await N8nActions(mock_client(lambda _r: httpx.Response(200))).health()
    assert (up.ok, up.kind, up.mode) == (True, "actions", "live")

    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nothing listening", request=request)

    assert (await N8nActions(mock_client(dead)).health()).ok is False
