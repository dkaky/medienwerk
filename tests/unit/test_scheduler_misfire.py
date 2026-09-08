"""Scheduler-Karenz: verspaetete Laeufe duerfen NICHT still verworfen werden.

Vorfall 28.07.: Der Server lief 3 Tage durch, HTTP antwortete, alle Jobs waren
registriert – trotzdem lief kein einziger Lauf. Ursache: APScheduler verwirft mit
dem Default ``misfire_grace_time=1`` jeden Job, der >1 Sekunde zu spaet dran ist.
Auf einem Windows-Rechner (Schlafmodus, ausgelagerter Prozess) trifft das jeden Lauf.
"""
from __future__ import annotations

import asyncio

import pytest

from app import scheduler as scheduler_module


@pytest.fixture()
def started_scheduler():
    """start_scheduler() braucht einen laufenden Event-Loop (AsyncIOScheduler)."""
    scheduler_module._scheduler = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop.run_until_complete(_start())
    finally:
        scheduler_module.shutdown_scheduler()
        loop.close()
        asyncio.set_event_loop(None)


async def _start():
    return scheduler_module.start_scheduler()


def test_misfire_grace_survives_system_sleep(started_scheduler):
    """Ein Lauf, der Minuten zu spaet kommt, muss trotzdem ausgefuehrt werden."""
    grace = started_scheduler._job_defaults["misfire_grace_time"]

    assert grace is None or grace >= 600, (
        f"misfire_grace_time={grace}s verwirft verspaetete Laeufe still "
        "(Windows-Schlafmodus verzoegert regelmaessig um Minuten)"
    )


def test_missed_runs_coalesce_into_one(started_scheduler):
    """Nach langem Ruhezustand EIN Nachholer statt Nachhol-Sturm."""
    assert started_scheduler._job_defaults["coalesce"] is True


def test_long_run_does_not_overlap_itself(started_scheduler):
    """Monitoring laeuft ueber ~250 Listings – parallele Instanzen waeren doppelte Pushes."""
    assert started_scheduler._job_defaults["max_instances"] == 1


def test_every_job_inherits_the_grace(started_scheduler):
    """Kein Job darf die Karenz per Einzel-Konfiguration wieder auf 1s druecken."""
    too_strict = [
        job.id for job in started_scheduler.get_jobs()
        if job.misfire_grace_time is not None and job.misfire_grace_time < 600
    ]

    assert not too_strict, f"Jobs mit zu kurzer Karenz: {too_strict}"
