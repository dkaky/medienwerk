"""Beleg-Abruf auf Zuruf: Knopf im Dashboard -> lokaler Helfer holt die Belege.

WARUM DIESER UMWEG: AliExpress hat fuer den Kaufbeleg keine Schnittstelle – er
entsteht erst im Browser einer angemeldeten Sitzung. Der Server kommt an keinen
Browser bei Wajjahat oder Kaky heran, ein Knopf kann den Abruf also nicht direkt
ausloesen. Stattdessen hinterlegt der Knopf einen AUFTRAG; der Helfer auf dem
Rechner (``scripts/belege_backfill.py --dienst``) fragt regelmaessig nach, nimmt
ihn an und meldet das Ergebnis zurueck.

Zustand liegt als JSON-Datei (``data/beleg_abruf.json``) – kein Schema, keine
Migration, und nach einem Neustart ist der Auftrag noch da.

Beide Rechner duerfen mitlesen, aber nur EINER bekommt den Auftrag: ``uebernehmen``
sperrt ihn und gibt danach niemandem mehr etwas. Sonst laufen zwei Sammler
gleichzeitig durch dieselben Bestellungen.

Wer den Auftrag hat, wird BEWUSST NICHT gespeichert (Nutzerwunsch 17.08.): fuer die
Sperre reicht ein Zeitstempel, Benutzer- und Rechnername gehen niemanden etwas an.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DATEI = Path("./data") / "beleg_abruf.json"

# Nimmt ein Helfer einen Auftrag an und stirbt (Rechner zu, Chrome zu, Absturz),
# darf der Auftrag nicht ewig als "laeuft" haengen. Nach dieser Zeit ist er wieder frei.
_UEBERNAHME_VERFAELLT = timedelta(minutes=30)


def _jetzt() -> datetime:
    return datetime.now(timezone.utc)


def _lies() -> dict:
    try:
        return json.loads(_DATEI.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 – fehlt/kaputt = kein Auftrag
        return {}


def _schreib(daten: dict) -> None:
    """Atomar schreiben: ein halb geschriebener Auftrag waere schlimmer als keiner."""
    _DATEI.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(_DATEI.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(daten, f, ensure_ascii=False)
        os.replace(tmp, _DATEI)
    except Exception:  # noqa: BLE001
        Path(tmp).unlink(missing_ok=True)
        raise


def _abgelaufen(daten: dict) -> bool:
    wann = daten.get("uebernommen_am")
    if not wann:
        return False
    try:
        return _jetzt() - datetime.fromisoformat(wann) > _UEBERNAHME_VERFAELLT
    except (ValueError, TypeError):
        return True


def status() -> dict:
    """Was ist gerade los? Fuer die Anzeige im Dashboard UND fuer den Helfer."""
    d = _lies()
    offen = bool(d.get("angefordert_am")) and not d.get("erledigt_am")
    laeuft = offen and bool(d.get("uebernommen_am")) and not _abgelaufen(d)
    return {
        "offen": offen,
        "laeuft": laeuft,
        "wartet": offen and not laeuft,          # angefordert, aber kein Helfer dran
        "angefordert_am": d.get("angefordert_am"),
        "erledigt_am": d.get("erledigt_am"),
        "ergebnis": d.get("ergebnis"),
        # Ueberdauert neue Auftraege: das Dashboard faerbt den Knopf ab 20 Uhr rot,
        # wenn an diesem Tag noch niemand die Belege geholt hat.
        "zuletzt_gelaufen_am": d.get("zuletzt_gelaufen_am"),
    }


def anfordern() -> dict:
    """Knopf im Dashboard: Auftrag hinterlegen.

    Laeuft schon einer, wird NICHT neu angefordert – sonst startet ein zweiter
    Sammler mitten in den ersten hinein.
    """
    st = status()
    if st["laeuft"]:
        return {**st, "hinweis": "Ein Abruf laeuft bereits."}
    alt = _lies().get("zuletzt_gelaufen_am")
    _schreib({"angefordert_am": _jetzt().isoformat(),
              **({"zuletzt_gelaufen_am": alt} if alt else {})})
    return status()


def uebernehmen() -> dict:
    """Helfer meldet: ich mache das. Nur der erste bekommt den Zuschlag."""
    d = _lies()
    if not d.get("angefordert_am") or d.get("erledigt_am"):
        return {"uebernommen": False, "grund": "kein offener Auftrag"}
    if d.get("uebernommen_am") and not _abgelaufen(d):
        return {"uebernommen": False, "grund": "laeuft bereits"}
    d["uebernommen_am"] = _jetzt().isoformat()
    _schreib(d)
    return {"uebernommen": True}


def fertig(*, geholt: int = 0, uebersprungen: int = 0, fehler: int = 0,
           rechnungen: int = 0, meldung: str | None = None) -> dict:
    """Helfer meldet das Ergebnis zurueck – damit steht es auch im Dashboard."""
    d = _lies()
    d["erledigt_am"] = _jetzt().isoformat()
    d["zuletzt_gelaufen_am"] = d["erledigt_am"]
    d["ergebnis"] = {"geholt": int(geholt), "uebersprungen": int(uebersprungen),
                     "fehler": int(fehler), "rechnungen": int(rechnungen),
                     **({"meldung": meldung[:300]} if meldung else {})}
    _schreib(d)
    return status()
