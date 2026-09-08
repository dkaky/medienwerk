"""Ein Motiv in eine SVG-Datei umwandeln.

Wozu: Eine SVG-Datei besteht aus Formen, nicht aus Pixeln. Sie laesst sich
beliebig vergroessern, ohne weich zu werden - man kann sie also selbst
ausdrucken, auf jede Groesse ziehen und an einen Schneideplotter geben, ohne je
wieder ueber DPI nachzudenken. Genau dafuer ist sie hier.

**Was hier passiert, ist Nachzeichnen, keine echte Vektorgrafik.** Bildmodelle
liefern Pixel. ``vtracer`` legt Farbflaechen zusammen und zieht Umrisse darum.
Bei dem, was auf Textil landet - Flaechen, Linien, klare Kanten - kommt dabei
etwas sehr Brauchbares heraus. Bei einem Foto oder einem weich verlaufenden
Motiv entsteht ein Haufen ueberlagerter Flecken: gross, langsam, haesslich.
Die Funktion sagt das im Ergebnis, statt es den Nutzer beim Drucken merken zu
lassen.

Die drei Stufen sind gemessen, nicht geraten (Bergmotiv, 1024x1024, 08.09.2026):

===========  ==========  ============  ======================================
Stufe        Pfade       Dateigroesse  wofuer
===========  ==========  ============  ======================================
fein         11.206      6,7 MB        Ansehen, Archiv; zu schwer zum Drucken
plakativ      1.193      1,5 MB        Der Normalfall - drucken, skalieren
schnitt           5      0,4 MB        Schneideplotter, Flexfolie, ein Ton
===========  ==========  ============  ======================================

Printify nimmt SVG bis 20 MB und 20.000 Pfade an. "fein" liegt mit 11.000 Pfaden
darunter, ist aber schon unangenehm zu handhaben - deshalb ist "plakativ" die
Vorgabe.

**Nicht vor dem Nachzeichnen vergroessern.** Dieselbe Messung mit vorher auf
2250 Pixel vergroesserter Vorlage: fein 46.161 Pfade / 32 MB, plakativ 3.361 /
6,6 MB, schnitt 22 / 1,3 MB. Drei- bis vierfache Groesse ohne Gewinn, und "fein"
faellt dadurch aus den Printify-Grenzen. Der Grund: Die Vergroesserung rechnet
weiche Uebergaenge an jede Kante, und diese Zwischentoene werden hier zu
zusaetzlichen Flaechen. Die Schaerfe einer SVG kommt nicht aus den Pixeln der
Vorlage, sondern daraus, dass am Ende Formen stehen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("app.studio.vektor")


class VektorFehler(ValueError):
    """Das Motiv liess sich nicht nachzeichnen."""


#: Grenzen, die Printify fuer SVG-Uploads nennt. Wer darueber liegt, bekommt die
#: Datei nicht hochgeladen - besser hier melden als dort scheitern.
MAX_PFADE = 20_000
MAX_BYTES = 20 * 1024 * 1024

#: Ab so vielen Pfaden wird die Datei unhandlich, auch wenn sie formal passt.
VIELE_PFADE = 6_000


@dataclass(frozen=True)
class Stufe:
    """Eine Nachzeichen-Einstellung mit einem Zweck."""

    key: str
    label: str
    zweck: str
    farbmodus: str
    optionen: dict


STUFEN: dict[str, Stufe] = {
    "fein": Stufe(
        "fein", "Fein (viele Farbstufen)",
        "Kommt dem Original am naechsten. Grosse Datei - zum Ansehen, nicht zum Drucken.",
        "color",
        dict(color_precision=8, filter_speckle=4, path_precision=8,
             corner_threshold=60, layer_difference=16),
    ),
    "plakativ": Stufe(
        "plakativ", "Plakativ (Vorgabe)",
        "Glatte Flaechen, wenige Farbstufen. Der Normalfall zum Drucken und Skalieren.",
        "color",
        dict(color_precision=5, filter_speckle=12, path_precision=6,
             corner_threshold=80, layer_difference=32),
    ),
    "schnitt": Stufe(
        "schnitt", "Schnittkontur (einfarbig)",
        "Eine Farbe, harte Kanten - fuer Schneideplotter und Flexfolie.",
        "binary",
        dict(filter_speckle=20, path_precision=4, corner_threshold=90),
    ),
}


@dataclass(frozen=True)
class Vektorisiert:
    """Ergebnis des Nachzeichnens - mit ehrlicher Auskunft ueber die Brauchbarkeit."""

    pfad: Path
    stufe: str
    pfade: int
    bytes: int

    @property
    def zu_gross(self) -> bool:
        return self.pfade > MAX_PFADE or self.bytes > MAX_BYTES

    @property
    def hinweis(self) -> str:
        kb = self.bytes / 1024
        groesse = f"{kb/1024:.1f} MB" if kb >= 1024 else f"{kb:.0f} KB"
        text = f"{self.pfade:,} Pfade, {groesse}.".replace(",", ".")
        if self.zu_gross:
            text += (f" ZU GROSS fuer Printify (Grenze {MAX_PFADE:,} Pfade / 20 MB) - "
                     "eine gröbere Stufe nehmen.").replace(",", ".")
        elif self.pfade > VIELE_PFADE:
            text += (" Sehr viele Pfade - laesst sich oeffnen, aber traege bearbeiten. "
                     "Fuer den Druck reicht meist die Stufe 'plakativ'.")
        return text


def stufe(key: str) -> Stufe:
    s = STUFEN.get(key)
    if s is None:
        raise VektorFehler(f"Unbekannte Stufe '{key}'. Moeglich: {', '.join(STUFEN)}")
    return s


def zeichne_nach(bildpfad: Path | str, zielpfad: Path | str, *,
                 stufe_key: str = "plakativ") -> Vektorisiert:
    """Ein PNG in eine SVG-Datei nachzeichnen.

    Bewusst dateibasiert: ``vtracer`` arbeitet auf Dateien, und die Druckdatei
    liegt ohnehin schon auf der Platte. Ein Umweg ueber den Speicher braechte
    nichts ausser einer Kopie.
    """
    try:
        import vtracer
    except ImportError as exc:  # pragma: no cover - optionale Abhaengigkeit
        raise VektorFehler(
            "vtracer ist nicht installiert (pip install vtracer). Ohne das Paket "
            "gibt es keine SVG-Ausgabe."
        ) from exc

    s = stufe(stufe_key)
    quelle, ziel = Path(bildpfad), Path(zielpfad)
    if not quelle.is_file():
        raise VektorFehler(f"Bilddatei nicht gefunden: {quelle.name}")
    ziel.parent.mkdir(parents=True, exist_ok=True)

    try:
        vtracer.convert_image_to_svg_py(str(quelle), str(ziel),
                                        colormode=s.farbmodus, **s.optionen)
    except Exception as exc:  # noqa: BLE001 - Fremdbibliothek, Fehler lesbar machen
        raise VektorFehler(f"Nachzeichnen fehlgeschlagen: {exc}") from exc

    if not ziel.is_file():
        raise VektorFehler("Nachzeichnen lief durch, hat aber keine Datei erzeugt.")

    text = ziel.read_text(encoding="utf-8", errors="replace")
    ergebnis = Vektorisiert(ziel, s.key, text.count("<path"), ziel.stat().st_size)
    logger.info("Motiv nachgezeichnet", extra={"stufe": s.key, "pfade": ergebnis.pfade,
                                               "datei": ziel.name})
    return ergebnis
