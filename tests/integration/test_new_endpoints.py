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


def test_monitoring_sync_single_not_found(client):
    r = client.post("/api/v1/monitoring/sync/999999")
    assert r.status_code == 404


def test_health_exposes_engine(client):
    h = client.get("/health").json()
    assert h["fulfillment_engine"] in ("native", "autods")
    assert "sandbox" in h
