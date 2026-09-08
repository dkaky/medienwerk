"""Orders-Liste: chronologisch nach ECHTEM Verkaufsdatum (sale_date), nicht created_at.

Regression: die Liste war nach created_at (DB-Einfuegezeit) sortiert -> bei
Backfill/Import stimmte die Reihenfolge nicht und aktuelle Verkaeufe fielen aus
dem Limit ("verschwunden", keine Tracking-Zuordnung mehr moeglich).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.models import Listing, Product, Sale
from app.services import analytics_service


def test_list_orders_sorted_by_sale_date_not_created_at(db):
    # Sale A: ALTES Verkaufsdatum, aber ZUERST eingefuegt (aeltestes created_at).
    a = Sale(ebay_transaction_id="A", status="delivered", price_eur=Decimal("10"),
             sale_date=datetime(2026, 1, 1, tzinfo=timezone.utc))
    db.add(a); db.commit()
    # Sale B: NEUES Verkaufsdatum, danach eingefuegt.
    b = Sale(ebay_transaction_id="B", status="delivered", price_eur=Decimal("10"),
             sale_date=datetime(2026, 6, 1, tzinfo=timezone.utc))
    db.add(b); db.commit()
    # Sale C: Verkaufsdatum DAZWISCHEN, aber als LETZTES eingefuegt (neuestes created_at).
    c = Sale(ebay_transaction_id="C", status="delivered", price_eur=Decimal("10"),
             sale_date=datetime(2026, 3, 1, tzinfo=timezone.utc))
    db.add(c); db.commit()

    result = analytics_service.list_orders(db, status="all", limit=100)
    order = [o["ebay_transaction_id"] for o in result["orders"]]

    # Nach sale_date absteigend: B (Jun) > C (Mär) > A (Jan) – NICHT created_at (A,B,C).
    assert order == ["B", "C", "A"]


def test_list_orders_respects_high_limit(db):
    for i in range(60):
        db.add(Sale(ebay_transaction_id=f"S{i}", status="delivered",
                    price_eur=Decimal("5"),
                    sale_date=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    db.commit()
    # Frueher Default-Limit 50 -> aeltere fielen raus; jetzt zeigen wir alle.
    result = analytics_service.list_orders(db, status="all", limit=1000)
    assert len(result["orders"]) == 60


def test_variant_image_matches_by_value_not_axis_name():
    # eBay hat die Achse umbenannt (Farbe -> Duft), die WERTE stimmen aber.
    p = Product(aliexpress_url="https://de.aliexpress.com/item/vi.html", aliexpress_id="vi",
                variants={"skus": [
                    {"attr": "a", "options": {"Farbe": "Rot"}, "image": "http://img/rot.jpg"},
                    {"attr": "b", "options": {"Farbe": "Blau"}, "image": "http://img/blau.jpg"}]})
    assert analytics_service._variant_image(p, {"Duft": "Blau"}) == "http://img/blau.jpg"
    assert analytics_service._variant_image(p, {"Farbe": "Rot"}) == "http://img/rot.jpg"
    # kein Treffer / keine Auswahl -> None (Fallback = Listing-Hauptbild im Frontend)
    assert analytics_service._variant_image(p, {"Farbe": "Grün"}) is None
    assert analytics_service._variant_image(p, None) is None
    assert analytics_service._variant_image(None, {"Farbe": "Rot"}) is None


def test_list_orders_exposes_item_id_and_variant_image(db):
    p = Product(aliexpress_url="https://de.aliexpress.com/item/oi.html", aliexpress_id="oi",
                variants={"skus": [
                    {"attr": "a", "options": {"Farbe": "Rot"}, "image": "http://img/rot.jpg"},
                    {"attr": "b", "options": {"Farbe": "Blau"}, "image": "http://img/blau.jpg"}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-1", title_seo="Shirt", description="d",
                listing_status="active", ebay_item_id="1122334455",
                image_url="http://img/main.jpg", price_eur=Decimal("19.95"))
    db.add(l); db.flush()
    db.add(Sale(ebay_transaction_id="S1", status="delivered", price_eur=Decimal("19.95"),
                listing_id=l.id, variant_selected={"Farbe": "Blau"},
                sale_date=datetime(2026, 5, 1, tzinfo=timezone.utc)))
    db.commit()
    o = analytics_service.list_orders(db, status="all", limit=10)["orders"][0]
    assert o["ebay_item_id"] == "1122334455"
    assert o["variant_image"] == "http://img/blau.jpg"   # verkaufte Variante, nicht Hauptbild


# ------------- EK je VERKAUFTER Variante statt Worst-Case (Vorfall Smart-Brille) -------------
def _mk_multivariant(db, *, with_learned_map=False):
    p = Product(aliexpress_url="https://de.aliexpress.com/item/mv1.html", aliexpress_id="mv1",
                variants={"axes": {"Farbnamen": ["as picture"]},
                          "skus": [
                              {"id": "S1", "attr": "182:10#as picture", "price": "8.0",
                               "options": {"Farbnamen": "as picture"},
                               "image": "https://img/sku10.jpg"},
                              {"id": "S2", "attr": "182:175#as picture", "price": "22.0",
                               "options": {"Farbnamen": "as picture"},
                               "image": "https://img/sku175.jpg"}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, title_seo="Smart Brille", description="d",
                listing_status="active", price_eur=Decimal("12.95"),
                cost_eur=Decimal("30.19"),               # bewusst Worst-Case (teuerste Variante)
                supplier_ship_eur=Decimal("1.99"))
    if with_learned_map:
        from app.services import order_service
        key = order_service.variant_map_key({"Farbe": "Only Goggles (365458)"})
        l.variant_map = {f"mv1|{key}": "182:175#as picture", key: "182:175#as picture"}
    db.add(l); db.commit()
    return p, l


def test_economics_uses_sold_variant_cost_not_listing_worst_case(db, monkeypatch):
    """Sale mit aufgeloestem attr: EK = SKU-Preis der GEKAUFTEN (guenstigen) Variante,
    nicht der Listing-Worst-Case (30,19) -> kein falscher Verlust mehr."""
    from app.services import pricing
    from app.config import get_settings
    # Varianten-Wahl isoliert: EK-Aufschlaege (Prozent + Pauschalzoll) neutralisieren, damit
    # der Test allein prueft, dass der GEKAUFTE (8 EUR) statt der Worst-Case-EK (30,19) zaehlt.
    monkeypatch.setattr(get_settings(), "aliexpress_tax_pct", 0.0)
    monkeypatch.setattr(get_settings(), "customs_fee_eur", 0.0)
    p, l = _mk_multivariant(db)
    s = Sale(ebay_transaction_id="mv-a", listing_id=l.id, status="delivered", quantity=1,
             price_eur=Decimal("12.95"), fee_eur_actual=Decimal("2.00"),
             variant_selected={"Farbe": "X", "attr": "182:10#as picture"})
    db.add(s); db.commit()
    eco = analytics_service._order_economics(s, None, l, p, fee_pct=0.22, fixed_fee=0.45)
    expected = round(pricing.effective_cost_bundle(8.0, quantity=1, settings=get_settings(),
                                                   ship_override=1.99), 2)
    assert eco["cost"] == expected and eco["cost_source"] == "variant_est"
    assert eco["profit"] == round(12.95 - expected - 2.00, 2)
    assert eco["profit"] > 0                      # vorher: 12.95 - 30.19 - 2.00 = Verlust


def test_economics_unresolvable_multivariant_is_honest_none(db):
    """Multivarianten-Produkt, Variante NICHT aufloesbar -> EK unbekannt (None) statt
    Worst-Case-Verlust (Nutzerregel: keine geschaetzten Finanzzahlen)."""
    p, l = _mk_multivariant(db)
    s = Sale(ebay_transaction_id="mv-b", listing_id=l.id, status="delivered", quantity=1,
             price_eur=Decimal("12.95"), variant_selected=None)
    db.add(s); db.commit()
    eco = analytics_service._order_economics(s, None, l, p, fee_pct=0.22, fixed_fee=0.45)
    assert eco["cost"] is None and eco["profit"] is None and eco["cost_known"] is False


def test_economics_single_variant_listing_estimate_times_qty(db):
    """Produkt ohne echte Varianten: Listing-EK bleibt korrekt – jetzt MAL Menge."""
    p = Product(aliexpress_url="https://de.aliexpress.com/item/sv2.html", aliexpress_id="sv2",
                variants={"skus": [{"id": "S", "attr": "1:1#one", "price": "5.0",
                                    "options": {"Farbe": "one"}}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, title_seo="Einzel", description="d", listing_status="active",
                price_eur=Decimal("19.95"), cost_eur=Decimal("7.00"))
    db.add(l); db.flush()
    s = Sale(ebay_transaction_id="sv-a", listing_id=l.id, status="delivered", quantity=2,
             price_eur=Decimal("39.90"), fee_eur_actual=Decimal("5.00"))
    db.add(s); db.commit()
    eco = analytics_service._order_economics(s, None, l, p, fee_pct=0.22, fixed_fee=0.45)
    assert eco["cost"] == 14.00 and eco["cost_source"] == "listing_est"


def test_variant_image_via_attr_and_learned_map(db):
    """Motocross-Fall: AE-Werte alle 'as picture' -> Werte-Vergleich scheitert.
    (1) attr am Sale trifft direkt; (2) gelernte variant_map loest eBay-Wert auf."""
    p, l = _mk_multivariant(db, with_learned_map=True)
    # 1) attr direkt
    img = analytics_service._variant_image(p, {"Farbe": "egal", "attr": "182:10#as picture"}, l)
    assert img == "https://img/sku10.jpg"
    # 2) ohne attr, aber gelernte Map fuer den eBay-Wert
    img2 = analytics_service._variant_image(p, {"Farbe": "Only Goggles (365458)"}, l)
    assert img2 == "https://img/sku175.jpg"
    # 3) gar kein Anhaltspunkt -> None (Fallback Hauptbild macht das Frontend)
    assert analytics_service._variant_image(p, {"Farbe": "Unbekannt (999)"}, l) is None


def test_variant_image_prefers_own_ebay_listing_image(db):
    """Nutzer-Wunsch 11.07.: die Orders-Vorschau zeigt das Varianten-Bild des EIGENEN
    eBay-Listings (ebay_variant_images), nicht das AliExpress-SKU-Bild."""
    from app.services import order_service
    p, l = _mk_multivariant(db, with_learned_map=True)
    key = order_service.variant_map_key({"Farbe": "Only Goggles (365458)"})
    l.ebay_variant_images = {key: "https://ebay.img/eigenes-varianten-bild.jpg"}
    db.commit()
    img = analytics_service._variant_image(p, {"Farbe": "Only Goggles (365458)"}, l)
    assert img == "https://ebay.img/eigenes-varianten-bild.jpg"   # eBay-Bild schlaegt AE-Bild


def test_pull_ebay_variant_images_builds_map(db, monkeypatch):
    """pull_ebay_variant_images: Gruppe -> variantSKUs -> Inventory-Items (variierende
    Aspekte + Bild) -> Map am Listing (Schluessel = variant_map_key der eBay-Auswahl)."""
    import asyncio
    from app.services import golive_service, order_service

    p, l = _mk_multivariant(db)
    l.ebay_sku = "AE-77"; l.ebay_draft_id = "AE-77-GRP"
    db.commit()

    class _Ebay:
        async def get_inventory_item_group(self, gk):
            assert gk == "AE-77-GRP"
            return {"variantSKUs": ["AE-77-V1", "AE-77-V2"],
                    "variesBy": {"specifications": [{"name": "Farbe"}]}}

        async def get_inventory_item(self, sku):
            n = sku[-1]
            return {"product": {
                "aspects": {"Farbe": [f"Only Goggles (36545{n})"], "Marke": ["Markenlos"]},
                "imageUrls": [f"https://ebay.img/v{n}.jpg"]}}

    monkeypatch.setattr(golive_service, "_real_ebay", lambda: _Ebay())
    r = asyncio.run(golive_service.pull_ebay_variant_images(db, listing_id=l.id))
    assert r["images"] == 2
    db.refresh(l)
    k1 = order_service.variant_map_key({"Farbe": "Only Goggles (365451)"})
    assert l.ebay_variant_images[k1] == "https://ebay.img/v1.jpg"
    # Nicht-variierende Aspekte (Marke) fliessen NICHT in den Schluessel ein
    assert all("markenlos" not in k for k in l.ebay_variant_images)


def test_attach_receipt_creates_order_row_and_sets_real_cost(db):
    """Beleg-Upload fuer manuell bestellte Sales (keine Order-Zeile): Zeile wird angelegt,
    echter EK (amount) gesetzt -> Orders-Gewinn wird real (Fall Smart-Brille)."""
    from sqlalchemy import select
    from app.models import OrderAliexpress
    from app.services import invoice_service
    p, l = _mk_multivariant(db)
    s = Sale(ebay_transaction_id="blg-1", listing_id=l.id, status="delivered", quantity=1,
             price_eur=Decimal("12.95"))
    db.add(s); db.commit()
    invoice_service.attach_original_receipt(
        db, file_bytes=b"fake-img", ext="png", invoice_type="aliexpress_purchase",
        sale_id=s.id, amount=10.48)
    o = db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == s.id))
    assert o is not None and float(o.cost_cny) == 10.48
    eco = analytics_service._order_economics(s, o, l, p, fee_pct=0.22, fixed_fee=0.45)
    assert eco["cost"] == 10.48 and eco["cost_is_actual"] is True


def test_pull_ebay_variant_images_trading_fallback(db, monkeypatch):
    """Importiertes (Trading-)Listing ohne Inventory-Gruppe (Fall Motocross): Bilder aus
    GetItem/VariationSpecificPictureSet; Schluessel matcht die Verkaufs-Auswahl."""
    import asyncio
    from app.services import golive_service, order_service

    p, l = _mk_multivariant(db)
    l.ebay_item_id = "389813036022"; l.ebay_draft_id = None
    db.commit()

    class _Ebay:
        async def get_item_variation_pictures(self, item_id):
            assert item_id == "389813036022"
            return {"axis_name": "Farbe", "values": {
                "Only Goggles (365458)": "https://i.ebayimg.com/v365458.jpg",
                "Only Goggles (200004889)": "https://i.ebayimg.com/v200004889.jpg"}}

    monkeypatch.setattr(golive_service, "_real_ebay", lambda: _Ebay())
    r = asyncio.run(golive_service.pull_ebay_variant_images(db, listing_id=l.id))
    assert r["images"] == 2 and r["note"] == "trading"
    db.refresh(l)
    # Genau die Auswahl, die eBay am Verkauf liefert, findet jetzt ihr Bild:
    img = analytics_service._variant_image(p, {"Farbe": "Only Goggles (200004889)"}, l)
    assert img == "https://i.ebayimg.com/v200004889.jpg"
