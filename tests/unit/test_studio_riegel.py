"""Beweise, dass der Studio-Trakt ausgeschaltet ist und der Handel unveraendert laeuft.

Diese Tests sind das Sicherheitsnetz des gesamten Umbaus. Schlaegt einer davon
aus, ist der laufende Verkaufsbetrieb betroffen - dann nicht weiterbauen,
sondern erst die Ursache klaeren.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.database import Base
from app.models import Listing
from app.studio import exclude_studio, is_studio, studio_enabled, studio_listing_ids

GRUNDLINIE = Path(__file__).resolve().parents[1] / "data" / "handel_baseline.json"


class FakeListing:
    """Ein Angebot, wie es in den Schleifen des Handels vorkommt."""

    def __init__(self, kennung: int = 42):
        self.id = kennung


# --- Der Schalter -------------------------------------------------------------

def test_schalter_ist_standardmaessig_aus():
    """Ein Deployment allein darf nichts freischalten."""
    from app.config import Settings

    assert Settings.model_fields["studio_enabled"].default is False


def test_riegel_ist_wirkungslos_solange_der_schalter_aus_ist():
    assert studio_enabled() is False
    assert is_studio(FakeListing()) is False
    assert studio_listing_ids() == frozenset()


def test_riegel_vertraegt_sonderformen():
    """Der Riegel steht in Schleifen - er darf sie nie zum Stehen bringen."""
    assert is_studio(None) is False
    assert is_studio(object()) is False          # Objekt ohne id


def test_abfrage_bleibt_zeichengenau_gleich():
    """Ausgeschaltet muss dieselbe SQL herauskommen wie ohne den Riegel."""
    vorher = select(Listing).where(Listing.listing_status == "active")
    assert str(exclude_studio(vorher)) == str(vorher)


# --- Der Handel ---------------------------------------------------------------

@pytest.fixture(scope="module")
def grundlinie() -> dict:
    if not GRUNDLINIE.exists():
        pytest.skip("Keine Grundlinie hinterlegt")
    return json.loads(GRUNDLINIE.read_text(encoding="utf-8"))


def test_keine_neuen_nachtjobs(grundlinie):
    """Etappe 1 bringt keinen einzigen neuen Nachtjob mit.

    Gelesen wird aus dem Quelltext, NICHT durch Starten des Zeitplaners: Der
    Start braucht eine Ereignisschleife, und wer sie hier anfasst, beschaedigt
    nachfolgende Tests (genau das ist beim Bauen passiert).
    """
    import re

    quelle = (
        Path(__file__).resolve().parents[2] / "app" / "scheduler.py"
    ).read_text(encoding="utf-8")
    kennungen = sorted(set(re.findall(r'^\s+id="([a-z0-9_]+)",', quelle, re.MULTILINE)))

    assert kennungen == grundlinie["nachtjob_kennungen_im_quelltext"]


def test_handelstabellen_unveraendert(grundlinie):
    """Am Schema des Handels wurde nichts geaendert - kein ALTER TABLE."""
    for name, erwartete_spalten in grundlinie["tabellen_spalten"].items():
        tabelle = Base.metadata.tables.get(name)
        assert tabelle is not None, f"Tabelle {name} fehlt"
        assert sorted(c.name for c in tabelle.columns) == erwartete_spalten


def test_studio_tabellen_existieren_aber_getrennt():
    """Die Studio-Tabellen sind angelegt und fassen die Handelstabellen nicht an."""
    assert "studio_designs" in Base.metadata.tables
    assert "studio_listing_links" in Base.metadata.tables
    # Die Verknuepfung zeigt auf listings, aendert dort aber nichts.
    verknuepfung = Base.metadata.tables["studio_listing_links"]
    ziele = {fk.column.table.name for fk in verknuepfung.foreign_keys}
    assert "listings" in ziele


def test_studio_endpunkte_sind_gesperrt():
    """Solange der Schalter aus ist, gibt es die Studio-Endpunkte nach aussen nicht."""
    from fastapi import HTTPException

    from app.studio import require_studio_enabled

    with pytest.raises(HTTPException) as fehler:
        require_studio_enabled()
    assert fehler.value.status_code == 404      # nicht 403: Existenz wird nicht verraten


def test_einstellung_ist_lesbar():
    assert get_settings().studio_enabled is False


# --- Die Endpunkte ------------------------------------------------------------

def test_endpunkte_sind_gesperrt_solange_der_schalter_aus_ist(monkeypatch):
    """Alle Studio-Adressen antworten mit 404 - die Existenz wird nicht verraten."""
    monkeypatch.setenv("DASHBOARD_PASSWORD", "")
    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        for pfad in ("/api/v1/studio/status", "/api/v1/studio/designs", "/api/v1/studio/links"):
            assert c.get(pfad).status_code == 404, pfad
        assert c.post("/api/v1/studio/designs", json={"title": "X"}).status_code == 404
        # Der Gesundheitsbericht bleibt erreichbar
        assert c.get("/health").status_code == 200
    get_settings.cache_clear()


def test_riegel_greift_erst_nach_der_zuordnung(db, monkeypatch):
    """Ein Angebot gehoert zum Studio, sobald es zugeordnet ist - vorher nicht.

    Nutzt die vorhandene Test-Vorrichtung des Projekts (db-Fixture). Eigene
    Datenbank-Infrastruktur im Test hat sich als Fehlerquelle erwiesen.
    """
    from sqlalchemy import select

    from app.models import Listing
    from app.studio import guard, service

    monkeypatch.setattr(guard, "studio_enabled", lambda: True)
    guard.invalidate()

    angebot = Listing(title_seo="Probe", description="x", listing_status="active")
    handel = Listing(title_seo="Handel", description="y", listing_status="active")
    db.add_all([angebot, handel])
    db.commit()
    db.refresh(angebot)
    db.refresh(handel)

    assert is_studio(angebot, db) is False          # noch nicht zugeordnet

    design = service.create_design(db, title="Probe-Motiv")
    service.link_listing(db, listing_id=angebot.id, design_id=design.id)

    assert is_studio(angebot, db) is True           # jetzt geschuetzt
    assert is_studio(handel, db) is False           # der Handel bleibt unberuehrt

    # Die Abfrage nimmt das Studio-Angebot heraus, das Handelsangebot bleibt drin
    alle = select(Listing).where(Listing.listing_status == "active")
    uebrig = {x.id for x in db.scalars(exclude_studio(alle, db)).all()}
    assert angebot.id not in uebrig
    assert handel.id in uebrig

    # Loesen gibt das Angebot wieder frei
    service.unlink_listing(db, listing_id=angebot.id)
    assert is_studio(angebot, db) is False
    guard.invalidate()
