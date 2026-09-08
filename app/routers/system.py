"""System-Endpoints: Healthcheck (Spec Kap. 6.3) + Build-Info."""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app import __version__
from app.config import get_settings
from app.database import get_db
from app.models import TaskLog
from app.schemas import HealthResponse

router = APIRouter(tags=["System"])

_START_TIME = datetime.now(timezone.utc)


def _build_info() -> dict:
    """Laufenden Git-Stand EINMAL beim Start ermitteln (welcher Commit laeuft gerade?).

    Beim Deploy per ``git pull`` + Neustart spiegelt das exakt den Live-Code. Ohne .git
    (z.B. Export-Deploy) -> 'unknown', bricht nie."""
    repo = Path(__file__).resolve().parents[2]

    def _run(*args: str) -> str:
        try:
            r = subprocess.run(["git", "-C", str(repo), *args],
                               capture_output=True, text=True, timeout=3)
            return (r.stdout or "").strip()
        except Exception:  # noqa: BLE001 – Git evtl. nicht vorhanden
            return ""

    return {
        "commit": _run("rev-parse", "--short", "HEAD") or "unknown",
        "committed_at": _run("show", "-s", "--format=%cI", "HEAD") or None,
        "subject": _run("log", "-1", "--format=%s") or None,
    }


_BUILD = _build_info()


@router.get("/api/v1/version", tags=["System"])
def version() -> dict:
    """Welcher Code laeuft gerade live? Commit + Startzeit des Prozesses (= letzter Deploy).

    ``pricing`` zeigt die EFFEKTIV geladenen Kalkulations-Konstanten (nach evtl. .env-
    Overrides) - damit laesst sich verifizieren, dass eine Modell-Aenderung auf dem VPS
    wirklich greift (z. B. EK-Pauschalzoll statt Prozentaufschlag)."""
    s = get_settings()
    return {
        "version": __version__,
        "commit": _BUILD["commit"],
        "committed_at": _BUILD["committed_at"],
        "subject": _BUILD["subject"],
        "started_at": _START_TIME.isoformat(),
        "pricing": {
            "aliexpress_tax_pct": s.aliexpress_tax_pct,     # EK-Prozentaufschlag (soll 0.0 sein)
            "customs_fee_eur": s.customs_fee_eur,           # EK-Pauschalzoll je Order (soll 3.57)
            "ebay_fee_vat_pct": s.ebay_fee_vat_pct,         # MwSt auf eBay-Gebuehr (0.19)
            "ebay_ad_rate_pct": s.ebay_ad_rate_pct,         # pauschale Anzeigenrate (0.10)
        },
    }


@router.get("/api/v1/settings/zugaenge", tags=["System"])
def zugaenge(db: Session = Depends(get_db)):
    """Welche Zugaenge sind eingerichtet, welche fehlen, welche sind Attrappe.

    Liefert NIE einen Wert - nur, ob er da ist, und die Klartextnamen fehlender
    Felder. Und ausdruecklich keine Aussage darueber, ob ein Zugang FUNKTIONIERT:
    ein abgelaufener Token sieht von hier aus wie ein gueltiger. Diese Trennung
    steht auch im Hinweis der Antwort, damit sie in der Oberflaeche ankommt.
    """
    from app.services.zugaenge_service import uebersicht
    return uebersicht(db)


@router.get("/api/v1/settings/dhl-key", tags=["System"])
def dhl_key_status(db: Session = Depends(get_db)):
    """Status des DHL-Zustell-Tracking-Keys (nie im Klartext) – fuers Dashboard-Feld."""
    from app.services.app_settings import effective_dhl_api_key, masked_key
    key = effective_dhl_api_key(db)
    return {"configured": bool(key), "masked": masked_key(key),
            "from_env": bool((get_settings().dhl_api_key or "").strip())}


@router.get("/api/v1/settings/dhl-key/test", tags=["System"])
async def test_dhl_key(db: Session = Depends(get_db)):
    """Self-Test: prueft mit dem konfigurierten Key, ob die DHL-Tracking-API antwortet/den Key
    akzeptiert (read-only, ein Test-Abruf). Gibt NIE den Key zurueck – nur ok/Grund."""
    from app.integrations.dhl import DhlTrackingClient
    from app.services.app_settings import effective_dhl_api_key
    return await DhlTrackingClient(effective_dhl_api_key(db)).probe()


@router.post("/api/v1/settings/dhl-key", tags=["System"])
def set_dhl_key(body: dict = Body(...), db: Session = Depends(get_db)):
    """DHL-API-Key im Dashboard setzen (in der DB, nicht im Code) -> aktiviert die echte
    Zustell-Erkennung. Auth-geschuetzt (AuthMiddleware). Wert wird nie zurueckgegeben."""
    from app.services.app_settings import masked_key, set_app_setting
    key = str((body or {}).get("key") or "").strip()
    if not (16 <= len(key) <= 128) or any(c.isspace() for c in key):
        raise HTTPException(status_code=400, detail="Ungültiger DHL-API-Key (16–128 Zeichen, keine Leerzeichen).")
    set_app_setting(db, "dhl_api_key", key)
    # den SOEBEN gespeicherten Wert maskiert zurueckgeben (nicht den evtl. abweichenden env-Key);
    # from_env warnt, falls am Server bereits ein Key gesetzt ist, der Vorrang hat.
    return {"configured": True, "masked": masked_key(key),
            "from_env": bool((get_settings().dhl_api_key or "").strip())}


@router.get("/health", response_model=HealthResponse)
def health(db: Session = Depends(get_db)):
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False

    pending = db.scalar(
        select(func.count()).select_from(TaskLog).where(TaskLog.status.in_(["pending", "in_progress"]))
    ) or 0
    uptime = (datetime.now(timezone.utc) - _START_TIME).total_seconds() / 3600.0
    s = get_settings()

    return HealthResponse(
        status="ok" if db_ok else "degraded",
        uptime_hours=round(uptime, 4),
        db_connected=db_ok,
        tasks_pending=int(pending),
        mocks_enabled=s.use_mocks,
        # Die geldrelevanten eBay-Operationen nutzen bewusst IMMER den echten
        # RealEbayClient (unabhaengig von MOCK_EBAY), sofern echte Zugangsdaten
        # gesetzt sind. Darum ist DAS das ehrliche "Live"-Signal - nicht use_mock.
        #
        # NACHTRAG: Fuer das VEROEFFENTLICHEN gilt das nicht mehr. Die beiden
        # eBay-Knoepfe weisen bei MOCK_EBAY=true ab (routers/products.py), weil
        # sonst ein Klick im vermeintlichen Probebetrieb ein echtes Angebot
        # einstellt. Lesen laeuft weiter echt. Ein einzelnes Ja/Nein beschreibt
        # den Zustand also nicht mehr - deshalb das zweite Feld darunter.
        ebay_live=bool(s.ebay_client_id and s.ebay_refresh_token),
        ebay_schreiben_gesperrt=s.use_mock("ebay"),
        mock_llm=s.use_mock("llm"),
        fulfillment_engine=s.fulfillment_engine,
        sandbox=s.ebay_use_sandbox,
        growth_engine_enabled=s.growth_engine_enabled,
        # Damit die Oberflaeche den Studio-Eintrag ausblenden kann, solange der
        # Riegel zu ist. Ohne diese Angabe fuehrte der Eintrag auf eine Seite, die
        # sofort mit "Studio ist ausgeschaltet" antwortet.
        studio_enabled=s.studio_enabled,
    )
