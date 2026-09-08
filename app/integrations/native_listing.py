"""Native Listing-Engine – vollwertiger AutoDS-Ersatz (Spec-Erweiterung).

AutoDS uebernimmt im Dropshipping vier Dinge: (1) Produkt-Import als eBay-Draft,
(2) Listing-Sync, (3) Repricing, (4) Bestands-Sync. Diese Engine bildet (1)+(2)
direkt ueber die eBay Sell Inventory API ab – ohne AutoDS-Abo. Repricing/Bestand
(3)+(4) liegen im `monitoring_service`, der dieselben eBay-Calls nutzt.

`get_listing_client()` (in integrations/__init__) liefert je nach
`FULFILLMENT_ENGINE` entweder die native Engine (Default) oder einen AutoDS-Adapter
– beide implementieren `ListingBackend`, sodass der Produkt-Service backend-agnostisch ist.
"""
from __future__ import annotations

import abc
import hashlib
from dataclasses import dataclass
from typing import Optional


@dataclass
class DraftResult:
    """Backend-neutrales Ergebnis eines Draft-Imports."""

    ebay_draft_id: Optional[str]   # native: offerId | autods: AutoDS-Draft-ID
    backend: str                   # "native" | "autods"
    sku: Optional[str] = None
    price_eur: Optional[float] = None
    cost_eur: Optional[float] = None
    status: str = "draft_created"


def make_sku(aliexpress_id: str | None, aliexpress_url: str) -> str:
    """Stabile, kurze SKU. Bevorzugt die AliExpress-ID, sonst URL-Hash."""
    if aliexpress_id:
        return f"AE-{aliexpress_id}"
    return "AE-" + hashlib.sha256(aliexpress_url.encode()).hexdigest()[:12]


class ListingBackend(abc.ABC):
    """Gemeinsames Interface fuer Listing-Erstellung/-Sync (native oder AutoDS)."""

    name: str = "abstract"

    @abc.abstractmethod
    async def create_draft(self, *, sku: str, aliexpress_url: str = "", title_seo: str,
                           description: str, image_urls: list[str], category_id: str | None,
                           price_eur: float, quantity: int,
                           cost_eur: float | None = None,
                           aspects: dict | None = None) -> DraftResult:
        ...

    @abc.abstractmethod
    async def sync_listing(self, *, sku: str, offer_id: str | None, title: str | None,
                           description: str | None, price_eur: float | None,
                           quantity: int | None) -> None:
        ...


class NativeListingBackend(ListingBackend):
    """AutoDS-frei: createOrReplaceInventoryItem + createOffer direkt auf eBay."""

    name = "native"

    def __init__(self, ebay_client) -> None:
        self.ebay = ebay_client

    async def create_draft(self, *, sku, aliexpress_url="", title_seo, description, image_urls,
                           category_id, price_eur, quantity, cost_eur=None, aspects=None):
        # Ohne Kategorie ging hier frueher die "0" an eBay - eine Nummer, die es
        # nicht gibt. eBay antwortete mit Fehler 25002, der Aufrufer verbuchte das
        # als blosse Warnung, und der Entwurf entstand nie. Deshalb wird die
        # Kategorie jetzt VOR dem Anlegen ermittelt (eBays eigener Vorschlag zum
        # Titel) und bei Misserfolg klar gemeldet statt still verschluckt.
        if not category_id:
            # getattr statt Direktaufruf: nicht jeder Client kann Kategorien
            # vorschlagen. Fehlt die Faehigkeit, soll die Meldung unten greifen -
            # nicht ein AttributeError, der wie ein Programmfehler aussieht.
            vorschlagen = getattr(self.ebay, "suggest_category", None)
            if vorschlagen is not None:
                category_id = await vorschlagen(title_seo)
        if not category_id:
            raise ValueError(
                "Keine eBay-Kategorie zum Titel gefunden. Ohne Kategorie lehnt eBay "
                "das Angebot ab (Fehler 25002). Titel praezisieren oder Kategorie "
                "am Listing hinterlegen."
            )

        await self.ebay.create_inventory_item(
            sku, title=title_seo, description=description,
            image_urls=image_urls or [], quantity=quantity, aspects=aspects or {},
        )
        try:
            offer_id = await self.ebay.create_offer(
                sku, price_eur=price_eur, category_id=category_id, quantity=quantity,
                listing_description=description,
            )
        except Exception:
            # Rollback: verwaistes Inventory-Item entfernen, sonst SKU-Konflikt beim Retry.
            try:
                await self.ebay.delete_inventory_item(sku)
            except Exception:  # noqa: BLE001 – Best-Effort-Cleanup, Originalfehler zaehlt
                pass
            raise
        return DraftResult(
            ebay_draft_id=offer_id, backend=self.name, sku=sku,
            price_eur=price_eur, cost_eur=cost_eur,
        )

    async def sync_listing(self, *, sku, offer_id, title, description, price_eur, quantity):
        # Hinweis: eBays Inventory-API aktualisiert keine Beschreibung via update_inventory
        # (nur Titel/Kategorie/Preis/Menge) – `description` wird hier bewusst nicht genutzt.
        await self.ebay.update_inventory(
            sku, title=title, price_eur=price_eur, quantity=quantity,
        )


class AutoDSBackend(ListingBackend):
    """Adapter auf den bestehenden AutoDSClient (Mock/Real), gleiche Schnittstelle."""

    name = "autods"

    def __init__(self, autods_client) -> None:
        self.autods = autods_client

    async def create_draft(self, *, sku, aliexpress_url="", title_seo, description, image_urls,
                           category_id, price_eur, quantity, cost_eur=None, aspects=None):
        draft = await self.autods.import_aliexpress(
            aliexpress_url=aliexpress_url or sku,  # echte AliExpress-URL fuer den AutoDS-Import
            # AutoDS bekommt weiterhin die "0", wenn nichts gesetzt ist: dort ist es
            # der hauseigene Platzhalter, nicht der eBay-Fehler von oben. Der reale
            # AutoDS-Client ist ohnehin ein Stummel (NotImplementedError) - hier wird
            # bewusst nichts umgebaut, was niemand aufruft.
            title_seo=title_seo, description=description, category_id=category_id or "0",
        )
        return DraftResult(
            ebay_draft_id=draft.ebay_draft_id, backend=self.name, sku=sku,
            price_eur=price_eur, cost_eur=cost_eur, status=draft.status,
        )

    async def sync_listing(self, *, sku, offer_id, title, description, price_eur, quantity):
        await self.autods.sync_listing(
            offer_id or sku, title=title or "", description=description or "",
        )
