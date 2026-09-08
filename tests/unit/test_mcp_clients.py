"""Unit-Tests fuer die eBay-/AliExpress-MCP-Clients (Mock-Modus, netzwerkfrei).

Beide Clients haengen nur von httpx ab (kein MCP-SDK), daher in der Haupt-venv
testbar. Die Server selbst (server.py) laufen in der gemeinsamen .venv-mcp.
"""
from __future__ import annotations

from aliexpress_mcp.client import AliExpressClient, AliExpressConfig
from ebay_mcp.client import EbayClient, EbayConfig


# ----------------------------------------------------------------- eBay
def test_ebay_mock_without_creds(monkeypatch):
    for v in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_REFRESH_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    assert EbayConfig.from_env().mock is True


def test_ebay_live_with_creds():
    cfg = EbayConfig(client_id="a", client_secret="b", refresh_token="c")
    assert cfg.mock is False
    assert cfg.host == "https://api.sandbox.ebay.com"  # use_sandbox Default True


def test_ebay_get_orders_mock():
    r = EbayClient(EbayConfig()).get_orders()
    assert r["mode"] == "mock"
    assert r["orders"] and "orderId" in r["orders"][0]


def test_ebay_analytics_mock():
    r = EbayClient(EbayConfig()).get_listing_analytics("123")
    assert r["impressions"] >= 0 and "views" in r


def test_ebay_test_connection_mock():
    assert EbayClient(EbayConfig()).test_connection()["ok"] is True


# ----------------------------------------------------------------- AliExpress
def test_ali_mock_without_keys(monkeypatch):
    for v in ("ALIEXPRESS_APP_KEY", "ALIEXPRESS_APP_SECRET"):
        monkeypatch.delenv(v, raising=False)
    assert AliExpressConfig.from_env().mock is True


def test_ali_live_with_keys():
    assert AliExpressConfig(app_key="k", app_secret="s").mock is False


def test_ali_get_product_mock():
    r = AliExpressClient(AliExpressConfig()).get_product("1005006150132971")
    assert r["mode"] == "mock"
    assert r["title"] and "priceEur" in r


def test_ali_search_mock():
    r = AliExpressClient(AliExpressConfig()).search("fahrradschloss", limit=3)
    assert 1 <= len(r["results"]) <= 3


def test_ali_sign_deterministic_uppercase_hex():
    c = AliExpressClient(AliExpressConfig(app_key="k", app_secret="topsecret"))
    s1 = c._sign({"b": "2", "a": "1", "method": "x"})
    s2 = c._sign({"a": "1", "method": "x", "b": "2"})  # Reihenfolge egal -> sortiert
    assert s1 == s2
    assert len(s1) == 64 and s1 == s1.upper()
