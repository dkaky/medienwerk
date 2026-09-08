"""Externe Integrationen.

Alle Clients liegen hinter abstrakten Interfaces. `USE_MOCKS=true` (Default)
liefert deterministische Mock-Clients, die ohne Credentials und ohne externe
Dienste laufen. Mit echten Keys + `USE_MOCKS=false` werden die Real*-Clients
verwendet.

Die Factory-Funktionen sind der einzige Einstiegspunkt - Services importieren
nie eine konkrete Implementierung direkt.

**Am 08.09.2026 sind AliExpress und AutoDS hier ausgezogen.** Medienwerk
verkauft eigene Motive, keine Handelsware; ein Lieferantenclient hat damit
keinen Zweck mehr. eBay bleibt - nicht als Bezugsquelle, sondern als
VERKAUFSKANAL fuer die eigenen Print-on-Demand-Produkte.
"""
from __future__ import annotations

from functools import lru_cache

from app.config import get_settings

from .ebay import EbayClient, MockEbayClient, RealEbayClient
from .llm import LLMClient, MockLLMClient, RealLLMClient
from .native_listing import ListingBackend, NativeListingBackend
from .storage import InvoiceStorage, LocalInvoiceStorage


def _ensure_safe_mock_usage(s) -> None:
    """Fail-fast: in Produktion darf der Mock nicht laufen.

    Der MockEbayClient akzeptiert jede nicht-leere Signatur (bewusst, fuer Dev/Tests).
    Auf einem oeffentlichen Webhook in Produktion waere die Signaturpruefung damit
    effektiv aus -> harter Startfehler statt stiller Fehlkonfiguration.
    """
    if s.is_production and s.use_mock("ebay"):
        raise RuntimeError(
            "Mock-eBay ist in Produktion (APP_ENV=production) nicht erlaubt – "
            "die Mock-Signaturpruefung akzeptiert jede Signatur."
        )


@lru_cache
def get_ebay_client() -> EbayClient:
    s = get_settings()
    _ensure_safe_mock_usage(s)
    return MockEbayClient() if s.use_mock("ebay") else RealEbayClient(s)


def get_listing_client() -> ListingBackend:
    """Listing-Backend fuer eBay.

    Frueher stand hier eine Weiche zwischen der nativen Engine und AutoDS. AutoDS
    ist ein Dropshipping-Dienst und mit dem Handelsteil ausgezogen; geblieben ist
    der Weg, der ohnehin der Standard war: direkt ueber die Sell Inventory API.

    Bewusst NICHT ``@lru_cache``-gecacht - der zugrundeliegende Client ist es
    selbst, und so wirkt ein Wechsel des Betriebsmodus sofort.
    """
    return NativeListingBackend(get_ebay_client())


@lru_cache
def get_llm_client() -> LLMClient:
    s = get_settings()
    return MockLLMClient() if s.use_mock("llm") else RealLLMClient(s)


@lru_cache
def get_storage() -> InvoiceStorage:
    s = get_settings()
    return LocalInvoiceStorage(s.invoice_path)


__all__ = [
    "get_ebay_client",
    "get_listing_client",
    "get_llm_client",
    "get_storage",
    "EbayClient",
    "ListingBackend",
    "LLMClient",
    "InvoiceStorage",
]
