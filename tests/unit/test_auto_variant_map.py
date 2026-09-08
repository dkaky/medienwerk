"""Stapel-Zuordnung eBay-Variationen -> Quell-SKUs (Nutzerwunsch 09.08., Cuban Chain).

Kern: fail-closed. Masse entscheiden deterministisch (8mm != 12mm), Ties bleiben offen,
die KI-Stufe darf nie eine Zuordnung liefern, die ein Mass veraendert.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from app.models import Listing, Product
from app.services import golive_service


def _seed(db, *, skus, item="am1"):
    p = Product(aliexpress_url=f"https://de.aliexpress.com/item/automap-{item}.html",
                aliexpress_id="am" + item, price_cny=Decimal("5"),
                variants={"axes": {"Größe": [list(s["options"].values())[0] for s in skus]},
                          "skus": skus})
    db.add(p)
    db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-" + item, title_seo="AutoMap " + item,
                description="d", listing_status="active", ebay_item_id="11000" + item)
    db.add(l)
    db.commit()
    return l, p


class _FakeEbay:
    def __init__(self, variations):
        self._variations = variations

    async def get_item_price_info(self, item_id):
        return {"variations": self._variations}


async def _no_llm(db, *, product, listing, variant_selected):
    return None, "none"


def test_auto_map_by_measurements_and_idempotent(db, monkeypatch):
    # Optionstexte OHNE Wert-Ueberlappung zur eBay-Variante (Stufe 1 greift nicht) —
    # die Masse stecken nur im attr (Wahrheit nach '#') -> Stufe 2 muss entscheiden.
    skus = [{"attr": "s1#8mm 20cm", "options": {"Größe": "Type A"}, "price": "5", "stock": 5},
            {"attr": "s2#12mm 20cm", "options": {"Größe": "Type B"}, "price": "6", "stock": 5}]
    l, _p = _seed(db, skus=skus)
    variations = [{"sku": "uuid-a", "specifics": [("Größe", "8mm-20cm Silber")]},
                  {"sku": "uuid-b", "specifics": [("Größe", "12mm-20cm Silber")]}]
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: _FakeEbay(variations))
    from app.services import order_service
    monkeypatch.setattr(order_service, "resolve_variant_smart", _no_llm)

    r = asyncio.run(golive_service.auto_map_ebay_variations(db, listing_id=l.id))
    assert r["mapped"] == 2 and r["open"] == [], r
    db.refresh(l)
    assert set((l.variant_map or {}).values()) >= {"s1#8mm 20cm", "s2#12mm 20cm"}

    # Zweiter Lauf: alles schon bekannt (gelernte Map) -> idempotent, nichts Neues.
    r2 = asyncio.run(golive_service.auto_map_ebay_variations(db, listing_id=l.id))
    assert r2["already"] == 2 and r2["mapped"] == 0


def test_auto_map_stays_open_on_measurement_tie(db, monkeypatch):
    # Zwei Quell-SKUs mit IDENTISCHEN Massen -> Tie -> offen lassen, nie raten.
    skus = [{"attr": "t1#8mm 20cm", "options": {"Größe": "8mm 20cm gold"}, "price": "5", "stock": 5},
            {"attr": "t2#8mm 20cm", "options": {"Größe": "8mm 20cm silber"}, "price": "5", "stock": 5}]
    l, _p = _seed(db, skus=skus, item="tie")
    variations = [{"sku": "uuid-t", "specifics": [("Größe", "8mm-20cm")]}]
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: _FakeEbay(variations))
    from app.services import order_service
    monkeypatch.setattr(order_service, "resolve_variant_smart", _no_llm)

    r = asyncio.run(golive_service.auto_map_ebay_variations(db, listing_id=l.id))
    assert r["mapped"] == 0 and len(r["open"]) == 1
    db.refresh(l)
    assert not (l.variant_map or {}), "bei Mehrdeutigkeit darf nichts gelernt werden"


def test_auto_map_llm_blocked_when_measurement_would_change(db, monkeypatch):
    # KI schlaegt eine Variante mit ANDEREM Mass vor -> Guard verwirft (Regel 9).
    skus = [{"attr": "g1#12mm", "options": {"Größe": "12mm"}, "price": "5", "stock": 5},
            {"attr": "g2#kein-mass", "options": {"Größe": "Classic"}, "price": "5", "stock": 5}]
    l, _p = _seed(db, skus=skus, item="llm")
    variations = [{"sku": "uuid-g", "specifics": [("Größe", "8mm Silber")]}]
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: _FakeEbay(variations))
    from app.services import order_service

    async def _bad_llm(db, *, product, listing, variant_selected):
        return {"attr": "g1#12mm", "options": {}}, "llm"     # 8mm -> 12mm waere falsche Ware
    monkeypatch.setattr(order_service, "resolve_variant_smart", _bad_llm)

    r = asyncio.run(golive_service.auto_map_ebay_variations(db, listing_id=l.id))
    assert r["mapped"] == 0 and len(r["open"]) == 1, "Mass-Guard muss den KI-Vorschlag blocken"
