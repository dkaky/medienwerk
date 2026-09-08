"""Tests für die Go-Live-Warteschlange (app/services/publish_queue.py)."""
from __future__ import annotations

import asyncio

import pytest

from app.models import Listing
from app.services import golive_service, publish_queue


def _make_listing(db, *, ebay_item_id=None, queued=False):
    listing = Listing(title_seo="Queue-Test", description="d", listing_status="draft",
                      price_eur=19.95, ebay_item_id=ebay_item_id, publish_queued=queued)
    db.add(listing)
    db.commit()
    return listing


@pytest.fixture(autouse=True)
def _fresh_queue():
    publish_queue._queue = asyncio.Queue()
    publish_queue._pending.clear()
    yield
    publish_queue._pending.clear()
    publish_queue._queue = None


async def test_enqueue_dedupes_double_clicks(db):
    listing = _make_listing(db)
    r1 = await publish_queue.enqueue(listing.id)
    r2 = await publish_queue.enqueue(listing.id)   # Doppel-Klick
    assert r1 == {"listing_id": listing.id, "status": "queued", "already": False}
    assert r2["already"] is True and r2["status"] == "queued"
    assert publish_queue._queue.qsize() == 1       # nur EIN Auftrag
    db.refresh(listing)
    assert listing.publish_queued is True


async def test_enqueue_already_live_is_noop(db):
    listing = _make_listing(db, ebay_item_id="800000000001")
    r = await publish_queue.enqueue(listing.id)
    assert r["status"] == "active" and r["already"] is True
    assert publish_queue._queue.qsize() == 0


async def test_process_success_clears_flag(db, monkeypatch):
    listing = _make_listing(db, queued=True)

    async def fake_publish(db_, *, listing_id):
        return {"listing_id": listing_id, "ebay_item_id": "800000000002"}

    monkeypatch.setattr(golive_service, "publish_listing_live", fake_publish)
    await publish_queue._process(listing.id)
    db.refresh(listing)
    assert listing.publish_queued is False
    assert listing.publish_error is None


async def test_process_transient_error_retries_then_succeeds(db, monkeypatch):
    listing = _make_listing(db, queued=True)
    calls = {"n": 0}

    async def flaky_publish(db_, *, listing_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("eBay 500: internal error")   # transient
        return {"listing_id": listing_id, "ebay_item_id": "800000000003"}

    monkeypatch.setattr(golive_service, "publish_listing_live", flaky_publish)
    monkeypatch.setattr(publish_queue, "_RETRY_WAITS_S", [0])   # kein echtes Warten
    await publish_queue._process(listing.id)
    assert calls["n"] == 2
    db.refresh(listing)
    assert listing.publish_queued is False and listing.publish_error is None


async def test_process_permanent_error_keeps_draft_and_stores_error(db, monkeypatch):
    listing = _make_listing(db, queued=True)
    calls = {"n": 0}

    async def broken_publish(db_, *, listing_id):
        calls["n"] += 1
        raise RuntimeError('eBay 400: {"errors":[{"errorId":25002}]}')   # Datenfehler

    monkeypatch.setattr(golive_service, "publish_listing_live", broken_publish)
    monkeypatch.setattr(publish_queue, "_RETRY_WAITS_S", [0])
    await publish_queue._process(listing.id)
    assert calls["n"] == 1                       # KEIN Retry bei eBay-400
    db.refresh(listing)
    assert listing.listing_status == "draft"     # Entwurf bleibt stehen
    assert listing.publish_queued is False
    assert "25002" in (listing.publish_error or "")


async def test_start_worker_requeues_after_restart(db):
    hanging = _make_listing(db, queued=True)                       # Neustart-Opfer
    finished = _make_listing(db, queued=True, ebay_item_id="800000000004")  # schon live
    requeued = await publish_queue.start_worker()
    try:
        assert requeued == 1
        assert hanging.id in publish_queue.pending_ids()
        db.refresh(finished)
        assert finished.publish_queued is False   # Flag abgeraeumt statt neu einreihen
    finally:
        await publish_queue.stop_worker()


async def test_persistent_error_fails_fast_without_retry(db, monkeypatch):
    """PersistentError (keine Bilder, Token fehlt, ...) -> sofort publish_error, KEIN Backoff."""
    from app.retry import PersistentError
    listing = _make_listing(db, queued=True)
    calls = {"n": 0}

    async def broken_publish(db_, *, listing_id):
        calls["n"] += 1
        raise PersistentError("Listing hat keine Bilder – eBay verlangt mindestens eines.")

    monkeypatch.setattr(golive_service, "publish_listing_live", broken_publish)
    await publish_queue._process(listing.id)
    assert calls["n"] == 1
    db.refresh(listing)
    assert "keine Bilder" in (listing.publish_error or "")

    # Ausnahme: Kategorie-Vorschlagsdienst ist bewusst transient
    from app.services.publish_queue import _is_transient
    assert _is_transient(PersistentError("... (Vorschlagsdienst nicht erreichbar) ...")) is True
    assert _is_transient(PersistentError("EBAY_REFRESH_TOKEN fehlt")) is False


async def test_enqueue_db_error_does_not_leak_pending(db, monkeypatch):
    """DB-Fehler beim Einreihen darf das Listing nicht dauerhaft als 'queued' blockieren."""
    listing = _make_listing(db)

    class _BoomSession:
        def get(self, *a, **k):
            raise RuntimeError("database is locked")
        def close(self):
            pass

    monkeypatch.setattr(publish_queue, "SessionLocal", lambda: _BoomSession())
    with pytest.raises(RuntimeError):
        await publish_queue.enqueue(listing.id)
    assert listing.id not in publish_queue.pending_ids()

    monkeypatch.undo()   # zweiter Versuch mit intakter DB laeuft normal durch
    r = await publish_queue.enqueue(listing.id)
    assert r == {"listing_id": listing.id, "status": "queued", "already": False}


async def test_retry_job_skips_queue_owned_listings(db, monkeypatch):
    """Doppel-Publish-Schutz: Sicherheitsnetz-Job fasst Queue-eigene Listings nicht an."""
    from app.models import TaskLog
    from app.services.golive_service import retry_failed_publishes

    listing = _make_listing(db, queued=True)   # gehoert der Queue (Backoff-Fenster)
    listing.product_id = 1
    db.add(TaskLog(task_type="golive", reference_id=str(listing.id), status="failed"))
    db.commit()

    called = {"direct": 0, "enqueued": 0}

    async def fake_publish(db_, *, listing_id, draft_only=False):
        called["direct"] += 1

    async def fake_enqueue(listing_id):
        called["enqueued"] += 1
        return {"status": "queued"}

    monkeypatch.setattr(golive_service, "publish_listing_live", fake_publish)
    monkeypatch.setattr(publish_queue, "enqueue", fake_enqueue)
    r = await retry_failed_publishes(db)
    assert called == {"direct": 0, "enqueued": 0}   # uebersprungen
    assert r["candidates"] == 0

    # Nicht mehr queue-eigen -> Job reiht ueber die Queue ein (kein Direkt-Publish)
    listing.publish_queued = False
    db.commit()
    r2 = await retry_failed_publishes(db)
    assert called["direct"] == 0 and called["enqueued"] == 1
    assert r2["candidates"] == 1
