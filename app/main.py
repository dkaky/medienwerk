"""FastAPI-Einstiegspunkt (Spec Kap. 1.2).

Startet Logging, DB-Init und Scheduler; bindet alle vier Bereichs-Router ein.
Start lokal:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app import __version__
from app.auth import AuthMiddleware
from app.auth import router as auth_router
from app.config import get_settings
from app.database import init_db
from app.logging_config import setup_logging
from app.routers import (
    analytics,
    kontist,
    ebay_notifications,
    growth,
    invoices,
    listings,
    monitoring,
    orders,
    pricing,
    products,
    research,
    system,
)
from app.scheduler import shutdown_scheduler, start_scheduler

settings = get_settings()
setup_logging(settings.log_level)
logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("startup", extra={"env": settings.app_env, "mocks": settings.use_mocks})
    if not settings.dashboard_password:
        logger.warning(
            "DASHBOARD_PASSWORD nicht gesetzt – Dashboard laeuft OHNE Login-Schutz "
            "(nur fuer lokale Entwicklung okay)"
        )
    init_db()
    if not settings.background_jobs_enabled:
        logger.info("background jobs disabled; dashboard and studio remain available")
        yield
        return
    # Zombie-Tasks aus frueheren Prozessen abschliessen (haengen sonst ewig "in Arbeit")
    try:
        from app.database import SessionLocal
        from app.services.common import cleanup_stale_tasks
        _db = SessionLocal()
        try:
            cleanup_stale_tasks(_db)
            # Verwaiste 'ordering'-Claims (Crash/CancelledError vor dem Ergebnis) aus
            # frueheren Prozessen aufraeumen -> Sale needs_manual_review + Alarm.
            from app.services import order_service
            order_service.sweep_stale_claims(_db)
        finally:
            _db.close()
    except Exception as exc:  # noqa: BLE001 – Cleanup darf den Start nie verhindern
        logger.warning("stale task cleanup failed", extra={"error": str(exc)})
    try:
        start_scheduler()
    except Exception as exc:  # noqa: BLE001 – App auch ohne Scheduler lauffaehig
        logger.warning("scheduler not started", extra={"error": str(exc)})
    # Publish-Warteschlange: Worker starten + nach Neustart haengende Go-Lives
    # wieder einreihen (frueher gingen die bei jedem Neustart verloren).
    try:
        from app.services import publish_queue
        await publish_queue.start_worker()
    except Exception as exc:  # noqa: BLE001
        logger.warning("publish queue not started", extra={"error": str(exc)})
    # Produkt-Ideen-Importe: nach Neustart in 'importing' haengende Ideen wieder einreihen
    # (der In-Memory-Task starb beim Neustart -> sonst ewig "wird angelegt"). Non-fatal.
    try:
        from app.services import product_research_service
        product_research_service.recover_stuck_imports()
    except Exception as exc:  # noqa: BLE001 – Recovery darf den Start nie verhindern
        logger.warning("stuck import recovery failed", extra={"error": str(exc)})
    # Shop-Import: lief einer beim Neustart noch, wird er AUTOMATISCH fortgesetzt
    # (Wiederanlauf-Infos in der Status-Datei; bereits Angelegtes wird uebersprungen).
    # Ohne Wiederanlauf-Infos (Alt-Zustand) wird er als unterbrochen gemeldet.
    try:
        from app.services import store_import_service
        store_import_service.startup_check()
    except Exception as exc:  # noqa: BLE001
        logger.warning("store import startup check failed", extra={"error": str(exc)})
    # HINWEIS: Der frühere Auto-Backfill (925->versilbert per Wort-Ersetzung) ist bewusst ENTFERNT –
    # reines Ersetzen erzeugte holprige Titel/Beschreibungen („versilbert-versilbert") und liess
    # KI-Falschbehauptungen (Stempel/Zertifikat/echtes Edelmetall) stehen. Bestehende Listings werden
    # stattdessen KONTROLLIERT per KI neu generiert (Titel SEO + saubere Beschreibung); der
    # Generierungs-Filter hält neue Listings sauber.
    yield
    from app.services import publish_queue
    await publish_queue.stop_worker()
    shutdown_scheduler()
    logger.info("shutdown")


app = FastAPI(
    title="medienwerk",
    description="Standalone-Automatisierung: Produktupload, Auftragsabwicklung, "
                "Belegablage, Listing-Optimierung.",
    version=__version__,
    lifespan=lifespan,
)

# Login-Schutz (aktiv sobald DASHBOARD_PASSWORD in der .env gesetzt ist).
app.add_middleware(AuthMiddleware)
app.include_router(auth_router)

app.include_router(system.router)
app.include_router(products.router)
app.include_router(orders.router)
app.include_router(invoices.router)
app.include_router(listings.router)
app.include_router(pricing.router)
app.include_router(monitoring.router)
app.include_router(analytics.router)
app.include_router(research.router)
app.include_router(ebay_notifications.router)
app.include_router(kontist.router)
app.include_router(growth.router)
# Studio-Trakt: riegelt sich selbst ab (404), solange STUDIO_ENABLED aus ist.
from app.studio.router import router as studio_router
app.include_router(studio_router)
from app.pod_router import router as pod_router
app.include_router(pod_router)


STATIC_DIR = Path(__file__).resolve().parent / "static"


# Das Dashboard ist EINE Datei, die sich mit jedem Deploy aendert. Ohne diesen
# Hinweis serviert der Browser sie aus dem Cache – neue Funktionen tauchen dann
# erst nach Strg/Cmd+Shift+R auf (real passiert: Rechnungs-Generator war live,
# aber unsichtbar). "no-cache" heisst NICHT "nie speichern", sondern "vor dem
# Benutzen kurz rueckfragen" – dank ETag antwortet der Server sonst mit 304.
_HTML_FRISCH = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/", include_in_schema=False)
@app.get("/dashboard", include_in_schema=False)
def dashboard():
    """Web-Dashboard (statische Single-Page-Oberflaeche, nutzt die JSON-API)."""
    return FileResponse(STATIC_DIR / "index.html", headers=_HTML_FRISCH)


# /klassisch ist am 25.08.2026 entfallen. Dahinter lag index_classic.html: eine
# fast vollstaendige Zweitkopie derselben Oberflaeche (5.500 Zeilen, 63 derselben
# Serveradressen), deren einziger Unterschied die Farben waren. Jede Aenderung an
# der Oberflaeche musste damit zweimal gebaut werden - und blieb die zweite aus,
# lief der Nutzer in einen veralteten Stand.
# Hell und Dunkel macht index.html jetzt selbst: :root ist dunkel, body.light
# faerbt um, ein Knopf oben rechts schaltet und merkt die Wahl.


@app.get("/studio", include_in_schema=False)
def studio_seite() -> FileResponse:
    """Der Studio-Bereich.

    Traegt dieselbe Farbwelt wie das Dashboard - kuehles Anthrazit mit Cyan.
    Frueher stand hier, das Studio gebe den Stil vor und die alten Bereiche
    wuerden nachgezogen. Es kam umgekehrt: das Dashboard hat die GbR-Palette
    abgelegt und eine eigene bekommen, das Studio ist gefolgt. Beide teilen
    sich jetzt auch die Hell/Dunkel-Wahl ueber denselben Speicherschluessel.
    """
    return FileResponse(STATIC_DIR / "studio.html", headers=_HTML_FRISCH)


@app.get("/studio/bilder/{name:path}", include_in_schema=False)
def studio_bild(name: str, breite: int | None = None) -> FileResponse:
    """Ein erzeugtes Motiv ausliefern.

    ``{name:path}`` statt ``{name}``, und das war kein Schoenheitsfehler: ein
    einfacher Pfadparameter endet am ersten Schraegstrich. Motive liegen aber
    laengst in Unterordnern - ``repariert/`` fuer die nachgebesserten,
    ``vorlagen/`` fuer die uebernommenen Ideengeber. 57 von 62 Bildern liefen
    deshalb ins Leere, und das Studio sah fast leer aus: sichtbar blieben genau
    die fuenf Dateien, die direkt im Bilderordner lagen - darunter die beiden
    Platzhalter des Attrappen-Anbieters.

    Die Ausbruchsperre unten wird dadurch WICHTIGER, nicht unwichtiger: erst
    jetzt kann ``name`` ueberhaupt Schraegstriche enthalten. Sie prueft den
    aufgeloesten Pfad und faengt damit auch ``../`` ab.
    """
    from fastapi import HTTPException

    ordner = Path(get_settings().studio_image_dir).resolve()
    ziel = (ordner / name).resolve()
    # Kein Ausbrechen aus dem Bildordner ueber Pfadangaben wie ../
    if not str(ziel).startswith(str(ordner)) or not ziel.is_file():
        raise HTTPException(status_code=404, detail="Nicht gefunden")

    # Verkleinert ausliefern, wenn eine Breite gewuenscht ist. Ohne Angabe geht
    # weiterhin das Original raus - der Druckweg braucht die vollen 4500 Pixel.
    # Die Liste im Studio braucht sie nie: 45 Kacheln zogen am 03.09.2026
    # 106 MB und 153 Megapixel, entpackt rund 0,6 GB. Der Browser blieb stehen.
    # Ortlicher Import wie das HTTPException oben: die Bildroute soll nicht am
    # Studio-Paket haengen, das hinter dem Riegel STUDIO_ENABLED sitzt.
    from app.studio import vorschau as studio_vorschau

    gewuenscht = studio_vorschau.erlaubte_breite(breite)
    if gewuenscht:
        ziel = studio_vorschau.hole(ziel, breite=gewuenscht, wurzel=ordner)
    return FileResponse(ziel, media_type="image/png")


@app.get("/favicon.svg", include_in_schema=False)
def favicon_svg():
    """Tab-Logo (gekreuzte Schwerter, militaerisches Emblem)."""
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    """Echtes ICO - fuer Lesezeichen und die Windows-Taskleiste.

    Hier wurde frueher das SVG unter dem Namen favicon.ico ausgeliefert, mit der
    Typangabe image/svg+xml. Der Browser-Reiter kommt damit zurecht, die
    TASKLEISTE nicht: Wer die Seite anheftet, behielt dort das alte Symbol des
    fremden Betriebs, weil das angebotene fuer sie unlesbar war.

    Die Datei traegt mehrere Groessen (16 bis 256); Windows sucht sich die
    passende. Erzeugt von scripts/erzeuge_favicon.py.
    """
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")


@app.get("/logo-banner.jpg", include_in_schema=False)
def logo_banner():
    """Kopfzeilen-Banner (Startrampen-Motiv). Als Datei statt inline, damit die
    index.html schlank bleibt und der Browser das Bild zwischenspeichern kann."""
    return FileResponse(STATIC_DIR / "logo-banner.jpg", media_type="image/jpeg")


@app.get("/api", tags=["System"])
def api_info():
    return {
        "name": "medienwerk",
        "version": __version__,
        "dashboard": "/",
        "docs": "/docs",
        "health": "/health",
        "mocks_enabled": settings.use_mocks,
    }
