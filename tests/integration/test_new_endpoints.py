"""Integration-Tests fuer die neuen Tools: Pricing, Monitoring, Dashboard/Analytics."""
from __future__ import annotations


def _upload(client, slug):
    r = client.post("/api/v1/products/upload",
                    json={"aliexpress_url": f"https://de.aliexpress.com/item/{slug}.html"})
    assert r.status_code == 201, r.text
    return r.json()


def test_pricing_calculate_from_cost(client):
    r = client.post("/api/v1/pricing/calculate", json={"cost_eur": 100})
    assert r.status_code == 200
    b = r.json()
    assert b["rounded_price_eur"] >= b["price_eur"] > b["cost_eur"] > 0
    assert b["profit_eur"] > 0
    # Preisendung entspricht dem konfigurierten Cent-Wert
    assert round((b["rounded_price_eur"] * 100) % 100) == round(b["price_cents"] * 100)


def test_pricing_calculate_requires_input(client):
    r = client.post("/api/v1/pricing/calculate", json={})
    assert r.status_code == 400


def test_pricing_respects_overrides(client):
    low = client.post("/api/v1/pricing/calculate",
                      json={"cost_eur": 50, "profit_pct": 0.1, "min_profit_eur": 0}).json()
    high = client.post("/api/v1/pricing/calculate",
                       json={"cost_eur": 50, "profit_pct": 0.8, "min_profit_eur": 0}).json()
    assert high["rounded_price_eur"] > low["rounded_price_eur"]


def test_upload_sets_price_and_native_backend(client):
    data = _upload(client, "pricetest")
    assert data["backend"] == "native"
    assert data["price_eur"] and data["price_eur"] > 0
    assert data["profit_eur"] is not None


def test_monitoring_status_and_sync(client):
    _upload(client, "monitor1")
    st = client.get("/api/v1/monitoring/status")
    assert st.status_code == 200
    assert st.json()["stats"]["monitored"] >= 1

    sync = client.post("/api/v1/monitoring/sync")
    assert sync.status_code == 200
    assert sync.json()["checked"] >= 1


def test_monitoring_sync_single_not_found(client):
    r = client.post("/api/v1/monitoring/sync/999999")
    assert r.status_code == 404


def test_dashboard_summary_shape(client):
    _upload(client, "summary1")
    r = client.get("/api/v1/dashboard/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["listings"]["total"] >= 1
    assert "revenue_eur" in body["sales"]
    assert "pending" in body["tasks"]


def test_summary_zaehlt_gescheiterte_uploads(client, db):
    """publish_errors zaehlt Entwuerfe, deren Upload zu eBay hart scheiterte.

    Die Startseite baut darauf ihre dringendste Kachel. Ohne diese Zahl liegt so
    ein Entwurf unsichtbar in der Liste - er sieht aus wie jeder andere, obwohl
    er auf eine Entscheidung wartet.

    Gezaehlt werden nur ENTWUERFE: bei einem bereits aktiven Listing ist ein
    alter Fehlertext Vergangenheit, kein offener Punkt.
    """
    from app.models import Listing

    _upload(client, "fehler1")
    leer = client.get("/api/v1/dashboard/summary").json()
    assert leer["listings"]["publish_errors"] == 0

    entwurf = db.query(Listing).filter(Listing.listing_status == "draft").first()
    assert entwurf is not None, "Der Upload haette einen Entwurf anlegen muessen"
    entwurf.publish_error = "eBay 25002: Kategorie ungueltig"
    db.commit()

    mit = client.get("/api/v1/dashboard/summary").json()
    assert mit["listings"]["publish_errors"] == 1

    # Live gegangen -> der alte Fehler zaehlt nicht mehr als offener Punkt.
    entwurf.listing_status = "active"
    db.commit()
    danach = client.get("/api/v1/dashboard/summary").json()
    assert danach["listings"]["publish_errors"] == 0


def test_dashboard_profit_series_length(client):
    r = client.get("/api/v1/dashboard/profit?days=14")
    assert r.status_code == 200
    body = r.json()
    assert body["days"] == 14
    assert len(body["series"]) == 14


def test_dashboard_orders_and_activity(client):
    _upload(client, "orders1")
    assert client.get("/api/v1/dashboard/orders").status_code == 200
    act = client.get("/api/v1/dashboard/activity")
    assert act.status_code == 200
    # Upload erzeugt einen Task-Log-Eintrag
    assert len(act.json()["activity"]) >= 1


def test_health_exposes_engine(client):
    h = client.get("/health").json()
    assert h["fulfillment_engine"] in ("native", "autods")
    assert "sandbox" in h


def test_finalize_without_draft_is_rejected(client):
    # skip_autods=True -> kein eBay-Draft -> finalize muss sauber scheitern (kein Fake-Offer).
    up = client.post("/api/v1/products/upload",
                     json={"aliexpress_url": "https://de.aliexpress.com/item/nodraft.html",
                           "skip_autods": True})
    assert up.status_code == 201
    assert up.json()["ebay_draft_id"] is None
    lid = up.json()["listing_id"]
    fin = client.post(f"/api/v1/products/finalize/{lid}", json={"approve": True})
    assert fin.status_code == 404  # PersistentError -> kein Live-Stellen ohne Offer


def test_variant_map_learn_and_suggestion(client, db, monkeypatch):
    """Manuelle Varianten-Zuordnung (09.08.): lernen + gruener Vorschlag aus der Quelle."""
    import json as _j
    from types import SimpleNamespace
    from sqlalchemy.orm.attributes import flag_modified
    data = _upload(client, "vmap1")
    lid = data["listing_id"]
    from app.models import Listing, Product
    listing = db.get(Listing, lid)
    product = db.get(Product, listing.product_id)
    # Mock-Scrape liefert das Alt-Format ohne skus -> echte SKU-Struktur seeden.
    skus = [{"attr": "14:1#Rot", "options": {"Farbe": "Rot"}, "price": "5", "stock": 9},
            {"attr": "14:2#Blau", "options": {"Farbe": "Blau"}, "price": "5", "stock": 9}]
    product.variants = {"axes": {"Farbe": ["Rot", "Blau"]}, "skus": skus}
    flag_modified(product, "variants")
    db.commit()
    from app.services import supplier_service

    async def fake_scrape(url):
        return SimpleNamespace(variants={"skus": skus}, in_stock=True, price_cny="5",
                               aliexpress_id=str(product.aliexpress_id))
    monkeypatch.setattr(supplier_service, "_real_ae",
                        lambda: SimpleNamespace(scrape_product=fake_scrape))
    sku = skus[0]
    sel = dict(sku["options"])

    r = client.post(f"/api/v1/products/{lid}/variant-map",
                    json={"selection": sel, "attr": sku["attr"]})
    assert r.status_code == 200, r.text
    db.refresh(listing)
    assert sku["attr"] in (listing.variant_map or {}).values(), "Zuordnung muss gelernt sein"

    r2 = client.post(f"/api/v1/products/{lid}/variant-map",
                     json={"selection": sel, "attr": "gibtsnicht"})
    assert r2.status_code == 409, "unbekannte Quell-Variante darf nicht lernbar sein"

    r3 = client.get(f"/api/v1/products/{lid}/sources/{product.aliexpress_id}/variants",
                    params={"sel": _j.dumps(sel)})
    assert r3.status_code == 200, r3.text
    assert r3.json().get("suggested_attr") == sku["attr"], \
        "gelernte/aufgeloeste Zuordnung muss als gruener Vorschlag zurueckkommen"


def test_self_stock_triggers_fast_policy_result(client, db):
    """Regression: Live-Listing + Eigenbestand muss ein fast_policy-Ergebnis melden.

    Der fehlende Listing-Import im Endpunkt fuehrte zu NameError ("not defined"),
    der im Best-Effort-Catch als note verschluckt wurde — Umstellung lief nie."""
    data = _upload(client, "selfstock1")
    lid = data["listing_id"]
    from app.models import Listing
    listing = db.get(Listing, lid)
    listing.ebay_item_id = "110123456789"
    db.commit()

    r = client.post(f"/api/v1/products/{lid}/self-stock",
                    json={"sku_attr": "977:1#One", "qty": 3, "cost_eur": 2.0})
    assert r.status_code == 200, r.text
    fp = r.json().get("fast_policy")
    assert fp is not None, "fast_policy fehlt in der Antwort"
    assert "not defined" not in (fp.get("note") or ""), fp
