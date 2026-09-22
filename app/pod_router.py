"""Kanalneutrale Betriebsübersicht für Print-on-Demand.

Diese API speichert nur eigene Betriebsdaten. Sie importiert keine Produkte,
bestellt nichts und veröffentlicht nichts bei einem Verkaufskanal.
"""
import json
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.studio.models import PodLedgerEntry, PodListing, PodOrder, PodProduct, StudioDesign

router = APIRouter(prefix="/api/v1/pod", tags=["POD-Betrieb"])


class ProductIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    design_id: int | None = None
    provider: str | None = Field(default=None, max_length=40)
    base_cost_eur: float | None = Field(default=None, ge=0)
    target_price_eur: float | None = Field(default=None, ge=0)


class ListingIn(BaseModel):
    product_id: int
    channel: str = Field(min_length=1, max_length=40)
    price_eur: float | None = Field(default=None, ge=0)
    quantity_available: int | None = Field(default=None, ge=0)


class OrderIn(BaseModel):
    channel: str = Field(min_length=1, max_length=40)
    listing_id: int | None = None
    external_id: str | None = Field(default=None, max_length=120)
    sale_total_eur: float | None = Field(default=None, ge=0)
    fulfillment_cost_eur: float | None = Field(default=None, ge=0)


class LedgerIn(BaseModel):
    kind: str = Field(min_length=1, max_length=30)
    amount_eur: float
    reference: str | None = Field(default=None, max_length=255)
    note: str | None = None


def _produktbild_klein(design: StudioDesign | None) -> str | None:
    """Vorschaubild fuer eine Zeile in der Angebotsliste: das Motivbild, ohne Mockup-Suche."""
    return design.image_url if design is not None else None


def _product(x: PodProduct, design: StudioDesign | None = None) -> dict:
    return {"id": x.id, "title": x.title, "status": x.status, "provider": x.provider,
            "base_cost_eur": x.base_cost_eur, "target_price_eur": x.target_price_eur,
            "stock_mode": x.stock_mode, "created_at": x.created_at,
            "design_id": x.design_id, "image": _produktbild_klein(design)}


def _listing(x: PodListing, verkaeufe: int = 0) -> dict:
    return {"id": x.id, "product_id": x.product_id, "channel": x.channel, "status": x.status,
            "price_eur": x.price_eur, "quantity_available": x.quantity_available, "url": x.url,
            "sales_total": verkaeufe}


def _verkaeufe_je_listing(db: Session) -> dict[int, int]:
    """Anzahl Bestellungen je Angebot - stornierte/erstattete zaehlen nicht als Verkauf."""
    zeilen = db.execute(
        select(PodOrder.listing_id, func.count())
        .where(PodOrder.listing_id.is_not(None), PodOrder.status.notin_(("cancelled", "refunded")))
        .group_by(PodOrder.listing_id)
    ).all()
    return {listing_id: n for listing_id, n in zeilen}


def _produktbild(design: StudioDesign | None, p: dict) -> dict:
    """Bild fuer die Zeile: das Produktfoto in der verkauften Farbe, sonst das Motiv selbst."""
    from app.config import get_settings
    from app.studio import mockup_montage

    ordner = Path(get_settings().studio_image_dir)
    if design is not None and p.get("produktart") and p.get("farbe"):
        mockups = ordner / "mockups" / str(design.id)
        code = mockup_montage.farbcode(p["farbe"])
        treffer = sorted(mockups.glob(f"{p['produktart']}-vorne-{code}-*.jpg"),
                         key=lambda f: f.stat().st_mtime, reverse=True)
        treffer = [f for f in treffer if not f.stem.endswith("-leer")] or treffer
        if treffer:
            return {"bild": "/studio/bilder/" + treffer[0].resolve().relative_to(ordner.resolve()).as_posix(),
                    "bild_art": "produkt"}
    if design is not None and design.image_url:
        return {"bild": design.image_url, "bild_art": "motiv"}
    return {"bild": None, "bild_art": None}


def _positionen(db: Session, x: PodOrder) -> list[dict]:
    """Was in der Bestellung steckt, samt Download-Adressen der Motivdateien."""
    from app.studio import druckseiten, ebay_weg

    try:
        roh = json.loads(x.positionen_json or "[]")
    except ValueError:
        roh = []
    aus = []
    for n, p in enumerate(roh):
        label = ebay_weg.PRODUKTE[p["produktart"]].label if p.get("produktart") in ebay_weg.PRODUKTE else None
        downloads = []
        design = db.get(StudioDesign, p["design_id"]) if p.get("design_id") else None
        if design is not None:
            seiten = druckseiten.lese(db, design)
            for seite, name in (("vorne", "Vorderseite"), ("hinten", "Rückseite")):
                for e, ebene in enumerate(getattr(seiten, seite)):
                    zusatz = f" {e + 1}" if len(getattr(seiten, seite)) > 1 else ""
                    downloads.append({
                        "name": f"{name}{zusatz}",
                        "url": f"/api/v1/pod/orders/{x.id}/motiv?position={n}&seite={seite}&ebene={e}"})
        aus.append({**p, "produkt": label, "downloads": downloads, **_produktbild(design, p)})
    return aus


def _bestellung(db: Session, x: PodOrder) -> dict:
    return {"id": x.id, "channel": x.channel, "external_id": x.external_id, "status": x.status,
            "sale_total_eur": x.sale_total_eur, "fulfillment_cost_eur": x.fulfillment_cost_eur,
            "ordered_at": x.ordered_at.isoformat() if x.ordered_at else None,
            "titel": x.note, "positionen": _positionen(db, x)}


@router.get("/orders/{order_id}/motiv")
def order_motiv(order_id: int, position: int = 0, seite: str = "vorne", ebene: int = 0,
                db: Session = Depends(get_db)) -> FileResponse:
    """Die Motivdatei einer Bestellung zum Herunterladen.

    Bei einem weissen Shirt ist es die Fassung mit SCHWARZER Schrift - genau die Datei,
    die fuer diese Farbe gedruckt gehoert.
    """
    from app.config import get_settings
    from app.studio import druckseiten, produktweg
    from app.studio.postprocess import schriftfarbe

    x = db.get(PodOrder, order_id)
    if x is None:
        raise HTTPException(status_code=404, detail="Bestellung nicht gefunden")
    try:
        p = json.loads(x.positionen_json or "[]")[position]
    except (ValueError, IndexError):
        raise HTTPException(status_code=404, detail="Position nicht gefunden") from None
    design = db.get(StudioDesign, p.get("design_id")) if p.get("design_id") else None
    if design is None or seite not in ("vorne", "hinten"):
        raise HTTPException(status_code=404, detail="Zu dieser Bestellung gibt es kein Motiv im Studio")
    ebenen = getattr(druckseiten.lese(db, design), seite)
    if ebene >= len(ebenen):
        raise HTTPException(status_code=404, detail="Diese Seite hat kein Motiv")
    motiv = ebenen[ebene].design
    try:
        pfad = produktweg.bildpfad(motiv, Path(get_settings().studio_image_dir))
    except produktweg.MotivFehler as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    weiss = p.get("farbcode") == "White"
    if weiss:
        pfad = schriftfarbe.dunkle_fassung(pfad)
    slug = re.sub(r"[^a-z0-9]+", "-", (motiv.title or "motiv").lower().encode("ascii", "ignore").decode()).strip("-")[:40] or "motiv"
    name = f"{slug}-{seite}{'-schwarze-schrift' if weiss else ''}{pfad.suffix}"
    return FileResponse(pfad, filename=name)


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)) -> dict:
    listings = list(db.scalars(select(PodListing).order_by(PodListing.updated_at.desc()).limit(8)).all())
    orders = list(db.scalars(select(PodOrder).order_by(PodOrder.created_at.desc()).limit(8)).all())
    zaehlt = PodOrder.status.notin_(("cancelled", "refunded", "pending"))     # nur echte, bezahlte Verkaeufe
    revenue = db.scalar(select(func.coalesce(func.sum(PodOrder.sale_total_eur), 0.0)).where(zaehlt)) or 0.0
    cost = db.scalar(select(func.coalesce(func.sum(PodOrder.fulfillment_cost_eur), 0.0))) or 0.0
    return {
        "kpis": {"products": db.scalar(select(func.count()).select_from(PodProduct)) or 0,
                 "active_listings": db.scalar(select(func.count()).select_from(PodListing).where(PodListing.status == "active")) or 0,
                 "open_orders": db.scalar(select(func.count()).select_from(PodOrder).where(PodOrder.status.in_(("new", "needs_review")))) or 0,
                 "revenue_eur": round(float(revenue), 2), "contribution_eur": round(float(revenue - cost), 2)},
        "listings": [_listing(x) for x in listings],
        "orders": [_bestellung(db, x) for x in orders],
    }


@router.get("/products")
def products(db: Session = Depends(get_db)) -> list[dict]:
    produkte = list(db.scalars(select(PodProduct).order_by(PodProduct.updated_at.desc())).all())
    design_ids = {p.design_id for p in produkte if p.design_id}
    designs = {d.id: d for d in db.scalars(select(StudioDesign).where(StudioDesign.id.in_(design_ids)))} if design_ids else {}
    return [_product(x, designs.get(x.design_id)) for x in produkte]


@router.post("/products", status_code=201)
def create_product(body: ProductIn, db: Session = Depends(get_db)) -> dict:
    x = PodProduct(**body.model_dump(), status="draft")
    db.add(x); db.commit(); db.refresh(x)
    return _product(x)


@router.get("/listings")
def listings(db: Session = Depends(get_db)) -> list[dict]:
    verkaeufe = _verkaeufe_je_listing(db)
    return [_listing(x, verkaeufe.get(x.id, 0))
            for x in db.scalars(select(PodListing).order_by(PodListing.updated_at.desc())).all()]


@router.post("/listings", status_code=201)
def create_listing(body: ListingIn, db: Session = Depends(get_db)) -> dict:
    x = PodListing(**body.model_dump(), status="draft")
    db.add(x); db.commit(); db.refresh(x)
    return _listing(x)


@router.get("/orders")
def orders(db: Session = Depends(get_db)) -> list[dict]:
    return [_bestellung(db, x) for x in db.scalars(select(PodOrder).order_by(PodOrder.created_at.desc())).all()]


@router.post("/orders/abgleich")
async def orders_abgleich(erzwingen: bool = False, db: Session = Depends(get_db)) -> dict:
    """Bestellungen bei eBay lesen und in die eigene Liste uebernehmen. Nur lesend."""
    from app.integrations.ebay import RealEbayClient
    from app.studio import bestellimport

    s = get_settings()
    if not (s.ebay_client_id and s.ebay_client_secret):
        raise HTTPException(status_code=400, detail="Kein eBay-Zugang eingerichtet - Bestellungen lassen sich nicht abrufen.")
    ebay = RealEbayClient(s)
    try:
        return await bestellimport.gleiche_ab(db, ebay, erzwingen=erzwingen)
    except Exception as exc:  # noqa: BLE001 - Anbieterfehler lesbar weitergeben
        raise HTTPException(status_code=502, detail=f"eBay-Abgleich fehlgeschlagen: {str(exc)[:200]}") from exc
    finally:
        if getattr(ebay, "_client", None) is not None:
            await ebay._client.aclose()


@router.post("/orders", status_code=201)
def create_order(body: OrderIn, db: Session = Depends(get_db)) -> dict:
    x = PodOrder(**body.model_dump(), status="new", ordered_at=datetime.utcnow())
    db.add(x); db.commit(); db.refresh(x)
    return {"id": x.id, "status": x.status}


@router.get("/ledger")
def ledger(db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": x.id, "kind": x.kind, "amount_eur": x.amount_eur, "reference": x.reference,
             "occurred_at": x.occurred_at} for x in db.scalars(select(PodLedgerEntry).order_by(PodLedgerEntry.created_at.desc())).all()]


@router.post("/ledger", status_code=201)
def create_ledger_entry(body: LedgerIn, db: Session = Depends(get_db)) -> dict:
    x = PodLedgerEntry(**body.model_dump(), occurred_at=datetime.utcnow())
    db.add(x); db.commit(); db.refresh(x)
    return {"id": x.id, "kind": x.kind, "amount_eur": x.amount_eur}
