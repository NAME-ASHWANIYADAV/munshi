"""The Indian festival calendar — the single strongest seasonal driver in a kirana's year.

A festival does not lift every shelf equally: Janmashtami moves dairy, Karva Chauth moves
personal care and mithai, Diwali moves everything but especially confectionery and cleaning
supplies. So uplift is expressed **per category** and ramps up through a prep window rather
than appearing on the day itself — merchants stock and households buy *before* the date.

2026 is the canonical year (SPEC.md §6). 2025 and early 2027 are carried so that any 180-day
window, and :func:`upcoming` from any ``as_of``, still have real dates to work with.

All dates are IST calendar dates. Category keys match :data:`munshiji.seed.catalog.CATEGORIES`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Final

from munshiji.seed.catalog import CATEGORY_SHARES

__all__ = [
    "ALL_FESTIVALS",
    "FESTIVALS_2026",
    "Festival",
    "active_on",
    "day_uplift",
    "days_until",
    "festivals_between",
    "prep_window",
    "upcoming",
    "uplift_for",
]

#: Share of the uplift already felt at the very start of a prep window. The rest ramps linearly.
RAMP_FLOOR: Final[float] = 0.35

#: Ceiling on the combined multiplier when two festival windows overlap.
MAX_COMBINED_UPLIFT: Final[float] = 3.0


@dataclass(frozen=True, slots=True)
class Festival:
    """One festival, with the shelf it moves.

    ``day`` is the main date. ``duration_days`` covers multi-day festivals (Navratri runs nine
    nights); the uplift stays at full strength for the whole span. ``prep_window_days`` is how
    many days *before* ``day`` buying starts to build.
    """

    key: str
    name_en: str
    name_hi: str
    day: date
    prep_window_days: int
    category_uplift: Mapping[str, float] = field(default_factory=dict)
    duration_days: int = 1
    note_en: str = ""
    note_hi: str = ""

    @property
    def start_day(self) -> date:
        """First day of the prep window."""
        return self.day - timedelta(days=self.prep_window_days)

    @property
    def end_day(self) -> date:
        """Last day the festival itself is observed."""
        return self.day + timedelta(days=max(0, self.duration_days - 1))

    def covers(self, day: date) -> bool:
        """Whether ``day`` falls inside the prep window or the festival span."""
        return self.start_day <= day <= self.end_day

    def ramp(self, day: date) -> float:
        """Fraction (0.0–1.0) of this festival's uplift in force on ``day``."""
        if not self.covers(day):
            return 0.0
        if day >= self.day:
            return 1.0
        if self.prep_window_days <= 0:
            return 1.0
        elapsed = (day - self.start_day).days
        progress = elapsed / self.prep_window_days
        return RAMP_FLOOR + (1.0 - RAMP_FLOOR) * progress

    def uplift(self, day: date, category: str) -> float:
        """Multiplier this festival applies to ``category`` on ``day`` (1.0 = no effect)."""
        ramp = self.ramp(day)
        if ramp <= 0.0:
            return 1.0
        raw = self.category_uplift.get(category, 1.0)
        return 1.0 + (raw - 1.0) * ramp


def _f(
    key: str,
    name_en: str,
    name_hi: str,
    day: date,
    prep: int,
    uplift: Mapping[str, float],
    *,
    duration: int = 1,
) -> Festival:
    return Festival(
        key=key,
        name_en=name_en,
        name_hi=name_hi,
        day=day,
        prep_window_days=prep,
        category_uplift=dict(uplift),
        duration_days=duration,
    )


# Per-festival category uplift profiles, reused across years.
_SANKRANTI = {"staples": 1.40, "confectionery": 1.50, "dairy": 1.20, "spices": 1.15}
_HOLI = {
    "confectionery": 1.80,
    "snacks": 1.70,
    "beverages": 1.50,
    "dairy": 1.60,
    "staples": 1.20,
}
_EID = {
    "dairy": 1.60,
    "confectionery": 1.70,
    "staples": 1.30,
    "beverages": 1.30,
    "spices": 1.25,
}
_RAM_NAVAMI = {"staples": 1.25, "dairy": 1.30, "confectionery": 1.20}
_RAKSHA_BANDHAN = {"confectionery": 1.90, "snacks": 1.35, "dairy": 1.20}
_JANMASHTAMI = {"dairy": 1.80, "confectionery": 1.50, "snacks": 1.20}
_GANESH = {"confectionery": 1.60, "dairy": 1.40, "staples": 1.20, "snacks": 1.15}
_NAVRATRI = {
    "staples": 1.50,
    "dairy": 1.60,
    "snacks": 1.25,
    "confectionery": 1.30,
    "beverages": 1.15,
}
_DUSSEHRA = {"snacks": 1.40, "confectionery": 1.50, "staples": 1.20}
_KARVA_CHAUTH = {
    "dairy": 1.50,
    "confectionery": 1.60,
    "personal_care": 1.70,
    "snacks": 1.20,
}
_DIWALI = {
    "confectionery": 2.40,
    "snacks": 1.90,
    "household": 1.50,
    "dairy": 1.45,
    "staples": 1.35,
    "beverages": 1.30,
    "spices": 1.30,
    "personal_care": 1.25,
}
_BHAI_DOOJ = {"confectionery": 1.80, "snacks": 1.30, "dairy": 1.20}
_CHRISTMAS = {
    "confectionery": 1.90,
    "snacks": 1.40,
    "beverages": 1.35,
    "dairy": 1.30,
    "household": 1.15,
}


#: The canonical 2026 calendar required by SPEC.md §6.
FESTIVALS_2026: Final[tuple[Festival, ...]] = (
    _f("makar_sankranti_2026", "Makar Sankranti", "मकर संक्रांति", date(2026, 1, 14), 5, _SANKRANTI),
    _f("holi_2026", "Holi", "होली", date(2026, 3, 4), 7, _HOLI, duration=2),
    _f("eid_ul_fitr_2026", "Eid-ul-Fitr", "ईद-उल-फ़ितर", date(2026, 3, 20), 10, _EID),
    _f("ram_navami_2026", "Ram Navami", "राम नवमी", date(2026, 3, 27), 4, _RAM_NAVAMI),
    _f("raksha_bandhan_2026", "Raksha Bandhan", "रक्षा बंधन", date(2026, 8, 28), 6, _RAKSHA_BANDHAN),
    _f("janmashtami_2026", "Janmashtami", "जन्माष्टमी", date(2026, 9, 4), 4, _JANMASHTAMI),
    _f(
        "ganesh_chaturthi_2026",
        "Ganesh Chaturthi",
        "गणेश चतुर्थी",
        date(2026, 9, 14),
        5,
        _GANESH,
        duration=2,
    ),
    _f(
        "navratri_2026",
        "Sharad Navratri",
        "शरद नवरात्रि",
        date(2026, 10, 11),
        7,
        _NAVRATRI,
        duration=9,
    ),
    _f("dussehra_2026", "Dussehra", "दशहरा", date(2026, 10, 20), 6, _DUSSEHRA),
    _f("karva_chauth_2026", "Karva Chauth", "करवा चौथ", date(2026, 10, 29), 5, _KARVA_CHAUTH),
    _f("diwali_2026", "Diwali", "दिवाली", date(2026, 11, 8), 18, _DIWALI, duration=2),
    _f("bhai_dooj_2026", "Bhai Dooj", "भाई दूज", date(2026, 11, 10), 3, _BHAI_DOOJ),
    _f("christmas_2026", "Christmas", "क्रिसमस", date(2026, 12, 25), 10, _CHRISTMAS),
)

#: 2025, carried so a long look-back window still sees a real Navratri/Diwali ramp.
FESTIVALS_2025: Final[tuple[Festival, ...]] = (
    _f("makar_sankranti_2025", "Makar Sankranti", "मकर संक्रांति", date(2025, 1, 14), 5, _SANKRANTI),
    _f("holi_2025", "Holi", "होली", date(2025, 3, 14), 7, _HOLI, duration=2),
    _f("eid_ul_fitr_2025", "Eid-ul-Fitr", "ईद-उल-फ़ितर", date(2025, 3, 31), 10, _EID),
    _f("ram_navami_2025", "Ram Navami", "राम नवमी", date(2025, 4, 6), 4, _RAM_NAVAMI),
    _f("raksha_bandhan_2025", "Raksha Bandhan", "रक्षा बंधन", date(2025, 8, 9), 6, _RAKSHA_BANDHAN),
    _f("janmashtami_2025", "Janmashtami", "जन्माष्टमी", date(2025, 8, 16), 4, _JANMASHTAMI),
    _f(
        "ganesh_chaturthi_2025",
        "Ganesh Chaturthi",
        "गणेश चतुर्थी",
        date(2025, 8, 27),
        5,
        _GANESH,
        duration=2,
    ),
    _f(
        "navratri_2025",
        "Sharad Navratri",
        "शरद नवरात्रि",
        date(2025, 9, 22),
        7,
        _NAVRATRI,
        duration=9,
    ),
    _f("dussehra_2025", "Dussehra", "दशहरा", date(2025, 10, 2), 6, _DUSSEHRA),
    _f("karva_chauth_2025", "Karva Chauth", "करवा चौथ", date(2025, 10, 10), 5, _KARVA_CHAUTH),
    _f("diwali_2025", "Diwali", "दिवाली", date(2025, 10, 20), 18, _DIWALI, duration=2),
    _f("bhai_dooj_2025", "Bhai Dooj", "भाई दूज", date(2025, 10, 23), 3, _BHAI_DOOJ),
    _f("christmas_2025", "Christmas", "क्रिसमस", date(2025, 12, 25), 10, _CHRISTMAS),
)

#: Early 2027, so ``upcoming()`` keeps working through a December ``as_of``.
FESTIVALS_2027: Final[tuple[Festival, ...]] = (
    _f("makar_sankranti_2027", "Makar Sankranti", "मकर संक्रांति", date(2027, 1, 15), 5, _SANKRANTI),
    _f("holi_2027", "Holi", "होली", date(2027, 3, 22), 7, _HOLI, duration=2),
)

ALL_FESTIVALS: Final[tuple[Festival, ...]] = tuple(
    sorted(FESTIVALS_2025 + FESTIVALS_2026 + FESTIVALS_2027, key=lambda f: f.day)
)


def festivals_between(first: date, last: date) -> list[Festival]:
    """Every festival whose prep window or span overlaps ``[first, last]``."""
    return [f for f in ALL_FESTIVALS if f.start_day <= last and f.end_day >= first]


def active_on(day: date) -> list[Festival]:
    """Festivals whose prep window or span includes ``day``."""
    return [f for f in ALL_FESTIVALS if f.covers(day)]


def upcoming(as_of: date, within_days: int = 45) -> list[Festival]:
    """Festivals landing in ``(as_of, as_of + within_days]``, soonest first.

    Drives the FESTIVAL_PREP insight: "Navratri 27 din door hai, stock kya badhana hai?"
    """
    horizon = as_of + timedelta(days=within_days)
    return [f for f in ALL_FESTIVALS if as_of < f.day <= horizon]


def days_until(festival: Festival, as_of: date) -> int:
    """Whole days from ``as_of`` to the festival date (negative once it has passed)."""
    return (festival.day - as_of).days


def prep_window(festival: Festival) -> tuple[date, date]:
    """``(start, end)`` of the festival's buying window, prep run-up included."""
    return festival.start_day, festival.end_day


def uplift_for(day: date, category: str) -> float:
    """Demand multiplier for ``category`` on ``day``; 1.0 when no festival window is open.

    Overlapping windows compound multiplicatively, clamped at :data:`MAX_COMBINED_UPLIFT`.
    """
    multiplier = 1.0
    for festival in ALL_FESTIVALS:
        if festival.covers(day):
            multiplier *= festival.uplift(day, category)
    return min(multiplier, MAX_COMBINED_UPLIFT)


def day_uplift(day: date, category_shares: Mapping[str, float] | None = None) -> float:
    """Whole-shop demand multiplier for ``day``: category uplifts weighted by basket share."""
    shares = category_shares if category_shares is not None else CATEGORY_SHARES
    total = sum(shares.values())
    if total <= 0:
        return 1.0
    weighted = sum(share * uplift_for(day, category) for category, share in shares.items())
    return weighted / total
