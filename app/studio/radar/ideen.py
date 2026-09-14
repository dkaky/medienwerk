"""Stufe 3b: die Funde ablegen - und die Entscheidung des Menschen schuetzen.

Eine Zeile in ``studio_motiv_ideen`` ist eine Beobachtung, kein Werk. Deshalb
zwei Regeln, die dieses Modul durchsetzt:

1. **Ein Fund bleibt ein Fund.** Wiedererkannt wird er an
   ``(Plattform, Shop, Fremdnummer)``. Ein zweiter Lauf legt ihn nicht noch
   einmal an, sondern frischt seine Zahlen auf - Verkaufszahlen aendern sich,
   das ist ja der Sinn.
2. **Was der Mensch entschieden hat, bleibt stehen.** ``status``, ``notiz``,
   ``eigener_prompt`` und ``design_id`` fasst ein Erntelauf NIE an. Wer eine
   Idee einmal verworfen hat, soll sie nicht beim naechsten Lauf wieder als
   "neu" vorgesetzt bekommen.
"""
from __future__ import annotations

import json
import logging
from typing import Iterable

from sqlalchemy import select

from app.studio.models import IDEE_STATUS, MotivIdee
from app.studio.radar import signale
from app.studio.radar.ernte import Fund
from app.studio.radar.quellen import Quelle

logger = logging.getLogger("app.studio.radar.ideen")


def stichworte_von(idee: MotivIdee) -> list[str]:
    """Die Stichwortliste einer Idee - leer, wenn das Feld kaputt ist.

    Ein beschaedigter JSON-Eintrag darf die Liste nicht sprengen; er heisst
    dann eben "keine Stichworte".
    """
    roh = getattr(idee, "stichworte", None)
    if not roh:
        return []
    try:
        werte = json.loads(roh)
    except (ValueError, TypeError):
        return []
    return [str(w) for w in werte] if isinstance(werte, list) else []


def _vorhanden(db, quelle: Quelle, fund: Fund) -> MotivIdee | None:
    """Dieselbe Beobachtung aus einem frueheren Lauf finden."""
    grund = select(MotivIdee).where(
        MotivIdee.quelle_plattform == quelle.plattform,
        MotivIdee.quelle_shop == quelle.shop,
    )
    if fund.fremd_id:
        return db.execute(
            grund.where(MotivIdee.fremd_id == str(fund.fremd_id))
        ).scalars().first()
    # Ohne Artikelnummer bleibt nur der Titel. Schwaecher, aber besser als
    # jedes Mal eine neue Zeile.
    return db.execute(
        grund.where(MotivIdee.fremdtitel == fund.titel)
    ).scalars().first()


def speichere(db, quelle: Quelle, funde: Iterable[Fund]) -> dict:
    """Funde ablegen. Meldet, was neu war und was nur aufgefrischt wurde."""
    neu = aufgefrischt = 0
    for fund in funde:
        if not (fund.titel or "").strip():
            continue
        wert, grund = signale.bewerte(fund)
        idee = _vorhanden(db, quelle, fund)
        if idee is None:
            idee = MotivIdee(
                quelle_plattform=quelle.plattform,
                quelle_shop=quelle.shop,
                fremd_id=str(fund.fremd_id) if fund.fremd_id else None,
                fremdtitel=fund.titel,
                status="neu",
            )
            db.add(idee)
            neu += 1
        else:
            aufgefrischt += 1

        # Beobachtung auffrischen - die Entscheidungsfelder bleiben unangetastet.
        idee.quelle_url = fund.url or idee.quelle_url
        idee.bild_url = fund.bild_url or idee.bild_url
        idee.thema = signale.thema(fund.titel)
        idee.stichworte = json.dumps(signale.stichworte(fund.titel),
                                     ensure_ascii=False)
        idee.signal = wert
        idee.signal_grund = grund[:255]
        idee.verkauft = fund.verkauft
        idee.bewertungen = fund.bewertungen
        idee.platz = fund.platz

    db.commit()
    logger.info("radar ideen abgelegt",
                extra={"quelle": quelle.schluessel, "neu": neu,
                       "auffrischung": aufgefrischt})
    return {"neu": neu, "aufgefrischt": aufgefrischt, "quelle": quelle.schluessel}


def liste(db, *, status: str | None = "neu", min_signal: float | None = None,
          shop: str | None = None, quelle: str | None = None,
          limit: int = 50) -> list[MotivIdee]:
    """Die Ideen, nach Signal absteigend.

    Ideen ohne Signal stehen ganz hinten - aber sie stehen da. Sie sind nicht
    geprueft und nicht schlecht, nur unbekannt (Eiserne Regel 3).

    ``shop`` grenzt auf einen Laden ein. Ohne das mischen sich ab dem zweiten
    beobachteten Shop die Funde, und die Frage "wie sieht DIESER Laden aus"
    laesst sich nicht mehr beantworten.
    """
    frage = select(MotivIdee)
    if status:
        frage = frage.where(MotivIdee.status == status)
    if shop:
        frage = frage.where(MotivIdee.quelle_shop == shop)
    # "trend" = Vorschlaege aus der Websuche, "shop" = Funde aus fremden Shops.
    if quelle == "trend":
        frage = frage.where(MotivIdee.quelle_plattform == "trend")
    elif quelle == "shop":
        frage = frage.where(MotivIdee.quelle_plattform != "trend")
    if min_signal is not None:
        frage = frage.where(MotivIdee.signal.is_not(None),
                            MotivIdee.signal >= float(min_signal))
    treffer = list(db.execute(frage).scalars().all())
    if quelle == "trend":
        # Trends haben kein Zahlensignal (kein erfundenes Suchvolumen) - geordnet
        # wird nach dem Rang der Recherche.
        treffer.sort(key=lambda i: (i.platz is None, i.platz or 0, -i.id))
    else:
        treffer.sort(key=lambda i: (i.signal is None, -(i.signal or 0.0), i.id))
    return treffer[:max(1, int(limit))]


def setze_status(db, idee_id: int, status: str,
                 *, notiz: str | None = None) -> MotivIdee:
    """Uebernehmen oder verwerfen - die Entscheidung des Menschen festhalten."""
    if status not in IDEE_STATUS:
        raise ValueError(
            f"Unbekannter Zustand '{status}'. Erlaubt: {', '.join(IDEE_STATUS)}")
    idee = db.get(MotivIdee, int(idee_id))
    if idee is None:
        raise ValueError(f"Motiv-Idee {idee_id} gibt es nicht.")
    idee.status = status
    if notiz is not None:
        idee.notiz = notiz
    db.commit()
    return idee
