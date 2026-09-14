"""Gestaltung eines Motivs: welche Motive wo auf Vorder- und Rueckseite sitzen.

Vorgaben des Betreibers (13./14.09.2026):

* Vorder- und Rueckseite sind einzeln belegbar, eine Seite darf leer bleiben.
  Gezeigt werden trotzdem immer beide Seiten.
* Je Seite mehrere Motive (Ebenen), jedes verschiebbar und in der Groesse
  einstellbar.

**Gestaltungsflaeche = die ganze Ware** (Vorgabe vom 14.09.2026): flach von
vorne bzw. hinten mit Aermeln, bei der Tasse der Koerper ohne Henkel. Eine Ebene
steht darin mit ``mitte_x`` (0 = linker, 1 = rechter Rand), ``oben`` (Oberkante,
0 = oberer Rand, 1 = unterer Rand) und ``groesse`` (das Motiv passt in
``groesse`` x Flaeche). Neben dem Stoff wird nichts gedruckt. Aus den Ebenen
entsteht je Produkt und Seite ein **Druckbild** (PNG, transparent) im
Seitenverhaeltnis der Ware, aus dem die Mockups gerechnet werden.

Version 1 (13.09.2026) bezog die Werte auf ein Brustfeld im Format 3:4. Solche
Gestaltungen werden beim Lesen auf die ganze Ware umgerechnet.

Gespeichert am Motiv in ``meta_json["druck"] = {"vorne": [Ebene...], "hinten": [...]}``.
Ohne Eintrag gilt: vorne dieses Motiv mittig auf der Brust (``STANDARD``), hinten leer. Das
fruehere Format ``{"vorne": id|None, "hinten": id|None}`` wird weiter gelesen.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.studio.models import StudioDesign

SEITEN = ("vorne", "hinten")
MAX_EBENEN = 5
VERSION = 2
#: Lange Kante des Druckbilds in Pixel.
KANTE = 4000
ORDNER = "druckbilder"
#: Lage eines neuen bzw. unbearbeiteten Motivs: mittig auf der Brust.
STANDARD = {"mitte_x": 0.5, "oben": 0.2, "groesse": 0.4}

_GRENZEN = {"mitte_x": (0.0, 1.0, STANDARD["mitte_x"]), "oben": (-0.5, 1.0, STANDARD["oben"]),
            "groesse": (0.05, 1.5, STANDARD["groesse"])}


class DruckseitenFehler(ValueError):
    """Die gewuenschte Gestaltung geht so nicht."""


@dataclass
class Ebene:
    design: Any
    mitte_x: float = STANDARD["mitte_x"]
    oben: float = STANDARD["oben"]
    groesse: float = STANDARD["groesse"]

    def als_dict(self) -> dict:
        return {"design_id": self.design.id, "title": self.design.title,
                "image_url": self.design.image_url, "mitte_x": self.mitte_x,
                "oben": self.oben, "groesse": self.groesse}


@dataclass
class Druckseiten:
    vorne: list[Ebene] = field(default_factory=list)
    hinten: list[Ebene] = field(default_factory=list)

    @property
    def beschreibung(self) -> str:
        if self.vorne and self.hinten:
            return "Druck: Vorder- und Rückseite bedruckt"
        if self.hinten:
            return "Druck: Rückseite bedruckt, Vorderseite unbedruckt"
        return "Druck: Vorderseite bedruckt, Rückseite unbedruckt"

    def als_dict(self) -> dict[str, list[dict]]:
        return {seite: [e.als_dict() for e in getattr(self, seite)] for seite in SEITEN}


def _zahl(wert: Any, schluessel: str, v1: bool = False) -> float:
    unten, oben, standard = _GRENZEN[schluessel]
    if v1:                                         # Version 1: Motiv oben im Feld, volle Breite
        standard = {"mitte_x": 0.5, "oben": 0.0, "groesse": 1.0}[schluessel]
    try:
        zahl = float(wert)
    except (TypeError, ValueError):
        return standard
    return round(min(oben, max(unten, zahl)), 4)


def _aus_version_1(mitte_x: float, oben: float, groesse: float) -> tuple[float, float, float]:
    """Brustfeld 3:4 (Version 1) -> ganze Ware. Das Feld lag mittig, rund 40 % der Ware breit."""
    return (round(min(1.0, max(0.0, 0.5 + (mitte_x - 0.5) * 0.4)), 4),
            round(min(1.0, max(-0.5, 0.2 + oben * 0.4 * 4 / 3)), 4),
            round(max(0.05, groesse * 0.4), 4))


def _meta(design: Any) -> dict:
    try:
        meta = json.loads(design.meta_json) if design.meta_json else {}
    except (TypeError, ValueError):
        meta = {}
    return meta if isinstance(meta, dict) else {}


def _motiv(db, design: Any, wert: Any) -> Any | None:
    try:
        nummer = int(wert)
    except (TypeError, ValueError):
        return None
    ziel = design if nummer == design.id else db.get(StudioDesign, nummer)
    return ziel if ziel is not None and ziel.image_url else None


def _roh_ebenen(roh: Any) -> list[dict]:
    if roh is None:
        return []
    if isinstance(roh, (int, str)):                # frueheres Format: nur eine Motiv-Nummer
        return [{"design_id": roh}]
    return [e for e in roh if isinstance(e, dict)] if isinstance(roh, list) else []


def lese(db, design: Any) -> Druckseiten:
    druck = _meta(design).get("druck")
    if not isinstance(druck, dict):
        return Druckseiten(vorne=[Ebene(design)], hinten=[])
    alt = druck.get("version") != VERSION
    seiten = {}
    for seite in SEITEN:
        ebenen = []
        for e in _roh_ebenen(druck.get(seite))[:MAX_EBENEN]:
            motiv = _motiv(db, design, e.get("design_id"))
            if motiv is None:                      # geloeschtes Motiv -> Ebene faellt weg
                continue
            if alt:
                werte = _aus_version_1(*(_zahl(e.get(k), k, v1=True) for k in ("mitte_x", "oben", "groesse")))
            else:
                werte = tuple(_zahl(e.get(k), k) for k in ("mitte_x", "oben", "groesse"))
            ebenen.append(Ebene(motiv, *werte))
        seiten[seite] = ebenen
    return Druckseiten(**seiten)


def setze(db, design: Any, *, vorne: list[dict], hinten: list[dict]) -> Druckseiten:
    if not vorne and not hinten:
        raise DruckseitenFehler("Mindestens eine Seite braucht ein Motiv.")
    gespeichert = {}
    for seite, ebenen in (("vorne", vorne), ("hinten", hinten)):
        if len(ebenen) > MAX_EBENEN:
            raise DruckseitenFehler(f"Hoechstens {MAX_EBENEN} Motive je Seite.")
        liste = []
        for e in ebenen:
            if _motiv(db, design, e.get("design_id")) is None:
                raise DruckseitenFehler(f"Motiv {e.get('design_id')} gibt es nicht oder es hat kein Bild.")
            liste.append({"design_id": int(e["design_id"]),
                          **{k: _zahl(e.get(k), k) for k in ("mitte_x", "oben", "groesse")}})
        gespeichert[seite] = liste
    meta = _meta(design)
    meta["druck"] = {"version": VERSION, **gespeichert}
    design.meta_json = json.dumps(meta, ensure_ascii=False)
    db.commit()
    return lese(db, design)


def druckbild(ebenen: list[Ebene], bildordner: Path, *, verhaeltnis: float, ziel_ordner: Path) -> Path | None:
    """Die Ebenen einer Seite zu einem transparenten PNG zusammensetzen (zwischengespeichert).

    ``verhaeltnis`` ist Breite : Hoehe der Ware; die lange Kante hat ``KANTE`` Pixel.
    """
    if not ebenen:
        return None
    from PIL import Image

    from app.studio import produktweg

    quellen = [produktweg.bildpfad(e.design, bildordner) for e in ebenen]
    schluessel = hashlib.sha1(json.dumps(
        [VERSION, KANTE, round(verhaeltnis, 4)]
        + [[str(q.resolve()), int(q.stat().st_mtime), e.mitte_x, e.oben, e.groesse]
           for q, e in zip(quellen, ebenen)]).encode()).hexdigest()[:12]
    ziel = ziel_ordner / f"druck-{schluessel}.png"
    if ziel.is_file():
        return ziel

    if verhaeltnis >= 1:
        breite, hoehe = KANTE, max(1, round(KANTE / verhaeltnis))
    else:
        breite, hoehe = max(1, round(KANTE * verhaeltnis)), KANTE
    leinwand = Image.new("RGBA", (breite, hoehe), (0, 0, 0, 0))
    for quelle, e in zip(quellen, ebenen):
        with Image.open(quelle) as roh:
            motiv = roh.convert("RGBA")
        rahmen = motiv.getbbox()
        if rahmen:
            motiv = motiv.crop(rahmen)
        faktor = min(breite * e.groesse / motiv.width, hoehe * e.groesse / motiv.height)
        motiv = motiv.resize((max(1, round(motiv.width * faktor)), max(1, round(motiv.height * faktor))),
                             Image.LANCZOS)
        ebene = Image.new("RGBA", leinwand.size, (0, 0, 0, 0))
        ebene.paste(motiv, (round(e.mitte_x * breite - motiv.width / 2), round(e.oben * hoehe)))
        leinwand = Image.alpha_composite(leinwand, ebene)

    ziel_ordner.mkdir(parents=True, exist_ok=True)
    leinwand.save(ziel, "PNG", compress_level=1)
    for alt in ziel_ordner.glob("druck-*.png"):    # nur der aktuelle Stand bleibt liegen
        if alt != ziel:
            alt.unlink(missing_ok=True)
    return ziel


def druckbilder(seiten: Druckseiten, bildordner: Path, *, produkt: str, textil: bool, design_id: int,
                vorlagen_ordner: Path | None = None) -> tuple[Path | None, Path | None]:
    """Die Druckbilder dieses Produkts fuer vorne und hinten (``None`` = unbedruckt)."""
    from app.studio import mockup_montage

    ordner = Path(vorlagen_ordner) if vorlagen_ordner else mockup_montage.ORDNER

    def verhaeltnis(seite: str) -> float:
        try:
            return mockup_montage.seitenverhaeltnis(produkt, seite, textil=textil, ordner=ordner)
        except (mockup_montage.MontageFehler, OSError):
            return 3 / 4                           # ohne Vorlage: uebliches Textilformat

    basis = Path(bildordner) / ORDNER / str(design_id)
    return (druckbild(seiten.vorne, bildordner, verhaeltnis=verhaeltnis("vorne"),
                      ziel_ordner=basis / f"{produkt}-vorne"),
            druckbild(seiten.hinten, bildordner, verhaeltnis=verhaeltnis("hinten"),
                      ziel_ordner=basis / f"{produkt}-hinten"))
