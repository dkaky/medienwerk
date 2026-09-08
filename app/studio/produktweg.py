"""Vom Motiv zum Produkt - das fehlende Stueck zwischen zwei fertigen Teilen.

Beide Enden waren gebaut und wurden nie verbunden:

* ``postprocess/umrechner.py`` bringt ein Motiv ins Druckformat und BRICHT AB,
  wenn die Aufloesung nicht reicht.
* ``printify/service.py`` legt aus einer Bilddatei ein Printify-Produkt an.

Dazwischen fehlte der Weg. Und mehr noch: ``erstelle_produkt`` prueft die
Aufloesung NICHT. Direkt verbunden haette der Weg ein 1024 Pixel breites Motiv
fuer einen Textildruck hochgeladen, der 4500 verlangt. Printify haette
klaglos gedruckt, und die Retoure waere zu uns gekommen.

Deshalb sitzt der Umrechner hier ZWINGEND dazwischen. Er ist nicht Zierde,
sondern die Stelle, an der ein untaugliches Motiv aufgehalten wird - vor der
Bestellung, nicht danach.

Der Weg hat zwei Stufen, wie ueberall in diesem Programm:

1. ``pruefe()`` - liest, rechnet, aendert nichts. Sagt, ob es ginge und warum
   nicht.
2. ``lege_an()`` - erzeugt die Druckdatei und schickt sie zu Printify. Nur mit
   ausdruecklicher Bestaetigung.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.studio.postprocess.umrechner import (
    FormatFehler,
    platziere,
    pruefe_aufloesung,
    speichere,
    trimme,
    zielformat,
)
from app.studio.printify.products import PRODUCT_TYPES

logger = logging.getLogger("app.studio.produktweg")

# Welcher Produkttyp braucht welches Zielformat. Die Zuordnung gehoert hierher
# und nicht in eine der beiden Seiten: der Umrechner kennt keine Produkte, und
# der Printify-Teil kennt keine Druckformate.
FORMAT_JE_TYP = {
    "tshirt": "textil",
    "hoodie": "textil",
    "mug": "tasse",
}

# Wohin die erzeugten Druckdateien kommen. Bewusst ein eigener Ordner: eine
# Druckdatei ist kein Motiv, sondern ein Erzeugnis daraus - und sie soll sich
# jederzeit neu erzeugen lassen, ohne das Original zu gefaehrden.
DRUCK_ORDNER = "druckdateien"


class MotivFehler(ValueError):
    """Das Motiv taugt fuer diesen Produkttyp nicht."""


@dataclass(frozen=True)
class Pruefung:
    """Ergebnis der Vorabpruefung - reine Auskunft, nichts wurde geaendert."""

    moeglich: bool
    produkttyp: str
    format_key: str
    grund: str | None = None
    breite: int | None = None
    hoehe: int | None = None
    dpi: float | None = None
    zielmasse: tuple[int, int] | None = None


def bildpfad(design: Any, bildordner: Path) -> Path:
    """Aus der Web-Adresse eines Motivs den Dateipfad machen.

    ``image_url`` steht als ``/studio/bilder/...`` in der Datenbank - das ist
    die Adresse, unter der der Webserver liefert, nicht der Ort auf der Platte.
    """
    url = (getattr(design, "image_url", None) or "").strip()
    if not url:
        raise MotivFehler("Dieses Motiv hat kein Bild.")
    rel = url.split("/studio/bilder/", 1)[-1].lstrip("/")
    pfad = bildordner / rel
    if not pfad.is_file():
        raise MotivFehler(f"Bilddatei nicht gefunden: {rel}")
    return pfad


def pruefe(design: Any, *, produkttyp_key: str, bildordner: Path) -> Pruefung:
    """Ginge dieses Motiv als dieses Produkt? Aendert NICHTS.

    Beantwortet die Frage vor dem Klick, statt sie beim Anlegen zu beantworten -
    ein abgebrochener Printify-Aufruf hinterlaesst sonst ein halbes Produkt.
    """
    if produkttyp_key not in PRODUCT_TYPES:
        erlaubt = ", ".join(PRODUCT_TYPES)
        raise MotivFehler(f"Unbekannter Produkttyp '{produkttyp_key}'. Moeglich: {erlaubt}")

    format_key = FORMAT_JE_TYP.get(produkttyp_key)
    if format_key is None:
        raise MotivFehler(
            f"Fuer '{produkttyp_key}' ist kein Druckformat hinterlegt. "
            "Ohne Format laesst sich nicht sagen, ob die Aufloesung reicht."
        )
    ziel = zielformat(format_key)

    try:
        pfad = bildpfad(design, bildordner)
    except MotivFehler as exc:
        return Pruefung(False, produkttyp_key, format_key, grund=str(exc))

    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(pfad) as bild:
            motiv = trimme(bild)
            breite, hoehe = motiv.width, motiv.height
            # Quer gegen hoch: der Umrechner schneidet bewusst NICHT zu - bei
            # einer Tasse gingen sonst zwei Drittel des Motivs verloren.
            if ziel.quer and hoehe > breite * 1.2:
                return Pruefung(
                    False, produkttyp_key, format_key, breite=breite, hoehe=hoehe,
                    zielmasse=ziel.groesse,
                    grund=(f"Hochformat passt nicht auf {ziel.label}. Ein Zuschnitt "
                           "wuerde rund zwei Drittel des Motivs verlieren. Dafuer "
                           "braucht es ein eigenes Querlayout."))
            try:
                dpi = pruefe_aufloesung(motiv, ziel)
            except FormatFehler as exc:
                return Pruefung(False, produkttyp_key, format_key, breite=breite,
                                hoehe=hoehe, zielmasse=ziel.groesse, grund=str(exc))
    except (UnidentifiedImageError, OSError):
        # Kommt vor: eine Datei mit 0 Bytes aus einem abgebrochenen Lauf. Sie
        # sieht im Ordner aus wie ein Bild und ist keines. Das gehoert gemeldet,
        # nicht als Absturz - sonst reisst ein kaputtes Motiv die ganze Liste mit.
        groesse = pfad.stat().st_size if pfad.exists() else 0
        return Pruefung(False, produkttyp_key, format_key, zielmasse=ziel.groesse,
                        grund=(f"Die Bilddatei laesst sich nicht lesen "
                               f"({pfad.name}, {groesse} Bytes). Vermutlich ein "
                               "abgebrochener Schreibvorgang - das Motiv muss "
                               "neu erzeugt werden."))

    return Pruefung(True, produkttyp_key, format_key, breite=breite, hoehe=hoehe,
                    dpi=round(dpi, 1), zielmasse=ziel.groesse)


def erzeuge_druckdatei(design: Any, *, produkttyp_key: str, bildordner: Path) -> Path:
    """Das Motiv ins Druckformat bringen und ablegen. Kein Netzzugriff.

    Wirft ``MotivFehler``, wenn es nicht geht - dieselbe Pruefung wie in
    ``pruefe()``, nur diesmal mit Folgen.
    """
    ergebnis = pruefe(design, produkttyp_key=produkttyp_key, bildordner=bildordner)
    if not ergebnis.moeglich:
        raise MotivFehler(ergebnis.grund or "Motiv nicht druckfaehig.")

    quelle = bildpfad(design, bildordner)
    from PIL import Image

    with Image.open(quelle) as bild:
        fertig = platziere(bild, ergebnis.format_key)

    name = f"{quelle.stem}__{ergebnis.format_key}.png"
    ziel = bildordner / DRUCK_ORDNER / name
    speichere(fertig, ziel)
    logger.info("Druckdatei erzeugt", extra={"motiv": getattr(design, "id", None),
                                             "format": ergebnis.format_key,
                                             "datei": name})
    return ziel


async def lege_an(design: Any, *, produkttyp_key: str, bildordner: Path,
                  beschreibung: str | None = None, client: Any = None) -> dict:
    """Druckdatei erzeugen und als Printify-ENTWURF anlegen.

    Der Entwurf wird nicht veroeffentlicht - das bleibt ein eigener Klick bei
    Printify. Propose-only gilt auch hier.
    """
    from app.studio.printify.service import erstelle_produkt

    druckdatei = erzeuge_druckdatei(design, produkttyp_key=produkttyp_key,
                                    bildordner=bildordner)
    titel = (getattr(design, "title", None) or "Motiv").strip()[:120]
    ergebnis = await erstelle_produkt(
        bild_pfad=druckdatei,
        titel=titel,
        beschreibung=beschreibung or titel,
        produkttyp_key=produkttyp_key,
        client=client,
    )
    return {
        "printify_id": ergebnis.printify_id,
        "titel": ergebnis.titel,
        "produkttyp": ergebnis.produkttyp,
        "preis_cents": ergebnis.preis_cents,
        "kosten_cents": ergebnis.kosten_cents,
        "varianten": ergebnis.varianten,
        "mockups": ergebnis.mockups,
        "druckdatei": druckdatei.name,
    }
