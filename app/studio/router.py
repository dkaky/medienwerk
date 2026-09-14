"""Endpunkte des Studio-Trakts.

Der gesamte Router haengt an ``require_studio_enabled``: Solange der Schalter
``STUDIO_ENABLED`` aus ist, antwortet jede Adresse mit 404. Bewusst 404 und nicht
403 - von aussen soll nicht erkennbar sein, dass es diesen Bereich gibt.

Etappe 1 kennt keinen Endpunkt, der etwas erzeugt, kauft oder veroeffentlicht.
"""

from __future__ import annotations

import logging
import json
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.config import get_settings
from app.studio import kosten, service
from app.studio.generation import motivregeln
from app.studio.guard import require_studio_enabled
from app.studio.radar import dienst as radar_dienst
from app.studio.radar import beschreibung as radar_beschreibung
from app.studio.radar import ideen as radar_ideen
from app.studio.radar import umwandlung as radar_umwandlung
from app.studio.radar.ernte import ErnteFehler
from app.studio.radar.quellen import QuelleUnklar
from app.studio.schemas import (
    DruckseitenIn,
    GenerateIn,
    GenerateOut,
    DesignIn,
    DesignOut,
    DesignStatusIn,
    EntwurfIn,
    EntwurfOut,
    IdeeOut,
    IdeeStatusIn,
    LinkIn,
    LinkOut,
    RadarErzeugenIn,
    RadarLaufIn,
    StudioStatus,
    TrendLaufIn,
    VeredelnIn,
    VeredelnOut,
)

logger = logging.getLogger("app.studio.router")

router = APIRouter(
    prefix="/api/v1/studio",
    tags=["Studio"],
    dependencies=[Depends(require_studio_enabled)],
)


def _fehler(exc: service.StudioFehler) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/status", response_model=StudioStatus)
def studio_status(db: Session = Depends(get_db)) -> StudioStatus:
    """Kurzuebersicht: Schalter, Anzahl Motive, verknuepfte Angebote."""
    return StudioStatus(**service.status(db))


# --- Motive -------------------------------------------------------------------

@router.get("/designs", response_model=list[DesignOut])
def list_designs(
    status: str | None = Query(default=None, pattern="^(draft|ready|archived)$"),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[DesignOut]:
    gefunden = service.list_designs(db, status=status, limit=limit)
    return [DesignOut.model_validate(d) for d in gefunden]


@router.post("/designs", response_model=DesignOut, status_code=201)
def create_design(body: DesignIn, db: Session = Depends(get_db)) -> DesignOut:
    try:
        design = service.create_design(
            db,
            title=body.title,
            source=body.source,
            image_url=body.image_url,
            meta_json=body.meta_json,
        )
    except service.StudioFehler as exc:
        raise _fehler(exc) from exc
    return DesignOut.model_validate(design)


@router.get("/designs/{design_id}", response_model=DesignOut)
def get_design(design_id: int, db: Session = Depends(get_db)) -> DesignOut:
    design = service.get_design(db, design_id)
    if design is None:
        raise HTTPException(status_code=404, detail="Motiv nicht gefunden")
    return DesignOut.model_validate(design)


@router.patch("/designs/{design_id}/status", response_model=DesignOut)
def set_design_status(
    design_id: int, body: DesignStatusIn, db: Session = Depends(get_db)
) -> DesignOut:
    try:
        design = service.set_design_status(db, design_id=design_id, status=body.status)
    except service.StudioFehler as exc:
        raise _fehler(exc) from exc
    return DesignOut.model_validate(design)


# --- Verknuepfung -------------------------------------------------------------

# --------------------------------------------------------------------------
# Vom Motiv zum Produkt
#
# Beide Enden lagen fertig und unverbunden: der Umrechner, der ein Motiv ins
# Druckformat bringt, und der Printify-Teil, der daraus ein Produkt macht. Was
# fehlte, war der Weg - und die Pruefung dazwischen.
#
# Zwei Stufen, wie ueberall hier: erst lesen, dann tun.
# --------------------------------------------------------------------------
@router.get("/designs/{design_id}/druckcheck")
def druckcheck(design_id: int, typ: str = Query("tshirt"),
               db: Session = Depends(get_db)):
    """Ginge dieses Motiv als dieses Produkt? Aendert NICHTS.

    Beantwortet die Frage VOR dem Klick. Ein abgebrochener Printify-Aufruf
    hinterliesse sonst ein halbes Produkt - und ein zu kleines Motiv wuerde
    klaglos gedruckt, matschig, mit der Retoure zu uns.
    """
    from app.studio import produktweg

    design = service.get_design(db, design_id)
    if design is None:
        raise HTTPException(status_code=404, detail="Motiv nicht gefunden")
    try:
        e = produktweg.pruefe(design, produkttyp_key=typ,
                              bildordner=Path(get_settings().studio_image_dir))
    except produktweg.MotivFehler as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "design_id": design_id, "produkttyp": e.produkttyp,
        "format": e.format_key, "moeglich": e.moeglich, "grund": e.grund,
        "breite": e.breite, "hoehe": e.hoehe, "dpi": e.dpi,
        "zielmasse": list(e.zielmasse) if e.zielmasse else None,
        # Seit 08.09.2026: Ein "nein" allein half niemandem weiter - es traf
        # JEDES erzeugte Motiv, weil kein Bildmodell die noetigen 2250 Pixel
        # liefert. Jetzt steht daneben, ob es MIT Vergroesserung ginge.
        "mit_vergroesserung": e.mit_vergroesserung,
        "faktor": e.faktor,
        "noetige_breite": e.noetige_breite,
        "weich": e.weich,
    }


@router.get("/designs/{design_id}/druckcheck-alle")
def druckcheck_alle(design_id: int, db: Session = Depends(get_db)):
    """Derselbe Check fuer ALLE Produkttypen auf einmal.

    Beantwortet die Frage, die man beim Motiv wirklich hat: "Wofuer taugt das
    hier?" Statt viermal einzeln zu fragen, kommt eine Zeile je Produkt.
    Aendert nichts und kostet nichts - alles rein oertlich gerechnet.
    """
    from app.studio import produktweg

    design = service.get_design(db, design_id)
    if design is None:
        raise HTTPException(status_code=404, detail="Motiv nicht gefunden")

    ordner = Path(get_settings().studio_image_dir)
    zeilen = []
    for typ in produktweg.FORMAT_JE_TYP:
        try:
            e = produktweg.pruefe(design, produkttyp_key=typ, bildordner=ordner)
        except produktweg.MotivFehler as exc:
            zeilen.append({"produkttyp": typ, "moeglich": False, "grund": str(exc)})
            continue
        zeilen.append({
            "produkttyp": typ, "format": e.format_key, "moeglich": e.moeglich,
            "grund": e.grund, "dpi": e.dpi, "breite": e.breite, "hoehe": e.hoehe,
            "mit_vergroesserung": e.mit_vergroesserung, "faktor": e.faktor,
            "noetige_breite": e.noetige_breite, "weich": e.weich,
        })
    return {"design_id": design_id, "produkte": zeilen}


@router.post("/designs/{design_id}/svg", status_code=201)
def als_svg(design_id: int, stufe: str = Query("plakativ"),
            hochskalieren: bool = Query(False),
            db: Session = Depends(get_db)):
    """Aus dem Motiv eine SVG-Datei machen - zum Selberdrucken.

    Eine SVG besteht aus Formen statt Pixeln: beliebig vergroesserbar, ohne weich
    zu werden. Damit laesst sich das Motiv selbst ausdrucken, auf jede Groesse
    ziehen und an einen Schneideplotter geben.

    Rein oertlich - kein Netzzugriff, keine Kosten. Deshalb braucht dieser
    Endpunkt anders als der Printify-Weg KEINE Bestaetigung: Es entsteht eine
    Datei auf der eigenen Platte, sonst nichts.

    ``stufe``: 'plakativ' (Vorgabe, zum Drucken), 'fein' (naeher am Original,
    grosse Datei) oder 'schnitt' (einfarbig, fuer Schneideplotter).
    """
    from app.studio import produktweg
    from app.studio.postprocess import vektor

    design = service.get_design(db, design_id)
    if design is None:
        raise HTTPException(status_code=404, detail="Motiv nicht gefunden")
    try:
        ergebnis = produktweg.erzeuge_svg(
            design, bildordner=Path(get_settings().studio_image_dir),
            stufe=stufe, hochskalieren=hochskalieren)
    except (vektor.VektorFehler, produktweg.MotivFehler) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "design_id": design_id, "stufe": ergebnis.stufe, "datei": ergebnis.pfad.name,
        "pfade": ergebnis.pfade, "bytes": ergebnis.bytes,
        "zu_gross": ergebnis.zu_gross, "hinweis": ergebnis.hinweis,
        "url": f"/studio/druckdateien/{ergebnis.pfad.name}",
    }


@router.get("/vektor-stufen")
def vektor_stufen():
    """Welche Nachzeichen-Stufen es gibt und wofuer sie taugen."""
    from app.studio.postprocess import vektor

    return [{"key": s.key, "label": s.label, "zweck": s.zweck}
            for s in vektor.STUFEN.values()]


@router.post("/designs/{design_id}/printify", status_code=201)
async def als_printify_produkt(design_id: int, typ: str = Query("tshirt"),
                               bestaetigt: bool = Query(False),
                               db: Session = Depends(get_db)):
    """Motiv ins Druckformat bringen und als Printify-ENTWURF anlegen.

    Braucht ``bestaetigt=true``. Der Aufruf geht nach aussen: er laedt das Bild
    zu Printify hoch und legt dort ein Produkt an. Das ist zwar nur ein Entwurf
    und wird nicht veroeffentlicht - aber es entsteht etwas in einem fremden
    Konto, und das soll kein Fehlklick ausloesen.

    Der Umrechner sitzt zwingend dazwischen: reicht die Aufloesung nicht,
    entsteht gar nichts. Lieber eine Absage als ein matschiger Druck.
    """
    from app.studio import produktweg

    design = service.get_design(db, design_id)
    if design is None:
        raise HTTPException(status_code=404, detail="Motiv nicht gefunden")
    if not bestaetigt:
        raise HTTPException(
            status_code=428,
            detail=("Anlegen bei Printify muss bestaetigt werden "
                    "(bestaetigt=true). Vorher mit /druckcheck ansehen, ob das "
                    "Motiv fuer diesen Produkttyp taugt."))

    s = get_settings()
    if not (s.printify_token and s.printify_shop_id):
        raise HTTPException(
            status_code=409,
            detail="Printify ist nicht eingerichtet (Token und Shop-Nummer fehlen).")

    try:
        ergebnis = await produktweg.lege_an(
            design, produkttyp_key=typ,
            bildordner=Path(s.studio_image_dir))
        # Printify liefert fertige Produktansichten mit - das eigene Motiv auf
        # deren echten Produktfotos. Die gehoeren ans Motiv, sonst waeren sie
        # nach dieser einen Antwort weg und man muesste fuer einen zweiten Blick
        # ein zweites Produkt anlegen.
        try:
            service.merke_printify(
                db, design_id=design_id,
                printify_id=ergebnis.get("printify_id") or "",
                produkttyp=ergebnis.get("produkttyp") or typ,
                mockups=ergebnis.get("mockups") or [])
        except Exception as exc:  # noqa: BLE001
            # Das Produkt steht schon bei Printify. Ein Fehler beim Notieren
            # darf den Erfolg nicht in eine Fehlermeldung verwandeln.
            logger.warning("Printify-Mockups nicht gespeichert: %s", exc)
        return ergebnis
    except produktweg.MotivFehler as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502,
                            detail=f"Printify-Anlage fehlgeschlagen: {exc}") from exc


@router.get("/links", response_model=list[LinkOut])
def list_links(
    limit: int = Query(default=200, ge=1, le=500), db: Session = Depends(get_db)
) -> list[LinkOut]:
    return [LinkOut.model_validate(v) for v in service.list_links(db, limit=limit)]


@router.post("/links", response_model=LinkOut, status_code=201)
def link_listing(body: LinkIn, db: Session = Depends(get_db)) -> LinkOut:
    """Ein Angebot dem Studio zuordnen - danach fasst der Handel es nicht mehr an."""
    try:
        verknuepfung = service.link_listing(
            db, listing_id=body.listing_id, design_id=body.design_id, note=body.note
        )
    except service.StudioFehler as exc:
        raise _fehler(exc) from exc
    return LinkOut.model_validate(verknuepfung)


@router.delete("/links/{listing_id}")
def unlink_listing(listing_id: int, db: Session = Depends(get_db)) -> dict:
    """Zuordnung loesen. Achtung: danach greifen die Handels-Automatiken wieder."""
    if not service.unlink_listing(db, listing_id=listing_id):
        raise HTTPException(status_code=404, detail="Zuordnung nicht gefunden")
    return {"listing_id": listing_id, "geloest": True}

# --- Bilderzeugung ------------------------------------------------------------

@router.post("/prompt/veredeln", response_model=VeredelnOut)
async def prompt_veredeln(body: VeredelnIn) -> VeredelnOut:
    """Eine Motividee pruefen und zu einem druckfertigen Prompt schaerfen.

    Erzeugt **nichts**: kein Bild, kein Entwurf, kein Datenbankeintrag. Der
    Rueckgabewert fuellt nur das Eingabefeld; auf "Erzeugen" drueckt weiterhin
    ein Mensch (Eiserne Regel 1). Deshalb auch 200 statt 201 - es entsteht
    keine Ressource.

    Das Bildbudget wird nicht angefasst. Die Kostenbremse (Eiserne Regel 5)
    sitzt an der Bilderzeugung; ein Textaufruf ist um Groessenordnungen
    billiger und wuerde sie nur unbrauchbar fein machen.
    """
    from app.studio.generation import promptveredelung

    try:
        ergebnis = await promptveredelung.veredle(body.idee, ziel=body.ziel)
    except promptveredelung.VeredelungFehler as exc:
        # Fehlendes Regelwerk ist ein Einrichtungsfehler, kein Nutzerfehler -
        # und ohne Regeln wird nicht veredelt, statt irgendetwas zu liefern.
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return VeredelnOut(
        prompt=ergebnis.prompt,
        breite=ergebnis.breite,
        hoehe=ergebnis.hoehe,
        stil=ergebnis.stil,
        ziel=ergebnis.ziel,
        bericht=ergebnis.bericht,
        abbruch=ergebnis.abbruch,
        quelle=ergebnis.quelle,
    )


@router.post("/generate", response_model=GenerateOut, status_code=201)
def generate_design(body: GenerateIn, hintergrund: BackgroundTasks,
                    db: Session = Depends(get_db)) -> GenerateOut:
    """Ein Motiv erzeugen und als Entwurf ablegen.

    Die Reihenfolge der Sicherungen steckt im Dienst: erst Schutzfilter, dann
    Kostenbremse, dann erzeugen. Ein gesperrtes Motiv kostet nichts, weil es gar
    nicht erst entsteht.
    """
    from app.studio.generation import service as gen

    try:
        ergebnis = gen.erzeuge(
            db,
            prompt=body.prompt,
            breite=body.breite,
            hoehe=body.hoehe,
            anbieter=body.anbieter,
            stil=body.stil,
            )
    except gen.MotivGesperrt as exc:
        raise HTTPException(status_code=422, detail=f"Gesperrt: {exc}") from exc
    except gen.motivregeln.MotivartFehler as exc:
        # Kein Serverfehler, sondern eine Formulierungssache - und der Text
        # traegt bereits den Verbesserungsvorschlag. Ohne diesen Zweig kaeme er
        # als 502 "Erzeugung fehlgeschlagen" an und waere unbrauchbar.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except kosten.BudgetErschoepft as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - Anbieterfehler lesbar weiterreichen
        raise HTTPException(status_code=502, detail=f"Erzeugung fehlgeschlagen: {exc}") from exc
    bild = ergebnis.bild.image

    pfad = _ablegen(bild, body.titel or body.prompt[:60])
    design = service.create_design(
        db,
        title=body.titel or body.prompt[:80],
        source=ergebnis.bild.provider,
        image_url=f"/studio/bilder/{pfad.name}",
    )
    return GenerateOut(
        design=DesignOut.model_validate(design, from_attributes=True),
        kosten_usd=ergebnis.kosten_usd,
        rest_budget_usd=ergebnis.rest_budget_usd,
        anbieter=ergebnis.bild.provider,
    )


@router.get("/designs/{design_id}/ebay")
def ebay_stand(design_id: int, db: Session = Depends(get_db)) -> dict:
    """Ginge dieses Motiv zu eBay, und ist es schon dort? Ohne Netz, aendert nichts."""
    from app.studio import ebay_weg

    design = service.get_design(db, design_id)
    if design is None:
        raise HTTPException(status_code=404, detail="Motiv nicht gefunden")
    s = get_settings()
    b = ebay_weg.pruefe(design, s=s, bildordner=Path(s.studio_image_dir))
    produkte = []
    for p in ebay_weg.PRODUKTE.values():
        angebot = ebay_weg.aktives_angebot(db, design_id, p.key)
        quelle, fehlende = ebay_weg.fotoquelle(p, s)
        produkte.append({
            "key": p.key, "label": p.label, "preis_eur": ebay_weg.preis(p, s),
            "groessen": ebay_weg.groessen(p, s), "farben": ebay_weg.farben(p),
            "titel": ebay_weg.titel(design, p),
            "fotoquelle": quelle, "fehlende_vorlagen": fehlende,
            "angebot": ({"listing_id": angebot.external_id, "url": angebot.url}
                        if angebot else None),
        })
    from app.studio import mockup_plan

    return {"bereit": b.bereit, "fehlt": b.fehlt, "probebetrieb": b.probebetrieb,
            "automatisch": s.ebay_auto_veroeffentlichen, "produkte": produkte,
            "fotos_je_motiv": mockup_plan.bilder_je_motiv()}


@router.get("/designs/{design_id}/druckseiten")
def druckseiten_lesen(design_id: int, db: Session = Depends(get_db)) -> dict:
    """Die Ebenen je Seite: welches Motiv wo und wie gross sitzt (leer = unbedruckt)."""
    from app.studio import druckseiten

    design = service.get_design(db, design_id)
    if design is None:
        raise HTTPException(status_code=404, detail="Motiv nicht gefunden")
    return druckseiten.lese(db, design).als_dict()


@router.put("/designs/{design_id}/druckseiten")
def druckseiten_setzen(design_id: int, body: DruckseitenIn, db: Session = Depends(get_db)) -> dict:
    """Gestaltung speichern: Motive je Seite mit Lage und Groesse. Eine Seite darf leer bleiben."""
    from app.studio import druckseiten

    design = service.get_design(db, design_id)
    if design is None:
        raise HTTPException(status_code=404, detail="Motiv nicht gefunden")
    try:
        return druckseiten.setze(db, design, vorne=[e.model_dump() for e in body.vorne],
                                 hinten=[e.model_dump() for e in body.hinten]).als_dict()
    except druckseiten.DruckseitenFehler as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/mockups/vorlagen")
def mockup_vorlagen() -> dict:
    """Welche Produktfoto-Vorlagen es gibt, mit Ansichten und Farben. Ohne Netz, ohne Kosten."""
    from app.studio import ebay_weg, mockup_montage, mockup_plan

    ordner = Path(get_settings().mockup_montage_ordner)
    produkte = []
    for p in ebay_weg.PRODUKTE.values():
        farben = ([{"name": f.name, "hex": f.hex} for f in mockup_plan.FARBEN] if p.textil
                  else [{"name": ebay_weg.TASSENFARBE, "hex": "#FFFFFF"}])
        produkte.append({
            "key": p.key, "label": p.label, "farben": farben,
            "ansichten": list(mockup_montage.ansichten(p.textil)),
            "vorhanden": list(mockup_montage.vorlagen_pfade(p.key, textil=p.textil, ordner=ordner)),
            "fehlt": mockup_montage.fehlende_vorlagen(p.key, textil=p.textil, ordner=ordner),
        })
    return {"produkte": produkte}


@router.get("/mockups/flaeche")
async def mockup_flaeche(produkt: str = Query("tshirt"), seite: str = Query("vorne"),
                         farbe: str = Query("Weiß")) -> dict:
    """Die leere Ware als Gestaltungsflaeche fuer den Editor - Bild und Seitenverhaeltnis."""
    import asyncio

    from app.studio import ebay_weg, mockup_montage, mockup_plan

    p = ebay_weg.PRODUKTE.get(produkt)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Unbekanntes Produkt: {produkt}")
    if seite not in ("vorne", "hinten"):
        raise HTTPException(status_code=422, detail="seite ist vorne oder hinten")
    if farbe not in ebay_weg.farben(p):
        farbe = ebay_weg.farben(p)[0]
    hexwert = mockup_plan.farbe(farbe).hex if p.textil else "#FFFFFF"
    s = get_settings()
    bildordner, ordner = Path(s.studio_image_dir), Path(s.mockup_montage_ordner)
    try:
        pfad = await asyncio.to_thread(
            mockup_montage.flaechenbild, p.key, seite, farbe, hexwert, textil=p.textil,
            ziel_ordner=bildordner / ebay_weg.MOCKUP_ORDNER / "flaechen", ordner=ordner)
        verhaeltnis = mockup_montage.seitenverhaeltnis(p.key, seite, textil=p.textil, ordner=ordner)
    except mockup_montage.MontageFehler as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"produkt": p.key, "seite": seite, "farbe": farbe, "breite_zu_hoehe": round(verhaeltnis, 5),
            "url": "/studio/bilder/" + pfad.resolve().relative_to(bildordner.resolve()).as_posix()}


@router.get("/mockups/bilder")
async def mockup_bilder(produkt: str = Query("tshirt"), farbe: str = Query("Weiß"),
                        design_id: int | None = Query(None),
                        db: Session = Depends(get_db)) -> dict:
    """Produktfotos in der eBay-Reihenfolge: Ware allein, Mann, Frau.

    Mit ``design_id`` steht das Motiv drauf - es sind dieselben Dateien, die
    spaeter zu eBay gehen. Ohne ``design_id`` die leeren Vorlagen. Kostet nichts:
    montiert wird lokal, Ergebnisse bleiben zwischengespeichert. Geld kostet nur
    das Erzeugen eines Motivs.
    """
    import asyncio

    from app.studio import ebay_weg, mockup_montage, mockup_plan, produktweg

    p = ebay_weg.PRODUKTE.get(produkt)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Unbekanntes Produkt: {produkt}")
    if farbe not in ebay_weg.farben(p):
        raise HTTPException(status_code=422, detail=f"{p.label} gibt es nicht in {farbe}")
    hexwert = mockup_plan.farbe(farbe).hex if p.textil else "#FFFFFF"
    s = get_settings()
    bildordner, ordner = Path(s.studio_image_dir), Path(s.mockup_montage_ordner)
    ansichten = list(mockup_montage.vorlagen_pfade(p.key, textil=p.textil, ordner=ordner))
    druck = None
    try:
        if design_id is None:
            ziel = bildordner / ebay_weg.MOCKUP_ORDNER / "vorlagen"
            pfade = await asyncio.to_thread(lambda: [
                mockup_montage.rendere_leer(p.key, a, farbe, hexwert, ziel_ordner=ziel, ordner=ordner)
                for a in ansichten])
        else:
            design = service.get_design(db, design_id)
            if design is None:
                raise HTTPException(status_code=404, detail="Motiv nicht gefunden")
            from app.studio import druckseiten

            seiten = druckseiten.lese(db, design)
            vorne, hinten = await asyncio.to_thread(
                druckseiten.druckbilder, seiten, bildordner, produkt=p.key, textil=p.textil,
                design_id=design.id, vorlagen_ordner=ordner)
            fotos = await asyncio.to_thread(
                mockup_montage.rendere, vorne, hinten=hinten, produkt=p.key, textil=p.textil,
                ganzflaeche=True,
                farben=[(farbe, hexwert)], ordner=ordner,
                ziel_ordner=bildordner / ebay_weg.MOCKUP_ORDNER / str(design.id))
            pfade = fotos[farbe]
            ansichten = mockup_montage.folge(p.key, textil=p.textil, ordner=ordner,
                                             vorne_leer=vorne is None and hinten is not None)
            druck = seiten.als_dict()
    except (mockup_montage.MontageFehler, produktweg.MotivFehler) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    wurzel = bildordner.resolve()
    return {"produkt": p.key, "farbe": farbe,
            "fehlt": mockup_montage.fehlende_vorlagen(p.key, textil=p.textil, ordner=ordner),
            "druck": druck,
            "bilder": [{"ansicht": a, "seite": "hinten" if mockup_montage.ist_hinten(a) else "vorne",
                        "url": "/studio/bilder/" + x.resolve().relative_to(wurzel).as_posix()}
                       for a, x in zip(ansichten, pfade)]}


@router.post("/designs/{design_id}/ebay", status_code=201)
async def bei_ebay_einstellen(design_id: int, bestaetigt: bool = Query(False),
                              produkte: str = Query(..., description="z. B. tshirt,polo,tasse"),
                              db: Session = Depends(get_db)) -> dict:
    """Motiv als gewaehlte Produkte bei eBay einstellen - LIVE. Braucht ``bestaetigt=true``.

    Je Produkt ein Angebot. Scheitert eines, laufen die anderen weiter; das
    Ergebnis nennt je Produkt Erfolg oder Fehler.

    Der bewusste Klick oeffnet die Schreibsperre fuer genau dieses eine Motiv,
    auch im Probebetrieb (siehe app/services/freigabe.py).
    """
    from app.integrations.ebay import RealEbayClient
    from app.services import freigabe
    from app.studio import ebay_weg

    if not bestaetigt:
        raise HTTPException(status_code=400, detail="Einstellen braucht bestaetigt=true.")
    keys = [k.strip() for k in produkte.split(",") if k.strip()]
    unbekannt = [k for k in keys if k not in ebay_weg.PRODUKTE]
    if not keys or unbekannt:
        raise HTTPException(status_code=422, detail=(
            f"Unbekannte Produkte: {', '.join(unbekannt)}. " if unbekannt else "Kein Produkt gewaehlt. ")
            + f"Moeglich: {', '.join(ebay_weg.PRODUKTE)}")
    design = service.get_design(db, design_id)
    if design is None:
        raise HTTPException(status_code=404, detail="Motiv nicht gefunden")
    s = get_settings()
    bildordner = Path(s.studio_image_dir)
    bereitschaft = ebay_weg.pruefe(design, s=s, bildordner=bildordner)
    if not bereitschaft.bereit:
        raise HTTPException(status_code=422,
                            detail="Noch nicht bereit: " + "; ".join(bereitschaft.fehlt))

    ebay = RealEbayClient(s)
    schluessel = ebay_weg.freigabe_schluessel(design_id)
    freigabe.erteile(schluessel)
    ergebnisse = []
    try:
        with freigabe.beim_veroeffentlichen(schluessel):
            for key in keys:
                try:
                    ergebnisse.append({"ok": True, **await ebay_weg.veroeffentliche(
                        db, design, produkt_key=key, ebay=ebay, s=s, bildordner=bildordner)})
                except Exception as exc:  # noqa: BLE001 - je Produkt melden, weitermachen
                    ergebnisse.append({"ok": False, "produkt": key, "fehler": str(exc)[:400]})
    finally:
        freigabe.widerrufe(schluessel)
        if ebay._client is not None:
            await ebay._client.aclose()
    return {"ergebnisse": ergebnisse}


def _ablegen(bild, titel: str):
    """Erzeugtes Motiv auf die Platte legen und den Pfad liefern."""
    import re
    from datetime import datetime

    ordner = Path(get_settings().studio_image_dir)
    ordner.mkdir(parents=True, exist_ok=True)
    sauber = re.sub(r"[^a-z0-9]+", "-", titel.lower()).strip("-")[:50] or "motiv"
    name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{sauber}.png"
    ziel = ordner / name
    bild.save(ziel, "PNG")
    return ziel


# --------------------------------------------------------------------------
# Motiv-Radar: lesen, was in fremden Shops laeuft
# --------------------------------------------------------------------------

def _idee_raus(db: Session, idee) -> IdeeOut:
    """Modell -> Ausgabe.

    DREI Spalten stehen als JSON-TEXT in der Datenbank und als Objekt im
    Schema: ``stichworte``, ``beschreibung`` und ``druckcheck``. Sie werden
    umgewandelt, BEVOR Pydantic prueft - ein ``model_validate`` ueber das
    Modell ist an genau dieser Stelle schon einmal gescheitert (05.09.2026,
    ``stichworte``), und mit jedem weiteren JSON-Feld waechst die Falle.
    """
    felder = {name: getattr(idee, name, None) for name in IdeeOut.model_fields}
    felder["stichworte"] = radar_ideen.stichworte_von(idee)
    felder["beschreibung"] = radar_beschreibung.gelesen(idee)
    felder["druckcheck"] = _json_obj(getattr(idee, "druckcheck", None))
    felder["design_bild_url"] = _design_bild(db, idee.design_id)
    return IdeeOut(**felder)


def _json_obj(roh: str | None) -> dict:
    """JSON-Objekt aus einer Textspalte; unlesbare Altdaten bleiben leer."""
    if not roh:
        return {}
    try:
        daten = json.loads(roh)
    except (TypeError, ValueError):
        return {}
    return daten if isinstance(daten, dict) else {}


def _design_bild(db: Session, design_id: int | None) -> str | None:
    """Adresse des ERZEUGTEN Motivs - nicht zu verwechseln mit ``bild_url``.

    ``bild_url`` ist das fremde Produktfoto (Herkunft), das hier ist unser
    eigenes Erzeugnis. In der Karte stehen beide nebeneinander: links, was der
    andere verkauft, rechts, was daraus geworden ist.
    """
    if not design_id:
        return None
    design = service.get_design(db, design_id)
    return getattr(design, "image_url", None) if design else None


@router.get("/radar/ideen", response_model=list[IdeeOut])
def radar_liste(status: str | None = Query("neu"),
                min_signal: float | None = Query(None, ge=0, le=100),
                quelle: str | None = Query(None, pattern="^(trend|shop)$"),
                limit: int = Query(50, ge=1, le=500),
                db: Session = Depends(get_db)) -> list[IdeeOut]:
    """Die Funde, staerkstes Signal zuerst. Unbekanntes steht hinten - aber es steht da.

    ``quelle=trend`` liefert die Vorschlaege der Websuche (nach Rang), ``quelle=shop``
    die Funde aus fremden Shops.
    """
    return [_idee_raus(db, i) for i in
            radar_ideen.liste(db, status=status, min_signal=min_signal, quelle=quelle,
                              limit=limit)]


@router.post("/radar/trends")
async def radar_trends(body: TrendLaufIn, db: Session = Depends(get_db)) -> dict:
    """Im Netz nach Trends suchen und eigene Motive vorschlagen. Erzeugt KEIN Bild."""
    from app.studio.radar import trends

    try:
        return await trends.lauf(db, s=get_settings(), anzahl=body.anzahl)
    except kosten.BudgetErschoepft as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except trends.TrendFehler as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/radar/ideen/{idee_id}/erzeugen", status_code=201)
def radar_erzeugen(idee_id: int, body: RadarErzeugenIn | None = None,
                   db: Session = Depends(get_db)) -> dict:
    """Aus einem Vorschlag ein Motiv erzeugen - erst dieser Klick kostet ein Bild."""
    from app.studio.generation import service as gen
    from app.studio.models import MotivIdee
    from app.studio.radar import nutzen

    idee = db.get(MotivIdee, idee_id)
    if idee is None:
        raise HTTPException(status_code=404, detail=f"Motiv-Idee {idee_id} gibt es nicht.")
    try:
        e = nutzen.erzeuge_aus_idee(db, idee, anbieter=(body.anbieter if body else "openai"),
                                    bildordner=Path(get_settings().studio_image_dir))
    except gen.MotivGesperrt as exc:
        raise HTTPException(status_code=422, detail=f"Gesperrt: {exc}") from exc
    except (gen.motivregeln.MotivartFehler, radar_umwandlung.WortlautUebernommen,
            radar_umwandlung.ZuWenigThema) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except kosten.BudgetErschoepft as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - Anbieterfehler lesbar weiterreichen
        raise HTTPException(status_code=502, detail=f"Erzeugung fehlgeschlagen: {exc}") from exc
    return {"design": DesignOut.model_validate(e["design"], from_attributes=True).model_dump(),
            "prompt": e["prompt"], "kosten_usd": e["kosten_usd"],
            "rest_budget_usd": e["rest_budget_usd"], "anbieter": e["anbieter"]}


@router.post("/radar/lauf")
async def radar_lauf(body: RadarLaufIn, db: Session = Depends(get_db)) -> dict:
    """Einen fremden Shop lesen. Dauert eine Weile - es faehrt ein echter Browser."""
    try:
        return await radar_dienst.lauf(db, body.link, limit=body.limit,
                                       nur_verkauft=body.nur_verkauft)
    except QuelleUnklar as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ErnteFehler as exc:
        # 502: nicht wir sind kaputt, die fremde Seite gibt nichts her.
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.patch("/radar/ideen/{idee_id}/status", response_model=IdeeOut)
def radar_status(idee_id: int, body: IdeeStatusIn,
                 db: Session = Depends(get_db)) -> IdeeOut:
    """Uebernehmen oder verwerfen - ein Erntelauf fasst das nie wieder an."""
    try:
        return _idee_raus(db, radar_ideen.setze_status(db, idee_id, body.status,
                                                       notiz=body.notiz))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/radar/ideen/{idee_id}/entwurf", response_model=EntwurfOut)
def radar_entwurf(idee_id: int, body: EntwurfIn | None = None,
                  db: Session = Depends(get_db)) -> EntwurfOut:
    """Aus dem Thema eine EIGENE Bildanweisung machen - ohne zu erzeugen.

    Propose-only (Eiserne Regel 1): hier entsteht Text zum Lesen. Das Bild
    kostet Geld und braucht den Klick eines Menschen.
    """
    from app.studio.models import MotivIdee

    idee = db.get(MotivIdee, idee_id)
    if idee is None:
        raise HTTPException(status_code=404, detail=f"Motiv-Idee {idee_id} gibt es nicht.")
    try:
        prompt = radar_umwandlung.entwirf_und_merke(
            db, idee, zusatz=(body.zusatz if body else None))
    except (radar_umwandlung.WortlautUebernommen, radar_umwandlung.ZuWenigThema,
            motivregeln.MotivartFehler) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return EntwurfOut(idee_id=idee.id, prompt=prompt)
