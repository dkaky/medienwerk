"""Zeitgesteuerte Hintergrundlaeufe.

**Am 08.09.2026 auf Medienwerk umgestellt.** Vorher standen hier 18 Jobs, von
denen 12 den Dropshipping-Betrieb bedienten: Lieferantenpreise ueberwachen,
Sendungsnummern von AliExpress nach eBay melden, Trendprodukte suchen, fremde
Shops ernten, Mengenrabatte pflegen, Bestellungen automatisch beim Lieferanten
ausloesen. Diese Dienste gibt es nicht mehr - eigene Motive haben keinen
Lieferanten, dessen Preis sich nachts aendert.

Geblieben ist, was auch fuer eigene Produkte gilt: eBay-Gebuehren und
Anzeigenraten nachziehen, fehlende Kategorien nachtragen, haengengebliebene
Veroeffentlichungen nachholen, die Bank spiegeln.

Alle Jobs sind LESEND gegenueber dem Verkaufskanal oder holen eine bereits vom
Menschen freigegebene Veroeffentlichung nach. Keiner legt von sich aus ein
Angebot an, aendert einen Preis oder bestellt etwas (Eiserne Regel 1).

Der ganze Trakt laeuft nur mit ``BACKGROUND_JOBS_ENABLED=true``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.config import get_settings
from app.database import SessionLocal

logger = logging.getLogger("app.scheduler")
_scheduler: AsyncIOScheduler | None = None

# Karenz fuer verspaetete Laeufe. APScheduler verwirft mit dem Default (1 Sekunde)
# JEDEN Job, der auch nur Sekunden zu spaet dran ist - auf einem Windows-Rechner, der
# schlaeft oder den Prozess auslagert, ist das der Normalfall. Der Scheduler wirkt dann
# gesund (HTTP antwortet, Jobs sind registriert), fuehrt aber dauerhaft nichts mehr aus.
MISFIRE_GRACE_SECONDS = 3600


async def _fee_sync_job() -> None:
    """Echte eBay-Gebuehren (Finances API) alle 2h nachziehen.

    Ohne diesen Lauf zeigt ein frischer Verkauf tagelang die geschaetzte Gebuehr
    statt der abgerechneten - und damit eine Marge, die es so nie gab.
    """
    db = SessionLocal()
    try:
        from app.services import finance_service
        fr = await finance_service.sync_ebay_fees(db, days=14)
        logger.info("scheduler: fee sync (2h)", extra=fr if isinstance(fr, dict) else {})
    except Exception as exc:  # noqa: BLE001 - ein Job darf die Loop nie crashen
        logger.error("scheduler: 2h fee sync failed", extra={"error": str(exc)})
    finally:
        db.close()


async def _ad_rate_sync_job() -> None:
    """Echte Promoted-Listings-Anzeigenraten (Marketing API) taeglich nachziehen.

    Die Gebuehrenrechnung nutzt dann die tatsaechliche Rate je Angebot statt der
    Pauschale.
    """
    db = SessionLocal()
    try:
        from app.services import ad_rate_service
        r = await ad_rate_service.sync_ad_rates(db)
        logger.info("scheduler: ad rate sync (daily)", extra=r if isinstance(r, dict) else {})
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: ad rate sync failed", extra={"error": str(exc)})
    finally:
        db.close()


async def _category_backfill_job() -> None:
    """Fehlende eBay-Kategorien nachtragen -> kategoriegenaue Provision.

    GetMyeBaySelling liefert die Kategorie nicht mit; ohne diesen Lauf rechnet die
    Kalkulation mit dem Standardsatz statt dem echten. Rein lesend bei eBay.
    Laeuft sich von selbst tot, sobald alle Angebote eine Kategorie haben
    (Filter: category_name IS NULL -> keine Kandidaten, keine Aufrufe).
    """
    db = SessionLocal()
    try:
        from app.services import ebay_import_service
        r = await ebay_import_service.backfill_categories(db, limit=400)
        if r.get("scanned"):
            logger.info("scheduler: category backfill", extra={
                "updated": r.get("updated"), "errors": r.get("errors"),
                "remaining": r.get("remaining")})
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: category backfill failed", extra={"error": str(exc)})
    finally:
        db.close()


async def _listing_stats_job() -> None:
    """Verkaufs- und Aufrufzahlen der eigenen Angebote taeglich auffrischen."""
    db = SessionLocal()
    try:
        from app.services import ebay_import_service
        stats = await ebay_import_service.sync_listing_stats(db)
        logger.info("scheduler: listing stats synced", extra=stats)
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: stats sync failed", extra={"error": str(exc)})
    finally:
        db.close()


async def _ebay_loeschmeldungen_job() -> None:
    """eBay-Loeschmeldungen von der Supabase-Funktion abholen und verarbeiten.

    Stuendlich, weil eBay bis zu ~1.500 Meldungen am Tag schickt und die Funktion
    je Abholung hoechstens 500 herausgibt. Nicht eingerichtet -> still ueberspringen.
    """
    from app.config import get_settings as _s
    from app.services import ebay_loeschmeldungen as lm
    if not lm.ist_eingerichtet(_s()):
        return
    db = SessionLocal()
    try:
        z = await lm.hole_und_verarbeite(db)
        if z.get("abgeholt"):
            logger.info("scheduler: ebay loeschmeldungen", extra=z)
    except Exception as exc:  # noqa: BLE001 - ein Job darf die Loop nie crashen
        logger.error("scheduler: ebay loeschmeldungen failed", extra={"error": str(exc)[:200]})
    finally:
        db.close()


async def _trend_radar_job() -> None:
    """Morgens im Netz nach Trends suchen und Motive vorschlagen. Erzeugt kein Bild."""
    from app.config import get_settings as _s
    s = _s()
    if not s.trend_radar_taeglich:
        return
    db = SessionLocal()
    try:
        from app.studio.radar import trends
        bericht = await trends.lauf(db, s=s)
        logger.info("scheduler: trend-radar", extra={"neu": bericht.get("neu"),
                                                     "aufgefrischt": bericht.get("aufgefrischt")})
    except Exception as exc:  # noqa: BLE001 - ein Job darf die Loop nie crashen
        logger.error("scheduler: trend-radar failed", extra={"error": str(exc)[:200]})
    finally:
        db.close()


# Der Job "haengende Veroeffentlichungen nachholen" ist am 08.09.2026 entfallen.
# Er lief ueber golive_service, das Lieferantenware auf eBay stellte und mit dem
# Handelsteil ausgezogen ist. Er kommt zurueck, sobald der Print-on-Demand-Weg
# steht - dann fuer alle Kanaele, nicht nur eBay.


async def _kontist_bank_sync_job() -> None:
    """Kontobuchungen spiegeln und kontieren (Nur-Lese-Buchhaltung).

    Bewegt kein Geld und aendert keine Angebote.
    """
    from app.integrations import kontist
    # Nicht konfiguriert/verbunden -> still ueberspringen (kein naechtlicher
    # failed-Eintrag). Laeuft der Token MITTEN im Job ab, ist der Fehler dagegen
    # gewollt: der Nutzer muss neu verbinden.
    if not (kontist.is_configured() and kontist.is_connected()):
        return
    db = SessionLocal()
    try:
        from app.services import bank_sync_service, kontierung_service
        from app.services.common import task_log
        with task_log(db, task_type="bank_sync", reference_id="kontist") as tl:
            ergebnis = await bank_sync_service.sync_bank_transactions(db)
            ergebnis.update(kontierung_service.kontiere_neue(db))
            # Eigener try-Block: ein eBay-Ausfall darf den bereits gespeicherten
            # Kontospiegel nicht als gescheitert erscheinen lassen.
            try:
                ergebnis.update(await bank_sync_service.sync_ebay_payouts(db))
            except Exception as exc:  # noqa: BLE001
                logger.error("scheduler: ebay payout check failed", extra={"error": str(exc)})
                ergebnis["payouts_error"] = str(exc)[:200]
            try:
                ergebnis.update(await bank_sync_service.archive_bank_csv(db))
            except Exception as exc:  # noqa: BLE001
                logger.error("scheduler: bank archiv failed", extra={"error": str(exc)})
                ergebnis["archiv_error"] = str(exc)[:200]
            tl.result_data = ergebnis
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduler: kontist bank sync failed", extra={"error": str(exc)})
    finally:
        db.close()


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    settings = get_settings()
    # coalesce: mehrere waehrend eines Ruhezustands verpasste Laeufe werden zu EINEM
    # Nachholer zusammengefasst (kein Nachhol-Sturm nach dem Aufwachen).
    # max_instances=1: ein langer Lauf darf sich nicht mit dem naechsten ueberlappen.
    sched = AsyncIOScheduler(
        timezone="UTC",
        job_defaults={
            "misfire_grace_time": MISFIRE_GRACE_SECONDS,
            "coalesce": True,
            "max_instances": 1,
        },
    )
    sched.add_job(_fee_sync_job, CronTrigger(hour="*/2", minute=25),
                  id="fee_sync_2h", replace_existing=True)
    sched.add_job(_ad_rate_sync_job, CronTrigger(hour=2, minute=30),
                  id="ad_rate_sync_daily", replace_existing=True)
    sched.add_job(_listing_stats_job, CronTrigger(hour=3, minute=0),
                  id="listing_stats_daily", replace_existing=True)
    sched.add_job(_kontist_bank_sync_job, CronTrigger(hour=3, minute=15),
                  id="kontist_bank_sync", replace_existing=True)
    sched.add_job(_category_backfill_job, CronTrigger(hour=3, minute=35),
                  id="category_backfill", replace_existing=True)
    sched.add_job(_ebay_loeschmeldungen_job, CronTrigger(minute=10),
                  id="ebay_loeschmeldungen_stuendlich", replace_existing=True)
    sched.add_job(_trend_radar_job, CronTrigger(hour=5, minute=30),
                  id="trend_radar_taeglich", replace_existing=True)
    # Einmalig 3 Min nach dem Start denselben Lauf anstossen, damit ein frischer
    # Start den Rueckstand nicht erst am naechsten Morgen aufholt (gleiche
    # Funktion, gleicher NULL-Filter -> ohne Rueckstand ein reiner
    # Datenbank-Blick, keine eBay-Aufrufe).
    sched.add_job(_category_backfill_job,
                  DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(minutes=3)),
                  id="category_backfill_startup", replace_existing=True)

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
