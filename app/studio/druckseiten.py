"""Welches Motiv auf welche Seite der Ware kommt.

Vorgabe des Betreibers (13.09.2026): Vorder- und Rueckseite sind einzeln
waehlbar. Ein Motiv kann vorne, hinten oder auf beiden Seiten sitzen, und vorne
und hinten duerfen verschiedene Motive tragen. Gezeigt werden trotzdem immer
beide Seiten - eine Seite ohne Motiv erscheint unbedruckt.

Gespeichert am Motiv in ``meta_json["druck"] = {"vorne": id|None, "hinten": id|None}``.
Ohne Eintrag gilt: vorne dieses Motiv, hinten leer - so bleiben alle bisherigen
Motive unveraendert.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.studio.models import StudioDesign

SEITEN = ("vorne", "hinten")


class DruckseitenFehler(ValueError):
    """Die gewuenschte Belegung geht so nicht."""


@dataclass
class Druckseiten:
    vorne: Any | None
    hinten: Any | None

    @property
    def beschreibung(self) -> str:
        if self.vorne is not None and self.hinten is not None:
            return "Druck: Vorder- und Rückseite bedruckt"
        if self.hinten is not None:
            return "Druck: Rückseite bedruckt, Vorderseite unbedruckt"
        return "Druck: Vorderseite bedruckt, Rückseite unbedruckt"

    def als_dict(self) -> dict[str, dict | None]:
        return {seite: _kurz(getattr(self, seite)) for seite in SEITEN}


def _kurz(design: Any | None) -> dict | None:
    if design is None:
        return None
    return {"design_id": design.id, "title": design.title, "image_url": design.image_url}


def _meta(design: Any) -> dict:
    try:
        meta = json.loads(design.meta_json) if design.meta_json else {}
    except (TypeError, ValueError):
        meta = {}
    return meta if isinstance(meta, dict) else {}


def lese(db, design: Any) -> Druckseiten:
    druck = _meta(design).get("druck")
    if not isinstance(druck, dict):
        return Druckseiten(vorne=design, hinten=None)

    def hol(seite: str) -> Any | None:
        wert = druck.get(seite)
        if wert is None:
            return None
        if int(wert) == design.id:
            return design
        return db.get(StudioDesign, int(wert))    # geloeschtes Motiv -> Seite bleibt leer

    return Druckseiten(vorne=hol("vorne"), hinten=hol("hinten"))


def setze(db, design: Any, *, vorne: int | None, hinten: int | None) -> Druckseiten:
    if vorne is None and hinten is None:
        raise DruckseitenFehler("Mindestens eine Seite braucht ein Motiv.")
    for wert in (vorne, hinten):
        if wert is None:
            continue
        ziel = design if wert == design.id else db.get(StudioDesign, wert)
        if ziel is None or not ziel.image_url:
            raise DruckseitenFehler(f"Motiv {wert} gibt es nicht oder es hat kein Bild.")
    meta = _meta(design)
    meta["druck"] = {"vorne": vorne, "hinten": hinten}
    design.meta_json = json.dumps(meta, ensure_ascii=False)
    db.commit()
    return lese(db, design)


def pfade(seiten: Druckseiten, bildordner: Path) -> tuple[Path | None, Path | None]:
    """Die Bilddateien fuer vorne und hinten (``None`` = unbedruckt)."""
    from app.studio import produktweg

    return tuple(produktweg.bildpfad(d, bildordner) if d is not None else None
                 for d in (seiten.vorne, seiten.hinten))
