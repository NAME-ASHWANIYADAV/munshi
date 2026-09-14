"""Sales engines: collection anomaly, peak hour, payment mix, margin leak.

The collection anomaly is the opening line of the demo, so it carries the most statistical care:
a robust same-weekday baseline, a partial-day projection with an empirical band, and an honest
refusal to project when the day is too young for the arithmetic to mean anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from munshiji.clock import ist_date_of, to_ist, to_utc, weekday_name
from munshiji.db.enums import InsightKind, PaymentMethod, Severity
from munshiji.insights.base import InsightContext, InsightDraft
from munshiji.insights.stats import (
    chi2_sf,
    chi_square,
    clamp,
    mad_is_degenerate,
    robust_z,
    safe_div,
)
from munshiji.logging import get_logger
from munshiji.money import fmt_inr, fmt_pct, pct_change
from munshiji.repositories import analytics

__all__ = [
    "BASELINE_WEEKS",
    "MIN_BASELINE_SAMPLES",
    "Z_CRITICAL",
    "Z_MEDIUM",
    "CollectionAnomalyEngine",
    "MarginLeakEngine",
    "PaymentMixEngine",
    "PeakHourEngine",
    "hour_label_en",
    "hour_label_hi",
    "severity_from_z",
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


# ── Tuning constants (SPEC.md §7) ───────────────────────────────────────────

#: Eight same-weekday observations ≈ two months — long enough for a stable median, short enough
#: that a genuine seasonal shift is not averaged away.
BASELINE_WEEKS = 8

#: Below this many samples the median/MAD pair is not trustworthy: we still report, but with a
#: reduced confidence and capped at MEDIUM severity.
MIN_BASELINE_SAMPLES = 4

Z_CRITICAL = 3.0
Z_MEDIUM = 1.5

#: Elapsed-share threshold under which a projection is refused outright.
MIN_PROJECTION_SHARE = analytics.MIN_PROJECTION_SHARE

#: Below this elapsed share the projection is allowed but the confidence is discounted.
SHAKY_PROJECTION_SHARE = 0.25

#: Trailing complete days used by the peak-hour histogram.
PEAK_WINDOW_DAYS = 30
PEAK_WINDOW_HOURS = 2

#: Payment-mix comparison windows: recent 14 days against the prior 28.
MIX_RECENT_DAYS = 14
MIX_PRIOR_DAYS = 28
MIX_DRIFT_PP = 8.0
MIX_STRONG_DRIFT_PP = 15.0
MIX_MIN_TXNS = 30

#: Margin comparison windows: recent 30 days against the prior 60.
MARGIN_RECENT_DAYS = 30
MARGIN_PRIOR_DAYS = 60
MARGIN_DROP_PP = 2.0
MIN_CATEGORY_REVENUE_PAISE = 200_000  # ₹2,000 in the recent window
MIN_CATEGORY_LINES = 10
MAX_MARGIN_DRAFTS = 3


def severity_from_z(z: float, *, sample_count: int) -> Severity:
    """Map a robust z to a severity.

    ``|z| >= 3`` is a three-sigma event against a *robust* scale — for a shop that is a genuine
    emergency when money is missing (CRITICAL) and a notable opportunity when it is a surge
    (HIGH). ``|z| >= 1.5`` is worth mentioning (MEDIUM). Everything else is a normal day (INFO),
    which we still publish so the voice layer always has today's numbers to speak.

    A thin baseline can never produce more than MEDIUM, however extreme the z looks.
    """
    magnitude = abs(z)
    if sample_count < MIN_BASELINE_SAMPLES:
        return Severity.MEDIUM if magnitude >= Z_MEDIUM else Severity.INFO
    if magnitude >= Z_CRITICAL:
        return Severity.CRITICAL if z < 0 else Severity.HIGH
    if magnitude >= Z_MEDIUM:
        return Severity.MEDIUM
    return Severity.INFO


def baseline_confidence(sample_count: int, *, degenerate_mad: bool) -> float:
    """Confidence in a weekday baseline, from how much history backs it.

    Eight clean samples earn 0.90; four earn 0.72; below the minimum we drop to 0.50 because the
    median of three numbers is barely a median. A degenerate MAD costs a further 25%, since the
    denominator of the z-score is then an assumption (see :func:`munshiji.insights.stats.robust_z`).
    """
    if sample_count >= 8:
        confidence = 0.90
    elif sample_count >= 6:
        confidence = 0.85
    elif sample_count >= 5:
        confidence = 0.80
    elif sample_count >= MIN_BASELINE_SAMPLES:
        confidence = 0.72
    else:
        confidence = 0.50
    if degenerate_mad:
        confidence *= 0.75
    return round(clamp(confidence, 0.2, 0.95), 3)


_HI_DAYPART = ((4, "रात"), (12, "सुबह"), (16, "दोपहर"), (20, "शाम"), (24, "रात"))


def hour_label_en(hour: int) -> str:
    """``18`` -> ``'6 PM'``."""
    hour = int(hour) % 24
    suffix = "AM" if hour < 12 else "PM"
    display = hour % 12 or 12
    return f"{display} {suffix}"


def hour_label_hi(hour: int) -> str:
    """``18`` -> ``'शाम 6 बजे'`` — the daypart merchants actually say."""
    hour = int(hour) % 24
    daypart = next(name for bound, name in _HI_DAYPART if hour < bound)
    display = hour % 12 or 12
    return f"{daypart} {display} बजे"


@dataclass(slots=True)
class _DayVerdict:
    """A single day scored against its own weekday baseline."""

    day_label: str
    actual_paise: int
    baseline_paise: float
    z: float
    delta_pct: float | None


# ─────────────────────────────────────────────────────────────────────────────
# Collection anomaly
# ─────────────────────────────────────────────────────────────────────────────


class CollectionAnomalyEngine:
    """Is today's money tracking where this weekday normally lands?

    Emits up to two findings:

    * ``collection_anomaly:today`` — always, when a baseline exists. Carries the live projection
      so the dashboard and the voice layer can open with a number even on a normal day.
    * ``collection_anomaly:week`` — only when the trailing seven days *together* drift. Each day
      is z-scored against its own weekday baseline and the scores are combined Stouffer-style
      (``sum(z)/sqrt(n)``); a soft 8% dip that no single day would flag is unmistakable across
      seven of them, which is exactly the shape of a real slow patch.
    """

    kind = InsightKind.COLLECTION_ANOMALY

    def __init__(self, *, weeks: int = BASELINE_WEEKS) -> None:
        self.weeks = weeks

    # -- today ---------------------------------------------------------------

    def _today_draft(self, ctx: InsightContext) -> InsightDraft | None:
        session, merchant_id, as_of = ctx.session, ctx.merchant_id, ctx.as_of
        today = ist_date_of(as_of)
        baseline = analytics.weekday_baseline(session, merchant_id, as_of=as_of, weeks=self.weeks)
        if baseline.sample_count == 0 or baseline.median_paise <= 0:
            # No history, or this weekday has never taken money (shop shut). Either way there is
            # no baseline to be "15% below", so say nothing rather than divide by zero.
            return None

        # Bounds must be UTC: SQLite stores naive UTC strings, so an IST-aware bound would be
        # bound as an IST wall clock and silently shift the window by 5h30.
        start, _ = analytics.day_window(as_of)
        collected = analytics.revenue_between(session, merchant_id, start, to_utc(as_of))
        profile = analytics.intraday_profile(session, merchant_id, as_of=as_of, weeks=self.weeks)
        projection = analytics.project_close(profile, collected, at=as_of)

        degenerate = mad_is_degenerate(baseline.mad_paise)
        confidence = baseline_confidence(baseline.sample_count, degenerate_mad=degenerate)

        median_paise = baseline.median_paise
        if projection.too_early or projection.projected_paise is None:
            z = None
            delta = None
            severity = Severity.INFO
            confidence = round(min(confidence, 0.35), 3)
            impact = 0
        else:
            z = robust_z(projection.projected_paise, median_paise, baseline.mad_paise)
            delta = pct_change(projection.projected_paise, median_paise)
            severity = severity_from_z(z, sample_count=baseline.sample_count)
            if projection.elapsed_share < SHAKY_PROJECTION_SHARE:
                confidence = round(confidence * 0.8, 3)
            impact = int(abs(projection.projected_paise - median_paise))

        weekday_en = weekday_name(today)
        weekday_hi = weekday_name(today, hindi=True)
        clock_en = hour_label_en(to_ist(as_of).hour)
        clock_hi = hour_label_hi(to_ist(as_of).hour)

        metrics = {
            "as_of": to_ist(as_of).isoformat(timespec="minutes"),
            "day": today.isoformat(),
            "weekday": weekday_en,
            "weekday_hi": weekday_hi,
            "collected_so_far_paise": projection.collected_paise,
            "projected_close_paise": projection.projected_paise,
            "baseline_paise": int(round(median_paise)),
            "baseline_mad_paise": int(round(baseline.mad_paise)),
            "baseline_samples": baseline.sample_count,
            "baseline_weeks": self.weeks,
            "delta_paise": (
                None
                if projection.projected_paise is None
                else int(projection.projected_paise - round(median_paise))
            ),
            "delta_pct": None if delta is None else round(delta, 2),
            "robust_z": None if z is None else round(z, 3),
            "band_low_paise": projection.band_low_paise,
            "band_high_paise": projection.band_high_paise,
            "hours_elapsed_share": projection.elapsed_share,
            "projection_sample_days": projection.sample_days,
            "too_early": projection.too_early,
            "mad_degenerate": degenerate,
            "intraday_cum_share": profile.cum_by_hour(),
            "baseline_days": [
                {"day": day.day.isoformat(), "revenue_paise": day.revenue_paise}
                for day in baseline.days
            ],
        }

        if projection.too_early:
            title_en = f"Too early to project {weekday_en}'s close"
            title_hi = f"{weekday_hi} का अनुमान लगाने के लिए अभी बहुत जल्दी है"
            body_en = (
                f"Only {_inr(collected)} is in so far ({projection.elapsed_share * 100:.0f}% "
                f"of a typical {weekday_en}). Baseline close is {_inr(int(median_paise))}. "
                "Ask me again after the morning rush."
            )
            body_hi = (
                f"अभी तक सिर्फ़ {_inr(collected)} आए हैं — आम {weekday_hi} का लगभग "
                f"{projection.elapsed_share * 100:.0f}%। बेसलाइन {_inr(int(median_paise))} है। "
                "सुबह की भीड़ के बाद दोबारा पूछिए।"
            )
        elif severity in (Severity.CRITICAL, Severity.MEDIUM) and (delta or 0) < 0:
            # The words carry the direction ("below" / "कम"), so the number is unsigned —
            # "tracking -44% below baseline" reads as a double negative.
            gap = fmt_pct(abs(delta or 0.0), signed=False, digits=0)
            title_en = f"{weekday_en} collection tracking {gap} below baseline"
            title_hi = f"{weekday_hi} की कलेक्शन बेसलाइन से {gap} कम चल रही है"
            body_en = (
                f"By {clock_en} you have {_inr(collected)}. At this pace today closes near "
                f"{_inr(projection.projected_paise or 0)} "
                f"({_inr(projection.band_low_paise or 0)}–"
                f"{_inr(projection.band_high_paise or 0)}), against a "
                f"{baseline.sample_count}-{weekday_en} median of "
                f"{_inr(int(median_paise))}. Robust z {z:+.1f}."
            )
            body_hi = (
                f"{clock_hi} तक {_inr(collected)} आए हैं। इसी रफ़्तार से आज लगभग "
                f"{_inr(projection.projected_paise or 0)} पर बंद होगा — पिछले "
                f"{baseline.sample_count} {weekday_hi} का median {_inr(int(median_paise))} था। "
                f"यानी करीब {_inr(impact)} कम।"
            )
        elif severity in (Severity.HIGH, Severity.MEDIUM) and (delta or 0) > 0:
            title_en = f"{weekday_en} running {fmt_pct(delta, digits=0)} ahead of baseline"
            title_hi = f"{weekday_hi} बेसलाइन से {fmt_pct(delta, digits=0)} ऊपर चल रहा है"
            body_en = (
                f"{_inr(collected)} by {clock_en} projects to "
                f"{_inr(projection.projected_paise or 0)} against a median "
                f"{_inr(int(median_paise))}. Keep the fast movers stocked."
            )
            body_hi = (
                f"{clock_hi} तक {_inr(collected)} — अनुमान "
                f"{_inr(projection.projected_paise or 0)}, median "
                f"{_inr(int(median_paise))}। तेज़ बिकने वाला माल भरा रखिए।"
            )
        else:
            title_en = f"{weekday_en} on track at {_inr(projection.projected_paise or 0)}"
            title_hi = (
                f"{weekday_hi} ठीक चल रहा है — अनुमान " f"{_inr(projection.projected_paise or 0)}"
            )
            body_en = (
                f"{_inr(collected)} banked by {clock_en}; projected close "
                f"{_inr(projection.projected_paise or 0)} against a median "
                f"{_inr(int(median_paise))} ({fmt_pct(delta)})."
            )
            body_hi = (
                f"{clock_hi} तक {_inr(collected)}; अनुमानित बंदी "
                f"{_inr(projection.projected_paise or 0)}, median "
                f"{_inr(int(median_paise))} ({fmt_pct(delta)})।"
            )

        if (delta or 0) < 0 and severity is not Severity.INFO:
            tool = "schedule_followup"
            params = {
                "topic": "collection_check",
                "at_hour": 21,
                "note_en": f"Re-check {weekday_en} close against {_inr(int(median_paise))}.",
                "note_hi": f"{weekday_hi} की बंदी {_inr(int(median_paise))} से मिलाकर देखें।",
            }
        else:
            tool = "save_merchant_note"
            params = {
                "text_en": title_en,
                "text_hi": title_hi,
                "tags": ["collection", today.isoformat()],
            }

        return InsightDraft(
            kind=self.kind,
            severity=severity,
            title_en=title_en,
            title_hi=title_hi,
            body_en=body_en,
            body_hi=body_hi,
            metrics=metrics,
            suggested_tool=tool,
            suggested_params=params,
            impact_paise=impact,
            confidence=confidence,
            dedupe_key="collection_anomaly:today",
            expires_in_days=1,
        )

    # -- trailing week -------------------------------------------------------

    def _week_draft(self, ctx: InsightContext) -> InsightDraft | None:
        session, merchant_id, as_of = ctx.session, ctx.merchant_id, ctx.as_of
        today = ist_date_of(as_of)
        verdicts: list[_DayVerdict] = []
        actual_total = 0
        baseline_total = 0.0
        thin = False

        for offset in range(1, 8):
            day = today - timedelta(days=offset)
            anchor = to_ist(as_of) - timedelta(days=offset)
            baseline = analytics.weekday_baseline(
                session, merchant_id, as_of=anchor, weeks=self.weeks
            )
            if baseline.sample_count == 0 or baseline.median_paise <= 0:
                continue
            if baseline.sample_count < MIN_BASELINE_SAMPLES:
                thin = True
            frame = analytics.daily_revenue(session, merchant_id, day, day)
            if day not in frame:
                continue
            actual = frame[day].revenue_paise
            z = robust_z(actual, baseline.median_paise, baseline.mad_paise)
            verdicts.append(
                _DayVerdict(
                    day_label=day.isoformat(),
                    actual_paise=actual,
                    baseline_paise=baseline.median_paise,
                    z=z,
                    delta_pct=pct_change(actual, baseline.median_paise),
                )
            )
            actual_total += actual
            baseline_total += baseline.median_paise

        if len(verdicts) < 5:
            return None

        mean_z = sum(verdict.z for verdict in verdicts) / len(verdicts)
        stouffer = sum(verdict.z for verdict in verdicts) / (len(verdicts) ** 0.5)
        if abs(mean_z) < Z_MEDIUM / 2:
            return None

        delta = pct_change(actual_total, baseline_total)
        severity = severity_from_z(
            mean_z, sample_count=MIN_BASELINE_SAMPLES - 1 if thin else BASELINE_WEEKS
        )
        if severity is Severity.INFO:
            return None

        impact = int(abs(actual_total - baseline_total))
        direction_en = "below" if mean_z < 0 else "above"
        direction_hi = "कम" if mean_z < 0 else "ज़्यादा"
        gap = fmt_pct(abs(delta or 0.0), signed=False, digits=0)
        raw_confidence = clamp(0.55 + 0.05 * len(verdicts), 0.5, 0.88)
        confidence = round(raw_confidence * (0.8 if thin else 1.0), 3)

        return InsightDraft(
            kind=self.kind,
            severity=severity,
            title_en=f"Last 7 days are {gap} {direction_en} the weekday baseline",
            title_hi=f"पिछले 7 दिन बेसलाइन से {gap} {direction_hi} रहे",
            body_en=(
                f"{_inr(actual_total)} collected against an expected "
                f"{_inr(int(baseline_total))} — a gap of {_inr(impact)}. Each day was "
                f"compared with its own weekday median; the average robust z is {mean_z:+.2f} "
                f"(combined {stouffer:+.2f} over {len(verdicts)} days), so this is a pattern, "
                "not one bad day."
            ),
            body_hi=(
                f"पिछले हफ़्ते {_inr(actual_total)} आए, उम्मीद थी "
                f"{_inr(int(baseline_total))} — {_inr(impact)} का फ़र्क़। हर दिन उसी वार के "
                f"median से तुलना की गई; औसत robust z {mean_z:+.2f} है, यानी यह एक दिन की बात "
                "नहीं, पैटर्न है।"
            ),
            metrics={
                "window_days": len(verdicts),
                "actual_paise": actual_total,
                "baseline_paise": int(round(baseline_total)),
                "delta_paise": int(actual_total - baseline_total),
                "delta_pct": None if delta is None else round(delta, 2),
                "robust_z": round(mean_z, 3),
                "stouffer_z": round(stouffer, 3),
                "thin_baseline": thin,
                "days": [
                    {
                        "day": verdict.day_label,
                        "actual_paise": verdict.actual_paise,
                        "baseline_paise": int(round(verdict.baseline_paise)),
                        "robust_z": round(verdict.z, 3),
                        "delta_pct": (
                            None if verdict.delta_pct is None else round(verdict.delta_pct, 2)
                        ),
                    }
                    for verdict in verdicts
                ],
            },
            suggested_tool="save_merchant_note",
            suggested_params={
                "text_en": (
                    f"7-day collection {direction_en} baseline by {_inr(impact)} "
                    f"(mean robust z {mean_z:+.2f})."
                ),
                "text_hi": (
                    f"7 दिन की कलेक्शन बेसलाइन से {_inr(impact)} {direction_hi} "
                    f"(औसत robust z {mean_z:+.2f})।"
                ),
                "tags": ["collection", "weekly"],
            },
            impact_paise=impact,
            confidence=confidence,
            dedupe_key="collection_anomaly:week",
            expires_in_days=2,
        )

    def run(self, ctx: InsightContext) -> list[InsightDraft]:
        drafts: list[InsightDraft] = []
        today = self._today_draft(ctx)
        if today is not None:
            drafts.append(today)
        week = self._week_draft(ctx)
        if week is not None:
            drafts.append(week)
        return drafts


# ─────────────────────────────────────────────────────────────────────────────
# Peak hour
# ─────────────────────────────────────────────────────────────────────────────


class PeakHourEngine:
    """Where the day's money actually arrives — a staffing and stocking fact.

    Reports the best two **non-overlapping** two-hour windows, because a kirana's day is bimodal
    (morning ration run, evening after-work rush) and a single peak would hide half the story.
    """

    kind = InsightKind.PEAK_HOUR

    def __init__(self, *, days: int = PEAK_WINDOW_DAYS, width: int = PEAK_WINDOW_HOURS) -> None:
        self.days = days
        self.width = width

    def run(self, ctx: InsightContext) -> list[InsightDraft]:
        histogram = analytics.hour_histogram(
            ctx.session, ctx.merchant_id, as_of=ctx.as_of, days=self.days
        )
        total = sum(histogram.values())
        if total <= 0:
            return []

        windows = [
            (start, sum(histogram[hour] for hour in range(start, start + self.width)))
            for start in range(0, 24 - self.width + 1)
        ]
        windows.sort(key=lambda item: (-item[1], item[0]))
        best = windows[0]
        runner = next(
            (item for item in windows[1:] if abs(item[0] - best[0]) >= self.width),
            None,
        )
        chosen = [best] + ([runner] if runner else [])
        combined = sum(revenue for _, revenue in chosen)
        combined_share = safe_div(combined, total, 0.0) * 100.0
        daily_peak = int(round(safe_div(combined, self.days, 0.0)))

        def describe(window: tuple[int, int]) -> dict[str, object]:
            start, revenue = window
            return {
                "start_hour": start,
                "end_hour": start + self.width,
                "label_en": f"{hour_label_en(start)}–{hour_label_en(start + self.width)}",
                "label_hi": f"{hour_label_hi(start)} से {hour_label_hi(start + self.width)}",
                "revenue_paise": revenue,
                "share_pct": round(safe_div(revenue, total, 0.0) * 100.0, 2),
            }

        described = [describe(window) for window in chosen]
        labels_en = " and ".join(str(item["label_en"]) for item in described)
        labels_hi = " और ".join(str(item["label_hi"]) for item in described)

        return [
            InsightDraft(
                kind=self.kind,
                severity=Severity.INFO,
                title_en=f"{combined_share:.0f}% of takings arrive in {labels_en}",
                title_hi=f"{combined_share:.0f}% कमाई {labels_hi} के बीच आती है",
                body_en=(
                    f"Over the last {self.days} days, {labels_en} carried {_inr(combined)} of "
                    f"{_inr(total)} — about {_inr(daily_peak)} a day. Staff and restock "
                    "before these windows, not during them."
                ),
                body_hi=(
                    f"पिछले {self.days} दिनों में {labels_hi} के बीच {_inr(combined)} आए, कुल "
                    f"{_inr(total)} में से — रोज़ लगभग {_inr(daily_peak)}। भीड़ से पहले माल "
                    "और स्टाफ़ तैयार रखिए।"
                ),
                metrics={
                    "window_days": self.days,
                    "window_width_hours": self.width,
                    "total_revenue_paise": total,
                    "peak_windows": described,
                    "combined_share_pct": round(combined_share, 2),
                    "peak_daily_revenue_paise": daily_peak,
                    "hour_histogram": {str(hour): value for hour, value in histogram.items()},
                },
                suggested_tool="save_merchant_note",
                suggested_params={
                    "text_en": f"Peak windows: {labels_en} ({combined_share:.0f}% of revenue).",
                    "text_hi": f"सबसे व्यस्त समय: {labels_hi} ({combined_share:.0f}% कमाई)।",
                    "tags": ["peak_hour"],
                },
                impact_paise=daily_peak,
                confidence=0.9,
                dedupe_key="peak_hour",
                expires_in_days=7,
            )
        ]


# ─────────────────────────────────────────────────────────────────────────────
# Payment mix
# ─────────────────────────────────────────────────────────────────────────────


class PaymentMixEngine:
    """Has the way customers pay shifted?

    A move from UPI back to cash costs the merchant a digital trail (and the credit history that
    comes with it); a move *to* UPI means the soundbox is earning its keep. Drift is measured in
    percentage points of revenue share, and significance with a Pearson chi-square over
    transaction *counts* (shares are not counts — the test needs the latter).
    """

    kind = InsightKind.PAYMENT_MIX

    def __init__(self, *, recent_days: int = MIX_RECENT_DAYS, prior_days: int = MIX_PRIOR_DAYS):
        self.recent_days = recent_days
        self.prior_days = prior_days

    def run(self, ctx: InsightContext) -> list[InsightDraft]:
        today = ist_date_of(ctx.as_of)
        recent_last = today - timedelta(days=1)
        recent_first = recent_last - timedelta(days=self.recent_days - 1)
        prior_last = recent_first - timedelta(days=1)
        prior_first = prior_last - timedelta(days=self.prior_days - 1)

        recent = analytics.payment_mix(
            ctx.session, ctx.merchant_id, first_day=recent_first, last_day=recent_last
        )
        prior = analytics.payment_mix(
            ctx.session, ctx.merchant_id, first_day=prior_first, last_day=prior_last
        )
        if recent.total_count < MIX_MIN_TXNS or prior.total_count < MIX_MIN_TXNS:
            return []

        methods = [method.value for method in PaymentMethod]
        shifts = []
        for method in methods:
            recent_share = recent.revenue_share(method) * 100.0
            prior_share = prior.revenue_share(method) * 100.0
            shifts.append(
                {
                    "method": method,
                    "recent_share_pct": round(recent_share, 2),
                    "prior_share_pct": round(prior_share, 2),
                    "delta_pp": round(recent_share - prior_share, 2),
                    "recent_revenue_paise": recent.revenue_paise.get(method, 0),
                }
            )
        biggest = max(shifts, key=lambda item: abs(float(item["delta_pp"])))
        drift = abs(float(biggest["delta_pp"]))
        if drift < MIX_DRIFT_PP:
            return []

        observed = [recent.counts.get(method, 0) for method in methods]
        expected = [
            recent.total_count * safe_div(prior.counts.get(method, 0), prior.total_count, 0.0)
            for method in methods
        ]
        active = sum(1 for value in expected if value > 0)
        dof = max(1, active - 1)
        statistic = chi_square(observed, expected)
        p_value = chi2_sf(statistic, dof)
        if p_value >= 0.20:
            return []

        if p_value >= 0.05:
            severity = Severity.LOW
            confidence = 0.55
        elif drift >= MIX_STRONG_DRIFT_PP:
            severity = Severity.HIGH
            confidence = 0.85
        else:
            severity = Severity.MEDIUM
            confidence = 0.8

        method = str(biggest["method"])
        delta_pp = float(biggest["delta_pp"])
        moved_paise = int(round(abs(delta_pp) / 100.0 * recent.total_paise))
        direction_en = "up" if delta_pp > 0 else "down"
        direction_hi = "बढ़ा" if delta_pp > 0 else "घटा"
        method_hi = {
            "upi": "UPI",
            "cash": "नकद",
            "card": "कार्ड",
            "wallet": "वॉलेट",
            "soundbox": "साउंडबॉक्स",
        }.get(method, method)

        return [
            InsightDraft(
                kind=self.kind,
                severity=severity,
                title_en=(
                    f"{method.upper()} share is {direction_en} {abs(delta_pp):.0f} points "
                    f"in {self.recent_days} days"
                ),
                title_hi=(
                    f"{self.recent_days} दिनों में {method_hi} का हिस्सा "
                    f"{abs(delta_pp):.0f} अंक {direction_hi} है"
                ),
                body_en=(
                    f"{method.upper()} moved from {biggest['prior_share_pct']}% to "
                    f"{biggest['recent_share_pct']}% of revenue — roughly {_inr(moved_paise)} "
                    f"changed hands differently. Chi-square {statistic:.1f} on {dof} df, "
                    f"p={p_value:.3f} against the prior {self.prior_days} days."
                ),
                body_hi=(
                    f"{method_hi} का हिस्सा {biggest['prior_share_pct']}% से "
                    f"{biggest['recent_share_pct']}% हो गया — करीब {_inr(moved_paise)} का "
                    f"फ़र्क़। पिछले {self.prior_days} दिनों से तुलना में p={p_value:.3f}।"
                ),
                metrics={
                    "recent_days": self.recent_days,
                    "prior_days": self.prior_days,
                    "recent_txns": recent.total_count,
                    "prior_txns": prior.total_count,
                    "recent_revenue_paise": recent.total_paise,
                    "shifts": shifts,
                    "biggest_shift": biggest,
                    "moved_paise": moved_paise,
                    "chi_square": round(statistic, 3),
                    "dof": dof,
                    # 8 places, not 5: a decisive result rounds to a bare 0.0 at 5 and stops
                    # looking like evidence.
                    "p_value": round(p_value, 8),
                },
                suggested_tool="save_merchant_note",
                suggested_params={
                    "text_en": (
                        f"{method.upper()} share {direction_en} {abs(delta_pp):.0f}pp "
                        f"(p={p_value:.3f})."
                    ),
                    "text_hi": f"{method_hi} का हिस्सा {abs(delta_pp):.0f} अंक {direction_hi}।",
                    "tags": ["payment_mix", method],
                },
                impact_paise=moved_paise,
                confidence=confidence,
                dedupe_key=f"payment_mix:{method}",
                expires_in_days=5,
            )
        ]


# ─────────────────────────────────────────────────────────────────────────────
# Margin leak
# ─────────────────────────────────────────────────────────────────────────────


class MarginLeakEngine:
    """Revenue can look fine while the money underneath it quietly thins.

    Realised gross margin per category, last 30 days against the prior 60, computed from the
    *line-level* price and cost actually recorded on each sale — so a supplier price rise the
    merchant absorbed shows up even though the shelf price never moved.
    """

    kind = InsightKind.MARGIN_LEAK

    def __init__(
        self,
        *,
        recent_days: int = MARGIN_RECENT_DAYS,
        prior_days: int = MARGIN_PRIOR_DAYS,
        drop_pp: float = MARGIN_DROP_PP,
    ) -> None:
        self.recent_days = recent_days
        self.prior_days = prior_days
        self.drop_pp = drop_pp

    def run(self, ctx: InsightContext) -> list[InsightDraft]:
        today = ist_date_of(ctx.as_of)
        recent_last = today - timedelta(days=1)
        recent_first = recent_last - timedelta(days=self.recent_days - 1)
        prior_last = recent_first - timedelta(days=1)
        prior_first = prior_last - timedelta(days=self.prior_days - 1)

        recent = analytics.category_margins(
            ctx.session, ctx.merchant_id, first_day=recent_first, last_day=recent_last
        )
        prior = analytics.category_margins(
            ctx.session, ctx.merchant_id, first_day=prior_first, last_day=prior_last
        )
        if not recent or not prior:
            return []

        candidates = []
        for category, frame in recent.items():
            reference = prior.get(category)
            if reference is None or reference.revenue_paise <= 0:
                continue
            if (
                frame.revenue_paise < MIN_CATEGORY_REVENUE_PAISE
                or frame.line_count < MIN_CATEGORY_LINES
            ):
                continue
            # Rounded before comparison: floating point puts an exact 6.00-point slip at
            # 5.999999999999998, which would silently demote its severity.
            drop = round(reference.margin_pct - frame.margin_pct, 6)
            if drop < self.drop_pp:
                continue
            impact = int(round(drop / 100.0 * frame.revenue_paise))
            candidates.append((drop, impact, category, frame, reference))

        candidates.sort(key=lambda item: -item[1])
        drafts: list[InsightDraft] = []
        for drop, impact, category, frame, reference in candidates[:MAX_MARGIN_DRAFTS]:
            if drop >= 6.0:
                severity = Severity.HIGH
            elif drop >= 4.0:
                severity = Severity.MEDIUM
            else:
                severity = Severity.LOW
            confidence = round(clamp(0.6 + 0.01 * min(frame.line_count, 30), 0.6, 0.9), 3)

            drafts.append(
                InsightDraft(
                    kind=self.kind,
                    severity=severity,
                    title_en=f"{category.title()} margin down {drop:.1f} points",
                    title_hi=f"{category} का मार्जिन {drop:.1f} अंक गिरा है",
                    body_en=(
                        f"{category.title()} earned {frame.margin_pct:.1f}% gross over the last "
                        f"{self.recent_days} days against {reference.margin_pct:.1f}% in the "
                        f"{self.prior_days} before it. On {_inr(frame.revenue_paise)} of "
                        f"recent sales that is {_inr(impact)} of margin gone — check the "
                        "purchase rate or raise the shelf price."
                    ),
                    body_hi=(
                        f"{category} पर पिछले {self.recent_days} दिन में मार्जिन "
                        f"{frame.margin_pct:.1f}% रहा, उससे पहले {self.prior_days} दिन में "
                        f"{reference.margin_pct:.1f}% था। {_inr(frame.revenue_paise)} की बिक्री "
                        f"पर करीब {_inr(impact)} का नुक़सान — ख़रीद रेट देखिए या दाम बढ़ाइए।"
                    ),
                    metrics={
                        "category": category,
                        "recent_days": self.recent_days,
                        "prior_days": self.prior_days,
                        "recent_margin_pct": round(frame.margin_pct, 2),
                        "prior_margin_pct": round(reference.margin_pct, 2),
                        "drop_pp": round(drop, 2),
                        "recent_revenue_paise": frame.revenue_paise,
                        "recent_cost_paise": frame.cost_paise,
                        "prior_revenue_paise": reference.revenue_paise,
                        "recent_units": frame.units,
                        "recent_lines": frame.line_count,
                        "margin_lost_paise": impact,
                    },
                    suggested_tool="save_merchant_note",
                    suggested_params={
                        "text_en": (
                            f"{category.title()} margin {frame.margin_pct:.1f}% "
                            f"(was {reference.margin_pct:.1f}%) — renegotiate or reprice."
                        ),
                        "text_hi": (
                            f"{category} मार्जिन {frame.margin_pct:.1f}% "
                            f"(पहले {reference.margin_pct:.1f}%) — रेट देखिए।"
                        ),
                        "tags": ["margin", category],
                    },
                    impact_paise=impact,
                    confidence=confidence,
                    dedupe_key=f"margin_leak:{category}",
                    expires_in_days=7,
                )
            )
        return drafts
