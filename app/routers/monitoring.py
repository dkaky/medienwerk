"""Monitoring-Endpoints: Lieferanten-Ueberwachung (EK/Bestand; KEINE Auto-Preise)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.schemas import (
    MonitoringStatusResponse,
    MonitoringSyncResponse,
    SyncListingResponse,
)
from app.services import monitoring_service

logger = logging.getLogger("app.routers.monitoring")
router = APIRouter(prefix="/api/v1/monitoring", tags=["Monitoring & Repricing"])

_sync_bg = {"active": False}


async def _run_sync_background() -> None:
    # BEWUSST async: laeuft dadurch auf dem Haupt-Event-Loop (nicht Threadpool +
    # asyncio.run). Der gecachte AliExpress-Client (httpx.AsyncClient + Lock) ist
    # an den Haupt-Loop gebunden - ein zweiter Loop wuerde ihn im Real-Modus
    # zerstoeren ("Event loop is closed" fuer alle folgenden Scheduler-Laeufe).
    _sync_bg["active"] = True
    db = SessionLocal()
    try:
        # ZUERST die (schnellen) echten eBay-Preise + Report-Rebuild: die Drift-Anzeige im Cockpit
        # ist so nach ~1-2 Min da, OHNE auf den langsamen AliExpress-Lieferanten-Abgleich zu warten
        # (Fund 17.07.: Nutzer sah lange weiter den internen statt echten eBay-Preis). Deckt "intern
        # != eBay"-Drift auf (z.B. Preis nie zu eBay gepusht). Best-effort, entkoppelt vom EK-Sync.
        try:
            from app.services.ebay_import_service import sync_ebay_live_prices
            from app.services.listing_match_service import rebuild_reprice_report

            await sync_ebay_live_prices(db)
            rebuild_reprice_report(db)
        except Exception as exc:  # noqa: BLE001
            logger.error("ebay price sync failed", extra={"error": str(exc)})
        # DANN der langsame Lieferanten-Abgleich (EK/Bestand) + finaler Report-Rebuild.
        try:
            from app.services.listing_match_service import rebuild_reprice_report

            await monitoring_service.run_monitoring(db, only_auto=False)
            rebuild_reprice_report(db)
        except Exception as exc:  # noqa: BLE001
            logger.error("background sync failed", extra={"error": str(exc)})
        # Zustellungen erkennen: „Unterwegs" -> „Zugestellt", sobald der Zusteller-Status es meldet.
        # Getrennte try-Bloecke (wie im Scheduler), damit ein Fehler die Zeit-Heuristik nicht mitreisst.
        from app.services import order_service
        try:
            await order_service.promote_delivered_from_dhl(db)   # echter DHL-Zustellstatus zuerst
        except Exception as exc:  # noqa: BLE001
            logger.error("dhl delivered detection failed", extra={"error": str(exc)})
        try:
            await order_service.promote_delivered_from_tracking(db)
        except Exception as exc:  # noqa: BLE001
            logger.error("delivered detection failed", extra={"error": str(exc)})
        try:
            order_service.promote_stale_tracking(db)   # Rest per Zeit-Frist
        except Exception as exc:  # noqa: BLE001
            logger.error("stale-promotion failed", extra={"error": str(exc)})
    finally:
        db.close()
        _sync_bg["active"] = False


@router.get("/status", response_model=MonitoringStatusResponse)
def status(db: Session = Depends(get_db)):
    """Pro-Listing-Status (Preis/Kosten/Marge/Bestand) + Aggregat."""
    return monitoring_service.monitor_status(db)


@router.post("/sync", response_model=MonitoringSyncResponse)
async def sync_all(only_auto: bool = False, db: Session = Depends(get_db)):
    """Alle verknuepften Listings jetzt mit dem Lieferanten abgleichen (EK/Bestand;
    Preise werden NIE automatisch geaendert - Freigabe im Preis-Check)."""
    return await monitoring_service.run_monitoring(db, only_auto=only_auto)


@router.post("/sync-background", status_code=202)
def sync_all_background(background: BackgroundTasks, db: Session = Depends(get_db)):
    """Kompletter Lieferanten-Abgleich im Hintergrund (Dashboard-Knopf).

    Bei ~300 verknuepften Listings dauert der Lauf durch das AliExpress-Rate-Limit
    einige Minuten -> nicht synchron im Request. Fortschritt: pcLastSync im UI.
    """
    if _sync_bg["active"]:
        return {"status": "running", "message": "Abgleich läuft bereits."}
    background.add_task(_run_sync_background)
    return {"status": "started"}


@router.post("/sync/{listing_id}", response_model=SyncListingResponse)
async def sync_one(listing_id: int, db: Session = Depends(get_db)):
    """Ein einzelnes Listing sofort abgleichen (EK/Bestand; Preise werden nie
    automatisch geaendert - Freigabe im Preis-Check)."""
    try:
        return await monitoring_service.sync_listing(db, listing_id=listing_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
