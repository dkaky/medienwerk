"""Tests fuer die Produkt-Tab-Ueberarbeitung (2026-07-05):
China-Herkunft-Filter, .95-Rundung, Per-Varianten-Preise, Hauptbild, Variante loeschen,
Ausverkauft->Menge 0, Variantenbilder in der Galerie."""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Listing, Product
from app.retry import PersistentError
from app.services import golive_service, pricing
from app.spec_filter import strip_forbidden_specs


# ----------------------------- China-Herkunft-Filter (F) -----------------------------
def test_strip_forbidden_specs_removes_china_origin_only():
    specs = {"Marke": "Markenlos", "Herkunftsland": "China", "Herstellungsland": "CN",
             "Ursprungsland": "Volksrepublik China", "Material": "Edelstahl",
             "Farbe": "China Red"}   # Farbe darf NICHT entfernt werden
    out = strip_forbidden_specs(specs)
    assert "Herkunftsland" not in out and "Herstellungsland" not in out
    assert "Ursprungsland" not in out
    assert out["Material"] == "Edelstahl" and out["Farbe"] == "China Red"


def test_strip_forbidden_specs_keeps_non_china_origin():
    out = strip_forbidden_specs({"Herkunftsland": "Deutschland", "Marke": "X"})
    assert out["Herkunftsland"] == "Deutschland"


# ----------------------------- .95-Rundung (A) -----------------------------
@pytest.mark.parametrize("price,expected", [
    (20.87, 20.95), (20.11, 19.95), (19.60, 19.95), (21.00, 20.95),
    (0.30, 0.95), (21.50, 21.95), (21.95, 21.95),
])
def test_round_to_nearest_95(price, expected):
    assert pricing.round_to_nearest_cents(price, 0.95) == expected


# ----------------------------- Preis je Variante (A) -----------------------------
def _variant_product(db, stocks=(9, 9)):
    p = Product(
        aliexpress_url="https://de.aliexpress.com/item/pt-1.html", aliexpress_id="pt1",
        price_cny=Decimal("6.00"), supplier_id="store-a",
        images=["https://img/main1.jpg", "https://img/main2.jpg"],
        variants={"axes": {"Farbe": ["Schwarz", "Rot"]},
                  "skus": [{"attr": "14:1", "options": {"Farbe": "Schwarz"}, "id": 1,
                            "price": 6.0, "stock": stocks[0], "image": "https://img/v-black.jpg"},
                           {"attr": "14:2", "options": {"Farbe": "Rot"}, "id": 2,
                            "price": 8.0, "stock": stocks[1], "image": "https://img/v-red.jpg"}]})
    db.add(p)
    db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-PT", title_seo="T", description="d",
                listing_status="active", price_eur=Decimal("24.95"), cost_eur=Decimal("7.00"))
    db.add(l)
    db.commit()
    return p, l


def test_preview_variant_prices_margin_and_profit(db):
    p, l = _variant_product(db)
    # Margen-Modus: jede Variante bekommt Preis mit ~25% Marge, gerundet auf x,95
    prev = asyncio.run(golive_service.preview_variant_prices(db, listing_id=l.id, mode="margin", value=25))
    assert len(prev["rows"]) == 2
    for r in prev["rows"]:
        assert r["new_price_eur"] is not None
        assert abs(round(r["new_price_eur"] % 1, 2) - 0.95) < 0.001   # Endung ,95
        assert r["new_margin"] >= 0.20     # ungefaehr Zielmarge (>=20%)
    # Gewinn-Modus: absoluter Gewinn je Variante
    prev2 = asyncio.run(golive_service.preview_variant_prices(db, listing_id=l.id, mode="profit", value=10))
    for r in prev2["rows"]:
        assert r["new_profit_eur"] >= 8.5   # ~10€ Ziel (,95-Rundung + Gebuehr inkl. MwSt)


def test_bulk_reprice_preview_hits_target_margin_across_listings(db):
    """Bulk-Marge (Vorschau) rechnet je Variante mehrerer Listings auf die Zielmarge,
    ohne live zu setzen (apply=False)."""
    p, l1 = _variant_product(db)
    l2 = Listing(product_id=p.id, ebay_sku="AE-PT2", title_seo="T2", description="d",
                 listing_status="active", price_eur=Decimal("24.95"), cost_eur=Decimal("7.00"))
    db.add(l2)
    db.commit()
    res = asyncio.run(golive_service.bulk_reprice(
        db, listing_ids=[l1.id, l2.id], mode="margin", value=25, apply=False))
    assert res["applied"] is False
    assert res["ok_count"] == 2
    assert res["variant_count"] == 4          # 2 Listings x 2 Varianten
    for entry in res["results"]:
        assert entry["ok"] is True
        assert len(entry["rows"]) == 2
        for r in entry["rows"]:
            assert r["new_price_eur"] is not None
            assert r["new_margin"] >= 0.20    # ungefaehr Zielmarge
            assert abs(round(r["new_price_eur"] % 1, 2) - 0.95) < 0.001
    # Vorschau darf NICHTS aendern (propose-only)
    assert float(db.get(Listing, l1.id).price_eur) == 24.95


def test_bulk_reprice_skips_broken_listing_and_reports_reason(db):
    """Ein defektes Listing (fehlt) bricht den Bulk-Lauf nicht ab – Rest laeuft weiter."""
    _, l1 = _variant_product(db)
    res = asyncio.run(golive_service.bulk_reprice(
        db, listing_ids=[l1.id, 999999], mode="profit", value=10, apply=False))
    assert res["ok_count"] == 1
    bad = [r for r in res["results"] if not r["ok"]]
    assert len(bad) == 1 and bad[0]["listing_id"] == 999999 and bad[0].get("reason")


def test_bulk_reprice_rejects_bad_mode(db):
    _, l1 = _variant_product(db)
    with pytest.raises(PersistentError):
        asyncio.run(golive_service.bulk_reprice(
            db, listing_ids=[l1.id], mode="totaler_quatsch", value=5, apply=False))


def test_bulk_reprice_rejects_negative_value(db):
    """Geld-Schutz: negativer Gewinn/negative Marge darf NICHT durchrutschen (wuerde sonst
    stumm auf 0,95 € kollabieren und live gepusht werden). Fail-fast, nichts wird gerechnet."""
    _, l1 = _variant_product(db)
    with pytest.raises(PersistentError):
        asyncio.run(golive_service.bulk_reprice(
            db, listing_ids=[l1.id], mode="profit", value=-50, apply=True))
    with pytest.raises(PersistentError):
        asyncio.run(golive_service.bulk_reprice(
            db, listing_ids=[l1.id], mode="margin", value=-5, apply=False))
    # Absurd hohe Marge (>=95 %) ebenfalls abgelehnt.
    with pytest.raises(PersistentError):
        asyncio.run(golive_service.bulk_reprice(
            db, listing_ids=[l1.id], mode="margin", value=99, apply=False))


def test_preview_variant_prices_rejects_negative_value(db):
    """Derselbe Schutz auf dem Einzel-Listing-Pfad (eine Wahrheit)."""
    _, l1 = _variant_product(db)
    with pytest.raises(PersistentError):
        asyncio.run(golive_service.preview_variant_prices(db, listing_id=l1.id, mode="profit", value=-1))


def test_apply_variant_prices_draft_sets_local(db):
    p, l = _variant_product(db)
    l.ebay_item_id = None   # Entwurf
    db.commit()
    r = asyncio.run(golive_service.apply_variant_prices(
        db, listing_id=l.id, price_by_sku={"AE-PT-V1": 19.99, "AE-PT-V2": 24.10}))
    assert r["pushed_to_ebay"] is False
    # Rundung auf x,95, Basis-Preis = hoechster
    assert r["prices"]["AE-PT-V1"] == 19.95
    assert float(db.get(Listing, l.id).price_eur) == 23.95


# ----------------------------- Hauptbild (E) -----------------------------
def test_set_main_image_reorders_and_adds_variant_image(db):
    p, l = _variant_product(db)
    # Variantenbild als Hauptbild -> wird an Position 0 aufgenommen
    r = asyncio.run(golive_service.set_main_image(db, listing_id=l.id,
                                                  image_url="https://img/v-red.jpg"))
    assert r["images"][0] == "https://img/v-red.jpg"
    assert db.get(Product, p.id).images[0] == "https://img/v-red.jpg"


def test_set_main_image_rejects_foreign(db):
    p, l = _variant_product(db)
    with pytest.raises(PersistentError, match="gehoert nicht"):
        asyncio.run(golive_service.set_main_image(db, listing_id=l.id,
                                                  image_url="https://evil/x.jpg"))


def test_ebay_listing_images_uses_live_gallery(monkeypatch):
    """Titelbild-Auswahl nutzt die ECHTEN eBay-Bilder (Inventory API), nicht die Quelle."""
    from app.services import golive_service as gl

    class _FakeEbay:
        async def get_inventory_item(self, sku):
            return {"product": {"imageUrls": ["https://ebay/1.jpg", "https://ebay/2.jpg"]}}

    async def _skus(ebay, listing):
        return ["AE-1"]

    monkeypatch.setattr(gl, "_listing_variant_skus", _skus)

    class _L:
        ebay_sku = "AE-1"
        ebay_item_id = "9"

    imgs = asyncio.run(gl.ebay_listing_images(_FakeEbay(), _L()))
    assert [c["url"] for c in imgs] == ["https://ebay/1.jpg", "https://ebay/2.jpg"]
    assert imgs[0]["is_main"] is True and imgs[1]["is_main"] is False
    assert all(c["kind"] == "ebay" for c in imgs)


# ----------------------------- Variantenbilder in Galerie (D) -----------------------------
def test_gallery_includes_variant_images(db):
    p, l = _variant_product(db)
    gal = golive_service.gallery_images(p)
    assert gal[:2] == ["https://img/main1.jpg", "https://img/main2.jpg"]   # Standard zuerst
    assert "https://img/v-black.jpg" in gal and "https://img/v-red.jpg" in gal
    cands = golive_service.image_candidates(p)
    assert any(c["kind"] == "variant" for c in cands)
    assert cands[0]["is_main"] is True


# ----------------------------- Variante loeschen (C) -----------------------------
def test_delete_variant(db):
    p, l = _variant_product(db)
    r = golive_service.delete_variant(db, listing_id=l.id, attr="14:2")
    assert r["remaining"] == 1
    skus = db.get(Product, p.id).variants["skus"]
    assert [s["attr"] for s in skus] == ["14:1"]
    # Achsen neu abgeleitet: 'Rot' ist weg
    assert db.get(Product, p.id).variants["axes"]["Farbe"] == ["Schwarz"]


def test_delete_last_variant_refused(db):
    p, l = _variant_product(db)
    golive_service.delete_variant(db, listing_id=l.id, attr="14:2")
    with pytest.raises(PersistentError, match="letzte"):
        golive_service.delete_variant(db, listing_id=l.id, attr="14:1")


# ----------------------------- KI-Bearbeitung (B) -----------------------------
def test_ai_edit_listing_proposes_only(db):
    from app.services import product_service
    p, l = _variant_product(db)
    l.item_specifics = {"Marke": "Markenlos", "Herkunftsland": "China"}
    l.category_id = "111"
    db.commit()
    r = asyncio.run(product_service.ai_edit_listing(
        db, listing_id=l.id,
        instruction="Titel kürzer. Merkmal Material: Edelstahl. Kategorie 12345."))
    assert r["applied"] is False                      # nichts gespeichert
    prop = r["proposal"]
    assert prop["category_hint"] == "12345"           # Kategorie-Hinweis erkannt
    assert prop["item_specifics"].get("Material") == "Edelstahl"
    assert "Herkunftsland" not in prop["item_specifics"]   # China nie im Vorschlag
    # DB unverändert (propose-only)
    db.refresh(l)
    assert l.category_id == "111"


def test_ai_edit_requires_instruction(db):
    from app.services import product_service
    p, l = _variant_product(db)
    with pytest.raises(PersistentError, match="Anweisung"):
        asyncio.run(product_service.ai_edit_listing(db, listing_id=l.id, instruction="  "))


# ----------------------------- Bild-Achse datengetrieben (Vorfall Adler) -----------------------------
def test_detect_image_axis_by_data_not_color_name():
    # 2 Achsen, KEINE heisst "Farbe": Bild variiert nach 'Adler-Design' (je Style 1 Bild),
    # NICHT nach 'Lieferumfang' (beide Werte teilen alle Bilder).
    axes = ["Adler-Design", "Lieferumfang"]
    variants = [
        {"options": {"Adler-Design": "Style 1", "Lieferumfang": "Mit Kette"}, "image": "s1.jpg"},
        {"options": {"Adler-Design": "Style 1", "Lieferumfang": "Nur Anhänger"}, "image": "s1.jpg"},
        {"options": {"Adler-Design": "Style 2", "Lieferumfang": "Mit Kette"}, "image": "s2.jpg"},
        {"options": {"Adler-Design": "Style 2", "Lieferumfang": "Nur Anhänger"}, "image": "s2.jpg"},
    ]
    assert golive_service._detect_image_axis(axes, variants) == "Adler-Design"
    # _image_axis (mehrachsig, kein Farbname) findet dieselbe Achse
    assert golive_service._image_axis(axes, axes, {a: a for a in axes}, variants) == "Adler-Design"


def test_detect_image_axis_needs_image_on_all():
    axes = ["Design"]
    variants = [{"options": {"Design": "A"}, "image": "a.jpg"},
                {"options": {"Design": "B"}}]   # eins ohne Bild
    assert golive_service._detect_image_axis(axes, variants) is None


def test_publish_multi_one_image_per_variant_no_standard_dragged_in(db):
    """Jede Variante bekommt GENAU IHR EIGENES Bild – keine Standardbilder mitgeschleppt
    (frueher 1+8 Bilder je Variante -> hunderte Bilder, unuebersichtlich)."""
    p = Product(aliexpress_url="https://de.aliexpress.com/item/img.html", aliexpress_id="im1",
                price_cny=Decimal("12"),
                images=["https://i/std1.jpg", "https://i/std2.jpg", "https://i/std3.jpg"],
                variants={"axes": {"Farbe": ["Schwarz", "Rot"]},
                          "skus": [
                    {"attr": "c1", "options": {"Farbe": "Schwarz"}, "price": 12.0, "stock": 9,
                     "image": "https://i/black.jpg"},
                    {"attr": "c2", "options": {"Farbe": "Rot"}, "price": 12.0, "stock": 9,
                     "image": "https://i/red.jpg"}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-IM", title_seo="Img", description="d",
                listing_status="active", price_eur=Decimal("24.95"))
    db.add(l); db.commit()

    imgs_by_sku = {}
    class _Ebay:
        async def get_required_aspects(self, cat): return []
        async def create_inventory_item(self, sku, **kw): imgs_by_sku[sku] = kw.get("image_urls")
        async def create_offer(self, sku, **kw): return f"o-{sku}"
        async def create_inventory_item_group(self, gk, **kw): return None
        async def publish_offer_by_inventory_item_group(self, gk): return "IT-IM"
        async def _first_offer_for_sku(self, sku): return None
        async def delete_inventory_item(self, sku): return None

    from app.config import get_settings
    axis_names, variants = golive_service._usable_variants(p)
    asyncio.run(golive_service._publish_multi(
        _Ebay(), l, p, category="123", aspects={"Marke": "Markenlos"}, brand="Markenlos",
        axis_names=axis_names, variants=variants, base_sku="AE-IM",
        settings=get_settings(), draft_only=True))
    # Genau ein Bild je Variante = das eigene Variantenbild, KEINE Standardbilder.
    assert imgs_by_sku["AE-IM-V1"] == ["https://i/black.jpg"]
    assert imgs_by_sku["AE-IM-V2"] == ["https://i/red.jpg"]


def test_publish_multi_varies_images_by_design_axis(db):
    # Wie der Adler-Artikel: Bild-Achse 'Adler-Design' -> image_varies_by MUSS gesetzt sein.
    p = Product(aliexpress_url="https://de.aliexpress.com/item/adler.html", aliexpress_id="ad1",
                price_cny=Decimal("12"), images=["https://i/main.jpg"],
                variants={"axes": {"Adler-Design": ["Style 1", "Style 2"],
                                   "Lieferumfang": ["Mit Kette", "Nur Anhänger"]},
                          "skus": [
                    {"attr": "a1", "options": {"Adler-Design": "Style 1", "Lieferumfang": "Mit Kette"}, "price": 12.0, "stock": 9, "image": "https://i/s1.jpg"},
                    {"attr": "a2", "options": {"Adler-Design": "Style 1", "Lieferumfang": "Nur Anhänger"}, "price": 10.0, "stock": 9, "image": "https://i/s1.jpg"},
                    {"attr": "a3", "options": {"Adler-Design": "Style 2", "Lieferumfang": "Mit Kette"}, "price": 13.0, "stock": 9, "image": "https://i/s2.jpg"},
                    {"attr": "a4", "options": {"Adler-Design": "Style 2", "Lieferumfang": "Nur Anhänger"}, "price": 11.0, "stock": 9, "image": "https://i/s2.jpg"}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-AD", title_seo="Adler", description="d",
                listing_status="active", price_eur=Decimal("24.95"))
    db.add(l); db.commit()

    captured = {}
    class _Ebay:
        async def get_required_aspects(self, cat): return []
        async def create_inventory_item(self, sku, **kw): pass
        async def create_offer(self, sku, **kw): return f"o-{sku}"
        async def create_inventory_item_group(self, gk, **kw):
            captured["image_varies_by"] = kw.get("image_varies_by")
        async def publish_offer_by_inventory_item_group(self, gk): return "IT-AD"
        async def _first_offer_for_sku(self, sku): return None
        async def delete_inventory_item(self, sku): return None

    from app.config import get_settings
    axis_names, variants = golive_service._usable_variants(p)
    asyncio.run(golive_service._publish_multi(
        _Ebay(), l, p, category="123", aspects={"Marke": "Markenlos"}, brand="Markenlos",
        axis_names=axis_names, variants=variants, base_sku="AE-AD",
        settings=get_settings(), draft_only=True))
    assert captured["image_varies_by"] == ["Adler-Design"]   # NICHT None!


# ----------------------------- Preis-Override ueberlebt Publish -----------------------------
def test_variant_price_override_survives_compute(db):
    from app.config import get_settings
    p, l = _variant_product(db)   # skus attr 14:1 / 14:2, price 6/8
    # Nutzer setzt Preise (20% Marge) -> als Override persistiert
    asyncio.run(golive_service.apply_variant_prices(
        db, listing_id=l.id, price_by_sku={"AE-PT-V1": 18.95, "AE-PT-V2": 21.95}))
    db.refresh(l)
    assert l.variant_prices and l.variant_prices.get("14:1") == 18.95
    # Publish-Kalkulation nutzt jetzt die Overrides statt der Config-Formel
    prices = {pr["sku"]: pr["price_eur"] for pr in golive_service.compute_variant_prices(
        l, db.get(Product, p.id), get_settings())}
    assert prices["AE-PT-V1"] == 18.95 and prices["AE-PT-V2"] == 21.95


def test_apply_variant_prices_sets_each_variant_own_price(db, monkeypatch):
    """KERN-FIX 18.07. (Puzzlematten-Bug): je Variante wird IHR EIGENER Preis gesetzt – NICHT
    mehr alle Variationen auf einen Preis flach. Klassisches Listing (kein Inventory-Offer) ->
    EIN ReviseFixedPriceItem, jede Variation mit IHREM StartPrice, zugeordnet per Merkmals-
    Signatur trotz abweichender eBay-SKUs (importiert)."""
    from decimal import Decimal
    from app.models import Product, Listing
    from app.services import golive_service as gl
    p = Product(aliexpress_url="https://ae/pp", aliexpress_id="pp", price_cny=Decimal("6"),
                variants={"axes": {"Farbe": ["Rot", "Blau"]},
                          "skus": [{"attr": "14:1", "options": {"Farbe": "Rot"}, "id": 1, "price": 6, "stock": 9},
                                   {"attr": "14:2", "options": {"Farbe": "Blau"}, "id": 2, "price": 6, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-PP", ebay_item_id="EPP", title_seo="X", description="d",
                listing_status="active", price_eur=Decimal("36.95"),
                variant_prices={"14:1": 36.95, "14:2": 36.95})
    db.add(l); db.commit()

    captured = {}
    class _FakeEbay:
        async def _first_offer_for_sku(self, sku): return None      # klassisch (kein Inventory-Offer)
        async def get_item_price_info(self, item_id):
            # eBay-SKUs weichen ab (importiert) – NUR die Merkmale matchen die Positions-Varianten
            return {"item_id": item_id, "current_price": 36.95, "variations": [
                {"sku": "IMPORT-A", "price": 36.95, "quantity": "10", "specifics": [("Farbe", "Rot")]},
                {"sku": "IMPORT-B", "price": 36.95, "quantity": "5", "specifics": [("Farbe", "Blau")]}]}
        async def revise_variation_prices(self, item_id, variations):
            captured["variations"] = variations; return len(variations)
        async def revise_item_price_smart(self, item_id, price):
            raise AssertionError("Multivarianten-Listing NIE flach auf einen Preis setzen!")
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    asyncio.run(gl.apply_variant_prices(
        db, listing_id=l.id, price_by_sku={"AE-PP-V1": 42.95, "AE-PP-V2": 48.95}))
    got = {tuple(v["specifics"]): v["price_eur"] for v in captured["variations"]}
    assert got[(("Farbe", "Rot"),)] == 42.95      # Rot -> IHR Preis
    assert got[(("Farbe", "Blau"),)] == 48.95     # Blau -> IHR ANDERER Preis (nicht 42,95!)
    assert len({v["price_eur"] for v in captured["variations"]}) == 2   # zwei verschiedene Preise
    # GELD-/BESTAND-SCHUTZ: jede gesendete Variation traegt ihre aktuelle MENGE (sonst nullt eBay
    # den Bestand) und ihre SKU.
    qty = {tuple(v["specifics"]): v["quantity"] for v in captured["variations"]}
    assert qty[(("Farbe", "Rot"),)] == 10 and qty[(("Farbe", "Blau"),)] == 5
    assert all(v.get("quantity") is not None for v in captured["variations"])
    db.refresh(l)
    assert l.variant_prices["14:1"] == 42.95 and l.variant_prices["14:2"] == 48.95


def test_apply_variant_prices_untargeted_variation_kept_at_current_price(db, monkeypatch):
    """SICHERHEIT (18.07.): wird nur EINE von mehreren Variationen angehakt, enthaelt der
    ReviseFixedPriceItem trotzdem ALLE Variationen – die nicht angehakten behalten ihren
    AKTUELLEN eBay-Preis. So kann eBay keine weggelassene Variation als geloescht interpretieren
    und nichts wird flach gesetzt. Nur die angehakte wird als Override persistiert."""
    from decimal import Decimal
    from app.models import Product, Listing
    from app.services import golive_service as gl
    p = Product(aliexpress_url="https://ae/e3", aliexpress_id="e3", price_cny=Decimal("6"),
                variants={"axes": {"Farbe": ["Rot", "Blau", "Grün"]},
                          "skus": [{"attr": "14:1", "options": {"Farbe": "Rot"}, "id": 1, "price": 6, "stock": 9},
                                   {"attr": "14:2", "options": {"Farbe": "Blau"}, "id": 2, "price": 6, "stock": 9},
                                   {"attr": "14:3", "options": {"Farbe": "Grün"}, "id": 3, "price": 6, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-E3", ebay_item_id="E3", title_seo="X", description="d",
                listing_status="active", price_eur=Decimal("36.95"))
    db.add(l); db.commit()

    captured = {}
    class _FakeEbay:
        async def _first_offer_for_sku(self, sku): return None
        async def get_item_price_info(self, item_id):
            return {"item_id": item_id, "current_price": 36.95, "variations": [
                {"sku": "A", "price": 36.95, "quantity": "10", "specifics": [("Farbe", "Rot")]},
                {"sku": "B", "price": 40.95, "quantity": "20", "specifics": [("Farbe", "Blau")]},
                {"sku": "C", "price": 44.95, "quantity": "30", "specifics": [("Farbe", "Grün")]}]}
        async def revise_variation_prices(self, item_id, variations):
            captured["variations"] = variations; return len(variations)
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    # Nur Rot (V1) anheben
    asyncio.run(gl.apply_variant_prices(db, listing_id=l.id, price_by_sku={"AE-E3-V1": 49.95}))
    got = {tuple(v["specifics"]): v["price_eur"] for v in captured["variations"]}
    qty = {tuple(v["specifics"]): v["quantity"] for v in captured["variations"]}
    assert len(captured["variations"]) == 3                    # ALLE drei gesendet (nichts weggelassen)
    assert got[(("Farbe", "Rot"),)] == 49.95                   # angehakt -> neuer Preis
    assert got[(("Farbe", "Blau"),)] == 40.95                  # unveraendert (aktueller eBay-Preis)
    assert got[(("Farbe", "Grün"),)] == 44.95                  # unveraendert
    # jede Variation traegt ihre Menge (kein Bestands-Reset auf 0)
    assert qty == {(("Farbe", "Rot"),): 10, (("Farbe", "Blau"),): 20, (("Farbe", "Grün"),): 30}
    db.refresh(l)
    assert l.variant_prices.get("14:1") == 49.95               # nur Rot als Override persistiert
    assert "14:2" not in (l.variant_prices or {}) and "14:3" not in (l.variant_prices or {})


def test_apply_variant_prices_skips_variation_with_unknown_quantity(db, monkeypatch):
    """GELD-/BESTAND-SCHUTZ: kennt eBay fuer eine Ziel-Variation KEINE Menge, wird sie NICHT
    geschrieben (sonst nullt eBay ihren Bestand) und als Fehlschlag gemeldet – die andere Variante
    mit bekannter Menge wird korrekt gesetzt."""
    from decimal import Decimal
    from app.models import Product, Listing
    from app.services import golive_service as gl
    p = Product(aliexpress_url="https://ae/q", aliexpress_id="q", price_cny=Decimal("6"),
                variants={"axes": {"Farbe": ["Rot", "Blau"]},
                          "skus": [{"attr": "14:1", "options": {"Farbe": "Rot"}, "id": 1, "price": 6, "stock": 9},
                                   {"attr": "14:2", "options": {"Farbe": "Blau"}, "id": 2, "price": 6, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-Q", ebay_item_id="EQ", title_seo="X", description="d",
                listing_status="active", price_eur=Decimal("17.95"),
                variant_prices={"14:1": 17.95, "14:2": 17.95})
    db.add(l); db.commit()

    captured = {}
    class _FakeEbay:
        async def _first_offer_for_sku(self, sku): return None
        async def get_item_price_info(self, item_id):
            return {"item_id": item_id, "current_price": 17.95, "variations": [
                {"sku": "R", "price": 17.95, "quantity": None, "specifics": [("Farbe", "Rot")]},   # keine Menge!
                {"sku": "B", "price": 17.95, "quantity": "5", "specifics": [("Farbe", "Blau")]}]}
        async def revise_variation_prices(self, item_id, variations):
            captured["variations"] = variations; return len(variations)
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    res = asyncio.run(gl.apply_variant_prices(
        db, listing_id=l.id, price_by_sku={"AE-Q-V1": 24.95, "AE-Q-V2": 26.95}))
    db.refresh(l)
    assert l.variant_prices.get("14:2") == 26.95              # Blau (Menge bekannt) gesetzt
    assert l.variant_prices.get("14:1") == 17.95              # Rot (Menge unbekannt) NICHT gesetzt
    assert any(f["sku"] == "AE-Q-V1" for f in res["failed"])
    assert all(tuple(v["specifics"]) != (("Farbe", "Rot"),) for v in captured["variations"])


def test_revise_variation_prices_xml_carries_quantity_and_identifiers(monkeypatch):
    """XML-Ebene (Regress gegen den Fund 18.07.): jede &lt;Variation&gt; traegt &lt;Quantity&gt; UND SKU +
    VariationSpecifics. Eine Variation OHNE Menge wird NICHT geschrieben (sonst Bestands-Reset)."""
    from app.config import get_settings
    from app.integrations.ebay import RealEbayClient
    c = RealEbayClient(get_settings())
    captured = {}
    async def _fake_call(name, body):
        captured["name"] = name; captured["body"] = body; return "<ack/>"
    c._trading_call = _fake_call
    n = asyncio.run(c.revise_variation_prices("IT1", [
        {"specifics": [("Farbe", "Rot")], "sku": "S-ROT", "quantity": 7, "price_eur": 42.95},
        {"specifics": [("Farbe", "Blau")], "sku": None, "quantity": 3, "price_eur": 48.95},
        {"specifics": [("Farbe", "Grün")], "sku": "S-G", "quantity": None, "price_eur": 50.0}]))
    b = captured["body"]
    assert captured["name"] == "ReviseFixedPriceItem"
    assert "<Quantity>7</Quantity>" in b and "<Quantity>3</Quantity>" in b
    assert "<SKU>S-ROT</SKU>" in b and "<VariationSpecifics>" in b
    assert "<StartPrice>42.95</StartPrice>" in b and "<StartPrice>48.95</StartPrice>" in b
    assert "Grün" not in b                # Variation ohne Menge NICHT geschrieben
    assert n == 2


def test_apply_variant_prices_updates_live_prices_immediately(db, monkeypatch, tmp_path):
    """Fund 20.07.: nach dem Anheben zieht apply_variant_prices die ECHTEN neuen Preise SOFORT in
    ebay_live_prices nach (Positions-SKU + Merkmals-Signatur) und rechnet den Cockpit-Report neu –
    damit Marge/Spanne/Bucket sofort stimmen, ohne erneutes 🔄 Synchronisieren."""
    from decimal import Decimal
    from app.models import Product, Listing
    from app.services import golive_service as gl
    from app.services import listing_match_service as lms
    monkeypatch.setattr(lms, "REPORT_FILE", str(tmp_path / "r.json"))
    p = Product(aliexpress_url="https://ae/lp", aliexpress_id="lp", price_cny=Decimal("6"),
                variants={"axes": {"Farbe": ["Rot", "Blau"]},
                          "skus": [{"attr": "14:1", "options": {"Farbe": "Rot"}, "id": 1, "price": 6, "stock": 9},
                                   {"attr": "14:2", "options": {"Farbe": "Blau"}, "id": 2, "price": 6, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-LP", ebay_item_id="ELP", title_seo="X", description="d",
                listing_status="active", price_eur=Decimal("36.95"), cost_eur=Decimal("8.00"),
                ebay_live_prices={"AE-LP-V1": 36.95, "AE-LP-V2": 36.95,
                                  "sig:rot": 36.95, "sig:blau": 36.95})
    db.add(l); db.commit()

    class _FakeEbay:
        async def _first_offer_for_sku(self, sku): return None      # klassisch (Trading)
        async def get_item_price_info(self, item_id):
            return {"item_id": item_id, "current_price": 36.95, "variations": [
                {"sku": "AE-LP-V1", "price": 36.95, "quantity": "9", "specifics": [("Farbe", "Rot")]},
                {"sku": "AE-LP-V2", "price": 36.95, "quantity": "9", "specifics": [("Farbe", "Blau")]}]}
        async def revise_variation_prices(self, item_id, variations): return len(variations)
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    asyncio.run(gl.apply_variant_prices(
        db, listing_id=l.id, price_by_sku={"AE-LP-V1": 42.95, "AE-LP-V2": 48.95}))
    db.refresh(l)
    # Live-Preise sofort nachgezogen (Positions-SKU + Signatur), nicht mehr 36,95
    assert lms.effective_ebay_price(l, "AE-LP-V1", {"Farbe": "Rot"}) == 42.95
    assert lms.effective_ebay_price(l, "AE-LP-V2", {"Farbe": "Blau"}) == 48.95
    assert l.ebay_live_prices["sig:rot"] == 42.95 and l.ebay_live_prices["sig:blau"] == 48.95


def test_apply_variant_prices_grp_uses_inventory_api_via_real_sku(db, monkeypatch):
    """Fund 18.07. (Anime-Figur -GRP): der Import hatte ebay_sku auf den Gruppen-Key gesetzt, die
    konstruierte SKU (...-GRP-V1) traf KEIN Offer -> frueher Trading-Revise -> von eBay abgelehnt
    ('warenbestandsbasiert'). Fix: die ECHTE Variations-SKU aus GetItem trifft das Sell-Offer ->
    Preis wird korrekt per Inventory-API (bulk_update_price) je Variante gesetzt, NICHT per Trading."""
    from decimal import Decimal
    from app.models import Product, Listing
    from app.services import golive_service as gl
    p = Product(aliexpress_url="https://ae/g", aliexpress_id="g", price_cny=Decimal("6"),
                variants={"axes": {"Farbe": ["Rot", "Blau"]},
                          "skus": [{"attr": "14:1", "options": {"Farbe": "Rot"}, "id": 1, "price": 6, "stock": 9},
                                   {"attr": "14:2", "options": {"Farbe": "Blau"}, "id": 2, "price": 6, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-X-GRP", ebay_item_id="EG", ebay_draft_id="AE-X-GRP",
                title_seo="X", description="d", listing_status="active", price_eur=Decimal("17.95"),
                variant_prices={"14:1": 17.95, "14:2": 17.95})
    db.add(l); db.commit()

    bulk_calls = []
    class _FakeEbay:
        async def _first_offer_for_sku(self, sku):
            # NUR die ECHTEN SKUs (ohne -GRP) haben ein Sell-Offer
            return {"offerId": f"off-{sku}"} if str(sku) in ("AE-X-V1", "AE-X-V2") else None
        async def get_item_price_info(self, item_id):
            return {"item_id": item_id, "current_price": 17.95, "variations": [
                {"sku": "AE-X-V1", "price": 17.95, "quantity": "9", "specifics": [("Farbe", "Rot")]},
                {"sku": "AE-X-V2", "price": 17.95, "quantity": "9", "specifics": [("Farbe", "Blau")]}]}
        async def bulk_update_price(self, updates):
            bulk_calls.extend(updates)
        async def revise_variation_prices(self, item_id, variations):
            raise AssertionError("Inventory-Listing NIE per Trading-ReviseFixedPriceItem setzen!")
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    asyncio.run(gl.apply_variant_prices(
        db, listing_id=l.id, price_by_sku={"AE-X-GRP-V1": 24.95, "AE-X-GRP-V2": 29.95}))
    got = {u["sku"]: u["price_eur"] for u in bulk_calls}
    assert got == {"AE-X-V1": 24.95, "AE-X-V2": 29.95}     # per ECHTER SKU ueber Inventory-API
    db.refresh(l)
    assert l.variant_prices["14:1"] == 24.95 and l.variant_prices["14:2"] == 29.95


def test_prefix_of_distinct_safety():
    """GELD-SCHUTZ (Review 20.07.): WORTGRENZEN-Präfix vermeidet Fehltreffer, die einen FALSCHEN
    Variantenpreis setzen würden. '60cm' darf NIE '160cm', 'niger' NIE 'nigeria' (Grenze mitten im
    Wort → anderes Land), 'guinea' NIE 'äquatorialguinea' treffen. Werte OHNE Leerzeichen-Strip."""
    from app.services.golive_service import _prefix_of_distinct as pf
    assert pf(["60cm"], ["60cm oder 23,6 zoll"]) is True      # Präfix + Wortgrenze (Leerzeichen)
    assert pf(["60cm"], ["160cm oder 63 zoll"]) is False      # 60cm ist NICHT Anfang von 160cm
    assert pf(["niger"], ["nigeria"]) is False                # Grenze mitten im Wort -> KEIN Treffer
    assert pf(["guinea"], ["äquatorialguinea"]) is False      # kein Präfix
    assert pf(["guinea"], ["guinea-bissau"]) is True          # Präfix + Grenze '-' (global gefiltert)
    assert pf(["guinea"], ["guinea"]) is True                 # exakt
    assert pf(["60cm", "guinea"], ["60cm oder 23,6 zoll", "guinea"]) is True    # bijektiv
    assert pf(["60cm", "guinea"], ["60cm oder 23,6 zoll", "benin"]) is False    # guinea fehlt
    assert pf([], ["x"]) is False                             # leer -> fail-closed


def test_apply_variant_prices_bijective_resolves_guinea_ambiguity(db, monkeypatch, tmp_path):
    """Fund 20.07.: „Guinea" ist Präfix von „Guinea" UND „Guinea-Bissau" -> Einzel-Zuordnung
    mehrdeutig. Die GLOBALE bijektive Zwangs-Zuordnung löst es: „Guinea-Bissau" (eindeutig) belegt
    seine Variation, „Guinea" wird auf die verbleibende gezwungen -> jede bekommt IHREN Preis."""
    from decimal import Decimal
    from app.models import Product, Listing
    from app.services import golive_service as gl
    from app.services import listing_match_service as lms
    monkeypatch.setattr(lms, "REPORT_FILE", str(tmp_path / "r.json"))
    p = Product(aliexpress_url="https://ae/flag", aliexpress_id="flag", price_cny=Decimal("6"),
                variants={"axes": {"Land": ["Guinea", "Guinea-Bissau"]},
                          "skus": [{"attr": "a1", "options": {"Land": "Guinea"}, "id": 1, "price": 6, "stock": 9},
                                   {"attr": "a2", "options": {"Land": "Guinea-Bissau"}, "id": 2, "price": 6, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="uuid-flag", ebay_item_id="EFL", title_seo="X",
                description="d", listing_status="active", price_eur=Decimal("30.00"), variant_prices={})
    db.add(l); db.commit()
    captured = {}
    class _FakeEbay:
        async def _first_offer_for_sku(self, sku): return None
        async def get_item_price_info(self, item_id):
            return {"item_id": item_id, "current_price": 30.0, "variations": [
                {"sku": "R1", "price": 30.0, "quantity": "9", "specifics": [("Land", "Guinea")]},
                {"sku": "R2", "price": 30.0, "quantity": "9", "specifics": [("Land", "Guinea-Bissau")]}]}
        async def revise_variation_prices(self, item_id, variations):
            captured["variations"] = variations; return len(variations)
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())
    base = "uuid-flag"
    asyncio.run(gl.apply_variant_prices(db, listing_id=l.id,
        price_by_sku={f"{base}-V1": 39.95, f"{base}-V2": 49.95}))   # V1=Guinea, V2=Guinea-Bissau
    got = {dict(v["specifics"])["Land"]: v["price_eur"] for v in captured["variations"]}
    assert got["Guinea"] == 39.95            # Guinea korrekt (NICHT auf Guinea-Bissau gelandet)
    assert got["Guinea-Bissau"] == 49.95
    db.refresh(l)
    assert l.variant_prices["a1"] == 39.95 and l.variant_prices["a2"] == 49.95


def test_apply_variant_prices_niger_not_mismatched_to_nigeria(db, monkeypatch, tmp_path):
    """GELD-SCHUTZ (Review-Fund 20.07.): fehlt unsere Variante „Niger" auf eBay und eBay führt nur
    „Nigeria", darf der Niger-Preis NIE auf Nigeria landen (Präfix, aber Grenze MITTEN im Wort ->
    anderes Land) -> fail-closed, nichts gesetzt."""
    from decimal import Decimal
    from app.models import Product, Listing
    from app.services import golive_service as gl
    from app.services import listing_match_service as lms
    monkeypatch.setattr(lms, "REPORT_FILE", str(tmp_path / "r.json"))
    p = Product(aliexpress_url="https://ae/afr", aliexpress_id="afr", price_cny=Decimal("6"),
                variants={"axes": {"Land": ["Niger", "Chad"]},
                          "skus": [{"attr": "a1", "options": {"Land": "Niger"}, "id": 1, "price": 6, "stock": 9},
                                   {"attr": "a2", "options": {"Land": "Chad"}, "id": 2, "price": 6, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="uuid-afr", ebay_item_id="EAFR", title_seo="X",
                description="d", listing_status="active", price_eur=Decimal("30.00"), variant_prices={})
    db.add(l); db.commit()
    class _FakeEbay:
        async def _first_offer_for_sku(self, sku): return None
        async def get_item_price_info(self, item_id):   # eBay hat NUR Nigeria (Niger fehlt)
            return {"item_id": item_id, "current_price": 30.0, "variations": [
                {"sku": "R1", "price": 30.0, "quantity": "9", "specifics": [("Land", "Nigeria")]}]}
        async def revise_variation_prices(self, item_id, variations):
            raise AssertionError("Niger darf NIE auf Nigeria gesetzt werden!")
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())
    import pytest as _pt
    with _pt.raises(Exception):
        asyncio.run(gl.apply_variant_prices(db, listing_id=l.id, price_by_sku={"uuid-afr-V1": 39.95}))
    db.refresh(l)
    assert not (l.variant_prices or {})


def test_apply_variant_prices_bijective_conflict_fails_closed(db, monkeypatch, tmp_path):
    """GELD-SCHUTZ (Review-Härtung): fehlt auf eBay eine Variation, die wir intern haben, und unser
    Wert ist Präfix einer VORHANDENEN eBay-Variation (Guinea ⊂ Guinea-Bissau), dürfen NICHT beide
    internen Varianten dieselbe eBay-Variation greifen -> Konflikt -> NICHTS gesetzt (nie geraten)."""
    from decimal import Decimal
    from app.models import Product, Listing
    from app.services import golive_service as gl
    from app.services import listing_match_service as lms
    monkeypatch.setattr(lms, "REPORT_FILE", str(tmp_path / "r.json"))
    p = Product(aliexpress_url="https://ae/flag2", aliexpress_id="flag2", price_cny=Decimal("6"),
                variants={"axes": {"Land": ["Guinea", "Guinea-Bissau"]},
                          "skus": [{"attr": "a1", "options": {"Land": "Guinea"}, "id": 1, "price": 6, "stock": 9},
                                   {"attr": "a2", "options": {"Land": "Guinea-Bissau"}, "id": 2, "price": 6, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="uuid-flag2", ebay_item_id="EFL2", title_seo="X",
                description="d", listing_status="active", price_eur=Decimal("30.00"), variant_prices={})
    db.add(l); db.commit()
    class _FakeEbay:
        async def _first_offer_for_sku(self, sku): return None
        async def get_item_price_info(self, item_id):   # eBay hat NUR Guinea-Bissau (Guinea fehlt)
            return {"item_id": item_id, "current_price": 30.0, "variations": [
                {"sku": "R2", "price": 30.0, "quantity": "9", "specifics": [("Land", "Guinea-Bissau")]}]}
        async def revise_variation_prices(self, item_id, variations):
            raise AssertionError("Konflikt -> NICHTS setzen (Guinea nie auf Guinea-Bissau)!")
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())
    import pytest as _pt
    with _pt.raises(Exception):   # PersistentError: nichts gesetzt (fail-closed)
        asyncio.run(gl.apply_variant_prices(db, listing_id=l.id, price_by_sku={"uuid-flag2-V1": 39.95}))
    db.refresh(l)
    assert not (l.variant_prices or {})


def test_apply_variant_prices_prefix_ambiguous_fails_closed(db, monkeypatch, tmp_path):
    """GELD-SCHUTZ: ist die Präfix-Zuordnung NICHT global eindeutig (unser '60cm' ist Präfix von
    ZWEI eBay-Größen '60cm…' und '60cmxl…'), wird NICHTS gesetzt (fail-closed), nie geraten."""
    from decimal import Decimal
    from app.models import Product, Listing
    from app.services import golive_service as gl
    from app.services import listing_match_service as lms
    monkeypatch.setattr(lms, "REPORT_FILE", str(tmp_path / "r.json"))
    p = Product(aliexpress_url="https://ae/amb", aliexpress_id="amb", price_cny=Decimal("6"),
                variants={"axes": {"Größe": ["60cm", "45cm"]},
                          "skus": [{"attr": "a1", "options": {"Größe": "60cm"}, "id": 1, "price": 6, "stock": 9},
                                   {"attr": "a2", "options": {"Größe": "45cm"}, "id": 2, "price": 6, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="uuid-amb", ebay_item_id="EAMB", title_seo="X",
                description="d", listing_status="active", price_eur=Decimal("30.00"), variant_prices={})
    db.add(l); db.commit()
    class _FakeEbay:
        async def _first_offer_for_sku(self, sku): return None
        async def get_item_price_info(self, item_id):
            return {"item_id": item_id, "current_price": 30.0, "variations": [
                {"sku": "R1", "price": 30.0, "quantity": "9", "specifics": [("Größe", "60cm klein")]},
                {"sku": "R2", "price": 30.0, "quantity": "9", "specifics": [("Größe", "60cm XL groß")]}]}
        async def revise_variation_prices(self, item_id, variations):
            raise AssertionError("bei Mehrdeutigkeit NICHTS setzen!")
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())
    import pytest as _pt
    with _pt.raises(Exception):   # PersistentError: kein Variantenpreis gesetzt (fail-closed)
        asyncio.run(gl.apply_variant_prices(db, listing_id=l.id, price_by_sku={"uuid-amb-V1": 49.95}))
    db.refresh(l)
    assert not (l.variant_prices or {})   # nichts persistiert


def test_apply_variant_prices_matches_verbose_ebay_values_by_prefix(db, monkeypatch, tmp_path):
    """Fund 20.07. (Fußball/Flaggen, viele Var.): eBay speichert die VERBOSEN AliExpress-Werte
    („60cm oder 23,6 Zoll"), intern sind sie LLM-gekürzt („60cm"). Die exakte Signatur scheitert ->
    GLOBAL eindeutige Präfix-Zuordnung (unser Wert ist Anfang des eBay-Werts) findet die richtige
    Variante und setzt je Variante IHREN eigenen Preis (nicht flach, nicht falsch)."""
    from decimal import Decimal
    from app.models import Product, Listing
    from app.services import golive_service as gl
    from app.services import listing_match_service as lms
    monkeypatch.setattr(lms, "REPORT_FILE", str(tmp_path / "r.json"))
    p = Product(aliexpress_url="https://ae/wm2", aliexpress_id="wm2", price_cny=Decimal("6"),
        variants={"axes": {"Größe": ["60cm", "45cm"], "Land": ["Guinea", "Benin"]},
                  "skus": [
            {"attr": "a1", "options": {"Größe": "60cm", "Land": "Guinea"}, "id": 1, "price": 6, "stock": 9},
            {"attr": "a2", "options": {"Größe": "60cm", "Land": "Benin"}, "id": 2, "price": 6, "stock": 9},
            {"attr": "a3", "options": {"Größe": "45cm", "Land": "Guinea"}, "id": 3, "price": 6, "stock": 9},
            {"attr": "a4", "options": {"Größe": "45cm", "Land": "Benin"}, "id": 4, "price": 6, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="12541cd0-68cc-uuid", ebay_item_id="EWM", title_seo="X",
                description="d", listing_status="active", price_eur=Decimal("30.00"), variant_prices={})
    db.add(l); db.commit()

    captured = {}
    V, W = "60cm oder 23,6 Zoll (2000er)", "45cm oder 17,7 Zoll (2000er)"
    class _FakeEbay:
        async def _first_offer_for_sku(self, sku): return None      # klassisch (Trading)
        async def get_item_price_info(self, item_id):
            return {"item_id": item_id, "current_price": 30.0, "variations": [
                {"sku": "R1", "price": 30.0, "quantity": "9", "specifics": [("Größe", V), ("Land", "Guinea")]},
                {"sku": "R2", "price": 30.0, "quantity": "9", "specifics": [("Größe", V), ("Land", "Benin")]},
                {"sku": "R3", "price": 30.0, "quantity": "9", "specifics": [("Größe", W), ("Land", "Guinea")]},
                {"sku": "R4", "price": 30.0, "quantity": "9", "specifics": [("Größe", W), ("Land", "Benin")]}]}
        async def revise_variation_prices(self, item_id, variations):
            captured["variations"] = variations; return len(variations)
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    base = "12541cd0-68cc-uuid"
    asyncio.run(gl.apply_variant_prices(db, listing_id=l.id,
        price_by_sku={f"{base}-V1": 49.95, f"{base}-V4": 59.95}))   # V1=(60cm,Guinea), V4=(45cm,Benin)
    got = {(dict(v["specifics"])["Größe"][:4], dict(v["specifics"])["Land"]): v["price_eur"]
           for v in captured["variations"]}
    assert got[("60cm", "Guinea")] == 49.95     # V1 angehakt -> richtige eBay-Variation trotz verbosem Wert
    assert got[("45cm", "Benin")] == 59.95      # V4 angehakt
    assert got[("60cm", "Benin")] == 30.0 and got[("45cm", "Guinea")] == 30.0   # untouched: aktueller Preis
    db.refresh(l)
    assert l.variant_prices["a1"] == 49.95 and l.variant_prices["a4"] == 59.95


def test_apply_variant_prices_grp_alt_sku_from_draft_base(db, monkeypatch, tmp_path):
    """Fund 20.07. (Fußball-Figur): ebay_sku ist eine UUID (importweise verbogen); die konstruierte
    SKU {uuid}-V1 trifft KEIN Offer und die Signatur scheiterte -> Abbruch. Fix: die ECHTE
    Offer-SKU {publish-base}-V{i} wird aus ebay_draft_id ({base}-GRP) abgeleitet -> Offer gefunden
    -> Preis je Variante über die Inventory-API gesetzt (kein Trading, kein Fehlschlag)."""
    from decimal import Decimal
    from app.models import Product, Listing
    from app.services import golive_service as gl
    from app.services import listing_match_service as lms
    monkeypatch.setattr(lms, "REPORT_FILE", str(tmp_path / "r.json"))
    p = Product(aliexpress_url="https://ae/wm", aliexpress_id="wm", price_cny=Decimal("6"),
                variants={"axes": {"Farbe": ["Rot", "Blau"]},
                          "skus": [{"attr": "14:1", "options": {"Farbe": "Rot"}, "id": 1, "price": 6, "stock": 9},
                                   {"attr": "14:2", "options": {"Farbe": "Blau"}, "id": 2, "price": 6, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="fcd56b4b-54fe-4ed5-922f-af253ee1be85", ebay_item_id="EU",
                ebay_draft_id="AE-REAL-GRP", title_seo="X", description="d", listing_status="active",
                price_eur=Decimal("17.95"), variant_prices={"14:1": 17.95, "14:2": 17.95})
    db.add(l); db.commit()

    bulk_calls = []
    class _FakeEbay:
        async def _first_offer_for_sku(self, sku):
            # NUR die ECHTEN Offer-SKUs (publish-base aus ebay_draft_id) existieren
            return {"offerId": f"o-{sku}"} if str(sku) in ("AE-REAL-V1", "AE-REAL-V2") else None
        async def bulk_update_price(self, updates): bulk_calls.extend(updates)
        async def get_item_price_info(self, item_id):   # nur für den Read-back (informativ)
            return {"item_id": item_id, "current_price": 17.95, "variations": [
                {"sku": "AE-REAL-V1", "price": 24.95, "quantity": "9", "specifics": [("Farbe", "Rot")]},
                {"sku": "AE-REAL-V2", "price": 29.95, "quantity": "9", "specifics": [("Farbe", "Blau")]}]}
        async def revise_variation_prices(self, item_id, variations):
            raise AssertionError("Inventory-Listing NIE per Trading setzen!")
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    asyncio.run(gl.apply_variant_prices(db, listing_id=l.id, price_by_sku={
        "fcd56b4b-54fe-4ed5-922f-af253ee1be85-V1": 24.95,
        "fcd56b4b-54fe-4ed5-922f-af253ee1be85-V2": 29.95}))
    got = {u["sku"]: u["price_eur"] for u in bulk_calls}
    assert got == {"AE-REAL-V1": 24.95, "AE-REAL-V2": 29.95}   # via publish-base Offer-SKU (aus -GRP)
    db.refresh(l)
    assert l.variant_prices["14:1"] == 24.95 and l.variant_prices["14:2"] == 29.95


def test_apply_variant_prices_grp_inventory_reject_not_persisted(db, monkeypatch):
    """GELD-SCHUTZ: lehnt die Inventory-API den Preis EINER Variante ab (bulk_update_price wirft),
    wird sie NICHT als Override gespeichert und NICHT zusaetzlich per Trading versucht; die andere
    Variante mit erfolgreichem Push wird korrekt gesetzt."""
    from decimal import Decimal
    from app.models import Product, Listing
    from app.retry import PersistentError
    from app.services import golive_service as gl
    p = Product(aliexpress_url="https://ae/g2", aliexpress_id="g2", price_cny=Decimal("6"),
                variants={"axes": {"Farbe": ["Rot", "Blau"]},
                          "skus": [{"attr": "14:1", "options": {"Farbe": "Rot"}, "id": 1, "price": 6, "stock": 9},
                                   {"attr": "14:2", "options": {"Farbe": "Blau"}, "id": 2, "price": 6, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-Y-GRP", ebay_item_id="EG2", ebay_draft_id="AE-Y-GRP",
                title_seo="X", description="d", listing_status="active", price_eur=Decimal("17.95"),
                variant_prices={"14:1": 17.95, "14:2": 17.95})
    db.add(l); db.commit()

    class _FakeEbay:
        async def _first_offer_for_sku(self, sku):
            return {"offerId": f"off-{sku}"} if str(sku) in ("AE-Y-V1", "AE-Y-V2") else None
        async def get_item_price_info(self, item_id):
            return {"item_id": item_id, "current_price": 17.95, "variations": [
                {"sku": "AE-Y-V1", "price": 17.95, "quantity": "9", "specifics": [("Farbe", "Rot")]},
                {"sku": "AE-Y-V2", "price": 17.95, "quantity": "9", "specifics": [("Farbe", "Blau")]}]}
        async def bulk_update_price(self, updates):
            if any(u["sku"] == "AE-Y-V1" for u in updates):
                raise PersistentError("Preis unter Mindestpreis")     # Rot abgelehnt
        async def revise_variation_prices(self, item_id, variations):
            raise AssertionError("abgelehnte Inventory-Variante NIE per Trading nachziehen!")
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    res = asyncio.run(gl.apply_variant_prices(
        db, listing_id=l.id, price_by_sku={"AE-Y-GRP-V1": 24.95, "AE-Y-GRP-V2": 29.95}))
    db.refresh(l)
    assert l.variant_prices.get("14:2") == 29.95              # Blau gesetzt
    assert l.variant_prices.get("14:1") == 17.95              # Rot abgelehnt -> NICHT hochgesetzt
    assert any(f["sku"] == "AE-Y-GRP-V1" for f in res["failed"])


def test_get_item_price_info_parses_variation_images(monkeypatch):
    """eBay-getriebener Dialog (20.07.): get_item_price_info liefert je Variation das ECHTE eBay-Bild
    (Variations/Pictures nach Bild-Achse) + die Gallery – damit der Preis-Dialog die Bilder DES
    eBay-Listings zeigt, nicht die von AliExpress."""
    from app.config import get_settings
    from app.integrations.ebay import RealEbayClient
    c = RealEbayClient(get_settings())
    xml = ('<?xml version="1.0"?>'
           '<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack><Item>'
           '<SellingStatus><CurrentPrice>30.0</CurrentPrice></SellingStatus>'
           '<PictureDetails><PictureURL>https://ebay/main.jpg</PictureURL></PictureDetails>'
           '<Variations><Pictures><VariationSpecificName>Farbe</VariationSpecificName>'
           '<VariationSpecificPictureSet><VariationSpecificValue>Rot</VariationSpecificValue>'
           '<PictureURL>https://ebay/rot.jpg</PictureURL></VariationSpecificPictureSet>'
           '<VariationSpecificPictureSet><VariationSpecificValue>Blau</VariationSpecificValue>'
           '<PictureURL>https://ebay/blau.jpg</PictureURL></VariationSpecificPictureSet></Pictures>'
           '<Variation><SKU>R1</SKU><StartPrice>30.0</StartPrice><Quantity>5</Quantity>'
           '<VariationSpecifics><NameValueList><Name>Farbe</Name><Value>Rot</Value></NameValueList>'
           '</VariationSpecifics></Variation>'
           '<Variation><SKU>R2</SKU><StartPrice>32.0</StartPrice><Quantity>3</Quantity>'
           '<VariationSpecifics><NameValueList><Name>Farbe</Name><Value>Blau</Value></NameValueList>'
           '</VariationSpecifics></Variation></Variations></Item></GetItemResponse>')
    async def _fake_call(name, body): return xml
    c._trading_call = _fake_call
    info = asyncio.run(c.get_item_price_info("IT1"))
    imgs = {v["sku"]: v.get("image") for v in info["variations"]}
    assert imgs["R1"] == "https://ebay/rot.jpg"      # echtes eBay-Bild der Rot-Variante
    assert imgs["R2"] == "https://ebay/blau.jpg"
    assert info["gallery"] == ["https://ebay/main.jpg"]


def test_revise_item_price_smart_variations_carry_quantity(monkeypatch):
    """revise_item_price_smart (Einzelpreis-Fallback) muss je Variation die MENGE mitsenden; eine
    Variation mit UNBEKANNTER Menge wird NICHT geschrieben (sonst nullt eBay ihren Bestand)."""
    from app.config import get_settings
    from app.integrations.ebay import RealEbayClient
    c = RealEbayClient(get_settings())
    captured = {}
    async def _fake_get(item_id):
        return {"item_id": item_id, "current_price": 20.0, "variations": [
            {"sku": "S1", "price": 20.0, "quantity": "4", "specifics": [("Farbe", "Rot")]},
            {"sku": "S2", "price": 20.0, "quantity": None, "specifics": [("Farbe", "Blau")]}]}
    async def _fake_call(name, body):
        captured["body"] = body; return "<ack/>"
    c.get_item_price_info = _fake_get
    c._trading_call = _fake_call
    r = asyncio.run(c.revise_item_price_smart("IT9", 29.95))
    b = captured["body"]
    assert "<Quantity>4</Quantity>" in b and "<StartPrice>29.95</StartPrice>" in b
    assert "Blau" not in b                # Variation ohne Menge NICHT geschrieben
    assert r["variations"] == 1


def test_apply_variant_prices_unmatched_variant_not_persisted(db, monkeypatch):
    """GELD-SCHUTZ: laesst sich eine Ziel-Variante NICHT eindeutig einer echten eBay-Variation
    zuordnen, wird ihr Preis NICHT gesetzt und NICHT als Override gespeichert (fail-closed) –
    waehrend die eindeutig zuordenbare Variante korrekt IHREN eigenen Preis bekommt. Es wird
    NIE stattdessen alles flach gesetzt."""
    from decimal import Decimal
    from app.models import Product, Listing
    from app.services import golive_service as gl
    p = Product(aliexpress_url="https://ae/x", aliexpress_id="fp", price_cny=Decimal("6"),
                variants={"axes": {"Farbe": ["Rot", "Blau"]},
                          "skus": [{"attr": "14:1", "options": {"Farbe": "Rot"}, "id": 1, "price": 6, "stock": 9},
                                   {"attr": "14:2", "options": {"Farbe": "Blau"}, "id": 2, "price": 6, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-FP", ebay_item_id="EFP", title_seo="X", description="d",
                listing_status="active", price_eur=Decimal("17.95"),
                variant_prices={"14:1": 17.95, "14:2": 17.95})
    db.add(l); db.commit()

    captured = {}
    class _FakeEbay:
        async def _first_offer_for_sku(self, sku): return None
        async def get_item_price_info(self, item_id):
            # eBay kennt NUR die Rot-Variation; Blau fehlt -> nicht zuordenbar
            return {"item_id": item_id, "current_price": 17.95, "variations": [
                {"sku": "EBAY-ROT", "price": 17.95, "quantity": "8", "specifics": [("Farbe", "Rot")]}]}
        async def revise_variation_prices(self, item_id, variations):
            captured["variations"] = variations; return len(variations)
        async def revise_item_price_smart(self, item_id, price):
            raise AssertionError("nicht flach setzen!")
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    res = asyncio.run(gl.apply_variant_prices(
        db, listing_id=l.id, price_by_sku={"AE-FP-V1": 24.95, "AE-FP-V2": 26.95}))
    db.refresh(l)
    assert l.variant_prices.get("14:1") == 24.95      # Rot matcht per Signatur -> eigener Preis
    assert l.variant_prices.get("14:2") == 17.95      # Blau nicht auf eBay -> NICHT gesetzt
    assert len(captured["variations"]) == 1           # nur die eine matchende Variation im Revise
    assert captured["variations"][0]["price_eur"] == 24.95
    assert any(f["sku"] == "AE-FP-V2" for f in res["failed"])   # V2 als Fehlschlag gemeldet


# ======================= GRUNDSANIERUNG: eBay-getriebener Dialog (20.07.) =======================
# Der Reprice-Dialog baut die Zeilen fuer LIVE-Multivarianten DIREKT aus den echten eBay-Variationen
# (GetItem). Jede Zeile IST eine echte eBay-Variation (echte SKU/Specifics + echter Preis); der Write
# adressiert die Variation ueber ihre ECHTE SKU/Specifics -> KEIN Heuristik-Matching mehr.

def _live_multi_listing(db, *, ebay_sku="AE-EV", axes_values=("Rot", "Blau"), prices=(6, 8)):
    from decimal import Decimal
    from app.models import Product, Listing
    axis = "Farbe"
    skus = [{"attr": f"14:{i+1}", "options": {axis: v}, "id": i + 1, "price": prices[i], "stock": 9}
            for i, v in enumerate(axes_values)]
    p = Product(aliexpress_url="https://ae/ev", aliexpress_id="ev", price_cny=Decimal("6"),
                variants={"axes": {axis: list(axes_values)}, "skus": skus})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku=ebay_sku, ebay_item_id="EEV", title_seo="X", description="d",
                listing_status="active", price_eur=Decimal("19.95"), cost_eur=Decimal("8.00"))
    db.add(l); db.commit()
    return p, l


def test_preview_variant_prices_ebay_driven_rows_from_getitem(db, monkeypatch):
    """Der Dialog baut die Zeilen aus GetItem: echte eBay-SKU + echte (verbose) Merkmalswerte +
    ECHTER Preis JE Variation (nicht der Min-Preis-Fallback). EK wird best-effort aus dem internen
    Modell gemappt (verbose eBay-Werte vs. intern gekuerzte -> Praefix)."""
    from app.services import golive_service as gl
    # intern gekuerzt: '60cm' / '160cm'; eBay verbose + andere SKUs + UNTERSCHIEDLICHE Preise
    p, l = _live_multi_listing(db, ebay_sku="uuid-x", axes_values=("60cm", "160cm"), prices=(6, 10))

    class _FakeEbay:
        async def get_item_price_info(self, item_id):
            return {"item_id": item_id, "current_price": 19.95, "variations": [
                {"sku": "EB-A", "price": 19.95, "quantity": "7",
                 "specifics": [("Größe", "60cm oder 23,6 Zoll")]},
                {"sku": "EB-B", "price": 29.95, "quantity": "4",
                 "specifics": [("Größe", "160cm oder 63 Zoll")]}]}
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    prev = asyncio.run(gl.preview_variant_prices(db, listing_id=l.id))
    assert prev["ebay_source"] is True and len(prev["rows"]) == 2
    by_sku = {r["sku"]: r for r in prev["rows"]}
    assert set(by_sku) == {"EB-A", "EB-B"}                 # Zeilen-Identitaet = ECHTE eBay-SKU
    # ECHTER Preis JE Variation (kein Min-Preis-Fallback -> nicht beide 19,95)
    assert by_sku["EB-A"]["current_price_eur"] == 19.95
    assert by_sku["EB-B"]["current_price_eur"] == 29.95
    assert by_sku["EB-A"]["name"] == "60cm oder 23,6 Zoll"  # echte (verbose) eBay-Werte
    assert by_sku["EB-A"]["price_is_live"] is True
    assert by_sku["EB-A"]["stock"] == 7 and by_sku["EB-B"]["stock"] == 4
    # EK best-effort aus dem internen Modell gemappt (Praefix 60cm->'60cm oder…', 160cm->'160cm oder…')
    assert by_sku["EB-A"]["ek_eur"] is not None and by_sku["EB-B"]["ek_eur"] is not None
    # Margen-Vorschau nutzt den gemappten EK
    prev2 = asyncio.run(gl.preview_variant_prices(db, listing_id=l.id, mode="margin", value=25))
    assert all(r["new_price_eur"] is not None for r in prev2["rows"])


def test_ebay_driven_ek_exact_by_publish_sku_position_no_estimate(db, monkeypatch):
    """KERN (20.07.): EK je Variante EXAKT über die Publish-Position {base}-V{i} – die i-te eBay-
    Variation bekommt den EK der i-ten AliExpress-Variante (so haben WIR publiziert). Gilt selbst
    dann, wenn die eBay-Merkmalswerte mit den internen NICHT matchen (Werte-Heuristik würde scheitern).
    Kein Blanket-Listing-EK mehr."""
    from app.services import golive_service as gl
    # intern: V1 EK aus AE-Preis 3€, V2 EK aus AE-Preis 15€  -> deutlich unterschiedliche EKs
    p, l = _live_multi_listing(db, ebay_sku="AE-42", axes_values=("Rot", "Blau"), prices=(3, 15))

    class _FakeEbay:
        async def get_item_price_info(self, item_id):
            # ECHTE eBay-SKUs = Publish-Schema {base}-V{i}; Werte bewusst UNMATCHBAR zu 'Rot'/'Blau'
            return {"item_id": item_id, "current_price": 19.95, "variations": [
                {"sku": "AE-42-V1", "price": 19.95, "quantity": "5",
                 "specifics": [("Farbe", "紅色 / Ruby Deluxe")]},
                {"sku": "AE-42-V2", "price": 29.95, "quantity": "3",
                 "specifics": [("Farbe", "藍色 / Sapphire Deluxe")]}]}
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    prev = asyncio.run(gl.preview_variant_prices(db, listing_id=l.id))
    by_sku = {r["sku"]: r for r in prev["rows"]}
    assert set(by_sku) == {"AE-42-V1", "AE-42-V2"}
    ek1, ek2 = by_sku["AE-42-V1"]["ek_eur"], by_sku["AE-42-V2"]["ek_eur"]
    assert ek1 is not None and ek2 is not None      # exakt zugeordnet TROTZ unmatchbarer Werte
    assert ek1 < ek2                                 # positionsgenau: V1 (AE 3€) < V2 (AE 15€)
    assert not (ek1 == ek2 == 8.00)                  # NICHT der Blanket-Listing-EK (cost_eur 8,00)


def test_ebay_driven_ek_value_wins_over_position_after_variant_reorder(db, monkeypatch):
    """DRIFT-SCHUTZ (Review 20.07.): wurde product.variants NACH dem Publish umsortiert, zeigt die
    Publish-Position {base}-V{i} auf die falsche interne Variante. Weil die Zuordnung WERTE-basiert
    ZUERST läuft, bekommt jede eBay-Variation trotzdem den EK IHRES echten Merkmalswerts – nicht den
    der gleichen Positionsnummer. (Bei reiner Position wäre der EK vertauscht.)"""
    from app.services import golive_service as gl
    # INTERN jetzt umsortiert: V1=Rot(AE 5€), V2=Schwarz(AE 12€), V3=Blau(AE 20€)  -> distinkte EKs
    p, l = _live_multi_listing(db, ebay_sku="AE-r", axes_values=("Rot", "Schwarz", "Blau"),
                               prices=(5, 12, 20))

    class _FakeEbay:
        async def get_item_price_info(self, item_id):
            # ECHT auf eBay (Publish-Reihenfolge): V1=Schwarz, V2=Rot, V3=Blau – Werte vergleichbar
            return {"item_id": item_id, "current_price": 19.95, "variations": [
                {"sku": "AE-r-V1", "price": 19.95, "quantity": "5", "specifics": [("Farbe", "Schwarz")]},
                {"sku": "AE-r-V2", "price": 21.95, "quantity": "4", "specifics": [("Farbe", "Rot")]},
                {"sku": "AE-r-V3", "price": 23.95, "quantity": "3", "specifics": [("Farbe", "Blau")]}]}
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    prev = asyncio.run(gl.preview_variant_prices(db, listing_id=l.id))
    by_sku = {r["sku"]: r for r in prev["rows"]}
    ekV1, ekV2 = by_sku["AE-r-V1"]["ek_eur"], by_sku["AE-r-V2"]["ek_eur"]
    assert ekV1 is not None and ekV2 is not None
    # eBay-V1 ist Schwarz (AE 12€) -> teurer als eBay-V2 = Rot (AE 5€). Bei reiner Position wäre es umgekehrt.
    assert ekV1 > ekV2


def test_ebay_driven_ek_position_disabled_on_reorder_with_unmatchable(db, monkeypatch):
    """DRIFT-SCHUTZ Rest-Lücke (Review 20.07.): wird product.variants umsortiert (≥3er-Rotation) UND hat
    eine Variation einen werte-UNZUORDENBAREN (fremdsprachigen) eBay-Wert, darf der Positions-Fallback
    keinen falschen EK unterschieben. Der Positions-WIDERSPRUCH bei den werte-zuordenbaren Variationen
    erkennt das Umsortieren und schaltet den Positions-Fallback ab -> die fremdsprachige Variante zeigt
    ehrlich EK '?' statt des (falschen) Positions-EK."""
    from app.services import golive_service as gl
    # INTERN seit Publish rotiert: V1=Blau(AE 20€), V2=Rot(AE 5€), V3=Schwarz(AE 12€)
    p, l = _live_multi_listing(db, ebay_sku="AE-x", axes_values=("Blau", "Rot", "Schwarz"),
                               prices=(20, 5, 12))

    class _FakeEbay:
        async def get_item_price_info(self, item_id):
            # ECHTE eBay-SKUs in Publish-Reihenfolge V1=Rot,V2=Schwarz,V3=Blau; V1 fremdsprachig (unmatchbar)
            return {"item_id": item_id, "current_price": 19.95, "variations": [
                {"sku": "AE-x-V1", "price": 19.95, "quantity": "5", "specifics": [("Farbe", "红色 Ruby")]},
                {"sku": "AE-x-V2", "price": 21.95, "quantity": "4", "specifics": [("Farbe", "Schwarz")]},
                {"sku": "AE-x-V3", "price": 23.95, "quantity": "3", "specifics": [("Farbe", "Blau")]}]}
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    prev = asyncio.run(gl.preview_variant_prices(db, listing_id=l.id))
    by_sku = {r["sku"]: r for r in prev["rows"]}
    assert by_sku["AE-x-V2"]["ek_eur"] is not None    # Schwarz: werte-zugeordnet -> exakter EK
    assert by_sku["AE-x-V3"]["ek_eur"] is not None    # Blau:    werte-zugeordnet -> exakter EK
    # fremdsprachige V1 (real Rot): Umsortieren erkannt -> KEIN Positions-Raten -> '?' (nicht Blau-EK 20€)
    assert by_sku["AE-x-V1"]["ek_eur"] is None


def test_ebay_driven_ek_none_when_unmappable_no_estimate(db, monkeypatch):
    """Kein Schätz-EK mehr: eine eBay-Variation, die weder per Publish-Position {base}-V{i} noch per
    Werten zuordenbar ist, zeigt EK None ('?') – NICHT den Listing-Durchschnitt (cost_eur 8,00)."""
    from app.services import golive_service as gl
    p, l = _live_multi_listing(db, ebay_sku="AE-99", axes_values=("Rot", "Blau"), prices=(3, 15))

    class _FakeEbay:
        async def get_item_price_info(self, item_id):
            return {"item_id": item_id, "current_price": 19.95, "variations": [
                {"sku": "AE-99-V1", "price": 19.95, "quantity": "5", "specifics": [("Farbe", "Rot")]},
                # zweite Variation: FREMDE SKU (kein -V-Suffix) + unmatchbarer Wert -> unzuordenbar
                {"sku": "ZZZ-XYZ", "price": 29.95, "quantity": "3", "specifics": [("Farbe", "紫色 Nebula")]}]}
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    prev = asyncio.run(gl.preview_variant_prices(db, listing_id=l.id))
    by_sku = {r["sku"]: r for r in prev["rows"]}
    assert by_sku["ZZZ-XYZ"]["ek_eur"] is None       # unzuordenbar -> KEIN 8,00-Schätzwert
    assert by_sku["AE-99-V1"]["ek_eur"] is not None  # per Position sauber zugeordnet


def test_apply_variant_prices_ebay_driven_real_sku_exact_no_heuristic(db, monkeypatch):
    """KERN DER GRUNDSANIERUNG: sendet der Dialog die ECHTE eBay-SKU, landet der Preis EXAKT auf
    DIESER eBay-Variation – ohne Heuristik. Auch bei Kombi-Labels (eBay 'M/L' vs intern 'M'/'L'),
    die die alte werte-basierte Zuordnung theoretisch fehlleiten koennten."""
    from app.services import golive_service as gl
    p, l = _live_multi_listing(db, ebay_sku="uuid-ml", axes_values=("M", "L"))

    captured = {}
    class _FakeEbay:
        async def _first_offer_for_sku(self, sku): return None      # klassisch (Trading)
        async def get_item_price_info(self, item_id):
            return {"item_id": item_id, "current_price": 19.95, "variations": [
                {"sku": "EB-ML", "price": 19.95, "quantity": "5", "specifics": [("Größe", "M/L")]},
                {"sku": "EB-SM", "price": 22.95, "quantity": "6", "specifics": [("Größe", "S/M")]}]}
        async def revise_variation_prices(self, item_id, variations):
            captured["variations"] = variations; return len(variations)
        async def revise_item_price_smart(self, item_id, price):
            raise AssertionError("nie flach setzen!")
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    # Der Dialog schickt die ECHTE eBay-SKU 'EB-ML' (nicht 'uuid-ml-V1').
    res = asyncio.run(gl.apply_variant_prices(db, listing_id=l.id, price_by_sku={"EB-ML": 42.95}))
    got = {dict(v["specifics"])["Größe"]: v["price_eur"] for v in captured["variations"]}
    assert got["M/L"] == 42.95        # EXAKT auf die 'M/L'-Variation (nicht auf 'S/M')
    assert got["S/M"] == 22.95        # unberuehrt beim aktuellen eBay-Preis
    assert len(captured["variations"]) == 2   # ALLE Variationen gesendet (kein Loesch-Risiko)
    assert any(u.get("ebay_sku") == "EB-ML" for u in res["updated"])
    # live_prices unter der ECHTEN eBay-SKU nachgezogen (gleiches Key-Schema wie der Sync)
    db.refresh(l)
    assert (l.ebay_live_prices or {}).get("EB-ML") == 42.95


def test_apply_variant_prices_ebay_driven_evsig_no_ebay_sku(db, monkeypatch):
    """Hat eine echte eBay-Variation KEINE SKU, traegt die Dialog-Zeile die Identitaet
    'evsig:<Merkmals-Signatur>'. Der Write loest sie ueber die ECHTEN Specifics EINDEUTIG auf
    (revise_variation_prices per Specifics) – fail-closed bei Kollision."""
    from app.services import golive_service as gl
    p, l = _live_multi_listing(db, ebay_sku="uuid-ns", axes_values=("Rot", "Blau"))

    captured = {}
    class _FakeEbay:
        async def _first_offer_for_sku(self, sku): return None
        async def get_item_price_info(self, item_id):
            return {"item_id": item_id, "current_price": 19.95, "variations": [
                {"sku": None, "price": 19.95, "quantity": "9", "specifics": [("Farbe", "Rot")]},
                {"sku": None, "price": 19.95, "quantity": "9", "specifics": [("Farbe", "Blau")]}]}
        async def revise_variation_prices(self, item_id, variations):
            captured["variations"] = variations; return len(variations)
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    # Zeilen-Identitaet aus dem Dialog
    prev = asyncio.run(gl.preview_variant_prices(db, listing_id=l.id))
    keys = {r["sku"] for r in prev["rows"]}
    assert keys == {"evsig:rot", "evsig:blau"}
    # Nur Rot anheben
    asyncio.run(gl.apply_variant_prices(db, listing_id=l.id, price_by_sku={"evsig:rot": 27.95}))
    got = {dict(v["specifics"])["Farbe"]: v["price_eur"] for v in captured["variations"]}
    assert got["Rot"] == 27.95 and got["Blau"] == 19.95   # Rot per Specifics gesetzt, Blau unberuehrt


def test_apply_variant_prices_ebay_driven_exact_even_when_internal_unmappable(db, monkeypatch):
    """GELD-SICHERHEIT: selbst wenn das interne Modell die Variante NICHT (best-effort) mappen kann
    (nichtssagende interne Werte), setzt eine echte eBay-SKU den Preis dennoch EXAKT auf ihre
    Variation. Der Write haengt NICHT am internen Modell."""
    from app.services import golive_service as gl
    # intern nichtssagend ('Standard'/'Standard') -> keine EK-Zuordnung moeglich
    p, l = _live_multi_listing(db, ebay_sku="uuid-op", axes_values=("Standard", "Standard"))

    captured = {}
    class _FakeEbay:
        async def _first_offer_for_sku(self, sku): return None
        async def get_item_price_info(self, item_id):
            return {"item_id": item_id, "current_price": 19.95, "variations": [
                {"sku": "OP-1", "price": 19.95, "quantity": "3", "specifics": [("Farbe", "Rot")]},
                {"sku": "OP-2", "price": 19.95, "quantity": "3", "specifics": [("Farbe", "Blau")]}]}
        async def revise_variation_prices(self, item_id, variations):
            captured["variations"] = variations; return len(variations)
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    asyncio.run(gl.apply_variant_prices(db, listing_id=l.id, price_by_sku={"OP-2": 34.95}))
    got = {dict(v["specifics"])["Farbe"]: v["price_eur"] for v in captured["variations"]}
    assert got["Blau"] == 34.95 and got["Rot"] == 19.95   # OP-2 (Blau) exakt gesetzt


def test_preview_variant_prices_ebay_driven_false_forces_internal(db, monkeypatch):
    """Bulk-Marge (ebay_driven=False) nutzt bewusst das INTERNE Modell – KEIN GetItem je Listing,
    Zeilen-SKU = interne {base}-V{i}."""
    from app.services import golive_service as gl
    p, l = _live_multi_listing(db, ebay_sku="AE-INT", axes_values=("Rot", "Blau"))

    class _FakeEbay:
        async def get_item_price_info(self, item_id):
            raise AssertionError("ebay_driven=False darf KEIN GetItem ausloesen!")
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    prev = asyncio.run(gl.preview_variant_prices(db, listing_id=l.id, mode="margin", value=20,
                                                 ebay_driven=False))
    assert prev["ebay_source"] is False
    assert {r["sku"] for r in prev["rows"]} == {"AE-INT-V1", "AE-INT-V2"}   # interne Positions-SKUs


def test_apply_variant_prices_ebay_driven_persists_override_via_signature(db, monkeypatch, tmp_path):
    """Regression (Review 20.07.): eBay-getriebene Zeilen tragen die ECHTE eBay-SKU als Key (nicht
    {base}-V{i}). Der manuelle Preis muss trotzdem als Override (variant_prices, ueberlebt Re-Publish)
    persistiert werden – best-effort ueber die EINDEUTIGE Merkmals-Signatur (selbst publizierte
    Listings: eBay-Werte == interne Werte)."""
    from app.services import golive_service as gl
    from app.services import listing_match_service as lms
    monkeypatch.setattr(lms, "REPORT_FILE", str(tmp_path / "r.json"))
    # ebay_sku = Gruppen-Key -> interne SKU (AE-GRP-GRP-V1) weicht von der ECHTEN eBay-SKU ab
    p, l = _live_multi_listing(db, ebay_sku="AE-GRP-GRP", axes_values=("Rot", "Blau"))

    class _FakeEbay:
        async def _first_offer_for_sku(self, sku): return None       # klassisch (Trading)
        async def get_item_price_info(self, item_id):
            return {"item_id": item_id, "current_price": 19.95, "variations": [
                {"sku": "REAL-R", "price": 19.95, "quantity": "9", "specifics": [("Farbe", "Rot")]},
                {"sku": "REAL-B", "price": 19.95, "quantity": "9", "specifics": [("Farbe", "Blau")]}]}
        async def revise_variation_prices(self, item_id, variations): return len(variations)
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    asyncio.run(gl.apply_variant_prices(db, listing_id=l.id, price_by_sku={"REAL-R": 27.95}))
    db.refresh(l)
    # Override je AE-attr persistiert (ueber die Signatur 'rot'), obwohl der Key die echte SKU war
    assert (l.variant_prices or {}).get("14:1") == 27.95


def test_apply_variant_prices_colliding_signature_no_cockpit_poisoning(db, monkeypatch, tmp_path):
    """GELD-SCHUTZ (Review 20.07.): teilen zwei echte eBay-Variationen dieselbe normalisierte
    Merkmals-Signatur, darf das Anheben der EINEN NICHT ueber den 'sig:'-Cache den Preis der ANDEREN
    (unberuehrten) Variation zu hoch anzeigen (versteckter Verlust). Die kollidierende Signatur wird
    daher NICHT als 'sig:'-Key geschrieben."""
    from decimal import Decimal
    from app.models import Product, Listing
    from app.services import golive_service as gl
    from app.services import listing_match_service as lms
    monkeypatch.setattr(lms, "REPORT_FILE", str(tmp_path / "r.json"))
    # zwei Varianten mit kollidierender Wert-Signatur (nur Gross-/Kleinschreibung), aber eigenen SKUs
    p = Product(aliexpress_url="https://ae/col", aliexpress_id="col", price_cny=Decimal("6"),
                variants={"axes": {"Farbe": ["Marineblau", "MarineBlau"]},
                          "skus": [{"attr": "c1", "options": {"Farbe": "Marineblau"}, "id": 1, "price": 6, "stock": 9},
                                   {"attr": "c2", "options": {"Farbe": "MarineBlau"}, "id": 2, "price": 6, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="uuid-col", ebay_item_id="ECOL", title_seo="X", description="d",
                listing_status="active", price_eur=Decimal("19.95"),
                ebay_live_prices={"sig:marineblau": 19.95})
    db.add(l); db.commit()

    class _FakeEbay:
        async def _first_offer_for_sku(self, sku): return None
        async def get_item_price_info(self, item_id):
            return {"item_id": item_id, "current_price": 19.95, "variations": [
                {"sku": "COL-A", "price": 19.95, "quantity": "9", "specifics": [("Farbe", "Marineblau")]},
                {"sku": "COL-B", "price": 19.95, "quantity": "9", "specifics": [("Farbe", "MarineBlau")]}]}
        async def revise_variation_prices(self, item_id, variations): return len(variations)
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    # Nur COL-A anheben
    asyncio.run(gl.apply_variant_prices(db, listing_id=l.id, price_by_sku={"COL-A": 34.95}))
    db.refresh(l)
    lp = l.ebay_live_prices or {}
    # COL-A unter seiner ECHTEN SKU nachgezogen ...
    assert lp.get("COL-A") == 34.95
    # ... aber die KOLLIDIERENDE Signatur NICHT auf 34,95 gehoben (sonst zeigte COL-B faelschlich 34,95)
    assert lp.get("sig:marineblau") != 34.95


def test_apply_variant_prices_ebay_driven_grp_inventory_persists_override_via_inv_alt(db, monkeypatch, tmp_path):
    """Regression (Review 20.07., 2. Runde): eine selbst publizierte -GRP-Variante wird ueber die
    Inventory-API (Offer per ECHTER Offer-SKU) gesetzt. Schickt der eBay-getriebene Dialog die ECHTE
    Offer-SKU als Key, muss der Override dennoch persistieren – die interne SKU wird ueber inv_alt
    (Offer-SKU {publish-base}-V{i} -> interne {group-key}-V{i}) aufgeloest."""
    from decimal import Decimal
    from app.models import Product, Listing
    from app.services import golive_service as gl
    from app.services import listing_match_service as lms
    monkeypatch.setattr(lms, "REPORT_FILE", str(tmp_path / "r.json"))
    p = Product(aliexpress_url="https://ae/gi", aliexpress_id="gi", price_cny=Decimal("6"),
                variants={"axes": {"Farbe": ["Rot", "Blau"]},
                          "skus": [{"attr": "14:1", "options": {"Farbe": "Rot"}, "id": 1, "price": 6, "stock": 9},
                                   {"attr": "14:2", "options": {"Farbe": "Blau"}, "id": 2, "price": 6, "stock": 9}]})
    db.add(p); db.flush()
    # ebay_sku = Gruppen-Key; ebay_draft_id = {publish-base}-GRP -> alt_base = "AE-GI"
    l = Listing(product_id=p.id, ebay_sku="AE-GI-GRP", ebay_item_id="EGI", ebay_draft_id="AE-GI-GRP",
                title_seo="X", description="d", listing_status="active", price_eur=Decimal("17.95"))
    db.add(l); db.commit()

    bulk_calls = []
    class _FakeEbay:
        async def _first_offer_for_sku(self, sku):
            return {"offerId": f"off-{sku}"} if str(sku) in ("AE-GI-V1", "AE-GI-V2") else None
        async def get_item_price_info(self, item_id):
            return {"item_id": item_id, "current_price": 17.95, "variations": [
                {"sku": "AE-GI-V1", "price": 17.95, "quantity": "9", "specifics": [("Farbe", "Rot")]},
                {"sku": "AE-GI-V2", "price": 17.95, "quantity": "9", "specifics": [("Farbe", "Blau")]}]}
        async def bulk_update_price(self, updates): bulk_calls.extend(updates)
    monkeypatch.setattr(gl, "_real_ebay", lambda: _FakeEbay())

    # Dialog schickt die ECHTE Offer-SKU 'AE-GI-V1' (nicht 'AE-GI-GRP-V1')
    asyncio.run(gl.apply_variant_prices(db, listing_id=l.id, price_by_sku={"AE-GI-V1": 24.95}))
    assert any(u["sku"] == "AE-GI-V1" and u["price_eur"] == 24.95 for u in bulk_calls)  # per Inventory-API
    db.refresh(l)
    assert (l.variant_prices or {}).get("14:1") == 24.95   # Override ueber inv_alt persistiert


# ----------------------------- Ausverkauft -> Menge 0 (C) -----------------------------
def test_publish_multi_out_of_stock_variant_gets_qty_zero(db, monkeypatch):
    p, l = _variant_product(db, stocks=(9, 0))   # Rot ausverkauft

    created = []

    class _Ebay:
        async def get_required_aspects(self, cat): return []
        async def create_inventory_item(self, sku, **kw):
            created.append((sku, kw.get("quantity")))
        async def create_offer(self, sku, **kw): return f"offer-{sku}"
        async def create_inventory_item_group(self, *a, **kw): return None
        async def publish_offer_by_inventory_item_group(self, gk): return "ITEM-1"
        async def _first_offer_for_sku(self, sku): return None
        async def delete_inventory_item(self, sku): return None

    from app.config import get_settings
    axis_names, variants = golive_service._usable_variants(p)
    asyncio.run(golive_service._publish_multi(
        _Ebay(), l, p, category="123", aspects={"Marke": "Markenlos"}, brand="Markenlos",
        axis_names=axis_names, variants=variants, base_sku="AE-PT",
        settings=get_settings(), draft_only=True))
    qmap = dict(created)
    # V1 (Schwarz, Bestand 9) -> Standardmenge; V2 (Rot, Bestand 0) -> 0
    assert qmap["AE-PT-V1"] > 0
    assert qmap["AE-PT-V2"] == 0
