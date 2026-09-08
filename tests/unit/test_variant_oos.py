"""Per-Varianten-Ausverkauf: einzelne SKU ausverkauft -> eBay-Menge NUR dieser Variante
auf 0; verknuepfte Ausweich-Quelle haelt die Variante lieferbar; Preis-Check zeigt den
Sold-out-Status je Variante.

Deckt die reine Entscheidungs-/Zuordnungslogik ab (unabhaengig vom Mock-Scrape, der
keinen Per-SKU-Bestand liefert): Bestand wird direkt in product.variants gesetzt.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import get_settings
from app.models import Listing, Product
from app.retry import PersistentError
from app.services import golive_service, listing_match_service, monitoring_service, supplier_service

_COLORS = ["Rot", "Blau", "Grün"]


def _mk(db, stocks, *, alt_sources=None, variant_source_map=None):
    """Listing + Product mit len(stocks) Farb-Varianten (Bestand je Variante = stocks[i])."""
    n = len(stocks)
    p = Product(
        aliexpress_url="https://de.aliexpress.com/item/vo.html", aliexpress_id="vo",
        variants={"axes": {"Farbe": _COLORS[:n]},
                  "skus": [{"attr": f"a{i}", "options": {"Farbe": _COLORS[i]},
                            "price": "5", "stock": stocks[i]} for i in range(n)]},
    )
    if alt_sources is not None:
        p.alternatives = {"sources": alt_sources}
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-1", title_seo="T", description="d",
                listing_status="active", ebay_item_id="123", price_eur=Decimal("19.95"),
                variant_source_map=variant_source_map)
    db.add(l); db.flush()
    return l, p


class _CapEbay:
    """Faengt Mengen-Pushes je SKU ab (Mock-Pfad ruft update_inventory(sku, quantity=))."""
    def __init__(self):
        self.calls = []

    async def update_inventory(self, sku, *, quantity=None, **kw):
        self.calls.append((sku, quantity))


# ---------------------------------------------------------------- variant_stock_state

def test_variant_stock_state_conservative(db):
    # Bestand 10 -> lieferbar; 0 -> ausverkauft; None (unbekannt) -> lieferbar (Umsatzschutz)
    l, p = _mk(db, [10, 0])
    p.variants["skus"][1]["attr"] = "b1"           # eigener attr
    state = golive_service.variant_stock_state(l, p)
    assert [s["ebay_sku"] for s in state] == ["AE-1-V1", "AE-1-V2"]
    assert state[0]["oos"] is False and state[0]["in_stock"] is True
    assert state[1]["oos"] is True and state[1]["in_stock"] is False
    assert state[1]["attr"] == "b1"

    # Unbekannter Bestand (None) darf NIE als ausverkauft gelten.
    p.variants["skus"][1]["stock"] = None
    state2 = golive_service.variant_stock_state(l, p)
    assert state2[1]["oos"] is False and state2[1]["in_stock"] is True


def test_variant_stock_state_empty_for_single_variant(db):
    l, p = _mk(db, [5])   # nur 1 SKU -> kein echtes Multivarianten-Listing
    assert golive_service.variant_stock_state(l, p) == []


# ---------------------------------------------------------------- _variant_rows (Report)

def test_variant_rows_expose_oos_and_alt_fields(db):
    l, p = _mk(db, [10, 0],
               alt_sources=[{"aliexpress_id": "999", "url": "u", "in_stock": True}],
               variant_source_map={"a1": "999"})
    rows = listing_match_service._variant_rows(l, p, get_settings())
    by_sku = {r["sku"]: r for r in rows}
    assert by_sku["AE-1-V1"]["oos"] is False
    v2 = by_sku["AE-1-V2"]
    assert v2["oos"] is True                # bei Hauptquelle ausverkauft
    assert v2["alt_source_id"] == "999"
    assert v2["alt_in_stock"] is True
    assert v2["sellable"] is True           # bleibt verkaufbar dank Ausweich-Quelle


def test_variant_rows_oos_without_alt_is_not_sellable(db):
    l, p = _mk(db, [10, 0])
    v2 = {r["sku"]: r for r in listing_match_service._variant_rows(l, p, get_settings())}["AE-1-V2"]
    assert v2["oos"] is True and v2["sellable"] is False and v2["alt_source_id"] is None


# ---------------------------------------------------------------- _sync_variant_stock

@pytest.mark.asyncio
async def test_sync_zeros_only_the_oos_variant(db):
    l, p = _mk(db, [10, 0])
    s = get_settings()
    cap = _CapEbay()
    res = await monitoring_service._sync_variant_stock(db, cap, False, l, p, s)
    assert res["all_oos"] is False and res["changed"] == 2
    # NUR V2 auf 0, V1 auf Standardmenge:
    assert dict(cap.calls) == {"AE-1-V1": s.default_listing_quantity, "AE-1-V2": 0}
    assert l.variant_stock == {"AE-1-V1": s.default_listing_quantity, "AE-1-V2": 0}
    assert l.supplier_in_stock is True         # Listing bleibt sichtbar (V1 lieferbar)


@pytest.mark.asyncio
async def test_sync_is_idempotent(db):
    l, p = _mk(db, [10, 0])
    s = get_settings()
    await monitoring_service._sync_variant_stock(db, _CapEbay(), False, l, p, s)
    cap2 = _CapEbay()
    res = await monitoring_service._sync_variant_stock(db, cap2, False, l, p, s)
    assert res["changed"] == 0 and cap2.calls == []   # nichts geaendert -> kein Push


@pytest.mark.asyncio
async def test_sync_all_oos_marks_whole_listing(db):
    l, p = _mk(db, [0, 0])
    res = await monitoring_service._sync_variant_stock(db, _CapEbay(), False, l, p, get_settings())
    assert res["all_oos"] is True
    assert l.supplier_in_stock is False and l.quantity_available == 0


@pytest.mark.asyncio
async def test_linked_in_stock_alt_keeps_variant_sellable(db):
    # Beide bei der Hauptquelle ausverkauft; a0 hat eine lieferbare Ausweich-Quelle.
    l, p = _mk(db, [0, 0],
               alt_sources=[{"aliexpress_id": "999", "url": "u", "in_stock": True}],
               variant_source_map={"a0": "999"})
    s = get_settings()
    cap = _CapEbay()
    res = await monitoring_service._sync_variant_stock(db, cap, False, l, p, s)
    # a0 (V1) bleibt lieferbar (Ausweich-Quelle), a1 (V2) auf 0.
    assert dict(cap.calls) == {"AE-1-V1": s.default_listing_quantity, "AE-1-V2": 0}
    assert res["all_oos"] is False and l.supplier_in_stock is True


@pytest.mark.asyncio
async def test_out_of_stock_alt_does_not_keep_sellable(db):
    l, p = _mk(db, [0, 5],
               alt_sources=[{"aliexpress_id": "999", "url": "u", "in_stock": False}],
               variant_source_map={"a0": "999"})
    s = get_settings()
    cap = _CapEbay()
    await monitoring_service._sync_variant_stock(db, cap, False, l, p, s)
    # Ausweich-Quelle selbst ausverkauft -> V1 trotzdem auf 0.
    assert dict(cap.calls)["AE-1-V1"] == 0


# ---------------------------------------------------------------- link/unlink service

def test_set_variant_alt_source_validates(db):
    l, p = _mk(db, [10, 0],
               alt_sources=[{"aliexpress_id": "999", "url": "u", "in_stock": True}])
    # Unbekannte Variante -> Fehler
    with pytest.raises(PersistentError):
        supplier_service.set_variant_alt_source(db, listing_id=l.id, sku_attr="zzz", aliexpress_id="999")
    # aliexpress_id ist kein hinterlegter Slot -> Fehler
    with pytest.raises(PersistentError):
        supplier_service.set_variant_alt_source(db, listing_id=l.id, sku_attr="a1", aliexpress_id="nope")
    # Happy path
    r = supplier_service.set_variant_alt_source(db, listing_id=l.id, sku_attr="a1", aliexpress_id="999")
    assert r["variant_source_map"] == {"a1": "999"}
    assert db.get(Listing, l.id).variant_source_map == {"a1": "999"}


def test_clear_variant_alt_source(db):
    l, p = _mk(db, [10, 0],
               alt_sources=[{"aliexpress_id": "999", "url": "u", "in_stock": True}],
               variant_source_map={"a1": "999"})
    supplier_service.clear_variant_alt_source(db, listing_id=l.id, sku_attr="a1")
    assert (db.get(Listing, l.id).variant_source_map or {}) == {}


# ---------------------------------------------------------------- endpoint (routing)

# ---------------------------------------------------------------- Review-Fixes

def test_stock_num_unknown_vs_known():
    f = golive_service._stock_num
    assert f(None) == (False, 0) and f("") == (False, 0) and f("N/A") == (False, 0)
    assert f(True) == (False, 0)                    # bool ist KEIN Bestand
    assert f(0) == (True, 0) and f("0") == (True, 0)
    assert f(100) == (True, 100) and f("100") == (True, 100)
    assert f("1,000") == (True, 1000) and f("1 000") == (True, 1000)


def test_empty_string_stock_is_not_oos(db):
    # Leerer/unbekannter Bestand darf NIE als ausverkauft gelten (sonst Umsatzverlust).
    l, p = _mk(db, [10, ""])
    state = golive_service.variant_stock_state(l, p)
    assert state[1]["oos"] is False and state[1]["in_stock"] is True and state[1]["stock"] is None


class _FailEbay:
    async def update_inventory(self, *a, **k):
        raise RuntimeError("eBay down")


@pytest.mark.asyncio
async def test_push_failure_leaves_state_untouched(db):
    # Schlaegt der eBay-Push fehl, duerfen weder variant_stock noch supplier_in_stock
    # veraendert werden (sonst zeigt die DB 'ausverkauft' ohne genullte Menge).
    l, p = _mk(db, [10, 0])
    res = await monitoring_service._sync_variant_stock(db, _FailEbay(), False, l, p, get_settings())
    assert res["pushed"] is False and res["all_oos"] is False
    assert l.variant_stock is None                 # NICHT persistiert
    assert l.supplier_in_stock is True             # unveraendert (Retry naechster Lauf)


@pytest.mark.asyncio
async def test_recovery_restores_all_variants_after_whole_oos(db):
    # Nach einem Whole-OOS-Push steht variant_stock auf all-0. Kommt das Produkt zurueck,
    # MUESSEN alle Varianten wieder hochgesetzt werden (nicht auf 0 haengen bleiben).
    l, p = _mk(db, [10, 10])
    l.variant_stock = {"AE-1-V1": 0, "AE-1-V2": 0}
    s = get_settings()
    cap = _CapEbay()
    await monitoring_service._sync_variant_stock(db, cap, False, l, p, s)
    assert dict(cap.calls) == {"AE-1-V1": s.default_listing_quantity,
                               "AE-1-V2": s.default_listing_quantity}


def test_variant_alt_source_endpoint(db, client):
    l, p = _mk(db, [10, 0],
               alt_sources=[{"aliexpress_id": "999", "url": "u", "in_stock": True}])
    db.commit()
    resp = client.post(f"/api/v1/products/{l.id}/variant-alt-source",
                       json={"sku_attr": "a1", "aliexpress_id": "999"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["variant_source_map"] == {"a1": "999"}
    assert "resync" in body
    # Unlink
    resp2 = client.request("DELETE", f"/api/v1/products/{l.id}/variant-alt-source",
                           params={"sku_attr": "a1"})
    assert resp2.status_code == 200, resp2.text
    assert (resp2.json().get("variant_source_map") or {}) == {}


# ------------------- Ziel-Variante der Ausweich-Quelle (Projekt 11.07.) -------------------
def _alt_slot(aid="alt1", *, in_stock=True, skus=None, truncated=False):
    s = {"aliexpress_id": aid, "url": f"https://de.aliexpress.com/item/{aid}.html",
         "title": "Alt-Quelle", "in_stock": in_stock}
    if skus is not None:
        s["skus"] = skus
        s["skus_truncated"] = truncated
    return s


def test_parse_variant_alt_formats():
    P = supplier_service.parse_variant_alt
    assert P(None) is None and P("") is None and P({}) is None
    legacy = P("12345")
    assert legacy["source"] == "12345" and legacy["sku_attr"] is None
    d = P({"source": "9", "sku_attr": "x#1", "sku_id": "S1", "name": "Rot", "image": "i.jpg"})
    assert d["source"] == "9" and d["sku_attr"] == "x#1" and d["sku_id"] == "S1"
    assert P({"id": "7", "sku_attr": "y"})["source"] == "7"     # Alternativ-Key toleriert
    assert P({"kaputt": True}) is None


def _uniq(db, l, p, tag):
    """_mk-Produkt eindeutig machen (UNIQUE-Spalten) fuer Mehrfach-_mk im selben Test."""
    p.aliexpress_id = f"vo-{tag}"
    p.aliexpress_url = f"https://de.aliexpress.com/item/vo-{tag}.html"
    l.ebay_sku = f"AE-{tag}"
    l.ebay_item_id = f"123-{tag}"
    db.flush()
    return l, p


def test_variant_alt_availability_matrix(db):
    A = supplier_service.variant_alt_availability
    snap = [{"attr": "b#1", "id": "S1", "stock": 3, "price": "4.0", "image": "i.jpg"}]
    l, p = _mk(db, [0, 5], alt_sources=[_alt_slot(skus=snap)])
    entry = {"source": "alt1", "sku_attr": "b#1", "sku_id": "S1"}
    assert A(p, entry)["available"] is True                       # Bestand 3 >= 1
    assert A(p, entry, need=5)["available"] is False              # Bestand < Menge
    assert A(p, {"source": "weg", "sku_attr": "b#1"})["available"] is False   # Slot fehlt
    assert A(p, "alt1")["available"] is True                      # Legacy -> ganze Quelle
    assert A(p, {"source": "alt1", "sku_attr": "FEHLT#9"})["available"] is False  # attr weg
    drift = {"source": "alt1", "sku_attr": "b#1", "sku_id": "ANDERE"}
    assert A(p, drift)["available"] is False                      # SKU-ID weicht ab
    _uniq(db, l, p, "m1")                                         # 'vo' freigeben
    # Slot ausverkauft schlaegt alles
    l2, p2 = _mk(db, [0, 5], alt_sources=[_alt_slot(in_stock=False, skus=snap)])
    assert A(p2, entry)["available"] is False
    _uniq(db, l2, p2, "m2")
    # Kein Snapshot -> Uebergangs-Fallback ganze Quelle
    l3, p3 = _mk(db, [0, 5], alt_sources=[_alt_slot(skus=None)])
    assert A(p3, entry)["available"] is True
    _uniq(db, l3, p3, "m3")
    # Bestand unbekannt (None) + Slot lieferbar -> True (konservativ)
    snap_unk = [{"attr": "b#1", "id": "S1", "stock": None}]
    l4, p4 = _mk(db, [0, 5], alt_sources=[_alt_slot(skus=snap_unk)])
    assert A(p4, entry)["available"] is True


async def test_sync_nulls_variant_when_alt_sku_out_of_stock(db):
    """Slot als Ganzes lieferbar, aber die ZIEL-Variante hat Bestand 0 -> Variante wird
    trotzdem genullt (frueher haette die ganze Quelle sie faelschlich sellable gehalten)."""
    snap = [{"attr": "b#1", "id": "S1", "stock": 0}]
    l, p = _mk(db, [0, 5], alt_sources=[_alt_slot(in_stock=True, skus=snap)],
               variant_source_map={"a0": {"source": "alt1", "sku_attr": "b#1", "sku_id": "S1"}})
    eb = _CapEbay()
    await monitoring_service._sync_variant_stock(db, eb, False, l, p, get_settings())
    assert (l.variant_stock or {}).get("AE-1-V1") == 0            # genullt trotz Slot ok


async def test_sync_keeps_variant_when_alt_sku_in_stock(db):
    snap = [{"attr": "b#1", "id": "S1", "stock": 9}]
    l, p = _mk(db, [0, 5], alt_sources=[_alt_slot(skus=snap)],
               variant_source_map={"a0": {"source": "alt1", "sku_attr": "b#1", "sku_id": "S1"}})
    eb = _CapEbay()
    await monitoring_service._sync_variant_stock(db, eb, False, l, p, get_settings())
    q = get_settings().default_listing_quantity
    assert (l.variant_stock or {}).get("AE-1-V1") == q            # bleibt sellbar


def test_invalidate_variant_links_clears_all_listings(db):
    l, p = _mk(db, [0, 5], variant_source_map={"a0": "alt1"})
    n = supplier_service._invalidate_variant_links(db, p, reason="test primary switch")
    db.commit()
    assert n == 1
    db.refresh(l)
    assert l.variant_source_map is None


def test_set_variant_alt_source_with_target_variant(db):
    """Mit alt_sku_attr wird der Dict-Eintrag inkl. sku_id (Drift-Anker) gespeichert."""
    snap = [{"attr": "b#1", "id": "S1", "stock": 5, "image": "alt.jpg"}]
    l, p = _mk(db, [0, 5], alt_sources=[_alt_slot(skus=snap)])
    db.commit()
    r = supplier_service.set_variant_alt_source(
        db, listing_id=l.id, sku_attr="a0", aliexpress_id="alt1",
        alt_sku_attr="b#1", alt_name="Rouge / L")
    e = r["variant_source_map"]["a0"]
    assert e["source"] == "alt1" and e["sku_attr"] == "b#1" and e["sku_id"] == "S1"
    assert e["image"] == "alt.jpg" and e["name"] == "Rouge / L"
    # Unbekannte Ziel-Variante -> klare Ablehnung
    with pytest.raises(PersistentError, match="Ziel-Variante"):
        supplier_service.set_variant_alt_source(
            db, listing_id=l.id, sku_attr="a0", aliexpress_id="alt1", alt_sku_attr="GIBTS#NICHT")


# ------------------------------------------------- Restock-Beweis (Review-Fund 16.08.)

@pytest.mark.asyncio
async def test_degraded_scrape_does_not_restock_zeroed_variants(db):
    """Ohne positiven Bestands-Beweis (restock_ok=False) KEINE Mengen-Erhoehung:
    genullte Varianten bleiben 0 und der Spiegel behaelt die Null-Markierungen."""
    l, p = _mk(db, [10, 10])                       # stale gespeicherte Bestaende > 0
    s = get_settings()
    l.variant_stock = {"AE-1-V1": 0, "AE-1-V2": 0}   # zuvor komplett genullt
    cap = _CapEbay()
    res = await monitoring_service._sync_variant_stock(
        db, cap, False, l, p, s, restock_ok=False)
    assert cap.calls == []                                       # kein Restock-Push
    assert l.variant_stock == {"AE-1-V1": 0, "AE-1-V2": 0}       # Nullen bleiben
    assert res["all_oos"] is False


@pytest.mark.asyncio
async def test_degraded_scrape_still_allows_zeroing(db):
    """Der Beweis-Filter blockt nur ERHOEHUNGEN — eine echt gemeldete 0 wird
    weiterhin gepusht (Ausverkauft-Schutz bleibt voll wirksam)."""
    l, p = _mk(db, [10, 0])
    s = get_settings()
    l.variant_stock = {"AE-1-V1": s.default_listing_quantity,
                       "AE-1-V2": s.default_listing_quantity}
    cap = _CapEbay()
    await monitoring_service._sync_variant_stock(
        db, cap, False, l, p, s, restock_ok=False)
    assert dict(cap.calls) == {"AE-1-V2": 0}
    assert l.variant_stock == {"AE-1-V1": s.default_listing_quantity, "AE-1-V2": 0}


@pytest.mark.asyncio
async def test_restock_with_evidence_restores_zeroed_variants(db):
    l, p = _mk(db, [10, 10])
    s = get_settings()
    l.variant_stock = {"AE-1-V1": 0, "AE-1-V2": 0}
    cap = _CapEbay()
    await monitoring_service._sync_variant_stock(
        db, cap, False, l, p, s, restock_ok=True)
    assert dict(cap.calls) == {"AE-1-V1": s.default_listing_quantity,
                               "AE-1-V2": s.default_listing_quantity}
