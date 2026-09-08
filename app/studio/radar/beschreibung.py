"""Stufe 3c: hinsehen statt Titel raten.

Bis hierher kannte das Radar nur den Verkaufstitel. Der sagt "Lustiges T-Shirt
Einhorn - Spruch, Glitzer & Leopard" und verschweigt alles, was man zum
Zeichnen braucht: Haltung der Figur, Schriftart, Farbrollen, Aufbau.

Nutzerwunsch vom 05.09.2026, woertlich: **„Brauche eine sehr detaillierte
beschreibung damit die KI die bilder nachmacht."**

Dieses Modul sieht sich das Produktfoto an und schreibt auf, was darauf steht -
so genau, dass ein Bildmodell daraus etwas machen kann. Zwei Dinge sind dabei
Absicht:

* **Ein Fehlschlag wird NICHT als Befund gespeichert.** Eine leere Beschreibung
  und eine gescheiterte Anfrage sehen sonst gleich aus - die Idee waere danach
  dauerhaft als "nichts drauf" abgelegt. Deshalb ``fehler`` im Ergebnis, und
  der Zaehler zaehlt VERSUCHE statt Treffer (am 03.09.2026 wurden aus drei
  geplanten Aufrufen achtundzwanzig, weil er nur Erfolge zaehlte).
* **Der Wortlaut bleibt Beleg.** ``text_woertlich`` wird gespeichert, damit man
  den Fund wiedererkennt und pruefen kann, ob der eigene Entwurf zu nah dran
  ist. In einen Prompt geht er nie (siehe ``umwandlung``).
"""
from __future__ import annotations

import json
import logging

from app.studio.models import MotivIdee

logger = logging.getLogger("app.studio.radar.beschreibung")

#: Deckel je Aufruf. Jede Beschreibung ist ein Bildaufruf und kostet.
MAX_JE_LAUF = 25


class KeinBild(ValueError):
    """Zu dieser Idee gibt es kein Produktfoto."""


def gelesen(idee: MotivIdee) -> dict:
    """Die gespeicherte Beschreibung - leer, wenn keine oder eine kaputte da ist."""
    roh = getattr(idee, "beschreibung", None)
    if not roh:
        return {}
    try:
        daten = json.loads(roh)
    except (ValueError, TypeError):
        return {}
    return daten if isinstance(daten, dict) else {}


async def beschreibe(db, idee: MotivIdee, *, llm=None) -> dict:
    """Ein Motiv ansehen und die Beschreibung an der Idee festhalten."""
    if not idee.bild_url:
        raise KeinBild(
            f"Zu Idee {idee.id} ist kein Produktfoto gespeichert. Ein neuer "
            f"Erntelauf holt es nach.")
    if llm is None:
        from app.integrations import get_llm_client

        llm = get_llm_client()

    ergebnis = await llm.beschreibe_motiv(
        product_title=idee.fremdtitel, image_urls=[idee.bild_url])
    if ergebnis.get("fehler"):
        # NICHT speichern - sonst gilt der Fehlschlag hinterher als Befund.
        logger.warning("beschreibung gescheitert",
                       extra={"idee": idee.id,
                              "fehler": str(ergebnis["fehler"])[:120]})
        return ergebnis

    idee.beschreibung = json.dumps(ergebnis, ensure_ascii=False)
    # Das Thema aus dem BILD schlaegt das aus dem Titel: es kommt von dem, was
    # wirklich zu sehen ist, nicht von den Suchwoertern des fremden Verkaeufers.
    if ergebnis.get("thema"):
        idee.thema = str(ergebnis["thema"])[:255]
    db.commit()
    return ergebnis


async def beschreibe_alle(db, ideen, *, hoechstens: int = MAX_JE_LAUF,
                          llm=None) -> dict:
    """Mehrere Motive nacheinander ansehen. Gezaehlt werden VERSUCHE."""
    bericht: dict = {"versucht": 0, "beschrieben": 0, "ohne_bild": 0,
                     "fehler": []}
    for idee in ideen:
        if bericht["versucht"] >= max(1, int(hoechstens)):
            break
        bericht["versucht"] += 1
        try:
            ergebnis = await beschreibe(db, idee, llm=llm)
        except KeinBild:
            bericht["ohne_bild"] += 1
            continue
        if ergebnis.get("fehler"):
            bericht["fehler"].append(f"#{idee.id}: {str(ergebnis['fehler'])[:100]}")
        else:
            bericht["beschrieben"] += 1
    return bericht


#: Reihenfolge und Beschriftung der Anzeige - einmal festgelegt, damit Konsole,
#: Oberflaeche und Bericht dasselbe zeigen.
_FELDER = (
    ("motiv", "Motiv"),
    ("text_woertlich", "Aufdruck (Beleg)"),
    ("text_anordnung", "Textanordnung"),
    ("schrift", "Schrift"),
    ("stil", "Stil"),
    ("farben", "Farben"),
    ("komposition", "Aufbau"),
    ("effekte", "Effekte"),
    ("ware_farbe", "Ware"),
    ("zielgruppe", "Zielgruppe"),
    ("thema", "Thema"),
)


def als_text(idee: MotivIdee) -> str:
    """Die Beschreibung zum Lesen - fuer Konsole und Bericht."""
    d = gelesen(idee)
    if not d:
        return "  (noch nicht angesehen)"
    zeilen = []
    for schluessel, name in _FELDER:
        wert = d.get(schluessel)
        if isinstance(wert, list):
            wert = ", ".join(str(w) for w in wert)
        if wert:
            zeilen.append(f"  {name + ':':<18}{wert}")
    if not d.get("sicher"):
        zeilen.append("  (unsicher: das Foto war zu klein oder unscharf)")
    return "\n".join(zeilen)
