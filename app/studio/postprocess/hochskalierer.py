"""Ein Motiv auf Druckgroesse bringen.

Das fehlende Stueck. Der Umrechner nebenan platziert ein Motiv im Zielformat und
BRICHT AB, wenn die Aufloesung nicht reicht - richtig so, denn Printify wuerde
sonst klaglos matschig drucken und die Retoure kaeme zu uns. Nur: kein einziges
erzeugtes Motiv kam je durch diese Pruefung.

Die Rechnung dahinter ist unerbittlich. Textildruck verlangt 150 DPI ueber 15
Zoll Druckbreite, also 2250 Pixel. GPT Image 2 liefert hoechstens 1536. Macht 66
DPI - Faktor 2,3 zu wenig. Bei der Tasse (300 DPI) fehlt der Faktor 2,7.

Zwei Wege heraus, und dieses Modul kennt beide:

* **Vergroessern** (hier). Erfindet keine Bildinformation, aber es macht Kanten
  nicht kaputt. Bei Flaechen- und Linienmotiven - genau dem, was auf Textil
  landet - ist das Ergebnis sehr gut, weil solche Motive kaum echte Details
  haben, die verloren gehen koennten. Bei einem fotorealistischen Motiv wird es
  weich.
* **Gleich gross erzeugen** (siehe ``generation/``). Immer der bessere Weg, wenn
  der Anbieter es kann. Fuer die bereits vorhandenen Motive kommt er zu spaet.

**Dieses Modul luegt nicht ueber das Ergebnis.** Es liefert zu jedem Ergebnis
mit, um welchen Faktor vergroessert wurde und ob echte Pixel dahinterstehen
(Eiserne Regel 3). Ein um Faktor 4 aufgeblasenes Motiv ist etwas anderes als
eines, das so erzeugt wurde, und der Nutzer soll das sehen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PIL import Image

logger = logging.getLogger("app.studio.hochskalierer")

#: Ueber diesem Faktor wird auch ein Flaechenmotiv sichtbar weich. Kein Verbot,
#: sondern eine Warnung im Ergebnis - entscheiden soll der Mensch.
WARNSCHWELLE_FAKTOR = 3.0

#: Groesser als das machen wir nicht. 4500x5400 ist die Druckvorgabe fuer Textil,
#: Poster gehen bis 7200x10800. Darueber wird die Datei unhandlich, ohne dass ein
#: Drucker etwas davon haette.
MAX_KANTE = 10800


class SkalierFehler(ValueError):
    """Das Motiv laesst sich nicht auf die gewuenschte Groesse bringen."""


@dataclass(frozen=True)
class Ergebnis:
    """Was beim Vergroessern herauskam - samt Ehrlichkeit ueber das Wie."""

    bild: Image.Image
    faktor: float
    quelle_breite: int
    quelle_hoehe: int
    verfahren: str
    #: True, wenn ueber der Warnschwelle vergroessert wurde.
    weich: bool

    @property
    def hinweis(self) -> str:
        """Ein Satz, der im Studio neben dem Motiv stehen kann."""
        if self.faktor <= 1.0:
            return "Original ohne Vergroesserung."
        text = (f"{self.faktor:.1f}-fach vergroessert von "
                f"{self.quelle_breite}x{self.quelle_hoehe} ({self.verfahren}).")
        if self.weich:
            text += (" Ueber dem Dreifachen wird auch ein Flaechenmotiv sichtbar weich - "
                     "fuer den Verkauf lieber gleich gross neu erzeugen.")
        return text


def noetige_breite(min_dpi: int, druckbreite_zoll: float) -> int:
    """Wie breit ein Motiv sein muss, damit es die Druckpruefung besteht."""
    return int(round(min_dpi * druckbreite_zoll))


def faktor_fuer(bild: Image.Image, ziel_breite: int) -> float:
    """Um welchen Faktor dieses Motiv wachsen muesste."""
    if bild.width <= 0:
        raise SkalierFehler("Motiv hat keine Breite.")
    return ziel_breite / bild.width


def vergroessere(bild: Image.Image, *, ziel_breite: int,
                 verfahren: str = "lanczos") -> Ergebnis:
    """Ein Motiv proportional auf ``ziel_breite`` bringen.

    Immer proportional - die Hoehe folgt der Breite. Ungleiche Faktoren gibt es
    hier so wenig wie im Umrechner: Genau daran sind die 13 Altmotive kaputt
    gegangen, die von 1024x1536 auf 4500x5400 gezogen wurden und dabei 25 Prozent
    in die Breite liefen.
    """
    if ziel_breite <= 0:
        raise SkalierFehler("Zielbreite muss groesser als 0 sein.")
    quelle_b, quelle_h = bild.width, bild.height
    faktor = faktor_fuer(bild, ziel_breite)

    ziel_hoehe = int(round(quelle_h * faktor))
    if max(ziel_breite, ziel_hoehe) > MAX_KANTE:
        raise SkalierFehler(
            f"Zielgroesse {ziel_breite}x{ziel_hoehe} ueberschreitet die Grenze von "
            f"{MAX_KANTE} Pixeln. So gross druckt niemand."
        )

    if faktor <= 1.0:
        # Schon gross genug. NICHT verkleinern - das waere ein Verlust ohne Not;
        # das Platzieren im Zielformat verkleinert ohnehin proportional.
        return Ergebnis(bild.convert("RGBA"), 1.0, quelle_b, quelle_h, "unveraendert", False)

    gross = bild.convert("RGBA").resize((ziel_breite, ziel_hoehe), Image.LANCZOS)
    logger.info("Motiv vergroessert", extra={"faktor": round(faktor, 2),
                                             "von": f"{quelle_b}x{quelle_h}",
                                             "auf": f"{ziel_breite}x{ziel_hoehe}"})
    return Ergebnis(gross, faktor, quelle_b, quelle_h, verfahren,
                    faktor > WARNSCHWELLE_FAKTOR)


def fuer_zielformat(bild: Image.Image, ziel) -> Ergebnis:
    """Ein Motiv so gross machen, dass es die Pruefung fuer ``ziel`` besteht.

    ``ziel`` ist ein ``Zielformat`` aus dem Umrechner. Gerechnet wird auf die
    Mindestbreite, nicht auf die Leinwandbreite: Ein Brustmotiv nutzt nur einen
    Teil der Druckflaeche, und mehr Pixel als noetig kosten nur Speicher.

    Die Zielbreite ist trotzdem mindestens die Leinwandbreite, wenn das Motiv
    randfuellend gedruckt wird (Tasse, Poster) - dort ist die Druckbreite die
    Leinwandbreite.
    """
    gebraucht = noetige_breite(ziel.min_dpi, ziel.druckbreite_zoll)
    if not ziel.transparent:
        gebraucht = max(gebraucht, ziel.breite)
    return vergroessere(bild, ziel_breite=gebraucht)
