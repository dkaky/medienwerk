"""Die vier Stufen zu einem Lauf zusammengesetzt.

Ein Aufruf, ein Shop, ein Bericht. Mehr macht diese Datei nicht - die Arbeit
steckt in den Stufen. Sie ist trotzdem eigen, damit Skript und Oberflaeche
denselben Weg nehmen und nicht zwei leicht verschiedene.
"""
from __future__ import annotations

import logging

from app.studio.radar import ernte as ernte_stufe
from app.studio.radar import ideen as ideen_ablage
from app.studio.radar.quellen import Quelle, erkenne

logger = logging.getLogger("app.studio.radar")


async def lauf(db, link: str, *, limit: int = 40,
               nur_verkauft: bool = False) -> dict:
    """Einen fremden Shop lesen und die Funde ablegen.

    Nur lesen und ablegen - kein Bild, keine Kosten. Was daraus wird,
    entscheidet der Mensch in Stufe 4 (Propose-only, Eiserne Regel 1).
    """
    quelle: Quelle = erkenne(link)
    funde = await ernte_stufe.ernte(quelle, limit=limit, nur_verkauft=nur_verkauft)
    bericht = ideen_ablage.speichere(db, quelle, funde)
    bericht.update({
        "plattform": quelle.plattform,
        "shop": quelle.shop,
        "gelesen": len(funde),
        "mit_verkaufszahl": sum(1 for f in funde if f.verkauft is not None),
        "nur_verkauft": bool(nur_verkauft),
    })
    logger.info("radar-lauf fertig", extra=bericht)
    return bericht
