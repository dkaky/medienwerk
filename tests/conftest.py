"""Pytest-Fixtures: isolierte Test-DB (SQLite-Datei je Lauf) + TestClient.

WICHTIG: DATABASE_URL wird gesetzt, BEVOR app-Module importiert werden, damit
database.py die Test-Engine baut.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# --- Test-Umgebung VOR app-Import konfigurieren ---
# PID-Unterordner: mehrere Sessions/Entwickler lassen die Suite teils GLEICHZEITIG
# laufen (Vorfall 11.08.: zwei pytest-Prozesse teilten sich test.db -> drop/create
# zerschoss sich gegenseitig, "no such table"/"already exists"-Kaskade).
_TMP = Path(tempfile.gettempdir()) / "ebay_auto_tests" / f"run_{os.getpid()}"
_TMP.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["USE_MOCKS"] = "true"
os.environ["GROWTH_ENGINE_ENABLED"] = "false"
os.environ["INVOICE_DIR"] = str(_TMP / "invoices")
os.environ["LOG_LEVEL"] = "WARNING"
# Tests hermetisch halten: LLM/AliExpress/AutoDS zwingend mocken, unabhaengig davon,
# was in der lokalen .env steht (z.B. MOCK_LLM=false fuer den Echtbetrieb). Env-Vars
# haben Vorrang vor der .env-Datei -> keine echten API-Calls in der Suite.
# HINWEIS: mock_ebay wird bewusst NICHT geforct – test_production_mock_guard baut
# Settings direkt und braucht mock_ebay ungesetzt (folgt use_mocks). eBay bleibt via
# USE_MOCKS=true ohnehin gemockt.
os.environ["MOCK_LLM"] = "true"
os.environ["MOCK_ALIEXPRESS"] = "true"
os.environ["MOCK_AUTODS"] = "true"
# Dashboard-Login (app/auth.py) in Tests IMMER aus – die lokale .env kann ein
# DASHBOARD_PASSWORD setzen, die Suite erwartet aber den anonymen Default.
os.environ["DASHBOARD_PASSWORD"] = ""
# Studio-Riegel in Tests IMMER aus - genau wie GROWTH_ENGINE_ENABLED daneben.
# In der lokalen .env steht STUDIO_ENABLED=true, damit das Studio im Browser
# erreichbar ist. Die Riegel-Tests pruefen aber gerade, dass der Schalter im
# Auslieferungszustand AUS ist. Ohne diese Zeile testet die Suite die lokale
# Einstellung des Entwicklers statt das Verhalten des Programms.
os.environ["STUDIO_ENABLED"] = "false"

import atexit  # noqa: E402
atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))

from app.database import Base, SessionLocal, engine, init_db  # noqa: E402


# Hier stand bis 08.09.2026 ein Autouse-Schutz, der das echte
# data/reprice_report.json vor Testschreibzugriffen bewahrte (ein Test hatte es
# im Juli 2026 mit zwei erfundenen Zeilen ueberschrieben). Der Report gehoerte zu
# listing_match_service, das mit dem Handelsteil ausgezogen ist - es gibt keine
# Lieferantenpreise mehr, die gegen eBay-Preise abzugleichen waeren.
#
# Sollte der Print-on-Demand-Weg je wieder eine solche materialisierte Datei
# bekommen, gehoert derselbe Schutz sofort wieder hierher.


@pytest.fixture(autouse=True)
def _fresh_db():
    """Vor jedem Test: Schema neu aufbauen + Beleg-Ablage leeren (volle Isolation)."""
    Base.metadata.drop_all(bind=engine)
    init_db()
    invoice_dir = Path(os.environ["INVOICE_DIR"])
    if invoice_dir.exists():
        shutil.rmtree(invoice_dir, ignore_errors=True)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    """TestClient ohne Lifespan/Scheduler – DB wird von _fresh_db verwaltet."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
