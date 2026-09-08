"""Motive in Produktformate bringen - ohne sie je zu verzerren.

Das ist der Baustein, dessen Fehlen alle 13 Altmotive ruiniert hat: Sie wurden
von 1024x1536 (Verhaeltnis 0,667) stur auf 4500x5400 (0,833) gezogen, also um
25 Prozent breitgezerrt. Gesichter werden rund, Kreise werden Ellipsen.

Der Umrechner ist deshalb ausdruecklich ein **Platzierer**, kein Strecker:

* Er skaliert nur gleichmaessig. Ungleiche Faktoren sind hier nicht vorgesehen
  und werden von einem Test aktiv ausgeschlossen.
* Was uebrig bleibt, wird aufgefuellt - bei Textil transparent, denn dort wird
  nur gedruckt, wo Farbe ist.
* Reicht die Aufloesung nicht, bricht er ab. Printify wuerde stillschweigend
  drucken und die Retoure kaeme zu uns.

Was er BEWUSST nicht tut: ein Hochformat auf Tassenformat zuschneiden. Dabei
gingen rund zwei Drittel des Motivs verloren, und ein Spruch risse mitten durch.
Tassen brauchen ein eigenes Querlayout - fehlt es, sagt der Umrechner das.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps


class FormatFehler(ValueError):
    """Das Motiv passt nicht zum Zielformat."""


@dataclass(frozen=True)
class Zielformat:
    """Ein Produktformat mit allem, was zum Platzieren noetig ist."""

    key: str
    label: str
    breite: int
    hoehe: int
    # Textil wird nur bedruckt, wo Farbe ist -> transparent auffuellen.
    # Tasse und Poster werden randlos bedruckt -> Farbe noetig.
    transparent: bool = True
    # Unter diesem Wert wird der Druck matschig. Textil vertraegt weniger als
    # Keramik, weil Stoff die Kanten ohnehin weichzeichnet.
    min_dpi: int = 150
    # Auf welcher Breite in Zoll das Motiv gedruckt wird - Grundlage der
    # Aufloesungspruefung.
    druckbreite_zoll: float = 15.0
    quer: bool = False

    @property
    def groesse(self) -> tuple[int, int]:
        return (self.breite, self.hoehe)

    @property
    def verhaeltnis(self) -> float:
        return self.breite / self.hoehe


# Zielmasse als Tabelle, nicht im Code verstreut. Neue Produkte kommen hier
# dazu, nicht an fuenf Stellen im Programm.
FORMATE: dict[str, Zielformat] = {
    "textil": Zielformat(
        "textil", "T-Shirt / Hoodie (Direktdruck)", 4500, 5400,
        transparent=True, min_dpi=150, druckbreite_zoll=15.0,
    ),
    "textil_brust": Zielformat(
        "textil_brust", "Brustlogo klein", 1800, 1800,
        transparent=True, min_dpi=150, druckbreite_zoll=4.0,
    ),
    "tasse": Zielformat(
        "tasse", "Tasse 11 oz (Rundumdruck)", 2700, 1100,
        transparent=False, min_dpi=300, druckbreite_zoll=9.0, quer=True,
    ),
    "poster_2_3": Zielformat(
        "poster_2_3", "Poster 2:3", 3600, 5400,
        transparent=False, min_dpi=150, druckbreite_zoll=12.0,
    ),
}


def zielformat(key: str) -> Zielformat:
    f = FORMATE.get(key)
    if f is None:
        raise FormatFehler(f"Unbekanntes Format '{key}'. Moeglich: {', '.join(FORMATE)}")
    return f


def trimme(bild: Image.Image) -> Image.Image:
    """Leeren Rand wegschneiden - das Motiv bleibt uebrig.

    Danach hat der Master kein Format mehr, nur noch Aufloesung. Das Format
    entsteht erst beim Platzieren. Genau das loest das Formatproblem.
    """
    rgba = bild.convert("RGBA")
    rand = rgba.getbbox()
    return rgba.crop(rand) if rand else rgba


def pruefe_aufloesung(bild: Image.Image, ziel: Zielformat) -> float:
    """Echte Druckaufloesung berechnen. Wirft, wenn sie nicht reicht.

    Gerechnet wird mit den ECHTEN Pixeln des Motivs, nicht mit denen nach dem
    Hochskalieren - Hochskalieren erzeugt keine neuen Bildinformationen.
    """
    dpi = bild.width / ziel.druckbreite_zoll
    if dpi < ziel.min_dpi:
        raise FormatFehler(
            f"Aufloesung zu gering fuer {ziel.label}: {dpi:.0f} DPI bei "
            f"{ziel.druckbreite_zoll:.0f} Zoll Druckbreite, noetig sind "
            f"{ziel.min_dpi}. Motiv groesser erzeugen oder hochskalieren."
        )
    return dpi


def platziere(
    bild: Image.Image,
    ziel: Zielformat | str,
    *,
    hintergrund: tuple[int, int, int, int] | None = None,
    pruefen: bool = True,
) -> Image.Image:
    """Motiv proportional in ein Zielformat setzen.

    Nie verzerrt: Das Seitenverhaeltnis bleibt erhalten, der Rest wird
    aufgefuellt.
    """
    z = zielformat(ziel) if isinstance(ziel, str) else ziel
    motiv = trimme(bild)

    if z.quer and motiv.height > motiv.width * 1.2:
        raise FormatFehler(
            f"Hochformat passt nicht auf {z.label}. Ein Zuschnitt wuerde rund zwei "
            "Drittel des Motivs verlieren. Fuer Tassen ein eigenes Querlayout erzeugen."
        )

    if pruefen:
        pruefe_aufloesung(motiv, z)

    fuellung = hintergrund or ((0, 0, 0, 0) if z.transparent else (255, 255, 255, 255))
    # ImageOps.pad skaliert proportional und fuellt auf - ungleiche Faktoren
    # sind hier gar nicht moeglich.
    return ImageOps.pad(motiv, z.groesse, method=Image.LANCZOS, color=fuellung, centering=(0.5, 0.5))


def repariere_gestreckt(bild: Image.Image, faktor: float = 1.25) -> Image.Image:
    """Ein breitgezogenes Motiv zurueckstauchen.

    Fuer den Altbestand: Die Bilder wurden von 0,667 auf 0,833 gezogen, also um
    Faktor 1,25 in die Breite. Stauchen stellt die Proportionen wieder her -
    ohne das Motiv neu erzeugen zu muessen.
    """
    if faktor <= 0:
        raise FormatFehler("Faktor muss groesser als 0 sein.")
    neue_breite = int(round(bild.width / faktor))
    return bild.convert("RGBA").resize((neue_breite, bild.height), Image.LANCZOS)


def speichere(bild: Image.Image, pfad: str | Path, dpi: int = 300) -> Path:
    """Als PNG mit sRGB und DPI-Angabe ablegen.

    Die DPI-Angabe ist fuer den Druck selbst bedeutungslos - gedruckt wird nach
    Pixeln geteilt durch Zoll. Manche Pruefwerkzeuge lesen sie aber aus.
    """
    ziel = Path(pfad)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    bild.convert("RGBA").save(ziel, "PNG", dpi=(dpi, dpi))
    return ziel
