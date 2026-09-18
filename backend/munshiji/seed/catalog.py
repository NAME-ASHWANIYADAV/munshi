"""The kirana shelf — 75 real SKUs a Lajpat Nagar general store actually carries.

Prices are Delhi retail, 2026, expressed in **paise**. ``cost_paise`` is what the merchant pays
the distributor; ``sell_paise`` is MRP at the counter. Indian kirana margins are thin and very
uneven by category — dairy runs 9-11%, packaged snacks 14-16%, household 8-12% — and that
unevenness is what makes margin analysis worth doing, so it is modelled rather than flattened.

``popularity`` is a *relative* pick weight inside a category (not across categories; cross-category
mix lives in :data:`CATEGORY_SHARES`). ``festival_affinity`` boosts a SKU only while a festival
window is open — mithai and dry fruit move on Diwali, not in July.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final

__all__ = [
    "CATALOG",
    "CATEGORIES",
    "CATEGORY_LABELS_HI",
    "CATEGORY_MARGIN_TARGET",
    "CATEGORY_SHARES",
    "CO_OCCURRENCE",
    "KIRANA_CATALOG",
    "CatalogItem",
    "ShopCatalog",
    "at_trade_margin",
    "by_category",
    "by_sku",
    "get_catalog",
    "perishables",
]


@dataclass(frozen=True, slots=True)
class CatalogItem:
    """One shelf SKU. All money is int paise."""

    sku: str
    name: str
    name_hi: str
    category: str
    unit: str
    cost_paise: int
    sell_paise: int
    popularity: float
    qty_choices: tuple[float, ...]
    is_perishable: bool = False
    shelf_life_days: int | None = None
    festival_affinity: float = 1.0

    @property
    def margin_paise(self) -> int:
        """Gross margin per unit at catalogue prices, in paise."""
        return self.sell_paise - self.cost_paise

    @property
    def margin_pct(self) -> float:
        """Gross margin as a percentage of the selling price."""
        return 0.0 if self.sell_paise == 0 else self.margin_paise / self.sell_paise * 100.0


#: Canonical category order. Every uplift / share table is keyed on exactly these.
CATEGORIES: Final[tuple[str, ...]] = (
    "staples",
    "dairy",
    "snacks",
    "beverages",
    "personal_care",
    "household",
    "confectionery",
    "spices",
)

CATEGORY_LABELS_HI: Final[dict[str, str]] = {
    "staples": "राशन",
    "dairy": "डेयरी",
    "snacks": "नमकीन",
    "beverages": "पेय",
    "personal_care": "पर्सनल केयर",
    "household": "घरेलू सामान",
    "confectionery": "मिठाई",
    "spices": "मसाले",
}

#: Share of basket *lines* by category. Sums to 1.0.
CATEGORY_SHARES: Final[dict[str, float]] = {
    "staples": 0.20,
    "dairy": 0.19,
    "snacks": 0.16,
    "beverages": 0.11,
    "personal_care": 0.10,
    "household": 0.10,
    "confectionery": 0.08,
    "spices": 0.06,
}

#: Given the basket's anchor category, where the *next* line comes from. Each row sums to 1.0.
#: Atta pulls masala and oil; chips pull a cold drink; a soap run pulls detergent.
CO_OCCURRENCE: Final[dict[str, dict[str, float]]] = {
    "staples": {
        "staples": 0.22,
        "spices": 0.20,
        "dairy": 0.16,
        "household": 0.14,
        "beverages": 0.10,
        "snacks": 0.08,
        "personal_care": 0.06,
        "confectionery": 0.04,
    },
    "dairy": {
        "dairy": 0.20,
        "staples": 0.18,
        "beverages": 0.15,
        "snacks": 0.14,
        "confectionery": 0.12,
        "household": 0.08,
        "spices": 0.07,
        "personal_care": 0.06,
    },
    "snacks": {
        "snacks": 0.24,
        "beverages": 0.22,
        "confectionery": 0.18,
        "dairy": 0.12,
        "staples": 0.08,
        "household": 0.06,
        "personal_care": 0.05,
        "spices": 0.05,
    },
    "beverages": {
        "beverages": 0.22,
        "snacks": 0.24,
        "confectionery": 0.14,
        "dairy": 0.14,
        "staples": 0.10,
        "household": 0.06,
        "personal_care": 0.05,
        "spices": 0.05,
    },
    "personal_care": {
        "personal_care": 0.26,
        "household": 0.24,
        "dairy": 0.12,
        "snacks": 0.10,
        "staples": 0.10,
        "beverages": 0.08,
        "confectionery": 0.06,
        "spices": 0.04,
    },
    "household": {
        "household": 0.28,
        "personal_care": 0.22,
        "staples": 0.14,
        "dairy": 0.10,
        "snacks": 0.09,
        "beverages": 0.07,
        "spices": 0.06,
        "confectionery": 0.04,
    },
    "confectionery": {
        "confectionery": 0.22,
        "snacks": 0.24,
        "beverages": 0.18,
        "dairy": 0.14,
        "staples": 0.08,
        "personal_care": 0.05,
        "household": 0.05,
        "spices": 0.04,
    },
    "spices": {
        "spices": 0.24,
        "staples": 0.26,
        "dairy": 0.12,
        "household": 0.10,
        "snacks": 0.09,
        "beverages": 0.07,
        "personal_care": 0.06,
        "confectionery": 0.06,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# The shelf
# ─────────────────────────────────────────────────────────────────────────────

#: Shelf prices as listed. Trade margins are applied afterwards by :func:`_at_trade_margin`,
#: which is what produces the exported ``CATALOG``.
_LISTED: Final[tuple[CatalogItem, ...]] = (
    # ── staples ─────────────────────────────────────────────────────────────
    CatalogItem(
        sku="STP-ATTA-5KG",
        name="Aashirvaad Atta 5kg",
        name_hi="आशीर्वाद आटा 5 किलो",
        category="staples",
        unit="pack",
        cost_paise=26_200,
        sell_paise=28_500,
        popularity=1.70,
        qty_choices=(1.0, 1.0, 1.0, 2.0),
        festival_affinity=1.15,
    ),
    CatalogItem(
        sku="STP-BASMATI-1KG",
        name="India Gate Basmati Rice 1kg",
        name_hi="इंडिया गेट बासमती चावल 1 किलो",
        category="staples",
        unit="pack",
        cost_paise=12_000,
        sell_paise=13_500,
        popularity=1.10,
        qty_choices=(1.0, 1.0, 2.0),
        festival_affinity=1.30,
    ),
    CatalogItem(
        sku="STP-RICE-5KG",
        name="Sona Masoori Rice 5kg",
        name_hi="सोना मसूरी चावल 5 किलो",
        category="staples",
        unit="pack",
        cost_paise=30_200,
        sell_paise=33_000,
        popularity=0.75,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="STP-TOOR-1KG",
        name="Toor Dal 1kg",
        name_hi="तूर दाल 1 किलो",
        category="staples",
        unit="kg",
        cost_paise=16_800,
        sell_paise=18_500,
        popularity=1.25,
        qty_choices=(0.5, 1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="STP-CHANA-1KG",
        name="Chana Dal 1kg",
        name_hi="चना दाल 1 किलो",
        category="staples",
        unit="kg",
        cost_paise=8_600,
        sell_paise=9_500,
        popularity=0.95,
        qty_choices=(0.5, 1.0, 1.0),
    ),
    CatalogItem(
        sku="STP-MOONG-1KG",
        name="Moong Dal 1kg",
        name_hi="मूंग दाल 1 किलो",
        category="staples",
        unit="kg",
        cost_paise=12_200,
        sell_paise=13_500,
        popularity=0.85,
        qty_choices=(0.5, 1.0, 1.0),
    ),
    CatalogItem(
        sku="STP-SUNOIL-1L",
        name="Fortune Sunflower Oil 1L",
        name_hi="फॉर्च्यून सूरजमुखी तेल 1 लीटर",
        category="staples",
        unit="litre",
        cost_paise=15_200,
        sell_paise=16_500,
        popularity=1.45,
        qty_choices=(1.0, 1.0, 1.0, 2.0),
        festival_affinity=1.25,
    ),
    CatalogItem(
        sku="STP-MUSTOIL-1L",
        name="Mustard Oil 1L",
        name_hi="सरसों का तेल 1 लीटर",
        category="staples",
        unit="litre",
        cost_paise=16_000,
        sell_paise=17_500,
        popularity=1.05,
        qty_choices=(1.0, 1.0, 2.0),
        festival_affinity=1.20,
    ),
    CatalogItem(
        sku="STP-SUGAR-1KG",
        name="Sugar 1kg",
        name_hi="चीनी 1 किलो",
        category="staples",
        unit="kg",
        cost_paise=4_400,
        sell_paise=4_800,
        popularity=1.55,
        qty_choices=(0.5, 1.0, 1.0, 2.0),
        festival_affinity=1.35,
    ),
    CatalogItem(
        sku="STP-SALT-1KG",
        name="Tata Iodised Salt 1kg",
        name_hi="टाटा आयोडीन नमक 1 किलो",
        category="staples",
        unit="kg",
        cost_paise=2_400,
        sell_paise=2_800,
        popularity=1.30,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="STP-BESAN-500G",
        name="Besan 500g",
        name_hi="बेसन 500 ग्राम",
        category="staples",
        unit="pack",
        cost_paise=5_600,
        sell_paise=6_200,
        popularity=0.90,
        qty_choices=(1.0, 1.0, 2.0),
        festival_affinity=1.45,
    ),
    CatalogItem(
        sku="STP-POHA-500G",
        name="Poha 500g",
        name_hi="पोहा 500 ग्राम",
        category="staples",
        unit="pack",
        cost_paise=3_300,
        sell_paise=3_800,
        popularity=0.70,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="STP-SUJI-500G",
        name="Suji Rava 500g",
        name_hi="सूजी रवा 500 ग्राम",
        category="staples",
        unit="pack",
        cost_paise=3_000,
        sell_paise=3_500,
        popularity=0.65,
        qty_choices=(1.0, 1.0, 2.0),
        festival_affinity=1.40,
    ),
    CatalogItem(
        sku="STP-MAIDA-500G",
        name="Maida 500g",
        name_hi="मैदा 500 ग्राम",
        category="staples",
        unit="pack",
        cost_paise=2_800,
        sell_paise=3_200,
        popularity=0.55,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    # ── dairy (thin margins — this is where a cost rise bites) ───────────────
    CatalogItem(
        sku="DRY-TAAZA-500ML",
        name="Amul Taaza Milk 500ml",
        name_hi="अमूल ताज़ा दूध 500 मि.ली.",
        category="dairy",
        unit="pouch",
        cost_paise=2_600,
        sell_paise=2_900,
        popularity=2.60,
        qty_choices=(1.0, 1.0, 2.0, 2.0, 3.0),
        is_perishable=True,
        shelf_life_days=2,
    ),
    CatalogItem(
        sku="DRY-MDFC-1L",
        name="Mother Dairy Full Cream Milk 1L",
        name_hi="मदर डेयरी फुल क्रीम दूध 1 लीटर",
        category="dairy",
        unit="pouch",
        cost_paise=6_500,
        sell_paise=7_200,
        popularity=2.10,
        qty_choices=(1.0, 1.0, 2.0),
        is_perishable=True,
        shelf_life_days=2,
    ),
    CatalogItem(
        sku="DRY-BUTTER-100G",
        name="Amul Butter 100g",
        name_hi="अमूल बटर 100 ग्राम",
        category="dairy",
        unit="pack",
        cost_paise=5_500,
        sell_paise=6_200,
        popularity=1.15,
        qty_choices=(1.0, 1.0, 2.0),
        is_perishable=True,
        shelf_life_days=60,
    ),
    CatalogItem(
        sku="DRY-CHEESE-200G",
        name="Amul Cheese Slices 200g",
        name_hi="अमूल चीज़ स्लाइस 200 ग्राम",
        category="dairy",
        unit="pack",
        cost_paise=13_000,
        sell_paise=14_500,
        popularity=0.55,
        qty_choices=(1.0, 1.0),
        is_perishable=True,
        shelf_life_days=45,
    ),
    CatalogItem(
        sku="DRY-DAHI-400G",
        name="Amul Masti Dahi 400g",
        name_hi="अमूल मस्ती दही 400 ग्राम",
        category="dairy",
        unit="cup",
        cost_paise=4_000,
        sell_paise=4_500,
        popularity=1.60,
        qty_choices=(1.0, 1.0, 2.0),
        is_perishable=True,
        shelf_life_days=7,
    ),
    CatalogItem(
        sku="DRY-PANEER-200G",
        name="Paneer 200g",
        name_hi="पनीर 200 ग्राम",
        category="dairy",
        unit="pack",
        cost_paise=8_800,
        sell_paise=9_900,
        popularity=1.05,
        qty_choices=(1.0, 1.0, 2.0),
        is_perishable=True,
        shelf_life_days=5,
        festival_affinity=1.35,
    ),
    CatalogItem(
        sku="DRY-CREAM-250ML",
        name="Amul Fresh Cream 250ml",
        name_hi="अमूल फ्रेश क्रीम 250 मि.ली.",
        category="dairy",
        unit="pack",
        cost_paise=7_600,
        sell_paise=8_500,
        popularity=0.50,
        qty_choices=(1.0, 1.0),
        is_perishable=True,
        shelf_life_days=30,
        festival_affinity=1.40,
    ),
    CatalogItem(
        sku="DRY-MILKMAID-400G",
        name="Nestle Milkmaid 400g",
        name_hi="नेस्ले मिल्कमेड 400 ग्राम",
        category="dairy",
        unit="tin",
        cost_paise=14_800,
        sell_paise=16_500,
        popularity=0.35,
        qty_choices=(1.0, 1.0),
        festival_affinity=1.60,
    ),
    # ── snacks ──────────────────────────────────────────────────────────────
    CatalogItem(
        sku="SNK-LAYS-52G",
        name="Lay's Classic Salted 52g",
        name_hi="लेज़ क्लासिक सॉल्टेड 52 ग्राम",
        category="snacks",
        unit="pack",
        cost_paise=1_700,
        sell_paise=2_000,
        popularity=2.20,
        qty_choices=(1.0, 1.0, 2.0, 3.0),
    ),
    CatalogItem(
        sku="SNK-KURKURE-90G",
        name="Kurkure Masala Munch 90g",
        name_hi="कुरकुरे मसाला मंच 90 ग्राम",
        category="snacks",
        unit="pack",
        cost_paise=1_700,
        sell_paise=2_000,
        popularity=1.95,
        qty_choices=(1.0, 1.0, 2.0, 3.0),
    ),
    CatalogItem(
        sku="SNK-BHUJIA-200G",
        name="Haldiram Aloo Bhujia 200g",
        name_hi="हल्दीराम आलू भुजिया 200 ग्राम",
        category="snacks",
        unit="pack",
        cost_paise=4_800,
        sell_paise=5_500,
        popularity=1.50,
        qty_choices=(1.0, 1.0, 2.0),
        festival_affinity=1.30,
    ),
    CatalogItem(
        sku="SNK-BINGO-66G",
        name="Bingo Mad Angles 66g",
        name_hi="बिंगो मैड एंगल्स 66 ग्राम",
        category="snacks",
        unit="pack",
        cost_paise=1_700,
        sell_paise=2_000,
        popularity=1.20,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="SNK-PARLEG-250G",
        name="Parle-G Biscuit 250g",
        name_hi="पारले-जी बिस्किट 250 ग्राम",
        category="snacks",
        unit="pack",
        cost_paise=2_600,
        sell_paise=3_000,
        popularity=2.40,
        qty_choices=(1.0, 1.0, 2.0, 3.0),
    ),
    CatalogItem(
        sku="SNK-GOODDAY-200G",
        name="Britannia Good Day 200g",
        name_hi="ब्रिटानिया गुड डे 200 ग्राम",
        category="snacks",
        unit="pack",
        cost_paise=3_400,
        sell_paise=4_000,
        popularity=1.35,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="SNK-MARIE-250G",
        name="Britannia Marie Gold 250g",
        name_hi="ब्रिटानिया मैरी गोल्ड 250 ग्राम",
        category="snacks",
        unit="pack",
        cost_paise=3_000,
        sell_paise=3_500,
        popularity=1.10,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="SNK-MAGGI-4PK",
        name="Maggi Noodles 4-pack",
        name_hi="मैगी नूडल्स 4 पैक",
        category="snacks",
        unit="pack",
        cost_paise=8_400,
        sell_paise=9_600,
        popularity=1.80,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="SNK-SOANPAPDI-250G",
        name="Haldiram Soan Papdi 250g",
        name_hi="हल्दीराम सोन पापड़ी 250 ग्राम",
        category="snacks",
        unit="box",
        cost_paise=9_600,
        sell_paise=11_000,
        popularity=0.40,
        qty_choices=(1.0, 1.0, 2.0),
        festival_affinity=2.20,
    ),
    # ── beverages ───────────────────────────────────────────────────────────
    CatalogItem(
        sku="BEV-TATAGOLD-250G",
        name="Tata Tea Gold 250g",
        name_hi="टाटा टी गोल्ड 250 ग्राम",
        category="beverages",
        unit="pack",
        cost_paise=13_800,
        sell_paise=15_000,
        popularity=1.70,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="BEV-REDLABEL-500G",
        name="Brooke Bond Red Label Tea 500g",
        name_hi="ब्रुक बॉन्ड रेड लेबल चाय 500 ग्राम",
        category="beverages",
        unit="pack",
        cost_paise=25_200,
        sell_paise=27_500,
        popularity=1.20,
        qty_choices=(1.0, 1.0),
    ),
    CatalogItem(
        sku="BEV-NESCAFE-50G",
        name="Nescafe Classic 50g",
        name_hi="नेस्कैफे क्लासिक 50 ग्राम",
        category="beverages",
        unit="jar",
        cost_paise=19_200,
        sell_paise=21_000,
        popularity=0.55,
        qty_choices=(1.0, 1.0),
    ),
    CatalogItem(
        sku="BEV-BRU-100G",
        name="Bru Instant Coffee 100g",
        name_hi="ब्रू इंस्टेंट कॉफ़ी 100 ग्राम",
        category="beverages",
        unit="jar",
        cost_paise=29_500,
        sell_paise=32_000,
        popularity=0.30,
        qty_choices=(1.0, 1.0),
    ),
    CatalogItem(
        sku="BEV-COKE-750ML",
        name="Coca-Cola 750ml",
        name_hi="कोका-कोला 750 मि.ली.",
        category="beverages",
        unit="bottle",
        cost_paise=3_900,
        sell_paise=4_500,
        popularity=1.60,
        qty_choices=(1.0, 1.0, 2.0),
        festival_affinity=1.30,
    ),
    CatalogItem(
        sku="BEV-THUMSUP-750ML",
        name="Thums Up 750ml",
        name_hi="थम्स अप 750 मि.ली.",
        category="beverages",
        unit="bottle",
        cost_paise=3_900,
        sell_paise=4_500,
        popularity=1.40,
        qty_choices=(1.0, 1.0, 2.0),
        festival_affinity=1.30,
    ),
    CatalogItem(
        sku="BEV-REALJUICE-1L",
        name="Real Mixed Fruit Juice 1L",
        name_hi="रियल मिक्स्ड फ्रूट जूस 1 लीटर",
        category="beverages",
        unit="carton",
        cost_paise=11_200,
        sell_paise=12_500,
        popularity=0.75,
        qty_choices=(1.0, 1.0, 2.0),
        festival_affinity=1.35,
    ),
    CatalogItem(
        sku="BEV-BISLERI-1L",
        name="Bisleri Water 1L",
        name_hi="बिसलेरी पानी 1 लीटर",
        category="beverages",
        unit="bottle",
        cost_paise=1_500,
        sell_paise=2_000,
        popularity=1.85,
        qty_choices=(1.0, 1.0, 2.0, 3.0),
    ),
    CatalogItem(
        sku="BEV-FROOTI-600ML",
        name="Frooti 600ml",
        name_hi="फ्रूटी 600 मि.ली.",
        category="beverages",
        unit="bottle",
        cost_paise=3_900,
        sell_paise=4_500,
        popularity=1.05,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    # ── personal care ───────────────────────────────────────────────────────
    CatalogItem(
        sku="PER-COLGATE-200G",
        name="Colgate Strong Teeth 200g",
        name_hi="कोलगेट स्ट्रॉन्ग टीथ 200 ग्राम",
        category="personal_care",
        unit="tube",
        cost_paise=10_400,
        sell_paise=11_500,
        popularity=1.55,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="PER-CLOSEUP-150G",
        name="Close Up Red Hot 150g",
        name_hi="क्लोज़ अप रेड हॉट 150 ग्राम",
        category="personal_care",
        unit="tube",
        cost_paise=8_900,
        sell_paise=9_900,
        popularity=0.85,
        qty_choices=(1.0, 1.0),
    ),
    CatalogItem(
        sku="PER-LIFEBUOY-125G",
        name="Lifebuoy Soap 125g",
        name_hi="लाइफबॉय साबुन 125 ग्राम",
        category="personal_care",
        unit="bar",
        cost_paise=3_700,
        sell_paise=4_200,
        popularity=1.75,
        qty_choices=(1.0, 2.0, 3.0, 4.0),
    ),
    CatalogItem(
        sku="PER-DOVE-100G",
        name="Dove Soap 100g",
        name_hi="डव साबुन 100 ग्राम",
        category="personal_care",
        unit="bar",
        cost_paise=6_400,
        sell_paise=7_200,
        popularity=0.95,
        qty_choices=(1.0, 2.0, 3.0),
        festival_affinity=1.25,
    ),
    CatalogItem(
        sku="PER-CLINICPLUS-175ML",
        name="Clinic Plus Shampoo 175ml",
        name_hi="क्लिनिक प्लस शैम्पू 175 मि.ली.",
        category="personal_care",
        unit="bottle",
        cost_paise=9_900,
        sell_paise=11_000,
        popularity=1.15,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="PER-HS-180ML",
        name="Head & Shoulders 180ml",
        name_hi="हेड एंड शोल्डर्स 180 मि.ली.",
        category="personal_care",
        unit="bottle",
        cost_paise=16_800,
        sell_paise=18_500,
        popularity=0.45,
        qty_choices=(1.0, 1.0),
    ),
    CatalogItem(
        sku="PER-NIVEA-100ML",
        name="Nivea Soft Cream 100ml",
        name_hi="निविया सॉफ्ट क्रीम 100 मि.ली.",
        category="personal_care",
        unit="jar",
        cost_paise=15_000,
        sell_paise=16_500,
        popularity=0.40,
        qty_choices=(1.0, 1.0),
        festival_affinity=1.30,
    ),
    CatalogItem(
        sku="PER-MACH3-CART",
        name="Gillette Mach3 Cartridge 2s",
        name_hi="जिलेट मैक3 कार्ट्रिज 2 पीस",
        category="personal_care",
        unit="pack",
        cost_paise=31_000,
        sell_paise=34_000,
        popularity=0.22,
        qty_choices=(1.0, 1.0),
    ),
    CatalogItem(
        sku="PER-WHISPER-15",
        name="Whisper Ultra 15 pads",
        name_hi="व्हिस्पर अल्ट्रा 15 पैड",
        category="personal_care",
        unit="pack",
        cost_paise=18_000,
        sell_paise=19_900,
        popularity=0.80,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="PER-DETTOL-200ML",
        name="Dettol Handwash 200ml",
        name_hi="डेटॉल हैंडवॉश 200 मि.ली.",
        category="personal_care",
        unit="bottle",
        cost_paise=8_800,
        sell_paise=9_900,
        popularity=0.70,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    # ── household ───────────────────────────────────────────────────────────
    CatalogItem(
        sku="HH-SURF-1KG",
        name="Surf Excel Easy Wash 1kg",
        name_hi="सर्फ एक्सेल ईज़ी वॉश 1 किलो",
        category="household",
        unit="pack",
        cost_paise=12_400,
        sell_paise=13_500,
        popularity=1.65,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="HH-ARIEL-1KG",
        name="Ariel Matic Front Load 1kg",
        name_hi="एरियल मैटिक फ्रंट लोड 1 किलो",
        category="household",
        unit="pack",
        cost_paise=21_500,
        sell_paise=23_500,
        popularity=0.30,
        qty_choices=(1.0, 1.0),
    ),
    CatalogItem(
        sku="HH-VIMBAR-300G",
        name="Vim Dishwash Bar 300g",
        name_hi="विम डिशवॉश बार 300 ग्राम",
        category="household",
        unit="bar",
        cost_paise=2_600,
        sell_paise=3_000,
        popularity=1.90,
        qty_choices=(1.0, 2.0, 2.0, 3.0),
    ),
    CatalogItem(
        sku="HH-VIMLIQ-500ML",
        name="Vim Dishwash Liquid 500ml",
        name_hi="विम डिशवॉश लिक्विड 500 मि.ली.",
        category="household",
        unit="bottle",
        cost_paise=10_800,
        sell_paise=12_000,
        popularity=0.95,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="HH-HARPIC-500ML",
        name="Harpic Toilet Cleaner 500ml",
        name_hi="हार्पिक टॉयलेट क्लीनर 500 मि.ली.",
        category="household",
        unit="bottle",
        cost_paise=8_800,
        sell_paise=9_900,
        popularity=1.00,
        qty_choices=(1.0, 1.0, 2.0),
        festival_affinity=1.40,
    ),
    CatalogItem(
        sku="HH-LIZOL-500ML",
        name="Lizol Floor Cleaner 500ml",
        name_hi="लाइज़ोल फ्लोर क्लीनर 500 मि.ली.",
        category="household",
        unit="bottle",
        cost_paise=10_300,
        sell_paise=11_500,
        popularity=0.85,
        qty_choices=(1.0, 1.0, 2.0),
        festival_affinity=1.45,
    ),
    CatalogItem(
        sku="HH-COLIN-500ML",
        name="Colin Glass Cleaner 500ml",
        name_hi="कॉलिन ग्लास क्लीनर 500 मि.ली.",
        category="household",
        unit="bottle",
        cost_paise=8_800,
        sell_paise=9_900,
        popularity=0.55,
        qty_choices=(1.0, 1.0),
        festival_affinity=1.50,
    ),
    CatalogItem(
        sku="HH-GOODKNIGHT-REF",
        name="Good Knight Refill 45ml",
        name_hi="गुड नाइट रिफिल 45 मि.ली.",
        category="household",
        unit="pack",
        cost_paise=7_000,
        sell_paise=8_000,
        popularity=0.90,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="HH-GARBAGEBAG-30",
        name="Garbage Bags 30pc",
        name_hi="कचरा बैग 30 पीस",
        category="household",
        unit="roll",
        cost_paise=8_500,
        sell_paise=9_900,
        popularity=0.60,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="HH-MATCH-10PK",
        name="Matchbox 10-pack",
        name_hi="माचिस 10 पैक",
        category="household",
        unit="pack",
        cost_paise=1_600,
        sell_paise=2_000,
        popularity=1.20,
        qty_choices=(1.0, 1.0, 2.0),
        festival_affinity=1.60,
    ),
    # ── confectionery ───────────────────────────────────────────────────────
    CatalogItem(
        sku="CNF-DAIRYMILK-50G",
        name="Cadbury Dairy Milk 50g",
        name_hi="कैडबरी डेयरी मिल्क 50 ग्राम",
        category="confectionery",
        unit="bar",
        cost_paise=4_400,
        sell_paise=5_000,
        popularity=2.00,
        qty_choices=(1.0, 1.0, 2.0, 3.0),
        festival_affinity=1.70,
    ),
    CatalogItem(
        sku="CNF-5STAR-3PK",
        name="Cadbury 5 Star 3-pack",
        name_hi="कैडबरी 5 स्टार 3 पैक",
        category="confectionery",
        unit="pack",
        cost_paise=5_200,
        sell_paise=6_000,
        popularity=1.30,
        qty_choices=(1.0, 1.0, 2.0),
        festival_affinity=1.50,
    ),
    CatalogItem(
        sku="CNF-AMULCHOC-150G",
        name="Amul Dark Chocolate 150g",
        name_hi="अमूल डार्क चॉकलेट 150 ग्राम",
        category="confectionery",
        unit="bar",
        cost_paise=8_800,
        sell_paise=9_900,
        popularity=0.55,
        qty_choices=(1.0, 1.0),
        festival_affinity=1.60,
    ),
    CatalogItem(
        sku="CNF-KAJUKATLI-250G",
        name="Haldiram Kaju Katli 250g",
        name_hi="हल्दीराम काजू कतली 250 ग्राम",
        category="confectionery",
        unit="box",
        cost_paise=27_500,
        sell_paise=31_000,
        popularity=0.35,
        qty_choices=(1.0, 1.0, 2.0),
        festival_affinity=2.60,
    ),
    CatalogItem(
        sku="CNF-CELEBRATIONS-141G",
        name="Cadbury Celebrations 141g",
        name_hi="कैडबरी सेलिब्रेशन्स 141 ग्राम",
        category="confectionery",
        unit="box",
        cost_paise=19_600,
        sell_paise=22_000,
        popularity=0.30,
        qty_choices=(1.0, 1.0, 2.0),
        festival_affinity=2.80,
    ),
    CatalogItem(
        sku="CNF-MELODY-100G",
        name="Parle Melody Toffee 100g",
        name_hi="पारले मेलोडी टॉफ़ी 100 ग्राम",
        category="confectionery",
        unit="pack",
        cost_paise=3_400,
        sell_paise=4_000,
        popularity=1.45,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="CNF-MENTOS-ROLL",
        name="Mentos Roll",
        name_hi="मेंटोस रोल",
        category="confectionery",
        unit="roll",
        cost_paise=800,
        sell_paise=1_000,
        popularity=1.80,
        qty_choices=(1.0, 2.0, 2.0, 3.0),
    ),
    # ── spices ──────────────────────────────────────────────────────────────
    CatalogItem(
        sku="SPC-GARAM-100G",
        name="Everest Garam Masala 100g",
        name_hi="एवरेस्ट गरम मसाला 100 ग्राम",
        category="spices",
        unit="pack",
        cost_paise=8_600,
        sell_paise=9_500,
        popularity=1.35,
        qty_choices=(1.0, 1.0, 2.0),
        festival_affinity=1.30,
    ),
    CatalogItem(
        sku="SPC-DEGGI-100G",
        name="MDH Deggi Mirch 100g",
        name_hi="एमडीएच देगी मिर्च 100 ग्राम",
        category="spices",
        unit="pack",
        cost_paise=8_000,
        sell_paise=9_000,
        popularity=1.00,
        qty_choices=(1.0, 1.0),
    ),
    CatalogItem(
        sku="SPC-HALDI-200G",
        name="Everest Turmeric Powder 200g",
        name_hi="एवरेस्ट हल्दी पाउडर 200 ग्राम",
        category="spices",
        unit="pack",
        cost_paise=6_600,
        sell_paise=7_500,
        popularity=1.55,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="SPC-DHANIA-200G",
        name="Coriander Powder 200g",
        name_hi="धनिया पाउडर 200 ग्राम",
        category="spices",
        unit="pack",
        cost_paise=6_000,
        sell_paise=6_800,
        popularity=1.25,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="SPC-MIRCH-200G",
        name="Red Chilli Powder 200g",
        name_hi="लाल मिर्च पाउडर 200 ग्राम",
        category="spices",
        unit="pack",
        cost_paise=7_500,
        sell_paise=8_500,
        popularity=1.30,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="SPC-JEERA-100G",
        name="Cumin Seeds 100g",
        name_hi="जीरा 100 ग्राम",
        category="spices",
        unit="pack",
        cost_paise=7_000,
        sell_paise=7_800,
        popularity=1.10,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="SPC-SARSON-100G",
        name="Mustard Seeds 100g",
        name_hi="सरसों दाना 100 ग्राम",
        category="spices",
        unit="pack",
        cost_paise=2_400,
        sell_paise=2_800,
        popularity=0.70,
        qty_choices=(1.0, 1.0, 2.0),
    ),
    CatalogItem(
        sku="SPC-HING-25G",
        name="Hing Asafoetida 25g",
        name_hi="हींग 25 ग्राम",
        category="spices",
        unit="box",
        cost_paise=10_400,
        sell_paise=11_500,
        popularity=0.35,
        qty_choices=(1.0, 1.0),
    ),
)


#: Gross-margin band per category, as a share of the selling price.
#:
#: A kirana does not set its own prices — the pack carries an MRP and the customer knows it — so
#: the shelf price is fixed and the margin is whatever the distributor's trade margin leaves.
#: That is why this table adjusts *cost*, not price.
#:
#: The spread is the point. Staples and dairy are the traffic drivers a shop sells almost at cost
#: to get people through the door; spices, personal care and confectionery are where the shop
#: actually earns. Without that spread the margin-leak engine has nothing to find and "push the
#: profitable category" is not advice, it is noise. Bands are industry-typical rather than
#: sourced to one study; the blended result lands near 11%, which is the number to sanity-check.
CATEGORY_MARGIN_TARGET: Final[dict[str, tuple[float, float]]] = {
    "dairy": (0.04, 0.08),
    "staples": (0.05, 0.09),
    "beverages": (0.08, 0.13),
    "household": (0.12, 0.18),
    "snacks": (0.14, 0.20),
    "confectionery": (0.15, 0.22),
    "personal_care": (0.16, 0.23),
    "spices": (0.18, 0.28),
}


def _sku_fraction(sku: str) -> float:
    """A stable 0–1 value derived from the SKU.

    Deliberately not :func:`hash`, which is salted per process — two runs would otherwise price
    the same shelf differently and the demo would stop being reproducible.
    """
    total = sum(ord(char) * (index + 1) for index, char in enumerate(sku))
    return (total % 997) / 997.0


def at_trade_margin(
    items: tuple[CatalogItem, ...],
    margin_targets: dict[str, tuple[float, float]],
) -> tuple[CatalogItem, ...]:
    """Re-cost every SKU so its category lands inside its ``margin_targets`` band.

    Public because every shop-type catalogue module prices itself through the same rule —
    authored MRPs stay, costs are derived, so margin analysis behaves identically whatever
    the shelf holds.
    """
    priced: list[CatalogItem] = []
    for item in items:
        low, high = margin_targets[item.category]
        margin = low + (high - low) * _sku_fraction(item.sku)
        cost = max(1, int(round(item.sell_paise * (1.0 - margin))))
        priced.append(replace(item, cost_paise=cost))
    return tuple(priced)


def _at_trade_margin(items: tuple[CatalogItem, ...]) -> tuple[CatalogItem, ...]:
    """Back-compat shim over :func:`at_trade_margin` with the kirana bands."""
    return at_trade_margin(items, CATEGORY_MARGIN_TARGET)


CATALOG: Final[tuple[CatalogItem, ...]] = _at_trade_margin(_LISTED)


@dataclass(frozen=True, slots=True)
class ShopCatalog:
    """Everything category-shaped about one *type* of shop, bundled.

    The generator reads only this object (via ``SeedProfile.catalog_key``), so a pharmacy and
    a mobile-accessories counter run through exactly the same engine as the kirana — different
    shelf, same physics. Every table is keyed on ``categories`` and the same invariants hold:
    ``shares`` sums to 1.0, every ``co_occurrence`` row sums to 1.0, every category has a
    margin band and a Hindi label.
    """

    key: str
    categories: tuple[str, ...]
    labels_hi: dict[str, str]
    shares: dict[str, float]
    co_occurrence: dict[str, dict[str, float]]
    margin_targets: dict[str, tuple[float, float]]
    items: tuple[CatalogItem, ...]

    def validate(self) -> None:
        """Assert the cross-table invariants; raises ``ValueError`` with the first breach."""
        cats = set(self.categories)
        if abs(sum(self.shares.values()) - 1.0) > 1e-6:
            raise ValueError(f"{self.key}: shares sum to {sum(self.shares.values())}")
        for table_name, keys in (
            ("labels_hi", set(self.labels_hi)),
            ("shares", set(self.shares)),
            ("co_occurrence", set(self.co_occurrence)),
            ("margin_targets", set(self.margin_targets)),
        ):
            if keys != cats:
                raise ValueError(f"{self.key}: {table_name} keys != categories")
        for anchor, row in self.co_occurrence.items():
            if set(row) != cats:
                raise ValueError(f"{self.key}: co_occurrence[{anchor}] keys != categories")
            if abs(sum(row.values()) - 1.0) > 1e-6:
                raise ValueError(f"{self.key}: co_occurrence[{anchor}] sums to {sum(row.values())}")
        for item in self.items:
            if item.category not in cats:
                raise ValueError(f"{self.key}: SKU {item.sku} has unknown category {item.category}")
        for category in self.categories:
            if not any(item.category == category for item in self.items):
                raise ValueError(f"{self.key}: category {category} has no SKUs")


KIRANA_CATALOG: Final[ShopCatalog] = ShopCatalog(
    key="kirana",
    categories=CATEGORIES,
    labels_hi=CATEGORY_LABELS_HI,
    shares=CATEGORY_SHARES,
    co_occurrence=CO_OCCURRENCE,
    margin_targets=CATEGORY_MARGIN_TARGET,
    items=CATALOG,
)


def get_catalog(key: str) -> ShopCatalog:
    """The :class:`ShopCatalog` for a shop type; imports lazily so each shelf stays optional."""
    if key == "kirana":
        return KIRANA_CATALOG
    if key == "pharmacy":
        from munshiji.seed.catalog_pharmacy import PHARMACY_CATALOG

        return PHARMACY_CATALOG
    if key == "mobile":
        from munshiji.seed.catalog_mobile import MOBILE_CATALOG

        return MOBILE_CATALOG
    raise KeyError(f"unknown shop catalogue: {key!r}")

_BY_SKU: Final[dict[str, CatalogItem]] = {item.sku: item for item in CATALOG}
_BY_CATEGORY: Final[dict[str, tuple[CatalogItem, ...]]] = {
    category: tuple(item for item in CATALOG if item.category == category)
    for category in CATEGORIES
}


def by_sku(sku: str) -> CatalogItem:
    """Look up a catalogue item by SKU; raises ``KeyError`` if unknown."""
    return _BY_SKU[sku]


def by_category(category: str) -> tuple[CatalogItem, ...]:
    """Every catalogue item in ``category`` (empty tuple for an unknown category)."""
    return _BY_CATEGORY.get(category, ())


def perishables() -> tuple[CatalogItem, ...]:
    """Every SKU with a shelf life — the population for expiry-risk analysis."""
    return tuple(item for item in CATALOG if item.is_perishable)
