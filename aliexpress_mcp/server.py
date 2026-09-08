"""Lokaler MCP-Server fuer AliExpress (stdio).

Start (aus dem Projekt-Root):
    python -m aliexpress_mcp.server

Konfiguration via Umgebungsvariablen (siehe aliexpress_mcp/.env.example):
    ALIEXPRESS_APP_KEY, ALIEXPRESS_APP_SECRET, ALIEXPRESS_ACCESS_TOKEN (optional),
    ALIEXPRESS_BASE_URL (Default api-sg.aliexpress.com/sync)
Ohne App-Key/Secret -> Mock-Modus.
"""
from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from aliexpress_mcp.client import AliExpressClient, AliExpressError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aliexpress_mcp")

mcp = FastMCP("aliexpress")
_client = AliExpressClient()


def _safe(fn, **kw) -> dict:
    try:
        return fn(**kw)
    except AliExpressError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def aliexpress_test_connection() -> dict:
    """Prueft die AliExpress-Verbindung/Konfiguration (Mock oder Live)."""
    return _safe(_client.test_connection)


@mcp.tool()
def aliexpress_get_product(product_id: str, ship_to: str = "DE", currency: str = "EUR") -> dict:
    """Ruft Produktdaten zu einer AliExpress-Produkt-ID ab (Titel, Preis, Varianten)."""
    return _safe(_client.get_product, product_id=product_id, ship_to=ship_to, currency=currency)


@mcp.tool()
def aliexpress_search(keywords: str, limit: int = 10, ship_to: str = "DE") -> dict:
    """Sucht Produkte auf AliExpress nach Stichworten."""
    return _safe(_client.search, keywords=keywords, limit=limit, ship_to=ship_to)


def main() -> None:
    logger.info("AliExpress MCP-Server startet (mock=%s, base=%s)",
                _client.cfg.mock, _client.cfg.base_url)
    mcp.run()


if __name__ == "__main__":
    main()
