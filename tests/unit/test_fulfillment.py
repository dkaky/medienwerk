"""Tests fuer Auto-Fulfillment: eBay-Order-Sync, Varianten-Aufloesung, Tracking-Rueckmeldung."""
from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import select

from app.models import Listing, OrderAliexpress, Product, Sale
from app.services import order_service


# ----------------------------- Varianten-Aufloesung -----------------------------
def _product_with_variants(db):
    p = Product(
        aliexpress_url="https://de.aliexpress.com/item/ful-1.html", aliexpress_id="ful1",
        variants={"axes": {"Farbe": ["WEISS", "Rot"]},
                  "skus": [{"attr": "14:29", "options": {"Farbe": "WEISS"}},
                           {"attr": "14:10", "options": {"Farbe": "Rot"}}]},
    )
    db.add(p)
    db.commit()
    return p


def test_resolve_variant_maps_ebay_aspect_to_ali_attr(db):
    p = _product_with_variants(db)
    resolved = order_service._resolve_variant(p, {"Farbe": "WEISS"})
    assert resolved["attr"] == "14:29"
    # case-insensitive Achsenname
    assert order_service._resolve_variant(p, {"farbe": "Rot"})["attr"] == "14:10"
    # nichts ausgewaehlt -> None
    assert order_service._resolve_variant(p, None) is None


def test_match_listing_by_variant_sku_base(db):
    p = Product(aliexpress_url="https://de.aliexpress.com/item/ful-2.html", aliexpress_id="ful2")
    db.add(p)
    db.flush()
    listing = Listing(product_id=p.id, ebay_sku="AE-999", title_seo="T", description="d",
                      listing_status="active")
    db.add(listing)
    db.commit()
    # Varianten-SKU 'AE-999-V3' faellt auf Basis-SKU 'AE-999' zurueck
    assert order_service._match_listing(db, "AE-999-V3", None).id == listing.id


# ----------------------------- getOrders-Parser -----------------------------
def test_order_lines_parses_address_and_variant():
    order = {
        "orderId": "05-12345", "legacyOrderId": "111-222", "creationDate": "2026-07-01T10:00:00.000Z",
        "fulfillmentStartInstructions": [{"shippingStep": {"shipTo": {
            "fullName": "Erika Muster",
            "contactAddress": {"addressLine1": "Hauptstr. 5", "city": "Witten",
                               "stateOrProvince": "NRW", "postalCode": "58452", "countryCode": "DE"},
            "primaryPhone": {"phoneNumber": "+4915112345678"}}}}],
        "lineItems": [{"lineItemId": "LI-1", "legacyItemId": "800271691842", "sku": "AE-123-V2",
                       "quantity": 2, "variationAspects": [{"name": "Farbe", "value": "Rot"}],
                       "lineItemCost": {"value": "24.95", "currency": "EUR"}}],
    }
    lines = order_service._order_lines(order)
    assert len(lines) == 1
    f = lines[0]
    assert f["tx_id"] == "111-222-LI-1"
    assert f["ebay_order_id"] == "05-12345"
    assert f["ebay_line_item_id"] == "LI-1"
    assert f["delivery_address"]["city"] == "Witten"
    assert f["delivery_address"]["postal"] == "58452"
    assert f["variant_selected"] == {"Farbe": "Rot"}
    assert f["quantity"] == 2
    assert f["price_eur"] == Decimal("24.95")


# ----------------------------- Order-Sync -----------------------------
class _FakeEbay:
    def __init__(self, orders):
        self._orders = orders
        self.reported = []

    async def list_all_orders(self, *, since, max_orders=200):
        return self._orders

    async def create_shipping_fulfillment(self, ebay_order_id, *, tracking_number, line_items,
                                          carrier_code=None, shipped_date=None):
        self.reported.append((ebay_order_id, tracking_number, tuple(li["lineItemId"] for li in line_items)))
        return "FUL-1"


def test_sync_ebay_orders_creates_pending_sales_idempotent(db, monkeypatch):
    order = {
        "orderId": "O-1", "legacyOrderId": "L-1", "creationDate": "2026-07-01T09:00:00.000Z",
        "fulfillmentStartInstructions": [{"shippingStep": {"shipTo": {
            "fullName": "Max", "contactAddress": {"city": "Köln", "postalCode": "50667", "countryCode": "DE"}}}}],
        "lineItems": [{"lineItemId": "A", "sku": "AE-1", "quantity": 1,
                       "lineItemCost": {"value": "12.00"}}],
    }
    monkeypatch.setattr(order_service, "_real_ebay", lambda: _FakeEbay([order]))
    r = asyncio.run(order_service.sync_ebay_orders(db, days=30))
    assert r["sales_created"] == 1
    sale = db.query(Sale).filter(Sale.ebay_transaction_id == "L-1-A").one()
    assert sale.status == "pending"
    assert sale.ebay_line_item_id == "A"
    # zweiter Lauf legt NICHTS doppelt an
    r2 = asyncio.run(order_service.sync_ebay_orders(db, days=30))
    assert r2["sales_created"] == 0


def _ebay_order(oid, *, fulfillment=None, li_fulfillment=None, cancel=None):
    li = {"lineItemId": "A", "sku": "AE-1", "quantity": 1, "lineItemCost": {"value": "12.00"}}
    if li_fulfillment:
        li["lineItemFulfillmentStatus"] = li_fulfillment
    o = {
        "orderId": oid, "legacyOrderId": oid, "creationDate": "2026-07-01T09:00:00.000Z",
        "fulfillmentStartInstructions": [{"shippingStep": {"shipTo": {
            "fullName": "Max", "contactAddress": {"city": "Köln", "postalCode": "50667",
                                                  "countryCode": "DE"}}}}],
        "lineItems": [li],
    }
    if fulfillment:
        o["orderFulfillmentStatus"] = fulfillment
    if cancel:
        o["cancelStatus"] = {"cancelState": cancel}
    return o


def test_sync_marks_sale_shipped_when_ebay_fulfilled(db, monkeypatch):
    """eBay-Versandstatus zurueck in die DB: FULFILLED -> lokale offene Sale wird 'tracking'."""
    sale = Sale(ebay_transaction_id="LF-1-A", ebay_order_id="LF-1", ebay_line_item_id="A",
                quantity=1, status="ordered_aliexpress")
    db.add(sale); db.commit()
    monkeypatch.setattr(order_service, "_real_ebay",
                        lambda: _FakeEbay([_ebay_order("LF-1", fulfillment="FULFILLED",
                                                       li_fulfillment="FULFILLED")]))
    r = asyncio.run(order_service.sync_ebay_orders(db, days=30))
    assert r["marked_shipped"] == 1
    db.refresh(sale)
    assert sale.status == "tracking"


def test_sync_not_started_keeps_sale_open(db, monkeypatch):
    sale = Sale(ebay_transaction_id="LF-3-A", ebay_order_id="LF-3", ebay_line_item_id="A",
                quantity=1, status="ordered_aliexpress")
    db.add(sale); db.commit()
    monkeypatch.setattr(order_service, "_real_ebay",
                        lambda: _FakeEbay([_ebay_order("LF-3", fulfillment="NOT_STARTED")]))
    r = asyncio.run(order_service.sync_ebay_orders(db, days=30))
    assert r["marked_shipped"] == 0
    db.refresh(sale)
    assert sale.status == "ordered_aliexpress"


def test_sync_fulfilled_never_overwrites_cancelled(db, monkeypatch):
    """Ein Storno bleibt Storno, auch wenn eBay die Order als FULFILLED fuehrt."""
    sale = Sale(ebay_transaction_id="LF-2-A", ebay_order_id="LF-2", ebay_line_item_id="A",
                quantity=1, status="cancelled", ebay_cancel_state="CANCELED")
    db.add(sale); db.commit()
    monkeypatch.setattr(order_service, "_real_ebay",
                        lambda: _FakeEbay([_ebay_order("LF-2", fulfillment="FULFILLED",
                                                       li_fulfillment="FULFILLED")]))
    r = asyncio.run(order_service.sync_ebay_orders(db, days=30))
    assert r["marked_shipped"] == 0
    db.refresh(sale)
    assert sale.status == "cancelled"


def test_sync_creates_fulfilled_order_as_shipped(db, monkeypatch):
    """Neu importierte, auf eBay bereits versandte Order -> direkt 'tracking', nicht 'pending'."""
    monkeypatch.setattr(order_service, "_real_ebay",
                        lambda: _FakeEbay([_ebay_order("LF-4", fulfillment="FULFILLED",
                                                       li_fulfillment="FULFILLED")]))
    r = asyncio.run(order_service.sync_ebay_orders(db, days=30))
    assert r["sales_created"] == 1 and r["marked_shipped"] == 1
    sale = db.query(Sale).filter(Sale.ebay_transaction_id == "LF-4-A").one()
    assert sale.status == "tracking"


# ----------------------------- Tracking an eBay melden -----------------------------
def test_report_tracking_to_ebay(db, monkeypatch):
    sale = Sale(ebay_transaction_id="L-9-A", ebay_order_id="O-9", ebay_line_item_id="A",
                buyer_name="Max", quantity=1, status="ordered_aliexpress")
    db.add(sale)
    db.flush()
    order = OrderAliexpress(sale_id=sale.id, aliexpress_order_id="AE-77", quantity=1,
                            status="shipped", tracking_number="LP123456789CN", tracking_carrier="Cainiao")
    db.add(order)
    db.commit()

    fake = _FakeEbay([])
    monkeypatch.setattr(order_service, "_real_ebay", lambda: fake)
    # Neue Sicherung (Vorfall 2026-07-03): Meldungen nur mit MONITOR_PUSH_REAL
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "monitor_push_real", True)
    r = asyncio.run(order_service.report_tracking_to_ebay(db, order_id=order.id))
    assert r["fulfillment_id"] == "FUL-1"
    assert fake.reported == [("O-9", "LP123456789CN", ("A",))]
    db.refresh(sale)
    assert sale.status == "tracking"


def test_import_orders_csv_maps_and_links(db):
    from app.services import order_import_service as imp
    # Passende eBay-Sale fuer die Verknuepfung
    sale = Sale(ebay_transaction_id="111-999-A", ebay_order_id="111-999", quantity=1, status="pending")
    db.add(sale)
    db.commit()
    csv = (
        "AliExpress Order ID;eBay Order ID;Order Date;Product Title;Product Cost;Quantity;Tracking Number\n"
        "8000000001;111-999;2026-06-15;HTC NE70;22,79;1;LP123CN\n"
        "8000000002;;2026-06-16;Kette Gold;6,09 EUR;2;\n"
    )
    r = imp.import_orders_csv(db, content=csv.encode("utf-8"))
    assert r["created"] == 2
    assert r["linked_to_sale"] == 1                       # erste Zeile -> Sale
    assert r["columns_detected"]["cost"] == "Product Cost"
    o1 = db.scalar(select(OrderAliexpress).where(OrderAliexpress.aliexpress_order_id == "8000000001"))
    assert float(o1.cost_cny) == 22.79 and o1.sale_id == sale.id and o1.tracking_number == "LP123CN"
    o2 = db.scalar(select(OrderAliexpress).where(OrderAliexpress.aliexpress_order_id == "8000000002"))
    assert float(o2.cost_cny) == 6.09 and o2.quantity == 2
    # idempotent: zweiter Lauf legt nichts neu an
    r2 = imp.import_orders_csv(db, content=csv.encode("utf-8"))
    assert r2["created"] == 0 and r2["updated"] == 2


def test_import_orders_csv_rejects_unknown_columns(db):
    import pytest
    from app.retry import PersistentError
    from app.retry import PersistentError
    from app.services import order_import_service as imp
    with pytest.raises(PersistentError):
        imp.import_orders_csv(db, content=b"foo;bar\n1;2\n")


def test_retry_failed_publishes_retries_only_open_drafts(db, monkeypatch):
    """Selbstheilung: nur fehlgeschlagene, noch offene Drafts werden erneut versucht."""
    from app.models import TaskLog
    from app.services import golive_service as g

    p = Product(aliexpress_url="https://de.aliexpress.com/item/rt-1.html",
                aliexpress_id="rt1", images=["http://img/1.jpg"])
    db.add(p)
    db.flush()
    draft = Listing(product_id=p.id, ebay_sku="AE-RT1", title_seo="T", description="d",
                    listing_status="draft", price_eur=Decimal("19.95"))
    live = Listing(product_id=p.id, ebay_sku="AE-RT2", title_seo="T2", description="d",
                   listing_status="active", ebay_item_id="123", price_eur=Decimal("9.95"))
    db.add_all([draft, live])
    db.flush()
    db.add(TaskLog(task_type="golive", reference_id=str(draft.id), status="failed",
                   error_message="eBay 500"))
    db.add(TaskLog(task_type="golive", reference_id=str(live.id), status="failed",
                   error_message="eBay 500"))
    db.commit()

    calls = []

    async def fake_publish(_db, *, listing_id, draft_only=False):
        calls.append(("direct", listing_id, draft_only))
        return {"status": "active"}

    async def fake_enqueue(listing_id):
        calls.append(("queue", listing_id))
        return {"status": "queued"}

    from app.services import publish_queue
    monkeypatch.setattr(g, "publish_listing_live", fake_publish)
    monkeypatch.setattr(publish_queue, "enqueue", fake_enqueue)
    r = asyncio.run(g.retry_failed_publishes(db))
    assert r["candidates"] == 1 and r["succeeded"] == 1
    # Nur der offene Draft, nicht das Live-Listing — und Live-Publishes laufen
    # seit der Publish-Queue IMMER ueber enqueue (Doppel-Publish-Schutz).
    assert calls == [("queue", draft.id)]


def test_usable_variants_filters_junk_and_dedupes():
    from app.services.golive_service import _usable_variants

    class P:
        def __init__(self, variants):
            self.variants = variants

    # Platzhalter "as picture shows" für alle -> keine echte Variante -> EINZEL
    junk = P({"axes": {"Farbe": ["as picture shows"]},
              "skus": [{"options": {"Farbe": "as picture shows"}} for _ in range(6)]})
    assert _usable_variants(junk) == ([], [])

    # nur 1 Variante -> EINZEL
    single = P({"axes": {"Farbe": ["Black"]}, "skus": [{"options": {"Farbe": "Black"}}]})
    assert _usable_variants(single) == ([], [])

    # echte Varianten -> MULTI mit distinkten Kombinationen
    real = P({"axes": {"Farbe": ["Rot", "Blau", "Rot"]},
              "skus": [{"options": {"Farbe": "Rot"}}, {"options": {"Farbe": "Blau"}},
                       {"options": {"Farbe": "Rot"}}]})
    axes, uniq = _usable_variants(real)
    assert axes == ["Farbe"] and len(uniq) == 2  # dupliziertes "Rot" entfernt


def test_sync_tracking_all_reports_new_tracking(db, monkeypatch):
    """Auto-Sync: Order ohne Tracking holt es (Mock-AE) und meldet an eBay."""
    sale = Sale(ebay_transaction_id="TS-1-A", ebay_order_id="TS-1", ebay_line_item_id="A",
                quantity=1, status="ordered_aliexpress")
    db.add(sale)
    db.flush()
    order = OrderAliexpress(sale_id=sale.id, aliexpress_order_id="8000000099",
                            quantity=1, status="ordered")
    db.add(order)
    # CSV-Import (synthetische Referenz) darf NICHT angefasst werden
    db.add(OrderAliexpress(aliexpress_order_id="EBAY-111-0", quantity=1,
                           status="shipped", tracking_number="OLD1"))
    db.commit()

    fake = _FakeEbay([])
    monkeypatch.setattr(order_service, "_real_ebay", lambda: fake)
    from app.config import get_settings
    from app.integrations.aliexpress import TrackingInfo
    # FINALE DHL-Nummer -> wird gemeldet (nur finale Nummern gehen an eBay)
    monkeypatch.setattr(order_service, "get_aliexpress_client",
                        lambda: _FakeAE(TrackingInfo("00340434886281338687",
                                                     "AliExpress Selection Standard", "versendet")))
    monkeypatch.setattr(get_settings(), "monitor_push_real", True)
    r = asyncio.run(order_service.sync_tracking_all(db))
    assert r["checked"] == 1 and r["reported_to_ebay"] == 1 and r["errors"] == 0
    db.refresh(order)
    assert order.tracking_number == "00340434886281338687"  # finale DHL-Nummer geholt
    assert fake.reported and fake.reported[0][0] == "TS-1"
    db.refresh(sale)
    assert sale.status == "tracking"
    # zweiter Lauf: nichts mehr zu tun (idempotent)
    r2 = asyncio.run(order_service.sync_tracking_all(db))
    assert r2["checked"] == 0 and r2["reported_to_ebay"] == 0


def test_report_tracking_requires_tracking_number(db, monkeypatch):
    import pytest
    from app.retry import PersistentError
    from app.retry import PersistentError
    sale = Sale(ebay_transaction_id="L-8-A", ebay_order_id="O-8", ebay_line_item_id="A",
                quantity=1, status="ordered_aliexpress")
    db.add(sale)
    db.flush()
    order = OrderAliexpress(sale_id=sale.id, aliexpress_order_id="AE-8", quantity=1, status="ordered")
    db.add(order)
    db.commit()
    monkeypatch.setattr(order_service, "_real_ebay", lambda: _FakeEbay([]))
    with pytest.raises(PersistentError):
        asyncio.run(order_service.report_tracking_to_ebay(db, order_id=order.id))


# --------------------- Tracking von Hand nachtragen (manuelle AE-Order) ---------------------
def test_manual_tracking_creates_row_for_manual_order(db, monkeypatch):
    """Manuell auf AliExpress bestellter Sale hat KEINE OrderAliexpress-Zeile.
    add_manual_tracking legt sie an, meldet an eBay und markiert versendet."""
    from app.config import get_settings
    sale = Sale(ebay_transaction_id="L-M1-A", ebay_order_id="O-M1", ebay_line_item_id="A",
                buyer_name="Erika Manuell", quantity=1, status="ordered_aliexpress")
    db.add(sale)
    db.commit()
    # Voraussetzung: es gibt wirklich keine Order-Zeile
    assert db.query(OrderAliexpress).filter(OrderAliexpress.sale_id == sale.id).first() is None

    fake = _FakeEbay([])
    monkeypatch.setattr(order_service, "_real_ebay", lambda: fake)
    monkeypatch.setattr(get_settings(), "monitor_push_real", True)
    r = asyncio.run(order_service.add_manual_tracking(
        db, sale_id=sale.id, tracking_number="003ABCDEFGH", aliexpress_order_id="3074000000000000"))
    assert r["status"] == "reported"
    assert r["manual"] is True
    assert fake.reported == [("O-M1", "003ABCDEFGH", ("A",))]
    order = db.query(OrderAliexpress).filter(OrderAliexpress.sale_id == sale.id).one()
    assert order.tracking_number == "003ABCDEFGH"
    assert order.aliexpress_order_id == "3074000000000000"
    assert order.tracking_carrier == "DHL"  # 003-Prefix -> DHL erkannt
    db.refresh(sale)
    assert sale.status == "tracking"


def test_manual_tracking_fills_app_order_without_tracking(db, monkeypatch):
    """App hat bestellt (Order-Zeile da), aber die AliExpress-API liefert keine
    Sendungsnummer. Der Nutzer traegt sie nach -> vorhandene Zeile wird ergaenzt."""
    from app.config import get_settings
    sale = Sale(ebay_transaction_id="L-M2-A", ebay_order_id="O-M2", ebay_line_item_id="A",
                quantity=1, status="ordered_aliexpress")
    db.add(sale)
    db.flush()
    order = OrderAliexpress(sale_id=sale.id, aliexpress_order_id="AE-M2", quantity=1, status="ordered")
    db.add(order)
    db.commit()

    fake = _FakeEbay([])
    monkeypatch.setattr(order_service, "_real_ebay", lambda: fake)
    monkeypatch.setattr(get_settings(), "monitor_push_real", True)
    r = asyncio.run(order_service.add_manual_tracking(
        db, sale_id=sale.id, tracking_number="LP987654321CN"))
    assert r["tracking_number"] == "LP987654321CN"
    # Keine zweite Zeile angelegt
    assert db.query(OrderAliexpress).filter(OrderAliexpress.sale_id == sale.id).count() == 1
    db.refresh(order)
    assert order.aliexpress_order_id == "AE-M2"  # unveraendert
    assert order.tracking_number == "LP987654321CN"


def test_manual_tracking_rejects_empty_number(db):
    import pytest
    from app.retry import PersistentError
    from app.retry import PersistentError
    sale = Sale(ebay_transaction_id="L-M3-A", ebay_order_id="O-M3", ebay_line_item_id="A",
                quantity=1, status="ordered_aliexpress")
    db.add(sale)
    db.commit()
    with pytest.raises(PersistentError):
        asyncio.run(order_service.add_manual_tracking(db, sale_id=sale.id, tracking_number="  "))


# ---------------- NUR finale DHL-/Hermes-Nummern melden (Nutzerregel 05.07.) ----------------
def test_is_final_tracking_only_real_carriers():
    f = order_service._is_final_tracking
    assert f("00340434886281338687") is True    # DHL (003…)
    assert f("H1234567890ABCD") is True          # Hermes (H + 13)
    assert f("AP00826710436942") is False        # AliExpress/Cainiao provisorisch
    assert f("LP00826331957556") is False        # AliExpress/Cainiao provisorisch
    assert f("15827812345678901234") is False    # reine 20-stellige Cainiao-Nummer
    assert f(None) is False
    assert f("") is False


class _FakeAE:
    def __init__(self, info):
        self._info = info

    async def get_tracking(self, aeid):
        return self._info


def test_refresh_tracking_ignores_provisional_number(db, monkeypatch):
    from app.integrations.aliexpress import TrackingInfo
    sale = Sale(ebay_transaction_id="L-PR-A", ebay_order_id="O-PR", ebay_line_item_id="A",
                quantity=1, status="ordered_aliexpress")
    db.add(sale); db.flush()
    order = OrderAliexpress(sale_id=sale.id, aliexpress_order_id="AE-PR", quantity=1, status="ordered")
    db.add(order); db.commit()
    monkeypatch.setattr(order_service, "get_aliexpress_client",
                        lambda: _FakeAE(TrackingInfo("AP00826710436942",
                                                     "AliExpress Selection Standard", "unterwegs")))
    r = asyncio.run(order_service.refresh_tracking(db, order_id=order.id))
    db.refresh(order)
    assert order.tracking_number is None      # provisorische Nummer NICHT hinterlegt
    assert r["tracking_number"] is None


def test_refresh_tracking_stores_final_dhl(db, monkeypatch):
    from app.integrations.aliexpress import TrackingInfo
    sale = Sale(ebay_transaction_id="L-FN-A", ebay_order_id="O-FN", ebay_line_item_id="A",
                quantity=1, status="ordered_aliexpress")
    db.add(sale); db.flush()
    order = OrderAliexpress(sale_id=sale.id, aliexpress_order_id="AE-FN", quantity=1, status="ordered")
    db.add(order); db.commit()
    monkeypatch.setattr(order_service, "get_aliexpress_client",
                        lambda: _FakeAE(TrackingInfo("00340434886281338687",
                                                     "AliExpress Selection Standard", "Zugestellt")))
    asyncio.run(order_service.refresh_tracking(db, order_id=order.id))
    db.refresh(order)
    assert order.tracking_number == "00340434886281338687"
    assert order.tracking_carrier == "DHL"


def test_sync_tracking_all_skips_provisional_number(db, monkeypatch):
    """Die Auto-Sync meldet eine provisorische Nummer NICHT an eBay (reported=0)."""
    from app.integrations.aliexpress import TrackingInfo
    from app.config import get_settings
    sale = Sale(ebay_transaction_id="L-SP-A", ebay_order_id="O-SP", ebay_line_item_id="A",
                quantity=1, status="ordered_aliexpress")
    db.add(sale); db.flush()
    order = OrderAliexpress(sale_id=sale.id, aliexpress_order_id="AE-SP", quantity=1, status="ordered")
    db.add(order); db.commit()
    monkeypatch.setattr(order_service, "get_aliexpress_client",
                        lambda: _FakeAE(TrackingInfo("LP00826331957556",
                                                     "AliExpress Selection Standard", "unterwegs")))
    fake = _FakeEbay([])
    monkeypatch.setattr(order_service, "_real_ebay", lambda: fake)
    monkeypatch.setattr(get_settings(), "monitor_push_real", True)
    r = asyncio.run(order_service.sync_tracking_all(db))
    assert r["reported_to_ebay"] == 0
    assert fake.reported == []
    db.refresh(order)
    assert order.tracking_number is None


async def test_auto_fulfill_due_disabled_by_default(db):
    """Regression: auto_fulfill_due crashte mit NameError (get_settings nicht
    importiert), bevor der AUTO_FULFILL-Schalter geprueft wurde."""
    from app.services.order_service import auto_fulfill_due

    result = await auto_fulfill_due(db)
    assert result == {"enabled": False, "ordered": 0}


# ----------------------------- Auto-Fulfillment: Marge-Sperre -----------------------------
def _pending_sale(db, *, price, cost, aid="afm1"):
    """Bestellbereite Sale (pending) mit Listing+Produkt und gesetzten EK/VK."""
    from datetime import datetime, timezone
    p = Product(aliexpress_url=f"https://de.aliexpress.com/item/{aid}.html", aliexpress_id=aid)
    db.add(p)
    db.flush()
    listing = Listing(
        product_id=p.id, title_seo="T", description="d", listing_status="active",
        price_eur=Decimal(str(price)) if price is not None else None,
        cost_eur=Decimal(str(cost)) if cost is not None else None,
    )
    db.add(listing)
    db.flush()
    sale = Sale(
        ebay_transaction_id=f"tx-{aid}", listing_id=listing.id, buyer_name="Max Muster",
        quantity=1, status="pending",
        delivery_address={"city": "Köln", "postal": "50667", "country": "DE"},
        sale_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    db.add(sale)
    db.commit()
    return sale


def test_auto_fulfill_margin_ok_helper():
    """Reine Margen-Formel: >=20% ok, darunter/unbekannt/EK<=0 -> nicht ok (Fail-safe)."""
    from app.config import get_settings
    from app.services.order_service import _auto_fulfill_margin_ok

    s = get_settings()
    # price=30, cost=10 -> Gebuehr inkl. MwSt, marge ~39% -> ok
    ok, m = _auto_fulfill_margin_ok(Listing(price_eur=Decimal("30"), cost_eur=Decimal("10")), s)
    assert ok is True and m > 0.35
    # price=20, cost=14 -> marge ~5.75% -> nicht ok
    ok, _ = _auto_fulfill_margin_ok(Listing(price_eur=Decimal("20"), cost_eur=Decimal("14")), s)
    assert ok is False
    # Verlust -> nicht ok
    ok, _ = _auto_fulfill_margin_ok(Listing(price_eur=Decimal("15"), cost_eur=Decimal("14")), s)
    assert ok is False
    # EK unbekannt -> Fail-safe nicht ok
    ok, m = _auto_fulfill_margin_ok(Listing(price_eur=Decimal("30"), cost_eur=None), s)
    assert ok is False and m is None
    # VK unbekannt -> Fail-safe nicht ok
    ok, _ = _auto_fulfill_margin_ok(Listing(price_eur=None, cost_eur=Decimal("10")), s)
    assert ok is False
    # EK<=0 -> Fail-safe nicht ok
    ok, _ = _auto_fulfill_margin_ok(Listing(price_eur=Decimal("30"), cost_eur=Decimal("0")), s)
    assert ok is False


async def test_auto_fulfill_skips_low_margin(db, monkeypatch):
    """Marge < 20% -> NICHT automatisch bestellen; Sale -> needs_manual_review, keine Order."""
    from app.config import get_settings
    from app.services.order_service import auto_fulfill_due

    monkeypatch.setattr(get_settings(), "auto_fulfill", True)
    sale = _pending_sale(db, price=20, cost=14, aid="lowm")  # ~5.75% Marge

    r = await auto_fulfill_due(db)

    assert r == {"enabled": True, "ordered": 0, "errors": 0, "skipped_margin": 1}
    db.refresh(sale)
    assert sale.status == "needs_manual_review"
    # Es wurde KEINE AliExpress-Bestellung angelegt.
    assert db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == sale.id)) is None


async def test_auto_fulfill_skips_unknown_cost(db, monkeypatch):
    """Fail-safe: EK unbekannt -> Marge nicht verifizierbar -> kein Blindkauf."""
    from app.config import get_settings
    from app.services.order_service import auto_fulfill_due

    monkeypatch.setattr(get_settings(), "auto_fulfill", True)
    sale = _pending_sale(db, price=30, cost=None, aid="noek")

    r = await auto_fulfill_due(db)

    assert r["ordered"] == 0 and r["skipped_margin"] == 1
    db.refresh(sale)
    assert sale.status == "needs_manual_review"
    assert db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == sale.id)) is None


async def test_auto_fulfill_orders_when_margin_ok(db, monkeypatch):
    """Gesunde Marge (>=20%) + gueltiger Kaeufer -> automatische Bestellung laeuft durch."""
    from app.config import get_settings
    from app.services.order_service import auto_fulfill_due

    monkeypatch.setattr(get_settings(), "auto_fulfill", True)
    sale = _pending_sale(db, price=30, cost=10, aid="okm")  # ~43% Marge

    r = await auto_fulfill_due(db)

    assert r["ordered"] == 1 and r["skipped_margin"] == 0
    db.refresh(sale)
    assert sale.status in ("ordered_aliexpress",)
    assert db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == sale.id)) is not None


async def test_auto_fulfill_grace_defers_recent_sale(db, monkeypatch):
    """Karenzzeit-Config respektiert: grace>0 verschiebt eine frische Bestellung."""
    from datetime import datetime, timezone
    from app.config import get_settings
    from app.services.order_service import auto_fulfill_due

    monkeypatch.setattr(get_settings(), "auto_fulfill", True)
    monkeypatch.setattr(get_settings(), "auto_fulfill_grace_minutes", 15)
    sale = _pending_sale(db, price=30, cost=10, aid="fresh")
    sale.sale_date = datetime.now(timezone.utc)  # gerade eben -> innerhalb Karenz
    db.commit()

    r = await auto_fulfill_due(db)

    assert r["ordered"] == 0  # Karenz noch nicht um
    db.refresh(sale)
    assert sale.status == "pending"  # bleibt fuer den naechsten Lauf liegen


def test_delete_draft_removes_draft_and_history(db):
    """Entwurf löschen: Zeile + Preis-Historie weg, Audit-Log geschrieben."""
    from app.models import PriceHistory, TaskLog
    from app.services import golive_service as g

    draft = Listing(title_seo="Wegwerf-Entwurf", description="d",
                    listing_status="draft", price_eur=Decimal("9.95"))
    db.add(draft)
    db.flush()
    db.add(PriceHistory(listing_id=draft.id, new_price_eur=Decimal("9.95"), reason="reprice"))
    db.commit()
    did = draft.id

    r = asyncio.run(g.delete_draft(db, listing_id=did))
    assert r["deleted"] is True
    assert db.get(Listing, did) is None
    assert db.query(PriceHistory).filter_by(listing_id=did).count() == 0
    log = db.query(TaskLog).filter_by(task_type="delete_draft", reference_id=str(did)).one()
    assert log.status == "success"


def test_delete_draft_refuses_active_and_sold(db):
    import pytest
    from app.retry import PersistentError
    from app.models import Sale
    from app.retry import PersistentError
    from app.services import golive_service as g

    live = Listing(title_seo="Live", description="d", listing_status="active",
                   ebay_item_id="800123", price_eur=Decimal("9.95"))
    sold = Listing(title_seo="Verkauft", description="d", listing_status="draft",
                   price_eur=Decimal("9.95"))
    db.add_all([live, sold])
    db.flush()
    db.add(Sale(listing_id=sold.id, ebay_transaction_id="t-1", price_eur=Decimal("9.95"),
                status="pending"))
    db.commit()

    with pytest.raises(PersistentError, match="Nur Entwürfe"):
        asyncio.run(g.delete_draft(db, listing_id=live.id))
    with pytest.raises(PersistentError, match="Verkäufe"):
        asyncio.run(g.delete_draft(db, listing_id=sold.id))
    assert db.get(Listing, live.id) is not None
    assert db.get(Listing, sold.id) is not None


def test_delete_draft_refuses_while_uploading(db, monkeypatch):
    import pytest
    from app.retry import PersistentError
    from app.retry import PersistentError
    from app.services import golive_service as g
    from app.services import publish_queue

    draft = Listing(title_seo="Laedt hoch", description="d", listing_status="draft",
                    price_eur=Decimal("9.95"))
    db.add(draft)
    db.commit()
    monkeypatch.setattr(publish_queue, "pending_ids", lambda: {draft.id})
    with pytest.raises(PersistentError, match="lädt gerade"):
        asyncio.run(g.delete_draft(db, listing_id=draft.id))


def _translator_product(db, *, four_variants=True):
    skus = [{"id": "1", "attr": "14:193#Black", "options": {"Farbe": "Black"}, "price": "23.39", "stock": 5},
            {"id": "2", "attr": "14:29#White", "options": {"Farbe": "White"}, "price": "23.39", "stock": 9}]
    if four_variants:
        skus += [{"id": "3", "attr": "14:691#Black AI", "options": {"Farbe": "Black AI"}, "price": "23.59", "stock": 2},
                 {"id": "4", "attr": "14:350853#White AI", "options": {"Farbe": "White AI"}, "price": "23.59", "stock": 4}]
    p = Product(aliexpress_url=f"https://de.aliexpress.com/item/var-{four_variants}.html",
                aliexpress_id=f"var{four_variants}", variants={"skus": skus})
    db.add(p)
    db.commit()
    return p


def test_resolve_variant_translates_german_names(db):
    """Schwarz==Black, Weiss==White: uebersetzte eBay-Namen loesen sich auf."""
    p = _translator_product(db, four_variants=False)
    r = order_service._resolve_variant(p, {"Farbe": "Schwarz"})
    assert r["attr"] == "14:193#Black"
    assert order_service._resolve_variant(p, {"Farbe": "Weiß"})["attr"] == "14:29#White"


def test_resolve_variant_refuses_ambiguous_prefix_trap(db):
    """'Schwarz' bei Black UND Black AI -> NICHT raten (falsche Produktversion!)."""
    p = _translator_product(db, four_variants=True)
    r = order_service._resolve_variant(p, {"Farbe": "Schwarz"})
    assert not (r or {}).get("attr")          # unaufgeloest -> manuelle Zuordnung
    # 'Schwarz KI' dagegen ist eindeutig -> Black AI
    r2 = order_service._resolve_variant(p, {"Farbe": "Schwarz KI"})
    assert r2["attr"] == "14:691#Black AI"


def test_set_sale_variant_learns_mapping_for_listing(db):
    """Manuelle Zuordnung: Sale aufgeloest + Listing lernt -> naechster Sale automatisch."""
    p = _translator_product(db, four_variants=True)
    listing = Listing(product_id=p.id, ebay_sku="AE-VAR", title_seo="T", description="d",
                      listing_status="active")
    db.add(listing)
    db.flush()
    sale = Sale(listing_id=listing.id, ebay_transaction_id="V-1", quantity=1,
                status="needs_manual_review", variant_selected={"Farbe": "Schwarz"})
    db.add(sale)
    db.commit()

    r = order_service.set_sale_variant(db, sale_id=sale.id, attr="14:691#Black AI")
    assert r["learned_for_listing"] is True
    db.refresh(sale)
    assert sale.variant_selected["attr"] == "14:691#Black AI"
    assert sale.status == "pending"
    db.refresh(listing)
    assert listing.variant_map["schwarz"] == "14:691#Black AI"   # Legacy-Key
    # Neu: zusaetzlich quellen-genamespaced gelernt (sku_attrs gelten je Quelle)
    assert any(k.endswith("|schwarz") for k in listing.variant_map)

    # Naechster Sale desselben Listings mit 'Schwarz' loest sich AUTOMATISCH auf
    r2 = order_service._resolve_variant(p, {"Farbe": "Schwarz"}, listing=listing)
    assert r2["attr"] == "14:691#Black AI"


def test_resolve_variant_smart_uses_llm_when_rules_fail(db, monkeypatch):
    """KI-Fallback: kryptische Auswahl (keine Übersetzung möglich) -> LLM + Listing lernt."""
    import asyncio as _asyncio

    # Auswahl "Only Goggles" vs. AliExpress "Standard/Deluxe" -> Regeln finden nichts,
    # nur semantisches Verständnis (LLM) löst es.
    p = Product(aliexpress_url="https://de.aliexpress.com/item/smart-1.html", aliexpress_id="sm1",
                variants={"skus": [
                    {"id": "a", "attr": "14:1#Standard", "options": {"Set": "Standard"}, "price": "10.69"},
                    {"id": "b", "attr": "14:2#Deluxe Kit", "options": {"Set": "Deluxe Kit"}, "price": "18.99"}]})
    db.add(p)
    db.flush()
    listing = Listing(product_id=p.id, ebay_sku="AE-SM", title_seo="Brille", description="d",
                      listing_status="active")
    db.add(listing)
    db.commit()

    class _LLM:
        async def match_variant(self, *, ebay_selection, ali_variants, product_title=""):
            return {"attr": "14:1#Standard", "confidence": 0.9, "reasoning": "Nur Brille = Standard"}

    monkeypatch.setattr("app.integrations.get_llm_client", lambda: _LLM())
    v, src = _asyncio.run(order_service.resolve_variant_smart(
        db, product=p, listing=listing, variant_selected={"Farbe": "Only Goggles (771)"}))
    assert src == "llm" and v["attr"] == "14:1#Standard"
    # variant_map ist gesetzt (in-memory); der Fulfill-Flow committet sie danach
    assert (listing.variant_map or {}).get("onlygoggles771") == "14:1#Standard"


def test_resolve_variant_smart_rules_handle_german_ai_without_llm(db, monkeypatch):
    """Regeln lösen 'Schwarz KI' -> 'Black AI' selbst (KI nicht nötig, spart Tokens)."""
    import asyncio as _asyncio

    p = Product(aliexpress_url="https://de.aliexpress.com/item/smart-ai.html", aliexpress_id="smai",
                variants={"skus": [
                    {"id": "a", "attr": "14:193#Black", "options": {"Farbe": "Black"}},
                    {"id": "b", "attr": "14:691#Black AI", "options": {"Farbe": "Black AI"}}]})
    db.add(p)
    db.commit()

    class _LLM:  # darf gar nicht aufgerufen werden
        async def match_variant(self, **k):
            raise AssertionError("LLM sollte für 'Schwarz KI' nicht nötig sein")

    monkeypatch.setattr("app.integrations.get_llm_client", lambda: _LLM())
    v, src = _asyncio.run(order_service.resolve_variant_smart(
        db, product=p, listing=None, variant_selected={"Farbe": "Schwarz KI"}))
    assert src == "rule" and v["attr"] == "14:691#Black AI"


def test_resolve_variant_smart_rejects_low_confidence(db, monkeypatch):
    """Unsichere KI (< Schwelle) -> keine Zuordnung, kein Blindkauf."""
    import asyncio as _asyncio

    p = Product(aliexpress_url="https://de.aliexpress.com/item/smart-2.html", aliexpress_id="sm2",
                variants={"skus": [{"id": "a", "attr": "x#as picture", "options": {"Farbe": "as picture"}}]})
    db.add(p)
    db.commit()

    class _LLM:
        async def match_variant(self, **k):
            return {"attr": "x#as picture", "confidence": 0.4, "reasoning": "unsicher"}

    monkeypatch.setattr("app.integrations.get_llm_client", lambda: _LLM())
    v, src = _asyncio.run(order_service.resolve_variant_smart(
        db, product=p, listing=None, variant_selected={"Farbe": "Only Goggles (771)"}))
    assert v is None and src == "none"


def test_resolve_variant_smart_ignores_llm_hallucination(db, monkeypatch):
    """LLM nennt einen attr, den es nicht gibt -> wird verworfen (kein Fantasie-Kauf)."""
    import asyncio as _asyncio

    p = Product(aliexpress_url="https://de.aliexpress.com/item/smart-3.html", aliexpress_id="sm3",
                variants={"skus": [{"id": "a", "attr": "14:1#Red", "options": {"Farbe": "Red"}}]})
    db.add(p)
    db.commit()

    class _LLM:
        async def match_variant(self, **k):
            return {"attr": "99:99#Erfunden", "confidence": 0.99, "reasoning": "halluziniert"}

    # match_variant im echten Client validiert attr gegen die Liste; hier testen wir,
    # dass resolve_variant_smart einen unbekannten attr ohnehin nicht anwendet.
    monkeypatch.setattr("app.integrations.get_llm_client", lambda: _LLM())
    v, src = _asyncio.run(order_service.resolve_variant_smart(
        db, product=p, listing=None, variant_selected={"Farbe": "Blau"}))
    assert v is None and src == "none"


def test_mock_match_variant_no_prefix_misfire(db):
    """Fix: Mock darf 'Black' bei [Black, Black AI] NICHT auto-zuordnen (Fehlkauf-Schutz)."""
    import asyncio as _asyncio
    from app.integrations.llm import MockLLMClient
    m = MockLLMClient()
    ali = [{"attr": "14:193#Black", "name": "Black"}, {"attr": "14:691#Black AI", "name": "Black AI"}]
    # "Black" ist Präfix von "Black AI" -> mehrdeutig -> leer
    r = _asyncio.run(m.match_variant(ebay_selection={"Farbe": "Black"}, ali_variants=ali))
    assert r["attr"] == "" and r["confidence"] == 0.0
    # exakt eindeutig -> Treffer
    r2 = _asyncio.run(m.match_variant(ebay_selection={"Farbe": "Black AI"}, ali_variants=ali))
    assert r2["attr"] == "14:691#Black AI"
    # alle "as picture" -> nie ein Treffer
    ap = [{"attr": "x#as picture", "name": "as picture"}, {"attr": "y#as picture", "name": "as picture"}]
    r3 = _asyncio.run(m.match_variant(ebay_selection={"Farbe": "as picture"}, ali_variants=ap))
    assert r3["attr"] == ""


def test_resolve_variant_smart_blocks_llm_prefix_choice(db, monkeypatch):
    """Fix: Auch eine SELBSTSICHERE KI darf 'Black' bei existierendem 'Black AI'
    nicht auto-kaufen -> manuelle Zuordnung, keine gelernte Fehl-Map."""
    import asyncio as _asyncio

    p = Product(aliexpress_url="https://de.aliexpress.com/item/amb.html", aliexpress_id="amb",
                variants={"skus": [
                    {"id": "a", "attr": "14:193#Black", "options": {"Farbe": "Black"}},
                    {"id": "b", "attr": "14:691#Black AI", "options": {"Farbe": "Black AI"}}]})
    db.add(p)
    db.flush()
    listing = Listing(product_id=p.id, ebay_sku="AE-AMB", title_seo="X", description="d",
                      listing_status="active")
    db.add(listing)
    db.commit()

    class _LLM:
        async def match_variant(self, **k):
            return {"attr": "14:193#Black", "confidence": 0.99, "reasoning": "rät Black"}

    monkeypatch.setattr("app.integrations.get_llm_client", lambda: _LLM())
    v, src = _asyncio.run(order_service.resolve_variant_smart(
        db, product=p, listing=listing, variant_selected={"Farbe": "Schwarz"}))
    assert v is None and src == "none"          # NICHT übernommen
    assert not (listing.variant_map or {})      # NICHT gelernt


def test_set_sale_variant_can_correct_existing_mapping(db):
    """Fix: Eine (falsch) zugeordnete Variante lässt sich über denselben Weg korrigieren."""
    p = Product(aliexpress_url="https://de.aliexpress.com/item/corr.html", aliexpress_id="corr",
                variants={"skus": [
                    {"id": "a", "attr": "14:193#Black", "options": {"Farbe": "Black"}},
                    {"id": "b", "attr": "14:691#Black AI", "options": {"Farbe": "Black AI"}}]})
    db.add(p)
    db.flush()
    listing = Listing(product_id=p.id, ebay_sku="AE-CORR", title_seo="X", description="d",
                      listing_status="active")
    db.add(listing)
    db.commit()
    sale = Sale(listing_id=listing.id, ebay_transaction_id="C-1", quantity=1,
                status="pending", variant_selected={"Farbe": "Schwarz"})
    db.add(sale)
    db.commit()

    # 1. Zuordnung (falsch: Black)
    order_service.set_sale_variant(db, sale_id=sale.id, attr="14:193#Black")
    db.refresh(listing); db.refresh(sale)
    assert listing.variant_map["schwarz"] == "14:193#Black"
    assert sale.variant_selected["Farbe"] == "Schwarz"   # eBay-Text bewahrt
    # 2. Korrektur (richtig: Black AI) über denselben Sale -> map ÜBERSCHRIEBEN
    order_service.set_sale_variant(db, sale_id=sale.id, attr="14:691#Black AI")
    db.refresh(listing)
    assert listing.variant_map["schwarz"] == "14:691#Black AI"


def test_set_sale_variant_preserves_ebay_sku(db):
    """Fix: das Zuordnen einer Variante darf die eBay-Publish-SKU NICHT loeschen – sie wird fuer die
    positions-basierte Vorwahl/Korrektur gebraucht (ging bisher ueber _RESOLVED_KEYS verloren)."""
    p = Product(aliexpress_url="https://de.aliexpress.com/item/eb.html", aliexpress_id="eb",
                variants={"skus": [{"id": 1, "attr": "14:1", "options": {"Farbe": "Rot"}},
                                   {"id": 2, "attr": "14:2", "options": {"Farbe": "Blau"}}]})
    db.add(p); db.flush()
    listing = Listing(product_id=p.id, ebay_sku="AE-EB", title_seo="X", description="d",
                      listing_status="active")
    db.add(listing); db.commit()
    sale = Sale(listing_id=listing.id, ebay_transaction_id="EB-1", quantity=1, status="pending",
                variant_selected={"Farbe": "Blau", "ebay_sku": "AE-EB-V2"})
    db.add(sale); db.commit()
    order_service.set_sale_variant(db, sale_id=sale.id, attr="14:1")   # (bewusst falsch: Rot)
    db.refresh(sale); db.refresh(listing)
    assert sale.variant_selected["attr"] == "14:1"
    assert sale.variant_selected["ebay_sku"] == "AE-EB-V2"   # SKU BEWAHRT (fuer Positions-Korrektur)
    assert sale.variant_selected["Farbe"] == "Blau"          # eBay-Achse bewahrt
    assert sale.variant_selected["variant_locked"] is True   # menschliche Zuordnung -> gesperrt
    assert listing.variant_map.get("blau") == "14:1"         # Map-Schluessel OHNE ebay_sku/lock


def test_set_sale_status_and_bulk(db):
    """Manuelle Status-Aenderung + Bulk-Umschreibung (nur lokal)."""
    from app.services import order_service as os_
    a = Sale(ebay_transaction_id="ss-1", status="pending", price_eur=Decimal("10"))
    b = Sale(ebay_transaction_id="ss-2", status="pending", price_eur=Decimal("10"))
    db.add_all([a, b]); db.commit()
    # Einzeln setzen
    os_.set_sale_status(db, sale_id=a.id, status="delivered")
    db.refresh(a); assert a.status == "delivered"
    # Ungueltiger Status wird abgelehnt
    import pytest
    from app.retry import PersistentError
    from app.retry import PersistentError
    with pytest.raises(PersistentError):
        os_.set_sale_status(db, sale_id=a.id, status="quatsch")
    # Bulk: alle pending -> ordered_aliexpress
    r = os_.bulk_set_status(db, from_statuses=["pending"], to_status="ordered_aliexpress")
    assert r["updated"] == 1
    db.refresh(b); assert b.status == "ordered_aliexpress"


def test_mark_cancel_reviewed(db):
    from app.services import order_service as os_
    s = Sale(ebay_transaction_id="cr-1", status="cancelled", price_eur=Decimal("5"))
    db.add(s); db.commit()
    os_.mark_cancel_reviewed(db, sale_id=s.id)
    db.refresh(s); assert s.cancel_reviewed is True


def test_fulfill_sale_blocks_loss_order(db):
    """VERLUST-SPERRE: Einkauf >= eBay-Netto -> STOPP, kein Kauf (Fall HTC NE40)."""
    import pytest
    from app.retry import PersistentError
    from app.retry import PersistentError
    # VK 25, EK 26 -> Netto (25 - Gebuehren) < 26 -> Verlust
    sale = _pending_sale(db, price=25, cost=26, aid="loss1")
    with pytest.raises(PersistentError) as exc:
        asyncio.run(order_service.fulfill_sale(db, sale_id=sale.id))
    assert "VERLUST-STOPP" in str(exc.value)
    db.refresh(sale)
    assert sale.status == "needs_manual_review"
    assert db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == sale.id)) is None


def test_fulfill_sale_allows_healthy_margin(db):
    """Gesunde Marge -> Bestellung laeuft durch (kein Verlust-Stopp)."""
    sale = _pending_sale(db, price=30, cost=10, aid="ok1")
    r = asyncio.run(order_service.fulfill_sale(db, sale_id=sale.id))
    assert r["status"] == "ordered"
    assert db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == sale.id)) is not None


async def test_fulfill_single_variant_auto_selects_sku(db, monkeypatch):
    """Ein-SKU-Produkt ohne Käufer-Auswahl: die einzige Variante wird automatisch bestellt
    (place_order bekommt die sku_id) – sonst SKU_NOT_EXIST (Vorfall Sale 1116 Pinsel-Set)."""
    from decimal import Decimal
    from app.models import Product, Listing, Sale, OrderAliexpress
    from app.services import order_service

    p = Product(aliexpress_url="https://de.aliexpress.com/item/sv1.html", aliexpress_id="sv1",
                variants={"axes": {"Farbe": ["5 PCs"]},
                          "skus": [{"id": "SKU-ONLY", "attr": "14:10#5 PCs",
                                    "options": {"Farbe": "5 PCs"}, "price": "6.09", "stock": 100}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, title_seo="Pinsel Set", description="d", listing_status="active",
                price_eur=Decimal("16.95"), cost_eur=Decimal("8.08"))
    db.add(l); db.flush()
    s = Sale(ebay_transaction_id="tx-sv1", listing_id=l.id, buyer_name="Max Muster", quantity=1,
             status="needs_manual_review", variant_selected=None, price_eur=Decimal("16.95"),
             delivery_address={"street": "Begonienstr. 1", "city": "Köln", "postal": "50667",
                               "country": "DE"})
    db.add(s); db.commit()

    captured = {}

    class _Placed:
        aliexpress_order_id = "AE-OK-1"
        cost_cny = None
        tracking_number = None

    class _AE:
        async def place_order(self, *, url, variant, quantity, delivery_name, delivery_address):
            captured["variant"] = variant
            return _Placed()

        async def scrape_product(self, url):
            raise AssertionError("kein Scrape nötig – Varianten sind vorhanden")

        async def get_tracking(self, aliexpress_order_id):
            class _T:
                tracking_number = None
                carrier = None
                estimated_delivery = None
            return _T()

    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: _AE())

    await order_service.fulfill_sale(db, sale_id=s.id)

    assert captured["variant"] and captured["variant"].get("attr") == "14:10#5 PCs"
    assert captured["variant"].get("id") == "SKU-ONLY"          # richtige sku_id an AliExpress
    db.refresh(s)
    assert s.status == "ordered_aliexpress"
    assert db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == s.id)) is not None


async def test_record_ebay_tracking_pulls_and_stores(db, monkeypatch):
    """Selbst verschickt + Nummer auf eBay eingetragen: record_ebay_tracking liest sie und legt
    sie lokal ab – OHNE erneut an eBay zu melden (Eigenversand, keine AE-Order). Sale 812."""
    from decimal import Decimal
    from app.models import Listing, Product, Sale, OrderAliexpress
    from app.services import order_service

    p = Product(aliexpress_url="https://de.aliexpress.com/item/wk1.html", aliexpress_id="wk1")
    db.add(p); db.flush()
    l = Listing(product_id=p.id, title_seo="Weekender", description="d", listing_status="active",
                price_eur=Decimal("30"))
    db.add(l); db.flush()
    s = Sale(ebay_transaction_id="tx-wk1", ebay_order_id="13-14856-16626", ebay_line_item_id="li1",
             listing_id=l.id, buyer_name="Agnes Epping", quantity=1, status="tracking",
             delivery_address={"city": "Köln", "country": "DE"})
    db.add(s); db.commit()

    class _FakeEbay:
        async def get_shipping_fulfillments(self, ebay_order_id):
            assert ebay_order_id == "13-14856-16626"
            return [{"tracking_number": "02187120004168", "carrier": "DHL",
                     "shipped_date": "2026-07-06T10:07:54.000Z"}]

    monkeypatch.setattr(order_service, "_real_ebay", lambda: _FakeEbay())

    r = await order_service.record_ebay_tracking(db, sale_id=s.id)
    assert r["found"] and r["tracking_number"] == "02187120004168"
    o = db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == s.id))
    assert o is not None and o.tracking_number == "02187120004168"
    assert o.aliexpress_order_id is None            # Eigenversand: keine AliExpress-Bestellung
    assert o.tracking_carrier == "DHL"
    db.refresh(s); assert s.status == "tracking"


def test_discard_order_for_reorder(db):
    """Verfallene/unbezahlte Order verwerfen -> Sale wieder pending, Order-Zeile weg (Sale 1116)."""
    from datetime import datetime, timezone
    p = Product(aliexpress_url="https://de.aliexpress.com/item/ro1.html", aliexpress_id="ro1")
    db.add(p); db.flush()
    l = Listing(product_id=p.id, title_seo="T", description="d", listing_status="active")
    db.add(l); db.flush()
    s = Sale(ebay_transaction_id="tx-ro1", listing_id=l.id, quantity=1, status="ordered_aliexpress")
    db.add(s); db.flush()
    o = OrderAliexpress(sale_id=s.id, product_id=p.id, aliexpress_order_id="AE-EXPIRED",
                        quantity=1, status="ordered", order_date=datetime.now(timezone.utc))
    db.add(o); db.commit()

    r = order_service.discard_order_for_reorder(db, sale_id=s.id)
    assert r["discarded"] and r["status"] == "pending"
    assert r["old_aliexpress_order_id"] == "AE-EXPIRED"
    db.refresh(s); assert s.status == "pending"
    assert db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == s.id)) is None


def test_discard_order_refuses_when_shipped(db):
    """Versandte/getrackte Bestellung wird NIE verworfen (kein Doppelkauf)."""
    import pytest
    from app.retry import PersistentError
    from datetime import datetime, timezone
    from app.retry import PersistentError
    p = Product(aliexpress_url="https://de.aliexpress.com/item/ro2.html", aliexpress_id="ro2")
    db.add(p); db.flush()
    l = Listing(product_id=p.id, title_seo="T", description="d", listing_status="active")
    db.add(l); db.flush()
    s = Sale(ebay_transaction_id="tx-ro2", listing_id=l.id, quantity=1, status="tracking")
    db.add(s); db.flush()
    o = OrderAliexpress(sale_id=s.id, product_id=p.id, aliexpress_order_id="AE-SHIPPED",
                        tracking_number="003123", quantity=1, status="shipped",
                        order_date=datetime.now(timezone.utc))
    db.add(o); db.commit()
    with pytest.raises(PersistentError):
        order_service.discard_order_for_reorder(db, sale_id=s.id)
    assert db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == s.id)) is not None


async def test_fulfill_blocks_when_supplier_stock_below_quantity(db, monkeypatch):
    """Vorfall Sale 1124 (Adler Kette): SKU-Bestand 1, bestellt 3 -> AliExpress wuerde nur
    kryptisch DELIVERY_METHOD_NOT_EXIST melden. Der Preflight stoppt VOR dem Geld-Call
    mit Klartext und setzt needs_manual_review."""
    import pytest
    from app.retry import PersistentError
    from decimal import Decimal
    from app.models import Product, Listing, Sale
    from app.retry import PersistentError
    from app.services import order_service

    p = Product(aliexpress_url="https://de.aliexpress.com/item/stk1.html", aliexpress_id="stk1",
                variants={"axes": {"Farbe": ["Mit Kette", "Nur Anhänger"]},
                          "skus": [
                              {"id": "K1", "attr": "200000783:175", "price": "3.85", "stock": 1,
                               "options": {"Farbe": "Mit Kette"}},
                              {"id": "K2", "attr": "200000783:193", "price": "2.85", "stock": 50,
                               "options": {"Farbe": "Nur Anhänger"}}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, title_seo="Adler Kette", description="d",
                listing_status="active", price_eur=Decimal("14.95"), cost_eur=Decimal("5.84"))
    db.add(l); db.flush()
    s = Sale(ebay_transaction_id="tx-stk1", listing_id=l.id, buyer_name="Max", quantity=3,
             status="pending", variant_selected={"Farbe": "Mit Kette"},
             price_eur=Decimal("44.85"),
             delivery_address={"street": "Gaumnitzer Str. 43", "city": "Teuchern",
                               "province": "Sachsen-Anhalt", "postal": "06682", "country": "DE"})
    db.add(s); db.commit()

    class _AE:
        async def place_order(self, **kw):
            raise AssertionError("Geld-Call darf bei zu kleinem Bestand NICHT erfolgen")

        async def scrape_product(self, url):
            raise AssertionError("kein Scrape noetig")

    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: _AE())

    with pytest.raises(PersistentError, match="Bestand reicht nicht"):
        await order_service.fulfill_sale(db, sale_id=s.id)
    db.refresh(s)
    assert s.status == "needs_manual_review"

    # Genug Bestand (Menge 1) -> Preflight laesst durch (Geld-Call wuerde erfolgen)
    s2 = Sale(ebay_transaction_id="tx-stk2", listing_id=l.id, buyer_name="Max", quantity=1,
              status="pending", variant_selected={"Farbe": "Mit Kette"},
              price_eur=Decimal("14.95"),
              delivery_address={"street": "X 1", "city": "Teuchern", "postal": "06682",
                                "country": "DE"})
    db.add(s2); db.commit()

    reached = {}

    class _Placed:
        aliexpress_order_id = "AE-STK-1"
        cost_cny = None

    class _AE2(_AE):
        async def place_order(self, **kw):
            reached["ordered"] = True
            return _Placed()

        async def get_tracking(self, aliexpress_order_id):
            class _T:
                tracking_number = None
                carrier = None
                estimated_delivery = None
            return _T()

    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: _AE2())
    await order_service.fulfill_sale(db, sale_id=s2.id)
    assert reached.get("ordered") is True


# ----------------------------- Echte Zustell-Erkennung (Fund 17.07.) -----------------------------
# "Unterwegs" -> "Zugestellt", sobald der Zusteller-Status es meldet – nicht erst nach der 40-Tage-Frist.

def _delivered_setup(db, *, tx, aoid, tracking, days_ago):
    from datetime import datetime, timezone, timedelta
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    sale = Sale(ebay_transaction_id=tx, ebay_order_id=tx.split("-")[0], ebay_line_item_id="A",
                quantity=1, status="tracking", sale_date=when)
    db.add(sale)
    db.flush()
    db.add(OrderAliexpress(sale_id=sale.id, aliexpress_order_id=aoid, quantity=1,
                           status="shipped", tracking_number=tracking))
    db.commit()
    return sale


def test_is_delivered_tracking_markers():
    from app.services.order_service import _is_delivered_tracking
    # Positiv
    assert _is_delivered_tracking("Delivered")
    assert _is_delivered_tracking("Paket zugestellt")
    assert _is_delivered_tracking("Signed by recipient")
    assert _is_delivered_tracking("Successfully delivered to mailbox")
    assert _is_delivered_tracking("已签收")
    # Negativ – unterwegs
    assert not _is_delivered_tracking("Out for delivery")
    assert not _is_delivered_tracking("In transit")
    assert not _is_delivered_tracking(None)
    assert not _is_delivered_tracking("")
    # Negativ – Teilwort-Fallen (Review 17.07.): NICHT als zugestellt werten
    assert not _is_delivered_tracking("Shipment assigned to courier for delivery")  # assigned⊃signed
    assert not _is_delivered_tracking("Item undelivered, returned to sender")        # undelivered⊃delivered
    assert not _is_delivered_tracking("Die Sendung konnte nicht zugestellt werden")  # Negation
    assert not _is_delivered_tracking("no entregado")                                # Negation ES
    assert not _is_delivered_tracking("Colis non livré")                             # Negation FR
    assert not _is_delivered_tracking("拒绝签收")                                      # Ablehnung CN
    assert not _is_delivered_tracking("Failed delivery attempt")                     # Fehlversuch
    # Negativ – Zukunfts-/Verlaufsform (DE-Default!): „wird zugestellt" = unterwegs, NICHT fertig
    assert not _is_delivered_tracking("Ihre Sendung wird heute zugestellt")
    assert not _is_delivered_tracking("Wird voraussichtlich morgen zugestellt")
    assert not _is_delivered_tracking("Le colis sera livré demain")                  # FR Zukunft
    # Negativ – CJK NOCH-NICHT-Form: „待签收" = wartet auf Quittierung (am Abholpunkt), NICHT zugestellt
    assert not _is_delivered_tracking("待签收")
    assert not _is_delivered_tracking("即将签收")
    # Positiv – vollendete Form bleibt „zugestellt"/„签收"
    assert _is_delivered_tracking("Sendung wurde zugestellt")
    assert _is_delivered_tracking("Zugestellt am 12.07.2026")
    assert _is_delivered_tracking("已签收")


def test_promote_delivered_marks_delivered(db):
    from app.integrations.aliexpress import TrackingInfo
    sale = _delivered_setup(db, tx="D1-A", aoid="8001", tracking="00340434886281338687", days_ago=10)
    r = asyncio.run(order_service.promote_delivered_from_tracking(
        db, ae=_FakeAE(TrackingInfo("00340434886281338687", "DHL", "Delivered"))))
    assert r["checked"] == 1 and r["promoted"] == 1
    db.refresh(sale)
    assert sale.status == "delivered"


def test_promote_delivered_skips_in_transit(db):
    from app.integrations.aliexpress import TrackingInfo
    sale = _delivered_setup(db, tx="D2-A", aoid="8002", tracking="00340434886281338688", days_ago=10)
    r = asyncio.run(order_service.promote_delivered_from_tracking(
        db, ae=_FakeAE(TrackingInfo("00340434886281338688", "DHL", "In transit - out for delivery"))))
    assert r["promoted"] == 0
    db.refresh(sale)
    assert sale.status == "tracking"        # nicht faelschlich zugestellt


def test_promote_delivered_skips_too_fresh(db):
    from app.integrations.aliexpress import TrackingInfo
    sale = _delivered_setup(db, tx="D3-A", aoid="8003", tracking="00340434886281338689", days_ago=0)
    r = asyncio.run(order_service.promote_delivered_from_tracking(
        db, ae=_FakeAE(TrackingInfo("00340434886281338689", "DHL", "Delivered"))))
    assert r["checked"] == 0 and r["promoted"] == 0    # zu frisch (< min_age) -> nicht geprueft
    db.refresh(sale)
    assert sale.status == "tracking"


def test_promote_delivered_respects_toggle(db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "auto_delivered_from_tracking", False)
    _delivered_setup(db, tx="D4-A", aoid="8004", tracking="00340434886281338690", days_ago=10)
    r = asyncio.run(order_service.promote_delivered_from_tracking(db, ae=_FakeAE(None)))
    assert r.get("disabled") is True and r["promoted"] == 0


# ------------------------------------------ Storno vs. Tracking-Sync (Sale 1179, 16.08.)

def test_tracking_sync_ueberspringt_stornierte_sales(db, monkeypatch):
    """Vorfall Sale 1179: manuell storniert, aber der 45-min-Sync meldete das
    vorhandene Tracking erneut und setzte den Status zurueck auf 'unterwegs'."""
    sale = _delivered_setup(db, tx="ST1-A", aoid="9001",
                            tracking="00340434886281338699", days_ago=3)
    order_service.set_sale_status(db, sale_id=sale.id, status="cancelled")

    aufrufe = []

    async def _nie(db_, *, order_id):
        aufrufe.append(order_id)
    monkeypatch.setattr(order_service, "refresh_tracking", _nie)
    monkeypatch.setattr(order_service, "report_tracking_to_ebay", _nie)

    r = asyncio.run(order_service.sync_tracking_all(db))

    assert r["checked"] == 0 and aufrufe == []
    db.refresh(sale)
    assert sale.status == "cancelled"          # Storno bleibt Storno


def test_report_tracking_verweigert_stornierte_sales(db):
    sale = _delivered_setup(db, tx="ST2-A", aoid="9002",
                            tracking="00340434886281338698", days_ago=3)
    order_service.set_sale_status(db, sale_id=sale.id, status="cancelled")
    order = db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == sale.id))

    import pytest
    from app.retry import PersistentError
    with pytest.raises(PersistentError) as e:
        asyncio.run(order_service.report_tracking_to_ebay(db, order_id=order.id))
    assert "storniert" in str(e.value)
    db.refresh(sale)
    assert sale.status == "cancelled"


# ------------------------------- Auslands-Tracking (Sales 1266/1274/1277, 19.08.)

def _ausland_setup(db, *, land, tx, aoid):
    from app.models import Sale, OrderAliexpress
    s = Sale(ebay_transaction_id=tx, ebay_order_id=tx.split("-")[0],
             ebay_line_item_id="li9", quantity=1, status="ordered_aliexpress",
             delivery_address={"city": "Wien", "country": land})
    db.add(s); db.flush()
    o = OrderAliexpress(sale_id=s.id, aliexpress_order_id=aoid, quantity=1,
                        status="ordered")
    db.add(o); db.commit()
    return s, o


class _TrackAE:
    def __init__(self, nummer, carrier="AliExpress Selection Standard"):
        from app.integrations.aliexpress import TrackingInfo
        self._t = TrackingInfo(nummer, carrier, "Im Transit")

    async def get_tracking(self, aliexpress_order_id):
        return self._t


def test_refresh_tracking_speichert_standardnummer_bei_ausland(db, monkeypatch):
    """AT/IT: es folgt oft NIE eine deutsche Endzusteller-Nummer — die Cainiao-/
    Standard-Nummer ist die einzige trackbare und wird gespeichert (19.08.)."""
    s, o = _ausland_setup(db, land="AT", tx="AUS1-A", aoid="9101")
    monkeypatch.setattr(order_service, "get_aliexpress_client",
                        lambda: _TrackAE("158278800070184554306263"))
    r = asyncio.run(order_service.refresh_tracking(db, order_id=o.id))
    assert r["tracking_number"] == "158278800070184554306263"
    db.refresh(o)
    assert o.tracking_number == "158278800070184554306263"
    assert o.tracking_carrier            # Cainiao-Erkennung oder API-Carrier


def test_refresh_tracking_verwirft_standardnummer_bei_de(db, monkeypatch):
    """DE-Regel 2026-07-05 bleibt: provisorische Nummern werden NICHT gespeichert."""
    s, o = _ausland_setup(db, land="DE", tx="AUS2-A", aoid="9102")
    monkeypatch.setattr(order_service, "get_aliexpress_client",
                        lambda: _TrackAE("158278800070184554306263"))
    r = asyncio.run(order_service.refresh_tracking(db, order_id=o.id))
    assert r["tracking_number"] is None
    db.refresh(o)
    assert o.tracking_number is None


def test_tracking_meldbar_de_nur_final_ausland_auch_standard(db):
    from types import SimpleNamespace
    de = SimpleNamespace(delivery_address={"country": "DE"})
    at = SimpleNamespace(delivery_address={"country": "AT"})
    final_dhl = "00340434886281338687"
    cainiao = "158278800070184554306263"
    assert order_service._tracking_meldbar(final_dhl, de) is True
    assert order_service._tracking_meldbar(final_dhl, at) is True
    assert order_service._tracking_meldbar(cainiao, de) is False
    assert order_service._tracking_meldbar(cainiao, at) is True
    assert order_service._tracking_meldbar(None, at) is False


def test_refresh_tracking_upgrade_auf_endzusteller_bei_ausland(db, monkeypatch):
    """Uebergabe an Express One/Oesterr. Post: NEUE Nummer mit lokalem Carrier-
    Namen ersetzt die gemeldete Standard-Nummer (Nutzer-Hinweis 19.08.)."""
    s, o = _ausland_setup(db, land="AT", tx="AUS3-A", aoid="9103")
    o.tracking_number = "158278800070184554306263"
    o.tracking_carrier = "Cainiao (AliExpress Standard)"
    db.commit()
    monkeypatch.setattr(order_service, "get_aliexpress_client",
                        lambda: _TrackAE("EO998877665544", carrier="Express One"))
    asyncio.run(order_service.refresh_tracking(db, order_id=o.id))
    db.refresh(o)
    assert o.tracking_number == "EO998877665544"
    assert o.tracking_carrier == "Express One"


def test_refresh_tracking_kein_upgrade_bei_weiter_cainiao(db, monkeypatch):
    """Eine andere Cainiao-Nummer (Carrier weiter AliExpress) ersetzt nichts."""
    s, o = _ausland_setup(db, land="AT", tx="AUS4-A", aoid="9104")
    o.tracking_number = "158278800070184554306263"
    db.commit()
    monkeypatch.setattr(order_service, "get_aliexpress_client",
                        lambda: _TrackAE("158278800070999999999999"))
    asyncio.run(order_service.refresh_tracking(db, order_id=o.id))
    db.refresh(o)
    assert o.tracking_number == "158278800070184554306263"


def test_sync_meldet_upgrade_nach_ohne_doppelmeldung(db, monkeypatch):
    """Gemeldete Auslands-Sendung wird weiter beobachtet: Upgrade -> EINE Nachmeldung;
    ohne Aenderung -> keine erneute Meldung."""
    s, o = _ausland_setup(db, land="AT", tx="AUS5-A", aoid="9105")
    s.status = "tracking"
    o.tracking_number = "158278800070184554306263"
    db.commit()

    gemeldet = []

    async def _fake_report(db_, *, order_id):
        gemeldet.append(order_id)

    async def _fake_refresh(db_, *, order_id):
        o2 = db_.get(OrderAliexpress, order_id)
        o2.tracking_number = "EO111222333444"     # Endzusteller-Uebergabe simuliert
        o2.tracking_carrier = "Express One"
        db_.commit()
        return {}
    monkeypatch.setattr(order_service, "report_tracking_to_ebay", _fake_report)
    monkeypatch.setattr(order_service, "refresh_tracking", _fake_refresh)
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "monitor_push_real", True)

    r = asyncio.run(order_service.sync_tracking_all(db))
    assert r["checked"] == 1 and gemeldet == [o.id]

    # Zweiter Lauf ohne Aenderung: beobachtet weiter, meldet aber nicht erneut
    async def _fake_refresh_still(db_, *, order_id):
        return {}
    monkeypatch.setattr(order_service, "refresh_tracking", _fake_refresh_still)
    gemeldet.clear()
    r2 = asyncio.run(order_service.sync_tracking_all(db))
    assert gemeldet == []
    assert r2["checked"] >= 1
