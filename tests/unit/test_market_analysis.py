"""Tests fuer die KI-Marktanalyse (Optimierung C/D, 2026-07-05):
Konkurrenzpreise (Browse), analyze_market (LLM), market_analysis-Service,
Anzeigentarif setzen/anheben."""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.integrations.ebay import MockEbayClient
from app.integrations.llm import MockLLMClient
from app.models import Listing, Product
from app.services import optimization_service as O


# ----------------------------- eBay: Konkurrenzpreise -----------------------------
def test_mock_competitor_offers_have_prices():
    offers = asyncio.run(MockEbayClient().search_competitor_offers("HTC NE40", limit=5))
    assert offers and all(o["price_eur"] and o["price_eur"] > 0 for o in offers)
    assert all(o["currency"] == "EUR" for o in offers)
    assert all(o["sold"] is None for o in offers)
    # Titel-Wrapper bleibt kompatibel
    titles = asyncio.run(MockEbayClient().search_competitor_titles("HTC NE40"))
    assert titles and isinstance(titles[0], str)


def test_price_stats():
    s = O._price_stats([20.0, 10.0, 30.0])
    assert s == {"min": 10.0, "median": 20.0, "max": 30.0, "count": 3}
    assert O._price_stats([]) is None


# ----------------------------- LLM: analyze_market -----------------------------
def test_mock_analyze_market():
    r = asyncio.run(MockLLMClient().analyze_market(
        product_title="HTC NE40", current_price_eur=18.0,
        variants=[{"key": "AE-1-V1", "name": "Schwarz", "ek_eur": 8.0, "current_price_eur": 18.0}],
        competitor_prices=[{"title": "x", "price_eur": 25.0}, {"title": "y", "price_eur": 29.0}]))
    assert r["price_recommendations"] and r["price_recommendations"][0]["variant_key"] == "AE-1-V1"
    assert r["push_recommendations"]   # unter Median -> Push


# ----------------------------- Service: market_analysis -----------------------------
def _listing_with_variants(db, live=True):
    p = Product(aliexpress_url="https://de.aliexpress.com/item/m1.html", aliexpress_id="m1",
                price_cny=Decimal("6"), supplier_id="s1",
                variants={"axes": {"Farbe": ["Schwarz", "Rot"]},
                          "skus": [{"attr": "14:1", "options": {"Farbe": "Schwarz"}, "id": 1,
                                    "price": 6.0, "stock": 9},
                                   {"attr": "14:2", "options": {"Farbe": "Rot"}, "id": 2,
                                    "price": 8.0, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-MK", title_seo="HTC NE40 Kopfhörer Bluetooth",
                description="d", listing_status="active", price_eur=Decimal("18.95"),
                cost_eur=Decimal("8"), ebay_item_id=("800111" if live else None))
    db.add(l); db.commit()
    return l


def test_market_analysis_propose_only(db, monkeypatch):
    # market_analysis nutzt _real_ebay (auf dem VPS ist get_ebay_client gemockt) -> im
    # Test durch den Mock ersetzen, damit echte Konkurrenzpreise simuliert werden.
    monkeypatch.setattr(O, "_real_ebay", lambda: MockEbayClient())
    l = _listing_with_variants(db)
    r = asyncio.run(O.market_analysis(db, listing_id=l.id))
    assert r["listing_id"] == l.id
    assert r["competitor_price_range"] and r["competitor_price_range"]["count"] > 0
    assert len(r["variants"]) == 2
    for v in r["variants"]:
        assert v["recommended_price_eur"] and v["recommended_price_eur"] > 0
        # Rundung auf x,95
        assert abs(round(v["recommended_price_eur"] % 1, 2) - 0.95) < 0.001
    assert "ad_rate_pct" in r
    # propose-only: Preis am Listing unveraendert
    db.refresh(l)
    assert float(l.price_eur) == 18.95


def test_market_analysis_missing_listing(db):
    with pytest.raises(ValueError, match="nicht gefunden"):
        asyncio.run(O.market_analysis(db, listing_id=999999))


# ----------------------------- Anzeigentarif setzen/anheben -----------------------------
def test_set_ad_rate_mock():
    r = asyncio.run(MockEbayClient().set_ad_rate(["800111"], bid_pct=0.15))
    assert r["rate_pct"] == 15.0


def test_market_analysis_endpoint(client, db):
    l = _listing_with_variants(db)
    resp = client.post(f"/api/v1/optimization/{l.id}/market-analysis")
    assert resp.status_code == 200
    j = resp.json()
    assert j["listing_id"] == l.id and len(j["variants"]) == 2


def test_promote_endpoint_accepts_rate(client, db, monkeypatch):
    # /promote konstruiert RealEbayClient direkt -> im Test durch Mock ersetzen.
    monkeypatch.setattr("app.integrations.ebay.RealEbayClient", lambda *a, **k: MockEbayClient())
    l = _listing_with_variants(db)
    resp = client.post(f"/api/v1/products/{l.id}/promote", json={"rate_pct": 14})
    assert resp.status_code == 200
    assert resp.json()["rate_pct"] == 14.0


# ----------------------------- Kandidaten: exakter Filter, 14-Tage-Hide, Alter, Daten -----------------------------
def _active_listing(db, *, clicks=0, title="T", start_days_ago=None, optimized_days_ago=None):
    from datetime import datetime, timedelta, timezone
    l = Listing(ebay_sku=f"AE-{title}", ebay_item_id=f"it{title}", title_seo=title,
                description="d", listing_status="active", price_eur=Decimal("19.95"),
                clicks_week=clicks, impressions_week=100)
    if start_days_ago is not None:
        l.listing_start_date = datetime.now(timezone.utc) - timedelta(days=start_days_ago)
    if optimized_days_ago is not None:
        l.optimized_at = datetime.now(timezone.utc) - timedelta(days=optimized_days_ago)
    db.add(l); db.commit()
    return l


def test_candidates_exact_clicks_filter(db):
    _active_listing(db, clicks=0, title="c0")
    _active_listing(db, clicks=1, title="c1")
    _active_listing(db, clicks=2, title="c2")
    r0 = O.list_candidates(db, clicks_exact=0)
    assert {c["clicks"] for c in r0["candidates"]} == {0}
    r1 = O.list_candidates(db, clicks_exact=1)
    assert [c["clicks"] for c in r1["candidates"]] == [1]


def test_candidates_hide_recently_optimized_14d(db):
    _active_listing(db, clicks=0, title="fresh", optimized_days_ago=3)      # ausgeblendet
    old = _active_listing(db, clicks=0, title="old", optimized_days_ago=20)  # wieder sichtbar
    never = _active_listing(db, clicks=0, title="never")                     # nie optimiert
    ids = {c["listing_id"] for c in O.list_candidates(db, clicks_exact=0)["candidates"]}
    assert old.id in ids and never.id in ids
    assert all(c["title"] != "fresh" for c in O.list_candidates(db, clicks_exact=0)["candidates"])


def test_candidates_sorted_oldest_first_and_dates(db):
    from app.models import Sale
    from datetime import datetime, timezone
    newer = _active_listing(db, clicks=0, title="new", start_days_ago=10)
    older = _active_listing(db, clicks=0, title="old", start_days_ago=200)
    # ein Verkauf am aelteren Listing
    db.add(Sale(ebay_transaction_id="S-OPT", listing_id=older.id, quantity=1,
                price_eur=Decimal("19.95"), status="delivered",
                sale_date=datetime(2026, 6, 1, tzinfo=timezone.utc)))
    db.commit()
    cands = O.list_candidates(db, clicks_exact=0)["candidates"]
    assert cands[0]["listing_id"] == older.id   # aeltestes zuerst
    older_row = next(c for c in cands if c["listing_id"] == older.id)
    newer_row = next(c for c in cands if c["listing_id"] == newer.id)
    assert older_row["last_sale_date"] is not None       # hatte Verkauf
    assert newer_row["last_sale_date"] is None           # nie verkauft -> UI zeigt 0
    assert older_row["online_since_real"] is True


# ----------------------------- Einstelldatum nachtragen -----------------------------
def test_backfill_start_dates(db):
    from app.services import ebay_import_service as IMP
    l = _active_listing(db, clicks=0, title="js")   # listing_start_date = None
    assert l.listing_start_date is None

    class _FakeEbay:
        async def get_active_listings(self, *, max_items=2000):
            return [{"item_id": l.ebay_item_id, "start_time": "2026-06-06T14:25:08.000Z"}]

    r = asyncio.run(IMP.backfill_start_dates(db, ebay=_FakeEbay()))
    assert r["updated"] == 1
    db.refresh(l)
    assert l.listing_start_date is not None and l.listing_start_date.year == 2026 \
        and l.listing_start_date.month == 6


# ----------------------------- optimize_one + apply-all -----------------------------
def test_optimize_one_returns_title_and_market(db, monkeypatch):
    monkeypatch.setattr(O, "_real_ebay", lambda: MockEbayClient())
    l = _listing_with_variants(db)
    r = asyncio.run(O.optimize_one(db, listing_id=l.id))
    assert "suggestion" in r and "variants" in r and r["competitor_price_range"]
    assert "recommended_ad_rate_pct" in r and "fee_pct" in r


def test_apply_optimization_sets_optimized_and_applies(db, monkeypatch):
    monkeypatch.setattr(O, "_real_ebay", lambda: MockEbayClient())
    # Entwurf: Titel + Preise werden lokal übernommen (kein eBay-Push nötig) – testet
    # den Apply-All-Pfad + das 14-Tage-Ausblenden sauber. (Live-Preis-Push/Anzeigen-
    # tarif sind separat abgedeckt: apply_variant_prices/set_ad_rate.)
    l = _listing_with_variants(db, live=False)
    l.clicks_week = 0
    db.commit()
    r = asyncio.run(O.apply_optimization(
        db, listing_id=l.id, title="HTC NE40 Neuer Titel Bluetooth Kopfhörer",
        prices={"AE-MK-V1": 21.10, "AE-MK-V2": 24.40}))
    assert "Titel" in r["applied"]
    assert any("Preis" in a for a in r["applied"])
    assert r["hidden_days"] == 14
    db.refresh(l)
    assert l.optimized_at is not None            # 14-Tage-Hide-Anker
    assert l.optimization_suggestion is None
    assert l.title_seo.startswith("HTC NE40 Neuer Titel")
    # danach aus der Kandidatenliste ausgeblendet
    ids = {c["listing_id"] for c in O.list_candidates(db, clicks_exact=0)["candidates"]}
    assert l.id not in ids
