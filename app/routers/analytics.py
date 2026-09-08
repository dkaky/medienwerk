"""Analytics-/Dashboard-Endpoints: KPIs, Orders, Profit-Zeitreihe, Aktivitaet."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import analytics_service

router = APIRouter(prefix="/api/v1/dashboard", tags=["Dashboard & Analytics"])


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    """Kompakte Kennzahlen fuer die Uebersicht (Listings, Umsatz, Profit, Tasks)."""
    return analytics_service.dashboard_summary(db)


@router.get("/orders")
def orders(status: str | None = None, limit: int = Query(1000, ge=1, le=2000),
           db: Session = Depends(get_db)):
    """Verkaufs-/Auftragsliste (chronologisch nach Verkaufsdatum, neueste zuerst)."""
    return analytics_service.list_orders(db, status=status, limit=limit)


@router.get("/products")
def products(status: str | None = None, limit: int = Query(1000, ge=1, le=5000),
             db: Session = Depends(get_db)):
    """eBay-Produkt-/Listing-Übersicht (SKU, Item/Offer, Status, Preis, Bestand)."""
    return analytics_service.list_ebay_products(db, status=status, limit=limit)


@router.get("/products/{listing_id}")
def product_detail(listing_id: int, db: Session = Depends(get_db)):
    """Volldetails eines Listings (Bilder, Beschreibung, eBay-Link)."""
    detail = analytics_service.get_listing_detail(db, listing_id=listing_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Listing nicht gefunden")
    return detail


@router.get("/aliexpress-orders")
def aliexpress_orders(status: str | None = None, limit: int = Query(200, ge=1, le=500),
                      db: Session = Depends(get_db)):
    """Alle AliExpress-Bestellungen mit Kosten, Tracking und Kaufbeleg-Status."""
    return analytics_service.list_aliexpress_orders(db, status=status, limit=limit)


@router.post("/ebay-import")
async def ebay_import(days: int = Query(90, ge=1, le=90), db: Session = Depends(get_db)):
    """Echte eBay-Daten importieren: aktive Listings (Trading API) + Verkäufe (getOrders)."""
    from app.services import ebay_import_service
    try:
        return await ebay_import_service.import_all(db, days=days)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"eBay-Import fehlgeschlagen: {exc}")


@router.post("/sync-stats")
async def sync_stats(db: Session = Depends(get_db)):
    """Verkäufe gesamt (QuantitySold) + Aufrufe 30 Tage (Traffic-Report) aktualisieren."""
    from app.services import ebay_import_service
    try:
        return await ebay_import_service.sync_listing_stats(db)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Statistik-Sync fehlgeschlagen: {exc}")


@router.get("/profit")
def profit(days: int = Query(30, ge=1, le=365), db: Session = Depends(get_db)):
    """Taegliche Umsatz-/Profit-/Order-Zeitreihe der letzten `days` Tage."""
    return analytics_service.profit_timeseries(db, days=days)


@router.get("/activity")
def activity(limit: int = Query(25, ge=1, le=100), db: Session = Depends(get_db)):
    """Letzte Aktivitaeten (Task-Log) fuer den Feed."""
    return analytics_service.recent_activity(db, limit=limit)


@router.get("/portfolio")
def portfolio(db: Session = Depends(get_db)):
    """Portfolio-Analyse: Konzentration, Trichter, Neuzugaenge, Preisbaender (nur lesen)."""
    return analytics_service.get_portfolio_analysis(db)
