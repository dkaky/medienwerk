"""Externe Integrationen (Spec Kap. 4).

Alle Clients liegen hinter abstrakten Interfaces. `USE_MOCKS=true` (Default)
liefert deterministische Mock-Clients, die ohne Credentials und ohne externe
Dienste laufen. Mit echten Keys + `USE_MOCKS=false` werden die Real*-Clients
verwendet (deren API-Aufrufe sind als TODO markiert).

Die Factory-Funktionen sind der einzige Einstiegspunkt – Services importieren
nie eine konkrete Implementierung direkt.
"""
from __future__ import annotations

from functools import lru_cache

from app.config import get_settings

from .aliexpress import AliExpressClient, MockAliExpressClient, RealAliExpressClient
from .autods import AutoDSClient, MockAutoDSClient, RealAutoDSClient
from .ebay import EbayClient, MockEbayClient, RealEbayClient
from .llm import LLMClient, MockLLMClient, RealLLMClient
from .native_listing import AutoDSBackend, ListingBackend, NativeListingBackend
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


@lru_cache
def get_autods_client() -> AutoDSClient:
    s = get_settings()
    return MockAutoDSClient() if s.use_mock("autods") else RealAutoDSClient(s)


def get_listing_client() -> ListingBackend:
    """Listing-Backend nach FULFILLMENT_ENGINE: 'native' (Default, kein AutoDS) | 'autods'.

    Die native Engine publiziert AliExpress->eBay direkt ueber die Sell Inventory API
    und macht ein AutoDS-Abo damit ueberfluessig. Bewusst NICHT @lru_cache-gecacht: die
    zugrundeliegenden Clients sind selbst gecacht, und so greift ein Engine-Wechsel sofort.
    """
    s = get_settings()
    if s.fulfillment_engine.lower() == "autods":
        return AutoDSBackend(get_autods_client())
    return NativeListingBackend(get_ebay_client())


@lru_cache
def get_aliexpress_client() -> AliExpressClient:
    s = get_settings()
    return MockAliExpressClient() if s.use_mock("aliexpress") else RealAliExpressClient(s)


@lru_cache
def get_llm_client() -> LLMClient:
    s = get_settings()
    return MockLLMClient() if s.use_mock("llm") else RealLLMClient(s)


@lru_cache
def get_storage() -> InvoiceStorage:
    s = get_settings()
    # Aktuell nur lokale Ablage implementiert; gdrive/lexoffice als TODO.
    return LocalInvoiceStorage(s.invoice_path)


__all__ = [
    "get_ebay_client",
    "get_autods_client",
    "get_listing_client",
    "get_aliexpress_client",
    "get_llm_client",
    "get_storage",
    "EbayClient",
    "AutoDSClient",
    "ListingBackend",
    "AliExpressClient",
    "LLMClient",
    "InvoiceStorage",
]
