"""Den Aufdruck von den Produktbildern ablesen.

Warum es das braucht
--------------------
Bei Motiv-Bekleidung steht der Spruch fast immer NUR auf dem Bild. Im
AliExpress-Titel und in der Beschreibung taucht er nicht auf. Die Titelregel vom
03.09.2026 verlangt ihn aber im Verkaufstitel - und darf ihn nicht erfinden.
Ohne diese Analyse bleibt sie bei den meisten Artikeln wirkungslos.

Nutzerbefund, woertlich: "Evtl muessen wir eine funktion einbauen, die jedes
bild analysiert insbesondere auf den text auf dem bild. Da das nicht immer im
titel oder beschreibung auf aliexpress steht."

Kostenbremse
------------
Jede Analyse kostet Geld. Deshalb:

* Sie laeuft **nie von selbst**. Kein Import, kein Zeitplaner-Job ruft sie auf -
  nur der ausdrueckliche Aufruf ueber ``scripts/lies_aufdrucke.py``.
* ``max_bilder`` deckelt jeden Lauf.
* Ein bereits gelesener Aufdruck wird nicht erneut gelesen (``nur_fehlende``).

Wo das Ergebnis liegt
---------------------
Im vorhandenen Aufgaben-Protokoll (``task_logs``), Typ ``bildtext``, mit der
Listing-Nummer als Bezug. Bewusst dort und nicht in einer neuen Spalte: es
braucht keine Datenbank-Aenderung, es traegt von sich aus einen Zeitstempel, und
ein Ergebnis von heute laesst sich von einem aelteren unterscheiden.

Nicht gelesen heisst nicht leer
------------------------------
Findet die Analyse keinen Text, wird das ALS ERGEBNIS festgehalten
(``text: ""``). Sonst liefe derselbe Artikel bei jedem Lauf erneut durch die
Analyse und koestete jedes Mal Geld fuer dieselbe Auskunft.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import Listing, Product, TaskLog
from app.services.common import task_log

logger = logging.getLogger("app.services.bildtext")

#: Typ im Aufgaben-Protokoll.
TYP = "bildtext"


def gelesen(db: Session, listing_id: int) -> dict[str, Any] | None:
    """Das juengste Analyse-Ergebnis zu diesem Listing - oder None."""
    eintrag = db.scalars(
        select(TaskLog)
        .where(TaskLog.task_type == TYP,
               TaskLog.reference_id == str(listing_id),
               TaskLog.status == "success")
        .order_by(desc(TaskLog.created_at))
        .limit(1)
    ).first()
    return dict(eintrag.result_data) if eintrag and eintrag.result_data else None


def _bilder(product: Product | None) -> list[str]:
    return [u for u in list((product.images if product else None) or []) if u]


async def analysiere(db: Session, *, listing_id: int, llm=None) -> dict[str, Any]:
    """Einen Artikel analysieren. Kostet Geld - siehe Kostenbremse im Modulkopf."""
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ValueError("Listing nicht gefunden")
    product = db.get(Product, listing.product_id) if listing.product_id else None
    bilder = _bilder(product)
    if not bilder:
        return {"listing_id": listing_id, "text": "", "grund": "keine Bilder"}

    if llm is None:
        from app.integrations import get_llm_client
        llm = get_llm_client()

    with task_log(db, task_type=TYP, reference_id=listing_id) as tl:
        ergebnis = await llm.lies_aufdruck(
            product_title=listing.title_seo or (product.title_raw if product else ""),
            image_urls=bilder)
        # Ein GESCHEITERTER Aufruf ist kein Ergebnis. Wuerde er als "kein Aufdruck"
        # festgehalten, ueberspraenge ``nur_fehlende`` den Artikel fuer immer - und
        # das System behauptete dauerhaft, ein Motiv-Shirt trage keinen Aufdruck.
        # Beim ersten Lauf am 03.09.2026 passierte genau das dreimal.
        if (ergebnis or {}).get("fehler"):
            raise RuntimeError(f"Bildanalyse nicht moeglich: {ergebnis['fehler']}")
        tl.result_data = {
            "text": (ergebnis or {}).get("text") or "",
            "sicher": bool((ergebnis or {}).get("sicher")),
            "sprache": (ergebnis or {}).get("sprache") or "",
            "zielgruppe": (ergebnis or {}).get("zielgruppe") or "",
            "bilder": len(bilder),
        }
        daten = dict(tl.result_data)

    daten["listing_id"] = listing_id
    if daten["text"]:
        logger.info("Aufdruck gelesen (Listing %s): %r", listing_id, daten["text"][:60])
    return daten


async def analysiere_alle(db: Session, *, nur_entwuerfe: bool = True,
                          nur_fehlende: bool = True, max_bilder: int = 30,
                          llm=None) -> dict[str, Any]:
    """Mehrere Artikel nacheinander. ``max_bilder`` ist die Kostenbremse.

    Laeuft bewusst der Reihe nach statt parallel: die Analyse ist kein
    Massengeschaeft, und bei einem Fehler soll erkennbar bleiben, wo er auftrat.
    """
    frage = select(Listing).order_by(Listing.id)
    if nur_entwuerfe:
        frage = frage.where(Listing.listing_status == "draft")
    listings = list(db.scalars(frage).all())

    gefunden, leer, uebersprungen, fehler = [], 0, 0, 0
    versuche = 0
    deckel = max(1, int(max_bilder))
    for listing in listings:
        # Gezaehlt werden VERSUCHE, nicht Erfolge. Ein gescheiterter Aufruf kann
        # trotzdem Geld gekostet haben - und beim ersten Lauf am 03.09.2026 lief
        # der Deckel ins Leere: 28 Fehlversuche, obwohl 3 gewuenscht waren. Eine
        # Kostenbremse, die nur bei Erfolg bremst, ist keine.
        if versuche >= deckel:
            break
        if nur_fehlende and gelesen(db, listing.id) is not None:
            uebersprungen += 1
            continue
        versuche += 1
        try:
            daten = await analysiere(db, listing_id=listing.id, llm=llm)
        except Exception as exc:  # noqa: BLE001 - ein Artikel darf den Lauf nicht stoppen
            logger.warning("Bildanalyse fehlgeschlagen fuer %s: %s", listing.id, exc)
            fehler += 1
            continue
        if daten.get("text"):
            gefunden.append({"listing_id": listing.id, "titel": listing.title_seo,
                             "aufdruck": daten["text"], "sicher": daten.get("sicher"),
                             "zielgruppe": daten.get("zielgruppe", "")})
        else:
            leer += 1

    return {"geprueft": len(gefunden) + leer, "mit_aufdruck": len(gefunden),
            "ohne_aufdruck": leer, "schon_gelesen": uebersprungen,
            "fehler": fehler, "treffer": gefunden}
