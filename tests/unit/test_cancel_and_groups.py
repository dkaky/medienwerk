"""Tests: eBay-Storno-Abgleich, Doppelbestellungs-Lock, Mehrpositions-Bestellungen,
Varianten-Live-Nachladen und Quellen-Vergleich (2026-07-05)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Listing, OrderAliexpress, Product, Sale
from app.retry import PersistentError
from app.services import order_service


class _FakeEbay:
    def __init__(self, orders):
        self._orders = orders

    async def list_all_orders(self, *, since, max_orders=200):
        return self._orders


def _raw_order(order_id, line_items, cancel_state=None, payment_status=None):
    o = {
        "orderId": order_id, "legacyOrderId": order_id,
        "creationDate": "2026-07-03T10:00:00.000Z",
        "fulfillmentStartInstructions": [{"shippingStep": {"shipTo": {
            "fullName": "Max Muster",
            "contactAddress": {"addressLine1": "Weg 1", "city": "Witten",
                               "postalCode": "58452", "countryCode": "DE"}}}}],
        "lineItems": line_items,
    }
    if cancel_state:
        o["cancelStatus"] = {"cancelState": cancel_state}
    if payment_status:
        o["orderPaymentStatus"] = payment_status
    return o


def _li(li_id, sku="AE-1", qty=1, aspects=None):
    li = {"lineItemId": li_id, "sku": sku, "quantity": qty,
          "lineItemCost": {"value": "20.00"}}
    if aspects:
        li["variationAspects"] = [{"name": k, "value": v} for k, v in aspects.items()]
    return li


def _product(db, url="https://de.aliexpress.com/item/grp-1.html", ae_id="grp1",
             skus=True, supplier="store-a"):
    p = Product(
        aliexpress_url=url, aliexpress_id=ae_id, supplier_id=supplier,
        price_cny=Decimal("5.00"),
        variants=({"axes": {"Farbe": ["Schwarz", "Rot"]},
                   "skus": [{"attr": "14:1#Black", "options": {"Farbe": "Black"},
                             "id": 1, "price": 5.0, "stock": 9},
                            {"attr": "14:2#Red", "options": {"Farbe": "Red"},
                             "id": 2, "price": 6.0, "stock": 9}]} if skus else None),
    )
    db.add(p)
    db.flush()
    return p


def _listing(db, p, sku="AE-100"):
    l = Listing(product_id=p.id, ebay_sku=sku, title_seo="T", description="d",
                listing_status="active", price_eur=Decimal("29.95"),
                cost_eur=Decimal("7.00"))
    db.add(l)
    db.flush()
    return l


def _sale(db, listing, tx, ebay_order="EO-1", status="pending",
          variant=None, buyer="Max Muster"):
    s = Sale(ebay_transaction_id=tx, ebay_order_id=ebay_order, ebay_line_item_id=tx,
             listing_id=listing.id if listing else None, buyer_name=buyer,
             delivery_address={"street": "Weg 1", "city": "Witten",
                               "postal": "58452", "country": "DE"},
             quantity=1, variant_selected=variant, price_eur=Decimal("29.95"),
             status=status)
    db.add(s)
    db.commit()
    return s


# ----------------------------- Parser: Storno-Felder -----------------------------
def test_order_lines_extract_cancel_and_payment_state():
    order = _raw_order("O-C", [_li("A")], cancel_state="CANCELED",
                       payment_status="FULLY_REFUNDED")
    f = order_service._order_lines(order)[0]
    assert f["cancel_state"] == "CANCELED"
    assert f["payment_status"] == "FULLY_REFUNDED"


# ----------------------------- Sync: Storno-Abgleich -----------------------------
def test_sync_marks_open_sale_cancelled(db, monkeypatch):
    order = _raw_order("O-1", [_li("A")])
    monkeypatch.setattr(order_service, "_real_ebay", lambda: _FakeEbay([order]))
    asyncio.run(order_service.sync_ebay_orders(db, days=30))
    sale = db.scalar(select(Sale).where(Sale.ebay_transaction_id == "O-1-A"))
    assert sale.status == "pending"

    cancelled = _raw_order("O-1", [_li("A")], cancel_state="CANCELED",
                           payment_status="FULLY_REFUNDED")
    monkeypatch.setattr(order_service, "_real_ebay", lambda: _FakeEbay([cancelled]))
    r = asyncio.run(order_service.sync_ebay_orders(db, days=30))
    db.refresh(sale)
    assert r["sales_cancelled"] == 1
    assert sale.status == "cancelled"
    assert sale.ebay_cancel_state == "CANCELED"
    # Idempotent: zweiter Lauf zaehlt nicht doppelt
    r2 = asyncio.run(order_service.sync_ebay_orders(db, days=30))
    assert r2["sales_cancelled"] == 0


def test_sync_in_progress_flags_but_keeps_pending(db, monkeypatch):
    order = _raw_order("O-2", [_li("A")])
    monkeypatch.setattr(order_service, "_real_ebay", lambda: _FakeEbay([order]))
    asyncio.run(order_service.sync_ebay_orders(db, days=30))
    inprog = _raw_order("O-2", [_li("A")], cancel_state="IN_PROGRESS")
    monkeypatch.setattr(order_service, "_real_ebay", lambda: _FakeEbay([inprog]))
    asyncio.run(order_service.sync_ebay_orders(db, days=30))
    sale = db.scalar(select(Sale).where(Sale.ebay_transaction_id == "O-2-A"))
    assert sale.status == "pending"          # NUR Anfrage -> kein Storno
    assert sale.ebay_cancel_state == "IN_PROGRESS"
    # Anfrage abgelehnt -> Flag verschwindet
    none_req = _raw_order("O-2", [_li("A")], cancel_state="NONE_REQUESTED")
    monkeypatch.setattr(order_service, "_real_ebay", lambda: _FakeEbay([none_req]))
    asyncio.run(order_service.sync_ebay_orders(db, days=30))
    db.refresh(sale)
    assert sale.ebay_cancel_state is None


def test_sync_cancel_after_purchase_only_flags(db, monkeypatch):
    p = _product(db)
    l = _listing(db, p)
    sale = _sale(db, l, tx="O-3-A", ebay_order="O-3", status="ordered_aliexpress")
    db.add(OrderAliexpress(sale_id=sale.id, aliexpress_order_id="AE-1", status="ordered"))
    db.commit()
    cancelled = _raw_order("O-3", [_li("A")], cancel_state="CANCELED")
    monkeypatch.setattr(order_service, "_real_ebay", lambda: _FakeEbay([cancelled]))
    r = asyncio.run(order_service.sync_ebay_orders(db, days=30))
    db.refresh(sale)
    assert sale.status == "ordered_aliexpress"   # Status NIE zurueckdrehen
    assert sale.ebay_cancel_state == "CANCELED"  # aber Flag + Alarm
    assert r["sales_cancelled"] == 0


def test_sync_new_order_already_cancelled_creates_cancelled_sale(db, monkeypatch):
    cancelled = _raw_order("O-4", [_li("A")], cancel_state="CANCELED")
    monkeypatch.setattr(order_service, "_real_ebay", lambda: _FakeEbay([cancelled]))
    r = asyncio.run(order_service.sync_ebay_orders(db, days=30))
    sale = db.scalar(select(Sale).where(Sale.ebay_transaction_id == "O-4-A"))
    assert sale.status == "cancelled"
    assert r["sales_cancelled"] == 1


# ----------------------------- fulfill: Storno-Sperren -----------------------------
def test_fulfill_refuses_cancelled_sale(db):
    p = _product(db)
    l = _listing(db, p)
    sale = _sale(db, l, tx="C-1", status="cancelled")
    with pytest.raises(PersistentError, match="storniert"):
        asyncio.run(order_service.fulfill_sale(db, sale_id=sale.id))
    assert db.scalar(select(OrderAliexpress).where(
        OrderAliexpress.sale_id == sale.id)) is None


def test_mark_sale_cancelled_and_undo(db):
    p = _product(db)
    l = _listing(db, p)
    sale = _sale(db, l, tx="C-2", status="pending")
    r = order_service.mark_sale_cancelled(db, sale_id=sale.id)
    assert r["status"] == "cancelled"
    r2 = order_service.mark_sale_cancelled(db, sale_id=sale.id, undo=True)
    assert r2["undone"] and db.get(Sale, sale.id).status == "pending"


def test_mark_cancelled_after_purchase_only_flags(db):
    p = _product(db)
    l = _listing(db, p)
    sale = _sale(db, l, tx="C-3", status="ordered_aliexpress")
    db.add(OrderAliexpress(sale_id=sale.id, aliexpress_order_id="AE-2", status="ordered"))
    db.commit()
    r = order_service.mark_sale_cancelled(db, sale_id=sale.id)
    assert r.get("flagged") and "AliExpress" in r["warning"]
    assert db.get(Sale, sale.id).status == "ordered_aliexpress"


# ----------------------------- Doppelbestellungs-Lock -----------------------------
def test_claim_blocks_second_fulfill(db):
    p = _product(db)
    l = _listing(db, p)
    sale = _sale(db, l, tx="D-1", variant={"Farbe": "Black"})
    # Simulierter paralleler Prozess: Claim existiert schon (status=ordering, ohne AE-ID)
    db.add(OrderAliexpress(sale_id=sale.id, product_id=p.id, status="ordering"))
    db.commit()
    with pytest.raises(PersistentError, match="[Dd]oppelbestellung"):
        asyncio.run(order_service.fulfill_sale(db, sale_id=sale.id))
    assert db.scalar(select(OrderAliexpress).where(
        OrderAliexpress.sale_id == sale.id)).aliexpress_order_id is None


def test_fulfill_success_creates_single_order_and_is_idempotent(db):
    p = _product(db)
    l = _listing(db, p)
    sale = _sale(db, l, tx="D-2", variant={"Farbe": "Black"})
    r1 = asyncio.run(order_service.fulfill_sale(db, sale_id=sale.id))
    assert r1["aliexpress_order_id"]
    r2 = asyncio.run(order_service.fulfill_sale(db, sale_id=sale.id))
    assert r2["order_id"] == r1["order_id"]
    orders = db.scalars(select(OrderAliexpress).where(
        OrderAliexpress.sale_id == sale.id)).all()
    assert len(orders) == 1 and orders[0].status in ("ordered", "shipped")


# --------------------- Blindkauf-Sperre bei fehlenden Varianten ---------------------
def test_fulfill_blocks_when_variant_selected_but_no_skus(db, monkeypatch):
    # Quelle liefert keine skus (Live-Nachladen schlaegt fehl) -> KEIN Blindkauf
    p = _product(db, skus=False)
    l = _listing(db, p)
    sale = _sale(db, l, tx="B-1", variant={"Farbe": "Black Ai"})

    async def _no_variants(_db, _p):
        return []
    monkeypatch.setattr(order_service, "_ensure_product_variants", _no_variants)
    with pytest.raises(PersistentError, match="keine Varianten"):
        asyncio.run(order_service.fulfill_sale(db, sale_id=sale.id))
    assert db.get(Sale, sale.id).status == "needs_manual_review"
    assert db.scalar(select(OrderAliexpress).where(
        OrderAliexpress.sale_id == sale.id)) is None


# ----------------------------- Mehrpositions-Bestellung -----------------------------
def test_fulfill_ebay_order_groups_open_positions(db):
    p1 = _product(db, url="https://de.aliexpress.com/item/grp-10.html",
                  ae_id="grp10", supplier="store-x")
    p2 = _product(db, url="https://de.aliexpress.com/item/grp-11.html",
                  ae_id="grp11", supplier="store-y")
    l1 = _listing(db, p1, sku="AE-201")
    l2 = _listing(db, p2, sku="AE-202")
    s1 = _sale(db, l1, tx="G-1-A", ebay_order="GRP-1", variant={"Farbe": "Black"})
    s2 = _sale(db, l2, tx="G-1-B", ebay_order="GRP-1", variant={"Farbe": "Red"})
    # dritte Position ist SCHON bestellt -> wird uebersprungen
    s3 = _sale(db, l1, tx="G-1-C", ebay_order="GRP-1", status="ordered_aliexpress")
    db.add(OrderAliexpress(sale_id=s3.id, aliexpress_order_id="AE-OLD", status="ordered"))
    db.commit()

    r = asyncio.run(order_service.fulfill_ebay_order(db, ebay_order_id="GRP-1"))
    assert {x["sale_id"] for x in r["ordered"]} == {s1.id, s2.id}
    assert r["skipped_already_ordered"] == [s3.id]
    assert not r["errors"]
    for s in (s1, s2):
        db.refresh(s)
        assert s.status == "ordered_aliexpress"
        o = db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == s.id))
        assert o is not None and o.aliexpress_order_id
    # verschiedene Haendler -> zwei Gruppen
    assert r["groups"] == 2


def test_fulfill_ebay_order_all_or_nothing_on_ambiguous_variant(db):
    p1 = _product(db, url="https://de.aliexpress.com/item/grp-20.html",
                  ae_id="grp20", supplier="store-z")
    # Quelle mit Praefix-Mehrdeutigkeit: "Black" vs "Black AI" -> nie automatisch
    p2 = Product(
        aliexpress_url="https://de.aliexpress.com/item/grp-21.html",
        aliexpress_id="grp21", supplier_id="store-z", price_cny=Decimal("5"),
        variants={"axes": {"Farbe": ["Black", "Black AI"]},
                  "skus": [{"attr": "14:1#Black", "options": {"Farbe": "Black"}, "id": 1},
                           {"attr": "14:2#BlackAI", "options": {"Farbe": "Black AI"}, "id": 2}]})
    db.add(p2)
    db.flush()
    l1 = _listing(db, p1, sku="AE-301")
    l2 = _listing(db, p2, sku="AE-302")
    s1 = _sale(db, l1, tx="G-2-A", ebay_order="GRP-2", variant={"Farbe": "Black"})
    s2 = _sale(db, l2, tx="G-2-B", ebay_order="GRP-2", variant={"Farbe": "Black"})

    with pytest.raises(PersistentError, match="Gruppe NICHT bestellt"):
        asyncio.run(order_service.fulfill_ebay_order(db, ebay_order_id="GRP-2"))
    # NICHTS wurde bestellt (alles-oder-nichts), mehrdeutige Position -> Review
    assert db.scalars(select(OrderAliexpress)).all() == []
    db.refresh(s2)
    assert s2.status == "needs_manual_review"


def test_fulfill_ebay_order_orders_valid_and_skips_cancelled_sibling(db):
    # Eine stornierte Position darf die gueltige NICHT blockieren (Teilbestellung).
    p = _product(db, url="https://de.aliexpress.com/item/grp-30.html", ae_id="grp30")
    l = _listing(db, p, sku="AE-401")
    s_cancel = _sale(db, l, tx="G-3-A", ebay_order="GRP-3", status="cancelled")
    s_ok = _sale(db, l, tx="G-3-B", ebay_order="GRP-3", status="pending",
                 variant={"Farbe": "Black"})
    r = asyncio.run(order_service.fulfill_ebay_order(db, ebay_order_id="GRP-3"))
    assert [x["sale_id"] for x in r["ordered"]] == [s_ok.id]
    # Die stornierte Position wurde NIE bestellt
    assert db.scalar(select(OrderAliexpress).where(
        OrderAliexpress.sale_id == s_cancel.id)) is None
    db.refresh(s_cancel)
    assert s_cancel.status == "cancelled"


def test_fulfill_ebay_order_holds_in_progress_cancel(db):
    # Position mit offener Storno-Anfrage (IN_PROGRESS) wird nicht mitbestellt.
    p = _product(db, url="https://de.aliexpress.com/item/grp-31.html", ae_id="grp31")
    l = _listing(db, p, sku="AE-402")
    s = _sale(db, l, tx="G-4-A", ebay_order="GRP-4", status="pending",
              variant={"Farbe": "Black"})
    s.ebay_cancel_state = "IN_PROGRESS"
    db.commit()
    with pytest.raises(PersistentError, match="Storno"):
        asyncio.run(order_service.fulfill_ebay_order(db, ebay_order_id="GRP-4"))
    assert db.scalars(select(OrderAliexpress)).all() == []


# ----------------------------- Quellen-genamespacte variant_map -----------------------------
def test_learn_variant_namespaced_and_source_resolution(db):
    p = _product(db, ae_id="ns1")
    l = _listing(db, p, sku="AE-501")
    sel = {"Farbe": "Grau"}
    order_service._learn_variant(l, "ns1", sel, "14:1#Black")
    db.commit()
    assert l.variant_map[f"ns1|{order_service.variant_map_key(sel)}"] == "14:1#Black"
    # Fremde Quelle 'other' darf den ns1-Eintrag NICHT nutzen (nur exaktes Namespace)
    foreign_skus = [{"attr": "99:1", "options": {"Farbe": "Grey"}, "id": 9}]
    hit = order_service.resolve_selection_against_skus(sel, foreign_skus,
                                                       listing=l, source_id="other")
    assert (hit or {}).get("attr") == "99:1"   # per Regel (Grau->gray~grey), nicht per Map
    # Für die gelernte Quelle greift die Map exakt
    own_skus = [{"attr": "14:1#Black", "options": {"Farbe": "irgendwas"}, "id": 1}]
    hit2 = order_service.resolve_selection_against_skus(sel, own_skus,
                                                        listing=l, source_id="ns1")
    assert hit2["attr"] == "14:1#Black"


# ----------------------------- Varianten-Live-Nachladen -----------------------------
def test_variant_options_live_backfill(db, monkeypatch):
    p = _product(db, skus=False)
    p.variants = None
    l = _listing(db, p)
    sale = _sale(db, l, tx="V-1", variant={"Farbe": "Grau"})

    class _Scraped:
        aliexpress_id = p.aliexpress_id
        price_cny = Decimal("6.00")
        supplier_id = "store-a"
        images = []
        title_raw = "x"
        in_stock = True
        variants = {"axes": {"Farbe": ["Grey", "Grey-AI"]},
                    "skus": [{"attr": "14:5#Grey", "options": {"Farbe": "Grey"},
                              "id": 5, "price": 6.0, "stock": 3, "image": "i.jpg"}]}

    class _AE:
        async def scrape_product(self, url):
            return _Scraped()

    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: _AE())
    d = asyncio.run(order_service.variant_options(db, sale_id=sale.id))
    assert d["refreshed"] is True
    assert len(d["options"]) == 1 and d["options"][0]["attr"] == "14:5#Grey"
    db.refresh(p)
    assert (p.variants or {}).get("skus")   # persistiert


def test_variant_options_position_suggestion(db):
    """#1161: die AliExpress-Optionswerte helfen dem Kaeuferwert nicht (kein Ueberlappungssignal),
    Wert-Matching bleibt leer -> die Vorwahl im '🧩 Variante'-Dialog kommt deterministisch aus
    unserer Publish-SKU ({base}-V{i}). (Varianten sind distinkt -> echtes Multivarianten-Listing.)"""
    from decimal import Decimal
    from app.models import Product, Listing, Sale
    p = Product(aliexpress_url="https://de.aliexpress.com/item/i.html", aliexpress_id="ii",
                variants={"axes": {"Farbe": ["Style-A", "Style-B"]},
                          "skus": [{"attr": "70:1", "options": {"Farbe": "Style-A"}, "id": 1, "price": 5, "stock": 9},
                                   {"attr": "70:2", "options": {"Farbe": "Style-B"}, "id": 2, "price": 5, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-611", ebay_item_id="E611", title_seo="Ring",
                description="d", listing_status="active", price_eur=Decimal("14.95"))
    db.add(l); db.flush()
    # Kaeuferwert "1159" ueberlappt weder Style-A noch Style-B -> Wert-Matching leer -> Position (V2)
    s = Sale(ebay_transaction_id="P-1", listing_id=l.id, status="pending",
             variant_selected={"Hauptsteinfarbe": "1159", "ebay_sku": "AE-611-V2"}, price_eur=Decimal("14.95"))
    db.add(s); db.commit()
    d = asyncio.run(order_service.variant_options(db, sale_id=s.id))
    assert d["suggested_position"] is True
    assert d["suggested_attr"] == "70:2"          # V2 -> zweite Variante (Werte unterscheiden nichts)
    assert d["correction_attr"] is None           # Vorwahl == Position -> kein Korrektur-Hinweis noetig


def test_variant_options_exposes_position_for_correction(db):
    """Korrektur-Hinweis: ist (versehentlich) eine FALSCHE Variante zugeordnet, liefert
    variant_options weiterhin die richtige position_attr (aus der Publish-SKU) – die UI kann die
    Fehlzuordnung anzeigen und in einem Klick korrigieren."""
    from decimal import Decimal
    from app.models import Product, Listing, Sale
    p = Product(aliexpress_url="https://de.aliexpress.com/item/k.html", aliexpress_id="kk",
                variants={"axes": {"Farbe": ["Rot", "Blau"]},
                          "skus": [{"attr": "14:1", "options": {"Farbe": "Rot"}, "id": 1, "price": 5, "stock": 9},
                                   {"attr": "14:2", "options": {"Farbe": "Blau"}, "id": 2, "price": 5, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-613", ebay_item_id="E613", title_seo="Ring",
                description="d", listing_status="active", price_eur=Decimal("14.95"))
    db.add(l); db.flush()
    # Kaeufer kaufte V2 (Blau), aber versehentlich Rot (14:1) zugeordnet – ebay_sku bleibt erhalten.
    s = Sale(ebay_transaction_id="K-1", listing_id=l.id, status="pending",
             variant_selected={"Farbe": "Blau", "ebay_sku": "AE-613-V2",
                               "attr": "14:1", "options": {"Farbe": "Rot"}, "id": 1},
             price_eur=Decimal("14.95"))
    db.add(s); db.commit()
    d = asyncio.run(order_service.variant_options(db, sale_id=s.id))
    assert d["correction_attr"] == "14:2"         # richtig laut Publish-SKU (V2 = Blau) -> Korrektur-Hinweis
    assert d["selected"]["attr"] == "14:1"        # aktuell falsch zugeordnet (Rot, vom Wert NICHT gedeckt)


def test_variant_options_no_correction_when_value_verified(db):
    """Geld-Schutz (Review-HIGH): ist die markierte Variante durch die KAEUFERWERTE gedeckt, wird
    KEIN Korrektur-Hinweis gezeigt – auch wenn die Publish-SKU-Position (durch Drift) abweicht.
    Sonst wuerde der Hinweis eine korrekte, wertverifizierte Zuordnung faelschlich anzweifeln."""
    from decimal import Decimal
    from app.models import Product, Listing, Sale
    p = Product(aliexpress_url="https://de.aliexpress.com/item/v.html", aliexpress_id="vv",
                variants={"axes": {"Farbe": ["Rot", "Blau"]},
                          "skus": [{"attr": "14:1", "options": {"Farbe": "Rot"}, "id": 1, "price": 5, "stock": 9},
                                   {"attr": "14:2", "options": {"Farbe": "Blau"}, "id": 2, "price": 5, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-614", ebay_item_id="E614", title_seo="Ring",
                description="d", listing_status="active", price_eur=Decimal("14.95"))
    db.add(l); db.flush()
    # Kaeufer "Blau" -> Wert-Match 14:2 (verifiziert); ebay_sku V1 zeigt (Drift) auf Rot (14:1).
    s = Sale(ebay_transaction_id="V-1", listing_id=l.id, status="pending",
             variant_selected={"Farbe": "Blau", "ebay_sku": "AE-614-V1"}, price_eur=Decimal("14.95"))
    db.add(s); db.commit()
    d = asyncio.run(order_service.variant_options(db, sale_id=s.id))
    assert d["suggested_attr"] == "14:2"          # Wert-Match Blau (verifiziert)
    assert d["suggested_position"] is False
    assert d["correction_attr"] is None           # KEIN falscher Korrektur-Hinweis (Rot), obwohl Pos abweicht


def test_variant_options_verified_resolution_beats_position(db):
    """Geld-Schutz: die Position ueberschreibt NIE eine verifizierte Wert-Auflösung. Passt der
    Kaeuferwert eindeutig, kommt der Vorschlag daher (suggested_position False)."""
    from decimal import Decimal
    from app.models import Product, Listing, Sale
    p = Product(aliexpress_url="https://de.aliexpress.com/item/j.html", aliexpress_id="jj",
                variants={"axes": {"Farbe": ["Rot", "Blau"]},
                          "skus": [{"attr": "14:1", "options": {"Farbe": "Rot"}, "id": 1, "price": 5, "stock": 9},
                                   {"attr": "14:2", "options": {"Farbe": "Blau"}, "id": 2, "price": 5, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-612", ebay_item_id="E612", title_seo="Ring",
                description="d", listing_status="active", price_eur=Decimal("14.95"))
    db.add(l); db.flush()
    # Kaeufer "Blau", ebay_sku zeigt aber (per Position) auf V1=Rot -> Wert-Auflösung (Blau) gewinnt
    s = Sale(ebay_transaction_id="P-2", listing_id=l.id, status="pending",
             variant_selected={"Farbe": "Blau", "ebay_sku": "AE-612-V1"}, price_eur=Decimal("14.95"))
    db.add(s); db.commit()
    d = asyncio.run(order_service.variant_options(db, sale_id=s.id))
    assert d["suggested_position"] is False
    assert d["suggested_attr"] == "14:2"          # aus dem Wert "Blau", NICHT Position V1(Rot)


# ----------------------------- Geld-Sicherheit: unklarer Ausgang -----------------------------
def test_uncertain_order_keeps_claim_no_retry(db, monkeypatch):
    from app.integrations.aliexpress import OrderUncertainError
    p = _product(db, ae_id="unc1")
    l = _listing(db, p)
    sale = _sale(db, l, tx="U-1", variant={"Farbe": "Black"})

    calls = {"n": 0}

    class _AE:
        async def scrape_product(self, url):
            raise RuntimeError("should not be called (skus present)")
        async def place_order(self, **kw):
            calls["n"] += 1
            raise OrderUncertainError("Timeout")
        async def get_tracking(self, *a, **k):
            raise AssertionError("nicht erreichbar")

    # skus schon vorhanden -> kein scrape noetig
    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: _AE())
    with pytest.raises(PersistentError, match="UNKLAR"):
        asyncio.run(order_service.fulfill_sale(db, sale_id=sale.id))
    assert calls["n"] == 1                       # KEIN automatisches Retry des Geld-Calls
    o = db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == sale.id))
    assert o is not None and o.status == "verify_needed"   # Claim BEHALTEN
    db.refresh(sale)
    assert sale.status == "needs_manual_review"
    # Erneuter Versuch bestellt NICHT (blockt), solange verify_needed offen
    with pytest.raises(PersistentError, match="unklarem Ausgang"):
        asyncio.run(order_service.fulfill_sale(db, sale_id=sale.id))
    assert calls["n"] == 1


def test_rejected_order_rolls_back_claim(db, monkeypatch):
    from app.integrations.aliexpress import OrderRejectedError
    p = _product(db, ae_id="rej1")
    l = _listing(db, p)
    sale = _sale(db, l, tx="R-1", variant={"Farbe": "Black"})

    class _AE:
        async def place_order(self, **kw):
            raise OrderRejectedError("is_success=false")

    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: _AE())
    with pytest.raises(PersistentError, match="abgelehnt"):
        asyncio.run(order_service.fulfill_sale(db, sale_id=sale.id))
    # SICHER nicht bestellt -> Claim entfernt, Sale wieder bestellbar (needs_manual_review)
    assert db.scalar(select(OrderAliexpress).where(
        OrderAliexpress.sale_id == sale.id)) is None


def test_product_not_exist_routes_to_alternative(db, monkeypatch):
    """PRODUCT_NOT_EXIST (Quelle weg) -> Ausweich-Quellen-Fluss (alternative_pending mit Vorschlägen),
    NICHT totes needs_manual_review. Claim wird zurückgerollt (sicher nicht bestellt)."""
    from app.integrations.aliexpress import ProductNotFoundError
    p = _product(db, ae_id="gone1")
    p.images = ["https://img.example/x.jpg"]
    db.commit()
    l = _listing(db, p)
    sale = _sale(db, l, tx="G-1", variant={"Farbe": "Black"})

    class _AE:
        async def place_order(self, **kw):
            raise ProductNotFoundError("AliExpress-Bestellung abgelehnt: PRODUCT_NOT_EXIST")
        async def find_alternative(self, image_url):
            return [{"url": "https://de.aliexpress.com/item/alt.html", "title": "Alternative"}]

    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: _AE())
    with pytest.raises(PersistentError, match="verfuegbar"):
        asyncio.run(order_service.fulfill_sale(db, sale_id=sale.id))
    db.refresh(sale)
    assert sale.status == "alternative_pending"
    assert db.scalar(select(OrderAliexpress).where(
        OrderAliexpress.sale_id == sale.id)) is None      # sicher nicht bestellt -> Claim weg


def test_sweep_stale_claims_marks_review(db):
    from datetime import timedelta
    p = _product(db, ae_id="stale1")
    l = _listing(db, p)
    sale = _sale(db, l, tx="S-1", status="pending", variant={"Farbe": "Black"})
    old = datetime.now(timezone.utc) - timedelta(minutes=30)
    claim = OrderAliexpress(sale_id=sale.id, product_id=p.id, status="ordering",
                            order_date=old)
    db.add(claim)
    db.commit()
    r = order_service.sweep_stale_claims(db)
    assert r["swept"] == 1
    db.refresh(sale); db.refresh(claim)
    assert sale.status == "needs_manual_review"
    assert claim.status == "verify_needed"


def test_sync_new_refunded_order_not_orderable(db, monkeypatch):
    refunded = _raw_order("O-REF", [_li("A")], payment_status="FULLY_REFUNDED")
    monkeypatch.setattr(order_service, "_real_ebay", lambda: _FakeEbay([refunded]))
    r = asyncio.run(order_service.sync_ebay_orders(db, days=30))
    sale = db.scalar(select(Sale).where(Sale.ebay_transaction_id == "O-REF-A"))
    assert sale.status == "refunded"           # NICHT pending -> Auto-Fulfill kauft nicht
    assert r["sales_refunded"] == 1


# ----------------------------- AliExpress: Order-ID-Parsing -----------------------------
def test_extract_order_ids_variants():
    from app.integrations.aliexpress import RealAliExpressClient
    f = RealAliExpressClient._extract_order_ids
    assert f({"order_id": 123}) == ["123"]
    assert f({"order_list": {"number": [1, 2]}}) == ["1", "2"]
    assert f({"order_list": [5, 5, 6]}) == ["5", "6"]
    assert f({"ae_order_id": "9"}) == ["9"]


def test_place_order_multi_default_maps_one_to_one(db):
    from app.integrations.aliexpress import MockAliExpressClient
    ae = MockAliExpressClient()
    res = asyncio.run(ae.place_order_multi(
        items=[{"url": "https://de.aliexpress.com/item/a.html", "quantity": 1},
               {"url": "https://de.aliexpress.com/item/b.html", "quantity": 2}],
        delivery_name="Max", delivery_address={}))
    assert len(res) == 2
    assert res[0]["item_indexes"] == [0] and res[1]["item_indexes"] == [1]
    assert res[0]["aliexpress_order_id"] != res[1]["aliexpress_order_id"]


# ----------------------------- Analytics: Gruppen + Storno-Summen -----------------------------
def test_list_orders_marks_multi_position_and_voids_cancelled(db):
    from app.services import analytics_service
    p = _product(db, ae_id="an1")
    l = _listing(db, p, sku="AE-601")
    _sale(db, l, tx="AN-1-A", ebay_order="AN-1")
    _sale(db, l, tx="AN-1-B", ebay_order="AN-1")
    _sale(db, l, tx="AN-2-A", ebay_order="AN-2", status="cancelled")
    d = analytics_service.list_orders(db)
    multi = [o for o in d["orders"] if o["ebay_order_id"] == "AN-1"]
    assert all(o["order_item_count"] == 2 for o in multi)
    assert sorted(o["order_position"] for o in multi) == [1, 2]
    # Storno zaehlt nicht in die Summen
    assert d["summary"]["count"] == 2
    assert d["summary"]["revenue_eur"] == pytest.approx(2 * 29.95)


def test_promote_stale_tracking(db):
    """Alte 'tracking' -> 'delivered'; frische bleiben; Storno/offen unberuehrt."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    def mk(status, days_ago):
        s = Sale(ebay_transaction_id=f"T-{status}-{days_ago}", status=status,
                 sale_date=now - timedelta(days=days_ago))
        db.add(s); db.flush(); return s
    old = mk("tracking", 45)       # -> delivered
    fresh = mk("tracking", 10)     # bleibt tracking
    ref = mk("refunded", 60)       # bleibt refunded
    pend = mk("pending", 50)       # bleibt pending
    db.commit()

    r = order_service.promote_stale_tracking(db, days=40)
    assert r["promoted"] == 1
    for s in (old, fresh, ref, pend): db.refresh(s)
    assert old.status == "delivered"
    assert fresh.status == "tracking"
    assert ref.status == "refunded"
    assert pend.status == "pending"

    # days=0 -> Automatik aus, keine Aenderung
    old.status = "tracking"; db.commit()
    r2 = order_service.promote_stale_tracking(db, days=0)
    assert r2["promoted"] == 0
    db.refresh(old); assert old.status == "tracking"


def test_sync_dedups_by_order_lineitem_across_tx_formats(db, monkeypatch):
    """Regression (Vorfall 06./07.07.): bestehender Sale mit MISMATCHENDER tx (altes
    Order-Level-Format) darf beim Re-Import NICHT dupliziert werden – Dedup über den
    stabilen Schlüssel (order_id, line_item_id), nicht über tx_id."""
    db.add(Sale(ebay_transaction_id="13-14581-54353", ebay_order_id="13-14581-54353",
                ebay_line_item_id="1008", status="delivered", price_eur=Decimal("33")))
    db.commit()
    order = _raw_order("13-14581-54353", [_li("1008")])
    monkeypatch.setattr(order_service, "_real_ebay", lambda: _FakeEbay([order]))
    r = asyncio.run(order_service.sync_ebay_orders(db, days=30))
    assert r["sales_created"] == 0          # kein Duplikat trotz tx-Format-Mismatch
    assert db.query(Sale).count() == 1


# ----------------------------- "Bei AliExpress bezahlt"-Markierung -----------------------------
def test_mark_ae_paid_toggles_flag(db):
    """Der 'Bezahlen'-Knopf setzt nur eine lokale Notiz (grau = schon gezahlt) und
    laesst sich zuruecknehmen. Kein AliExpress-Call, kein Statuswechsel."""
    p = _product(db, url="https://de.aliexpress.com/item/paid-1.html", ae_id="paid1")
    l = _listing(db, p, sku="AE-900")
    s = _sale(db, l, tx="P-1", status="ordered_aliexpress")
    assert s.ae_paid is False

    r = order_service.mark_ae_paid(db, sale_id=s.id)
    assert r["ae_paid"] is True
    db.refresh(s)
    assert s.ae_paid is True
    assert s.status == "ordered_aliexpress"     # Status bleibt unberuehrt

    order_service.mark_ae_paid(db, sale_id=s.id, paid=False)
    db.refresh(s)
    assert s.ae_paid is False

    with pytest.raises(PersistentError):
        order_service.mark_ae_paid(db, sale_id=999999)


def test_variant_options_lists_all_sources(db):
    """Der Varianten-Dialog bekommt ALLE hinterlegten Quellen mitgeliefert, damit man
    auf eine (evtl. guenstigere) Ausweich-Quelle umschalten kann. Slot 0 = Hauptquelle."""
    p = _product(db, url="https://de.aliexpress.com/item/src-1.html", ae_id="src1")
    p.alternatives = {"sources": [
        {"aliexpress_id": "src1", "url": "https://de.aliexpress.com/item/src-1.html",
         "title": "Hauptquelle", "price_eur": 5.0, "in_stock": True},
        {"aliexpress_id": "src2", "url": "https://de.aliexpress.com/item/src-2.html",
         "title": "Guenstiger Ausweich", "price_eur": 3.5, "in_stock": True},
    ]}
    l = _listing(db, p, sku="AE-901")
    s = _sale(db, l, tx="S-1", variant={"Farbe": "Black"})

    d = asyncio.run(order_service.variant_options(db, sale_id=s.id))
    assert [x["aliexpress_id"] for x in d["sources"]] == ["src1", "src2"]
    assert d["sources"][0]["is_primary"] is True
    assert d["sources"][1]["is_primary"] is False
    assert d["listing_id"] == l.id
