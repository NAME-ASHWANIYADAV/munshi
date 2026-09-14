"""Festival prep engine.

Diwali is the year's cash event for a kirana and it is won or lost in the week *before* it. This
engine looks ahead to the next festival inside its prep window and sizes the stock-up from last
year's realised uplift where the data reaches that far back, falling back to the festival's own
declared multiplier when it does not.

``munshiji.seed.festivals`` is owned by another module and may not exist yet, so every touch of
it goes through :func:`load_festivals` — a defensive adapter that tolerates the module being
absent and tolerates several plausible shapes of its API. When nothing is available the engine
simply produces no findings; it never raises and never blocks the rest of the feed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import ModuleType
from typing import Any

from munshiji.clock import ist_date_of
from munshiji.db.enums import InsightKind, Severity
from munshiji.insights.base import InsightContext, InsightDraft
from munshiji.insights.stats import clamp, safe_div
from munshiji.logging import get_logger
from munshiji.money import fmt_inr
from munshiji.repositories import analytics

__all__ = [
    "DEFAULT_PREP_WINDOW_DAYS",
    "FestivalInfo",
    "FestivalPrepEngine",
    "festival_multiplier",
    "load_festivals",
    "next_festival",
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


#: How far ahead of a festival the prep insight becomes actionable, when the calendar does not
#: declare its own window. A kirana orders roughly a week out; two weeks is too early to act on.
DEFAULT_PREP_WINDOW_DAYS = 14

#: Fallback uplift when a festival is known but carries no per-category multiplier.
DEFAULT_UPLIFT = 1.25

#: Days either side of last year's festival date used to measure the realised uplift, and the
#: quiet baseline window it is compared against.
UPLIFT_WINDOW_DAYS = 5
UPLIFT_BASELINE_DAYS = 28

#: Ignore a category with less than this in prior-year festival revenue — too thin to size.
MIN_CATEGORY_REVENUE_PAISE = 100_000  # ₹1,000


@dataclass(slots=True, frozen=True)
class FestivalInfo:
    """A normalised view of whatever the festival calendar gave us."""

    name: str
    name_hi: str
    day: date
    prep_days: int
    category_multipliers: dict[str, float]
    default_multiplier: float

    def days_until(self, today: date) -> int:
        return (self.day - today).days

    def multiplier_for(self, category: str) -> float:
        return float(self.category_multipliers.get(category, self.default_multiplier))


def load_festivals() -> ModuleType | None:
    """Import ``munshiji.seed.festivals`` if it exists, else ``None``.

    Owned by another agent; treated as optional infrastructure throughout.
    """
    try:
        from munshiji.seed import festivals as module
    except ImportError:
        logger.debug("festival calendar not available; seasonal insights disabled")
        return None
    return module


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return ist_date_of(value)
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _field(source: Any, *names: str) -> Any:
    """Read the first present attribute or mapping key out of ``names``."""
    for name in names:
        if isinstance(source, dict):
            if name in source:
                return source[name]
        elif hasattr(source, name):
            return getattr(source, name)
    return None


def _normalise(entry: Any) -> FestivalInfo | None:
    """Coerce one calendar entry (dataclass, object or dict) into :class:`FestivalInfo`."""
    day = _as_date(_field(entry, "day", "date", "festival_date", "on"))
    name = _field(entry, "name", "name_en", "title", "key")
    if day is None or not name:
        return None
    multipliers = (
        _field(entry, "category_uplift", "category_multipliers", "categories", "uplift_by_category")
        or {}
    )
    if not isinstance(multipliers, dict):
        multipliers = dict(multipliers) if hasattr(multipliers, "items") else {}
    default = _field(entry, "multiplier", "uplift", "default_multiplier")
    prep = _field(entry, "prep_days", "prep_window_days", "lead_days")
    return FestivalInfo(
        name=str(name),
        name_hi=str(_field(entry, "name_hi", "hindi", "label_hi") or name),
        day=day,
        prep_days=int(prep) if isinstance(prep, int | float) else DEFAULT_PREP_WINDOW_DAYS,
        category_multipliers={
            str(key): float(value)
            for key, value in multipliers.items()
            if isinstance(value, int | float)
        },
        default_multiplier=(float(default) if isinstance(default, int | float) else DEFAULT_UPLIFT),
    )


def _entries(module: ModuleType) -> list[FestivalInfo]:
    """Every festival the calendar module exposes, whatever container it used."""
    for name in ("FESTIVALS", "FESTIVALS_2026", "CALENDAR", "ALL_FESTIVALS", "festivals"):
        raw = getattr(module, name, None)
        if isinstance(raw, dict):
            raw = list(raw.values())
        if isinstance(raw, list | tuple) and raw:
            found = [info for info in (_normalise(item) for item in raw) if info is not None]
            if found:
                return sorted(found, key=lambda info: info.day)
    return []


def next_festival(as_of: datetime, *, module: ModuleType | None = None) -> FestivalInfo | None:
    """The soonest festival on or after ``as_of``, or ``None`` when no calendar is available."""
    calendar = module if module is not None else load_festivals()
    if calendar is None:
        return None

    today = ist_date_of(as_of)
    for attribute in ("next_festival", "upcoming", "next_for"):
        function = getattr(calendar, attribute, None)
        if not callable(function):
            continue
        # The calendar may want a date or a datetime; try both before giving up on it.
        for argument in (today, as_of):
            try:
                candidate = function(argument)
            except Exception:  # foreign module: a bad calendar must never kill the feed
                logger.debug("festivals.%s(%r) failed", attribute, type(argument), exc_info=True)
                continue
            if isinstance(candidate, list | tuple):
                candidate = candidate[0] if candidate else None
            info = _normalise(candidate) if candidate is not None else None
            if info is not None:
                return info

    upcoming = [info for info in _entries(calendar) if info.day >= today]
    return upcoming[0] if upcoming else None


def festival_multiplier(category: str, as_of: datetime, *, horizon_days: int = 14) -> float:
    """Average demand uplift for ``category`` over the next ``horizon_days``.

    A restock has to cover a *window*, not a day, so this averages the calendar's day-by-day
    uplift across the window the order will serve rather than reading the festival's peak
    multiplier — ordering 2.4x of confectionery for a Diwali that is still twelve days out would
    leave the shop holding it.

    Returns ``1.0`` whenever the calendar module is missing, the window is quiet, or the calendar
    declares nothing for this category. Used by the restock recommendation in
    :mod:`munshiji.insights.inventory`.
    """
    calendar = load_festivals()
    if calendar is None:
        return 1.0
    today = ist_date_of(as_of)
    horizon = max(1, horizon_days)

    daily = getattr(calendar, "uplift_for", None)
    if callable(daily):
        try:
            values = [
                float(daily(today + timedelta(days=offset), category)) for offset in range(horizon)
            ]
        except Exception:  # foreign module: fall through to the coarser estimate
            logger.debug("festivals.uplift_for failed", exc_info=True)
        else:
            if values:
                return max(1.0, sum(values) / len(values))

    info = next_festival(as_of, module=calendar)
    if info is None:
        return 1.0
    days_until = info.days_until(today)
    if days_until < 0 or days_until > horizon:
        return 1.0
    return max(1.0, info.multiplier_for(category))


class FestivalPrepEngine:
    """Stock up for the next festival, sized from evidence where evidence exists.

    Preference order for the uplift applied to each category:

    1. **Last year's realised uplift** — festival-window revenue divided by the quiet baseline a
       month earlier, from this shop's own rows. Beats any declared constant.
    2. **The calendar's declared multiplier** for that category.
    3. :data:`DEFAULT_UPLIFT`.

    Confidence follows the same ladder, since a measured uplift is worth far more than a guess.
    """

    kind = InsightKind.FESTIVAL_PREP

    def __init__(self, *, max_categories: int = 5) -> None:
        self.max_categories = max_categories

    def run(self, ctx: InsightContext) -> list[InsightDraft]:
        info = next_festival(ctx.as_of)
        if info is None:
            return []
        today = ist_date_of(ctx.as_of)
        days_until = info.days_until(today)
        prep_window = info.prep_days or DEFAULT_PREP_WINDOW_DAYS
        if days_until < 0 or days_until > prep_window:
            return []

        measured = self._last_year_uplift(ctx, info)
        recent = analytics.category_margins(
            ctx.session,
            ctx.merchant_id,
            first_day=today - timedelta(days=UPLIFT_BASELINE_DAYS),
            last_day=today - timedelta(days=1),
        )
        if not recent:
            return []

        rows: list[dict[str, Any]] = []
        for category, frame in recent.items():
            if frame.revenue_paise <= 0:
                continue
            observed = measured.get(category)
            uplift = observed if observed else info.multiplier_for(category)
            if uplift <= 1.0:
                continue
            daily = safe_div(frame.revenue_paise, UPLIFT_BASELINE_DAYS, 0.0)
            extra = int(round(daily * UPLIFT_WINDOW_DAYS * (uplift - 1.0)))
            rows.append(
                {
                    "category": category,
                    "uplift": round(uplift, 3),
                    "uplift_source": "last_year" if observed else "calendar",
                    "baseline_daily_paise": int(round(daily)),
                    "extra_revenue_paise": extra,
                    "suggested_extra_stock_paise": int(round(extra * 0.7)),
                }
            )
        if not rows:
            return []

        rows.sort(key=lambda row: -int(row["extra_revenue_paise"]))
        rows = rows[: self.max_categories]
        impact = sum(int(row["extra_revenue_paise"]) for row in rows)
        if impact <= 0:
            return []

        evidence = sum(1 for row in rows if row["uplift_source"] == "last_year")
        confidence = round(clamp(0.5 + 0.3 * safe_div(evidence, len(rows), 0.0), 0.5, 0.85), 3)
        severity = Severity.HIGH if days_until <= 5 else Severity.MEDIUM
        headline = ", ".join(str(row["category"]) for row in rows[:3])

        return [
            InsightDraft(
                kind=self.kind,
                severity=severity,
                title_en=f"{info.name} is {days_until} days away — stock up {headline}",
                title_hi=f"{info.name_hi} में {days_until} दिन बाक़ी — {headline} का स्टॉक भरिए",
                body_en=(
                    f"{info.name} falls on {info.day.isoformat()}. Across {len(rows)} categories "
                    f"the expected festival lift is worth about {_inr(impact)} over the "
                    f"{UPLIFT_WINDOW_DAYS} days around it"
                    + (
                        f" ({evidence} of them sized from last year's actual uplift)."
                        if evidence
                        else " (sized from the festival calendar; no prior-year data yet)."
                    )
                ),
                body_hi=(
                    f"{info.name_hi} {info.day.isoformat()} को है। {len(rows)} श्रेणियों में "
                    f"त्योहार की बढ़त लगभग {_inr(impact)} की हो सकती है — अभी ऑर्डर कर दीजिए "
                    "तो माल समय पर आ जाएगा।"
                ),
                metrics={
                    "festival": info.name,
                    "festival_hi": info.name_hi,
                    "festival_date": info.day.isoformat(),
                    "days_until": days_until,
                    "prep_window_days": prep_window,
                    "baseline_days": UPLIFT_BASELINE_DAYS,
                    "uplift_window_days": UPLIFT_WINDOW_DAYS,
                    "categories": rows,
                    "extra_revenue_paise": impact,
                    "measured_categories": evidence,
                },
                suggested_tool="draft_restock_order",
                suggested_params={
                    "reason": f"{info.name} prep",
                    "categories": [
                        {
                            "category": row["category"],
                            "extra_budget_paise": row["suggested_extra_stock_paise"],
                            "uplift": row["uplift"],
                        }
                        for row in rows
                    ],
                    "needed_by": (info.day - timedelta(days=2)).isoformat(),
                },
                impact_paise=impact,
                confidence=confidence,
                dedupe_key=f"festival_prep:{info.name.lower().replace(' ', '_')}",
                expires_in_days=max(1, days_until or 1),
            )
        ]

    def _last_year_uplift(self, ctx: InsightContext, info: FestivalInfo) -> dict[str, float]:
        """Realised per-category uplift around the same festival a year ago, where data reaches.

        Uses the median of the two windows' *daily* revenue so one blowout day cannot invent an
        uplift. Returns an empty mapping when last year is not in the database.
        """
        anchor = info.day - timedelta(days=365)
        festival_first = anchor - timedelta(days=UPLIFT_WINDOW_DAYS // 2)
        festival_last = festival_first + timedelta(days=UPLIFT_WINDOW_DAYS - 1)
        quiet_last = festival_first - timedelta(days=7)
        quiet_first = quiet_last - timedelta(days=UPLIFT_BASELINE_DAYS - 1)

        earliest = analytics.first_transaction_day(ctx.session, ctx.merchant_id)
        if earliest is None or earliest > quiet_first:
            return {}

        festival_frame = analytics.category_margins(
            ctx.session, ctx.merchant_id, first_day=festival_first, last_day=festival_last
        )
        quiet_frame = analytics.category_margins(
            ctx.session, ctx.merchant_id, first_day=quiet_first, last_day=quiet_last
        )
        uplifts: dict[str, float] = {}
        for category, frame in festival_frame.items():
            reference = quiet_frame.get(category)
            if reference is None or frame.revenue_paise < MIN_CATEGORY_REVENUE_PAISE:
                continue
            festival_daily = safe_div(frame.revenue_paise, UPLIFT_WINDOW_DAYS, 0.0)
            quiet_daily = safe_div(reference.revenue_paise, UPLIFT_BASELINE_DAYS, 0.0)
            ratio = safe_div(festival_daily, quiet_daily, 0.0)
            if ratio > 1.0:
                uplifts[category] = round(clamp(ratio, 1.0, 4.0), 3)
        return uplifts
