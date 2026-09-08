"""Lokaler MCP-Server fuer eBay (stdio).

Start (aus dem Projekt-Root):
    python -m ebay_mcp.server

Konfiguration via Umgebungsvariablen (siehe ebay_mcp/.env.example):
    EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, EBAY_REFRESH_TOKEN,
    EBAY_MARKETPLACE_ID (Default EBAY_DE), EBAY_USE_SANDBOX (Default true)
Ohne Refresh-Token -> Mock-Modus.
"""
from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from ebay_mcp.client import EbayClient, EbayError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ebay_mcp")

mcp = FastMCP("ebay")
_client = EbayClient()


def _safe(fn, **kw) -> dict:
    try:
        return fn(**kw)
    except EbayError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def ebay_test_connection() -> dict:
    """Prueft die eBay-Verbindung/Konfiguration (Mock oder Live, Sandbox/Prod)."""
    return _safe(_client.test_connection)


@mcp.tool()
def ebay_get_orders(days: int = 7, limit: int = 50) -> dict:
    """Listet eBay-Bestellungen der letzten `days` Tage (Sell Fulfillment getOrders)."""
    return _safe(_client.get_orders, days=days, limit=limit)


@mcp.tool()
def ebay_get_order(order_id: str) -> dict:
    """Liefert eine einzelne eBay-Bestellung inkl. Lieferadresse/Positionen."""
    return _safe(_client.get_order, order_id=order_id)


@mcp.tool()
def ebay_get_listing_analytics(listing_id: str, days: int = 7) -> dict:
    """Traffic-Report eines Listings (Impressionen, Views, CTR).

    Hinweis: eBay liefert keine echten Klick-Zahlen; LISTING_VIEWS_TOTAL ist die
    naechste Entsprechung.
    """
    return _safe(_client.get_listing_analytics, listing_id=listing_id, days=days)


def main() -> None:
    logger.info("eBay MCP-Server startet (mock=%s, sandbox=%s, marketplace=%s)",
                _client.cfg.mock, _client.cfg.use_sandbox, _client.cfg.marketplace_id)
    mcp.run()


if __name__ == "__main__":
    main()
