"""Task-Scheduler (Spec Kap. 1.2 / 5.4) – APScheduler.

Jobs:
* Woechentliche Listing-Optimierung (Mo 00:00 UTC)
* Taegliche Performance-Pruefung (03:00 UTC)

Wird beim FastAPI-Startup gestartet und beim Shutdown gestoppt.
"""
from __future__ import annotations

import asyncio
import logging

from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.config import get_settings
from app.database import SessionLocal
from app.services import monitoring_service, optimization_service

logger = logging.getLogger("app.scheduler")
_scheduler: AsyncIOScheduler | None = None

# Karenz fuer verspaetete Laeufe. APScheduler verwirft mit dem Default (1 Sekunde)
# JEDEN Job, der auch nur Sekunden zu spaet dran ist – auf einem Windows-Rechner, der
# schlaeft oder den Prozess auslagert, ist das der Normalfall. Der Scheduler wirkt dann
# gesund (HTTP antwortet, Jobs sind registriert), fuehrt aber dauerhaft nichts mehr aus.
MISFIRE_GRACE_SECONDS = 3600


async def _weekly_optimization_job() -> None:
    # Propose-only ist jetzt PER-PRODUKT on-demand (Nutzerwunsch). Der Wochen-Job
    # generiert KEINE Vorschläge mehr automatisch (Kosten/Kontrolle), sondern frischt
    # nur die Klickzahlen auf, damit die 0-Klick-Liste aktuell bleibt.
    logger.info("scheduler: weekly click refresh start")
    db = SessionLocal()
    try:
        result = await optimization_service.refresh_click_data(db)
        logger.info("scheduler: weekly click refresh done", extra=result)
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: weekly click refresh failed", extra={"error": str(exc)})
    finally:
        db.close()


async def _fee_sync_job() -> None:
    """Echte eBay-Gebuehren (Finances API) alle 2h nachziehen -> neue Verkaeufe zeigen
    schnell das korrekte Netto (statt tagelang die Schaetzung)."""
    db = SessionLocal()
    try:
        from app.services import finance_service
        fr = await finance_service.sync_ebay_fees(db, days=14)
        logger.info("scheduler: fee sync (2h)", extra=fr if isinstance(fr, dict) else {})
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: 2h fee sync failed", extra={"error": str(exc)})
    finally:
        db.close()


async def _ad_rate_sync_job() -> None:
    """Echte Promoted-Listings-Anzeigenraten (Marketing API) 1x taeglich nachziehen ->
    die Gebuehrenkalkulation nutzt die tatsaechliche Rate je Listing statt der Pauschale."""
    db = SessionLocal()
    try:
        from app.services import ad_rate_service
        r = await ad_rate_service.sync_ad_rates(db)
        logger.info("scheduler: ad rate sync (daily)", extra=r if isinstance(r, dict) else {})
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: ad rate sync failed", extra={"error": str(exc)})
    finally:
        db.close()


async def _kontist_bank_sync_job() -> None:
    """Kontist-Buchungen spiegeln + AliExpress-EK-Abgleich (Nur-Lese-Buchhaltung).

    Ergebnis bzw. Fehler landet als TaskLog "bank_sync" im Aktivitaets-Log.
    Bewegt kein Geld und aendert keine eBay-/Bestell-Daten.
    """
    from app.integrations import kontist
    # Nicht konfiguriert/verbunden -> still ueberspringen (kein naechtlicher
    # failed-TaskLog-Laerm). Laeuft der Token MITTEN im Job ab, ist der failed-
    # Eintrag dagegen gewollt: der Nutzer muss neu verbinden.
    if not (kontist.is_configured() and kontist.is_connected()):
        return
    db = SessionLocal()
    try:
        from app.services import bank_sync_service
        from app.services.common import task_log
        with task_log(db, task_type="bank_sync", reference_id="kontist") as tl:
            r1 = await bank_sync_service.sync_bank_transactions(db)
            r2 = bank_sync_service.match_aliexpress_orders(db)
            # Kontierung (Wajjahat-Grundstein): reine DB-Regeln auf neue Buchungen;
            # manuell gesetzte Kontierungen bleiben unantastbar.
            from app.services import kontierung_service
            rk = kontierung_service.kontiere_neue(db)
            # Baustein 2: eBay-Auszahlungen muessen als Gutschrift ankommen.
            # Eigener try-Block: ein eBay-Ausfall darf den bereits committeten
            # Kontist-Spiegel + AliExpress-Abgleich nicht als failed maskieren.
            try:
                r3 = await bank_sync_service.sync_ebay_payouts(db)
            except Exception as exc:  # noqa: BLE001
                logger.error("scheduler: ebay payout check failed",
                             extra={"error": str(exc)})
                r3 = {"payouts_error": str(exc)[:200]}
            # In Kontist hinterlegte Belege uebernehmen ("neue ab jetzt",
            # Nutzerwunsch 18.08.). Fenster 30 Tage: die Historie soll NICHT
            # nachgezogen werden, und laenger zurueck bringt nichts, weil der
            # Lauf taeglich kommt. Eigener try-Block – ein fehlender Download
            # darf den Bank-Spiegel nicht als failed maskieren.
            try:
                from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                from app.services import konto_service
                r5 = await konto_service.belege_von_kontist(
                    db, seit=_dt.now(_tz.utc) - _td(days=30), anwenden=True)
                r5 = {"kontist_belege_uebernommen": r5.get("uebernommen", 0)}
            except Exception as exc:  # noqa: BLE001
                logger.error("scheduler: kontist-belege failed", extra={"error": str(exc)})
                r5 = {"kontist_belege_error": str(exc)[:200]}
            # Baustein 3: GoBD-Archiv (Monats-CSVs der Bank, eingefroren).
            try:
                r4 = await bank_sync_service.archive_bank_csv(db)
            except Exception as exc:  # noqa: BLE001
                logger.error("scheduler: bank archiv failed",
                             extra={"error": str(exc)})
                r4 = {"archiv_error": str(exc)[:200]}
            tl.result_data = {**r1, **r2, **rk, **r3, **r4, **r5}
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: kontist bank sync failed", extra={"error": str(exc)})
    finally:
        db.close()


async def _daily_performance_job() -> None:
    logger.info("scheduler: daily click refresh")
    db = SessionLocal()
    try:
        # Nur Klickzahlen auffrischen (1 Sammel-Report, kein LLM) – haelt die
        # 0-Klick-Liste aktuell, ohne die Analytics-Quota zu sprengen.
        r = await optimization_service.refresh_click_data(db)
        logger.info("scheduler: daily click refresh done", extra=r)
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: daily check failed", extra={"error": str(exc)})
    # Verkaufs-/Aufruf-Statistik taeglich auffrischen – eigener try-Block, damit ein
    # Fehler der Optimierungs-Pruefung den Statistik-Sync nicht mitreisst.
    try:
        from app.services import ebay_import_service
        stats = await ebay_import_service.sync_listing_stats(db)
        logger.info("scheduler: listing stats synced", extra=stats)
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: stats sync failed", extra={"error": str(exc)})
    # Echte eBay-Gebuehren je Order (Finances API) taeglich nachziehen -> korrekte
    # Marge/Steuer ohne manuellen Button (den gibt es im UI nicht mehr).
    try:
        from app.services import finance_service
        fr = await finance_service.sync_ebay_fees(db, days=45)
        logger.info("scheduler: ebay fees synced",
                    extra=fr if isinstance(fr, dict) else {})
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: fee sync failed", extra={"error": str(exc)})
    # eBay-Finanzbericht (laufendes Jahr) cachen -> UI laedt sofort statt ~30 API-Seiten.
    try:
        from datetime import datetime, timezone
        from app.services import finance_service
        await finance_service.ebay_finance_report(
            year=datetime.now(timezone.utc).year, refresh=True)
        logger.info("scheduler: finance report cached")
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: finance report cache failed", extra={"error": str(exc)})
    # Aus neuen AliExpress-BELEGEN die RECHNUNGEN erzeugen. Rein lokal (Beleg liegt
    # schon da) -> unabhaengig davon, WER die Belege eingesammelt hat. Genau diese
    # Haelfte der Kette lief bisher nur von Hand.
    # ``limit`` deckelt die Kosten: je Beleg eine Bilderkennung (~3 s). 200 reichen
    # fuer jeden normalen Tag; bleibt etwas liegen, holt es die naechste Nacht.
    try:
        from app.services import purchase_invoice
        rr = await purchase_invoice.erzeuge_alle_rechnungen(db, limit=200)
        logger.info("scheduler: kaufrechnungen erzeugt",
                    extra={"erzeugt": rr.get("erzeugt"), "offen": rr.get("offen"),
                           "probleme": len(rr.get("probleme") or [])})
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: kaufrechnungen failed", extra={"error": str(exc)})
    finally:
        db.close()


async def _monitoring_job() -> None:
    """Preis-/Bestands-Sync (nativer AutoDS-Ersatz) – alle N Stunden.

    only_auto=False: Bestands-Check laeuft fuer ALLE mit AliExpress verknuepften
    Listings (Preis-Pushes bleiben pro Listing via auto_reprice gesperrt).
    Danach: Verfuegbarkeit der Zusatz-Quellen (Slots 2+3) auffrischen.
    """
    logger.info("scheduler: monitoring/repricing start")
    db = SessionLocal()
    try:
        result = await monitoring_service.run_monitoring(db, only_auto=False)
        logger.info("scheduler: monitoring done", extra=result)
        from app.services import supplier_service
        alt_result = await supplier_service.refresh_secondary_sources(db)
        if alt_result.get("checked"):
            logger.info("scheduler: alt sources refreshed", extra=alt_result)
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: monitoring failed", extra={"error": str(exc)})
    finally:
        db.close()


_order_poll_counter = 0


async def _order_poll_job() -> None:
    """Alle 5 Min: neue eBay-Bestellungen holen (inkl. Storno-Abgleich); bei
    AUTO_FULFILL=true automatisch bei AliExpress bestellen (Default: aus).

    Jeder 12. Lauf (~stuendlich) weitet das Fenster auf 35 Tage, damit auch
    SPAETE Stornos/Erstattungen aelterer Bestellungen erkannt werden – das
    2-Tage-Fenster sieht nur frische Orders.
    """
    global _order_poll_counter
    from app.services import order_service
    _order_poll_counter += 1
    days = 35 if _order_poll_counter % 12 == 0 else 2
    db = SessionLocal()
    try:
        # Verwaiste 'ordering'-Claims (Crash/Neustart vor dem Ergebnis) aufraeumen,
        # bevor neue Bestellungen gezogen werden.
        try:
            order_service.sweep_stale_claims(db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("scheduler: claim sweep failed", extra={"error": str(exc)})
        r = await order_service.sync_ebay_orders(db, days=days,
                                                 max_orders=500 if days > 2 else 200)
        if r.get("sales_created") or r.get("sales_cancelled") or r.get("sales_refunded"):
            logger.info("scheduler: order poll", extra=r)
        af = await order_service.auto_fulfill_due(db)
        if af.get("ordered"):
            logger.info("scheduler: auto fulfill", extra=af)
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: order poll failed", extra={"error": str(exc)})
    finally:
        db.close()


async def _multibuy_cleanup_job() -> None:
    """Taeglich: aktive Mengenrabatte von eBay entfernen, deren Staffeln keinen echten Mehrgewinn
    mehr bringen (self-gated per settings.multibuy_auto_remove). NUR Entfernen, nie Hinzufuegen."""
    from app.services import optimization_service
    db = SessionLocal()
    try:
        r = await optimization_service.auto_remove_unprofitable_multibuy(db)
        if r.get("removed") or r.get("errors"):
            logger.info("scheduler: multibuy auto-remove", extra=r)
    except Exception as exc:  # noqa: BLE001 – ein Job darf die Loop nie crashen
        logger.error("scheduler: multibuy auto-remove failed", extra={"error": str(exc)})
    finally:
        db.close()


async def _ebay_price_sync_job() -> None:
    """Taeglich: echte Live-eBay-Preise je Variante abgleichen (Quelle der Wahrheit fuer die
    Cockpit-Marge + Drift-Warnung). Read-only von eBay, kein Push."""
    from app.services import ebay_import_service
    from app.services.listing_match_service import rebuild_reprice_report
    db = SessionLocal()
    try:
        r = await ebay_import_service.sync_ebay_live_prices(db)
        rebuild_reprice_report(db)   # Cockpit-Report mit den frischen Live-Preisen neu rechnen
        if r.get("drift") or r.get("errors"):
            logger.info("scheduler: ebay price sync", extra=r)
    except Exception as exc:  # noqa: BLE001 – ein Job darf die Loop nie crashen
        logger.error("scheduler: ebay price sync failed", extra={"error": str(exc)})
    finally:
        db.close()


async def _publish_retry_job() -> None:
    """Selbstheilung: fehlgeschlagene Produkt-Uploads automatisch nachholen."""
    from app.services import golive_service
    db = SessionLocal()
    try:
        result = await golive_service.retry_failed_publishes(db)
        if result.get("candidates"):
            logger.info("scheduler: publish retry done", extra=result)
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: publish retry failed", extra={"error": str(exc)})
    finally:
        db.close()


async def _tracking_sync_job() -> None:
    """Auto-Tracking-Sync: AliExpress-Sendungsnummern automatisch an eBay melden."""
    from app.services import order_service
    db = SessionLocal()
    try:
        result = await order_service.sync_tracking_all(db)
        if result.get("checked"):
            logger.info("scheduler: tracking sync done", extra=result)
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: tracking sync failed", extra={"error": str(exc)})
    # ECHTE Zustellung erkennen. ZUERST DHL (strukturierter Zustellstatus, wo AliExpress die
    # DHL-Zustellung nicht meldet), DANN AliExpress-Status – jeweils eigener try-Block.
    try:
        dhl_res = await order_service.promote_delivered_from_dhl(db)
        if dhl_res.get("promoted"):
            logger.info("scheduler: delivered-from-dhl", extra=dhl_res)
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: delivered-from-dhl failed", extra={"error": str(exc)})
    try:
        deliv = await order_service.promote_delivered_from_tracking(db)
        if deliv.get("promoted"):
            logger.info("scheduler: delivered-from-tracking", extra=deliv)
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: delivered-from-tracking failed", extra={"error": str(exc)})
    # Rest per Zeit-Heuristik nach der Frist hochstufen (Zusteller meldet nicht immer echte
    # Zustellung) – eigener try-Block, damit ein Fehler den Tracking-Sync nicht mitreisst.
    try:
        promo = order_service.promote_stale_tracking(db)
        if promo.get("promoted"):
            logger.info("scheduler: tracking->delivered promoted", extra=promo)
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: delivered-promotion failed", extra={"error": str(exc)})
    finally:
        db.close()


async def _trend_research_job() -> None:
    """KI-Trend-Recherche (Web) -> neue Produkt-Ideen. Taeglich (Nutzerwunsch), begrenzt auf
    trend_research_target (10) neue Produkte. Kostet Token + Web-Suchen (~0,10-0,15 $/Lauf)."""
    from app.config import get_settings
    from app.services import product_research_service as research
    # Denselben Lauf-Lock wie die manuellen Endpoints holen -> kein paralleler,
    # doppelt BEZAHLTER Lauf, wenn gerade eine manuelle Suche/Trend-Suche laeuft.
    if not research.try_acquire_run():
        logger.info("scheduler: trend research übersprungen (Suche läuft bereits)")
        return
    db = SessionLocal()
    try:
        # Nutzerwunsch 08/2026: der taegliche Sucher liefert NUR noch lokale Produkte
        # (EU-/DE-Lager-Tempo) -> Lieferzeit-Deckel aus den Settings.
        _s = get_settings()
        # local_only: seit dem Ships-From-Umbau zaehlt das ECHTE EU-Lager (SKU-Achse
        # 200007763), nicht mehr die versprochene Lieferzeit.
        # Nutzerwunsch 14.08.: taeglicher Input hat Vorrang — lokal zuerst, der
        # Rest des Tagesziels wird mit normalen China-Produkten aufgefuellt.
        r = await research.discover_trends(db, target=_s.trend_research_target,
                                           max_delivery=_s.trend_research_max_delivery,
                                           local_only=True, fallback_china=True)
        logger.info("scheduler: trend research done",
                    extra={"trends": len(r.get("trends", [])), "kept": r.get("kept", 0)})
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: trend research failed", extra={"error": str(exc)})
    finally:
        db.close()
        research.release_run()


async def _store_discovery_job() -> None:
    """Taegliche Store-Entdeckung: 3 neue Stores (aus den Ideen-Store-IDs) per Browser
    ernten -> Bestseller als 🏬-Produkt-Ideen (Nutzerauftrag 15.08., prune-fest)."""
    from app.services import product_research_service as research
    # Gleicher Lauf-Lock wie Suche/Trends: kein paralleler ProductIdea-Schreiber.
    if not research.try_acquire_run():
        logger.info("scheduler: store discovery übersprungen (Suche läuft bereits)")
        return
    db = SessionLocal()
    try:
        r = await research.daily_store_discovery(db)
        logger.info("scheduler: store discovery done",
                    extra={"stores": r.get("neu_verarbeitet", 0),
                           "ideen": r.get("ideen", 0),
                           "kandidaten": r.get("kandidaten", 0)})
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: store discovery failed", extra={"error": str(exc)})
    finally:
        db.close()
        research.release_run()


async def _idea_prune_job() -> None:
    """Alte, unbearbeitete Produkt-Ideen (offen + verworfen, >5 Tage) taeglich loeschen.

    Ersetzt den frueheren "Aufraeumen"-Knopf im Dashboard. Gemerkte und bereits
    importierte Ideen bleiben IMMER erhalten (siehe research.prune_old_ideas).
    """
    from app.services import product_research_service as research
    db = SessionLocal()
    try:
        r = research.prune_old_ideas(db, days=5)
        logger.info("scheduler: idea prune done", extra={"deleted": r.get("deleted", 0)})
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: idea prune failed", extra={"error": str(exc)})
    finally:
        db.close()


async def _category_backfill_job() -> None:
    """Fehlende eBay-Kategorien nachtragen -> kategoriegenaue Provision statt Default.

    GetMyeBaySelling liefert die Kategorie nicht mit; ohne diesen Lauf rechnet die
    Vorwaerts-Kalkulation (Upload-Preis, Cockpit-Marge) z. B. Schmuck mit 12 % statt
    16 %. Rein lesend bei eBay. Laeuft sich von selbst tot, sobald alle Listings eine
    Kategorie haben (Filter: category_name IS NULL -> keine Kandidaten, keine Calls).
    """
    from app.services import ebay_import_service
    db = SessionLocal()
    try:
        r = await ebay_import_service.backfill_categories(db, limit=400)
        if r.get("scanned"):
            logger.info("scheduler: category backfill", extra={
                "updated": r.get("updated"), "errors": r.get("errors"),
                "remaining": r.get("remaining"),
                "affected_listings": r.get("affected_listings")})
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: category backfill failed", extra={"error": str(exc)})
    finally:
        db.close()


async def _growth_scorecard_job() -> None:
    """Lokale, statusbereinigte Geschaeftskennzahlen + taeglicher Traffic-Stand.

    Keine externe API: der Job verwendet ausschliesslich bereits synchronisierte
    ORM-Daten und veraendert weder Listings noch Preise oder Bestellungen.
    """
    if not get_settings().growth_engine_enabled:
        logger.info("scheduler: growth scorecard skipped; feature disabled")
        return
    from app.services import growth_engine_service as growth
    db = SessionLocal()
    try:
        scorecard = growth.refresh_scorecard(db)
        traffic = growth.capture_listing_metrics(db)
        logger.info("scheduler: growth scorecard refreshed", extra={
            "scorecard_id": scorecard.id,
            "confidence": scorecard.confidence,
            "listing_snapshots": traffic.get("created", 0),
        })
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: growth scorecard failed", extra={"error": str(exc)})
    finally:
        db.close()


async def _growth_opportunity_detection_job() -> None:
    """Deterministische Chancenregeln; keine KI und keine Live-Ausfuehrung."""
    if not get_settings().growth_engine_enabled:
        logger.info("scheduler: growth opportunity detection skipped; feature disabled")
        return
    from app.services import growth_engine_service as growth
    db = SessionLocal()
    try:
        result = growth.detect_opportunities(db)
        logger.info("scheduler: growth opportunities detected", extra={
            "updated": result.get("opportunities_updated", 0),
            "auto_rejected": result.get("auto_rejected", 0),
        })
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: growth opportunity detection failed", extra={"error": str(exc)})
    finally:
        db.close()


async def _growth_opportunity_reevaluation_job() -> None:
    """Erzwingt Evidenz- und Margengates fuer bestehende Opportunities."""
    if not get_settings().growth_engine_enabled:
        logger.info("scheduler: growth opportunity reevaluation skipped; feature disabled")
        return
    from app.services import growth_engine_service as growth
    db = SessionLocal()
    try:
        result = growth.reevaluate_opportunities(db)
        logger.info("scheduler: growth opportunities reevaluated", extra=result)
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: growth opportunity reevaluation failed", extra={"error": str(exc)})
    finally:
        db.close()


async def _growth_experiment_evaluation_job() -> None:
    """Wertet registrierte Experimente aus; fuehrt keine Intervention aus."""
    if not get_settings().growth_engine_enabled:
        logger.info("scheduler: growth experiment evaluation skipped; feature disabled")
        return
    from app.services import growth_engine_service as growth
    db = SessionLocal()
    try:
        result = growth.evaluate_experiments(db)
        logger.info("scheduler: growth experiments evaluated", extra=result)
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: growth experiment evaluation failed", extra={"error": str(exc)})
    finally:
        db.close()


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    settings = get_settings()
    # coalesce: mehrere waehrend eines Ruhezustands verpasste Laeufe werden zu EINEM
    # Nachholer zusammengefasst (kein Nachhol-Sturm nach dem Aufwachen).
    # max_instances=1: ein langer Lauf (Monitoring ~250 Listings) darf sich nicht
    # mit dem naechsten ueberlappen.
    sched = AsyncIOScheduler(
        timezone="UTC",
        job_defaults={
            "misfire_grace_time": MISFIRE_GRACE_SECONDS,
            "coalesce": True,
            "max_instances": 1,
        },
    )
    sched.add_job(
        _weekly_optimization_job,
        CronTrigger(day_of_week="mon",
                    hour=settings.opt_weekly_cron_hour,
                    minute=settings.opt_weekly_cron_minute),
        id="weekly_optimization",
        replace_existing=True,
    )
    sched.add_job(
        _daily_performance_job,
        CronTrigger(hour=3, minute=0),
        id="daily_performance",
        replace_existing=True,
    )
    # Echte Gebuehren alle 2h nachziehen (korrektes Netto auf neuen Verkaeufen).
    sched.add_job(
        _fee_sync_job,
        CronTrigger(hour="*/2", minute=25),
        id="fee_sync_2h",
        replace_existing=True,
    )
    # Echte Anzeigenraten (Promoted Listings) 1x taeglich nachziehen -> korrekte
    # Gebuehr je Listing statt Pauschale.
    sched.add_job(
        _ad_rate_sync_job,
        CronTrigger(hour=2, minute=30),
        id="ad_rate_sync_daily",
        replace_existing=True,
    )
    # Kontist-Kontobuchungen spiegeln + AliExpress-EK-Abgleich (nur lesend; laeuft
    # ins Leere, solange kontist_client_id fehlt bzw. nicht verbunden ist).
    sched.add_job(
        _kontist_bank_sync_job,
        CronTrigger(hour=3, minute=15),
        id="kontist_bank_sync",
        replace_existing=True,
    )
    # IMMER registriert: der Job macht auch den BESTANDS-Sync (Ausverkauf -> Menge 0)
    # fuer alle verknuepften Listings. Preis-Pushes bleiben pro Listing durch
    # listing.auto_reprice gesteuert – der globale Schalter darf den Schutz vor
    # Verkaeufen ohne Ware nicht mit abschalten.
    sched.add_job(
        _monitoring_job,
        CronTrigger(hour=f"*/{max(1, settings.monitor_cron_hours)}", minute=15),
        id="price_stock_monitoring",
        replace_existing=True,
    )
    # Stuendlich: neue Sendungsnummern von AliExpress holen und an eBay melden
    sched.add_job(
        _tracking_sync_job,
        CronTrigger(minute=45),
        id="tracking_sync",
        replace_existing=True,
    )
    # KI-Trend-Recherche (nur wenn aktiviert; kostet Token + Web-Suchen). Taeglich (Nutzer)
    # oder woechentlich – jeweils nachts um 4 Uhr.
    if settings.trend_research_enabled:
        trig = (CronTrigger(hour=4, minute=0) if settings.trend_research_daily
                else CronTrigger(day_of_week=settings.trend_research_weekday, hour=4, minute=0))
        sched.add_job(_trend_research_job, trig, id="trend_research", replace_existing=True)
    # Taeglich 05:10 (NACH dem Trend-Lauf, der frische Store-IDs liefert): 3 neue
    # Stores per Browser ernten -> 🏬-Produkt-Ideen (Nutzerauftrag 15.08.).
    if settings.store_discovery_daily:
        sched.add_job(_store_discovery_job, CronTrigger(hour=5, minute=10),
                      id="store_discovery", replace_existing=True)
    # Taeglich 3:35: fehlende eBay-Kategorien nachtragen (400/Lauf). Sobald alle Listings
    # eine Kategorie haben, findet der Lauf keine Kandidaten mehr und macht KEINE Calls.
    sched.add_job(
        _category_backfill_job,
        CronTrigger(hour=3, minute=35),
        id="category_backfill",
        replace_existing=True,
    )
    # Einmalig 3 Min nach dem Start denselben Lauf anstossen, damit ein frischer Deploy
    # den Rueckstand nicht erst am naechsten Morgen aufholt (gleiche Funktion, gleicher
    # NULL-Filter -> ohne Rueckstand ein reiner Datenbank-Blick, keine eBay-Calls).
    sched.add_job(
        _category_backfill_job,
        DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(minutes=3)),
        id="category_backfill_startup",
        replace_existing=True,
    )
    # Taeglich 3:20: alte unbearbeitete Produkt-Ideen aufraeumen (frueher ein Knopf im UI).
    sched.add_job(
        _idea_prune_job,
        CronTrigger(hour=3, minute=20),
        id="idea_prune",
        replace_existing=True,
    )
    # Taeglich 2:50: echte Live-eBay-Preise je Variante abgleichen (Cockpit-Marge/Drift-Warnung).
    sched.add_job(
        _ebay_price_sync_job,
        CronTrigger(hour=2, minute=50),
        id="ebay_price_sync",
        replace_existing=True,
    )
    # Taeglich 3:40: unrentabel gewordene Mengenrabatte automatisch von eBay entfernen
    # (self-gated per settings.multibuy_auto_remove; nur Entfernen, nie Hinzufuegen).
    sched.add_job(
        _multibuy_cleanup_job,
        CronTrigger(hour=3, minute=40),
        id="multibuy_cleanup",
        replace_existing=True,
    )
    # Alle 20 Min: haengengebliebene Produkt-Uploads automatisch nachholen
    sched.add_job(
        _publish_retry_job,
        CronTrigger(minute="5,25,55"),
        id="publish_retry",
        replace_existing=True,
    )
    # Alle 5 Min: neue eBay-Bestellungen einsammeln (+ Auto-Fulfillment, falls aktiviert)
    sched.add_job(
        _order_poll_job,
        CronTrigger(minute="*/5"),
        id="order_poll",
        replace_existing=True,
    )
    if settings.growth_engine_enabled:
        # Growth Engine V1 arbeitet ausschliesslich auf lokal bereits synchronisierten
        # Geschaeftsdaten. Die versetzten Zeiten vermeiden eine gemeinsame lange
        # Schreibtransaktion; kein Job ruft Kauf-/Listing-/Preisfunktionen auf.
        sched.add_job(
            _growth_scorecard_job,
            CronTrigger(hour=6, minute=0),
            id="growth_scorecard_refresh",
            replace_existing=True,
        )
        sched.add_job(
            _growth_opportunity_detection_job,
            CronTrigger(hour=6, minute=10),
            id="growth_opportunity_detection",
            replace_existing=True,
        )
        sched.add_job(
            _growth_opportunity_reevaluation_job,
            CronTrigger(hour=6, minute=20),
            id="growth_opportunity_reevaluation",
            replace_existing=True,
        )
        sched.add_job(
            _growth_experiment_evaluation_job,
            CronTrigger(hour=6, minute=30),
            id="growth_experiment_evaluation",
            replace_existing=True,
        )
    sched.start()
    _scheduler = sched
    logger.info("scheduler started", extra={"jobs": [j.id for j in sched.get_jobs()]})
    return sched


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("scheduler stopped")
