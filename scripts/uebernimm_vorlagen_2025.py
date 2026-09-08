"""Die Skelett-Bilder von 2025 als VORLAGEN uebernehmen - nicht als Motive.

Quelle: ``C:\\Users\\HP\\Documents\\Marketingagentur POD\\Marketingagentur\\
Print on Demand`` (12 Bilder, April/Mai 2025). Sie stammen aus einer Zeit vor
diesem Projekt und wurden nie uebernommen: ``scripts/uebernimm_altmotive.py``
holte etwas anderes (die 33 Motive aus der Datenbank des geloeschten
POD-Shop-Ordners), ``scripts/hole_altbilder.py`` dessen Bilder.

Warum "Vorlage" und nicht "Motiv"
---------------------------------
Acht der zwoelf Bilder sind **keine Druckdateien, sondern T-Shirt-Fotos**: das
Motiv ist bereits auf ein Shirt gedruckt, der Stoff samt Aermeln ist im Bild.
So etwas laesst sich nicht drucken - man druckte ein Foto eines Shirts auf ein
Shirt. Genau der Fehler, den der Nutzer am 01.09.2026 beschrieb: "erstell mir
ein fenerbahce logo auf einem tshirt" - und es kam ein Shirt.

Nutzerwunsch, woertlich: **"Auch wenn diese motive jetzt nicht nutzbar sind,
werden sie in zukunft nutzbar sein wenn wir zb sagen, schau dir das an und
erstell ein neues motiv was so aehnlich ist mit unseren parametern aber."**

Deshalb werden sie uebernommen, aber ehrlich beschriftet: ``art: "vorlage"``
und ``druckfaehig: false`` mit Begruendung. So landet keins davon versehentlich
im Druckweg, und als Ideengeber stehen sie trotzdem bereit.

Einmal laufen lassen:

    .venv\\Scripts\\python.exe scripts\\uebernimm_vorlagen_2025.py

Der Lauf ist wiederholbar - was schon in der Datenbank steht, wird uebersprungen.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings          # noqa: E402
from app.database import SessionLocal        # noqa: E402
from app.studio import service               # noqa: E402

QUELLE = Path(os.environ.get(
    "VORLAGEN_QUELLE",
    r"C:\Users\HP\Documents\Marketingagentur POD\Marketingagentur\Print on Demand",
))

#: Unterordner im Bilderlager. Bewusst getrennt von den echten Motiven - eine
#: Vorlage ist kein Werk, aus dem gedruckt wird.
UNTERORDNER = "vorlagen"

# Was auf den Bildern zu sehen ist. Von Hand vergeben, weil die Dateinamen
# ("ChatGPT Image Apr 21, 2025, 10_06_56 PM.png") nichts hergeben.
# Zweiter Wert: zeigt das Bild ein Kleidungsstueck? Dann ist es keine Druckdatei.
BESCHREIBUNG: dict[str, tuple[str, bool, str]] = {
    "ChatGPT Image Apr 21, 2025, 10_06_56 PM.png": (
        "Rennendes Skelett mit Fahne - ALMOST THERE", True, ""),
    "ChatGPT Image Apr 21, 2025, 10_10_10 PM.png": (
        "Skelett am Roulette - SPIN IT TIL WUIN IT", True,
        "Schreibfehler im Bild: 'WUIN' statt 'WIN'"),
    "ChatGPT Image Apr 21, 2025, 10_11_36 PM.png": (
        "Skelett am Roulette - SPIN IT TIL UVVIN IT", True,
        "Schreibfehler im Bild: 'UVVIN' statt 'WIN'"),
    "ChatGPT Image Apr 21, 2025, 10_14_32 PM.png": (
        "Skelett mit Zylinder am Roulette - SPIN IT TIL YOU WIN IT (farbig)", True, ""),
    "ChatGPT Image Apr 21, 2025, 10_17_07 PM.png": (
        "Skelett mit Zylinder am Roulette - SPIN IT TIL YOU WIN IT", True, ""),
    "ChatGPT Image Apr 21, 2025, 10_26_38 PM.png": (
        "Tarotkarte MEMENTO JACKPOT - Fortune Favors the Dead", True, ""),
    "ChatGPT Image Apr 21, 2025, 10_32_52 PM.png": (
        "Tarotkarte XIII - JACKPOT", True, ""),
    "ChatGPT Image Apr 21, 2025, 10_49_23 PM.png": (
        "Tarotkarte X - THE CLIMBER (Skelett auf der Leiter zum Dollarzeichen)", True, ""),
    "ChatGPT Image May 12, 2025, 10_19_27 AM.png": (
        "Skelett im rosa Cabrio mit Blondine und Dollarzeichen", False, ""),
    "ChatGPT Image May 12, 2025, 11_12_04 AM.png": (
        "Skelett im rosa Cabrio, Geldspur hinter dem Wagen", False, ""),
    "Eyes on the Prize 1.png": (
        "Eyes on the Prize - Skelett im Cabrio vor blauem Himmel", False,
        "Hintergrundkasten im Bild, nicht freigestellt"),
    "Skeletons Cruising to Wealth.png": (
        "Skeletons Cruising to Wealth - Skelett im Cabrio", False, ""),
}


def sicherer_name(pfad: Path) -> str:
    """Dateiname ohne Leerzeichen und Kommata.

    Der Ursprungsname ("ChatGPT Image Apr 21, 2025, 10_06_56 PM.png") macht als
    Web-Adresse Aerger und sagt ohnehin nichts ueber den Inhalt.
    """
    kern = "".join(z if z.isalnum() else "-" for z in pfad.stem.lower())
    while "--" in kern:
        kern = kern.replace("--", "-")
    return "vorlage-" + kern.strip("-")[:60] + pfad.suffix.lower()


def main() -> int:
    if not QUELLE.is_dir():
        print(f"Quellordner nicht gefunden: {QUELLE}")
        return 1

    ziel_wurzel = Path(get_settings().studio_image_dir) / UNTERORDNER
    ziel_wurzel.mkdir(parents=True, exist_ok=True)

    dateien = sorted(
        p for p in QUELLE.rglob("*")
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
    )
    if not dateien:
        print("Keine Bilddateien gefunden.")
        return 1

    db = SessionLocal()
    neu = uebersprungen = 0
    try:
        vorhanden = {
            d.image_url for d in service.list_designs(db, limit=1000) if d.image_url
        }
        for pfad in dateien:
            beschr = BESCHREIBUNG.get(pfad.name)
            if beschr is None:
                # Neue Datei im Quellordner: nicht raten, sondern melden.
                print(f"  ? unbekannte Datei, uebersprungen: {pfad.name}")
                uebersprungen += 1
                continue
            titel, zeigt_shirt, hinweis = beschr

            sicher = sicherer_name(pfad)
            web = f"/studio/bilder/{UNTERORDNER}/{sicher}"
            if web in vorhanden:
                print(f"  = schon da: {titel[:52]}")
                uebersprungen += 1
                continue

            shutil.copy2(pfad, ziel_wurzel / sicher)
            gruende = []
            if zeigt_shirt:
                gruende.append("Shirt-Foto statt freigestelltem Motiv")
            if hinweis:
                gruende.append(hinweis)
            service.create_design(
                db,
                title=titel,
                source="vorlage-2025",
                image_url=web,
                meta_json=json.dumps({
                    "art": "vorlage",
                    "druckfaehig": False,
                    "grund": "; ".join(gruende) or "als Ideengeber uebernommen",
                    "zeigt_kleidungsstueck": zeigt_shirt,
                    "herkunft": "Marketingagentur POD (April/Mai 2025)",
                    "urspruenglicher_dateiname": pfad.name,
                }, ensure_ascii=False),
            )
            neu += 1
            print(f"  + {titel[:60]}")
    finally:
        db.close()

    print(f"\n{neu} Vorlagen uebernommen, {uebersprungen} uebersprungen.")
    print("Alle sind als 'art: vorlage' und 'druckfaehig: false' gekennzeichnet -")
    print("Ideengeber, keine Druckdateien.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
