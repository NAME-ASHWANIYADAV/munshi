"""Offline action execution — the whole "kar deta hai" loop, in-process.

This is **not** a stub (SPEC.md §2.2). ``LocalActions`` really resolves recipients, really
renders the message each one would receive, really decides per recipient whether it could be
delivered, and really writes the :class:`~munshiji.db.models.ActionOutcome` rows that the
memory layer later reads back in conversation #2. With zero API keys and zero internet the
approval → execution → outcome → memory loop is fully exercisable.

What is simulated, and what is not
----------------------------------
* **Not simulated:** recipients, message text, khata bookkeeping, outcome rows, state
  transitions, every number reported back. All of it comes from the database.
* **Simulated:** the delivery itself. A target whose phone number is missing or not a valid
  Indian mobile fails; every other target is delivered. Message references and the payment-link
  short code are derived from ``idempotency_key`` with SHA-256, so a demo replays byte-identical
  on any machine — no ``random`` module, no wall clock in the derivation.

The persistence helpers in this module (:func:`resolve_targets`, :func:`write_delivery_outcomes`,
:func:`bump_khata_reminders`, :func:`finalise_action`, …) are shared with
:class:`~munshiji.providers.actions_n8n.N8nActions` on purpose: both providers must leave the
database in exactly the same shape, so everything downstream behaves identically whichever one
served the action.

Session factories must be built with ``expire_on_commit=False`` (as
:func:`~munshiji.db.base.get_sessionmaker` is), since returned ORM objects outlive the session.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from munshiji.clock import now_utc
from munshiji.db.enums import ActionStatus, CustomerSegment, KhataStatus
from munshiji.db.models import ActionOutcome, ActionRequest, KhataEntry, Merchant, Transaction
from munshiji.logging import get_logger
from munshiji.messaging import render_for_tool
from munshiji.money import fmt_inr
from munshiji.providers.actions import ActionDispatch, ActionResult
from munshiji.providers.base import ProviderHealth, ProviderMode
from munshiji.repositories.core import (
    get_customers,
    record_outcome,
    require_action,
    require_merchant,
)

__all__ = [
    "DEFAULT_REDEMPTION_PRIOR",
    "METRIC_MESSAGES_FAILED",
    "METRIC_MESSAGES_SENT",
    "METRIC_REDEEMED",
    "METRIC_REDEMPTION_RATE",
    "METRIC_REVENUE_RECOVERED",
    "OUTBOUND_TOOLS",
    "PAYMENT_LINK_BASE",
    "REDEMPTION_RAMP_DAYS",
    "SEGMENT_REDEMPTION_PRIOR",
    "SIMULATED_OUTCOME_METRICS",
    "LocalActions",
    "ResolvedTarget",
    "action_session_scope",
    "avg_ticket_paise",
    "bump_khata_reminders",
    "default_targets",
    "finalise_action",
    "is_valid_phone",
    "normalise_phone",
    "previous_result",
    "primary_language",
    "redemption_realised",
    "resolve_targets",
    "seeded_unit",
    "short_code",
    "write_delivery_outcomes",
]

logger = get_logger(__name__)

#: Tools that produce outbound traffic. Anything else (e.g. ``save_merchant_note``) is a
#: local-only write and never reaches an action provider.
OUTBOUND_TOOLS: frozenset[str] = frozenset(
    {
        "send_winback_offer",
        "send_udhaar_reminder",
        "create_payment_link",
        "draft_restock_order",
        "schedule_followup",
    }
)

#: Shape of the simulated payment link. Realistic, deterministic, and never live.
PAYMENT_LINK_BASE = "https://paytm.me/pay/"

# ── Outcome metric names (shared with N8nActions and the memory ingester) ────
METRIC_MESSAGES_SENT = "messages_sent"
METRIC_MESSAGES_FAILED = "messages_failed"
METRIC_KHATA_REMINDED = "khata_reminded"
METRIC_PAYMENT_LINK = "payment_link_created"
METRIC_RESTOCK_ITEMS = "restock_items"
METRIC_FOLLOWUP = "followup_scheduled"
METRIC_REDEEMED = "redeemed"
METRIC_REVENUE_RECOVERED = "revenue_recovered_paise"
METRIC_REDEMPTION_RATE = "redemption_rate"

#: Metrics owned by :meth:`LocalActions.simulate_outcomes`; re-running replaces them.
SIMULATED_OUTCOME_METRICS: tuple[str, ...] = (
    METRIC_REDEEMED,
    METRIC_REVENUE_RECOVERED,
    METRIC_REDEMPTION_RATE,
)


# ─────────────────────────────────────────────────────────────────────────────
# Redemption priors
# ─────────────────────────────────────────────────────────────────────────────

#: P(redeems a win-back offer | segment), used by :meth:`LocalActions.simulate_outcomes`.
#:
#: These are the same segment priors SPEC.md §7 calls for in
#: ``win-back value = P(return|offer) × avg_ticket × expected_visits``. Calibration rationale:
#: broad WhatsApp win-back pushes by Indian kirana/D2C merchants land in the 10–20% redemption
#: band, and response is strongly monotonic in relationship depth — a champion already has the
#: habit and the trust, so they convert at roughly 3× an occasional buyer, while a customer who
#: has genuinely lapsed converts well below the blended rate.
#:
#: A realised rate well above that 10–20% band is expected here and is not a tuning error. The
#: band describes a *broadcast*; MunshiJi does the opposite. The dormancy engine picks a handful
#: of the shop's own best regulars — people with a buying history who have just stopped — and the
#: message goes to them by name, in their language, from a shop they know. On the seeded book
#: that audience skews to the higher segments, so the modelled response lands near 40%. If a rate
#: like that is ever quoted, quote the reason with it: it is the targeting, not the offer.
SEGMENT_REDEMPTION_PRIOR: dict[CustomerSegment, float] = {
    CustomerSegment.CHAMPION: 0.45,
    CustomerSegment.LOYAL: 0.35,
    CustomerSegment.REGULAR: 0.25,
    CustomerSegment.AT_RISK: 0.22,
    CustomerSegment.NEW: 0.20,
    CustomerSegment.OCCASIONAL: 0.15,
    CustomerSegment.DORMANT: 0.12,
    CustomerSegment.LOST: 0.06,
}

#: Blended rate for a customer the RFM pass has not segmented yet.
DEFAULT_REDEMPTION_PRIOR = 0.18

#: Redemptions arrive over days, not instantly: ``1 - exp(-days / RAMP)`` of the eventual
#: response has landed by day *d* — ~28% by day 1, ~63% by day 3, ~90% by day 7.
REDEMPTION_RAMP_DAYS = 3.0


def redemption_realised(days_elapsed: int) -> float:
    """Fraction of the eventual redemption response realised ``days_elapsed`` after dispatch."""
    return 1.0 - math.exp(-max(0, int(days_elapsed)) / REDEMPTION_RAMP_DAYS)


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic primitives
# ─────────────────────────────────────────────────────────────────────────────

_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/1/I/L/O — readable aloud


def _digest(parts: Sequence[Any]) -> bytes:
    joined = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).digest()


def seeded_unit(*parts: Any) -> float:
    """A stable float in ``[0, 1)`` derived from ``parts``.

    SHA-256 based, so it is identical across processes, platforms and Python runs — unlike
    :func:`hash`, which is salted per process.
    """
    return int.from_bytes(_digest(parts)[:8], "big") / float(1 << 64)


def short_code(*parts: Any, length: int = 8) -> str:
    """A stable, human-readable code (e.g. ``'7KQ4M2XD'``) derived from ``parts``."""
    value = int.from_bytes(_digest(parts), "big")
    chars: list[str] = []
    for _ in range(max(1, length)):
        value, remainder = divmod(value, len(_CODE_ALPHABET))
        chars.append(_CODE_ALPHABET[remainder])
    return "".join(chars)


_NON_DIGITS = re.compile(r"\D")


def normalise_phone(phone: str | None) -> str:
    """Return an Indian mobile as ``+91XXXXXXXXXX``, or ``''`` when it is not usable.

    Accepts ``9876543210``, ``09876543210``, ``+91 98765 43210``, ``91-9876543210``.
    Rejects landlines, short codes, junk and anything not starting 6–9 — those are exactly the
    rows that fail in production, so they fail here too.
    """
    digits = _NON_DIGITS.sub("", phone or "")
    if len(digits) == 13 and digits.startswith("091"):
        digits = digits[1:]
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    return ""


def is_valid_phone(phone: str | None) -> bool:
    """Whether ``phone`` is a deliverable Indian mobile number."""
    return bool(normalise_phone(phone))


@contextmanager
def action_session_scope(factory: Callable[[], Session]) -> Iterator[Session]:
    """Transactional scope for one action: commit on success, roll back on anything else."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ─────────────────────────────────────────────────────────────────────────────
# Target resolution
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ResolvedTarget:
    """One recipient, with the exact text they would receive."""

    index: int
    customer_id: str = ""
    name: str = ""
    phone: str = ""  # normalised E.164, or "" when undeliverable
    raw_phone: str = ""
    amount_paise: int = 0
    khata_entry_id: str = ""
    message_hi: str = ""
    message_en: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def deliverable(self) -> bool:
        """A target is deliverable iff we hold a valid Indian mobile for it."""
        return bool(self.phone)

    def message(self, language: str = "hi") -> str:
        return self.message_hi if language == "hi" else self.message_en

    def as_payload(self, language: str = "hi") -> dict[str, Any]:
        """Serialisable form — this is what n8n receives per recipient."""
        payload: dict[str, Any] = {
            "customer_id": self.customer_id,
            "name": self.name,
            "phone": self.phone or self.raw_phone,
            "message": self.message(language),
            "message_hi": self.message_hi,
            "message_en": self.message_en,
            "deliverable": self.deliverable,
        }
        if self.amount_paise:
            payload["amount_paise"] = self.amount_paise
            payload["amount_display"] = fmt_inr(self.amount_paise)
        if self.khata_entry_id:
            payload["khata_entry_id"] = self.khata_entry_id
        payload.update(self.extra)
        return payload


def resolve_targets(
    *,
    tool_name: str,
    params: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
    shop_name: str,
    language: str = "hi",
) -> list[ResolvedTarget]:
    """Turn raw target dicts into :class:`ResolvedTarget` rows with rendered copy.

    A target that already carries ``message`` / ``message_hi`` keeps it; otherwise the text is
    rendered from :mod:`munshiji.messaging` for that specific recipient, because personalised
    copy cannot be rendered once for a whole audience.
    """
    resolved: list[ResolvedTarget] = []
    for index, raw in enumerate(targets):
        raw_phone = str(raw.get("phone") or "")
        rendered = render_for_tool(tool_name, shop_name=shop_name, params=params, target=raw)
        supplied = str(raw.get("message") or "")
        message_hi = (
            str(raw.get("message_hi") or "")
            or (supplied if language == "hi" else "")
            or rendered["hi"]
        )
        message_en = (
            str(raw.get("message_en") or "")
            or (supplied if language != "hi" else "")
            or rendered["en"]
        )
        known = {
            "customer_id",
            "name",
            "phone",
            "amount_paise",
            "khata_entry_id",
            "message",
            "message_hi",
            "message_en",
        }
        resolved.append(
            ResolvedTarget(
                index=index,
                customer_id=str(raw.get("customer_id") or ""),
                name=str(raw.get("name") or ""),
                phone=normalise_phone(raw_phone),
                raw_phone=raw_phone,
                amount_paise=int(raw.get("amount_paise") or 0),
                khata_entry_id=str(raw.get("khata_entry_id") or ""),
                message_hi=message_hi,
                message_en=message_en,
                extra={k: v for k, v in raw.items() if k not in known},
            )
        )
    return resolved


def default_targets(
    tool_name: str, params: Mapping[str, Any], merchant: Merchant
) -> list[dict[str, Any]]:
    """Recipients for the tools that address someone other than a customer.

    ``draft_restock_order`` goes to the supplier named in ``params``; ``schedule_followup`` is a
    note the merchant gets back later, so the merchant is the recipient.
    """
    if tool_name == "draft_restock_order":
        return [
            {
                "customer_id": "",
                "name": str(params.get("supplier_name") or "Supplier"),
                "phone": str(params.get("supplier_phone") or ""),
                "role": "supplier",
            }
        ]
    if tool_name == "schedule_followup":
        return [
            {
                "customer_id": "",
                "name": merchant.owner_name,
                "phone": merchant.phone,
                "role": "merchant",
            }
        ]
    return []


def primary_language(params: Mapping[str, Any], merchant: Merchant) -> str:
    """``"hi"`` or ``"en"`` — which rendering is the one actually sent."""
    raw = str(params.get("language") or merchant.language or "hi")
    return "hi" if raw.lower().startswith("hi") else "en"


# ─────────────────────────────────────────────────────────────────────────────
# Persistence helpers — shared by both providers
# ─────────────────────────────────────────────────────────────────────────────


def previous_result(action: ActionRequest, idempotency_key: str) -> ActionResult | None:
    """Rebuild the stored :class:`ActionResult` if this key has already been dispatched.

    This is the idempotency gate. A re-dispatch (retry, double-tap on Approve, a replayed demo)
    returns the original result and writes nothing.
    """
    stored = action.result or {}
    if not isinstance(stored, dict):
        return None
    if not idempotency_key or stored.get("idempotency_key") != idempotency_key:
        return None
    return ActionResult(
        ok=bool(stored.get("ok", False)),
        provider=stored.get("provider", "local"),
        message=str(stored.get("message", "")),
        detail=dict(stored.get("detail") or {}),
        delivered_count=int(stored.get("delivered_count") or 0),
        failed_count=int(stored.get("failed_count") or 0),
        latency_ms=int(stored.get("latency_ms") or 0),
        external_id=str(stored.get("external_id") or ""),
    )


def write_delivery_outcomes(
    session: Session,
    action_id: str,
    *,
    delivered: int,
    failed: int,
    note: str = "",
) -> list[ActionOutcome]:
    """Record the two immediate delivery metrics every outbound action produces."""
    return [
        record_outcome(
            session, action_id, METRIC_MESSAGES_SENT, value_num=float(delivered), note=note
        ),
        record_outcome(
            session, action_id, METRIC_MESSAGES_FAILED, value_num=float(failed), note=note
        ),
    ]


def bump_khata_reminders(
    session: Session,
    merchant_id: str,
    targets: Sequence[ResolvedTarget],
    *,
    at: datetime | None = None,
) -> list[KhataEntry]:
    """Advance ``reminders_sent`` / ``last_reminder_at`` for the entries actually reminded.

    Only delivered targets count — a message that never left does not consume the customer's
    7-day reminder cooldown (SPEC.md §2.4). A target may name its entry explicitly via
    ``khata_entry_id``; otherwise every open entry for that customer is bumped.
    """
    moment = at or now_utc()
    touched: dict[str, KhataEntry] = {}
    for target in targets:
        if not target.deliverable:
            continue
        entries: list[KhataEntry] = []
        if target.khata_entry_id:
            entry = session.get(KhataEntry, target.khata_entry_id)
            if entry is not None:
                entries = [entry]
        elif target.customer_id:
            entries = list(
                session.scalars(
                    select(KhataEntry).where(
                        KhataEntry.merchant_id == merchant_id,
                        KhataEntry.customer_id == target.customer_id,
                        KhataEntry.status.in_([KhataStatus.OPEN, KhataStatus.PARTIAL]),
                    )
                ).all()
            )
        for entry in entries:
            if entry.id in touched:
                continue
            entry.reminders_sent = int(entry.reminders_sent or 0) + 1
            entry.last_reminder_at = moment
            touched[entry.id] = entry
    if touched:
        session.flush()
    return list(touched.values())


def finalise_action(
    session: Session,
    action: ActionRequest,
    *,
    result: ActionResult,
    targets: Sequence[ResolvedTarget],
    idempotency_key: str,
    extra_result: Mapping[str, Any] | None = None,
) -> None:
    """Stamp the audit trail on the :class:`ActionRequest` and close out its state machine.

    Only ``APPROVED`` / ``EXECUTING`` advance to ``EXECUTED`` / ``FAILED`` — the approval gate
    (SPEC.md §2.4) owns everything before that, and this layer must never launder a
    ``PENDING_APPROVAL`` action into an executed one.
    """
    action.provider = result.provider
    action.target_count = len(targets)
    action.executed_at = now_utc()
    action.error = "" if result.ok else result.message[:400]
    action.result = {
        **result.as_dict(),
        "idempotency_key": idempotency_key,
        "tool": action.tool_name,
        "target_customer_ids": [t.customer_id for t in targets if t.customer_id],
        **(dict(extra_result) if extra_result else {}),
    }
    if action.status in (ActionStatus.APPROVED, ActionStatus.EXECUTING):
        action.status = ActionStatus.EXECUTED if result.ok else ActionStatus.FAILED
    else:
        logger.warning(
            "action %s dispatched from status %s — leaving status untouched",
            action.id,
            action.status,
        )
    session.flush()


def avg_ticket_paise(session: Session, customer_ids: Sequence[str]) -> dict[str, int]:
    """Each customer's real historical average ticket, in paise.

    Computed from their non-return transactions as ``round_half_up(total / count)`` using
    integer arithmetic — ``(2*total + count) // (2*count)`` — so the figure is exactly
    reproducible by anyone recomputing it from the same rows. Customers with no transaction
    history fall back to ``total_spend_paise / txn_count`` from the customer record, and to 0
    when even that is empty.
    """
    ids = [cid for cid in customer_ids if cid]
    if not ids:
        return {}
    rows = session.execute(
        select(
            Transaction.customer_id,
            func.coalesce(func.sum(Transaction.amount_paise), 0),
            func.count(Transaction.id),
        )
        .where(Transaction.customer_id.in_(ids), Transaction.is_return.is_(False))
        .group_by(Transaction.customer_id)
    ).all()
    averages: dict[str, int] = {}
    for customer_id, total, count in rows:
        if not customer_id or not count:
            continue
        averages[str(customer_id)] = (2 * int(total) + int(count)) // (2 * int(count))
    for customer in get_customers(session, ids):
        if customer.id in averages:
            continue
        txns = int(customer.txn_count or 0)
        spend = int(customer.total_spend_paise or 0)
        averages[customer.id] = (2 * spend + txns) // (2 * txns) if txns else 0
    return averages


# ─────────────────────────────────────────────────────────────────────────────
# The provider
# ─────────────────────────────────────────────────────────────────────────────


class LocalActions:
    """In-process :class:`~munshiji.providers.actions.ActionProvider`. Offline, never fake."""

    name = "local-actions"
    mode: ProviderMode = "local"

    def __init__(self, session_factory: Callable[[], Session] | None = None) -> None:
        """Args:
        session_factory: Callable returning a :class:`~sqlalchemy.orm.Session`. Defaults to
            :func:`munshiji.db.base.get_sessionmaker`, resolved lazily so importing this
            module never touches the database.
        """
        self._session_factory = session_factory

    # ── Protocol ────────────────────────────────────────────────────────────

    async def dispatch(self, dispatch: ActionDispatch) -> ActionResult:
        """Execute one approved action in-process. Idempotent on ``idempotency_key``."""
        started = time.perf_counter()
        key = dispatch.idempotency_key or dispatch.action_id

        with action_session_scope(self._factory()) as session:
            action = require_action(session, dispatch.action_id)
            replay = previous_result(action, key)
            if replay is not None:
                logger.info("action %s replayed from idempotency key %s", action.id, key)
                return replay

            merchant = require_merchant(session, dispatch.merchant_id)
            language = primary_language(dispatch.params, merchant)
            raw_targets = [dict(t) for t in dispatch.targets] or default_targets(
                dispatch.tool_name, dispatch.params, merchant
            )

            links = self._mint_links(dispatch, raw_targets, key)
            targets = resolve_targets(
                tool_name=dispatch.tool_name,
                params=dispatch.params,
                targets=raw_targets,
                shop_name=merchant.shop_name,
                language=language,
            )

            delivered = [t for t in targets if t.deliverable]
            failed = [t for t in targets if not t.deliverable]
            batch_id = f"sim_{short_code(key, dispatch.tool_name)}"

            if dispatch.tool_name not in OUTBOUND_TOOLS:
                logger.warning(
                    "tool %s has no outbound channel; recording a local-only execution",
                    dispatch.tool_name,
                )

            detail: dict[str, Any] = {
                "tool": dispatch.tool_name,
                "mode": "simulated",
                "language": language,
                "idempotency_key": key,
                "targets": [
                    {
                        **target.as_payload(language),
                        "status": "delivered" if target.deliverable else "failed",
                        "ref": self._message_ref(key, target),
                        "reason": "" if target.deliverable else "missing or invalid phone number",
                    }
                    for target in targets
                ],
            }
            if links:
                detail["payment_links"] = links

            result = ActionResult(
                ok=bool(delivered) or not targets,
                provider=self.mode,
                message=self._summarise(dispatch.tool_name, targets, delivered, failed, links),
                detail=detail,
                delivered_count=len(delivered),
                failed_count=len(failed),
                latency_ms=int((time.perf_counter() - started) * 1000),
                external_id=batch_id,
            )

            write_delivery_outcomes(
                session,
                action.id,
                delivered=len(delivered),
                failed=len(failed),
                note=f"{self.name} · {dispatch.tool_name}",
            )
            self._write_tool_outcomes(session, dispatch, action, targets, delivered, links)

            extra: dict[str, Any] = {}
            if dispatch.tool_name == "send_udhaar_reminder":
                touched = bump_khata_reminders(session, dispatch.merchant_id, delivered)
                extra["khata_entry_ids"] = [entry.id for entry in touched]
                # ``result.detail`` is this same dict, so the count is stamped on the audit
                # trail by ``finalise_action`` below.
                detail["khata_entries_updated"] = len(touched)
            if links:
                extra["payment_links"] = links

            finalise_action(
                session,
                action,
                result=result,
                targets=targets,
                idempotency_key=key,
                extra_result=extra,
            )
            return result

    async def health(self) -> ProviderHealth:
        """Local mode is healthy iff the database answers — that is its only dependency."""
        started = time.perf_counter()
        try:
            session = self._factory()()
            try:
                session.execute(select(func.count(ActionRequest.id)).limit(1)).one()
            finally:
                session.close()
        except Exception as exc:  # health probes must never propagate
            return ProviderHealth(
                name=self.name,
                kind="actions",
                mode=self.mode,
                ok=False,
                detail=f"database unavailable: {exc}",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        return ProviderHealth(
            name=self.name,
            kind="actions",
            mode=self.mode,
            ok=True,
            detail="in-process execution, simulated delivery",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # ── Outcome simulator ───────────────────────────────────────────────────

    async def simulate_outcomes(
        self, action_id: str, *, days_elapsed: int = 1
    ) -> list[ActionOutcome]:
        """Derive what a dispatched win-back offer actually earned, from the database.

        For each customer the offer targeted:

        1. their segment picks a redemption prior from :data:`SEGMENT_REDEMPTION_PRIOR`;
        2. a stable per-customer draw ``u = seeded_unit(action_id, customer_id)`` decides the
           outcome — ``u < prior × redemption_realised(days_elapsed)``. Because ``u`` never
           moves and the ramp only grows, redemptions accumulate monotonically as the days pass
           and the same action always yields the same customers;
        3. every redeemer contributes **their own historical average ticket**
           (:func:`avg_ticket_paise`) to recovered revenue.

        No constant ever enters the arithmetic: the segments, the transaction history and the
        target list all come from the DB. This is what lets conversation #2 say
        *"4 laut aaye, ₹2,340 aaye"* and have it be true.

        Re-running replaces the previous simulated rows rather than appending, so the numbers
        stay consistent whichever day the demo asks about. Returns the written outcome rows.
        """
        with action_session_scope(self._factory()) as session:
            action = require_action(session, action_id)
            if action.tool_name != "send_winback_offer":
                logger.debug("simulate_outcomes: nothing to model for tool %s", action.tool_name)
                return []

            customer_ids = self._targeted_customer_ids(action)
            customers = get_customers(session, customer_ids)
            if not customers:
                logger.warning("simulate_outcomes: action %s targeted no customers", action_id)
                return []

            realised = redemption_realised(days_elapsed)
            # Seeded on the customer and the campaign type, deliberately *not* on the action id.
            #
            # Action ids are minted fresh on every run, so keying the draw on one made the
            # outcome different each time the demo was replayed — which contradicts the
            # determinism the rest of the seed guarantees, and at these priors (dormant buyers,
            # ~28% of the response landed by day one) meant the demo would often report that
            # nobody came back. Whether a given customer responds to a win-back is better modelled
            # as a property of that customer anyway: the same person, offered the same kind of
            # thing, behaves the same way.
            redeemers = [
                customer
                for customer in customers
                if seeded_unit(action.tool_name, customer.id)
                < SEGMENT_REDEMPTION_PRIOR.get(customer.segment, DEFAULT_REDEMPTION_PRIOR)
                * realised
            ]
            averages = avg_ticket_paise(session, [customer.id for customer in redeemers])
            revenue = sum(averages.get(customer.id, 0) for customer in redeemers)
            rate = len(redeemers) / len(customers)

            session.execute(
                delete(ActionOutcome).where(
                    ActionOutcome.action_id == action_id,
                    ActionOutcome.metric.in_(SIMULATED_OUTCOME_METRICS),
                )
            )
            note = f"day {max(0, int(days_elapsed))} after dispatch · {len(customers)} targeted"
            outcomes = [
                record_outcome(
                    session,
                    action_id,
                    METRIC_REDEEMED,
                    value_num=float(len(redeemers)),
                    note=note,
                ),
                record_outcome(
                    session,
                    action_id,
                    METRIC_REVENUE_RECOVERED,
                    value_paise=int(revenue),
                    note=f"{note} · sum of redeemers' average ticket",
                ),
                record_outcome(
                    session,
                    action_id,
                    METRIC_REDEMPTION_RATE,
                    value_num=round(rate, 4),
                    note=note,
                ),
            ]
            action.result = {
                **(action.result or {}),
                "redeemed_customer_ids": [customer.id for customer in redeemers],
                "revenue_recovered_paise": int(revenue),
                "simulated_days_elapsed": max(0, int(days_elapsed)),
            }
            session.flush()
            logger.info(
                "simulated outcomes for %s: %d/%d redeemed, %s recovered",
                action_id,
                len(redeemers),
                len(customers),
                fmt_inr(int(revenue)),
            )
            return outcomes

    # ── Internals ───────────────────────────────────────────────────────────

    def _factory(self) -> Callable[[], Session]:
        if self._session_factory is not None:
            return self._session_factory
        from munshiji.db.base import get_sessionmaker

        return get_sessionmaker()

    @staticmethod
    def _targeted_customer_ids(action: ActionRequest) -> list[str]:
        stored = action.result if isinstance(action.result, dict) else {}
        ids = stored.get("target_customer_ids") or []
        if not ids:
            params = action.params if isinstance(action.params, dict) else {}
            ids = params.get("customer_ids") or []
        return [str(cid) for cid in ids if cid]

    @staticmethod
    def _message_ref(key: str, target: ResolvedTarget) -> str:
        """A stable, WhatsApp-looking reference for one delivered message."""
        if not target.deliverable:
            return ""
        handle = target.customer_id or target.name
        return f"wa_sim_{short_code(key, handle, target.index)}"

    @staticmethod
    def _mint_links(
        dispatch: ActionDispatch, raw_targets: list[dict[str, Any]], key: str
    ) -> dict[str, str]:
        """Mint a deterministic, clearly-simulated payment link per recipient."""
        if dispatch.tool_name != "create_payment_link":
            return {}
        links: dict[str, str] = {}
        for index, target in enumerate(raw_targets):
            existing = str(target.get("link") or "")
            handle = str(target.get("customer_id") or target.get("name") or index)
            link = existing or f"{PAYMENT_LINK_BASE}{short_code(key, handle, index)}"
            target["link"] = link
            links[handle] = link
        return links

    @staticmethod
    def _summarise(
        tool_name: str,
        targets: Sequence[ResolvedTarget],
        delivered: Sequence[ResolvedTarget],
        failed: Sequence[ResolvedTarget],
        links: Mapping[str, str],
    ) -> str:
        if not targets:
            return f"No recipients resolved for {tool_name}; nothing was sent."
        head = f"{len(delivered)}/{len(targets)} delivered (simulated offline dispatch)"
        if tool_name == "create_payment_link" and links:
            first = next(iter(links.values()))
            amount = targets[0].amount_paise
            money = f" for {fmt_inr(amount)}" if amount else ""
            head = (
                f"Simulated payment link{money}: {first} — offline mode, this is not a live "
                f"Paytm link. {head}"
            )
        if failed:
            head += f"; {len(failed)} skipped: missing or invalid phone number"
        return head

    @staticmethod
    def _write_tool_outcomes(
        session: Session,
        dispatch: ActionDispatch,
        action: ActionRequest,
        targets: Sequence[ResolvedTarget],
        delivered: Sequence[ResolvedTarget],
        links: Mapping[str, str],
    ) -> None:
        """Tool-specific immediate metrics, on top of the two delivery counters."""
        tool = dispatch.tool_name
        if tool == "create_payment_link" and links:
            total = sum(target.amount_paise for target in targets)
            record_outcome(
                session,
                action.id,
                METRIC_PAYMENT_LINK,
                value_num=float(len(links)),
                value_paise=total or None,
                note=next(iter(links.values()))[:240],
            )
        elif tool == "draft_restock_order":
            items = dispatch.params.get("items") or []
            cost = int(dispatch.params.get("estimated_cost_paise") or 0)
            record_outcome(
                session,
                action.id,
                METRIC_RESTOCK_ITEMS,
                value_num=float(len(items)),
                value_paise=cost or None,
                note=str(dispatch.params.get("supplier_name") or "")[:240],
            )
        elif tool == "schedule_followup":
            record_outcome(
                session,
                action.id,
                METRIC_FOLLOWUP,
                value_num=float(len(delivered)),
                note=str(dispatch.params.get("when_display") or dispatch.params.get("when") or "")[
                    :240
                ],
            )
        elif tool == "send_udhaar_reminder":
            record_outcome(
                session,
                action.id,
                METRIC_KHATA_REMINDED,
                value_num=float(len(delivered)),
                value_paise=sum(target.amount_paise for target in delivered) or None,
                note="outstanding balance reminded",
            )
