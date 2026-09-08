"""Alle Motivbilder aus uebernommen/ in den Studio-Bildordner holen.

Warum noetig: Die Uebernahme der Motivdaten (scripts/uebernimm_altmotive.py) hat
die Datenbankzeilen angelegt, aber fuer 14 Motive nur ein 600er Vorschaubild
gefunden - der Webserver liefert nur aus data/studio_images/ aus, und dort lagen
die Originale nicht. Die ECHTEN Bilder (1024x1536) liegen in
uebernommen/pod-designs-angler* und wurden uebersehen, weil ihre Dateinamen das
Wort "angler" nicht enthalten: sie heissen 01-nur-noch-ein-wurf.png.

Was der Lauf tut:

1. Kopiert jedes Motivbild aus uebernommen/ nach data/studio_images/uebernommen/.
2. Haengt vorhandene Motive auf das BESTE verfuegbare Bild um - Reihenfolge:
   druckfaehig (>= 3000 px) schlaegt Original schlaegt Vorschau.
3. Legt Motive an, die es in der Datenbank noch nicht gibt.
4. Notiert die echte Aufloesung, damit spaeter niemand ein 1024er Bild fuer eine
   Druckdatei haelt.

Varianten (_white, _cut) werden NICHT als eigene Motive gefuehrt - sie sind
Bearbeitungen desselben Bildes und stehen als Hinweis in den Notizen.

Wiederholbar: schon vorhandene Bilder werden nicht neu kopiert.
"""

from __future__ import annotations

import json
import re
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

WURZEL = Path(__file__).resolve().parents[1]
# Der Ordner ist nach Dokumente umgezogen; das Skript hat seine Arbeit getan.
# Fuer einen erneuten Lauf POD_ARCHIV auf den Archivordner setzen.
QUELLE = Path(os.environ.get(
    "POD_ARCHIV", r"C:\Users\HP\Documents\Marketingagentur POD"))
ZIEL = WURZEL / "data" / "studio_images" / "uebernommen"
DB = WURZEL / "data" / "druckhelden.db"

# Ordner mit echten Motiven. thumbnails/ bleibt draussen: reine Vorschauen.
MOTIVORDNER = [
    "designs",
    "pod-designs-angler",
    "pod-designs-angler-flux",
    "pod-designs-angler-hybrid",
    "Marketingagentur/Print on Demand",
    "Marketingagentur/Print on Demand/Designs",
]
VARIANTEN = ("_white", "_cut")


def guete(breite: int) -> str:
    if breite >= 3000:
        return "druckfaehig"
    if breite >= 900:
        return "original"
    return "vorschau"


def rang(g: str) -> int:
    return {"druckfaehig": 3, "original": 2, "vorschau": 1}.get(g, 0)


def sammle() -> dict[str, dict]:
    """Alle Motivbilder einsammeln, je Stamm das beste behalten."""
    beste: dict[str, dict] = {}
    for ordner in MOTIVORDNER:
        p = QUELLE / ordner
        if not p.exists():
            continue
        for datei in sorted(p.rglob("*.png")) + sorted(p.rglob("*.jpg")):
            variante = next((v for v in VARIANTEN if v in datei.parts), None)
            try:
                with Image.open(datei) as bild:
                    breite, hoehe = bild.width, bild.height
            except Exception:  # noqa: BLE001 - eine kaputte Datei stoppt den Lauf nicht
                continue
            stamm = datei.stem
            eintrag = {
                "pfad": datei, "breite": breite, "hoehe": hoehe,
                "guete": guete(breite), "ordner": ordner, "variante": variante,
            }
            # Varianten nie als Hauptbild waehlen - sie sind Bearbeitungen.
            if variante:
                beste.setdefault(stamm, {}).setdefault("varianten", []).append(datei.name)
                continue
            vorher = beste.get(stamm)
            if vorher is None or not vorher.get("pfad"):
                varianten = (vorher or {}).get("varianten", [])
                eintrag["varianten"] = varianten
                beste[stamm] = eintrag
            elif rang(eintrag["guete"]) > rang(vorher["guete"]):
                eintrag["varianten"] = vorher.get("varianten", [])
                beste[stamm] = eintrag
    return {k: v for k, v in beste.items() if v.get("pfad")}


def main() -> int:
    if not DB.exists():
        print(f"Datenbank fehlt: {DB}")
        return 1
    ZIEL.mkdir(parents=True, exist_ok=True)

    bilder = sammle()
    print(f"Gefunden: {len(bilder)} Motivbilder in uebernommen/\n")

    kopiert = 0
    for eintrag in bilder.values():
        ziel = ZIEL / eintrag["pfad"].name
        if not ziel.exists():
            ziel.write_bytes(eintrag["pfad"].read_bytes())
            kopiert += 1
        eintrag["adresse"] = f"/studio/bilder/uebernommen/{eintrag['pfad'].name}"

    con = sqlite3.connect(DB)
    jetzt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Vorhandene Motive: auf das bessere Bild umhaengen
    umgehaengt = 0
    for zid, titel, url, meta in con.execute(
            "SELECT id, title, image_url, meta_json FROM studio_designs").fetchall():
        alt_name = Path(url or "").name
        # Die Vorschaubilder tragen einen Ordner-Praefix, die Originale nicht:
        # angler_03-anglerlatein_600.png gehoert zu 03-anglerlatein.png. Die ZAHL
        # bleibt stehen - sie unterscheidet die Motive voneinander, und ohne sie
        # verschmelzen "03-anglerlatein" und "01-anglerlatein" zu einem.
        stamm = re.sub(r"^(angler_flux_|angler_hybrid_|angler_)", "",
                       Path(alt_name).stem).replace("_600", "")
        treffer = bilder.get(stamm)
        if treffer is None or treffer["adresse"] == url:
            continue
        # Nur umhaengen, wenn das neue Bild BESSER ist
        alt_guete = "vorschau" if "_600" in alt_name else "original"
        if "repariert" in (url or ""):
            alt_guete = "druckfaehig"
        if rang(treffer["guete"]) <= rang(alt_guete):
            continue
        m = json.loads(meta) if meta else {}
        m.update({"bildguete": treffer["guete"],
                  "aufloesung": f"{treffer['breite']}x{treffer['hoehe']}",
                  "herkunft_ordner": treffer["ordner"]})
        if treffer.get("varianten"):
            m["varianten"] = treffer["varianten"]
        m.pop("warnung", None)
        if treffer["guete"] != "druckfaehig":
            m["warnung"] = (f"{treffer['breite']} Pixel breit - Textildruck verlangt 4500. "
                            "Zum Drucken muss das Motiv neu erzeugt oder hochskaliert werden.")
        con.execute("UPDATE studio_designs SET image_url=?, meta_json=?, updated_at=? WHERE id=?",
                    (treffer["adresse"], json.dumps(m, ensure_ascii=False), jetzt, zid))
        umgehaengt += 1

    # Bilder ohne Motiv: neu anlegen
    vorhanden = {Path(u or "").name for (u,) in
                 con.execute("SELECT image_url FROM studio_designs")}
    neu = 0
    for stamm, e in sorted(bilder.items()):
        if e["pfad"].name in vorhanden:
            continue
        titel = stamm.replace("-", " ").replace("_", " ").strip()
        titel = titel[:1].upper() + titel[1:]
        m = {"herkunft": "uebernommen/" + e["ordner"], "bildguete": e["guete"],
             "aufloesung": f"{e['breite']}x{e['hoehe']}"}
        if e.get("varianten"):
            m["varianten"] = e["varianten"]
        if e["guete"] != "druckfaehig":
            m["warnung"] = (f"{e['breite']} Pixel breit - Textildruck verlangt 4500.")
        con.execute(
            "INSERT INTO studio_designs (title, status, source, image_url, meta_json, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (titel[:255], "draft", "uebernommen", e["adresse"],
             json.dumps(m, ensure_ascii=False), jetzt, jetzt))
        neu += 1

    con.commit()
    gesamt = con.execute("SELECT COUNT(*) FROM studio_designs").fetchone()[0]
    nach_guete: dict[str, int] = {}
    for (meta,) in con.execute("SELECT meta_json FROM studio_designs WHERE meta_json IS NOT NULL"):
        try:
            g = json.loads(meta).get("bildguete", "unbekannt")
        except ValueError:
            g = "unbekannt"
        nach_guete[g] = nach_guete.get(g, 0) + 1
    ohne_bild = con.execute(
        "SELECT COUNT(*) FROM studio_designs WHERE image_url IS NULL").fetchone()[0]
    con.close()

    print(f"Bilder kopiert       : {kopiert}")
    print(f"Motive umgehaengt    : {umgehaengt}  (auf ein besseres Bild)")
    print(f"Motive neu angelegt  : {neu}")
    print(f"Motive im Studio     : {gesamt}, davon ohne Bild: {ohne_bild}")
    print(f"Nach Bildguete       : {nach_guete}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
