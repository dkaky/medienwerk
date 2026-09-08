"""Zugriff auf Printify.

Neu geschrieben statt kopiert: Das Vorprojekt arbeitet synchron, dieses Projekt
durchgehend nebenlaeufig. Ein Kopieren haette bedeutet, dass jeder Printify-
Aufruf den ganzen Server blockiert, waehrend er auf die Antwort wartet.

Die Schnittstellen-Pfade und die Wiederholungs-Semantik stammen aus dem
Vorprojekt (POD-Shop, src/pod/printify/client.py) - dort sind sie im Betrieb
erprobt.

Bewusst schmal gehalten: nur was Etappe 3 braucht. Bestellungen und Loeschen
kommen erst, wenn sie gebraucht werden - und geloescht wird bei Printify
ohnehin nie automatisch.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

logger = logging.getLogger("app.studio.printify")

BASE_URL = "https://api.printify.com/v1"

# Printify antwortet bei Ueberlast mit 429 und nennt im Kopf, wie lange zu warten
# ist. Ohne Beachtung dieses Wertes rennt man in eine Sperre.
_MAX_VERSUCHE = 3
_STANDARD_WARTE_S = 2.0


class PrintifyFehler(RuntimeError):
    """Ein Aufruf an Printify ist fehlgeschlagen."""


def _warte_sekunden(antwort: httpx.Response) -> float:
    kopf = antwort.headers.get("Retry-After")
    if kopf:
        try:
            return max(0.5, float(kopf))
        except ValueError:
            pass
    return _STANDARD_WARTE_S


class PrintifyClient:
    """Schmale Huelle um die Printify-Schnittstelle."""

    def __init__(self, token: str, shop_id: str | None = None, base_url: str = BASE_URL):
        if not token:
            raise ValueError("Kein Printify-Zugang gesetzt (PRINTIFY_TOKEN in der .env).")
        self._token = token
        self.shop_id = shop_id
        self._base = base_url.rstrip("/")

    # --- Lesen ---------------------------------------------------------------

    async def shops(self) -> list[dict[str, Any]]:
        """Alle Shops des Kontos - zum Pruefen, ob die Shop-Nummer stimmt."""
        return await self._anfrage("GET", "/shops.json")

    async def blueprints(self) -> list[dict[str, Any]]:
        """Produktvorlagen des Katalogs (T-Shirt, Hoodie, Tasse ...)."""
        return await self._anfrage("GET", "/catalog/blueprints.json")

    async def anbieter(self, blueprint_id: int) -> list[dict[str, Any]]:
        """Druckereien, die diese Vorlage anbieten."""
        return await self._anfrage(
            "GET", f"/catalog/blueprints/{blueprint_id}/print_providers.json"
        )

    async def varianten(self, blueprint_id: int, anbieter_id: int) -> dict[str, Any]:
        """Groessen und Farben samt Druckbereichen.

        Die Druckbereiche sind die Grundlage der Passform-Rechnung (``fit_scale``).
        """
        return await self._anfrage(
            "GET",
            f"/catalog/blueprints/{blueprint_id}/print_providers/{anbieter_id}/variants.json",
        )

    # --- Schreiben -----------------------------------------------------------

    async def bild_hochladen(self, dateiname: str, inhalt: bytes) -> dict[str, Any]:
        """Ein Motiv zu Printify hochladen. Liefert die Bild-Kennung."""
        return await self._anfrage(
            "POST",
            "/uploads/images.json",
            json={
                "file_name": dateiname,
                "contents": base64.b64encode(inhalt).decode("ascii"),
            },
        )

    async def produkt_anlegen(self, payload: dict[str, Any], shop_id: str | None = None) -> dict:
        """Produkt als ENTWURF anlegen - veroeffentlicht wird nichts.

        Propose-only: Was in den Verkauf geht, entscheidet der Mensch.
        """
        shop = shop_id or self.shop_id
        if not shop:
            raise ValueError("Keine Shop-Nummer gesetzt (PRINTIFY_SHOP_ID).")
        return await self._anfrage("POST", f"/shops/{shop}/products.json", json=payload)

    async def produkt(self, produkt_id: str, shop_id: str | None = None) -> dict[str, Any]:
        """Ein Produkt abrufen - unter anderem fuer die Vorschaubilder."""
        shop = shop_id or self.shop_id
        return await self._anfrage("GET", f"/shops/{shop}/products/{produkt_id}.json")

    # --- Grundlage -----------------------------------------------------------

    async def _anfrage(self, methode: str, pfad: str, **kwargs: Any) -> Any:
        """Aufruf mit Wiederholung bei Ueberlast.

        Wiederholt wird nur bei 429 und bei Serverfehlern - nicht bei 4xx, denn
        eine falsche Anfrage wird beim zweiten Mal nicht richtiger.
        """
        import asyncio

        kopf = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "User-Agent": "POD-Shop/1.0",
        }
        url = f"{self._base}{pfad}"

        letzter_fehler = ""
        async with httpx.AsyncClient(timeout=60.0) as client:
            for versuch in range(1, _MAX_VERSUCHE + 1):
                try:
                    antwort = await client.request(methode, url, headers=kopf, **kwargs)
                except httpx.HTTPError as exc:
                    letzter_fehler = f"Netzfehler: {exc}"
                    if versuch == _MAX_VERSUCHE:
                        break
                    await asyncio.sleep(_STANDARD_WARTE_S)
                    continue

                if antwort.status_code == 429 or antwort.status_code >= 500:
                    letzter_fehler = f"HTTP {antwort.status_code}: {antwort.text[:200]}"
                    if versuch == _MAX_VERSUCHE:
                        break
                    warte = _warte_sekunden(antwort)
                    logger.info("Printify %s, warte %.1fs (Versuch %s)", antwort.status_code, warte, versuch)
                    await asyncio.sleep(warte)
                    continue

                if antwort.status_code >= 400:
                    raise PrintifyFehler(
                        f"Printify {methode} {pfad} abgelehnt "
                        f"(HTTP {antwort.status_code}): {antwort.text[:300]}"
                    )
                return antwort.json() if antwort.content else {}

        raise PrintifyFehler(f"Printify {methode} {pfad} nach {_MAX_VERSUCHE} Versuchen: {letzter_fehler}")
