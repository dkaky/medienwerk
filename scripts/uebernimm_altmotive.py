"""Die 33 Motive aus dem alten POD-Shop in den Studio-Trakt uebernehmen.

Vorgeschichte: Der alte Projektordner wird geloescht. Seine Bilddateien sind
laengst hier, seine DATENBANK aber lag nur als Kopie unter uebernommen/ und wurde
vom laufenden Programm nie gelesen. Damit waeren die eigentlich wertvollen
Angaben verloren gegangen - nicht die Bilder, sondern das Wissen darueber:
welches Motiv freigegeben wurde, welches warum abgelehnt, mit welchem Prompt es
entstand, und zu welchem Printify-Entwurf es fuehrte.

Was uebertragen wird:

* 33 Motive aus ``designs`` -> ``studio_designs``
* die Freigabe-Entscheidung als Status (siehe ``STATUS_ABBILDUNG``)
* Prompt, Modell, Nische, Ablehngrund und die alte Nummer in ``meta_json``
* die 6 Printify-Produktnummern aus ``product_listings``, dem jeweiligen Motiv
  zugeordnet. Das sind ECHTE, drueben bereits angelegte Entwuerfe - ohne diese
  Nummern waere die Verbindung Motiv-zu-Produkt nicht wiederherstellbar.

Der Lauf ist wiederholbar: bereits uebernommene Motive erkennt er an der alten
Nummer in ``meta_json`` und ueberspringt sie.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

WURZEL = Path(__file__).resolve().parents[1]
# Der Ordner ist nach Dokumente umgezogen; das Skript hat seine Arbeit getan.
# Fuer einen erneuten Lauf POD_ARCHIV auf den Archivordner setzen.
QUELLE = Path(os.environ.get(
    "POD_ARCHIV", r"C:\Users\HP\Documents\Marketingagentur POD")) / "data" / "designs.sqlite3"
ZIEL = WURZEL / "data" / "druckhelden.db"
BILDORDNER = WURZEL / "data" / "studio_images"

# Der alte Bestand kannte drei Zustaende, der neue ebenfalls - sie heissen nur
# anders. "rejected" wird bewusst zu "archived" statt geloescht: eine Ablehnung
# ist eine Entscheidung, die man spaeter nachlesen koennen will.
STATUS_ABBILDUNG = {"pending": "draft", "approved": "ready", "rejected": "archived"}


def finde_bild(alter_pfad: str) -> tuple[str | None, str]:
    """Bild zum alten Pfad suchen. Liefert (Adresse, Guete).

    Die alten Pfade zeigen auf einen Ordner, den es hier nicht mehr gibt. Gesucht
    wird deshalb ueber den Dateinamen, in dieser Reihenfolge:

    1. ``repariert/`` - die zurueckgestauchten Motive. Die Originale waren um 25
       Prozent breitgezogen und sind unbrauchbar, die Reparatur gewinnt immer.
    2. der laufende Bildordner
    3. die Ablage ``uebernommen/`` - dort liegen Bestaende aus dem alten Projekt,
       die der Webserver nicht ausliefert. Treffer werden deshalb in den laufenden
       Bildordner KOPIERT, sonst zeigte die Oberflaeche ein totes Bild.

    Die Guete sagt, was gefunden wurde. ``vorschau`` heisst: nur ein 600er
    Vorschaubild. Das reicht zum Wiedererkennen, aber NICHT zum Drucken - Textil
    verlangt 4500 Pixel Breite. Diese Unterscheidung gehoert in die Notizen, sonst
    haelt man spaeter ein Vorschaubild fuer eine Druckdatei.
    """
    if not alter_pfad:
        return None, "kein_pfad"
    name = Path(alter_pfad.replace("\\", "/")).name
    stamm = Path(name).stem

    repariert = BILDORDNER / "repariert" / name
    if repariert.exists():
        return f"/studio/bilder/repariert/{name}", "repariert"

    if (BILDORDNER / name).exists():
        return f"/studio/bilder/{name}", "original"

    treffer = next(iter(BILDORDNER.rglob(name)), None)
    if treffer is not None:
        return f"/studio/bilder/{treffer.relative_to(BILDORDNER).as_posix()}", "original"

    # In der Ablage suchen - erst das Original, dann die Vorschau.
    ablage = WURZEL / "uebernommen"
    if ablage.exists():
        for kandidat, guete in ((name, "original"), (f"{stamm}_600.png", "vorschau")):
            fund = next((p for p in ablage.rglob(kandidat) if p.is_file()), None)
            if fund is None:
                continue
            ziel_ordner = BILDORDNER / "uebernommen"
            ziel_ordner.mkdir(parents=True, exist_ok=True)
            ziel = ziel_ordner / fund.name
            if not ziel.exists():
                ziel.write_bytes(fund.read_bytes())
            return f"/studio/bilder/uebernommen/{fund.name}", guete

    return None, "nicht_gefunden"


def printify_je_motiv(quelle: sqlite3.Connection) -> dict[int, list[dict]]:
    """Die Printify-Entwuerfe nach Motiv-Nummer buendeln."""
    cur = quelle.execute(
        "SELECT design_id, product_type, external_id, status, price_cents, marketplace "
        "FROM product_listings WHERE design_id IS NOT NULL"
    )
    aus: dict[int, list[dict]] = {}
    for design_id, art, fremd_id, status, cent, markt in cur.fetchall():
        aus.setdefault(design_id, []).append({
            "produktart": art, "fremd_id": fremd_id, "status": status,
            "preis_cent": cent, "marktplatz": markt,
        })
    return aus


def main() -> int:
    if not QUELLE.exists():
        print(f"Quelle fehlt: {QUELLE}")
        return 1
    if not ZIEL.exists():
        print(f"Ziel fehlt: {ZIEL}")
        return 1

    quelle = sqlite3.connect(QUELLE)
    ziel = sqlite3.connect(ZIEL)

    # Was steckt schon drin? Erkennungsmerkmal ist die alte Nummer im meta_json.
    schon_da: set[int] = set()
    for (meta,) in ziel.execute("SELECT meta_json FROM studio_designs WHERE meta_json IS NOT NULL"):
        try:
            alt = json.loads(meta).get("alt_id")
        except (ValueError, AttributeError):
            continue
        if alt is not None:
            schon_da.add(int(alt))

    printify = printify_je_motiv(quelle)
    jetzt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    zeilen = quelle.execute(
        "SELECT id, theme, prompt, provider, model, status, path, reason, "
        "overlay_text, niche, created_at FROM designs ORDER BY id"
    ).fetchall()

    neu = uebersprungen = ohne_bild = 0
    gueten: dict[str, int] = {}
    for (alt_id, thema, prompt, anbieter, modell, status, pfad, grund,
         spruch, nische, erstellt) in zeilen:
        if alt_id in schon_da:
            uebersprungen += 1
            continue

        bild, guete = finde_bild(pfad)
        if bild is None:
            ohne_bild += 1
        gueten[guete] = gueten.get(guete, 0) + 1

        meta = {
            "alt_id": alt_id,
            "alt_status": status,
            "prompt": prompt,
            "modell": modell,
            "nische": nische,
            "herkunft": "POD-Shop (alter Ordner)",
            "bildguete": guete,
        }
        if guete == "vorschau":
            meta["warnung"] = ("Nur 600er Vorschaubild vorhanden - das Original in "
                               "Druckaufloesung existiert nirgends mehr. Zum Drucken "
                               "muss das Motiv neu erzeugt werden.")
        if grund:
            meta["ablehngrund"] = grund
        if spruch:
            # Der Spruch wurde frueher als Text unter das Bild gelegt. Das Verfahren
            # ist abgeschafft - der Text bleibt aber als Notiz erhalten.
            meta["frueherer_spruch"] = spruch
        if alt_id in printify:
            meta["printify"] = printify[alt_id]

        ziel.execute(
            "INSERT INTO studio_designs (title, status, source, image_url, meta_json, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (thema or f"Motiv {alt_id}")[:255],
                STATUS_ABBILDUNG.get(status, "draft"),
                anbieter,
                bild,
                json.dumps(meta, ensure_ascii=False),
                erstellt or jetzt,
                jetzt,
            ),
        )
        neu += 1

    ziel.commit()

    gesamt = ziel.execute("SELECT COUNT(*) FROM studio_designs").fetchone()[0]
    nach_status = dict(ziel.execute(
        "SELECT status, COUNT(*) FROM studio_designs GROUP BY status").fetchall())

    quelle.close()
    ziel.close()

    print(f"Uebernommen             : {neu}")
    print(f"Uebersprungen (schon da): {uebersprungen}")
    print(f"Ohne gefundenes Bild    : {ohne_bild}")
    print(f"Bildguete               : {gueten}")
    print(f"Motive im Studio gesamt : {gesamt}")
    print(f"Nach Status             : {nach_status}")
    print(f"Mit Printify-Entwuerfen : {len(printify)} Motive, "
          f"{sum(len(v) for v in printify.values())} Produkte")
    return 0


if __name__ == "__main__":
    sys.exit(main())
