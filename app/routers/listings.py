"""Bereich 4: Listing-Optimierung-Endpoints (Spec Kap. 3.4)."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.schemas import (
    ListingPerformanceResponse,
    OptimizeStageRequest,
    OptimizeStageResponse,
    WeeklyOptimizeRequest,
)
from app.services import optimization_service

logger = logging.getLogger("app.routers.listings")
router = APIRouter(prefix="/api/v1", tags=["Bereich 4 – Listing-Optimierung"])


def _run_refresh_clicks() -> None:
    """Hintergrund-Refresh der eBay-Klickzahlen (Freigabe des Locks im finally)."""
    db = SessionLocal()
    try:
        r = asyncio.run(optimization_service.refresh_click_data(db))
        logger.info("click refresh done", extra=r)
    except Exception as exc:  # noqa: BLE001
        logger.error("click refresh failed", extra={"error": str(exc)})
    finally:
        db.close()
        optimization_service.release_opt()


@router.get("/listings/performance", response_model=ListingPerformanceResponse)
def performance(
    status: str | None = Query(default="active"),
    clicks_min: int | None = Query(default=None),
    clicks_max: int | None = Query(default=None),
    days: int = Query(default=7),
    db: Session = Depends(get_db),
):
    return optimization_service.list_performance(
        db, status=status, clicks_min=clicks_min, clicks_max=clicks_max
    )


@router.post("/listings/optimize/{listing_id}/stage/{stage}",
             response_model=OptimizeStageResponse, status_code=202)
async def optimize_stage(
    listing_id: int, stage: int, body: OptimizeStageRequest, db: Session = Depends(get_db)
):
    try:
        return await optimization_service.apply_stage(
            db, listing_id=listing_id, stage=stage,
            new_title=body.new_title, new_category=body.new_category,
            new_image_index=body.new_image_index, reason=body.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/scheduler/optimize-listings-weekly", status_code=202)
async def optimize_weekly(background: BackgroundTasks, body: WeeklyOptimizeRequest,
                          db: Session = Depends(get_db)):
    """Scheduler-Kompatibilität: dry_run frischt Klickzahlen auf (kein LLM)."""
    if body.dry_run:
        return await optimization_service.run_weekly_optimization(db, dry_run=True)
    if not optimization_service.try_acquire_opt():
        return {"status": "running", "message": "Läuft bereits."}
    background.add_task(_run_refresh_clicks)
    return {"status": "started"}


@router.get("/optimization/status")
def optimization_status():
    """Läuft gerade ein Hintergrund-Refresh? (fürs Dashboard-Polling) + letztes Ergebnis."""
    return {"running": optimization_service.is_opt_running(),
            "last_refresh": optimization_service.last_refresh() or None}


@router.get("/optimization/candidates")
def optimization_candidates(max_clicks: int = Query(default=0, ge=0, le=100),
                            clicks_exact: int | None = Query(default=None, ge=0, le=100000),
                            db: Session = Depends(get_db)):
    """Aktive Optimierungs-Kandidaten. ``clicks_exact`` = NUR Listings mit genau dieser
    Klickzahl (freier Filter); sonst höchstens ``max_clicks``. Älteste zuerst,
    kürzlich optimierte 14 Tage ausgeblendet."""
    return optimization_service.list_candidates(db, max_clicks=max_clicks,
                                                clicks_exact=clicks_exact)


@router.get("/optimization/buckets")
def optimization_buckets(db: Session = Depends(get_db)):
    """Kandidaten in 3 Funnel-Buckets mit je passendem Hebel: price (Klicks ohne
    Verkauf -> Preis/Angebot), title (Impressionen ohne Klick -> Titel/Bild),
    reactivate (kalt gewordener Selbstläufer -> Preis/Saison/Relist)."""
    return optimization_service.list_optimization_buckets(db)


@router.post("/optimization/{listing_id}/suggest")
async def optimization_suggest(listing_id: int, db: Session = Depends(get_db)):
    """„Optimieren": Titel-Vorschlag UND Marktanalyse (Konkurrenzpreise, Preisempfehlung
    je Variante, Push-Empfehlungen) in EINEM Schritt. Propose-only – ändert nichts."""
    try:
        return await optimization_service.optimize_one(db, listing_id=listing_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"{type(exc).__name__}: {exc}")


@router.post("/optimization/{listing_id}/market-analysis")
async def optimization_market(listing_id: int, db: Session = Depends(get_db)):
    """Nur die Marktanalyse (propose-only). Der UI-Button ist in „Optimieren" integriert;
    dieser Endpoint bleibt für Direktaufrufe/Kompatibilität bestehen."""
    try:
        return await optimization_service.market_analysis(db, listing_id=listing_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Marktanalyse fehlgeschlagen: {exc}")


@router.post("/optimization/{listing_id}/assess-images")
async def optimization_assess_images(listing_id: int, db: Session = Depends(get_db)):
    """KI-Bildbewertung (Vision): bewertet die Bilder der eBay-Anzeige und schlägt das
    beste Titelbild vor. Propose-only – ändert nichts."""
    try:
        return await optimization_service.assess_images(db, listing_id=listing_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Bildbewertung fehlgeschlagen: {exc}")


@router.post("/optimization/{listing_id}/cleanup-check")
async def optimization_cleanup_check(listing_id: int, db: Session = Depends(get_db)):
    """Stufe-B-Preisurteil fuer einen Aufraeum-Kandidaten (deutlich teurer + nicht
    verlustfrei senkbar = loeschen erwaegen). Nur Entscheidungshilfe, aendert nichts."""
    try:
        return await optimization_service.cleanup_price_verdict(db, listing_id=listing_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Preis-Check fehlgeschlagen: {exc}")


@router.get("/optimization/volume-pricing-candidates")
def optimization_volume_candidates(db: Session = Depends(get_db)):
    """Alle Listings, bei denen sich Mengenrabatt lohnt (read-only, propose-only)."""
    return optimization_service.list_volume_pricing_candidates(db)


@router.post("/optimization/{listing_id}/activate-volume-pricing")
async def optimization_activate_volume(listing_id: int, db: Session = Depends(get_db)):
    """Empfohlenen Mengenrabatt auf eBay aktivieren – NUR auf manuellen Klick (Aussen-Aktion)."""
    try:
        return await optimization_service.activate_volume_pricing(db, listing_id=listing_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Aktivierung auf eBay fehlgeschlagen: {exc}")


@router.post("/optimization/volume-pricing/activate-all")
async def optimization_activate_all_volume(db: Session = Depends(get_db)):
    """Alle empfohlenen Mengenrabatte auf einen Klick aktivieren (Aussen-/Geld-Aktion)."""
    try:
        return await optimization_service.activate_all_volume_pricing(db)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Sammel-Aktivierung fehlgeschlagen: {exc}")


@router.post("/optimization/volume-pricing/auto-remove-unprofitable")
async def optimization_auto_remove_multibuy(db: Session = Depends(get_db)):
    """Jetzt alle unrentabel gewordenen Mengenrabatte von eBay entfernen (dieselbe Logik wie die
    taegliche Automatik). Manueller Klick uebergeht den Schalter (explizite Zustimmung)."""
    try:
        return await optimization_service.auto_remove_unprofitable_multibuy(db, respect_toggle=False)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Auto-Entfernen fehlgeschlagen: {exc}")


@router.get("/optimization/cleanup/count")
def optimization_cleanup_count(db: Session = Depends(get_db)):
    """Wie viele Aufraeum-Kandidaten stehen gerade an (nie verkauft, 0 Klicks, wenig Impressionen)."""
    ids = optimization_service.cleanup_candidate_ids(db)
    return {"count": len(ids), "listing_ids": ids}


@router.post("/optimization/cleanup/end-all")
async def optimization_cleanup_end_all(body: dict = Body(default={}), db: Session = Depends(get_db)):
    """ALLE Aufraeum-Kandidaten auf eBay beenden – NUR auf ausdruecklichen Nutzer-Klick.

    Laeuft im HINTERGRUND (je Listing ein eBay-Call, sonst Gateway-Timeout) und kehrt sofort mit
    der Anzahl zurueck. Die Liste wird serverseitig neu berechnet; eine mitgeschickte ID-Liste
    kann sie nur einschraenken. Je Listing greift unmittelbar davor eine zweite Pruefung.
    """
    import asyncio

    ids = [int(x) for x in (body or {}).get("listing_ids") or []] or None
    valid = optimization_service.cleanup_candidate_ids(db)
    n = len([i for i in valid if not ids or i in set(ids)])
    if not n:
        return {"started": False, "count": 0}

    async def _run():
        from app.database import SessionLocal
        _db = SessionLocal()
        try:
            res = await optimization_service.end_cleanup_candidates(_db, listing_ids=ids)
            logger.info("cleanup end-all", extra={"beendet": res["n_beendet"],
                        "uebersprungen": res["n_uebersprungen"], "fehler": res["n_fehlgeschlagen"]})
        except Exception as exc:  # noqa: BLE001
            logger.error("cleanup end-all failed", extra={"error": str(exc)[:200]})
        finally:
            _db.close()

    asyncio.create_task(_run())
    return {"started": True, "count": n}


@router.post("/optimization/{listing_id}/deactivate-volume-pricing")
async def optimization_deactivate_volume(listing_id: int, db: Session = Depends(get_db)):
    """Aktiven Mengenrabatt wieder von eBay entfernen."""
    try:
        return await optimization_service.deactivate_volume_pricing(db, listing_id=listing_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Entfernen fehlgeschlagen: {exc}")


@router.post("/optimization/{listing_id}/volume-pricing")
def optimization_volume_pricing(listing_id: int, db: Session = Depends(get_db)):
    """Multi-Buy-Rabatt-Empfehlung (Mengenrabatt) fuer ein Listing – read-only, propose-only."""
    try:
        return optimization_service.suggest_volume_pricing(db, listing_id=listing_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/optimization/{listing_id}/dismiss-cleanup")
def optimization_dismiss_cleanup(listing_id: int, undo: bool = Query(default=False),
                                 db: Session = Depends(get_db)):
    """Aufraeum-Empfehlung verwerfen ('behalten', undo=false) bzw. zuruecknehmen
    (undo=true). Loescht/beendet NICHTS – nur das Ausblenden aus dem cleanup-Bucket."""
    try:
        return optimization_service.dismiss_cleanup(db, listing_id=listing_id, undo=undo)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/optimization/{listing_id}/dismiss")
def optimization_dismiss(listing_id: int, undo: bool = Query(default=False),
                         db: Session = Depends(get_db)):
    """Optimierung 'so lassen' (verwerfen, undo=false) bzw. zuruecknehmen (undo=true).
    Der Artikel laeuft unveraendert weiter und verschwindet aus den Optimier-Buckets."""
    try:
        return optimization_service.dismiss_optimization(db, listing_id=listing_id, undo=undo)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/optimization/{listing_id}/apply-all")
async def optimization_apply_all(listing_id: int, body: dict = Body(default={}),
                                 db: Session = Depends(get_db)):
    """„Alle Änderungen annehmen": Titel + Variantenpreise + Anzeigentarif auf einmal
    übernehmen. body: {title?, prices?:{sku:preis}, ad_rate_pct?}. Markiert das Listing
    als optimiert (14 Tage ausgeblendet)."""
    try:
        return await optimization_service.apply_optimization(
            db, listing_id=listing_id, title=body.get("title"),
            prices=body.get("prices"), ad_rate_pct=body.get("ad_rate_pct"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Übernehmen fehlgeschlagen: {exc}")


@router.post("/optimization/refresh-clicks", status_code=202)
def optimization_refresh_clicks(background: BackgroundTasks):
    """Klickzahlen aller aktiven Listings im Hintergrund von eBay auffrischen."""
    if not optimization_service.try_acquire_opt():
        return {"status": "running", "message": "Refresh läuft bereits."}
    background.add_task(_run_refresh_clicks)
    return {"status": "started"}


# -------- Propose-only: KI-Titelvorschlaege reviewen (nichts geht ohne Freigabe live) --------
@router.get("/optimization/suggestions")
def optimization_suggestions(db: Session = Depends(get_db)):
    """Offene KI-Titelvorschlaege (current vs. suggested) fuers Optimierungs-Tab."""
    return optimization_service.list_suggestions(db)


@router.post("/optimization/{listing_id}/accept")
async def optimization_accept(listing_id: int, db: Session = Depends(get_db)):
    """Vorschlag freigeben: neuen Titel auf eBay setzen + lokal uebernehmen."""
    try:
        return await optimization_service.accept_suggestion(db, listing_id=listing_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 – eBay-Fehler voll durchreichen
        raise HTTPException(status_code=422, detail=f"{type(exc).__name__}: {exc}")


@router.post("/optimization/{listing_id}/reject")
def optimization_reject(listing_id: int, db: Session = Depends(get_db)):
    """Vorschlag verwerfen: Titel bleibt unveraendert."""
    try:
        return optimization_service.reject_suggestion(db, listing_id=listing_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
