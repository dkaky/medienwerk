"""Integration-Tests: Endpoints ueber den TestClient (mit Mock-Integrationen)."""
from __future__ import annotations


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db_connected"] is True
    assert body["mocks_enabled"] is True


def test_invoice_search_empty(client):
    r = client.post("/api/v1/invoices/search", json={"type": "all"})
    assert r.status_code == 200
    assert r.json()["results"] == []


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Medienwerk" in r.text  # Marker aus dem Dashboard


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
