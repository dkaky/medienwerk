"""Kanalneutrale Betriebsübersicht für Print-on-Demand.

Diese API speichert nur eigene Betriebsdaten. Sie importiert keine Produkte,
bestellt nichts und veröffentlicht nichts bei einem Verkaufskanal.
"""
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.studio.models import PodLedgerEntry, PodListing, PodOrder, PodProduct

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


def _product(x: PodProduct) -> dict:
    return {"id": x.id, "title": x.title, "status": x.status, "provider": x.provider,
            "base_cost_eur": x.base_cost_eur, "target_price_eur": x.target_price_eur,
            "stock_mode": x.stock_mode, "created_at": x.created_at}


def _listing(x: PodListing) -> dict:
    return {"id": x.id, "product_id": x.product_id, "channel": x.channel, "status": x.status,
            "price_eur": x.price_eur, "quantity_available": x.quantity_available, "url": x.url}


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)) -> dict:
    listings = list(db.scalars(select(PodListing).order_by(PodListing.updated_at.desc()).limit(8)).all())
    orders = list(db.scalars(select(PodOrder).order_by(PodOrder.created_at.desc()).limit(8)).all())
    revenue = db.scalar(select(func.coalesce(func.sum(PodOrder.sale_total_eur), 0.0))) or 0.0
    cost = db.scalar(select(func.coalesce(func.sum(PodOrder.fulfillment_cost_eur), 0.0))) or 0.0
    return {
        "kpis": {"products": db.scalar(select(func.count()).select_from(PodProduct)) or 0,
                 "active_listings": db.scalar(select(func.count()).select_from(PodListing).where(PodListing.status == "active")) or 0,
                 "open_orders": db.scalar(select(func.count()).select_from(PodOrder).where(PodOrder.status.in_(("new", "needs_review")))) or 0,
                 "revenue_eur": round(float(revenue), 2), "contribution_eur": round(float(revenue - cost), 2)},
        "listings": [_listing(x) for x in listings],
        "orders": [{"id": x.id, "channel": x.channel, "external_id": x.external_id,
                    "status": x.status, "sale_total_eur": x.sale_total_eur,
                    "fulfillment_cost_eur": x.fulfillment_cost_eur} for x in orders],
    }


@router.get("/products")
def products(db: Session = Depends(get_db)) -> list[dict]:
    return [_product(x) for x in db.scalars(select(PodProduct).order_by(PodProduct.updated_at.desc())).all()]


@router.post("/products", status_code=201)
def create_product(body: ProductIn, db: Session = Depends(get_db)) -> dict:
    x = PodProduct(**body.model_dump(), status="draft")
    db.add(x); db.commit(); db.refresh(x)
    return _product(x)


@router.get("/listings")
def listings(db: Session = Depends(get_db)) -> list[dict]:
    return [_listing(x) for x in db.scalars(select(PodListing).order_by(PodListing.updated_at.desc())).all()]


@router.post("/listings", status_code=201)
def create_listing(body: ListingIn, db: Session = Depends(get_db)) -> dict:
    x = PodListing(**body.model_dump(), status="draft")
    db.add(x); db.commit(); db.refresh(x)
    return _listing(x)


@router.get("/orders")
def orders(db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": x.id, "channel": x.channel, "external_id": x.external_id, "status": x.status,
             "sale_total_eur": x.sale_total_eur, "fulfillment_cost_eur": x.fulfillment_cost_eur}
            for x in db.scalars(select(PodOrder).order_by(PodOrder.created_at.desc())).all()]


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
