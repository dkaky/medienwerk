"""Geloeschte Varianten auf eBay uebertragen – und die SKU-Zuordnung dabei stabil halten.

Bug (Fund 27.07., Sichtschutzfolie): `delete_variant` entfernte die Variante NUR lokal. Das
Dashboard verwies auf ein „neu veroeffentlichen“, das es gar nicht gab -> die Variante blieb
bei eBay kaufbar. Schlimmer: eBay-SKUs wurden ueberall positionsbasiert abgeleitet
(``AE-x-V{i}``). Nach dem Loeschen der 3. von 5 Varianten rutschten alle folgenden eine
Nummer hoch, d.h. Preis-/Bestands-Pushes und Gruppen-Updates trafen die FALSCHE Variante.

Fix in zwei Teilen:
1. ``variant_ebay_sku`` – die beim Publish festgeschriebene SKU schlaegt die Position;
   ``bind_live_variant_skus`` traegt sie fuer Altbestand ueber die Achsen-WERTE von eBay nach.
2. ``sync_variants_live`` – verwaiste SKUs auf Menge 0 (verlaesslich) und danach best effort
   ganz aus der Gruppe (laesst eBay bei verkauften Variationen nicht mehr zu).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.models import Listing, Product
from app.retry import PersistentError
from app.services import golive_service

_SIZES = ["35 x 100 cm", "40 x 100 cm", "35 x 500 cm", "40 x 500 cm", "40 x 200 cm"]


def _folie(db, *, sizes=None, live=True):
    """Sichtschutzfolie: EINE Achse 'Größe' mit 5 Werten (der reale Fall aus dem Dashboard)."""
    sizes = list(sizes if sizes is not None else _SIZES)
    p = Product(
        aliexpress_url="https://de.aliexpress.com/item/folie.html", aliexpress_id="fo1",
        price_cny=Decimal("9"), images=["https://i/main.jpg"],
        variants={"axes": {"Größe": sizes},
                  "skus": [{"attr": f"s{i+1}", "options": {"Größe": s},
                            "price": 3.0 + i, "stock": 8}
                           for i, s in enumerate(sizes)]})
    db.add(p)
    db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-FO", title_seo="Sichtschutzfolie",
                description="d", listing_status="active", price_eur=Decimal("8.19"),
                category_id="123", ebay_draft_id="AE-FO-GRP")
    if live:
        l.ebay_item_id = "IT-FO"
    db.add(l)
    db.commit()
    return p, l


class _FakeEbay:
    """eBay mit 5 live Varianten AE-FO-V1..V5 (Aspekt 'Größe' = der jeweilige Wert)."""

    def __init__(self, *, sizes=None, group_error=None, offers=True):
        sizes = list(sizes if sizes is not None else _SIZES)
        self._items = {f"AE-FO-V{i+1}": {"Größe": [s], "Marke": ["Markenlos"]}
                       for i, s in enumerate(sizes)}
        self._group = {"variantSKUs": list(self._items),
                       "variesBy": {"specifications": [{"name": "Größe", "values": sizes}]}}
        self._group_error = group_error
        self._offers = offers
        self.bulk_updates = []
        self.created_group = None
        self.published = 0

    async def get_inventory_item(self, sku):
        asp = self._items.get(sku)
        return {"product": {"aspects": asp}} if asp else None

    async def get_inventory_item_group(self, gk):
        return self._group

    async def _first_offer_for_sku(self, sku):
        return {"offerId": f"OF-{sku}"} if self._offers else None

    async def bulk_update_price(self, updates):
        self.bulk_updates.extend(updates)

    async def get_required_aspects(self, cat):
        return []

    async def build_aspects(self, cat, base):
        return {k: (v if isinstance(v, list) else [v]) for k, v in (base or {}).items()}

    async def create_inventory_item_group(self, gk, **kw):
        self.created_group = {"group_key": gk, **kw}

    async def publish_offer_by_inventory_item_group(self, gk):
        if self._group_error:
            raise RuntimeError(self._group_error)
        self.published += 1
        return "IT-FO"


def _patch(monkeypatch, ebay):
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: ebay)


def _delete(db, listing, attr):
    return golive_service.delete_variant(db, listing_id=listing.id, attr=attr)


# ------------------------------------------------------------------ variant_ebay_sku
def test_sku_prefers_pinned_over_position():
    """Festgeschriebene SKU schlaegt die Position – sonst gaebe es keinen Schutz."""
    v = {"attr": "s4", "ebay_sku": "AE-FO-V4"}
    assert golive_service.variant_ebay_sku("AE-FO", v, 3) == "AE-FO-V4"
    assert golive_service.variant_ebay_sku("AE-FO", {"attr": "s4"}, 3) == "AE-FO-V3"


def test_stock_and_price_follow_pinned_sku(db):
    """DER Kern-Bug: nach dem Loeschen der 3. Variante duerfen die uebrigen NICHT nachruecken."""
    p, l = _folie(db)
    for i, v in enumerate(p.variants["skus"], 1):     # Publish-Zustand nachstellen
        v["ebay_sku"] = f"AE-FO-V{i}"
    _delete(db, l, "s3")
    db.refresh(p)

    state = golive_service.variant_stock_state(l, p)
    assert [s["ebay_sku"] for s in state] == ["AE-FO-V1", "AE-FO-V2", "AE-FO-V4", "AE-FO-V5"]
    # '40 x 500 cm' behaelt V4 – ohne Fix bekaeme es V3, also die SKU von '35 x 500 cm'.
    assert state[2]["ebay_sku"] == "AE-FO-V4"
    assert "40 x 500" in state[2]["name"]

    prices = golive_service.compute_variant_prices(l, p, __import__(
        "app.config", fromlist=["get_settings"]).get_settings())
    assert [x["sku"] for x in prices] == ["AE-FO-V1", "AE-FO-V2", "AE-FO-V4", "AE-FO-V5"]


def test_legacy_without_pinned_sku_still_positional(db):
    """Altbestand ohne festgeschriebene SKU behaelt das alte Verhalten (kein Datenbruch)."""
    p, l = _folie(db)
    state = golive_service.variant_stock_state(l, p)
    assert [s["ebay_sku"] for s in state] == [f"AE-FO-V{i}" for i in range(1, 6)]


# ------------------------------------------------------------------ bind_live_variant_skus
def test_binding_recovers_true_skus_after_delete(db, monkeypatch):
    """Altbestand OHNE ebay_sku: die echte Zuordnung kommt ueber die Achsenwerte von eBay."""
    p, l = _folie(db)
    _delete(db, l, "s3")                       # '35 x 500 cm' raus (Position 3 von 5)
    db.refresh(p)
    ebay = _FakeEbay()
    r = asyncio.run(golive_service.bind_live_variant_skus(ebay, db, l, p))
    assert r["orphans"] == ["AE-FO-V3"]
    assert r["bound"] == {"s1": "AE-FO-V1", "s2": "AE-FO-V2",
                          "s4": "AE-FO-V4", "s5": "AE-FO-V5"}
    assert [v.get("ebay_sku") for v in p.variants["skus"]] == [
        "AE-FO-V1", "AE-FO-V2", "AE-FO-V4", "AE-FO-V5"]


def test_binding_refuses_when_values_ambiguous(db, monkeypatch):
    """Zwei live Varianten mit demselben Wert -> nicht unterscheidbar -> Abbruch statt Raten."""
    p, l = _folie(db, sizes=["A", "B"])
    ebay = _FakeEbay(sizes=["A", "A"])
    with pytest.raises(PersistentError, match="nicht eindeutig"):
        asyncio.run(golive_service.bind_live_variant_skus(ebay, db, l, p))


def test_binding_refuses_when_items_unreadable(db):
    """Unvollstaendig lesbare Live-Daten duerfen nie Grundlage einer Zuordnung sein."""
    p, l = _folie(db)
    ebay = _FakeEbay()
    ebay._items["AE-FO-V2"] = {}               # ein Item liefert keine Aspekte
    with pytest.raises(PersistentError, match="nicht vollstaendig lesbar"):
        asyncio.run(golive_service.bind_live_variant_skus(ebay, db, l, p))


def test_binding_refuses_when_local_variant_not_live(db):
    """Lokale Variante, die es live nicht gibt -> Abbruch (Zustand ist nicht verstanden)."""
    p, l = _folie(db)
    ebay = _FakeEbay(sizes=_SIZES[:4] + ["99 x 99 cm"])
    with pytest.raises(PersistentError, match="nicht eindeutig"):
        asyncio.run(golive_service.bind_live_variant_skus(ebay, db, l, p))


# ------------------------------------------------------------------ sync_variants_live
def test_sync_zeroes_and_removes_orphan(db, monkeypatch):
    p, l = _folie(db)
    _delete(db, l, "s3")
    ebay = _FakeEbay()
    _patch(monkeypatch, ebay)

    r = asyncio.run(golive_service.sync_variants_live(db, listing_id=l.id))

    assert r["changed"] is True and r["removed"] == ["AE-FO-V3"]
    # 1) Menge 0 auf GENAU der verwaisten SKU – die uebrigen bleiben unangetastet.
    assert ebay.bulk_updates == [{"sku": "AE-FO-V3", "offer_id": "OF-AE-FO-V3", "quantity": 0}]
    # 2) Gruppe ohne die verwaiste SKU, die uebrigen behalten ihre ECHTEN Namen.
    assert r["removed_from_group"] is True and ebay.published == 1
    assert ebay.created_group["variant_skus"] == [
        "AE-FO-V1", "AE-FO-V2", "AE-FO-V4", "AE-FO-V5"]
    assert ebay.created_group["specifications"][0]["values"] == [
        "35 x 100 cm", "40 x 100 cm", "40 x 500 cm", "40 x 200 cm"]
    # 3) Bestands-Spiegel wird sofort FRISCH gegen die korrekten SKUs gerechnet (statt
    #    auf den 6h-Monitor zu warten) - die verwaiste V3 taucht darin nicht mehr auf.
    db.refresh(l)
    assert l.variant_stock is not None and "AE-FO-V3" not in l.variant_stock
    assert set(l.variant_stock) == {"AE-FO-V1", "AE-FO-V2", "AE-FO-V4", "AE-FO-V5"}


def test_sync_keeps_quantity_zero_when_ebay_refuses_removal(db, monkeypatch):
    """Verkaufte Variation laesst eBay nicht mehr entfernen – Menge 0 muss trotzdem stehen."""
    p, l = _folie(db)
    _delete(db, l, "s3")
    ebay = _FakeEbay(group_error="25013 variation with sales cannot be removed")
    _patch(monkeypatch, ebay)

    r = asyncio.run(golive_service.sync_variants_live(db, listing_id=l.id))

    assert r["changed"] is True and r["removed_from_group"] is False
    assert r["quantity_zeroed"] == ["AE-FO-V3"]
    assert ebay.bulk_updates[0]["quantity"] == 0        # zuerst genullt -> nicht mehr kaufbar
    assert "nicht mehr kaufbar" in r["message"]
    assert "25013" in (r["group_error"] or "")


def test_sync_without_orphans_keeps_group_untouched(db, monkeypatch):
    """Nichts geloescht -> KEIN Gruppen-Umbau/Republish. Seit 09.08. laeuft aber der
    Ausverkauft-Mengen-Abgleich mit (Nutzer-Fund Pokemon-Poster: der Button meldete
    frueher 'stimmen ueberein', ohne die Mengen je anzufassen)."""
    p, l = _folie(db)
    ebay = _FakeEbay()
    _patch(monkeypatch, ebay)

    r = asyncio.run(golive_service.sync_variants_live(db, listing_id=l.id))

    assert r["removed"] == [] and r["removed_from_group"] is False
    assert ebay.bulk_updates == [] and ebay.created_group is None and ebay.published == 0
    assert "oos_sync" in r, "Mengen-Abgleich muss mitlaufen und gemeldet werden"
    # Die Zuordnung wird trotzdem nachgetragen (heilt Altbestand still).
    db.refresh(p)
    assert [v["ebay_sku"] for v in p.variants["skus"]] == [f"AE-FO-V{i}" for i in range(1, 6)]


def test_sync_changes_nothing_when_binding_fails(db, monkeypatch):
    """Mehrdeutige Zuordnung -> Abbruch VOR jedem eBay-Schreibzugriff."""
    p, l = _folie(db)
    _delete(db, l, "s3")
    ebay = _FakeEbay()
    ebay._items["AE-FO-V5"] = {}               # nicht vollstaendig lesbar
    _patch(monkeypatch, ebay)

    with pytest.raises(PersistentError):
        asyncio.run(golive_service.sync_variants_live(db, listing_id=l.id))
    assert ebay.bulk_updates == [] and ebay.created_group is None and ebay.published == 0


def test_sync_rejects_non_live_listing(db, monkeypatch):
    p, l = _folie(db, live=False)
    _patch(monkeypatch, _FakeEbay())
    with pytest.raises(PersistentError, match="aktiven Multivarianten-Artikel"):
        asyncio.run(golive_service.sync_variants_live(db, listing_id=l.id))


def test_publish_pins_skus_on_the_product(db):
    """Quelle der Stabilitaet: der Publish schreibt die SKU an jede Variante."""
    from app.config import get_settings

    p, l = _folie(db, live=False)

    class _PublishEbay(_FakeEbay):
        async def create_inventory_item(self, sku, **kw):
            pass

        async def create_offer(self, sku, **kw):
            pass

        async def delete_inventory_item(self, sku):
            pass

        async def withdraw_offer(self, offer_id):
            pass

        async def _first_offer_for_sku(self, sku):
            return None            # kein kollidierendes Einzel-Angebot

    ebay = _PublishEbay()
    axis_names, variants = golive_service._usable_variants(p)
    asyncio.run(golive_service._publish_multi(
        ebay, l, p, category="123", aspects={"Marke": "Markenlos"}, brand="Markenlos",
        axis_names=axis_names, variants=variants, base_sku="AE-FO",
        settings=get_settings()))
    db.commit()
    db.refresh(p)
    assert [v["ebay_sku"] for v in p.variants["skus"]] == [f"AE-FO-V{i}" for i in range(1, 6)]

    # ... und nach dem Loeschen bleibt die Zuordnung der uebrigen unveraendert.
    _delete(db, l, "s2")
    db.refresh(p)
    assert [v["ebay_sku"] for v in p.variants["skus"]] == [
        "AE-FO-V1", "AE-FO-V3", "AE-FO-V4", "AE-FO-V5"]


def test_delete_variant_note_warns_still_buyable(db):
    """Der Hinweis darf nicht mehr auf ein 'neu veroeffentlichen' zeigen, das es nicht gibt."""
    p, l = _folie(db)
    r = _delete(db, l, "s3")
    assert r["needs_republish"] is True
    assert "KAUFBAR" in r["note"] and "eBay uebertragen" in r["note"]
