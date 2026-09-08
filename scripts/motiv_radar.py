"""Motiv-Radar von Hand starten: einen fremden Shop lesen.

Was es tut
----------
Es liest die Artikel eines fremden Shops (eBay, Etsy, Redbubble, Spreadshirt),
merkt sich Titel, Verkaufszahl und Platz und legt daraus **Motiv-Ideen** an.
Es erzeugt KEIN Bild und kostet nichts.

Nutzerwunsch vom 29.08.2026, woertlich: „muessen wir ein scraper tool bauen, der
zb ein ebay store link bekommt oder ein etsy / printify sotre link. Hier werden
dann dei bestselling motive und sprueche etc analysiert."

Bedienung
---------
Einen Shop lesen::

    .venv\\Scripts\\python.exe scripts\\motiv_radar.py https://www.ebay.de/str/beispiel

Nur die schon gefundenen Ideen anzeigen::

    .venv\\Scripts\\python.exe scripts\\motiv_radar.py --liste

Zu einer Idee eine eigene Bildanweisung entwerfen (nur Text, kein Bild)::

    .venv\\Scripts\\python.exe scripts\\motiv_radar.py --entwurf 7

Hinweis zu ``--nur-verkauft``: eBay kann auf TATSAECHLICH VERKAUFTE Artikel
filtern - das beste Marktsignal ueberhaupt. Dafuer muss der interne Browser bei
eBay angemeldet sein (``scripts/browser_anmelden.py`` zeigt das Muster). Ohne
Anmeldung bricht der Lauf mit einer klaren Meldung ab, statt eine leere Liste zu
liefern.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Die Windows-Konsole steht auf cp1252 und macht aus "Möwen" ein "M?wen".
# Die Titel sind deutsch - ohne diese Zeile ist die Liste kaum lesbar.
for _strom in (sys.stdout, sys.stderr):
    try:
        _strom.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):   # umgeleitet oder schon utf-8
        pass

from app.database import SessionLocal, init_db          # noqa: E402
from app.studio.models import MotivIdee                 # noqa: E402
from app.studio.radar import (                          # noqa: E402
    beschreibung, dienst, ideen, umwandlung)
from app.studio.radar.ernte import ErnteFehler          # noqa: E402
from app.studio.radar.quellen import QuelleUnklar       # noqa: E402


def _zeile(idee: MotivIdee) -> str:
    signal = "  ?  " if idee.signal is None else f"{idee.signal:5.0f}"
    verkauft = "     ?" if idee.verkauft is None else f"{idee.verkauft:6d}"
    return (f"  #{idee.id:<4} {signal}  {verkauft}  "
            f"{(idee.thema or '-'):<28.28}  {idee.fremdtitel[:52]}")


def zeige(db, *, status: str | None, limit: int, shop: str | None = None) -> int:
    treffer = ideen.liste(db, status=status, shop=shop, limit=limit)
    if not treffer:
        print("Noch keine Ideen abgelegt.")
        return 0
    print(f"\n  {'Nr':<5} {'Signal':>5}  {'Verk.':>6}  {'Thema':<28}  Fremder Titel")
    print("  " + "-" * 104)
    for idee in treffer:
        print(_zeile(idee))
    ohne = sum(1 for i in treffer if i.signal is None)
    print(f"\n  {len(treffer)} Ideen, davon {ohne} ohne jede Zahl auf der Seite "
          f"(unbekannt - nicht null).\n")
    return 0


async def ansehen(db, *, anzahl: int, shop: str | None = None) -> int:
    """Die Produktfotos ansehen und genau beschreiben. Kostet je Bild etwas."""
    offen = [i for i in ideen.liste(db, status="neu", shop=shop, limit=500)
             if i.bild_url and not beschreibung.gelesen(i)]
    if not offen:
        print("Nichts anzusehen - entweder fehlen die Fotos (dann hilft ein "
              "neuer Erntelauf) oder alles ist schon beschrieben.")
        return 0
    print(f"Sehe mir {min(anzahl, len(offen))} Motive an ...")
    bericht = await beschreibung.beschreibe_alle(db, offen[:anzahl],
                                                 hoechstens=anzahl)
    print(f"\n  Versucht:     {bericht['versucht']}")
    print(f"  Beschrieben:  {bericht['beschrieben']}")
    if bericht["ohne_bild"]:
        print(f"  Ohne Foto:    {bericht['ohne_bild']}")
    for zeile in bericht["fehler"]:
        print(f"  Fehler: {zeile}")
    return 0


def zeige_beschreibung(db, idee_id: int) -> int:
    idee = db.get(MotivIdee, idee_id)
    if idee is None:
        print(f"Motiv-Idee {idee_id} gibt es nicht.")
        return 1
    print(f"\n#{idee.id}  {idee.fremdtitel}")
    print(f"Foto: {idee.bild_url or '-'}\n")
    print(beschreibung.als_text(idee))
    print()
    return 0


def entwerfen(db, idee_id: int, *, spruch: str | None = None) -> int:
    idee = db.get(MotivIdee, idee_id)
    if idee is None:
        print(f"Motiv-Idee {idee_id} gibt es nicht.")
        return 1
    try:
        text = umwandlung.entwirf_und_merke(db, idee, eigener_spruch=spruch)
    except (umwandlung.WortlautUebernommen, umwandlung.ZuWenigThema) as exc:
        print(f"Kein Entwurf: {exc}")
        return 1
    print(f"\nFremder Titel (nur als Beleg): {idee.fremdtitel}")
    print(f"Thema, das uebernommen wird:  {idee.thema}\n")
    print("Eigene Bildanweisung:\n")
    print(text)
    print("\nErzeugt wurde nichts - das kostet Geld und braucht deinen Klick.\n")
    return 0


def erzeuge(db, idee_id: int, *, spruch: str | None = None,
            anbieter: str = "openai") -> int:
    """Aus der Beschreibung ein EIGENES Motiv erzeugen. Kostet Budget.

    Der einzige Schritt im ganzen Radar, der Geld ausgibt - deshalb steht er
    hinter einem eigenen Schalter und laeuft nie nebenbei mit.
    """
    from app.studio import service as studio_service
    from app.studio.generation import service as erzeugung

    idee = db.get(MotivIdee, idee_id)
    if idee is None:
        print(f"Motiv-Idee {idee_id} gibt es nicht.")
        return 1
    try:
        prompt = umwandlung.entwirf_und_merke(db, idee, eigener_spruch=spruch)
    except (umwandlung.WortlautUebernommen, umwandlung.ZuWenigThema) as exc:
        print(f"Kein Entwurf: {exc}")
        return 1

    print(f"\n#{idee.id}  {idee.thema}")
    print(f"Anweisung: {prompt[:300]} ...\n")
    try:
        ergebnis = erzeugung.erzeuge(db, prompt=prompt, anbieter=anbieter)
    except Exception as exc:  # noqa: BLE001 - die Meldung ist die Nachricht
        print(f"Nicht erzeugt: {type(exc).__name__}: {exc}")
        return 2

    pfad = _ablegen(ergebnis.bild.image, f"radar-{idee.id}-{idee.thema or 'motiv'}")
    design = studio_service.create_design(
        db, title=f"{idee.thema or 'Motiv'} (Radar #{idee.id})",
        source=ergebnis.bild.provider, image_url=f"/studio/bilder/{pfad.name}")
    idee.design_id = design.id
    idee.status = "uebernommen"
    db.commit()

    print(f"  Motiv #{design.id} angelegt: {pfad}")
    print(f"  Kosten: {ergebnis.kosten_usd:.3f} $ · Rest heute: "
          f"{ergebnis.rest_budget_usd:.3f} $\n")
    return 0


def _ablegen(bild, titel: str):
    """Erzeugtes Motiv auf die Platte legen - wie im Studio-Router."""
    import re
    from datetime import datetime

    from app.config import get_settings

    ordner = Path(get_settings().studio_image_dir)
    ordner.mkdir(parents=True, exist_ok=True)
    sauber = re.sub(r"[^a-z0-9]+", "-", titel.lower()).strip("-")[:50] or "motiv"
    ziel = ordner / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{sauber}.png"
    bild.save(ziel, "PNG")
    return ziel


async def lesen(db, link: str, *, limit: int, nur_verkauft: bool) -> int:
    print(f"Lese {link} ... (ein echter Browser faehrt, das dauert)")
    try:
        bericht = await dienst.lauf(db, link, limit=limit, nur_verkauft=nur_verkauft)
    except QuelleUnklar as exc:
        print(f"Link nicht erkannt: {exc}")
        return 1
    except ErnteFehler as exc:
        print(f"Nichts zu lesen: {exc}")
        return 2
    print(f"\n  Shop:             {bericht['plattform']}:{bericht['shop']}")
    print(f"  Gelesen:          {bericht['gelesen']}")
    print(f"  Neu:              {bericht['neu']}")
    print(f"  Aufgefrischt:     {bericht['aufgefrischt']}")
    print(f"  Mit Verkaufszahl: {bericht['mit_verkaufszahl']}")
    return zeige(db, status="neu", limit=25)


def main() -> int:
    p = argparse.ArgumentParser(description="Motiv-Radar: fremde Shops lesen.")
    p.add_argument("link", nargs="?",
                   help="Shop-Link (eBay, Etsy, Redbubble, Spreadshirt)")
    p.add_argument("--limit", type=int, default=40,
                   help="hoechstens so viele Artikel (max 100)")
    p.add_argument("--nur-verkauft", action="store_true",
                   help="nur verkaufte Artikel (eBay, verlangt Anmeldung)")
    p.add_argument("--liste", action="store_true",
                   help="nur die abgelegten Ideen zeigen")
    p.add_argument("--alle", action="store_true",
                   help="mit --liste: auch verworfene und uebernommene")
    p.add_argument("--ansehen", type=int, metavar="ANZAHL",
                   help="so viele Produktfotos ansehen und genau beschreiben")
    p.add_argument("--shop", metavar="NAME",
                   help="nur diesen Shop (z.B. stickerexpress724)")
    p.add_argument("--zeige", type=int, metavar="NR",
                   help="die gespeicherte Beschreibung dieser Idee anzeigen")
    p.add_argument("--entwurf", type=int, metavar="NR",
                   help="zu dieser Idee eine eigene Bildanweisung entwerfen")
    p.add_argument("--spruch", metavar="TEXT",
                   help="mit --entwurf/--erzeuge: UNSER Schriftzug fuer das Bild")
    p.add_argument("--erzeuge", type=int, metavar="NR",
                   help="aus der Beschreibung ein eigenes Motiv ERZEUGEN "
                        "(kostet Budget)")
    p.add_argument("--anbieter", default="openai",
                   choices=("mock", "openai", "fal"),
                   help="womit erzeugt wird (Vorgabe: openai)")
    args = p.parse_args()

    init_db()
    db = SessionLocal()
    try:
        if args.ansehen:
            return asyncio.run(ansehen(db, anzahl=args.ansehen, shop=args.shop))
        if args.zeige:
            return zeige_beschreibung(db, args.zeige)
        if args.erzeuge:
            return erzeuge(db, args.erzeuge, spruch=args.spruch,
                           anbieter=args.anbieter)
        if args.entwurf:
            return entwerfen(db, args.entwurf, spruch=args.spruch)
        if args.liste or not args.link:
            return zeige(db, status=None if args.alle else "neu", limit=100,
                         shop=args.shop)
        return asyncio.run(lesen(db, args.link, limit=args.limit,
                                 nur_verkauft=args.nur_verkauft))
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
