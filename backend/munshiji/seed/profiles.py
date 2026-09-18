"""Merchant profiles — every knob the generator turns, in one declarative place.

A profile is the *shape* of a shop: how busy it is, how its customers behave, how much credit
it extends, and how strong each planted signal should be. Changing a number here changes the
generated world; the generator itself contains no magic constants about the business.

Money is int paise. Cadences and windows are whole days, measured in IST calendar days.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "BUSY_MARKET_PROFILE",
    "COHORTS",
    "DEFAULT_PROFILE",
    "FIRST_NAMES_FEMALE",
    "FIRST_NAMES_MALE",
    "PROFILES",
    "SURNAMES",
    "CohortSpec",
    "SeedProfile",
    "get_profile",
]


@dataclass(frozen=True, slots=True)
class CohortSpec:
    """One customer cohort: how often they come, how big they buy, how likely they take udhaar.

    ``mean_gap_days`` is the *personal* visit cadence that makes per-customer dormancy detection
    meaningful — a champion who vanishes for 12 days is a problem; an occasional buyer who does
    the same is Tuesday. ``gap_jitter`` is that cadence's relative spread across people in the
    cohort: tight for champions, loose for occasional buyers.

    Deliberately no ``segment`` here. A customer's RFM segment is derived from what they actually
    bought (``generator._segment_for``), never asserted by the cohort that generated them.
    """

    name: str
    share: float
    mean_gap_days: float
    gap_jitter: float
    ticket_bias: float
    khata_propensity: float


#: Cohort mix. Shares sum to 1.0. Roughly the RFM shape of a settled neighbourhood kirana.
COHORTS: Final[tuple[CohortSpec, ...]] = (
    CohortSpec("champion", 0.08, 3.0, 0.14, 1.45, 0.42),
    CohortSpec("loyal", 0.17, 5.0, 0.16, 1.20, 0.30),
    CohortSpec("regular", 0.30, 8.0, 0.20, 1.00, 0.16),
    CohortSpec("occasional", 0.45, 16.0, 0.28, 0.85, 0.06),
)


@dataclass(frozen=True, slots=True)
class SeedProfile:
    """Everything that defines a generated merchant world."""

    # ── identity ────────────────────────────────────────────────────────────
    key: str = "sharma_general_store"
    shop_name: str = "Sharma General Store"
    shop_name_hi: str = "शर्मा जनरल स्टोर"
    owner_name: str = "Rajesh Sharma"
    owner_name_hi: str = "राजेश शर्मा"
    category: str = "kirana"
    city: str = "Delhi"
    locality: str = "Lajpat Nagar"
    language: str = "hi-IN"
    phone: str = "+919811034572"
    #: Demo login password (hashed at seed time — the DB never stores this string). One shared
    #: word across shops keeps the venue pitch simple: "har dukaan ka password munshi123".
    password: str = "munshi123"
    #: Which shelf this shop carries — a :func:`munshiji.seed.catalog.get_catalog` key.
    catalog_key: str = "kirana"
    soundbox_id: str = "PTM-SB-LJP-4417"
    monthly_rent_paise: int = 4_500_000  # ₹45,000
    business_hours_start: int = 7
    business_hours_end: int = 22
    years_open: int = 9

    # ── volume ──────────────────────────────────────────────────────────────
    customer_count: int = 220
    base_txns_per_day: int = 52
    min_txns_per_day: int = 40
    max_txns_per_day: int = 70
    walkin_share: float = 0.35
    #: How the day multiplier splits between "more bills" and "bigger bills".
    count_exponent: float = 0.62
    daily_noise_sigma: float = 0.055
    trend_start: float = 0.94
    trend_end: float = 1.06
    #: Fraction of customers acquired *during* the window rather than present from day one.
    late_acquisition_share: float = 0.20

    # ── payment mix drift (soundbox/UPI adoption) ───────────────────────────
    upi_share_start: float = 0.55
    upi_share_end: float = 0.72
    soundbox_share_start: float = 0.05
    soundbox_share_end: float = 0.09
    #: How the non-UPI remainder splits: cash / card / wallet.
    cash_of_remainder: float = 0.72
    card_of_remainder: float = 0.19

    # ── planted signals ─────────────────────────────────────────────────────
    dormant_count: int = 13
    dormant_stop_min_days: int = 21
    dormant_stop_max_days: int = 42
    dormant_gap_min: float = 4.0
    dormant_gap_max: float = 8.0

    dead_stock_count: int = 4
    dead_stock_min_quiet_days: int = 52
    dead_stock_max_quiet_days: int = 78
    dead_stock_value_min_paise: int = 70_000  # ₹700
    dead_stock_value_max_paise: int = 260_000  # ₹2,600

    stockout_count: int = 3
    stockout_cover_min_days: float = 1.6
    stockout_cover_max_days: float = 2.9

    expiry_count: int = 2
    expiry_remaining_shelf_min: int = 3
    expiry_remaining_shelf_max: int = 6
    expiry_cover_min_days: float = 9.0
    expiry_cover_max_days: float = 16.0

    # A settled neighbourhood kirana carries a real credit book: dozens of open tabs, not a
    # handful. Sized so the outstanding lands in the low-to-mid teens as a percentage of monthly
    # turnover, which is where the working-capital pain the product addresses actually lives.
    open_khata_count: int = 34
    khata_over_60_count: int = 7
    khata_partial_count: int = 9
    settled_khata_count: int = 40
    khata_due_days: int = 15

    collection_dip_days: int = 7
    #: Softness planted into the demand weights for the dip window — a mild slowdown in both
    #: footfall and basket size. Deliberately shallower than the shortfall we want to observe.
    collection_dip_min: float = 0.93
    collection_dip_max: float = 0.97
    #: The shortfall an analyst should actually measure. Bills are trimmed from the soft week
    #: until the observed figure lands in this band, because a day's collection carries ~14%
    #: sampling noise and this number opens the live demo.
    observed_dip_min: float = 0.10
    observed_dip_max: float = 0.145
    #: Same-weekday history used both to plant and to detect the dip.
    baseline_weeks: int = 8

    margin_leak_category: str = "dairy"
    margin_leak_days: int = 30
    #: Dairy runs on a 4–8% trade margin, so a cost shock has very little to eat through. At a 6%
    #: uplift the category went *negative* — and no shopkeeper sells below cost for a month; he
    #: raises the price or stops stocking it. 3.5% destroys roughly three quarters of the margin
    #: on a high-volume line, which is the squeeze worth surfacing and still a number a kirana
    #: owner would recognise.
    margin_leak_cost_uplift: float = 1.035
    #: Gentle background cost inflation across the whole window, for every SKU.
    cost_drift: float = 0.015

    # ── stock ledger ────────────────────────────────────────────────────────
    # A kirana orders in cases, not units, so the shelf carries weeks of cover rather than days.
    # At 10–14 days the shop held ~10 days of stock overall, which understated both the capital
    # sitting in dead lines and the expiry exposure on perishables.
    reorder_cover_days: float = 5.0
    restock_cover_min_days: float = 18.0
    restock_cover_max_days: float = 28.0

    cohorts: tuple[CohortSpec, ...] = field(default=COHORTS)

    @property
    def dip_ratio_range(self) -> tuple[float, float]:
        """``(low, high)`` multiplier applied to the weekday baseline in the dip window."""
        return self.collection_dip_min, self.collection_dip_max


DEFAULT_PROFILE: Final[SeedProfile] = SeedProfile()

#: A second profile, useful for multi-merchant demos and for proving the generator is not
#: hard-wired to one shop. Busier, more walk-ins, far less udhaar — a market-road store.
BUSY_MARKET_PROFILE: Final[SeedProfile] = SeedProfile(
    key="verma_provision_store",
    shop_name="Verma Provision Store",
    shop_name_hi="वर्मा प्रोविज़न स्टोर",
    owner_name="Sunil Verma",
    owner_name_hi="सुनील वर्मा",
    locality="Sarojini Nagar",
    phone="+919810277431",
    soundbox_id="PTM-SB-SRJ-9082",
    monthly_rent_paise=6_200_000,
    customer_count=180,
    base_txns_per_day=64,
    min_txns_per_day=48,
    max_txns_per_day=86,
    walkin_share=0.48,
    open_khata_count=12,
    khata_over_60_count=3,
    settled_khata_count=18,
    margin_leak_category="staples",
)

#: A neighbourhood chemist. Different physics from a kirana: fewer, chunkier bills, a heavy
#: walk-in share, digitised payments, and expiry as the *headline* inventory risk — medicine
#: past its date is not markdown stock, it is a write-off with a compliance shadow.
#: ``years_open=6`` keeps its ``created_at`` after Sharma's, so the "default" alias (oldest
#: merchant) — and with it every n8n workflow — stays pinned to the kirana.
GUPTA_MEDICAL_PROFILE: Final[SeedProfile] = SeedProfile(
    key="gupta_medical_store",
    shop_name="Gupta Medical Store",
    shop_name_hi="गुप्ता मेडिकल स्टोर",
    owner_name="Anita Gupta",
    owner_name_hi="अनीता गुप्ता",
    category="pharmacy",
    catalog_key="pharmacy",
    locality="Malviya Nagar",
    phone="+919873046521",
    soundbox_id="PTM-SB-MLV-2210",
    monthly_rent_paise=5_500_000,
    years_open=6,
    customer_count=150,
    base_txns_per_day=34,
    min_txns_per_day=24,
    max_txns_per_day=46,
    walkin_share=0.42,
    upi_share_start=0.60,
    upi_share_end=0.78,
    dormant_count=9,
    dead_stock_count=3,
    dead_stock_value_min_paise=120_000,
    dead_stock_value_max_paise=380_000,
    stockout_count=3,
    expiry_count=3,
    open_khata_count=22,
    khata_over_60_count=5,
    khata_partial_count=6,
    settled_khata_count=30,
    margin_leak_category="otc",
)

#: A mobile-accessories counter — the youngest shop, almost all walk-ins, fat margins on
#: unbranded covers and glass, and dead stock with a face: covers cut for phone models nobody
#: buys any more. No perishables, so ``expiry_count=0`` — the expiry engine simply finds an
#: empty population rather than being switched off.
KHAN_MOBILE_PROFILE: Final[SeedProfile] = SeedProfile(
    key="khan_mobile_point",
    shop_name="Khan Mobile Point",
    shop_name_hi="ख़ान मोबाइल पॉइंट",
    owner_name="Imran Khan",
    owner_name_hi="इमरान ख़ान",
    category="mobile",
    catalog_key="mobile",
    locality="Karol Bagh",
    phone="+919811207344",
    soundbox_id="PTM-SB-KBG-7731",
    monthly_rent_paise=3_800_000,
    years_open=3,
    customer_count=110,
    base_txns_per_day=22,
    min_txns_per_day=14,
    max_txns_per_day=34,
    walkin_share=0.55,
    upi_share_start=0.65,
    upi_share_end=0.82,
    dormant_count=8,
    dead_stock_count=5,
    dead_stock_value_min_paise=90_000,
    dead_stock_value_max_paise=320_000,
    stockout_count=2,
    expiry_count=0,
    open_khata_count=8,
    khata_over_60_count=2,
    khata_partial_count=3,
    settled_khata_count=10,
    margin_leak_category="chargers",
)

PROFILES: Final[dict[str, SeedProfile]] = {
    DEFAULT_PROFILE.key: DEFAULT_PROFILE,
    BUSY_MARKET_PROFILE.key: BUSY_MARKET_PROFILE,
    GUPTA_MEDICAL_PROFILE.key: GUPTA_MEDICAL_PROFILE,
    KHAN_MOBILE_PROFILE.key: KHAN_MOBILE_PROFILE,
}

#: The worlds `munshiji seed` builds, in "default"-resolution order: Sharma FIRST (oldest
#: ``created_at`` via ``years_open``), so the n8n workflows and the runbook recipe keep
#: addressing the kirana while the login screen offers three different shops.
SEEDED_PROFILES: Final[tuple[SeedProfile, ...]] = (
    DEFAULT_PROFILE,
    GUPTA_MEDICAL_PROFILE,
    KHAN_MOBILE_PROFILE,
)


def get_profile(key: str | None = None) -> SeedProfile:
    """Look up a profile by key; ``None`` returns the default Sharma General Store."""
    if key is None:
        return DEFAULT_PROFILE
    try:
        return PROFILES[key]
    except KeyError as exc:  # pragma: no cover - defensive
        known = ", ".join(sorted(PROFILES))
        raise KeyError(f"unknown seed profile {key!r}; known profiles: {known}") from exc


# ─────────────────────────────────────────────────────────────────────────────
# Name pools — a Delhi customer book: Punjabi, UP/Bihari, Muslim, Sikh, South Indian.
# ─────────────────────────────────────────────────────────────────────────────

FIRST_NAMES_MALE: Final[tuple[str, ...]] = (
    "Rajesh",
    "Amit",
    "Sunil",
    "Vikram",
    "Manoj",
    "Deepak",
    "Anil",
    "Rakesh",
    "Sandeep",
    "Naveen",
    "Pankaj",
    "Ashok",
    "Gaurav",
    "Harish",
    "Jitendra",
    "Kuldeep",
    "Lalit",
    "Mahesh",
    "Nitin",
    "Pradeep",
    "Rahul",
    "Sachin",
    "Tarun",
    "Umesh",
    "Varun",
    "Yogesh",
    "Arjun",
    "Bhavesh",
    "Chetan",
    "Dinesh",
    "Imran",
    "Faizan",
    "Salman",
    "Arif",
    "Zubair",
    "Jaspreet",
    "Harpreet",
    "Gurpreet",
    "Balwinder",
    "Ramesh",
    "Suresh",
    "Vinod",
    "Ajay",
    "Vijay",
    "Mohit",
    "Rohit",
    "Saurabh",
    "Shubham",
    "Akash",
    "Kapil",
    "Nikhil",
    "Prashant",
    "Siddharth",
)

FIRST_NAMES_FEMALE: Final[tuple[str, ...]] = (
    "Priya",
    "Sunita",
    "Anjali",
    "Kavita",
    "Neha",
    "Pooja",
    "Rekha",
    "Shalini",
    "Meena",
    "Ritu",
    "Divya",
    "Nisha",
    "Seema",
    "Aarti",
    "Bhavna",
    "Geeta",
    "Jyoti",
    "Kiran",
    "Lata",
    "Mamta",
    "Nandini",
    "Poonam",
    "Radha",
    "Sarita",
    "Usha",
    "Vandana",
    "Shabana",
    "Farah",
    "Nazia",
    "Simran",
    "Manpreet",
    "Jasleen",
    "Swati",
    "Preeti",
    "Ruchi",
    "Tanvi",
    "Anita",
    "Babita",
    "Chhavi",
    "Deepa",
    "Ekta",
    "Garima",
    "Heena",
    "Isha",
    "Komal",
    "Megha",
    "Nidhi",
    "Payal",
    "Rachna",
    "Shweta",
    "Sneha",
    "Rashmi",
)

SURNAMES: Final[tuple[str, ...]] = (
    "Sharma",
    "Verma",
    "Gupta",
    "Aggarwal",
    "Bansal",
    "Jain",
    "Malhotra",
    "Kapoor",
    "Chopra",
    "Khanna",
    "Mehra",
    "Sethi",
    "Arora",
    "Bhatia",
    "Tandon",
    "Dua",
    "Sood",
    "Grover",
    "Chawla",
    "Ahuja",
    "Singh",
    "Kaur",
    "Gill",
    "Sandhu",
    "Bedi",
    "Khan",
    "Ansari",
    "Qureshi",
    "Siddiqui",
    "Sheikh",
    "Yadav",
    "Kumar",
    "Mishra",
    "Tiwari",
    "Pandey",
    "Dubey",
    "Chaudhary",
    "Rana",
    "Negi",
    "Rawat",
    "Bisht",
    "Joshi",
    "Nair",
    "Menon",
    "Iyer",
    "Reddy",
    "Naidu",
    "Das",
    "Ghosh",
    "Roy",
    "Saxena",
    "Srivastava",
    "Rastogi",
    "Goel",
    "Mittal",
    "Garg",
    "Singhal",
    "Jindal",
    "Bhardwaj",
    "Thakur",
)
