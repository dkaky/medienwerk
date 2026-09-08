"""Integration-Tests: Endpoints ueber den TestClient (mit Mock-Integrationen)."""
from __future__ import annotations


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db_connected"] is True
    assert body["mocks_enabled"] is True


def test_upload_and_finalize(client):
    # Upload
    r = client.post(
        "/api/v1/products/upload",
        json={"aliexpress_url": "https://de.aliexpress.com/item/abc123.html"},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["status"] == "draft_created"
    assert data["ebay_draft_id"]
    assert len(data["title_seo"]) <= 80
    listing_id = data["listing_id"]

    # Finalize -> live
    r2 = client.post(f"/api/v1/products/finalize/{listing_id}", json={"approve": True})
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "active"
    assert r2.json()["ebay_item_id"]


def test_upload_rejects_non_aliexpress_url(client):
    r = client.post("/api/v1/products/upload", json={"aliexpress_url": "https://amazon.de/x"})
    assert r.status_code == 400


def test_reupload_draft_refreshes_in_place(client):
    """Erneuter Import eines Entwurfs aktualisiert ihn IN PLACE (korrigierte Daten), kein 2. Produkt."""
    url = "https://de.aliexpress.com/item/dup.html"
    r1 = client.post("/api/v1/products/upload", json={"aliexpress_url": url})
    assert r1.status_code == 201
    r2 = client.post("/api/v1/products/upload", json={"aliexpress_url": url})
    assert r2.status_code == 201, r2.text
    assert r2.json()["product_id"] == r1.json()["product_id"]     # kein Duplikat
    assert any("bereits als Entwurf" in w for w in r2.json()["warnings"])


def test_reupload_published_rejected(client):
    """Nach dem Veröffentlichen wird ein erneuter Import abgelehnt (Live-Listing nicht anfassen).

    409 statt 400: das Dashboard unterscheidet die Duplikat-Sperre daran von allen
    anderen Eingabefehlern und bietet dafür den „Trotzdem hochladen"-Knopf an.
    """
    url = "https://de.aliexpress.com/item/pub.html"
    lid = client.post("/api/v1/products/upload", json={"aliexpress_url": url}).json()["listing_id"]
    client.post(f"/api/v1/products/finalize/{lid}", json={"approve": True})
    r = client.post("/api/v1/products/upload", json={"aliexpress_url": url})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "duplikat"


def test_invoice_search_empty(client):
    r = client.post("/api/v1/invoices/search", json={"type": "all"})
    assert r.status_code == 200
    assert r.json()["results"] == []


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "POD Shop" in r.text  # Marker aus dem Dashboard


def test_api_info(client):
    r = client.get("/api")
    assert r.status_code == 200
    body = r.json()
    assert body["dashboard"] == "/"
    assert body["docs"] == "/docs"


def test_ebay_deletion_challenge(client):
    """GET-Challenge gibt SHA256(code+token+endpointURL) als HEX zurueck (eBay-Pflicht)."""
    import hashlib

    from app.config import get_settings

    s = get_settings()
    code = "abc123challenge"
    r = client.get("/ebay/marketplace-account-deletion", params={"challenge_code": code})
    assert r.status_code == 200
    expected = hashlib.sha256(
        (code + s.ebay_ipn_verification_token + s.ebay_deletion_endpoint_url).encode()
    ).hexdigest()
    assert r.json()["challengeResponse"] == expected
    assert len(expected) == 64


def test_ebay_deletion_notification_acks(client):
    r = client.post("/ebay/marketplace-account-deletion", json={"notification": {"data": {}}})
    assert r.status_code == 200
    assert r.json()["status"] == "acknowledged"


def test_ebay_deletion_notification_anonymizes_buyer(client, db):
    """POST-Notification anonymisiert die PII des betroffenen Kaeufers (DSGVO)."""
    from app.models import Sale

    sale = Sale(
        ebay_transaction_id="tx-del-1",
        buyer_name="loeschnutzer",
        buyer_email="del@example.com",
        delivery_address={"street": "Weg 1", "city": "Koeln"},
        price_eur=29,
        status="delivered",
    )
    db.add(sale)
    db.commit()
    sale_id = sale.id

    r = client.post(
        "/ebay/marketplace-account-deletion",
        json={"notification": {"data": {"username": "loeschnutzer", "userId": "U9"}}},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "acknowledged"

    db.expire_all()
    refreshed = db.get(Sale, sale_id)
    assert refreshed.buyer_email is None
    assert refreshed.delivery_address is None
    assert refreshed.buyer_name.startswith("[geloescht")
    # Steuerlich relevante Felder bleiben.
    assert refreshed.ebay_transaction_id == "tx-del-1"
    assert float(refreshed.price_eur) == 29.0


def test_quelle_einpflegen_hebt_ausverkauft_stempel_auf(client, db, monkeypatch):
    """Regression: eine frisch eingepflegte, lieferbare Quelle muss den Artikel SOFORT
    aus der Cockpit-Kachel „Ausverkauft o. Alt." nehmen.

    Vorher blieb er stehen: `add_source` hat den Stempel `monitor_status='out_of_stock'`
    nicht angefasst (nur das 6h-Monitoring setzt ihn zurueck), und der Report uebernimmt
    den Stempel beim Rebuild aus der DB — der Artikel klebte also trotz frischer Quelle
    in der Liste.
    """
    from decimal import Decimal
    from unittest.mock import AsyncMock, MagicMock

    from app.integrations.aliexpress import ScrapedProduct
    from app.models import Listing, Product
    from app.services import listing_match_service, supplier_service

    # Ausgangslage: aktives Listing, Quelle tot, Ausverkauft-Stempel gesetzt.
    p = Product(aliexpress_url="https://de.aliexpress.com/item/900001.html",
                aliexpress_id="900001", title_raw="Tote Quelle", price_cny=Decimal("6.00"))
    db.add(p)
    db.flush()
    listing = Listing(title_seo="Ausverkauft-Test", description="x", listing_status="active",
                      price_eur=Decimal("19.99"), product_id=p.id,
                      monitor_status="out_of_stock", supplier_in_stock=False,
                      quantity_available=0)
    db.add(listing)
    db.commit()
    lid = listing.id

    # add_source erzwingt den ECHTEN AE-Client -> stubben (neue Quelle ist lieferbar).
    stub = MagicMock()
    stub.scrape_product = AsyncMock(return_value=ScrapedProduct(
        aliexpress_id="900002", title_raw="Frische Quelle", description_raw="",
        price_cny=Decimal("7.00"), images=["https://img/900002.jpg"], variants={},
        supplier_id="s1", supplier_rating=Decimal("4.8"), in_stock=True))
    monkeypatch.setattr(supplier_service, "_real_ae", lambda: stub)

    r = client.post(f"/api/v1/products/{lid}/sources",
                    json={"url": "https://de.aliexpress.com/item/900002.html", "primary": True})
    assert r.status_code == 200, r.text

    # Kern der Regression: der Stempel muss weg sein ...
    db.expire_all()
    assert db.get(Listing, lid).monitor_status != "out_of_stock"

    # ... und die Cockpit-Zeile darf nicht mehr als ausverkauft gelten.
    row = next((x for x in listing_match_service.reprice_report(db)["rows"]
                if x.get("listing_id") == lid), None)
    assert row is not None
    assert row["monitor_oos"] is False
    assert row["fully_out"] is False
    assert r.json().get("report_refreshed") is True
