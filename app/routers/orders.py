"""Bereich 2: Auftragsabwicklung-Endpoints (Spec Kap. 3.2)."""
from __future__ import annotations

import logging

from fastapi import (APIRouter, BackgroundTasks, Body, Depends, File, Header,
                     HTTPException, Request, UploadFile)
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.database import SessionLocal, get_db
from app.integrations import get_ebay_client
from app.retry import PersistentError
from app.schemas import FulfillRequest, FulfillResponse, IpnAck, TrackingResponse
from app.services import order_service

logger = logging.getLogger("app.routers.orders")
router = APIRouter(tags=["Bereich 2 – Auftragsabwicklung"])


def _background_fulfill(sale_id: int) -> None:
    """Hintergrund-Job nach IPN: eigene DB-Session, Fehler werden geloggt."""
    import asyncio

    db = SessionLocal()
    try:
        asyncio.run(order_service.fulfill_sale(db, sale_id=sale_id))
    except Exception as exc:  # noqa: BLE001 – Fehler landen in task_logs
        logger.error("background fulfill failed", extra={"sale_id": sale_id, "error": str(exc)})
    finally:
        db.close()


@router.post("/api/v1/sales/webhook/ebay-ipn", response_model=IpnAck)
async def ebay_ipn(
    request: Request,
    background: BackgroundTasks,
    x_ebay_sig: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """IPN-Listener: eBay schnell mit 200 bestaetigen, Verarbeitung asynchron."""
    raw = await request.body()
    # Die Signaturpruefung kann (Real-Client, Public-Key-Cache-Miss) synchrone
    # Netzwerk-Calls machen -> in den Threadpool auslagern, damit der Event-Loop
    # nicht blockiert (eingehende IPNs sind extern getriggert).
    valid = await run_in_threadpool(
        get_ebay_client().verify_ipn_signature, raw, x_ebay_sig or ""
    )
    if not valid:
        raise HTTPException(status_code=401, detail="IPN-Signatur ungueltig")

    # eBay sendet x-www-form-urlencoded; JSON wird ebenfalls akzeptiert.
    payload: dict
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = await request.json()
    else:
        form = await request.form()
        payload = dict(form)

    try:
        sale = order_service.ingest_ipn(db, payload)
    except PersistentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Sicherheit: AliExpress-Bestellung nur automatisch ausloesen, wenn ausdruecklich
    # per Config (AUTO_FULFILL=true) freigegeben. Standard = manuelle Freigabe im
    # Dashboard (Bestellung gibt echtes Geld aus).
    from app.config import get_settings
    if get_settings().auto_fulfill:
        background.add_task(_background_fulfill, sale.id)
    return IpnAck(status="received")


@router.post("/api/v1/orders/fulfill/{sale_id}", response_model=FulfillResponse, status_code=202)
async def fulfill(sale_id: int, body: FulfillRequest, db: Session = Depends(get_db)):
    try:
        return await order_service.fulfill_sale(
            db, sale_id=sale_id, approve_variant=body.approve_variant,
            override_delivery_name=body.override_delivery_name,
            source_aliexpress_id=body.source_aliexpress_id,
            sku_attr=body.sku_attr, force=body.force,
            # NUR der manuelle Klick (= Freigabe pro Bestellung) darf auf die im
            # Preis-Check verknuepfte Ausweich-Variante routen; Hintergrund-Fulfillment
            # (auto_fulfill/IPN) bleibt beim Default False.
            allow_alt_routing=True,
        )
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/api/v1/orders/fulfill-group/{ebay_order_id}", status_code=202)
async def fulfill_group(ebay_order_id: str, db: Session = Depends(get_db)):
    """ALLE offenen Positionen einer eBay-Bestellung zusammen bei AliExpress bestellen."""
    try:
        return await order_service.fulfill_ebay_order(db, ebay_order_id=ebay_order_id)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/api/v1/orders/{sale_id}/source-options")
async def source_options(sale_id: int, db: Session = Depends(get_db)):
    """Quellen-Vergleich fuer eine offene Bestellung (guenstigste lieferbare zuerst).

    Reiner VORSCHLAG – bestellt nichts; der Nutzer waehlt die Quelle explizit.
    """
    from app.services import supplier_service
    try:
        return await supplier_service.source_options_for_sale(db, sale_id=sale_id)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/api/v1/orders/{sale_id}/discard-order")
def discard_order(sale_id: int, db: Session = Depends(get_db)):
    """Verfallene/unbezahlte AliExpress-Bestellung verwerfen -> Sale wieder bestellbar (pending).
    Verwirft NIE eine versandte/getrackte Bestellung. AliExpress wird nicht angefasst."""
    try:
        return order_service.discard_order_for_reorder(db, sale_id=sale_id)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/api/v1/orders/{sale_id}/mark-cancelled")
def mark_cancelled(sale_id: int, body: dict = Body(default={}), db: Session = Depends(get_db)):
    """Sale manuell als storniert markieren (Fallback, z.B. bis der Sync es erkennt).

    body {"undo": true} macht eine versehentliche Stornierung rueckgaengig.
    """
    try:
        return order_service.mark_sale_cancelled(db, sale_id=sale_id,
                                                 undo=bool(body.get("undo")))
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/api/v1/orders/{sale_id}/set-status")
def set_status(sale_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    """Status eines Verkaufs manuell setzen (UI-Dropdown). Nur lokal, nie AliExpress/eBay."""
    try:
        return order_service.set_sale_status(db, sale_id=sale_id,
                                             status=str(body.get("status") or ""))
    except PersistentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/v1/orders/{sale_id}/cancel-reviewed")
def cancel_reviewed(sale_id: int, body: dict = Body(default={}), db: Session = Depends(get_db)):
    """Storno abhaken -> faellt aus der 'bitte pruefen'-Liste."""
    try:
        return order_service.mark_cancel_reviewed(
            db, sale_id=sale_id, reviewed=body.get("reviewed", True))
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/api/v1/orders/{sale_id}/mark-paid")
def mark_paid(sale_id: int, body: dict = Body(default={}), db: Session = Depends(get_db)):
    """'Bei AliExpress bezahlt' markieren/zuruecknehmen (rein lokale Notiz, kein Geld-Call)."""
    try:
        return order_service.mark_ae_paid(db, sale_id=sale_id,
                                          paid=bool(body.get("paid", True)))
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/api/v1/orders/bulk-set-status")
def bulk_set_status(body: dict = Body(...), db: Session = Depends(get_db)):
    """Alle Sales aus 'from' auf 'to' setzen (einmalige Bereinigung, z.B. alte
    pending -> ordered_aliexpress). Nur lokal."""
    frm = body.get("from") or []
    if isinstance(frm, str):
        frm = [frm]
    try:
        return order_service.bulk_set_status(db, from_statuses=list(frm),
                                             to_status=str(body.get("to") or ""))
    except PersistentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/api/v1/orders/{order_id}/tracking", response_model=TrackingResponse)
async def tracking(order_id: int, db: Session = Depends(get_db)):
    try:
        return await order_service.refresh_tracking(db, order_id=order_id)
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/api/v1/orders/{sale_id}/pull-ebay-tracking")
async def pull_ebay_tracking(sale_id: int, db: Session = Depends(get_db)):
    """Selbst verschickt + Nummer auf eBay eingetragen: die Sendungsnummer aus eBay LESEN und
    lokal hinterlegen (ohne erneut an eBay zu melden). Kein Geld-/Schreibcall."""
    try:
        return await order_service.record_ebay_tracking(db, sale_id=sale_id)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"eBay-Tracking-Abruf fehlgeschlagen: {exc}")


@router.post("/api/v1/orders/backfill-tracking")
async def backfill_tracking(db: Session = Depends(get_db)):
    """Alle auf eBay versendeten Orders ohne lokale Sendungsnummer aus eBay nachziehen."""
    try:
        return await order_service.backfill_tracking_from_ebay(db)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Tracking-Nachzug fehlgeschlagen: {exc}")


@router.post("/api/v1/orders/sync")
async def sync_orders(days: int = 30, db: Session = Depends(get_db)):
    """Neue eBay-Bestellungen holen und als Sale (pending) anlegen. Bestellt NICHTS."""
    try:
        return await order_service.sync_ebay_orders(db, days=days)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"eBay-Order-Sync fehlgeschlagen: {exc}")


@router.post("/api/v1/orders/import-json")
async def import_orders_json(request: Request, db: Session = Depends(get_db)):
    """Browser-Extraktion der AliExpress-Bestellhistorie importieren (Raw-JSON-Body).

    Akzeptiert text/plain (no-cors-POST aus dem Browser). Rohdaten werden zusätzlich
    als Backup-Datei abgelegt.
    """
    import json as _json
    from datetime import datetime, timezone
    from pathlib import Path

    from app.services import order_import_service
    raw = await request.body()
    try:
        rows = _json.loads(raw.decode("utf-8", errors="replace"))
        assert isinstance(rows, list)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Body muss eine JSON-Liste sein")
    backup = Path("./data") / f"ae_orders_browser_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(raw)
    result = order_import_service.import_browser_orders(db, rows)
    logger.info("browser order import", extra=result)
    return {**result, "backup": str(backup)}


@router.post("/api/v1/orders/import-file")
async def import_orders_file(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """AliExpress-/AutoDS-Kaufhistorie aus CSV importieren (Einkaufspreise + Tracking)."""
    from app.services import order_import_service
    content = await file.read()
    try:
        return order_import_service.import_orders_csv(db, content=content)
    except PersistentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Import fehlgeschlagen: {exc}")


@router.post("/api/v1/orders/sync-tracking")
async def sync_tracking(db: Session = Depends(get_db)):
    """Manuell: Tracking aller offenen AliExpress-Bestellungen abrufen + an eBay melden."""
    from app.services import order_service as osvc
    return await osvc.sync_tracking_all(db)


@router.get("/api/v1/orders/{sale_id}/variant-options")
async def variant_options(sale_id: int, db: Session = Depends(get_db)):
    """AliExpress-Varianten des Sale-Listings (fuer die manuelle Zuordnung).

    Laedt fehlende Varianten live von AliExpress nach und persistiert sie.
    """
    try:
        return await order_service.variant_options(db, sale_id=sale_id)
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/api/v1/orders/{sale_id}/variant")
def set_variant(sale_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    """eBay-Auswahl -> AliExpress-Variante zuordnen (wird am Listing gelernt)."""
    attr = str(body.get("attr") or "").strip()
    if not attr:
        raise HTTPException(status_code=400, detail="attr fehlt")
    try:
        return order_service.set_sale_variant(db, sale_id=sale_id, attr=attr)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


_backfill_costs_running = False


def _run_cost_backfill() -> None:
    global _backfill_costs_running
    import asyncio as _asyncio
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        res = _asyncio.run(order_service.backfill_real_order_costs(db))
        logger.info("EK-Backfill fertig: %s", {k: v for k, v in res.items() if k != "changes"})
    except Exception as exc:  # noqa: BLE001
        logger.error("EK-Backfill fehlgeschlagen: %s", str(exc)[:200])
    finally:
        db.close()
        _backfill_costs_running = False


@router.post("/api/v1/orders/backfill-real-costs", status_code=202)
async def backfill_real_costs(background: BackgroundTasks):
    """ECHTE AliExpress-Order-Summen (inkl. Versand + versteckter Steuer) fuer alle
    bestehenden Bestellungen nachziehen -> reale EKs/Gewinne statt Schaetzungen.
    Laeuft im Hintergrund (viele API-Calls -> Minuten); Doppel-Lauf wird abgewiesen."""
    global _backfill_costs_running
    if _backfill_costs_running:
        return {"status": "running", "message": "EK-Backfill läuft bereits."}
    _backfill_costs_running = True
    try:
        background.add_task(_run_cost_backfill)
    except BaseException:
        _backfill_costs_running = False
        raise
    return {"status": "started"}


@router.post("/api/v1/orders/{sale_id}/set-cost")
def set_cost(sale_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    """Tatsaechlichen Einkaufspreis (EK, EUR) manuell am Verkauf hinterlegen — ohne Beleg-Datei.

    Fuer die Doppelklick-Korrektur in den Verkaeufen. body: {"amount": 10.48}
    """
    if "amount" not in body:
        raise HTTPException(status_code=400, detail="amount fehlt")
    try:
        return order_service.set_sale_actual_cost(db, sale_id=sale_id, amount_eur=body.get("amount"))
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/api/v1/orders/{order_id}/report-tracking")
async def report_tracking(order_id: int, db: Session = Depends(get_db)):
    """Sendungsnummer an eBay melden (Order gilt als versandt)."""
    try:
        return await order_service.report_tracking_to_ebay(db, order_id=order_id)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Tracking-Meldung fehlgeschlagen: {exc}")


@router.post("/api/v1/orders/{sale_id}/manual-tracking")
async def manual_tracking(sale_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    """Sendungsnummer von Hand nachtragen und an eBay melden.

    Fuer manuell auf AliExpress bestellte Sales (keine Order-Zeile) ODER wenn die
    AliExpress-API die Nummer nicht liefert. body:
    {"tracking_number": "...", "carrier"?: "...", "aliexpress_order_id"?: "..."}
    """
    try:
        return await order_service.add_manual_tracking(
            db, sale_id=sale_id,
            tracking_number=str(body.get("tracking_number") or ""),
            carrier=(body.get("carrier") or None),
            aliexpress_order_id=(body.get("aliexpress_order_id") or None),
        )
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Tracking-Meldung fehlgeschlagen: {exc}")
