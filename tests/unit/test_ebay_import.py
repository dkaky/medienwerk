"""Tests fuer den eBay-Import (aktive Listings + Verkaeufe) mit injiziertem Fake-Client."""
from __future__ import annotations

import asyncio
from decimal import Decimal

from app.models import Listing, Product, Sale
from app.services import ebay_import_service


class _FakeEbay:
    _last_active_fetch_complete = True     # simuliert einen bis zur letzten Seite kompletten Fetch

    async def get_active_listings(self, *, max_items=500):
        return [{
            "item_id": "111", "title": "Allah Halskette Edelstahl", "sku": "S1",
            "price": "14.95", "currency": "EUR", "quantity": "220",
            "quantity_sold": "5", "category_id": "281", "view_url": None,
        }]

    async def list_all_orders(self, *, since, max_orders=500):
        # Vollstaendige lineItem-Struktur (lineItemId + total) – der Import laeuft jetzt
        # ueber den line-item-genauen sync_ebay_orders-Pfad.
        return [{
            "orderId": "O-1", "legacyOrderId": "O-1", "creationDate": "2026-06-30T12:00:00.000Z",
            "orderFulfillmentStatus": "FULFILLED", "buyer": {"username": "kaeufer1"},
            "pricingSummary": {"total": {"value": "14.95", "currency": "EUR"}},
            "lineItems": [{"legacyItemId": "111", "lineItemId": "L-1", "quantity": 1,
                           "total": {"value": "14.95", "currency": "EUR"}}],
        }]


class _FakeEbayVariant:
    """Order mit Publish-SKU 'AE-900-V2' + Kaeufer-Variante (Farbe: Blau) auf Listing 900."""
    _last_active_fetch_complete = True

    async def get_active_listings(self, *, max_items=500):
        return []

    async def list_all_orders(self, *, since, max_orders=500):
        # NICHT fulfilled -> Sale bleibt "pending" (Auto-Korrektur greift nur bei offenen Sales).
        return [{
            "orderId": "OX", "legacyOrderId": "OX", "creationDate": "2026-06-30T12:00:00.000Z",
            "buyer": {"username": "k"},
            "pricingSummary": {"total": {"value": "14.95", "currency": "EUR"}},
            "lineItems": [{"legacyItemId": "900", "lineItemId": "LX", "quantity": 1,
                           "sku": "AE-900-V2",
                           "variationAspects": [{"name": "Farbe", "value": "Blau"}],
                           "total": {"value": "14.95", "currency": "EUR"}}],
        }]


# ----------------------------- RECONCILE-Vollstaendigkeits-Wache (Fund 17.07.) -----------------------------
# Ursache 615->500: get_active_listings(max_items=500) kappte einen 615-Artikel-Shop, und der
# Reconcile markierte die 115 nicht geholten, aber LIVE Listings faelschlich als 'ended'. Fix:
# (a) vollstaendig paginieren, (b) Reconcile NUR bei nachweislich komplettem Fetch laufen lassen.

class _FakeListings:
    """Nur get_active_listings; ``complete`` simuliert das Vollstaendigkeits-Signal des echten
    Clients (True = bis zur letzten Seite paginiert; False = gekappt/leere Zwischenseite)."""
    def __init__(self, rows, complete=True):
        self._rows = rows
        self._last_active_fetch_complete = complete

    async def get_active_listings(self, *, max_items=10000):
        return self._rows[:max_items]


def _row(item_id):
    return {"item_id": str(item_id), "title": f"Artikel {item_id}", "sku": f"S{item_id}",
            "price": "19.95", "quantity": "5", "category_id": "281"}


def _seed_active(db, item_ids, *, status="active"):
    p = Product(aliexpress_url=f"https://ae/{item_ids[0]}", aliexpress_id=f"ae{item_ids[0]}")
    db.add(p)
    db.flush()
    for iid in item_ids:
        db.add(Listing(product_id=p.id, ebay_sku=f"S{iid}", ebay_item_id=str(iid),
                       title_seo=f"Artikel {iid}", description="d", listing_status=status,
                       price_eur=Decimal("19.95")))
    db.commit()


async def test_reconcile_ends_stale_only_on_complete_fetch(db):
    """Kompletter Fetch (bis zur letzten Seite): ein aktiv gebliebenes DB-Listing, das eBay NICHT
    mehr listet, wird korrekt auf 'ended' gesetzt (echter eBay-Zustand). Auch bei >50 % Abbau:
    das explizite Vollstaendigkeits-Signal erlaubt legitimen Massen-Abbau (kein Deadlock)."""
    _seed_active(db, [100, 200, 300, 400])
    res = await ebay_import_service.import_listings(
        db, ebay=_FakeListings([_row(100)], complete=True))   # 200/300/400 wirklich beendet (75 % weg)
    assert res["reconcile_skipped"] is None
    assert res["reconciled_ended"] == 3
    assert db.scalar(select_status(db, 100)) == "active"
    for iid in (200, 300, 400):
        assert db.scalar(select_status(db, iid)) == "ended"


async def test_reconcile_skipped_on_incomplete_fetch(db):
    """Unvollstaendiger Fetch (gekappt oder leere Zwischenseite -> complete=False): KEIN Listing
    wird beendet – genau der 615->500-Schutz, unabhaengig von der geholten Menge."""
    _seed_active(db, [100, 200, 300, 400])
    res = await ebay_import_service.import_listings(
        db, ebay=_FakeListings([_row(100), _row(200), _row(300)], complete=False))  # 400 fehlt, aber Fetch unvollstaendig
    assert res["reconcile_skipped"] == "incomplete_fetch"
    assert res["reconciled_ended"] == 0
    assert db.scalar(select_status(db, 400)) == "active"     # NICHT faelschlich beendet


async def test_import_reactivates_previously_ended_listing(db):
    """SELF-HEAL: ein faelschlich 'ended' Listing, das wieder in der aktiven eBay-Liste auftaucht,
    wird zurueck auf 'active' gesetzt (so heilen die 115 nach dem Fix)."""
    _seed_active(db, [100, 200, 300], status="active")
    _seed_active(db, [500], status="ended")     # war faelschlich beendet
    await ebay_import_service.import_listings(
        db, ebay=_FakeListings([_row(100), _row(200), _row(300), _row(500)]))
    assert db.scalar(select_status(db, 500)) == "active"


def select_status(db, item_id):
    from sqlalchemy import select
    return select(Listing.listing_status).where(Listing.ebay_item_id == str(item_id))


# --------------------- Kategorie-Nachtrag (kategoriegenaue Provision) ---------------------
class _FakeCatEbay:
    """GetItem-Ersatz: liefert PrimaryCategory je ItemID; 'boom' wirft."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls: list[str] = []

    async def get_item_category(self, item_id):
        self.calls.append(str(item_id))
        if str(item_id) == "boom":
            raise RuntimeError("GetItem kaputt")
        return self.mapping[str(item_id)]


def _act(db, item_id, name=None, cid=None):
    l = Listing(ebay_item_id=item_id, title_seo=f"A{item_id}", description="d",
                listing_status="active", price_eur=Decimal("24.95"),
                category_name=name, category_id=cid)
    db.add(l)
    db.flush()
    return l


def test_backfill_categories_fills_only_missing_and_survives_errors(db):
    """GetMyeBaySelling liefert keine Kategorie -> Nachtrag per GetItem. Bereits
    gefuellte Listings werden NICHT erneut abgefragt, ein Fehler stoppt den Lauf nicht."""
    a = _act(db, "111")                                   # ohne Kategorie -> wird geholt
    b = _act(db, "222", name="Auto & Motorrad:Teile")     # schon gefuellt -> uebersprungen
    c = _act(db, "boom")                                  # Fehler -> toleriert
    db.commit()
    fake = _FakeCatEbay({"111": ("281", "Uhren & Schmuck:Halsketten")})

    r = asyncio.run(ebay_import_service.backfill_categories(db, ebay=fake, rate_s=0))

    assert set(fake.calls) == {"111", "boom"}      # das gefuellte Listing wurde nicht angefasst
    assert r["updated"] == 1 and r["errors"] == 1
    db.refresh(a); db.refresh(b); db.refresh(c)
    assert a.category_name == "Uhren & Schmuck:Halsketten" and a.category_id == "281"
    assert b.category_name == "Auto & Motorrad:Teile"      # unveraendert
    assert c.category_name is None
    assert r["remaining"] == 1                             # das kaputte bleibt offen


def test_backfill_categories_respects_limit(db):
    for i in range(5):
        _act(db, f"L{i}")
    db.commit()
    fake = _FakeCatEbay({f"L{i}": ("1", "Sport:Radsport") for i in range(5)})
    r = asyncio.run(ebay_import_service.backfill_categories(db, ebay=fake, limit=2, rate_s=0))
    assert r["scanned"] == 2 and len(fake.calls) == 2
    assert r["remaining"] == 3


def test_category_fee_overview_flags_every_deviating_category(db):
    """Nicht nur Schmuck: die Auswertung zeigt JEDE Kategorie, deren echte Provision
    vom Default abweicht – mit Vorzeichen (Schmuck +4, Elektronik -5, Auto 0)."""
    _act(db, "1", name="Uhren & Schmuck:Halsketten")            # 16 % -> +4,0 pp
    _act(db, "2", name="Uhren & Schmuck:Armbaender")            # gleiche Top-Kategorie
    _act(db, "3", name="Auto & Motorrad:Teile & Zubehoer")      # 12 % -> 0,0 pp
    _act(db, "4", name="TV, Video & Audio:Zubehoer")            # 7 %  -> -5,0 pp
    _act(db, "5")                                               # ohne Kategorie
    db.commit()

    o = ebay_import_service.category_fee_overview(db)

    assert o["default_commission_pct"] == 12.0
    assert o["listings_without_category"] == 1
    by = {r["category"]: r for r in o["by_category"]}
    assert by["Uhren & Schmuck"]["listings"] == 2          # Top-Level zusammengefasst
    assert by["Uhren & Schmuck"]["diff_to_default_pp"] == 4.0
    assert by["TV, Video & Audio"]["diff_to_default_pp"] == -5.0
    assert by["Auto & Motorrad"]["diff_to_default_pp"] == 0.0
    assert o["affected_listings"] == 3                     # 2x Schmuck + 1x TV, Auto nicht
    # Groesste Abweichung zuerst -> das UI/Log zeigt das Wichtigste oben.
    assert o["by_category"][0]["category"] == "TV, Video & Audio"
