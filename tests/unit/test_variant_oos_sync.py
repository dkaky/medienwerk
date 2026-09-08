"""Ausverkauft-Abgleich & SKU-Guard (Nutzer-Fund 09.08.: Pokemon-Poster).

1. _safe_ebay_update darf keine leeren Update-Eintraege bauen (offers ohne Preis UND
   Menge lehnt eBay je SKU ab -> ganzer Batch kaputt) und muss fail-closed abbrechen,
   wenn KEIN Ziel-Schluessel eine Live-SKU trifft (sonst vergiftet der Aufrufer den
   variant_stock-Spiegel mit Phantom-Schluesseln -> nie wieder ein Retry).
2. Stufe C der Preis-Dialog-Zuordnung: eBay-Merkmalswerte werden ueber die
   Bestell-Maschinerie auf Quell-SKUs aufgeloest (fail-closed, keine Schaetzung) —
   damit bekommen importierte Listings endlich einen Varianten-EK statt "?".
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services import monitoring_service as ms
from app.services.golive_service import _map_variations_to_internal


class _QtyEbay:
    def __init__(self, offers):
        self._offers = offers          # {sku: offer_id}
        self.bulk = []

    async def _first_offer_for_sku(self, sku):
        oid = self._offers.get(sku)
        return {"offerId": oid} if oid else None

    async def bulk_update_price(self, updates):
        self.bulk.append(updates)

    async def revise_item_status(self, *a, **k):
        raise AssertionError("Trading-Fallback darf mit Inventory-Offers nicht laufen")


def _listing():
    return SimpleNamespace(ebay_sku="AE-X", ebay_item_id="110000000001")


def test_qty_push_fails_closed_on_phantom_keys(monkeypatch):
    from app.services import golive_service

    async def fake_skus(ebay, listing):
        return ["AE-X-V1", "AE-X-V2"]
    monkeypatch.setattr(golive_service, "_listing_variant_skus", fake_skus)
    ebay = _QtyEbay({"AE-X-V1": "OF-1", "AE-X-V2": "OF-2"})

    ok = asyncio.run(ms._safe_ebay_update(
        ebay, True, _listing(), quantity_by_sku={"OLD-V1": 0, "OLD-V2": 0}))

    assert ok is False, "Phantom-Schluessel duerfen nie als Erfolg gelten"
    assert ebay.bulk == []


def test_qty_push_skips_empty_entries(monkeypatch):
    from app.services import golive_service

    async def fake_skus(ebay, listing):
        return ["AE-X-V1", "AE-X-V2", "AE-X-V3"]
    monkeypatch.setattr(golive_service, "_listing_variant_skus", fake_skus)
    ebay = _QtyEbay({s: f"OF-{s}" for s in ("AE-X-V1", "AE-X-V2", "AE-X-V3")})

    ok = asyncio.run(ms._safe_ebay_update(
        ebay, True, _listing(), quantity_by_sku={"AE-X-V2": 0}))

    assert ok is True
    assert ebay.bulk == [[{"sku": "AE-X-V2", "offer_id": "OF-AE-X-V2",
                           "price_eur": None, "quantity": 0}]], \
        "NUR die geaenderte SKU, keine leeren Eintraege fuer die uebrigen"


# ---------------- Stufe C: eBay-Merkmalswerte -> Quell-SKUs (Bestell-Maschinerie) ----

def _stage_c_fixture(color_options):
    # Importiertes Listing: fremde UUID-SKUs (kein {base}-V{i}), keine axis_options
    # im internen Modell -> Stufen A und B greifen beide nicht.
    variations = [
        {"sku": "uuid-abc", "specifics": [("Farbe", "Rot")]},
        {"sku": "uuid-def", "specifics": [("Farbe", "Blau")]},
    ]
    skus = [{"attr": f"14:{i}#{c}", "options": {"Farbe": c}}
            for i, c in enumerate(color_options, 1)]
    internal_rows = [{"sku": f"I{i}", "axis_options": {}, "ek_eur": 3.0 + i,
                      "ae_price_eur": None, "attr": s["attr"]}
                     for i, s in enumerate(skus, 1)]
    listing = SimpleNamespace(variant_map={})
    product = SimpleNamespace(variants={"skus": skus}, aliexpress_id="P1")
    return variations, internal_rows, listing, product


def test_stage_c_maps_by_value_matching():
    variations, rows, listing, product = _stage_c_fixture(["Rot", "Blau"])
    out = _map_variations_to_internal(variations, rows, listing, product)
    assert out[0]["attr"] == "14:1#Rot"
    assert out[1]["attr"] == "14:2#Blau"


def test_stage_c_stays_closed_on_ambiguity():
    # Zwei Quell-SKUs mit demselben Wert -> Score-Tie -> KEINE Zuordnung (EK bleibt "?"),
    # lieber ehrlich unbekannt als ein moeglicherweise falscher EK.
    variations, rows, listing, product = _stage_c_fixture(["Rot", "Rot"])
    out = _map_variations_to_internal(variations, rows, listing, product)
    assert out == {}


def test_stage_c_off_without_context():
    # Ohne listing/product (alte Aufrufer) bleibt alles beim bisherigen Verhalten.
    variations, rows, _l, _p = _stage_c_fixture(["Rot", "Blau"])
    assert _map_variations_to_internal(variations, rows) == {}


# ---------------- Klassik-Listings: Mengen je Variation ueber Trading (Listing 200) --

class _ClassicEbay:
    def __init__(self, variations):
        self._variations = variations
        self.revised = []

    async def get_item_price_info(self, item_id):
        return {"variations": self._variations}

    async def revise_variation_quantities_by_specifics(self, item_id, updates):
        self.revised.append((item_id, updates))


def test_classic_variation_qty_push_maps_and_pushes():
    listing = SimpleNamespace(id=200, ebay_item_id="389766541048", variant_map={})
    product = SimpleNamespace(aliexpress_id="P1", variants={"skus": [
        {"attr": "a1#black", "options": {"Farbe": "black"}},
        {"attr": "a2#white", "options": {"Farbe": "white"}}]})
    state = [{"ebay_sku": "AE-200-V1", "attr": "a1#black"},
             {"ebay_sku": "AE-200-V2", "attr": "a2#white"}]
    ebay = _ClassicEbay([
        {"sku": "uuid-black", "specifics": [("Farbe", "Schwarz")]},
        {"sku": "uuid-white", "specifics": [("Farbe", "Weiss")]}])

    ok, err = asyncio.run(ms._push_classic_variation_qty(
        ebay, True, listing, product, {"AE-200-V1": 20}, state))

    assert ok is True and err is None
    assert ebay.revised == [("389766541048", [([("Farbe", "Schwarz")], 20)])],         "Schwarz<->black muss ueber die Bestell-Uebersetzung auf die Live-SKU treffen"


def test_classic_variation_qty_push_fails_closed_when_unmappable():
    listing = SimpleNamespace(id=200, ebay_item_id="389766541048", variant_map={})
    product = SimpleNamespace(aliexpress_id="P1", variants={"skus": [
        {"attr": "a1#black", "options": {"Farbe": "black"}}]})
    state = [{"ebay_sku": "AE-200-V1", "attr": "a1#black"}]
    ebay = _ClassicEbay([
        {"sku": "uuid-rot", "specifics": [("Farbe", "Rot")]}])   # passt nicht

    ok, err = asyncio.run(ms._push_classic_variation_qty(
        ebay, True, listing, product, {"AE-200-V1": 20}, state))

    assert ok is False and "zuordenbar" in str(err) and ebay.revised == [],         "nicht zuordenbar -> NICHTS pushen (Spiegel nicht vergiften)"
