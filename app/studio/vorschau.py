"""Verkleinerte Fassungen der Motive - damit die Oberflaeche benutzbar bleibt.

Anlass: Die Motivliste im Studio laedt jedes Bild in voller Groesse, um daraus
eine 200 Pixel breite Kachel zu machen. Gemessen am 03.09.2026 waren das
**106 MB und 153 Megapixel fuer 45 Kacheln** - entpackt rund 0,6 GB im
Arbeitsspeicher. Die Seite wurde dadurch so traege, dass der Browser beim
Zeichnen der Anprobe stehen blieb.

Druckdateien sind 4500x5400 Pixel gross, und das ist richtig so - gedruckt wird
daraus. Nur ansehen muss man sie in dieser Groesse nie.

Verfahren
---------
Eine angeforderte Breite liefert eine verkleinerte Kopie, die neben dem Original
liegt (Unterordner ``.vorschau``). Sie wird einmal gerechnet und danach
wiederverwendet. Veraltet sie - weil das Original neuer ist - wird sie neu
gerechnet; verglichen wird der Aenderungszeitpunkt, nicht der Inhalt.

Das gehoert bewusst NICHT in die Route: die soll ausliefern, nicht rechnen. Und
ein eigenes Modul laesst sich pruefen, ohne einen Webserver zu starten.

Durchsichtigkeit bleibt erhalten (RGBA, PNG). Eine Vorschau als JPEG waere
kleiner, wuerde aber den durchsichtigen Hintergrund schwarz fuellen - und genau
den muss man an einem Druckmotiv sehen koennen.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger("app.studio.vorschau")

#: Unterordner fuer die verkleinerten Fassungen. Punkt am Anfang, damit er beim
#: Durchsuchen des Bilderordners nicht als Motivordner missverstanden wird.
ORDNER = ".vorschau"

#: Erlaubte Breiten. Bewusst eine feste Liste statt beliebiger Zahlen: sonst
#: legt ein Aufruf mit tausend verschiedenen Breiten tausend Dateien an.
BREITEN = (200, 400, 900)

#: Ist das Original nicht wenigstens um diesen Faktor breiter, wird nicht
#: verkleinert - das Rechnen waere teurer als das Ausliefern.
MINDEST_ERSPARNIS = 1.3


def _saubere_kennung(rel: str) -> str:
    """Aus einem Unterpfad einen flachen, ungefaehrlichen Dateinamen machen."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", rel.replace("\\", "/"))


def erlaubte_breite(wunsch: int | None) -> int | None:
    """Der naechstliegende erlaubte Wert - oder None fuer das Original."""
    if not wunsch:
        return None
    try:
        w = int(wunsch)
    except (TypeError, ValueError):
        return None
    if w <= 0:
        return None
    return min(BREITEN, key=lambda b: abs(b - w))


def hole(original: Path, *, breite: int, wurzel: Path, zuschnitt: bool = False) -> Path:
    """Pfad einer verkleinerten Fassung. Legt sie an, falls noetig.

    ``zuschnitt`` schneidet den durchsichtigen Rand ab - so zeigt der Editor das
    Motiv mit denselben Massen, mit denen es ins Druckbild kommt.

    Faellt bei jedem Problem auf das Original zurueck - eine fehlende Vorschau
    darf nie dazu fuehren, dass ein Motiv gar nicht mehr angezeigt wird.
    """
    try:
        from PIL import Image
    except ImportError:                      # pragma: no cover
        return original

    try:
        with Image.open(original) as bild:
            quell_breite = bild.width
            if quell_breite <= breite * MINDEST_ERSPARNIS and not zuschnitt:
                return original              # lohnt nicht

            rel = original.relative_to(wurzel).as_posix()
            ziel_ordner = wurzel / ORDNER
            ziel_ordner.mkdir(parents=True, exist_ok=True)
            ziel = ziel_ordner / f"{_saubere_kennung(rel)}-{breite}{'-z' if zuschnitt else ''}.png"

            if ziel.is_file() and ziel.stat().st_mtime >= original.stat().st_mtime:
                return ziel                  # noch gueltig

            # RGBA und PNG, damit der durchsichtige Hintergrund durchsichtig
            # bleibt - an einem Druckmotiv ist das die wichtigste Angabe.
            quelle = bild.convert("RGBA")
            if zuschnitt:
                rahmen = quelle.getbbox()
                if rahmen:
                    quelle = quelle.crop(rahmen)
            zielbreite = min(breite, quelle.width)
            hoehe = max(1, round(quelle.height * zielbreite / quelle.width))
            klein = quelle if zielbreite == quelle.width else quelle.resize((zielbreite, hoehe), Image.LANCZOS)
            klein.save(ziel, format="PNG", optimize=True)
            logger.info("Vorschau angelegt: %s (%s -> %s px)", rel, quell_breite, breite)
            return ziel
    except Exception as exc:                 # noqa: BLE001
        logger.warning("Vorschau fehlgeschlagen fuer %s: %s", original.name, exc)
        return original
