"""Jede Variante bekommt ihre Kennung schon beim Import - und behaelt sie.

Nutzerwunsch vom 03.09.2026, woertlich: "damit nicht das selbe passiert wie im
ebay projekt, bei jedem import genau merken welche variante zu dem aliexpress
aequivalent passt, so haben wir auch immer eine genaue kalkulation pro variante
und wissen genau beim bestellen was schon angeklickt sein muss."

Was vorher fehlte
-----------------
Die Kennung (``ebay_sku``) wurde erst BEIM VEROEFFENTLICHEN festgeschrieben.
Bis dahin leitete ``variant_ebay_sku`` sie aus der POSITION ab: V1, V2, V3 in
der Reihenfolge, in der AliExpress die Varianten gerade liefert.

Das haelt nur, solange sich diese Reihenfolge nie aendert. Faellt eine Variante
weg oder sortiert AliExpress um, ruecken alle nachfolgenden eine Nummer hoch -
und Preis, Bestand und Bestellung landen auf der FALSCHEN Variante. Genau davor
warnt schon der Kommentar an ``variant_ebay_sku``; die Absicherung galt aber
erst ab dem Live-Gang. Zwischen Import und Klick war die Zuordnung ungesichert,
und dort liegen die Entwuerfe wochenlang.

Wie es jetzt haelt
------------------
Die Kennung entsteht beim Import und wird an der Variante festgeschrieben,
zugeordnet ueber ``attr`` - die SKU-Kennung von AliExpress. ``attr`` ist der
einzige Wert, der die Variante bei der Quelle eindeutig benennt; Position,
Optionsname und Preis aendern sich alle.

Bei einem erneuten Import gilt: **bereits vergebene Kennungen bleiben.** Neue
Varianten bekommen die naechste freie Nummer. Nichts wird umnummeriert - sonst
zeigte eine bereits verkaufte eBay-Variante ploetzlich auf eine andere Ware.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("app.services.variantenkennung")

#: Laengengrenze von eBay fuer eine SKU.
MAX_LAENGE = 50


def _naechste_nummer(vergeben: set[str], basis: str) -> int:
    """Die kleinste freie Nummer nach dem Muster ``<basis>-V<n>``."""
    benutzt = set()
    muster = re.compile(rf"^{re.escape(basis)}-V(\d+)$")
    for s in vergeben:
        treffer = muster.match(s or "")
        if treffer:
            benutzt.add(int(treffer.group(1)))
    n = 1
    while n in benutzt:
        n += 1
    return n


def vergib(variants: dict | None, basis_sku: str) -> tuple[dict | None, int]:
    """Fehlende ``ebay_sku`` an den Varianten ergaenzen. Vorhandene bleiben.

    Gibt ``(variants, anzahl_neu)`` zurueck. Arbeitet auf einer KOPIE - der
    Aufrufer entscheidet, ob und wann er sie speichert.
    """
    if not isinstance(variants, dict):
        return variants, 0
    skus = variants.get("skus")
    if not isinstance(skus, list) or not skus:
        return variants, 0

    basis = (basis_sku or "").strip()
    if not basis:
        return variants, 0

    vergeben = {str(s.get("ebay_sku")) for s in skus
                if isinstance(s, dict) and s.get("ebay_sku")}
    neue_skus, neu = [], 0
    for s in skus:
        if not isinstance(s, dict):
            neue_skus.append(s)
            continue
        if s.get("ebay_sku"):
            neue_skus.append(s)              # unveraendert - nie umnummerieren
            continue
        nummer = _naechste_nummer(vergeben, basis)
        kennung = f"{basis}-V{nummer}"[:MAX_LAENGE]
        vergeben.add(kennung)
        neue_skus.append({**s, "ebay_sku": kennung})
        neu += 1

    if not neu:
        return variants, 0
    logger.info("Variantenkennungen vergeben: %s neu (Basis %s)", neu, basis)
    return {**variants, "skus": neue_skus}, neu


def zuordnung(variants: dict | None) -> dict[str, str]:
    """Wer gehoert zu wem: ``ebay_sku`` -> AliExpress ``attr``.

    Das ist die Auskunft, die beim Bestellen gebraucht wird: welche Optionen
    muessen bei AliExpress angeklickt sein, damit die verkaufte Variante kommt.
    """
    if not isinstance(variants, dict):
        return {}
    paare = {}
    for s in variants.get("skus") or []:
        if isinstance(s, dict) and s.get("ebay_sku"):
            paare[str(s["ebay_sku"])] = str(s.get("attr") or "")
    return paare


def fehlende(variants: dict | None) -> list[dict]:
    """Varianten ohne Kennung - fuer Berichte und Vorabpruefungen."""
    if not isinstance(variants, dict):
        return []
    return [s for s in (variants.get("skus") or [])
            if isinstance(s, dict) and not s.get("ebay_sku")]
