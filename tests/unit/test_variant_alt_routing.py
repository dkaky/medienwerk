"""Auto-Routing auf die verknuepfte Ausweich-VARIANTE (Projekt 11.07.).

GELD-PFAD-Matrix: geroutet wird NUR bei (a) manuellem Klick (allow_alt_routing=True),
(b) Dict-Verknuepfung MIT Ziel-Variante, (c) frisch bewiesenem Primaer-OOS. Alles andere
laeuft den normalen Pfad (und blockt dort ggf. sauber).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import Listing, OrderAliexpress, Product, Sale
from app.retry import PersistentError
from app.services import order_service

PRIM_URL = "https://de.aliexpress.com/item/prim1.html"
ALT_URL = "https://de.aliexpress.com/item/alt1.html"


def _setup(db, *, vmap, prim_stock_cached=0, price_eur="19.95"):
    p = Product(
        aliexpress_url=PRIM_URL, aliexpress_id="prim1",
        variants={"axes": {"Farbe": ["Red", "Blue"]},
                  "skus": [
                      {"attr": "A:1#Red", "id": "P1", "price": "5.0",
                       "stock": prim_stock_cached, "options": {"Farbe": "Red"}},
                      {"attr": "A:2#Blue", "id": "P2", "price": "5.0",
                       "stock": 9, "options": {"Farbe": "Blue"}}]},
        alternatives={"sources": [
            {"aliexpress_id": "prim1", "url": PRIM_URL, "in_stock": True},
            {"aliexpress_id": "alt1", "url": ALT_URL, "title": "Backup-Anbieter",
             "in_stock": True,
             "skus": [{"attr": "B:9#Rouge", "id": "S9", "stock": 40, "price": "4.0"}]},
        ]},
    )
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-R1", title_seo="Kette", description="d",
                listing_status="active", price_eur=Decimal(price_eur),
                cost_eur=Decimal("6.99"), variant_source_map=vmap)
    db.add(l); db.flush()
    s = Sale(ebay_transaction_id="tx-route-1", listing_id=l.id, buyer_name="Max",
             quantity=1, status="pending", price_eur=Decimal(price_eur),
             fee_eur_actual=Decimal("4.50"),
             variant_selected={"Farbe": "Red"},
             delivery_address={"street": "Weg 1", "city": "Witten", "postal": "58452",
                               "country": "DE"})
    db.add(s); db.commit()
    return s, l, p


def _fake_ae(*, prim_live_stock=0, alt_skus=None, prim_error=None):
    """FakeAE: unterscheidet Primaer-/Alt-URL; place_order faengt Ziel ab."""
    captured = {}

    class _Placed:
        aliexpress_order_id = "AE-ROUTED-1"
        cost_cny = None

    class _T:
        tracking_number = None
        carrier = None
        estimated_delivery = None

    class _AE:
        async def scrape_product(self, url):
            if url == PRIM_URL:
                if prim_error is not None:
                    raise prim_error
                return SimpleNamespace(
                    aliexpress_id="prim1", in_stock=True,
                    variants={"skus": [
                        {"attr": "A:1#Red", "id": "P1", "price": "5.0",
                         "stock": prim_live_stock, "options": {"Farbe": "Red"}},
                        {"attr": "A:2#Blue", "id": "P2", "price": "5.0",
                         "stock": 9, "options": {"Farbe": "Blue"}}]})
            return SimpleNamespace(
                aliexpress_id="alt1", in_stock=True,
                variants={"skus": alt_skus if alt_skus is not None else [
                    {"attr": "B:9#Rouge", "id": "S9", "price": "4.0",
                     "stock": 40, "options": {"Farbe": "Rouge"}}]})

        async def place_order(self, *, url, variant, quantity, delivery_name, delivery_address):
            captured["url"] = url
            captured["variant"] = variant
            captured["quantity"] = quantity
            return _Placed()

        async def get_tracking(self, aliexpress_order_id):
            return _T()

    return _AE(), captured


def test_routes_to_alt_variant_when_primary_oos(db, monkeypatch):
    """Kernfall: Primaer-Variante frisch OOS + Ziel-Variante verknuepft -> bestellt bei
    der Alt-Quelle GENAU die verknuepfte SKU; Claim traegt die Alt-Quelle; Ergebnis
    ist transparent (auto_routed)."""
    s, l, p = _setup(db, vmap={"A:1#Red": {"source": "alt1", "sku_attr": "B:9#Rouge",
                                           "sku_id": "S9"}})
    ae, cap = _fake_ae(prim_live_stock=0)
    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: ae)
    r = asyncio.run(order_service.fulfill_sale(db, sale_id=s.id, allow_alt_routing=True))
    assert cap["url"] == ALT_URL
    assert cap["variant"]["attr"] == "B:9#Rouge" and cap["variant"]["id"] == "S9"
    assert r["auto_routed"] is True and r["source_aliexpress_id"] == "alt1"
    o = db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == s.id))
    assert o is not None and str(o.source_aliexpress_id) == "alt1"


def test_no_routing_without_flag(db, monkeypatch):
    """Hintergrund-Fulfillment (Default allow_alt_routing=False) routet NIE – der
    Bestands-Preflight blockt sauber in needs_manual_review."""
    s, l, p = _setup(db, vmap={"A:1#Red": {"source": "alt1", "sku_attr": "B:9#Rouge",
                                           "sku_id": "S9"}})
    ae, cap = _fake_ae(prim_live_stock=0)
    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: ae)
    with pytest.raises(PersistentError, match="Bestand reicht nicht"):
        asyncio.run(order_service.fulfill_sale(db, sale_id=s.id))
    assert "url" not in cap                       # kein Geld-Call
    db.refresh(s)
    assert s.status == "needs_manual_review"


def test_no_routing_for_legacy_string_mapping(db, monkeypatch):
    """Legacy-Verknuepfung (nur Quellen-ID, keine Ziel-Variante) routet NIE automatisch –
    das waere Blindkauf auf der Alt-Quelle."""
    s, l, p = _setup(db, vmap={"A:1#Red": "alt1"})
    ae, cap = _fake_ae(prim_live_stock=0)
    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: ae)
    with pytest.raises(PersistentError, match="Bestand reicht nicht"):
        asyncio.run(order_service.fulfill_sale(db, sale_id=s.id, allow_alt_routing=True))
    assert "url" not in cap


def test_no_routing_when_primary_restocked(db, monkeypatch):
    """Cache sagt OOS, aber der FRISCHE Scrape zeigt Bestand -> KEIN Alt-Kauf, es wird
    normal bei der Hauptquelle bestellt (kein teurer Fehl-Umweg auf Cache-Basis)."""
    s, l, p = _setup(db, vmap={"A:1#Red": {"source": "alt1", "sku_attr": "B:9#Rouge",
                                           "sku_id": "S9"}})
    ae, cap = _fake_ae(prim_live_stock=25)
    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: ae)

    async def _fresh_variants(_db, _p):
        # _ensure_product_variants liefert den DB-Stand; fuer den Test: nach Routing-Scrape
        # zaehlt der Cache – wir setzen ihn lieferbar, wie es der Monitor nach dem Scrape taete.
        _p.variants["skus"][0]["stock"] = 25
        return _p.variants["skus"]

    monkeypatch.setattr(order_service, "_ensure_product_variants", _fresh_variants)
    r = asyncio.run(order_service.fulfill_sale(db, sale_id=s.id, allow_alt_routing=True))
    assert cap["url"] == PRIM_URL                 # Hauptquelle!
    assert r["auto_routed"] is False


def test_no_routing_on_primary_scrape_error(db, monkeypatch):
    """Unklare Datenlage (Scrape-Fehler) -> kein Routing; normaler Pfad blockt per
    Preflight (Cache 0 < 1) in needs_manual_review."""
    s, l, p = _setup(db, vmap={"A:1#Red": {"source": "alt1", "sku_attr": "B:9#Rouge",
                                           "sku_id": "S9"}})
    ae, cap = _fake_ae(prim_error=RuntimeError("timeout"))
    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: ae)
    with pytest.raises(PersistentError, match="Bestand reicht nicht"):
        asyncio.run(order_service.fulfill_sale(db, sale_id=s.id, allow_alt_routing=True))
    assert "url" not in cap


def test_routed_sku_id_drift_blocks_purchase(db, monkeypatch):
    """Alt-Quelle liefert den attr noch, aber mit ANDERER sku_id (Haendler-Edit) ->
    kein Kauf, needs_manual_review, Klartext."""
    s, l, p = _setup(db, vmap={"A:1#Red": {"source": "alt1", "sku_attr": "B:9#Rouge",
                                           "sku_id": "S9"}})
    ae, cap = _fake_ae(prim_live_stock=0, alt_skus=[
        {"attr": "B:9#Rouge", "id": "S9-GEAENDERT", "price": "4.0", "stock": 40,
         "options": {"Farbe": "Rouge"}}])
    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: ae)
    with pytest.raises(PersistentError, match="SKU-ID fehlt oder weicht ab"):
        asyncio.run(order_service.fulfill_sale(db, sale_id=s.id, allow_alt_routing=True))
    assert "url" not in cap
    db.refresh(s)
    assert s.status == "needs_manual_review"


def test_routed_missing_alt_attr_blocks(db, monkeypatch):
    """Ziel-attr existiert nicht mehr in der frisch gescrapten Alt-Quelle -> kein Kauf."""
    s, l, p = _setup(db, vmap={"A:1#Red": {"source": "alt1", "sku_attr": "B:9#Rouge",
                                           "sku_id": "S9"}})
    ae, cap = _fake_ae(prim_live_stock=0, alt_skus=[
        {"attr": "B:7#Anders", "id": "S7", "price": "4.0", "stock": 40,
         "options": {"Farbe": "Anders"}}])
    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: ae)
    with pytest.raises(PersistentError, match="existiert nicht mehr"):
        asyncio.run(order_service.fulfill_sale(db, sale_id=s.id, allow_alt_routing=True))
    assert "url" not in cap
    db.refresh(s)
    assert s.status == "needs_manual_review"


def test_routed_loss_guard_uses_alt_price(db, monkeypatch):
    """Verlust-Sperre rechnet mit dem FRISCHEN Alt-SKU-Preis: teure Alt-Variante ->
    VERLUST-STOPP statt Kauf."""
    s, l, p = _setup(db, vmap={"A:1#Red": {"source": "alt1", "sku_attr": "B:9#Rouge",
                                           "sku_id": "S9"}})
    ae, cap = _fake_ae(prim_live_stock=0, alt_skus=[
        {"attr": "B:9#Rouge", "id": "S9", "price": "80.0", "stock": 40,
         "options": {"Farbe": "Rouge"}}])
    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: ae)
    with pytest.raises(PersistentError, match="VERLUST-STOPP"):
        asyncio.run(order_service.fulfill_sale(db, sale_id=s.id, allow_alt_routing=True))
    assert "url" not in cap


def test_routed_without_alt_price_blocks(db, monkeypatch):
    """Alt-SKU ohne Preis -> Verlust-Pruefung unmoeglich -> Block (keine Schaetzung
    mit dem billigeren Primaer-EK)."""
    s, l, p = _setup(db, vmap={"A:1#Red": {"source": "alt1", "sku_attr": "B:9#Rouge",
                                           "sku_id": "S9"}})
    ae, cap = _fake_ae(prim_live_stock=0, alt_skus=[
        {"attr": "B:9#Rouge", "id": "S9", "price": None, "stock": 40,
         "options": {"Farbe": "Rouge"}}])
    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: ae)
    with pytest.raises(PersistentError, match="ohne Preis"):
        asyncio.run(order_service.fulfill_sale(db, sale_id=s.id, allow_alt_routing=True))
    assert "url" not in cap


def test_explicit_source_suppresses_auto_routing(db, monkeypatch):
    """Explizite Nutzer-Wahl (💡-Modal) gewinnt: kein Auto-Routing, auto_routed=False."""
    s, l, p = _setup(db, vmap={"A:1#Red": {"source": "alt1", "sku_attr": "B:9#Rouge",
                                           "sku_id": "S9"}})
    ae, cap = _fake_ae(prim_live_stock=0)
    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: ae)
    r = asyncio.run(order_service.fulfill_sale(
        db, sale_id=s.id, allow_alt_routing=True,
        source_aliexpress_id="alt1", sku_attr="B:9#Rouge"))
    assert cap["url"] == ALT_URL
    assert r["auto_routed"] is False              # Nutzer hat selbst gewaehlt


def test_no_routing_without_stored_sku_id_anchor(db, monkeypatch):
    """Review-Fund #1a (fail-closed): Verknuepfung OHNE gespeicherte sku_id (z.B. Snapshot
    war gekappt) routet NIE automatisch – kein Pin = kein Blindkauf."""
    s, l, p = _setup(db, vmap={"A:1#Red": {"source": "alt1", "sku_attr": "B:9#Rouge",
                                           "sku_id": None}})
    ae, cap = _fake_ae(prim_live_stock=0)
    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: ae)
    with pytest.raises(PersistentError, match="Bestand reicht nicht"):
        asyncio.run(order_service.fulfill_sale(db, sale_id=s.id, allow_alt_routing=True))
    assert "url" not in cap                       # kein Geld-Call


def test_routed_blocks_when_fresh_sku_id_missing(db, monkeypatch):
    """Review-Fund #1b (fail-closed): frische Alt-SKU OHNE id -> Pin nicht verifizierbar
    -> kein Kauf, needs_manual_review."""
    s, l, p = _setup(db, vmap={"A:1#Red": {"source": "alt1", "sku_attr": "B:9#Rouge",
                                           "sku_id": "S9"}})
    ae, cap = _fake_ae(prim_live_stock=0, alt_skus=[
        {"attr": "B:9#Rouge", "id": None, "price": "4.0", "stock": 40,
         "options": {"Farbe": "Rouge"}}])
    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: ae)
    with pytest.raises(PersistentError, match="SKU-ID fehlt"):
        asyncio.run(order_service.fulfill_sale(db, sale_id=s.id, allow_alt_routing=True))
    assert "url" not in cap
    db.refresh(s)
    assert s.status == "needs_manual_review"


def test_force_suppresses_auto_routing(db, monkeypatch):
    """Review-Fund #4: force ('Trotzdem bestellen') skippt Preflight+Verlust-Sperre –
    darf darum NIE gleichzeitig die Geldquelle wechseln: bestellt die HAUPTQUELLE."""
    s, l, p = _setup(db, vmap={"A:1#Red": {"source": "alt1", "sku_attr": "B:9#Rouge",
                                           "sku_id": "S9"}})
    ae, cap = _fake_ae(prim_live_stock=0)
    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: ae)
    r = asyncio.run(order_service.fulfill_sale(db, sale_id=s.id,
                                               allow_alt_routing=True, force=True))
    assert cap["url"] == PRIM_URL                 # Hauptquelle, KEIN Alt-Umweg
    assert r["auto_routed"] is False


def test_unclear_product_level_oos_does_not_route(db, monkeypatch):
    """Review-Fund #5: in_stock=False, aber KEIN Bestand im SKU-Satz parsebar (Felddrift)
    = unklare Datenlage -> kein Routing (normaler Pfad blockt per Cache-Preflight)."""
    from types import SimpleNamespace

    s, l, p = _setup(db, vmap={"A:1#Red": {"source": "alt1", "sku_attr": "B:9#Rouge",
                                           "sku_id": "S9"}})

    class _AE2:
        async def scrape_product(self, url):
            assert url == PRIM_URL
            return SimpleNamespace(
                aliexpress_id="prim1", in_stock=False,   # Flag sagt OOS ...
                variants={"skus": [
                    {"attr": "A:1#Red", "id": "P1", "price": "5.0",
                     "stock": None, "options": {"Farbe": "Red"}}]})  # ... aber nichts parsebar

        async def place_order(self, **kw):
            raise AssertionError("darf nicht bestellen")

    monkeypatch.setattr(order_service, "get_aliexpress_client", lambda: _AE2())
    with pytest.raises(PersistentError, match="Bestand reicht nicht"):
        asyncio.run(order_service.fulfill_sale(db, sale_id=s.id, allow_alt_routing=True))
