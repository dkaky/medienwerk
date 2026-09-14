"""eBay-Loeschmeldungen abholen und Kaeuferdaten anonymisieren.

eBay schickt bei jeder geschlossenen eBay-Kontoverbindung eine Meldung - bis zu
~1.500 am Tag, fuer ALLE eBay-Nutzer, nicht nur fuer eigene Kaeufer. Der PC ist
nicht rund um die Uhr erreichbar, und eBay markiert einen Endpunkt nach 24 Stunden
ohne Antwort als ausgefallen. Deshalb nimmt eine Supabase-Funktion im
Lovable-Projekt die Meldungen an (``deploy/supabase-ebay-loeschung/``), und dieses
Modul holt sie ab, sobald das Programm laeuft.

Ablauf je Abholung:
  1. Offene Meldungen holen (hoechstens 500 je Lauf).
  2. Signatur pruefen - ueber den ECHTEN eBay-Client, auch im Probebetrieb. Der
     Attrappen-Client wuerde jede Signatur annehmen, und eine gefaelschte Meldung
     koennte dann echte Kaeuferdaten loeschen lassen.
  3. Nur wenn ein eigener Verkauf zu dem Konto passt: anonymisieren
     (``deletion_service.anonymize_buyer`` - Betraege bleiben, Kontaktdaten gehen).
  4. Verarbeitete Meldungen quittieren; die Funktion loescht sie dann.

**Eine ungueltige Signatur wird NICHT sofort verworfen.** ``verify_ipn_signature``
meldet ``False`` auch dann, wenn eBays oeffentlicher Schluessel wegen eines
Netzfehlers nicht zu laden war - von einer Faelschung ist das nicht zu
unterscheiden. Sofort verwerfen hiesse: Ein Netzaussetzer vernichtet echte
Loeschauftraege. Solche Meldungen bleiben deshalb liegen und werden erst nach
``VERWERFEN_NACH_TAGEN`` aufgegeben.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Sale
from app.services.deletion_service import anonymize_buyer, extract_deletion_target

logger = logging.getLogger("app.services.ebay_loeschmeldungen")

#: Wie lange eine Meldung mit ungueltiger Signatur liegen bleiben darf. Lang genug,
#: um einen mehrtaegigen Netz- oder eBay-Ausfall zu ueberstehen; kurz genug, dass sich
#: gefaelschte Meldungen nicht dauerhaft stapeln.
VERWERFEN_NACH_TAGEN = 3


class AbholFehler(RuntimeError):
    """Die Meldungen liessen sich nicht abholen oder quittieren."""


def ist_eingerichtet(s: Settings) -> bool:
    return bool(s.ebay_loesch_abhol_url and s.ebay_loesch_abhol_token)


def _kopf(s: Settings) -> dict[str, str]:
    return {"x-abhol-token": s.ebay_loesch_abhol_token}


def _alter(eingegangen_am: str | None, jetzt: datetime) -> timedelta | None:
    if not eingegangen_am:
        return None
    try:
        zeit = datetime.fromisoformat(eingegangen_am.replace("Z", "+00:00"))
    except ValueError:
        return None
    if zeit.tzinfo is None:
        zeit = zeit.replace(tzinfo=timezone.utc)
    return jetzt - zeit


def _hat_eigenen_verkauf(db: Session, username: str | None, user_id: str | None) -> bool:
    """Passt das Konto zu einem eigenen Verkauf? Billig vorab gefragt.

    Ohne diese Vorpruefung legte ``anonymize_buyer`` fuer JEDE Meldung einen
    Protokolleintrag an - bei ~1.500 fremden Konten am Tag ein Protokoll voller
    Leerlaeufe, in dem der eine echte Treffer untergeht.
    """
    namen = [v for v in (username, user_id) if v]
    if not namen:
        return False
    bedingungen = [Sale.buyer_name.in_(namen), Sale.buyer_email.in_(namen)]
    return bool(db.scalar(select(func.count()).select_from(Sale).where(or_(*bedingungen))))


def _echte_signaturpruefung(s: Settings) -> Callable[[bytes, str], bool]:
    from app.integrations.ebay import RealEbayClient

    return RealEbayClient(s).verify_ipn_signature


async def hole_und_verarbeite(
    db: Session,
    *,
    s: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    pruefe_signatur: Callable[[bytes, str], bool] | None = None,
    jetzt: datetime | None = None,
) -> dict[str, Any]:
    """Eine Abholung durchfuehren. Gibt Zaehler zurueck, wirft ``AbholFehler``."""
    s = s or get_settings()
    if not ist_eingerichtet(s):
        raise AbholFehler("EBAY_LOESCH_ABHOL_URL und EBAY_LOESCH_ABHOL_TOKEN fehlen in der .env.")
    pruefe_signatur = pruefe_signatur or _echte_signaturpruefung(s)
    jetzt = jetzt or datetime.now(timezone.utc)

    zaehler = {"abgeholt": 0, "anonymisiert": 0, "fremde_konten": 0,
               "zurueckgestellt": 0, "verworfen": 0, "quittiert": 0}

    async with httpx.AsyncClient(timeout=30.0, transport=transport) as client:
        try:
            r = await client.get(s.ebay_loesch_abhol_url, params={"abholen": "1"},
                                 headers=_kopf(s))
        except httpx.HTTPError as exc:
            raise AbholFehler(f"Funktion nicht erreichbar: {exc}") from exc
        if r.status_code == 401:
            raise AbholFehler("Abholschluessel abgelehnt - EBAY_LOESCH_ABHOL_TOKEN passt nicht "
                              "zum Secret EBAY_ABHOL_TOKEN in Lovable.")
        if r.status_code != 200:
            raise AbholFehler(f"Abholen fehlgeschlagen: HTTP {r.status_code} {r.text[:200]}")

        erledigt: list[int] = []
        for m in (r.json() or {}).get("meldungen") or []:
            zaehler["abgeholt"] += 1
            roh = m.get("roh") or ""
            try:
                gueltig = await asyncio.to_thread(pruefe_signatur, roh.encode("utf-8"),
                                                  m.get("signatur") or "")
            except Exception as exc:  # noqa: BLE001 - Pruefung unmoeglich, spaeter erneut
                logger.warning("Signaturpruefung nicht moeglich", extra={"error": str(exc)[:160]})
                zaehler["zurueckgestellt"] += 1
                continue

            if not gueltig:
                alter = _alter(m.get("eingegangen_am"), jetzt)
                if alter is not None and alter > timedelta(days=VERWERFEN_NACH_TAGEN):
                    zaehler["verworfen"] += 1
                    erledigt.append(m["id"])
                else:
                    zaehler["zurueckgestellt"] += 1
                continue

            try:
                ziel = extract_deletion_target(json.loads(roh))
            except ValueError:
                # Signiert, aber kein JSON - verarbeiten laesst sich das nie.
                zaehler["verworfen"] += 1
                erledigt.append(m["id"])
                continue

            if _hat_eigenen_verkauf(db, ziel["username"], ziel["user_id"]):
                anonymize_buyer(db, username=ziel["username"], user_id=ziel["user_id"])
                zaehler["anonymisiert"] += 1
            else:
                zaehler["fremde_konten"] += 1
            erledigt.append(m["id"])

        if erledigt:
            try:
                q = await client.post(s.ebay_loesch_abhol_url, params={"quittieren": "1"},
                                      headers=_kopf(s), json={"ids": erledigt})
            except httpx.HTTPError as exc:
                raise AbholFehler(f"Quittieren nicht erreichbar: {exc}") from exc
            if q.status_code != 200:
                # Nicht schlimm: Die Meldungen kommen beim naechsten Lauf wieder, und
                # das Anonymisieren ist wiederholbar.
                raise AbholFehler(f"Quittieren fehlgeschlagen: HTTP {q.status_code} {q.text[:200]}")
            zaehler["quittiert"] = len(erledigt)

    logger.info("eBay-Loeschmeldungen verarbeitet", extra=zaehler)
    return zaehler
