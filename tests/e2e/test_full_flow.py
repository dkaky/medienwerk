"""E2E: kompletter Flow Upload -> Finalize -> IPN -> Fulfill -> Invoice -> Optimize.

Laeuft komplett gegen die Mock-Integrationen (Spec Kap. 7.1 E2E).
"""
from __future__ import annotations


def test_full_flow(client):
    # 1) Produkt hochladen
    up = client.post(
        "/api/v1/products/upload",
        json={"aliexpress_url": "https://de.aliexpress.com/item/e2e-flow.html"},
    ).json()
    listing_id = up["listing_id"]

    # 2) Listing live stellen -> ebay_item_id
    fin = client.post(f"/api/v1/products/finalize/{listing_id}", json={"approve": True}).json()
    item_id = fin["ebay_item_id"]
    assert item_id

    # 3) eBay IPN simulieren (Verkauf)
    ipn = client.post(
        "/api/v1/sales/webhook/ebay-ipn",
        headers={"X-EBAY-SIG": "valid", "Content-Type": "application/json"},
        json={
            "transaction_id": "TX-E2E-1",
            "order_id": "ORD-E2E-1",
            "item_number": item_id,
            "buyer_name": "Max Mustermann",
            "buyer_email": "max@example.com",
            "delivery_address": {"street": "Hauptstr 1", "city": "Berlin",
                                  "postal": "10115", "country": "DE"},
            "quantity": 1,
        },
    )
    assert ipn.status_code == 200
    assert ipn.json()["status"] == "received"

    # Sale-ID ermitteln (Background-Fulfill kann schon gelaufen sein)
    from app.database import SessionLocal
    from app.models import Sale

    s = SessionLocal()
    try:
        sale = s.query(Sale).filter_by(ebay_transaction_id="TX-E2E-1").one()
        sale_id = sale.id
    finally:
        s.close()

    # 4) Fulfill explizit (idempotent gegenueber Background-Task)
    ful = client.post(f"/api/v1/orders/fulfill/{sale_id}", json={"approve_variant": True})
    assert ful.status_code in (202, 409)  # 409 falls Background bereits bestellt hat

    # Order-ID ermitteln
    from app.models import OrderAliexpress

    s = SessionLocal()
    try:
        order = s.query(OrderAliexpress).filter_by(sale_id=sale_id).one()
        order_id = order.id
    finally:
        s.close()

    # 5) Tracking abrufen
    trk = client.get(f"/api/v1/orders/{order_id}/tracking")
    assert trk.status_code == 200
    assert trk.json()["tracking_number"]

    # 6) Belege anhaengen
    inv = client.post(f"/api/v1/invoices/attach/{order_id}", json={})
    assert inv.status_code == 201, inv.text
    assert len(inv.json()["invoices"]) >= 1

    # 7) Woechentliche Optimierung (dry-run) muss durchlaufen
    opt = client.post("/api/v1/scheduler/optimize-listings-weekly", json={"dry_run": True})
    assert opt.status_code == 202
    assert opt.json()["listings_analyzed"] >= 1


def test_performance_endpoint(client):
    # Listing anlegen ueber Upload+Finalize, dann Performance abfragen
    up = client.post(
        "/api/v1/products/upload",
        json={"aliexpress_url": "https://de.aliexpress.com/item/perf.html"},
    ).json()
    client.post(f"/api/v1/products/finalize/{up['listing_id']}", json={"approve": True})

    r = client.get("/api/v1/listings/performance?status=active")
    assert r.status_code == 200
    body = r.json()
    assert body["stats"]["active"] >= 1
    assert isinstance(body["listings"], list)
