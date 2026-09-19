"""Das Browser- und Taskleisten-Symbol (favicon.ico und favicon.svg) erzeugen.

Das Zeichen ist das rote Quadrat aus dem Logo von Medienwerk. Die ganze Wortmarke
waere bei 16 Pixeln unlesbar; das Quadrat traegt die Marke allein.

Ein ICO traegt mehrere Groessen in einer Datei: 16 fuer die Reiterleiste, 32 fuer
die Taskleiste, 48 und mehr fuer Verknuepfungen. Windows sucht sich die passende.

Aufruf: python scripts/erzeuge_favicon.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

WURZEL = Path(__file__).resolve().parents[1]
STATIC = WURZEL / "app" / "static"
ROT = (214, 43, 49, 255)      # #d62b31, wie --brand im hellen Schema
GROESSEN = [256, 128, 64, 48, 32, 16]


def zeichne(kante: int) -> Image.Image:
    f = 4                                     # vierfach zeichnen, dann verkleinern: glatte Kanten
    gross = kante * f
    bild = Image.new("RGBA", (gross, gross), (0, 0, 0, 0))
    rand = round(gross * 0.14)
    ImageDraw.Draw(bild).rounded_rectangle(
        (rand, rand, gross - rand - 1, gross - rand - 1), radius=round(gross * 0.06), fill=ROT)
    return bild.resize((kante, kante), Image.LANCZOS)


def main() -> None:
    zeichne(256).save(STATIC / "favicon.ico", format="ICO", sizes=[(g, g) for g in GROESSEN])
    (STATIC / "favicon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<rect x="14" y="14" width="72" height="72" rx="6" fill="#d62b31"/></svg>',
        encoding="utf-8")
    print("favicon.ico und favicon.svg geschrieben")


if __name__ == "__main__":
    main()
