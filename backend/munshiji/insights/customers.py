"""Customer engines: per-cadence dormancy and new-customer acquisition drop.

The dormancy engine is where a naive threshold does the most damage. A global "no visit in 30
days" rule simultaneously *misses* the daily shopper who vanished three weeks ago and *libels*
the monthly ration customer who is simply not due yet. Every customer gets judged against their
own rhythm instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from munshiji.clock import ist_date_of
from munshiji.db.enums import CustomerSegment, InsightKind, Severity
from munshiji.insights.base import InsightContext, InsightDraft
from munshiji.insights.stats import clamp, iqr, median, safe_div, trend_slope
from munshiji.logging import get_logger
from munshiji.money import fmt_inr
from munshiji.repositories import analytics

__all__ = [
    "ABSOLUTE_DORMANCY_FLOOR_DAYS",
    "CADENCE_IQR_MULTIPLIER",
    "MIN_VISITS_FOR_CADENCE",
    "RETURN_PRIOR_BY_SEGMENT",
    "WINBACK_WINDOW_DAYS",
    "DormancyCandidate",
    "DormantCustomerEngine",
    "NewCustomerDropEngine",
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


# ── Dormancy tuning ─────────────────────────────────────────────────────────

#: Two gaps (three visits) is the minimum from which a median and an IQR mean anything.
MIN_VISITS_FOR_CADENCE = 3

#: Tukey's fence. ``median_gap + 1.5*IQR`` is the classic "mild outlier" boundary, applied here
#: to a customer's own gap distribution: being later than their own outlier fence is the signal.
CADENCE_IQR_MULTIPLIER = 1.5

#: A daily shopper has a 1-day cadence and a 0-day IQR, so their fence would be 1 day — they
#: would be "dormant" every Monday morning. This floor keeps the engine from crying wolf.
ABSOLUTE_DORMANCY_FLOOR_DAYS = 10

#: How far ahead the win-back value is estimated.
WINBACK_WINDOW_DAYS = 30

#: Maximum customers in one campaign — a merchant will not approve a 60-person blast, and the
#: outbound rate limit in ``agent/approval.py`` would reject it anyway.
MAX_WINBACK_TARGETS = 15

#: ``P(return | offer)`` priors by segment. These are **planning priors**, not measurements:
#: published reactivation rates for lapsed retail customers under a discount offer sit in the
#: 5–25% band, rising with prior purchase frequency, so the grid is anchored there and kept
#: deliberately conservative (a CHAMPION who has lapsed is the single most recoverable customer
#: a shop has; a LOST one-time walk-in is nearly gone). Every executed campaign writes an
#: ``ActionOutcome``, which is what will eventually replace these with the merchant's own rate.
RETURN_PRIOR_BY_SEGMENT: dict[CustomerSegment, float] = {
    CustomerSegment.CHAMPION: 0.45,
    CustomerSegment.LOYAL: 0.38,
    CustomerSegment.REGULAR: 0.28,
    CustomerSegment.AT_RISK: 0.22,
    CustomerSegment.OCCASIONAL: 0.18,
    CustomerSegment.NEW: 0.15,
    CustomerSegment.DORMANT: 0.15,
    CustomerSegment.LOST: 0.08,
}
DEFAULT_RETURN_PRIOR = 0.18

#: Discount ladder: the further past their own fence a cohort is, the harder the nudge needs to be.
BASE_DISCOUNT_PCT = 10
DEEP_DISCOUNT_PCT = 15
DEEP_DISCOUNT_OVERDUE_RATIO = 2.0
OFFER_VALID_DAYS = 7

# ── New-customer tuning ─────────────────────────────────────────────────────

NEW_CUSTOMER_WEEKS = 8
NEW_CUSTOMER_SLOPE_FLAG = -0.75  # first-time buyers lost per week
NEW_CUSTOMER_DROP_PCT = 25.0
MIN_NEW_CUSTOMERS_EARLY = 6  # total in the earlier half, else the series is too sparse to read


@dataclass(slots=True, frozen=True)
class DormancyCandidate:
    """One customer judged against their own visit cadence."""

    customer_id: str
    name: str
    segment: CustomerSegment
    visits: int
    median_gap_days: float
    gap_iqr_days: float
    threshold_days: float
    days_since_last: int
    last_visit: str | None
    avg_ticket_paise: int
    return_prior: float
    expected_visits: float
    recoverable_paise: int

    @property
    def overdue_ratio(self) -> float:
        """How many times their own expected gap they are overdue by."""
        return safe_div(self.days_since_last, max(self.median_gap_days, 1.0), 0.0)


class DormantCustomerEngine:
    """Customers who have broken *their own* rhythm, ranked by recoverable rupees.

    For each customer with at least :data:`MIN_VISITS_FOR_CADENCE` visits we take the median and
    IQR of their inter-visit gaps and call them dormant when

    ``days_since_last > max(median_gap + 1.5 * IQR, ABSOLUTE_DORMANCY_FLOOR_DAYS)``

    Win-back value is ``P(return|offer) x avg_ticket x expected_visits_in_window`` where
    ``expected_visits = WINBACK_WINDOW_DAYS / median_gap`` clipped to [1, 4] — a customer cannot
    contribute less than the one visit the offer is for, and crediting a daily shopper with 30
    visits from one message would be fantasy.
    """

    kind = InsightKind.DORMANT_CUSTOMERS

    def __init__(self, *, max_targets: int = MAX_WINBACK_TARGETS) -> None:
        self.max_targets = max_targets

    def candidates(self, ctx: InsightContext) -> list[DormancyCandidate]:
        """Every dormant customer, best recoverable value first. Reusable by the agent tools."""
        today = ist_date_of(ctx.as_of)
        histories = analytics.visit_histories(ctx.session, ctx.merchant_id, as_of=ctx.as_of)
        if not histories:
            return []
        rfm = analytics.compute_rfm(ctx)

        found: list[DormancyCandidate] = []
        for customer_id, history in histories.items():
            if history.visit_count < MIN_VISITS_FOR_CADENCE:
                continue
            gaps = history.gaps_days()
            if not gaps:
                continue
            median_gap = median(gaps)
            gap_iqr = iqr(gaps)
            threshold = max(
                median_gap + CADENCE_IQR_MULTIPLIER * gap_iqr,
                float(ABSOLUTE_DORMANCY_FLOOR_DAYS),
            )
            days_since = history.days_since_last(today)
            if days_since <= threshold:
                continue

            score = rfm.get(customer_id)
            segment = score.segment if score else CustomerSegment.OCCASIONAL
            prior = RETURN_PRIOR_BY_SEGMENT.get(segment, DEFAULT_RETURN_PRIOR)
            expected_visits = clamp(
                safe_div(WINBACK_WINDOW_DAYS, max(median_gap, 1.0), 1.0), 1.0, 4.0
            )
            recoverable = int(round(prior * history.avg_ticket_paise * expected_visits))
            found.append(
                DormancyCandidate(
                    customer_id=customer_id,
                    name=history.name,
                    segment=segment,
                    visits=history.visit_count,
                    median_gap_days=round(median_gap, 2),
                    gap_iqr_days=round(gap_iqr, 2),
                    threshold_days=round(threshold, 2),
                    days_since_last=days_since,
                    last_visit=history.last_visit.isoformat() if history.last_visit else None,
                    avg_ticket_paise=history.avg_ticket_paise,
                    return_prior=prior,
                    expected_visits=round(expected_visits, 3),
                    recoverable_paise=recoverable,
                )
            )
        found.sort(key=lambda item: (-item.recoverable_paise, item.customer_id))
        return found

    def run(self, ctx: InsightContext) -> list[InsightDraft]:
        everyone = self.candidates(ctx)
        if not everyone:
            return []
        targets = everyone[: self.max_targets]
        impact = sum(candidate.recoverable_paise for candidate in targets)
        if impact <= 0:
            return []

        lapsed_value = sum(candidate.avg_ticket_paise for candidate in targets)
        median_overdue = median([candidate.overdue_ratio for candidate in targets])
        discount = (
            DEEP_DISCOUNT_PCT
            if median_overdue >= DEEP_DISCOUNT_OVERDUE_RATIO
            else BASE_DISCOUNT_PCT
        )

        if impact >= 500_000:
            severity = Severity.HIGH
        elif impact >= 200_000:
            severity = Severity.MEDIUM
        else:
            severity = Severity.LOW

        # More visit history means a better-estimated cadence, so a cohort of well-known
        # customers earns more confidence than one built from three-visit minimums.
        rich = sum(1 for candidate in targets if candidate.visits >= 5)
        confidence = round(clamp(0.6 + 0.25 * safe_div(rich, len(targets), 0.0), 0.55, 0.85), 3)

        top = targets[0]
        names = ", ".join(candidate.name for candidate in targets[:3])

        return [
            InsightDraft(
                kind=self.kind,
                severity=severity,
                title_en=f"{len(targets)} regulars have stopped coming",
                title_hi=f"{len(targets)} पुराने ग्राहक आना बंद कर चुके हैं",
                body_en=(
                    f"{names} and {max(0, len(targets) - 3)} others are past their own usual gap "
                    f"— {top.name} normally returns every {top.median_gap_days:.0f} days and it "
                    f"has been {top.days_since_last}. Their combined basket is "
                    f"{_inr(lapsed_value)} a visit; at segment win-back rates that is about "
                    f"{_inr(impact)} recoverable in {WINBACK_WINDOW_DAYS} days."
                ),
                body_hi=(
                    f"{names} और {max(0, len(targets) - 3)} और ग्राहक अपनी आम आदत से देर कर चुके "
                    f"हैं — {top.name} हर {top.median_gap_days:.0f} दिन में आते थे, अब "
                    f"{top.days_since_last} दिन हो गए। इनका औसत बिल {_inr(lapsed_value)} है; "
                    f"ऑफ़र भेजें तो लगभग {_inr(impact)} वापस आ सकता है।"
                ),
                metrics={
                    "dormant_count": len(everyone),
                    "targeted_count": len(targets),
                    "recoverable_paise": impact,
                    "window_days": WINBACK_WINDOW_DAYS,
                    "median_overdue_ratio": round(median_overdue, 2),
                    "rule": (
                        "days_since_last > max(median_gap + "
                        f"{CADENCE_IQR_MULTIPLIER} * IQR, {ABSOLUTE_DORMANCY_FLOOR_DAYS}d)"
                    ),
                    "customers": [
                        {
                            "customer_id": candidate.customer_id,
                            "name": candidate.name,
                            "segment": candidate.segment.value,
                            "visits": candidate.visits,
                            "median_gap_days": candidate.median_gap_days,
                            "gap_iqr_days": candidate.gap_iqr_days,
                            "threshold_days": candidate.threshold_days,
                            "days_since_last": candidate.days_since_last,
                            "last_visit": candidate.last_visit,
                            "avg_ticket_paise": candidate.avg_ticket_paise,
                            "return_prior": candidate.return_prior,
                            "expected_visits": candidate.expected_visits,
                            "recoverable_paise": candidate.recoverable_paise,
                        }
                        for candidate in targets
                    ],
                },
                suggested_tool="send_winback_offer",
                suggested_params={
                    "customer_ids": [candidate.customer_id for candidate in targets],
                    "discount_pct": discount,
                    "valid_days": OFFER_VALID_DAYS,
                    "message_en": (
                        f"We have missed you! {discount}% off your next visit, "
                        f"valid {OFFER_VALID_DAYS} days."
                    ),
                    "message_hi": (
                        f"आपको बहुत दिनों से नहीं देखा! अगली ख़रीद पर {discount}% छूट, "
                        f"{OFFER_VALID_DAYS} दिन के लिए।"
                    ),
                },
                impact_paise=impact,
                confidence=confidence,
                dedupe_key="dormant_customers",
                expires_in_days=3,
            )
        ]


class NewCustomerDropEngine:
    """Is the shop still pulling in first-time buyers?

    Existing customers mask acquisition problems for months: revenue holds up while the top of
    the funnel dries out. Eight complete 7-day windows of first-ever purchases, a least-squares
    slope over them, and a confirmation that the recent half really is below the earlier half —
    the slope alone would fire on a single noisy week.
    """

    kind = InsightKind.NEW_CUSTOMER_DROP

    def __init__(self, *, weeks: int = NEW_CUSTOMER_WEEKS) -> None:
        self.weeks = weeks

    def run(self, ctx: InsightContext) -> list[InsightDraft]:
        counts = analytics.new_customers_per_week(
            ctx.session, ctx.merchant_id, as_of=ctx.as_of, weeks=self.weeks
        )
        if len(counts) < 4:
            return []
        half = len(counts) // 2
        early, late = counts[:half], counts[half:]
        early_mean = safe_div(sum(early), len(early), 0.0)
        late_mean = safe_div(sum(late), len(late), 0.0)
        if sum(early) < MIN_NEW_CUSTOMERS_EARLY:
            return []

        slope = trend_slope([float(value) for value in counts])
        drop_pct = safe_div(early_mean - late_mean, early_mean, 0.0) * 100.0
        if slope > NEW_CUSTOMER_SLOPE_FLAG or drop_pct < NEW_CUSTOMER_DROP_PCT:
            return []

        # What the shortfall is worth: the new customers not acquired over the next four weeks,
        # valued at what a first-time buyer has historically spent on that first visit.
        shortfall_per_week = max(0.0, early_mean - late_mean)
        first_ticket = self._avg_first_ticket(ctx)
        impact = int(round(shortfall_per_week * 4 * first_ticket))

        if slope <= -2.0:
            severity = Severity.HIGH
        elif slope <= -1.0:
            severity = Severity.MEDIUM
        else:
            severity = Severity.LOW
        confidence = round(clamp(0.5 + 0.04 * sum(counts), 0.5, 0.8), 3)

        return [
            InsightDraft(
                kind=self.kind,
                severity=severity,
                title_en=f"New customers down {drop_pct:.0f}% over {self.weeks} weeks",
                title_hi=f"{self.weeks} हफ़्तों में नए ग्राहक {drop_pct:.0f}% घटे हैं",
                body_en=(
                    f"First-time buyers per week went {counts} — from {early_mean:.1f} to "
                    f"{late_mean:.1f} on average, a trend of {slope:+.2f} per week. Regulars are "
                    f"holding the takings up, but at this rate roughly {_inr(impact)} of new "
                    "business goes unwritten over the next month."
                ),
                body_hi=(
                    f"हर हफ़्ते नए ग्राहक {counts} रहे — औसत {early_mean:.1f} से "
                    f"{late_mean:.1f} पर आ गया ({slope:+.2f} प्रति हफ़्ता)। पुराने ग्राहक कमाई "
                    f"संभाल रहे हैं, पर इस रफ़्तार से अगले महीने लगभग {_inr(impact)} का नया "
                    "कारोबार छूट जाएगा।"
                ),
                metrics={
                    "weeks": self.weeks,
                    "weekly_counts": counts,
                    "early_mean": round(early_mean, 3),
                    "late_mean": round(late_mean, 3),
                    "drop_pct": round(drop_pct, 2),
                    "trend_slope_per_week": round(slope, 3),
                    "avg_first_ticket_paise": first_ticket,
                    "shortfall_per_week": round(shortfall_per_week, 3),
                    "projected_lost_paise": impact,
                },
                suggested_tool="save_merchant_note",
                suggested_params={
                    "text_en": (
                        f"New customers {drop_pct:.0f}% down over {self.weeks} weeks "
                        f"({slope:+.2f}/week). Try a first-visit offer or a local push."
                    ),
                    "text_hi": (
                        f"{self.weeks} हफ़्तों में नए ग्राहक {drop_pct:.0f}% घटे। "
                        "पहली ख़रीद पर छूट या मोहल्ले में प्रचार सोचिए।"
                    ),
                    "tags": ["acquisition"],
                },
                impact_paise=impact,
                confidence=confidence,
                dedupe_key="new_customer_drop",
                expires_in_days=7,
            )
        ]

    def _avg_first_ticket(self, ctx: InsightContext) -> int:
        """Mean basket of a customer's first recorded purchase, in paise."""
        histories = analytics.visit_histories(ctx.session, ctx.merchant_id, as_of=ctx.as_of)
        firsts = [
            history.amounts_paise[0] for history in histories.values() if history.amounts_paise
        ]
        if not firsts:
            return 0
        return int(round(sum(firsts) / len(firsts)))
