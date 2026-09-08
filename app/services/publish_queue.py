"""Go-Live-Warteschlange: Publish laeuft im HINTERGRUND statt im HTTP-Request.

Warum: Ein Publish dauert 1-10 Minuten (viele eBay-Calls). Synchron im Request
fuehrte das zu Doppel-Klicks (Duplikat-Versuche), Browser-Timeouts und 50+
"Prozess beendet"-Abbruechen bei Server-Neustarts (40 % Fehlquote).

Ablauf jetzt:
1. "Live"-Klick -> Entwurf ist bereits gesichert -> enqueue() -> sofortige Antwort.
2. Ein Worker (eine Publish-Operation gleichzeitig, auf dem Haupt-Event-Loop)
   arbeitet die Warteschlange ab.
3. Transiente Fehler (eBay 5xx, Netz, Kategorie-Dienst) werden automatisch mit
   kurzem Backoff wiederholt (30s/2min); harte Datenfehler (eBay 400) landen als
   `publish_error` am Listing - Entwurf bleibt erhalten ("der Rest steht").
4. `publish_queued` ist DB-persistent: Beim App-Start werden haengende Auftraege
   neu eingereiht -> Neustarts verlieren nie wieder einen Go-Live.
5. Sicherheitsnetz bleibt: der stuendliche retry_failed_publishes-Job.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Listing
from app.retry import PersistentError
from app.services.common import error_text

logger = logging.getLogger("app.services.publish_queue")

# Kurzer In-Queue-Backoff fuer transiente Fehler; danach uebernimmt der
# langsame Selbstheilungs-Job (publish_retry, 3x/Stunde, max 5 Versuche).
_RETRY_WAITS_S = [30, 120]

_queue: asyncio.Queue[int] | None = None
_pending: set[int] = set()          # eingereiht ODER gerade in Arbeit
_worker_task: asyncio.Task | None = None


def _is_transient(exc: BaseException) -> bool:
    """Wiederholbar? eBay-5xx/Netz/Timeout/Kategorie-Dienst ja; Daten-/Config-Fehler nein."""
    text = error_text(exc)
    if "eBay 4" in text:            # 400/409/... = Daten-/Policy-Fehler, wiederholen bringt nichts
        return False
    if isinstance(exc, PersistentError):
        # PersistentError = endgueltig (keine Bilder, Token fehlt, kein Offer, ...).
        # Einzige gewollte Ausnahme: der eBay-Kategorie-Vorschlagsdienst flattert.
        return "Vorschlagsdienst" in text
    return True                     # 5xx, Timeouts, DNS, RateLimit, Unbekanntes


def pending_ids() -> set[int]:
    return set(_pending)


async def enqueue_all_drafts(db, *, dry_run: bool = True, limit: int = 500,
                             freigeben: bool = False) -> dict:
    """ALLE Entwuerfe live stellen - einer nach dem anderen ueber dieselbe Warteschlange.

    Bewusst KEIN Sammel-Call an eBay: die Warteschlange arbeitet ein Listing nach dem
    anderen ab, ist neustartfest und wiederholt voruebergehende Fehler von selbst.
    Bleibt eines haengen, laufen die uebrigen weiter; der harte Fehler steht danach als
    publish_error am Listing.

    ``dry_run=True`` (Vorgabe) reiht NICHTS ein, sondern meldet nur, was passieren
    wuerde. Erst ``dry_run=False`` stellt live.

    ``freigeben=True`` setzt der Router nur beim BESTAETIGTEN Echtlauf. Dann
    bekommt jedes eingereihte Listing eine einmalige Schreibfreigabe, die es im
    Probebetrieb (``MOCK_EBAY=true``) durch die Sperre laesst - der Klick eines
    Menschen soll wirken. Vorgabe ist ``False``, damit ein Aufruf aus einem Job
    diese Wirkung nicht versehentlich erbt.

    Geld-Hinweis: Live-Stellen kostet eBay-Einstellgebuehren und laesst sich nicht per
    Klick zurueckdrehen (das Beenden von Listings ist bewusst gesperrt, Eiserne Regel 2).
    Darum die zweistufige Fuehrung.
    """
    from sqlalchemy import select as _select
    entwuerfe = db.scalars(
        _select(Listing)
        .where(Listing.listing_status == "draft", Listing.ebay_item_id.is_(None))
        .order_by(Listing.id)
        .limit(max(1, int(limit)))
    ).all()
    bereit, ohne_preis, schon_in_arbeit = [], [], []
    for l in entwuerfe:
        if l.id in _pending or l.publish_queued:
            schon_in_arbeit.append(l.id)
        elif not l.price_eur:
            ohne_preis.append(l.id)          # ohne Preis wuerde eBay ohnehin ablehnen
        else:
            bereit.append(l.id)
    ergebnis = {
        "gefunden": len(entwuerfe), "bereit": len(bereit),
        "ohne_preis": len(ohne_preis), "schon_in_arbeit": len(schon_in_arbeit),
        "ohne_preis_ids": ohne_preis[:50], "dry_run": bool(dry_run), "eingereiht": 0,
    }
    if dry_run:
        return ergebnis
    n = 0
    from app.services import freigabe as _freigabe
    for lid in bereit:
        try:
            if freigeben:
                _freigabe.erteile(lid)
            await enqueue(lid)
            n += 1
        except Exception as exc:  # noqa: BLE001 - ein Fehler stoppt den Rest nicht
            logger.warning("bulk publish enqueue failed",
                           extra={"listing_id": lid, "error": str(exc)[:120]})
    ergebnis["eingereiht"] = n
    logger.info("bulk publish", extra={"eingereiht": n, "bereit": len(bereit)})
    return ergebnis


async def enqueue(listing_id: int) -> dict:
    """Listing zum Live-Stellen einreihen (idempotent - Doppel-Klicks prallen ab)."""
    global _queue
    if _queue is None:
        raise RuntimeError("Publish-Queue nicht gestartet (start_worker fehlt)")
    if listing_id in _pending:
        return {"listing_id": listing_id, "status": "queued", "already": True}
    _pending.add(listing_id)
    try:
        db = SessionLocal()
        try:
            listing = db.get(Listing, listing_id)
            if listing is None:
                raise ValueError("Listing nicht gefunden")
            if listing.ebay_item_id:
                _pending.discard(listing_id)
                return {"listing_id": listing_id, "status": "active",
                        "ebay_item_id": listing.ebay_item_id, "already": True}
            listing.publish_queued = True
            listing.publish_error = None
            db.commit()
        finally:
            db.close()
    except BaseException:
        # DB-Fehler o.ae.: _pending abraeumen, sonst blockt "already queued" fuer
        # immer alle weiteren Versuche, obwohl nie etwas eingereiht wurde.
        _pending.discard(listing_id)
        raise
    await _queue.put(listing_id)
    logger.info("publish queued", extra={"listing_id": listing_id, "queue": _queue.qsize()})
    return {"listing_id": listing_id, "status": "queued", "already": False}


async def _publish_once(listing_id: int) -> dict:
    """Einen Auftrag tatsaechlich veroeffentlichen.

    Hier stand bis 08.09.2026 ``golive_service.publish_listing_live``. Der Dienst
    ist mit dem Handelsteil ausgezogen: Er stellte LIEFERANTENWARE auf eBay -
    mit Varianten aus dem Lieferantenkatalog, dessen Versandprofilen und dessen
    Einkaufspreisen. Ein eigenes Motiv hat nichts davon.

    Die Warteschlange selbst bleibt, denn an ihr haengt nichts Handelsspezifisches:
    ein Auftrag je Klick, Wiederholversuche, Freigabepruefung, Wiedereinreihen nach
    einem Neustart. Genau diese Mechanik braucht der neue Weg auch - und zwar fuer
    mehrere Kanaele gleichzeitig.

    Bis der Print-on-Demand-Weg steht, scheitert ein Auftrag hier mit einer klaren
    Meldung. Das ist Absicht: Eine stille Erfolgsmeldung waere schlimmer - der
    Nutzer haelte ein Angebot fuer veroeffentlicht, das nie entstanden ist.
    """
    raise PersistentError(
        "Veroeffentlichen ist gerade nicht moeglich: Der eBay-Weg wird fuer eigene "
        "Motive neu gebaut. Der alte Weg stellte Lieferantenware ein und passt nicht."
    )


def _finish(listing_id: int, *, error: str | None) -> None:
    """Queue-Status am Listing abraeumen (Erfolg ODER endgueltiger Fehler)."""
    db = SessionLocal()
    try:
        listing = db.get(Listing, listing_id)
        if listing is not None:
            listing.publish_queued = False
            listing.publish_error = (error or "")[:500] or None
            db.commit()
    finally:
        db.close()


async def _process(listing_id: int) -> None:
    """Einen Auftrag abarbeiten - inklusive der eigenen Wiederholversuche.

    Der ``freigabe``-Block umschliesst bewusst die GANZE Schleife und nicht den
    Einzelversuch: ein Klick ist ein Auftrag. Waere die Freigabe je Versuch
    faellig, wuerde im Probebetrieb schon der erste Netzwackler den Klick still
    verpuffen lassen - der Nutzer saehe "eingereiht" und danach nie ein Angebot.

    Liegt keine Freigabe vor (der Auftrag kam vom Selbstheilungs-Job, nicht von
    einem Klick), bleibt das Tor zu und die Schreibsperre greift wie bisher.
    """
    from app.services import freigabe

    with freigabe.beim_veroeffentlichen(listing_id):
        await _process_versuche(listing_id)


async def _process_versuche(listing_id: int) -> None:
    for attempt, wait in enumerate([0] + _RETRY_WAITS_S):
        if wait:
            await asyncio.sleep(wait)
        try:
            result = await _publish_once(listing_id)
            _finish(listing_id, error=None)
            logger.info("publish ok", extra={"listing_id": listing_id,
                                             "item": result.get("ebay_item_id"),
                                             "attempt": attempt})
            return
        except Exception as exc:  # noqa: BLE001 – Fehler klassifizieren, nie crashen
            err = error_text(exc)
            if _is_transient(exc) and attempt < len(_RETRY_WAITS_S):
                logger.warning("publish transient, retry folgt",
                               extra={"listing_id": listing_id, "attempt": attempt,
                                      "wait_s": _RETRY_WAITS_S[attempt], "error": err[:200]})
                continue
            _finish(listing_id, error=err)
            logger.error("publish failed (Entwurf bleibt erhalten)",
                         extra={"listing_id": listing_id, "error": err[:300]})
            return


async def _worker() -> None:
    assert _queue is not None
    while True:
        listing_id = await _queue.get()
        try:
            await _process(listing_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 – Worker darf niemals sterben
            logger.error("publish worker error", extra={"listing_id": listing_id,
                                                        "error": error_text(exc)})
        finally:
            _pending.discard(listing_id)
            _queue.task_done()


async def start_worker() -> int:
    """Worker starten + nach Neustart haengende Auftraege wieder einreihen.

    Rueckgabe: Anzahl neu eingereihter Alt-Auftraege (Listings mit
    publish_queued=True, die der letzte Prozess nicht mehr geschafft hat).
    """
    global _queue, _worker_task
    if _queue is None:
        _queue = asyncio.Queue()
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker(), name="publish-queue-worker")

    requeued = 0
    db = SessionLocal()
    try:
        # Studio-Angebote gehoeren NICHT in diese Warteschlange: sie haben kein
        # AliExpress-Produkt und wuerden ueber den Dropshipping-Weg veroeffentlicht.
        # Diese Abfrage laeuft bei JEDEM Serverstart.
        from app.studio import exclude_studio
        stale_stmt = exclude_studio(
            select(Listing).where(Listing.publish_queued.is_(True),
                                  Listing.ebay_item_id.is_(None)),
            db,
        )
        stale = db.scalars(stale_stmt).all()
        # Bereits live gewordene mit haengendem Flag direkt abraeumen
        done = db.scalars(select(Listing).where(Listing.publish_queued.is_(True),
                                                Listing.ebay_item_id.isnot(None))).all()
        for l in done:
            l.publish_queued = False
        if done:
            db.commit()
        ids = [l.id for l in stale]
    finally:
        db.close()
    for lid in ids:
        if lid not in _pending:
            _pending.add(lid)
            await _queue.put(lid)
            requeued += 1
    if requeued:
        logger.warning("publish queue: %s Auftraege nach Neustart wieder eingereiht", requeued)
    return requeued


async def stop_worker() -> None:
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _worker_task = None
