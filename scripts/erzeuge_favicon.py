"""Das Browser- und Taskleisten-Symbol als echtes ICO erzeugen.

Warum noetig: Die Seite verweist auf ein SVG, und der Server lieferte unter
/favicon.ico ebenfalls das SVG - mit der Typangabe image/svg+xml. Der Reiter im
Browser kommt damit zurecht, die WINDOWS-TASKLEISTE nicht: Wer die Seite
anheftet, bekommt dort weiterhin das alte Symbol, weil das angebotene nicht
lesbar ist.

Ein ICO traegt mehrere Groessen in einer Datei. Windows sucht sich die passende:
16 fuer die Reiterleiste, 32 fuer die Taskleiste, 48 und groesser fuer
Verknuepfungen auf dem Schreibtisch.

Gezeichnet wird nicht das volle Monogramm aus dem SVG - der duenne Kreisring
verschwimmt bei 16 Pixeln zu einem grauen Fleck. Stattdessen die kraeftige
Fassung: gefuellte Flaeche, zwei Buchstaben. Das ist dasselbe Zeichen, nur fuer
kleine Groessen gebaut.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WURZEL = Path(__file__).resolve().parents[1]
ZIEL = WURZEL / "app" / "static" / "favicon.ico"

# Dieselben Werte wie in der Oberflaeche: dunkles Cyan als Grund, helles als
# Zeichen. Nicht die alten Gruen-Gold-Werte.
GRUND = (18, 51, 60, 255)      # #12333c
ZEICHEN = (25, 167, 189, 255)  # #19a7bd

GROESSEN = [256, 128, 64, 48, 32, 16]

# Windows-Schriften, fett und breit genug fuer zwei Buchstaben auf kleinem Raum.
SCHRIFTEN = [
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\calibrib.ttf",
]


def _schrift(groesse: int):
    for pfad in SCHRIFTEN:
        if Path(pfad).is_file():
            try:
                return ImageFont.truetype(pfad, groesse)
            except OSError:
                continue
    return ImageFont.load_default()


def zeichne(kante: int) -> Image.Image:
    # Vierfach gross zeichnen und herunterrechnen - so werden die Rundungen und
    # Buchstabenkanten glatt statt treppig.
    f = 4
    gross = kante * f
    bild = Image.new("RGBA", (gross, gross), (0, 0, 0, 0))
    stift = ImageDraw.Draw(bild)

    radius = int(gross * 0.22)
    stift.rounded_rectangle([0, 0, gross - 1, gross - 1], radius=radius, fill=GRUND)

    # Schriftgroesse so, dass "DH" die Flaeche fuellt, ohne anzustossen.
    text = "DH"
    groesse = int(gross * 0.52)
    schrift = _schrift(groesse)
    links, oben, rechts, unten = stift.textbbox((0, 0), text, font=schrift)
    breite, hoehe = rechts - links, unten - oben
    # Bei sehr kleinen Kanten kann die Vorgabeschrift zu breit sein - dann kuerzen.
    while breite > gross * 0.78 and groesse > 8:
        groesse = int(groesse * 0.92)
        schrift = _schrift(groesse)
        links, oben, rechts, unten = stift.textbbox((0, 0), text, font=schrift)
        breite, hoehe = rechts - links, unten - oben

    stift.text(((gross - breite) / 2 - links, (gross - hoehe) / 2 - oben),
               text, font=schrift, fill=ZEICHEN)
    return bild.resize((kante, kante), Image.LANCZOS)


def main() -> int:
    bilder = [zeichne(k) for k in GROESSEN]
    # Pillow schreibt alle Groessen in EINE ICO-Datei.
    bilder[0].save(ZIEL, format="ICO",
                   sizes=[(k, k) for k in GROESSEN])
    print(f"Geschrieben: {ZIEL.relative_to(WURZEL)}")
    print(f"  Groessen  : {', '.join(str(k) for k in GROESSEN)}")
    print(f"  Datei     : {ZIEL.stat().st_size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
