"""Outbound compliance rules.

These tests exist to stop the guardrails quietly becoming decoration. Each one pins a refusal
that must keep happening: a send outside collection hours, a promotional message to someone who
never consented, a message to someone who asked to be left alone, and a tool trying to send under
a category it is not entitled to.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from munshiji.clock import IST
from munshiji.compliance import (
    CONSENT_TAG,
    OPT_OUT_TAG,
    QUIET_HOURS_TOOLS,
    check_send_window,
    consented_ids,
    screen_recipients,
)
from munshiji.db.models import Customer, Merchant
from munshiji.economics import TemplateCategory


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 19, hour, minute, tzinfo=IST)


def _customer(session: Session, merchant: Merchant, name: str, **tags: bool) -> Customer:
    customer = Customer(merchant_id=merchant.id, name=name, phone="9800000000", tags=dict(tags))
    session.add(customer)
    session.flush()
    return customer


# ── the contact window ───────────────────────────────────────────────────────


@pytest.mark.parametrize("hour", [0, 5, 7, 19, 21, 23])
def test_reminders_are_refused_outside_collection_hours(hour: int) -> None:
    blocked = check_send_window("send_udhaar_reminder", at=_at(hour))
    assert blocked is not None, f"{hour}:00 should be outside the window"
    reason_en, reason_hi = blocked
    assert reason_en and reason_hi


@pytest.mark.parametrize("hour", [8, 11, 14, 18])
def test_reminders_go_out_during_the_day(hour: int) -> None:
    assert check_send_window("send_udhaar_reminder", at=_at(hour)) is None


def test_the_window_binds_collection_contact_only() -> None:
    """A win-back offer is shop marketing, not debt collection.

    Applying the recovery-hours rule to everything would look thorough and be wrong — offers are
    governed by consent instead. If that ever changes, this test should be the thing that argues.
    """
    assert set(QUIET_HOURS_TOOLS) == {"send_udhaar_reminder"}
    assert check_send_window("send_winback_offer", at=_at(22)) is None


# ── consent ──────────────────────────────────────────────────────────────────


def test_marketing_needs_consent_that_was_actually_given(
    session: Session, merchant: Merchant
) -> None:
    consented = _customer(session, merchant, "Anjali", **{CONSENT_TAG: True})
    never_asked = _customer(session, merchant, "Ramesh")

    decision = screen_recipients("send_winback_offer", [consented, never_asked], at=_at(11))

    assert decision.allowed_ids == [consented.id]
    assert [refusal.rule for refusal in decision.refused] == ["no_marketing_consent"]
    assert "Ramesh" in decision.refused[0].reason_en


def test_a_reminder_about_your_own_balance_does_not_need_opt_in(
    session: Session, merchant: Merchant
) -> None:
    """Different footing: it concerns a transaction the customer entered into themselves."""
    never_asked = _customer(session, merchant, "Ramesh")

    decision = screen_recipients("send_udhaar_reminder", [never_asked], at=_at(11))

    assert decision.allowed_ids == [never_asked.id]
    assert not decision.refused


def test_opting_out_stops_everything(session: Session, merchant: Merchant) -> None:
    gone = _customer(session, merchant, "Usha", **{CONSENT_TAG: True, OPT_OUT_TAG: True})

    for tool in ("send_winback_offer", "send_udhaar_reminder"):
        decision = screen_recipients(tool, [gone], at=_at(11))
        assert not decision.allowed, f"{tool} contacted someone who opted out"
        assert decision.refused[0].rule == "opted_out"


def test_consented_ids_excludes_opt_outs(session: Session, merchant: Merchant) -> None:
    yes = _customer(session, merchant, "Anjali", **{CONSENT_TAG: True})
    _customer(session, merchant, "Usha", **{CONSENT_TAG: True, OPT_OUT_TAG: True})
    _customer(session, merchant, "Ramesh")

    assert consented_ids(session.query(Customer).all()) == [yes.id]


# ── template category ────────────────────────────────────────────────────────


def test_a_tool_cannot_send_under_a_cheaper_category(session: Session) -> None:
    """Billing a promotional message as utility is a policy breach, not an optimisation."""
    blocked = check_send_window("send_winback_offer", category=TemplateCategory.UTILITY, at=_at(11))
    assert blocked is not None
    assert "marketing" in blocked[0]


def test_declaring_the_right_category_is_allowed() -> None:
    assert (
        check_send_window("send_winback_offer", category=TemplateCategory.MARKETING, at=_at(11))
        is None
    )


# ── what the merchant hears ──────────────────────────────────────────────────


def test_a_block_explains_itself_in_both_languages(session: Session, merchant: Merchant) -> None:
    someone = _customer(session, merchant, "Ramesh")
    decision = screen_recipients("send_udhaar_reminder", [someone], at=_at(23))

    assert decision.is_blocked
    assert decision.sentence(hindi=True)
    assert decision.sentence(hindi=False)
    assert decision.sentence(hindi=True) != decision.sentence(hindi=False)


def test_refusals_are_counted_for_the_merchant(session: Session, merchant: Merchant) -> None:
    _customer(session, merchant, "A")
    _customer(session, merchant, "B")
    allowed = _customer(session, merchant, "C", **{CONSENT_TAG: True})

    decision = screen_recipients("send_winback_offer", session.query(Customer).all(), at=_at(11))

    assert decision.allowed_ids == [allowed.id]
    assert "2" in decision.sentence(hindi=True)
    payload = decision.as_dict()
    assert payload["refused_count"] == 2
    assert payload["allowed_count"] == 1
    assert "send_window" in payload["rules_applied"]


def test_a_clean_list_says_nothing(session: Session, merchant: Merchant) -> None:
    """No refusals means no noise — the merchant only hears about what was dropped."""
    _customer(session, merchant, "Anjali", **{CONSENT_TAG: True})
    decision = screen_recipients("send_winback_offer", session.query(Customer).all(), at=_at(11))
    assert decision.sentence(hindi=True) == ""


# ── the seeded shop ──────────────────────────────────────────────────────────


def test_the_seeded_shop_has_people_who_cannot_be_marketed_to(session: Session) -> None:
    """Seeding everyone as consented would hide the rule, because nothing would ever refuse."""
    from munshiji.seed.generator import generate

    generate(session, days=60)
    session.commit()

    customers = session.query(Customer).all()
    consented = set(consented_ids(customers))

    assert consented, "nobody can be marketed to at all"
    assert len(consented) < len(customers), "every seeded customer consented — the rule is untested"
    assert [c for c in customers if (c.tags or {}).get(OPT_OUT_TAG)], "nobody opted out"


def test_seeded_consent_is_deterministic(session: Session) -> None:
    """The same demo must drop the same people every time it is replayed."""
    from munshiji.seed.generator import _consent_tags

    for customer_id in ("cus_01ABCDEF", "cus_01ZZZZZZ", "cus_01Q7W8E9"):
        assert _consent_tags(customer_id) == _consent_tags(customer_id)

    # And the rule must actually vary — a constant would make the screen meaningless.
    outcomes = {tuple(sorted(_consent_tags(f"cus_{index:08d}").items())) for index in range(200)}
    assert len(outcomes) > 1, "consent is constant across customers"


def test_clock_helper_covers_the_boundary() -> None:
    """08:00 is inside the window; 19:00 is not."""
    assert check_send_window("send_udhaar_reminder", at=_at(8, 0)) is None
    assert check_send_window("send_udhaar_reminder", at=_at(18, 59)) is None
    assert check_send_window("send_udhaar_reminder", at=_at(19, 0)) is not None
    assert check_send_window("send_udhaar_reminder", at=_at(7, 59)) is not None


def test_window_label_mentions_a_real_span() -> None:
    blocked = check_send_window("send_udhaar_reminder", at=_at(2))
    assert blocked is not None
    assert "8" in blocked[0] and "7" in blocked[0]


def test_empty_recipient_list_is_not_an_error() -> None:
    decision = screen_recipients("send_winback_offer", [], at=_at(11))
    assert decision.allowed == []
    assert decision.refused == []
    assert not decision.is_blocked


def test_the_window_is_ist_not_server_local() -> None:
    """A UTC host must not get a window shifted by five and a half hours.

    17:30 UTC is 23:00 in Delhi. Read naively as server-local time it looks like a perfectly
    reasonable hour to chase someone for money, which is exactly the mistake worth pinning.
    """
    utc_evening = datetime(2026, 9, 19, 17, 30, tzinfo=UTC)
    assert utc_evening.astimezone(IST).hour == 23
    assert check_send_window("send_udhaar_reminder", at=utc_evening) is not None

    utc_morning = datetime(2026, 9, 19, 6, 0, tzinfo=UTC)  # 11:30 IST
    assert check_send_window("send_udhaar_reminder", at=utc_morning) is None
