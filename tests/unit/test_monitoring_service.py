"""Tests fuer den Monitoring-/Repricing-Service (Preis- + Bestands-Sync)."""
from __future__ import annotations

from decimal import Decimal

from app.models import Listing, PriceHistory, Product
from app.services import monitoring_service


def _make_listing(db, *, url, price_eur=Decimal("99.00"), auto=True, status="active"):
    ae_id = str(abs(hash(url)) % 10_000_000)
    product = Product(aliexpress_url=url, aliexpress_id=ae_id)
    db.add(product)
    db.flush()
    listing = Listing(
        product_id=product.id, ebay_sku=f"AE-{ae_id}", title_seo="Test", description="d",
        listing_status=status, price_eur=price_eur, cost_eur=Decimal("5.00"),
        markup_pct=Decimal("0.35"), auto_reprice=auto, quantity_available=1,
        supplier_in_stock=True, monitor_status="ok",
    )
    db.add(listing)
    db.commit()
    return listing


async def test_sync_flags_price_drift_but_never_reprices(db):
    """KEIN Auto-Repricing (Nutzer-Regel): Abweichung wird nur MARKIERT.

    Der eBay-Preis bleibt unangetastet; die Anpassung passiert manuell im
    Preis-Check. Frueher wurde hier automatisch repriced - bewusst entfernt."""
    listing = _make_listing(db, url="https://de.aliexpress.com/item/rep.html",
                            price_eur=Decimal("999.00"))
    res = await monitoring_service.sync_listing(db, listing_id=listing.id)
    assert res["action"] == "price_drift"
    assert res.get("target_price_eur") is not None
    db.refresh(listing)
    assert float(listing.price_eur) == 999.0          # Preis NICHT veraendert
    assert listing.monitor_status == "price_changed"  # aber als Abweichung markiert
    assert float(listing.cost_eur) != 5.0             # EK wurde aktualisiert
    hist = db.query(PriceHistory).filter_by(listing_id=listing.id, reason="reprice").all()
    assert hist == []                                  # keine Auto-Reprice-Historie mehr


async def test_sync_marks_out_of_stock(db):
    listing = _make_listing(db, url="https://de.aliexpress.com/item/oos-x.html")
    res = await monitoring_service.sync_listing(db, listing_id=listing.id)
    assert res["action"] == "out_of_stock"
    db.refresh(listing)
    assert listing.monitor_status == "out_of_stock"
    assert listing.supplier_in_stock is False
    assert listing.quantity_available == 0


async def test_restock_resets_quantity(db):
    listing = _make_listing(db, url="https://de.aliexpress.com/item/back.html")
    listing.supplier_in_stock = False
    listing.quantity_available = 0
    db.commit()
    await monitoring_service.sync_listing(db, listing_id=listing.id)
    db.refresh(listing)
    assert listing.supplier_in_stock is True
    # Restock setzt auf die Standard-Sichtmenge (default_listing_quantity), nicht 1
    from app.config import get_settings
    assert listing.quantity_available == get_settings().default_listing_quantity


async def test_run_monitoring_aggregates(db):
    _make_listing(db, url="https://de.aliexpress.com/item/a.html", price_eur=Decimal("999"))
    _make_listing(db, url="https://de.aliexpress.com/item/oos-b.html")
    res = await monitoring_service.run_monitoring(db)
    assert res["checked"] == 2
    assert res["out_of_stock"] == 1
    assert res["errors"] == 0


def test_monitor_status_reports_stats(db):
    _make_listing(db, url="https://de.aliexpress.com/item/s.html")
    status = monitoring_service.monitor_status(db)
    assert status["stats"]["monitored"] == 1
    assert status["stats"]["auto_reprice_on"] == 1
    assert len(status["items"]) == 1
    assert status["items"][0]["profit_eur"] is not None


async def test_run_monitoring_skips_sales_hold(db):
    """Verkaufs-Sperre: Monitor fasst das Listing NICHT an -> kein Auto-Restock.

    Ohne Sperre wuerde die (lieferbare) Ware mit Menge 0 auf die Standardmenge
    zurueckgesetzt. Mit sales_hold bleibt sie auf 0 (dauerhafte Pause)."""
    held = _make_listing(db, url="https://de.aliexpress.com/item/hold.html")
    held.sales_hold = True
    held.hold_reason = "Falsche Maße – Neu-Einstellung nötig"
    held.quantity_available = 0
    db.commit()
    res = await monitoring_service.run_monitoring(db)
    assert res["held"] == 1
    assert res["checked"] == 0
    db.refresh(held)
    assert held.quantity_available == 0          # bleibt pausiert, kein Restock


def test_monitor_status_exposes_hold(db):
    listing = _make_listing(db, url="https://de.aliexpress.com/item/h2.html")
    listing.sales_hold = True
    listing.hold_reason = "Falsche Poster-Maße"
    db.commit()
    status = monitoring_service.monitor_status(db)
    assert status["stats"]["held"] == 1
    item = next(i for i in status["items"] if i["listing_id"] == listing.id)
    assert item["sales_hold"] is True
    assert item["hold_reason"] == "Falsche Poster-Maße"


async def test_sync_rechnet_eu_lager_nicht_wie_china(db, monkeypatch):
    """Der Monitor darf bei EU-Lager keinen Zoll und keinen China-Versand aufschlagen.

    Fund 29.08.2026: sync_listing rief price_from_cny OHNE local= auf (Vorgabe False)
    und schlug damit die Zollpauschale (3,57 EUR) auf Ware aus einem deutschen Lager.
    Der Import (product_service:349) macht es richtig, dieser Aufruf nicht - und
    ueberschreibt cost_eur bei jedem Lauf.

    Folge im Testbestand: alle zwanzig Entwuerfe trugen einen um 3,57 EUR zu hohen EK,
    waehrend der Preis unveraendert blieb. Die Marge sah nach 4 % aus statt nach 20 %,
    und jedes Listing war als "price_changed" markiert - zwanzig Fehlalarme, die zu
    unnoetigen Preiserhoehungen gefuehrt haetten.
    """
    from app.integrations.aliexpress import ScrapedProduct
    from app.services import product_service

    listing = _make_listing(db, url="https://de.aliexpress.com/item/eulager.html",
                            price_eur=Decimal("18.95"))
    product = db.get(Product, listing.product_id)

    def _scraped(ship_from):
        return ScrapedProduct(
            aliexpress_id=product.aliexpress_id, title_raw="T-Shirt Baumwolle",
            description_raw="d", price_cny=Decimal("9.08"),
            images=["https://x/1.jpg"],
            variants={"skus": [{"attr": "a", "price": "9.08", "stock": 10,
                                "options": {"Größe": "M"}, "ship_from": ship_from}],
                      "axes": {"Größe": ["M"]}},
            specs=[], supplier_id="1", supplier_rating=Decimal("0"),
            in_stock=True, stock_reported=True)

    async def _kein_versand(*a, **kw):
        return 0.0
    monkeypatch.setattr(product_service, "resolve_supplier_ship_eur", _kein_versand)

    kosten = {}
    for name, ship_from in (("eu", "Deutschland"), ("china", "China")):
        async def _scrape(url, _s=ship_from):
            return _scraped(_s)
        from app.integrations import get_aliexpress_client
        monkeypatch.setattr(get_aliexpress_client(), "scrape_product", _scrape)
        await monitoring_service.sync_listing(db, listing_id=listing.id)
        db.refresh(listing)
        kosten[name] = float(listing.cost_eur)

    assert kosten["eu"] == 9.08, (
        f"EU-Lager: nur der Warenpreis, kein Zoll - war {kosten['eu']}")
    assert kosten["china"] > kosten["eu"], (
        "aus China gehoeren Versand und Zoll SEHR WOHL dazu - sonst waere die Bremse "
        "zu weit gegangen")
