"""Verkaufspreise je Produktart, vom Betreiber jederzeit im Studio einstellbar.

Rangfolge: Studio-Einstellung (Tabelle ``studio_preise``) vor ``EBAY_PREISE`` in der
.env vor dem Katalogpreis in ``ebay_weg.PRODUKTE``. Die Preise sind Endpreise
einschliesslich 19 % MwSt. und Versand.

Eine Aenderung gilt fuer Angebote, die ab jetzt eingestellt oder erneut eingestellt
werden. Bereits aktive eBay-Angebote aendert sie NICHT von selbst - nichts geht ohne
Klick eines Menschen live (Eiserne Regel 1).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import Settings
from app.studio.models import StudioPreis

MIN_EUR = 1.00
MAX_EUR = 999.99


class PreisFehler(ValueError):
    """Unbekannte Produktart oder unbrauchbarer Preis."""


def _katalog():
    from app.studio import ebay_weg

    return ebay_weg.PRODUKTE


def eingestellt(db: Session) -> dict[str, float]:
    return {z.produkt: z.preis_eur for z in db.query(StudioPreis).all()}


def aktuell(produkt: str) -> float | None:
    """Der im Studio eingestellte Preis - oder None, wenn keiner eingestellt ist."""
    from app.database import SessionLocal

    with SessionLocal() as db:
        zeile = db.get(StudioPreis, produkt)
        return None if zeile is None else float(zeile.preis_eur)


def uebersicht(db: Session, s: Settings) -> list[dict]:
    from app.studio import ebay_weg

    eigene = eingestellt(db)
    aus = []
    for p in _katalog().values():
        preis = eigene.get(p.key)
        aus.append({
            "key": p.key, "label": p.label,
            "preis_eur": round(preis, 2) if preis is not None else ebay_weg.preis(p, s),
            "standard_eur": round(p.preis_eur, 2),
            "eigener_preis": preis is not None,
        })
    return aus


def setze(db: Session, produkt: str, preis_eur: float) -> float:
    if produkt not in _katalog():
        raise PreisFehler(f"Unbekannte Produktart '{produkt}'.")
    try:
        preis = round(float(preis_eur), 2)
    except (TypeError, ValueError):
        raise PreisFehler("Der Preis muss eine Zahl sein.") from None
    if not MIN_EUR <= preis <= MAX_EUR:
        raise PreisFehler(f"Der Preis muss zwischen {MIN_EUR:.2f} und {MAX_EUR:.2f} Euro liegen.")
    zeile = db.get(StudioPreis, produkt)
    if zeile is None:
        db.add(StudioPreis(produkt=produkt, preis_eur=preis))
    else:
        zeile.preis_eur = preis
    db.commit()
    return preis


def zuruecksetzen(db: Session, produkt: str) -> None:
    if produkt not in _katalog():
        raise PreisFehler(f"Unbekannte Produktart '{produkt}'.")
    zeile = db.get(StudioPreis, produkt)
    if zeile is not None:
        db.delete(zeile)
        db.commit()
