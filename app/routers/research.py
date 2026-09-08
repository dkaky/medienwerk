"""Produkt-Research-Endpoints: AliExpress nach neuen margenstarken Produkten durchsuchen."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.services import product_research_service as research

logger = logging.getLogger("app.routers.research")
router = APIRouter(prefix="/api/v1/research", tags=["Produkt-Research"])

# Lauf-Lock liegt im Service (research.try_acquire_run/release_run/is_running),
# damit manuelle Suche, Trend-Suche UND der Scheduler-Job denselben Mutex teilen.


def _run_discovery(target: int, niches: list[str] | None,
                   min_cost: float = 0.0, max_cost: float = 0.0,
                   min_price: float = 0.0, min_profit: float = 0.0,
                   min_rating: float = 4.0, max_delivery: int = 10,
                   min_margin: float = 0.20, local_only: bool = False) -> None:
    db = SessionLocal()
    try:
        asyncio.run(research.discover(db, niches=niches, target=target,
                                      min_cost=min_cost, max_cost=max_cost,
                                      min_price=min_price, min_profit=min_profit,
                                      min_rating=min_rating, max_delivery=max_delivery,
                                      min_margin=min_margin, local_only=local_only))
    except Exception as exc:  # noqa: BLE001
        logger.error("discovery failed", extra={"error": str(exc)})
    finally:
        db.close()
        research.release_run()


def _run_trends(target: int, min_price: float, min_profit: float,
                min_rating: float, max_delivery: int, max_cost: float,
                min_margin: float = 0.20, local_only: bool = False) -> None:
    db = SessionLocal()
    try:
        asyncio.run(research.discover_trends(
            db, target=target, min_price=min_price, min_profit=min_profit,
            min_rating=min_rating, max_delivery=max_delivery, max_cost=max_cost,
            min_margin=min_margin, local_only=local_only))
    except Exception as exc:  # noqa: BLE001
        logger.error("trend research failed", extra={"error": str(exc)})
    finally:
        db.close()
        research.release_run()


def _run_winner_clone(target: int, min_price: float, min_profit: float,
                      min_rating: float, max_delivery: int, max_cost: float,
                      min_margin: float = 0.20, local_only: bool = False) -> None:
    db = SessionLocal()
    try:
        asyncio.run(research.winner_clone(
            db, target=target, min_price=min_price, min_profit=min_profit,
            min_rating=min_rating, max_delivery=max_delivery, max_cost=max_cost,
            min_margin=min_margin, local_only=local_only))
    except Exception as exc:  # noqa: BLE001
        logger.error("winner clone failed", extra={"error": str(exc)})
    finally:
        db.close()
        research.release_run()


def _num(v) -> float:
    """Robuste Zahl aus Nutzereingabe ('12,50' -> 12.5); ungültig/negativ -> 0."""
    try:
        return max(0.0, float(str(v).replace(",", ".")))
    except (TypeError, ValueError):
        return 0.0


def _crit(body: dict, key: str, default: float) -> float:
    """Basis-Kriterium aus dem Body: fehlend/leer/ungültig -> Default; 0 ist erlaubt
    (z.B. min_profit=0 = Gewinn-Filter aus)."""
    v = body.get(key)
    if v is None or v == "":
        return default
    try:
        return max(0.0, float(str(v).replace(",", ".")))
    except (TypeError, ValueError):
        return default


def _margin(body: dict, key: str, default: float) -> float:
    """Marge-Kriterium: akzeptiert Prozent (25) ODER Bruch (0.25). Wert > 1 -> als Prozent
    gedeutet und /100. Fehlend/leer/ungültig -> Default (0.25 = 25 %)."""
    v = body.get(key)
    if v is None or v == "":
        return default
    try:
        m = max(0.0, float(str(v).replace(",", ".")))
    except (TypeError, ValueError):
        return default
    return round(m / 100.0, 4) if m > 1 else round(m, 4)


@router.post("/ideas/prune")
def prune_ideas(body: dict = Body(default={}), db: Session = Depends(get_db)):
    """Alte unbearbeitete Produkt-Ideen loeschen (gegen den Stau). body: {days?: 5}.
    'gemerkt'/importierte Ideen bleiben erhalten."""
    try:
        days = int(body.get("days") or 5)
    except (TypeError, ValueError):
        days = 5
    return research.prune_old_ideas(db, days=days)


@router.post("/discover", status_code=202)
def discover(background: BackgroundTasks, body: dict = Body(default={}),
             db: Session = Depends(get_db)):
    """Startet die Produktsuche im Hintergrund (Rate-Limit -> dauert einige Minuten).

    Nutzer-Kriterien: target (1-200), niches (Liste ODER Komma-String; leer =
    Standard-Mix), min_cost/max_cost = EK-Spanne in EUR (0 = keine Grenze).
    """
    # Lock ATOMAR im Handler holen (vor add_task) – sonst startet ein zweiter
    # Klick im Fenster zwischen 202-Response und Task-Start einen Parallel-Lauf.
    if not research.try_acquire_run():
        return {"status": "running", "message": "Suche läuft bereits."}
    try:
        try:
            target = int(body.get("target") or 100)
        except (TypeError, ValueError):
            target = 100
        target = max(1, min(target, 200))
        niches = body.get("niches") or None
        if isinstance(niches, str):
            niches = [n.strip() for n in niches.split(",") if n.strip()] or None
        min_cost, max_cost = _num(body.get("min_cost")), _num(body.get("max_cost"))
        if max_cost and min_cost > max_cost:
            min_cost, max_cost = max_cost, min_cost
        # Basis-Kriterien (einstellbar; leer = Defaults). Neue Vorgabe 10.07.: kein VK-/
        # Gewinn-Zwang mehr (0 = aus), alle Preise/Kategorien – Bedingung ist die Mindest-Marge.
        min_price = _crit(body, "min_price", 0.0)
        min_profit = _crit(body, "min_profit", 0.0)
        min_rating = min(_crit(body, "min_rating", 4.0), 5.0)
        max_delivery = int(_crit(body, "max_delivery", 10)) or 10
        min_margin = _margin(body, "min_margin", 0.20)
        local_only = bool(body.get("local_only"))
        background.add_task(_run_discovery, target, niches, min_cost, max_cost,
                            min_price, min_profit, min_rating, max_delivery, min_margin,
                            local_only)
    except BaseException:
        research.release_run()   # Task nie eingereiht -> Lock sofort freigeben
        raise
    return {"status": "started", "target": target, "niches": len(niches) if niches else 0,
            "min_cost": min_cost, "max_cost": max_cost, "min_price": min_price,
            "min_profit": min_profit, "min_rating": min_rating, "max_delivery": max_delivery,
            "min_margin": min_margin}


@router.post("/discover-trends", status_code=202)
def discover_trends(background: BackgroundTasks, body: dict = Body(default={}),
                    db: Session = Depends(get_db)):
    """KI-Trend-Recherche starten: Claude sucht aktuelle Trends im Web und findet
    passende AliExpress-Produkte (Marge-Kriterien wie bei der normalen Suche)."""
    if not research.try_acquire_run():
        return {"status": "running", "message": "Eine Suche läuft bereits."}
    try:
        try:
            target = max(1, min(int(body.get("target") or 25), 200))
        except (TypeError, ValueError):
            target = 25
        min_price = _crit(body, "min_price", 0.0)
        min_profit = _crit(body, "min_profit", 0.0)
        min_rating = min(_crit(body, "min_rating", 4.0), 5.0)
        max_delivery = int(_crit(body, "max_delivery", 10)) or 10
        max_cost = _num(body.get("max_cost"))
        min_margin = _margin(body, "min_margin", 0.20)
        local_only = bool(body.get("local_only"))
        background.add_task(_run_trends, target, min_price, min_profit,
                            min_rating, max_delivery, max_cost, min_margin, local_only)
    except BaseException:
        research.release_run()
        raise
    return {"status": "started", "target": target, "min_margin": min_margin}


@router.post("/winner-clone", status_code=202)
def winner_clone_endpoint(background: BackgroundTasks, body: dict = Body(default={}),
                          db: Session = Depends(get_db)):
    """Gewinner klonen: KI leitet Suchbegriffe aus den eigenen Bestsellern ab und
    sucht damit AliExpress nach Varianten/Schwester-Produkten (Marge-Kriterien
    wie bei der normalen Suche). Ergebnis sind ProductIdeas — nichts geht live."""
    if not research.try_acquire_run():
        return {"status": "running", "message": "Eine Suche läuft bereits."}
    try:
        if not research.winner_dna_context(db):
            research.release_run()
            return {"status": "empty",
                    "message": 'Keine Gewinner mit genug Verkäufen gefunden — erst „Statistik laden" (eBay-Produkte) ausführen.'}
        try:
            target = max(1, min(int(body.get("target") or 25), 200))
        except (TypeError, ValueError):
            target = 25
        min_price = _crit(body, "min_price", 0.0)
        min_profit = _crit(body, "min_profit", 0.0)
        min_rating = min(_crit(body, "min_rating", 4.0), 5.0)
        max_delivery = int(_crit(body, "max_delivery", 10)) or 10
        max_cost = _num(body.get("max_cost"))
        min_margin = _margin(body, "min_margin", 0.20)
        local_only = bool(body.get("local_only"))
        background.add_task(_run_winner_clone, target, min_price, min_profit,
                            min_rating, max_delivery, max_cost, min_margin, local_only)
    except BaseException:
        research.release_run()
        raise
    return {"status": "started", "target": target, "min_margin": min_margin}


@router.get("/source-info/{aliexpress_id}")
async def source_info_endpoint(aliexpress_id: str):
    """Diagnose (nur lesen): Roh-Logistik + SKU-Achsen eines AliExpress-Produkts.
    Zweck: das „Versand aus"-Feld der echten API bestimmen (Lokal-Filter-Vorarbeit)."""
    return await research.source_info(aliexpress_id)


@router.get("/last-trends")
def last_trends():
    """Zuletzt recherchierte Trend-Begriffe (fuers Dashboard)."""
    return research.last_trends()


@router.get("/status")
def status():
    # letztes_ergebnis: erklaert im Dashboard, warum ein Lauf ggf. 0 Ideen brachte
    # (Nutzerfrage 19.08. — vorher gab es ausser running=false keinerlei Auskunft).
    return {"running": research.is_running(),
            "letztes_ergebnis": research.last_result()}


@router.get("/ideas")
def ideas(status: str | None = None, limit: int = 200, sort: str = "newest",
          niche: str | None = None, db: Session = Depends(get_db)):
    return research.list_ideas(db, status=status, limit=limit, sort=sort, niche=niche)


@router.post("/ideas/{idea_id}/status")
def set_idea_status(idea_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    return research.set_status(db, idea_id=idea_id, status=str(body.get("status") or "new"))


@router.post("/ideas/{idea_id}/alternatives")
async def alternatives(idea_id: int, db: Session = Depends(get_db)):
    """Per Bildsuche gleiche/ähnliche Produkte anderer Händler zur Idee finden."""
    return await research.find_alternatives(db, idea_id=idea_id)


@router.post("/ideas/{idea_id}/create", status_code=202)
async def create_from_idea(idea_id: int, body: dict = Body(default={}), db: Session = Depends(get_db)):
    """Idee -> Listing als eBay-Entwurf (publish=false) oder live (publish=true) anlegen.

    Läuft im HINTERGRUND und kehrt SOFORT zurück (status=importing) – so blockiert kein
    HTTP-Timeout mehr, wenn mehrere Ideen gleichzeitig angeklickt werden. Der Fortschritt
    steht am Idee-Status (importing -> imported / import_failed mit Klartext-Fehler)."""
    publish = bool(body.get("publish"))
    force = bool(body.get("force"))   # haengenden „importing"-Zombie sofort neu anstossen
    mp = body.get("min_profit_eur")
    try:
        mp = float(str(mp).replace(",", ".")) if mp not in (None, "") else None
    except (TypeError, ValueError):
        mp = None
    try:
        return research.begin_import(db, idea_id=idea_id, publish=publish, min_profit_eur=mp,
                                     force=force)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"{type(exc).__name__}: {exc}")
