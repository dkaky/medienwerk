"""Die Aufdrucke der vorhandenen Entwuerfe von den Bildern ablesen.

Nutzerwunsch vom 03.09.2026: die Bildanalyse zuerst nur auf die vorhandenen
Entwuerfe loslassen, damit das Ergebnis sichtbar ist, bevor sie in jeden Import
wandert.

Kostet Geld je Bild. Deshalb laeuft nichts von selbst - dieses Skript ist der
einzige Weg, und ohne ``--lesen`` zeigt es nur, was es TUN wuerde.

Ansehen::

    .venv\\Scripts\\python.exe scripts\\lies_aufdrucke.py

Wirklich lesen (kostet)::

    .venv\\Scripts\\python.exe scripts\\lies_aufdrucke.py --lesen
    .venv\\Scripts\\python.exe scripts\\lies_aufdrucke.py --lesen --anzahl 5

Bereits gelesene Artikel werden uebersprungen - ein zweiter Lauf kostet also
nur fuer das, was noch fehlt.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal          # noqa: E402
from app.models import Listing, Product        # noqa: E402
from app.services import bildtext              # noqa: E402


def vorschau(db, anzahl: int) -> int:
    listings = (db.query(Listing)
                .filter(Listing.listing_status == "draft")
                .order_by(Listing.id).all())
    offen, schon = [], 0
    for l in listings:
        if bildtext.gelesen(db, l.id) is not None:
            schon += 1
            continue
        p = db.get(Product, l.product_id) if l.product_id else None
        bilder = len([u for u in ((p.images if p else None) or []) if u])
        offen.append((l.id, l.title_seo or "", bilder))

    print(f"{len(listings)} Entwuerfe, davon {schon} bereits gelesen, {len(offen)} offen.")
    print(f"Dieser Lauf wuerde hoechstens {min(anzahl, len(offen))} Artikel lesen.\n")
    for lid, titel, bilder in offen[:anzahl]:
        print(f"  #{lid:<4} {bilder} Bild(er)  {titel[:58]}")
    return len(offen)


async def lesen(db, anzahl: int) -> None:
    ergebnis = await bildtext.analysiere_alle(db, max_bilder=anzahl)
    print(f"\ngeprueft: {ergebnis['geprueft']}  |  mit Aufdruck: {ergebnis['mit_aufdruck']}"
          f"  |  ohne: {ergebnis['ohne_aufdruck']}"
          f"  |  schon gelesen: {ergebnis['schon_gelesen']}"
          f"  |  Fehler: {ergebnis['fehler']}")
    if not ergebnis["treffer"]:
        print("\nKein Aufdruck gefunden.")
        return
    print("\nGefundene Aufdrucke:")
    for t in ergebnis["treffer"]:
        sicher = "sicher" if t["sicher"] else "UNSICHER"
        print(f"\n  #{t['listing_id']}  ({sicher})")
        print(f"    Aufdruck   : {t['aufdruck']}")
        if t["zielgruppe"]:
            print(f"    Zielgruppe : {t['zielgruppe']}")
        print(f"    Titel jetzt: {(t['titel'] or '')[:64]}")


def main() -> int:
    p = argparse.ArgumentParser(description="Aufdrucke von den Produktbildern ablesen.")
    p.add_argument("--lesen", action="store_true", help="wirklich lesen (kostet Geld)")
    p.add_argument("--anzahl", type=int, default=30, help="Hoechstzahl je Lauf")
    args = p.parse_args()

    db = SessionLocal()
    try:
        offen = vorschau(db, args.anzahl)
        if not args.lesen:
            print("\nTROCKENLAUF - es wurde nichts gelesen und nichts berechnet.")
            print("Zum wirklichen Lesen: --lesen anhaengen.")
            return 0
        if not offen:
            print("\nNichts zu tun.")
            return 0
        asyncio.run(lesen(db, args.anzahl))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
