"""Echte Produktfotos ueber Dynamic Mockups (dynamicmockups.com).

Wozu: Die gezeichneten Umrisse aus ``ebay_weg`` taugen als Notbehelf, verkaufen
aber schlecht. Dynamic Mockups legt ein Motiv auf fotografierte Vorlagen - Ware
an Mann und Frau, flach von vorne - und faerbt die Ware um, ohne Licht und
Falten zu verlieren.

Angebunden an die REST-Schnittstelle (Doku abgerufen 13.09.2026):

* Basis ``https://app.dynamicmockups.com/api/v1``, Anmeldung ``x-api-key``.
* ``GET /mockups`` listet Vorlagen samt Smart Objects (Ebenen, in die ein Bild
  oder eine Farbe kommt).
* ``POST /renders`` rendert EIN Bild. Das Motiv geht als **Datei** mit
  (FormData, ``smart_objects[i][asset][file]``) - so muss es nirgends oeffentlich
  liegen. Die Farbe der Ware setzt man an einem EIGENEN Smart Object
  (``smart_objects[i][color]``). Das ``color``-Feld am Motiv-Objekt wuerde
  dagegen das Motiv einfaerben, nicht das Shirt.

Kosten: 1 gerendertes Bild = 1 Credit; fehlgeschlagene Aufrufe kosten nichts.
Im kostenlosen Tarif tragen Schnittstellen-Bilder ein Wasserzeichen. Deshalb
rendert der Aufrufer nie doppelt: ``bild_im_zwischenspeicher`` prueft zuerst.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("app.integrations.dynamic_mockups")

BASIS_URL = "https://app.dynamicmockups.com/api/v1"


class MockupFehler(RuntimeError):
    """Dynamic Mockups hat nicht geliefert."""


def _meldung(antwort: httpx.Response) -> str:
    try:
        daten = antwort.json()
    except ValueError:
        return antwort.text[:200]
    return str((daten or {}).get("message") or daten)[:200]


class DynamicMockupsClient:
    """Duenner, nebenlaeufiger Client - blockiert den Server nicht beim Warten."""

    def __init__(self, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 120.0) -> None:
        if not api_key:
            raise MockupFehler("DYNAMIC_MOCKUPS_API_KEY fehlt in der .env.")
        self._client = httpx.AsyncClient(
            base_url=BASIS_URL, timeout=timeout, transport=transport,
            headers={"x-api-key": api_key, "Accept": "application/json"})

    async def aclose(self) -> None:
        await self._client.aclose()

    def _pruefe(self, antwort: httpx.Response, was: str) -> dict[str, Any]:
        if antwort.status_code == 401:
            raise MockupFehler("Dynamic Mockups hat den Schluessel abgelehnt "
                               "(DYNAMIC_MOCKUPS_API_KEY pruefen).")
        if antwort.status_code >= 400:
            raise MockupFehler(f"{was} fehlgeschlagen: HTTP {antwort.status_code} {_meldung(antwort)}")
        daten = antwort.json() if antwort.content else {}
        if isinstance(daten, dict) and daten.get("success") is False:
            raise MockupFehler(f"{was} fehlgeschlagen: {daten.get('message')}")
        return daten if isinstance(daten, dict) else {}

    async def vorlagen(self, *, name: str | None = None, alle_kataloge: bool = True) -> list[dict]:
        """Vorlagen mit Smart Objects. ``alle_kataloge`` schliesst die fertigen Kataloge ein."""
        params: dict[str, str] = {}
        if alle_kataloge:
            params["include_all_catalogs"] = "true"
        if name:
            params["name"] = name
        daten = self._pruefe(await self._client.get("/mockups", params=params), "Vorlagen lesen")
        return list(daten.get("data") or [])

    async def rendere(self, *, mockup_uuid: str, motiv_objekt: str, motiv_datei: Path,
                      farb_objekt: str | None = None, farbe_hex: str | None = None,
                      breite: int = 1600, label: str = "") -> str:
        """Ein Bild rendern. Gibt die Adresse des fertigen Bildes zurueck."""
        datei = Path(motiv_datei)
        felder = {
            "mockup_uuid": mockup_uuid,
            "export_label": label,
            "export_options[image_format]": "jpg",
            "export_options[image_size]": str(int(breite)),
            "export_options[mode]": "view",
            "smart_objects[0][uuid]": motiv_objekt,
            "smart_objects[0][asset][fit]": "contain",
        }
        if farb_objekt and farbe_hex:
            felder["smart_objects[1][uuid]"] = farb_objekt
            felder["smart_objects[1][color]"] = farbe_hex
        dateien = {"smart_objects[0][asset][file]": (datei.name, datei.read_bytes(), "image/png")}
        daten = self._pruefe(await self._client.post("/renders", data=felder, files=dateien),
                             f"Rendern ({label or mockup_uuid})")
        adresse = ((daten.get("data") or {}).get("export_path")) or ""
        if not adresse:
            raise MockupFehler(f"Rendern ({label}) lieferte keine Bildadresse.")
        logger.info("Mockup gerendert", extra={"label": label, "mockup": mockup_uuid})
        return adresse

    async def lade_herunter(self, adresse: str, ziel: Path) -> Path:
        """Das fertige Bild lokal ablegen (ohne Anmeldekopf - fremder Speicher)."""
        ziel = Path(ziel)
        ziel.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=120.0, transport=self._client._transport) as roh:
            antwort = await roh.get(adresse)
        if antwort.status_code >= 400 or not antwort.content:
            raise MockupFehler(f"Bild nicht abrufbar: HTTP {antwort.status_code}")
        ziel.write_bytes(antwort.content)
        return ziel
