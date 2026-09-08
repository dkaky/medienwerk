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
    # Seit 08.09.2026: Reicht die Aufloesung nicht, ist das kein blosses Nein mehr.
    # ``mit_vergroesserung`` sagt, dass es MIT Hochskalieren ginge, und ``faktor``
    # um wieviel. Beides bleibt Auskunft - vergroessert wird erst, wenn jemand es
    # ausdruecklich verlangt (``erzeuge_druckdatei(hochskalieren=True)``).
    mit_vergroesserung: bool = False
    faktor: float | None = None
    noetige_breite: int | None = None

    @property
    def weich(self) -> bool:
        """Waere die Vergroesserung so stark, dass man sie sieht?"""
        from app.studio.postprocess.hochskalierer import WARNSCHWELLE_FAKTOR

        return bool(self.faktor and self.faktor > WARNSCHWELLE_FAKTOR)


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
                # Bis 08.09.2026 endete es hier mit einem blossen Nein - und traf
                # damit JEDES erzeugte Motiv, weil kein Bildmodell die noetigen
                # 2250 Pixel liefert. Jetzt steht daneben, was es braeuchte.
                from app.studio.postprocess import hochskalierer

                gebraucht = hochskalierer.noetige_breite(ziel.min_dpi, ziel.druckbreite_zoll)
                faktor = gebraucht / breite if breite else None
                return Pruefung(
                    False, produkttyp_key, format_key, breite=breite, hoehe=hoehe,
                    zielmasse=ziel.groesse, grund=str(exc),
                    mit_vergroesserung=True, faktor=round(faktor, 2) if faktor else None,
                    noetige_breite=gebraucht,
                )
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


def erzeuge_druckdatei(design: Any, *, produkttyp_key: str, bildordner: Path,
                       hochskalieren: bool = False) -> Path:
    """Das Motiv ins Druckformat bringen und ablegen. Kein Netzzugriff.

    Wirft ``MotivFehler``, wenn es nicht geht - dieselbe Pruefung wie in
    ``pruefe()``, nur diesmal mit Folgen.

    ``hochskalieren=True`` erlaubt, ein zu kleines Motiv vorher zu vergroessern.
    Bewusst ein Schalter und nicht die Vorgabe: Vergroessern erfindet keine
    Bildinformation, und der Unterschied zwischen "so erzeugt" und "aufgeblasen"
    gehoert zu den Angaben, die ein Verkaeufer kennen muss. Der Dateiname traegt
    den Faktor mit, damit man es einer Druckdatei spaeter noch ansieht.
    """
    ergebnis = pruefe(design, produkttyp_key=produkttyp_key, bildordner=bildordner)
    if not ergebnis.moeglich and not (hochskalieren and ergebnis.mit_vergroesserung):
        grund = ergebnis.grund or "Motiv nicht druckfaehig."
        if ergebnis.mit_vergroesserung:
            grund += (f" Mit {ergebnis.faktor}-facher Vergroesserung ginge es - "
                      "dafuer 'hochskalieren' setzen.")
        raise MotivFehler(grund)

    quelle = bildpfad(design, bildordner)
    from PIL import Image

    from app.studio.postprocess import hochskalierer as hs

    zusatz = ""
    with Image.open(quelle) as bild:
        if hochskalieren and ergebnis.mit_vergroesserung:
            ziel_format = zielformat(ergebnis.format_key)
            gross = hs.fuer_zielformat(trimme(bild), ziel_format)
            fertig = platziere(gross.bild, ergebnis.format_key)
            zusatz = f"__x{gross.faktor:.1f}".replace(".", "-")
            logger.info("Motiv vor dem Druck vergroessert",
                        extra={"motiv": getattr(design, "id", None),
                               "faktor": round(gross.faktor, 2)})
        else:
            fertig = platziere(bild, ergebnis.format_key)

    name = f"{quelle.stem}__{ergebnis.format_key}{zusatz}.png"
    ziel = bildordner / DRUCK_ORDNER / name
    speichere(fertig, ziel)
    logger.info("Druckdatei erzeugt", extra={"motiv": getattr(design, "id", None),
                                             "format": ergebnis.format_key,
                                             "datei": name})
    return ziel


def erzeuge_svg(design: Any, *, bildordner: Path, stufe: str = "plakativ",
                hochskalieren: bool = False) -> Any:
    """Aus dem Motiv eine SVG-Datei machen - zum Selberdrucken.

    Eine SVG besteht aus Formen statt Pixeln: beliebig vergroesserbar, ohne weich
    zu werden. Damit laesst sich das Motiv selbst ausdrucken, auf jede Groesse
    ziehen und an einen Schneideplotter geben.

    **Ohne Vergroesserung, und das ist gemessen, nicht geraten.** Zuerst stand
    hier das Gegenteil, mit der plausiblen Begruendung, mehr Pixel je Kante
    ergaeben glattere Kurven. Der Versuch am Bergmotiv sagt etwas anderes
    (08.09.2026, gleiche Stufe, einmal so und einmal so):

    ==========  ==============  ==============
    Stufe       ohne Vergr.     mit Vergr.
    ==========  ==============  ==============
    fein        11.206 / 6,7 MB 46.161 / 32 MB
    plakativ     1.193 / 1,5 MB  3.361 / 6,6 MB
    schnitt          5 / 0,4 MB     22 / 1,3 MB
    ==========  ==============  ==============

    Drei- bis vierfache Groesse ohne jeden Gewinn - "fein" fliegt dadurch sogar
    ueber die Printify-Grenze. Der Grund ist einleuchtend, sobald man ihn sieht:
    Die Vergroesserung rechnet weiche Uebergaenge an jede Kante, und genau diese
    Zwischentoene macht der Nachzeichner zu zusaetzlichen Flaechen. Die Schaerfe
    der SVG kommt ohnehin nicht aus den Pixeln, sondern daraus, dass am Ende
    Formen stehen - die skalieren von sich aus.

    Der Schalter bleibt, weil er bei sehr kleinen Vorlagen (unter ~600 Pixel)
    doch etwas bringen kann. Vorgabe ist er nicht mehr.
    """
    from PIL import Image

    from app.studio.postprocess import hochskalierer as hs
    from app.studio.postprocess import vektor

    quelle = bildpfad(design, bildordner)
    zeichen_quelle = quelle

    if hochskalieren:
        ziel_format = zielformat("textil")
        with Image.open(quelle) as bild:
            gross = hs.fuer_zielformat(trimme(bild), ziel_format)
        if gross.faktor > 1.0:
            zeichen_quelle = bildordner / DRUCK_ORDNER / f"{quelle.stem}__vektorvorlage.png"
            speichere(gross.bild, zeichen_quelle)

    ziel = bildordner / DRUCK_ORDNER / f"{quelle.stem}__{stufe}.svg"
    return vektor.zeichne_nach(zeichen_quelle, ziel, stufe_key=stufe)


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
