"""Namens-Reparatur Option A (Vorfall Zirkonia-Kette 09.08.) + Bestands-Scan.

Kern: eBay laesst Variationsnamen nicht aendern (25013) -> korrekte Quell-Varianten
werden ZUSAETZLICH angehaengt, falsch benannte auf Menge 0 gesetzt. Nichts wird
beendet; Listing/Historie bleiben.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.models import Listing, Product
from app.retry import PersistentError
from app.services import golive_service


class _FakeEbay:
    def __init__(self, group, items=None, offers=None, variations=None):
        self._group, self._items = group, (items or {})
        self._offers, self._variations = (offers or {}), (variations or [])
        self.created_items: dict = {}
        self.created_offers: dict = {}
        self.created_group = None
        self.published = 0
        self.bulk: list = []

    async def get_inventory_item_group(self, gk):
        return self._group

    async def create_inventory_item(self, sku, **kw):
        self.created_items[sku] = kw

    async def create_offer(self, sku, **kw):
        self.created_offers[sku] = kw
        return "OF-" + sku

    async def build_aspects(self, category_id, aspects):
        return dict(aspects)

    async def create_inventory_item_group(self, gk, **kw):
        self.created_group = kw

    async def publish_offer_by_inventory_item_group(self, gk):
        self.published += 1

    async def get_inventory_item(self, sku):
        return self._items.get(sku)

    async def _first_offer_for_sku(self, sku):
        oid = self._offers.get(sku)
        return {"offerId": oid} if oid else None

    async def bulk_update_price(self, updates):
        self.bulk.append(updates)

    async def get_item_price_info(self, item_id):
        return {"variations": self._variations}


def _zirkonia(db, *, axes=None, skus=None):
    # REALER Zirkonia-Zustand: options tragen die beim Upload GERMANISIERTEN (erfundenen)
    # Farbnamen — die WAHRHEIT (Nummern) steht nur im attr nach '#'. Die Reparatur muss
    # die Nummern anhaengen, nicht die erfundenen Namen erneut.
    skus = skus or [
        {"attr": "z1#1", "options": {"Farbe": "Gold"}, "price": "5", "stock": 9,
         "image": "https://img/1.jpg"},
        {"attr": "z2#2", "options": {"Farbe": "Silber"}, "price": "6", "stock": 0},
    ]
    p = Product(aliexpress_url="https://de.aliexpress.com/item/zirkonia.html",
                aliexpress_id="zk1", price_cny=Decimal("5"),
                variants={"axes": axes or {"Farbe": ["Gold", "Silber"]}, "skus": skus})
    db.add(p)
    db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-ZK", title_seo="Zirkonia Kette",
                description="d", listing_status="active", ebay_item_id="110001",
                ebay_draft_id="AE-ZK-GRP", price_eur=Decimal("19.95"),
                cost_eur=Decimal("6"), category_id="123")
    db.add(l)
    db.commit()
    return l, p


_GROUP = {"variantSKUs": ["AE-ZK-V1", "AE-ZK-V2"],
          "variesBy": {"specifications": [{"name": "Farbe",
                                           "values": ["Gold", "Silber"]}],
                       "aspectsImageVariesBy": ["Farbe"]}}
_ITEMS = {"AE-ZK-V1": {"product": {"aspects": {"Farbe": ["Gold"]}}},
          "AE-ZK-V2": {"product": {"aspects": {"Farbe": ["Silber"]}}}}
_OFFERS = {"AE-ZK-V1": "OF-V1", "AE-ZK-V2": "OF-V2"}


def test_append_adds_source_variants_and_zeroes_invented(db, monkeypatch):
    l, p = _zirkonia(db)
    ebay = _FakeEbay(_GROUP, _ITEMS, _OFFERS)
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: ebay)

    r = asyncio.run(golive_service.append_corrected_variants(db, listing_id=l.id))

    assert r["appended"] == 2
    # Neue Items/Offers mit den ORIGINAL-Namen der Quelle; ausverkaufte Variante -> Menge 0.
    assert set(ebay.created_items) == {"AE-ZK-R1", "AE-ZK-R2"}
    assert ebay.created_items["AE-ZK-R1"]["aspects"] == {"Farbe": ["1"]}, \
        "eBay verlangt Aspect-Werte als Liste (Fehler 2004 beim Echtlauf 758)"
    assert ebay.created_items["AE-ZK-R2"]["quantity"] == 0
    # Gruppe = ALTE SKUs/Werte + neue (nichts entfernt -> kein 25013).
    assert ebay.created_group["variant_skus"] == ["AE-ZK-V1", "AE-ZK-V2",
                                                  "AE-ZK-R1", "AE-ZK-R2"]
    assert ebay.created_group["specifications"] == [
        {"name": "Farbe", "values": ["Gold", "Silber", "1", "2"]}]
    assert ebay.published == 1
    # Erfundene Namen (Gold/Silber existieren in der Quelle nicht) -> Menge 0.
    assert r["zeroed"] == ["AE-ZK-V1", "AE-ZK-V2"]
    assert ebay.bulk == [[{"sku": "AE-ZK-V1", "offer_id": "OF-V1", "quantity": 0},
                          {"sku": "AE-ZK-V2", "offer_id": "OF-V2", "quantity": 0}]]
    # Neue SKUs sind an den Quell-Varianten gepinnt; Bestands-Spiegel geleert.
    db.refresh(p); db.refresh(l)
    assert [v.get("ebay_sku") for v in p.variants["skus"]] == ["AE-ZK-R1", "AE-ZK-R2"]
    assert l.variant_stock is None


def test_append_keeps_correctly_named_live_variants(db, monkeypatch):
    l, p = _zirkonia(db)
    group = {"variantSKUs": ["AE-ZK-V1", "AE-ZK-V2"],
             "variesBy": {"specifications": [{"name": "Farbe",
                                              "values": ["Gold", "2"]}],
                          "aspectsImageVariesBy": None}}
    items = {"AE-ZK-V1": {"product": {"aspects": {"Farbe": ["Gold"]}}},
             "AE-ZK-V2": {"product": {"aspects": {"Farbe": ["2"]}}}}
    ebay = _FakeEbay(group, items, _OFFERS)
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: ebay)

    r = asyncio.run(golive_service.append_corrected_variants(db, listing_id=l.id))

    assert r["appended"] == 1 and set(ebay.created_items) == {"AE-ZK-R1"}
    # Nur die ERFUNDENE Variante (Gold) wird genullt — "2" ist korrekt benannt.
    assert r["zeroed"] == ["AE-ZK-V1"]


def test_append_resumes_half_finished_repair(db, monkeypatch):
    """Echtlauf 758: eBay uebernahm die Gruppe trotz 400 — ein Folgelauf muss die
    restlichen Schritte (Publish, Nullung, Pinning) zu Ende fuehren, ohne neu anzulegen."""
    l, p = _zirkonia(db)
    group = {"variantSKUs": ["AE-ZK-V1", "AE-ZK-V2", "AE-ZK-R1", "AE-ZK-R2"],
             "variesBy": {"specifications": [{"name": "Farbe",
                                              "values": ["Gold", "Silber", "1", "2"]}],
                          "aspectsImageVariesBy": None}}
    ebay = _FakeEbay(group, _ITEMS, _OFFERS)
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: ebay)

    r = asyncio.run(golive_service.append_corrected_variants(db, listing_id=l.id))

    assert r["appended"] == 0 and r["resumed"] == 2
    assert ebay.created_items == {}, "keine neuen Items anlegen"
    # Gruppe wird auch beim Resume neu geschrieben (heilt fehlende Pflicht-Merkmale,
    # Publish-25002 im Echtlauf 758) — SKUs/Werte bleiben unveraendert.
    assert ebay.created_group["variant_skus"] == ["AE-ZK-V1", "AE-ZK-V2",
                                                  "AE-ZK-R1", "AE-ZK-R2"]
    assert ebay.created_group["specifications"] == [
        {"name": "Farbe", "values": ["Gold", "Silber", "1", "2"]}]
    assert ebay.published == 1, "Publish muss nachgeholt werden"
    assert r["zeroed"] == ["AE-ZK-V1", "AE-ZK-V2"], "erfundene Namen jetzt nullen"
    db.refresh(p); db.refresh(l)
    assert [v.get("ebay_sku") for v in p.variants["skus"]] == ["AE-ZK-R1", "AE-ZK-R2"]
    assert l.variant_stock is None


def test_append_refuses_multi_axis(db, monkeypatch):
    skus = [{"attr": "m1", "options": {"Farbe": "1", "Größe": "20cm"}, "price": "5", "stock": 5},
            {"attr": "m2", "options": {"Farbe": "2", "Größe": "25cm"}, "price": "5", "stock": 5}]
    l, _p = _zirkonia(db, axes={"Farbe": ["1", "2"], "Größe": ["20cm", "25cm"]}, skus=skus)
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: _FakeEbay(_GROUP, _ITEMS, _OFFERS))
    with pytest.raises(PersistentError, match="EIN-Achsen"):
        asyncio.run(golive_service.append_corrected_variants(db, listing_id=l.id))


def test_variant_name_audit_flags_invented_names(db, monkeypatch):
    # Import-Szenario: lokale Quelle traegt die ROHEN Nummern, eBay erfundene Farben.
    l, _p = _zirkonia(db, axes={"Farbe": ["1", "2"]}, skus=[
        {"attr": "z1#1", "options": {"Farbe": "1"}, "price": "5", "stock": 9},
        {"attr": "z2#2", "options": {"Farbe": "2"}, "price": "6", "stock": 0}])
    variations = [{"sku": "AE-ZK-V1", "specifics": [("Farbe", "Gold")]},
                  {"sku": "AE-ZK-V2", "specifics": [("Farbe", "Silber")]}]
    ebay = _FakeEbay(_GROUP, _ITEMS, _OFFERS, variations=variations)
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: ebay)

    r = asyncio.run(golive_service.variant_name_audit(db))
    assert r["suspicious"] == 1
    hit = r["listings"][0]
    assert hit["listing_id"] == l.id and hit["unmapped"] == 2
    assert "Gold" in " ".join(hit["beispiele"])
