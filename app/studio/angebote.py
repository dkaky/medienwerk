"""Eingestellte eBay-Angebote bearbeiten: Preis je Angebot, Farben und Groessen abschalten.

Gespeichert wird nur lokal (``studio_angebot_optionen``). Bei eBay wirkt es, sobald das
Angebot mit "Bei eBay aktualisieren" erneut uebertragen wird - das ist der bewusste Klick
(Eiserne Regel 1). ``ebay_weg.veroeffentliche`` liest dieselben Optionen.
"""
from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.studio import ebay_weg, preise
from app.studio.models import PodListing, PodProduct, StudioAngebotOption, StudioDesign


class AngebotFehler(ValueError):
    """Unbekanntes Angebot oder unzulaessige Einstellung."""


def _liste(text: str | None) -> list[str]:
    try:
        wert = json.loads(text) if text else []
    except ValueError:
        return []
    return [str(x) for x in wert] if isinstance(wert, list) else []


def optionen(db: Session, design_id: int, produkt_key: str) -> dict:
    z = db.get(StudioAngebotOption, (design_id, produkt_key))
    if z is None:
        return {"preis_eur": None, "farben_aus": [], "groessen_aus": []}
    return {"preis_eur": z.preis_eur, "farben_aus": _liste(z.farben_aus),
            "groessen_aus": _liste(z.groessen_aus)}


def aktive_farben(db: Session, design_id: int, p: ebay_weg.Produkt) -> list[str]:
    aus = set(optionen(db, design_id, p.key)["farben_aus"])
    return [f for f in ebay_weg.farben(p) if f not in aus]


def aktive_groessen(db: Session, design_id: int, p: ebay_weg.Produkt, s: Settings) -> list[str]:
    aus = set(optionen(db, design_id, p.key)["groessen_aus"])
    return [g for g in ebay_weg.groessen(p, s) if g not in aus]


def preis_fuer(db: Session, design_id: int, p: ebay_weg.Produkt, s: Settings) -> float:
    eigener = optionen(db, design_id, p.key)["preis_eur"]
    return round(eigener, 2) if eigener is not None else ebay_weg.preis(p, s)


def uebersicht(db: Session, s: Settings) -> list[dict]:
    zeilen = db.execute(
        select(PodListing, PodProduct, StudioDesign)
        .join(PodProduct, PodListing.product_id == PodProduct.id)
        .join(StudioDesign, PodProduct.design_id == StudioDesign.id)
        .where(PodListing.channel == ebay_weg.KANAL, PodListing.status.in_(("active", "ended")))
        .order_by(PodListing.id.desc())).all()
    aus = []
    for angebot, produkt, design in zeilen:
        p = ebay_weg.PRODUKTE.get(produkt.produktart)
        if p is None:
            continue
        o = optionen(db, design.id, p.key)
        aus.append({
            "design_id": design.id, "motiv": ebay_weg.motivname(design), "bild": design.image_url,
            "produkt": p.key, "label": p.label, "listing_id": angebot.external_id,
            "url": angebot.url, "status": angebot.status, "preis_live": angebot.price_eur,
            "preis_eur": preis_fuer(db, design.id, p, s), "eigener_preis": o["preis_eur"] is not None,
            "farben": [{"name": f, "aus": f in o["farben_aus"]} for f in ebay_weg.farben(p)],
            "groessen": [{"name": g, "aus": g in o["groessen_aus"]} for g in ebay_weg.groessen(p, s)],
            "unterschied": abs(preis_fuer(db, design.id, p, s) - (angebot.price_eur or 0)) > 0.004
                           or bool(o["farben_aus"] or o["groessen_aus"]),
        })
    return aus


def setze(db: Session, s: Settings, design_id: int, produkt_key: str, *, preis_eur, farben_aus,
          groessen_aus) -> dict:
    p = ebay_weg.PRODUKTE.get(produkt_key)
    if p is None or db.get(StudioDesign, design_id) is None:
        raise AngebotFehler("Angebot nicht gefunden.")
    preis = None
    if preis_eur not in (None, ""):
        try:
            preis = round(float(preis_eur), 2)
        except (TypeError, ValueError):
            raise AngebotFehler("Der Preis muss eine Zahl sein.") from None
        if not preise.MIN_EUR <= preis <= preise.MAX_EUR:
            raise AngebotFehler(f"Der Preis muss zwischen {preise.MIN_EUR:.2f} und "
                                f"{preise.MAX_EUR:.2f} Euro liegen.")
    farben, groessen = ebay_weg.farben(p), ebay_weg.groessen(p, s)
    f_aus = [f for f in (farben_aus or []) if f in farben]
    g_aus = [g for g in (groessen_aus or []) if g in groessen]
    if farben and len(f_aus) >= len(farben):
        raise AngebotFehler("Mindestens eine Farbe muss verfuegbar bleiben.")
    if groessen and len(g_aus) >= len(groessen):
        raise AngebotFehler("Mindestens eine Groesse muss verfuegbar bleiben.")
    z = db.get(StudioAngebotOption, (design_id, produkt_key))
    if z is None:
        z = StudioAngebotOption(design_id=design_id, produkt=produkt_key)
        db.add(z)
    z.preis_eur, z.farben_aus, z.groessen_aus = preis, json.dumps(f_aus), json.dumps(g_aus)
    db.commit()
    return optionen(db, design_id, produkt_key)


def loesche_entwurf(db: Session, product_id: int) -> dict:
    """Ein Entwurf oder fehlgeschlagenes Produkt lokal entfernen.

    Nur Status draft/fehler, nie mit aktivem eBay-Angebot und nie, wenn eine Bestellung
    daran haengt. Bei eBay wird nichts geloescht - dafuer gibt es "Von eBay loeschen".
    """
    produkt = db.get(PodProduct, product_id)
    if produkt is None:
        raise AngebotFehler("Eintrag nicht gefunden.")
    if produkt.status not in ("draft", "fehler"):
        raise AngebotFehler(f"Nur Entwuerfe und Fehler lassen sich loeschen (Status: {produkt.status}).")
    angebote_ = db.scalars(select(PodListing).where(PodListing.product_id == product_id)).all()
    if any(a.status == "active" for a in angebote_):
        raise AngebotFehler("Hat ein aktives eBay-Angebot - erst dort beenden.")
    from app.studio.models import PodOrder

    ids = [a.id for a in angebote_]
    if ids and db.scalar(select(func.count()).select_from(PodOrder).where(PodOrder.listing_id.in_(ids))):
        raise AngebotFehler("Es gibt Bestellungen dazu - bleibt zur Dokumentation erhalten.")
    for a in angebote_:
        db.delete(a)
    titel = produkt.title
    db.delete(produkt)
    db.commit()
    return {"geloescht": product_id, "titel": titel}
