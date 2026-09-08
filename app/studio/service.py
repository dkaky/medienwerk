"""Fachlogik des Studio-Trakts.

Etappe 1 kann bewusst wenig: Motive anlegen, auflisten, ihren Zustand aendern und
ein eBay-Angebot einem Motiv zuordnen. Erzeugt oder veroeffentlicht wird hier
nichts - das kommt in den spaeteren Etappen.

Wichtig bei jeder Schreibfunktion: ``guard.invalidate()``. Der Riegel merkt sich
die Menge der Studio-Angebote fuer eine Minute; ohne das Verwerfen wuerde eine
frische Zuordnung bis zu 60 Sekunden lang nicht greifen - und genau in dieser
Zeit koennte ein Nachtjob das Angebot anfassen.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.studio import guard
from app.studio import kosten
from app.studio.models import DESIGN_STATUS, StudioDesign, StudioListingLink


class StudioFehler(ValueError):
    """Fachlicher Fehler im Studio - wird als 400 an den Browser gereicht."""


# --- Motive -------------------------------------------------------------------

def list_designs(db: Session, *, status: str | None = None, limit: int = 200) -> list[StudioDesign]:
    """Motive auflisten, neueste zuerst."""
    stmt = select(StudioDesign)
    if status:
        stmt = stmt.where(StudioDesign.status == status)
    stmt = stmt.order_by(StudioDesign.id.desc()).limit(max(1, min(limit, 500)))
    return list(db.scalars(stmt).all())


def get_design(db: Session, design_id: int) -> StudioDesign | None:
    return db.get(StudioDesign, design_id)


def create_design(db: Session, *, title: str, source: str | None = None,
                  image_url: str | None = None, meta_json: str | None = None) -> StudioDesign:
    """Ein Motiv anlegen. Es startet immer als Entwurf."""
    sauber = (title or "").strip()
    if not sauber:
        raise StudioFehler("Ohne Titel geht es nicht.")
    design = StudioDesign(
        title=sauber[:255],
        status="draft",
        source=source,
        image_url=image_url,
        meta_json=meta_json,
    )
    db.add(design)
    db.commit()
    db.refresh(design)
    return design


def set_design_status(db: Session, *, design_id: int, status: str) -> StudioDesign:
    """Zustand eines Motivs aendern. Geloescht wird nie, nur archiviert."""
    if status not in DESIGN_STATUS:
        raise StudioFehler(f"Unbekannter Zustand: {status}")
    design = db.get(StudioDesign, design_id)
    if design is None:
        raise StudioFehler("Motiv nicht gefunden.")
    design.status = status
    db.commit()
    db.refresh(design)
    return design


def merke_printify(db: Session, *, design_id: int, printify_id: str,
                   produkttyp: str, mockups: list[str] | None) -> StudioDesign:
    """Printify-Produkt und dessen Mockup-Bilder am Motiv festhalten.

    Warum ueberhaupt speichern: ``erstelle_produkt`` bekommt von Printify
    fertige Produktansichten zurueck - das eigene Motiv auf deren echten
    Produktfotos. Bisher wurden die einmal an den Aufrufer durchgereicht und
    waren danach weg. Wer sie spaeter noch einmal sehen wollte, haette ein
    zweites Produkt anlegen muessen.

    Sie gehoeren ins ``meta_json`` und nicht in eigene Spalten: es sind fremde
    Adressen, die Printify jederzeit aendern kann. Ein Feld, auf dem nichts
    rechnet, braucht keine Spalte - und ``stand`` sagt, wie alt die Angabe ist,
    damit niemand eine tote Adresse fuer einen Fehler haelt.
    """
    design = db.get(StudioDesign, design_id)
    if design is None:
        raise StudioFehler("Motiv nicht gefunden.")

    try:
        meta = json.loads(design.meta_json) if design.meta_json else {}
    except (TypeError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}

    meta["printify"] = {
        "id": str(printify_id or ""),
        "produkttyp": produkttyp,
        "mockups": [str(u) for u in (mockups or []) if u],
        "stand": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    design.meta_json = json.dumps(meta, ensure_ascii=False)
    db.commit()
    db.refresh(design)
    return design


# --- Verknuepfung mit einem eBay-Angebot --------------------------------------

def link_listing(db: Session, *, listing_id: int, design_id: int | None = None,
                 note: str | None = None) -> StudioListingLink:
    """Ein Angebot dem Studio zuordnen.

    Ab diesem Augenblick fassen die Handels-Automatiken das Angebot nicht mehr an.
    """
    from app.models import Listing

    if db.get(Listing, listing_id) is None:
        raise StudioFehler("Angebot nicht gefunden.")
    if design_id is not None and db.get(StudioDesign, design_id) is None:
        raise StudioFehler("Motiv nicht gefunden.")

    vorhanden = db.get(StudioListingLink, listing_id)
    if vorhanden is not None:
        vorhanden.design_id = design_id
        vorhanden.note = note
        vorhanden.linked_at = datetime.now(timezone.utc)
        verknuepfung = vorhanden
    else:
        verknuepfung = StudioListingLink(
            listing_id=listing_id,
            design_id=design_id,
            note=note,
            linked_at=datetime.now(timezone.utc),
        )
        db.add(verknuepfung)

    db.commit()
    db.refresh(verknuepfung)
    guard.invalidate()          # sonst greift der Riegel bis zu 60 Sekunden lang nicht
    return verknuepfung


def unlink_listing(db: Session, *, listing_id: int) -> bool:
    """Zuordnung loesen. Das Angebot faellt damit zurueck an den Handel.

    Bewusst eine ausdrueckliche Handlung: danach fassen die Automatiken es wieder
    an, und das soll niemandem versehentlich passieren.
    """
    verknuepfung = db.get(StudioListingLink, listing_id)
    if verknuepfung is None:
        return False
    db.delete(verknuepfung)
    db.commit()
    guard.invalidate()
    return True


def list_links(db: Session, *, limit: int = 200) -> list[StudioListingLink]:
    stmt = select(StudioListingLink).limit(max(1, min(limit, 500)))
    return list(db.scalars(stmt).all())


# --- Uebersicht ---------------------------------------------------------------

def status(db: Session) -> dict:
    """Kurzuebersicht fuer das Dashboard."""
    return {
        "enabled": guard.studio_enabled(),
        "designs": db.scalar(select(func.count()).select_from(StudioDesign)) or 0,
        "verknuepfte_angebote": db.scalar(select(func.count()).select_from(StudioListingLink)) or 0,
        "tagesbudget_usd": kosten.tagesbudget(),
        "verbraucht_heute_usd": kosten.verbraucht_heute(db),
        "rest_heute_usd": kosten.rest_heute(db),
    }
