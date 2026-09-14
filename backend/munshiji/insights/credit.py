"""Udhaar (informal credit) engine.

Khata is a relationship, not a receivable. The engine therefore optimises for *recovered rupees
without damaged relationships*: it chases the money most likely to come back, and it picks the
register for each message from the customer's own settlement history rather than from the size
of the debt. Per SPEC.md §2.4 there is no tier harsher than FIRM, and FIRM means clear, not
threatening.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from munshiji.clock import ist_date_of, to_ist
from munshiji.config import get_settings
from munshiji.db.enums import InsightKind, Severity, Tone
from munshiji.insights.base import InsightContext, InsightDraft
from munshiji.insights.stats import clamp, safe_div
from munshiji.logging import get_logger
from munshiji.money import fmt_inr
from munshiji.repositories import analytics
from munshiji.repositories.core import get_open_khata

__all__ = [
    "AGING_BUCKETS",
    "BUCKET_RISK_WEIGHT",
    "RELIABILITY_PRIOR",
    "RELIABILITY_PRIOR_STRENGTH",
    "SETTLE_REFERENCE_DAYS",
    "ChaseTarget",
    "UdhaarOverdueEngine",
    "bucket_for",
    "recoverability",
    "reliability_score",
    "select_tone",
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


# ── Aging ───────────────────────────────────────────────────────────────────

#: ``(label, inclusive_upper_bound_in_days)``; ``None`` means "and beyond". Age is measured from
#: ``opened_at`` — "kitne din pehle udhaar liya" — which is how a khata is actually kept. The
#: days past ``due_at`` are reported separately in ``days_overdue`` for entries that carry a due
#: date. Boundaries are inclusive: exactly 15 days old is still 0–15.
AGING_BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("0-15", 15),
    ("16-30", 30),
    ("31-60", 60),
    ("60+", None),
)

#: How hard to push each bucket. Weight rises with age because the probability of ever collecting
#: falls with it — the classic receivables shape — so old money deserves the scarce attention.
BUCKET_RISK_WEIGHT: dict[str, float] = {
    "0-15": 0.4,
    "16-30": 0.7,
    "31-60": 1.0,
    "60+": 1.3,
}

# ── Reliability ─────────────────────────────────────────────────────────────

#: Settling within this many days counts as fully prompt; 30 days is the informal credit term a
#: kirana extends against a monthly salary cycle.
SETTLE_REFERENCE_DAYS = 30

#: Weight split between "how fast do they pay" and "do they pay by the promised date".
PROMPTNESS_WEIGHT = 0.6
ON_TIME_WEIGHT = 0.4

#: Uninformative prior for a customer with no closed entries, and the pseudo-count it is worth.
#: Two closed entries move the score halfway from the prior to the observed rate; one does not.
RELIABILITY_PRIOR = 0.5
RELIABILITY_PRIOR_STRENGTH = 2.0

#: ``recoverability`` maps reliability onto an expected-collection factor. Even a poor payer has
#: a floor — a reminder still works sometimes — and a good one never quite reaches certainty.
RECOVERABILITY_FLOOR = 0.35
RECOVERABILITY_RANGE = 0.65

#: Reliability boundaries used by the tone table.
RELIABLE_AT = 0.7
UNRELIABLE_BELOW = 0.4

#: Most customers to put on one chase list.
MAX_CHASE_TARGETS = 8


def bucket_for(age_days: int) -> str:
    """Aging bucket label for an entry that is ``age_days`` old (boundaries inclusive)."""
    for label, upper in AGING_BUCKETS:
        if upper is None or age_days <= upper:
            return label
    return AGING_BUCKETS[-1][0]


def reliability_score(stats: analytics.SettleStats | None) -> float:
    """Blend settlement speed and on-time rate into 0–1, shrunk toward an uninformative prior.

    ``raw = 0.6 * promptness + 0.4 * (1 - late_share)`` where
    ``promptness = 1 - avg_days_to_settle / 30``, clamped to [0, 1].

    That raw score is then shrunk: ``(n*raw + k*0.5) / (n + k)`` with ``k = 2``. Without this a
    customer who settled a single entry in one day would score 1.0 and out-rank someone with a
    decade of good history — a classic small-sample trap, and an expensive one when the output
    decides who gets chased.
    """
    if stats is None or stats.settled_count <= 0:
        return RELIABILITY_PRIOR
    speed = safe_div(stats.avg_days_to_settle, SETTLE_REFERENCE_DAYS, 1.0)
    promptness = clamp(1.0 - speed, 0.0, 1.0)
    on_time = clamp(1.0 - stats.late_share, 0.0, 1.0)
    raw = PROMPTNESS_WEIGHT * promptness + ON_TIME_WEIGHT * on_time
    count = float(stats.settled_count)
    shrunk = (count * raw + RELIABILITY_PRIOR_STRENGTH * RELIABILITY_PRIOR) / (
        count + RELIABILITY_PRIOR_STRENGTH
    )
    return round(clamp(shrunk, 0.0, 1.0), 4)


def recoverability(reliability: float) -> float:
    """Expected share of an outstanding amount that a reminder actually brings back."""
    return round(RECOVERABILITY_FLOOR + RECOVERABILITY_RANGE * clamp(reliability, 0.0, 1.0), 4)


#: Tone table (SPEC.md §2.4). Rows are aging buckets, columns are reliability bands
#: ``(>= 0.7, 0.4-0.7, < 0.4)``. A good payer is never pushed hard inside a month — they are
#: almost certainly just waiting for salary day — while money past 60 days with a poor history
#: earns clarity, which is what FIRM is.
TONE_TABLE: dict[str, tuple[Tone, Tone, Tone]] = {
    "0-15": (Tone.GENTLE, Tone.GENTLE, Tone.GENTLE),
    "16-30": (Tone.GENTLE, Tone.STANDARD, Tone.STANDARD),
    "31-60": (Tone.STANDARD, Tone.STANDARD, Tone.FIRM),
    "60+": (Tone.STANDARD, Tone.FIRM, Tone.FIRM),
}


def select_tone(bucket: str, reliability: float) -> Tone:
    """Pick the reminder register from the aging bucket and the customer's reliability."""
    row = TONE_TABLE.get(bucket, TONE_TABLE["31-60"])
    if reliability >= RELIABLE_AT:
        return row[0]
    if reliability >= UNRELIABLE_BELOW:
        return row[1]
    return row[2]


@dataclass(slots=True, frozen=True)
class ChaseTarget:
    """One khata entry selected for a reminder."""

    entry_id: str
    customer_id: str
    customer_name: str
    outstanding_paise: int
    age_days: int
    days_overdue: int | None
    bucket: str
    reliability: float
    recoverability: float
    tone: Tone
    chase_priority: float
    reminders_sent: int
    last_reminder_at: str | None

    @property
    def expected_recovery_paise(self) -> int:
        return int(round(self.outstanding_paise * self.recoverability))


class UdhaarOverdueEngine:
    """Who to chase, how hard, and what it is realistically worth.

    ``chase_priority = outstanding x risk_weight(bucket) x recoverability(reliability)`` — size,
    urgency and likelihood in one number, so ₹200 from a reliable customer never outranks ₹4,000
    from one who pays eventually. Entries reminded within ``settings.reminder_cooldown_days`` are
    dropped before ranking: the cooldown is a product-safety rule, not a nicety, and enforcing it
    here means the merchant is never even *offered* a nagging action.
    """

    kind = InsightKind.UDHAAR_OVERDUE

    def __init__(
        self,
        *,
        max_targets: int = MAX_CHASE_TARGETS,
        cooldown_days: int | None = None,
    ) -> None:
        self.max_targets = max_targets
        self._cooldown_days = cooldown_days

    @property
    def cooldown_days(self) -> int:
        if self._cooldown_days is not None:
            return self._cooldown_days
        return int(get_settings().reminder_cooldown_days)

    def _in_cooldown(self, last_reminder_at: datetime | None, as_of: datetime) -> bool:
        if last_reminder_at is None:
            return False
        return to_ist(last_reminder_at) > to_ist(as_of) - timedelta(days=self.cooldown_days)

    def run(self, ctx: InsightContext) -> list[InsightDraft]:
        entries = get_open_khata(ctx.session, ctx.merchant_id)
        if not entries:
            return []
        today = ist_date_of(ctx.as_of)
        settle_stats = analytics.khata_settle_stats(ctx.session, ctx.merchant_id)
        names = {score.customer_id: score.name for score in analytics.compute_rfm(ctx).values()}

        buckets = {label: {"count": 0, "total_paise": 0} for label, _ in AGING_BUCKETS}
        total_outstanding = 0
        suppressed = 0
        targets: list[ChaseTarget] = []

        for entry in entries:
            outstanding = entry.outstanding_paise
            if outstanding <= 0:
                continue
            age_days = (today - ist_date_of(entry.opened_at)).days
            bucket = bucket_for(age_days)
            buckets[bucket]["count"] += 1
            buckets[bucket]["total_paise"] += outstanding
            total_outstanding += outstanding

            if self._in_cooldown(entry.last_reminder_at, ctx.as_of):
                suppressed += 1
                continue

            reliability = reliability_score(settle_stats.get(entry.customer_id))
            factor = recoverability(reliability)
            days_overdue = (
                (today - ist_date_of(entry.due_at)).days if entry.due_at is not None else None
            )
            customer = entry.customer
            targets.append(
                ChaseTarget(
                    entry_id=entry.id,
                    customer_id=entry.customer_id,
                    customer_name=(
                        customer.name
                        if customer is not None
                        else names.get(entry.customer_id, entry.customer_id)
                    ),
                    outstanding_paise=outstanding,
                    age_days=age_days,
                    days_overdue=days_overdue,
                    bucket=bucket,
                    reliability=reliability,
                    recoverability=factor,
                    tone=select_tone(bucket, reliability),
                    chase_priority=round(outstanding * BUCKET_RISK_WEIGHT[bucket] * factor, 2),
                    reminders_sent=entry.reminders_sent,
                    last_reminder_at=(
                        to_ist(entry.last_reminder_at).isoformat(timespec="minutes")
                        if entry.last_reminder_at
                        else None
                    ),
                )
            )

        if total_outstanding <= 0:
            return []

        targets.sort(key=lambda target: (-target.chase_priority, target.entry_id))
        chase = targets[: self.max_targets]
        expected = sum(target.expected_recovery_paise for target in chase)
        over_60 = buckets["60+"]["total_paise"]
        open_count = sum(bucket["count"] for bucket in buckets.values())

        if over_60 >= 1_500_000 or total_outstanding >= 3_000_000:
            severity = Severity.CRITICAL
        elif over_60 >= 500_000 or total_outstanding >= 1_000_000:
            severity = Severity.HIGH
        elif total_outstanding >= 300_000:
            severity = Severity.MEDIUM
        else:
            severity = Severity.LOW

        if not chase:
            # Everything is inside the cooldown: still report the position, propose nothing.
            return [
                self._draft(
                    severity=Severity.INFO,
                    chase=[],
                    buckets=buckets,
                    total_outstanding=total_outstanding,
                    open_count=open_count,
                    suppressed=suppressed,
                    expected=0,
                )
            ]

        return [
            self._draft(
                severity=severity,
                chase=chase,
                buckets=buckets,
                total_outstanding=total_outstanding,
                open_count=open_count,
                suppressed=suppressed,
                expected=expected,
            )
        ]

    def _draft(
        self,
        *,
        severity: Severity,
        chase: list[ChaseTarget],
        buckets: dict[str, dict[str, int]],
        total_outstanding: int,
        open_count: int,
        suppressed: int,
        expected: int,
    ) -> InsightDraft:
        over_60 = buckets["60+"]["total_paise"]
        chase_rows = [
            {
                "entry_id": target.entry_id,
                "customer_id": target.customer_id,
                "customer_name": target.customer_name,
                "outstanding_paise": target.outstanding_paise,
                "age_days": target.age_days,
                "days_overdue": target.days_overdue,
                "bucket": target.bucket,
                "reliability": target.reliability,
                "recoverability": target.recoverability,
                "tone": target.tone.value,
                "chase_priority": target.chase_priority,
                "expected_recovery_paise": target.expected_recovery_paise,
                "reminders_sent": target.reminders_sent,
                "last_reminder_at": target.last_reminder_at,
            }
            for target in chase
        ]
        metrics = {
            "total_outstanding_paise": total_outstanding,
            "open_count": open_count,
            "buckets": {
                label: {
                    "count": buckets[label]["count"],
                    "total_paise": buckets[label]["total_paise"],
                    "risk_weight": BUCKET_RISK_WEIGHT[label],
                }
                for label, _ in AGING_BUCKETS
            },
            "over_60_paise": over_60,
            "chase_count": len(chase),
            "expected_recovery_paise": expected,
            "suppressed_by_cooldown": suppressed,
            "cooldown_days": self.cooldown_days,
            "chase": chase_rows,
        }

        if not chase:
            return InsightDraft(
                kind=self.kind,
                severity=severity,
                title_en=f"{_inr(total_outstanding)} on udhaar, all reminded recently",
                title_hi=f"उधार में {_inr(total_outstanding)} बाक़ी, सबको हाल में याद दिलाया है",
                body_en=(
                    f"{open_count} open entries total {_inr(total_outstanding)}, with "
                    f"{_inr(over_60)} past 60 days. Every one was reminded within the last "
                    f"{self.cooldown_days} days, so nothing goes out today."
                ),
                body_hi=(
                    f"{open_count} खाते में कुल {_inr(total_outstanding)} बाक़ी है, जिसमें "
                    f"{_inr(over_60)} 60 दिन से ज़्यादा पुराना है। सबको पिछले "
                    f"{self.cooldown_days} दिनों में याद दिला चुके हैं — आज कुछ नहीं भेजेंगे।"
                ),
                metrics=metrics,
                suggested_tool=None,
                suggested_params={},
                impact_paise=0,
                confidence=0.9,
                dedupe_key="udhaar_overdue",
                expires_in_days=2,
            )

        top = chase[0]
        tones = [target.tone.value for target in chase]
        dominant = max(set(tones), key=tones.count)
        names = ", ".join(target.customer_name for target in chase[:3])

        return InsightDraft(
            kind=self.kind,
            severity=severity,
            title_en=(
                f"{_inr(total_outstanding)} outstanding on udhaar " f"across {open_count} khatas"
            ),
            title_hi=f"{open_count} खातों में उधार के {_inr(total_outstanding)} बाक़ी हैं",
            body_en=(
                f"{_inr(over_60)} of it is over 60 days old. Chasing the top {len(chase)} — "
                f"{names} — should bring back about {_inr(expected)}. "
                f"{top.customer_name} is the biggest at {_inr(top.outstanding_paise)}, "
                f"{top.age_days} days old, reliability {top.reliability:.2f}, so a "
                f"{top.tone.value} reminder."
                + (f" {suppressed} entries skipped — reminded too recently." if suppressed else "")
            ),
            body_hi=(
                f"इसमें {_inr(over_60)} 60 दिन से ज़्यादा पुराना है। ऊपर के {len(chase)} "
                f"खाते — {names} — याद दिलाने पर लगभग {_inr(expected)} वापस आ सकता है। "
                f"सबसे बड़ा {top.customer_name} का {_inr(top.outstanding_paise)} है, "
                f"{top.age_days} दिन पुराना।"
                + (f" {suppressed} खाते छोड़ दिए — हाल ही में याद दिलाया था।" if suppressed else "")
            ),
            metrics=metrics,
            suggested_tool="send_udhaar_reminder",
            suggested_params={
                "entry_ids": [target.entry_id for target in chase],
                "customer_ids": [target.customer_id for target in chase],
                "tone": dominant,
                "targets": [
                    {
                        "entry_id": target.entry_id,
                        "customer_id": target.customer_id,
                        "customer_name": target.customer_name,
                        "amount_paise": target.outstanding_paise,
                        "tone": target.tone.value,
                    }
                    for target in chase
                ],
            },
            impact_paise=expected,
            confidence=0.88,
            dedupe_key="udhaar_overdue",
            expires_in_days=2,
        )
