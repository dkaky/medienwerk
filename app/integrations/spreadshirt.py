"""Zugriff auf Spreadshirt (Public Shop API).

Spreadshirt ist hier ein zweiter VERKAUFSKANAL, keine zweite Druckerei. Die
oeffentliche Schnittstelle liest Shop und Artikel und baut Warenkoerbe - Motive
hochladen und daraus Produkte anlegen kann sie nicht. Das geht nur ueber die
Partner-Schnittstelle, fuer die Spreadshirt den Schreibzugriff einzeln
freischaltet. Wer hier eine ``lade_motiv_hoch``-Funktion sucht: die gehoert nach
Printify (``app/studio/printify/``), nicht hierher.

Zwei Eigenarten, die man sonst teuer lernt:

* **EU und Nordamerika sind getrennte Welten.** ``api.spreadshirt.net`` (EU) und
  ``api.spreadshirt.com`` (NA) haben eigene Schluessel. Ein EU-Schluessel an der
  NA-Basis antwortet mit 401, ohne das zu erklaeren.
* **Der User-Agent ist Pflicht, kein Schmuck.** Spreadshirt sperrt Anfragen ohne
  aussagekraeftige Kennung. Deshalb wirft dieser Client schon beim Anlegen, wenn
  keine gesetzt ist - ein spaeteres 403 waere im Betrieb kaum zu deuten.

Nebenlaeufig wie der Printify-Client (``httpx.AsyncClient``): ein
blockierender Aufruf wuerde den ganzen Server anhalten, waehrend er wartet.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger("app.integrations.spreadshirt")

BASE_URL_EU = "https://api.spreadshirt.net/api/v1"
BASE_URL_NA = "https://api.spreadshirt.com/api/v1"

_MAX_VERSUCHE = 3
_STANDARD_WARTE_S = 2.0


class SpreadshirtFehler(RuntimeError):
    """Ein Aufruf an Spreadshirt ist fehlgeschlagen."""


def _warte_sekunden(antwort: httpx.Response) -> float:
    kopf = antwort.headers.get("Retry-After")
    if kopf:
        try:
            return max(0.5, float(kopf))
        except ValueError:
            pass
    return _STANDARD_WARTE_S


def signatur(methode: str, url: str, secret: str, zeitpunkt: int) -> str:
    """Die Signatur fuer schreibende Aufrufe der Partner-Schnittstelle.

    Spreadshirt bildet SHA-1 ueber ``"<METHODE> <volle URL> <Zeit> <Secret>"``.
    Die URL muss die VOLLE Adresse sein, nicht nur der Pfad - mit dem Pfad
    allein stimmt die Signatur nicht und der Server antwortet mit 401.

    SHA-1 ist Spreadshirts Vorgabe, nicht unsere Wahl.
    """
    return hashlib.sha1(f"{methode} {url} {zeitpunkt} {secret}".encode()).hexdigest()


class SpreadshirtClient:
    """Schmale Huelle um die Spreadshirt-Schnittstelle. Nur Lesen."""

    def __init__(
        self,
        api_key: str,
        shop_id: str = "",
        *,
        api_secret: str = "",
        base_url: str = BASE_URL_EU,
        user_agent: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Kein Spreadshirt-Zugang gesetzt (SPREADSHIRT_API_KEY in der .env).")
        if not user_agent.strip():
            raise ValueError(
                "Kein User-Agent gesetzt (SPREADSHIRT_USER_AGENT in der .env). "
                'Format: "Name/Version (URL; Mail)". Ohne ihn sperrt Spreadshirt die Anfragen.'
            )
        self._key = api_key
        self._secret = api_secret
        self.shop_id = shop_id
        self._base = base_url.rstrip("/")
        self._ua = user_agent.strip()
        self._transport = transport

    # --- Koepfe ---------------------------------------------------------------

    def kopf(self) -> dict[str, str]:
        """Der einfache Kopf: reicht fuer alles Lesende am oeffentlichen Shop."""
        return {
            "Authorization": f'SprdAuth apiKey="{self._key}"',
            "User-Agent": self._ua,
            "Accept": "application/json",
        }

    def kopf_signiert(self, methode: str, url: str, session: str | None = None) -> dict[str, str]:
        """Der signierte Kopf fuer schreibende Aufrufe.

        Wird von diesem Client noch nirgends benutzt - er steht hier, damit der
        Weg dokumentiert und geprueft ist, wenn der Schreibzugriff kommt.
        """
        if not self._secret:
            raise SpreadshirtFehler(
                "Signierte Aufrufe brauchen SPREADSHIRT_API_SECRET (fehlt in der .env)."
            )
        jetzt = int(time.time())
        daten = f"{methode} {url} {jetzt}"
        wert = (
            f'SprdAuth apiKey="{self._key}", data="{daten}", '
            f'sig="{signatur(methode, url, self._secret, jetzt)}"'
        )
        if session:
            wert += f', sessionId="{session}"'
        kopf = self.kopf()
        kopf["Authorization"] = wert
        return kopf

    # --- Weg nach draussen ----------------------------------------------------

    async def _anfrage(self, methode: str, pfad: str, params: dict[str, Any] | None = None) -> Any:
        anfrage_params = dict(params or {})
        # Ohne diesen Schalter antwortet Spreadshirt in XML.
        anfrage_params.setdefault("mediaType", "json")
        url = f"{self._base}{pfad}"

        letzter: httpx.Response | None = None
        for versuch in range(1, _MAX_VERSUCHE + 1):
            async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as client:
                antwort = await client.request(
                    methode, url, headers=self.kopf(), params=anfrage_params
                )
            letzter = antwort
            if antwort.status_code == 429 or antwort.status_code >= 500:
                if versuch < _MAX_VERSUCHE:
                    warte = _warte_sekunden(antwort)
                    logger.warning(
                        "Spreadshirt %s %s -> %s, warte %.1fs (Versuch %s/%s)",
                        methode, pfad, antwort.status_code, warte, versuch, _MAX_VERSUCHE,
                    )
                    await asyncio.sleep(warte)
                    continue
            break

        assert letzter is not None
        if letzter.status_code >= 400:
            raise SpreadshirtFehler(
                f"{methode} {pfad}: {letzter.status_code} {letzter.text[:300]}"
            )
        if not letzter.content:
            return None
        try:
            return letzter.json()
        except ValueError as fehler:  # XML statt JSON -> mediaType verloren
            raise SpreadshirtFehler(
                f"{methode} {pfad}: Antwort ist kein JSON ({fehler})."
            ) from fehler

    # --- Lesen ----------------------------------------------------------------

    def _shop_pfad(self) -> str:
        """Der Shop-Pfad - und die Pruefung, die einen ratlosen 404 verhindert.

        Spreadshirt adressiert Shops ueber eine ZAHL, nicht ueber den Namen aus
        der Adresszeile. Wer "meinshop" eintraegt, bekommt 404 "Not found." und
        sucht den Fehler beim Schluessel. Die Nummer steht im Partnerbereich in
        den Shop-Einstellungen.
        """
        if not self.shop_id:
            raise SpreadshirtFehler("Keine Shop-Nummer gesetzt (SPREADSHIRT_SHOP_ID in der .env).")
        if not self.shop_id.isdigit():
            raise SpreadshirtFehler(
                f"SPREADSHIRT_SHOP_ID ist {self.shop_id!r} - Spreadshirt erwartet hier die "
                "ZAHL des Shops, nicht seinen Namen. Sie steht im Partnerbereich unter "
                "den Shop-Einstellungen."
            )
        return f"/shops/{self.shop_id}"

    async def shop(self) -> dict[str, Any]:
        """Stammdaten des Shops - der guenstigste Test, ob der Zugang steht."""
        return await self._anfrage("GET", self._shop_pfad())

    async def artikel(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """Eine Seite der Artikelliste."""
        return await self._anfrage(
            "GET", f"{self._shop_pfad()}/articles", params={"limit": limit, "offset": offset}
        )

    async def artikel_alle(self, hoechstens: int = 500, seitengroesse: int = 50) -> list[dict[str, Any]]:
        """Alle Artikel, seitenweise geholt.

        ``hoechstens`` ist eine Bremse, keine Schaetzung: ohne sie laeuft ein
        Shop mit tausenden Artikeln die Schnittstelle leer. Was fehlt, fehlt
        sichtbar - es wird nichts ergaenzt.
        """
        gesammelt: list[dict[str, Any]] = []
        offset = 0
        while len(gesammelt) < hoechstens:
            rest = hoechstens - len(gesammelt)
            seite = await self.artikel(limit=min(seitengroesse, rest), offset=offset)
            treffer = (seite or {}).get("articles") or []
            if not treffer:
                break
            # Abschneiden statt vertrauen: Spreadshirt darf mehr liefern als
            # angefragt, die Bremse muss trotzdem halten.
            gesammelt.extend(treffer[:rest])
            offset += len(treffer)
            if len(treffer) < seitengroesse:
                break
        return gesammelt


def aus_umgebung(transport: httpx.AsyncBaseTransport | None = None) -> SpreadshirtClient:
    """Client aus der .env. Wirft, wenn der Kanal nicht eingerichtet ist."""
    s = get_settings()
    return SpreadshirtClient(
        api_key=s.spreadshirt_api_key,
        shop_id=s.spreadshirt_shop_id,
        api_secret=s.spreadshirt_api_secret,
        base_url=s.spreadshirt_base_url,
        user_agent=s.spreadshirt_user_agent,
        transport=transport,
    )


def ist_eingerichtet() -> bool:
    """Ob der Kanal ueberhaupt benutzbar ist - ohne etwas anzufassen."""
    s = get_settings()
    return bool(s.spreadshirt_api_key and s.spreadshirt_shop_id and s.spreadshirt_user_agent.strip())
