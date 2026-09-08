"""Einmal-Lauf: die breitgezogenen Altmotive entzerren.

Sie wurden von 1024x1536 auf 4500x5400 gestreckt, also um Faktor 1,25 in die
Breite. Hier wird zurueckgestaucht und mittig auf die Zielleinwand gesetzt.
Die Originale bleiben unangetastet.
"""
import sys
from pathlib import Path

# Wie die uebrigen Skripte des Projekts: den Projektordner bekannt machen,
# sonst findet Python das Paket app nicht.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from app.studio.postprocess import platziere, repariere_gestreckt, speichere, zielformat

quelle = Path("uebernommen/designs")
ziel = Path("data/studio_images/repariert")
ziel.mkdir(parents=True, exist_ok=True)
textil = zielformat("textil")

gerettet = uebersprungen = 0
for datei in sorted(quelle.glob("*.png")):
    with Image.open(datei) as bild:
        b, h = bild.size
        if abs(b / h - 0.833) > 0.02:
            print(f"  {datei.name[:38]:40} {b}x{h} nicht betroffen")
            uebersprungen += 1
            continue
        entzerrt = repariere_gestreckt(bild)
        fertig = platziere(entzerrt, textil, pruefen=False)
        speichere(fertig, ziel / datei.name)
    print(f"  {datei.name[:38]:40} entzerrt auf {entzerrt.width}x{entzerrt.height}, Leinwand {fertig.size[0]}x{fertig.size[1]}")
    gerettet += 1

print(f"\nGerettet: {gerettet} | Uebersprungen: {uebersprungen}")
