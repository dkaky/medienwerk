"""Fun-Shirt-Sprueche-Kollektion: besteht die echte Qualitaets- und Rechtepruefung."""
from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.studio.radar import funshirt_kollektion as fk
from app.studio.radar import trends


@pytest.fixture
def db():
    sitzung = SessionLocal()
    yield sitzung
    sitzung.close()


def test_kollektion_hat_spruch_und_kleines_motiv_je_eintrag():
    ideen = fk.eintraege()
    assert len(ideen) >= 25
    assert len({i.thema for i in ideen}) == len(ideen)
    for i in ideen:
        assert i.spruch.strip(), i.thema                      # jeder Eintrag hat einen Spruch
        assert len(i.spruch.split()) <= 8
        assert any(w in i.motiv.lower() for w in ("klein", "winzig"))  # Motiv bleibt dem Spruch untergeordnet
        assert trends.qualitaetsgrund(i, "funshirt") is None, (i.thema, trends.qualitaetsgrund(i, "funshirt"))


def test_laden_besteht_die_echte_pruefung_und_verdoppelt_nichts(db):
    erst = fk.lade(db)
    assert erst["abgelehnt"] == [], erst["abgelehnt"]
    assert erst["neu"] == len(fk.eintraege())
    zweit = fk.lade(db)
    assert zweit["neu"] == 0 and zweit["aufgefrischt"] == len(fk.eintraege())


def test_funshirt_ist_eigene_kategorie():
    assert "funshirt" in trends.KATEGORIEN
    assert "FUN-SHIRT-SPRUECHE" in trends.anweisung(6, trends.date(2026, 9, 17), "funshirt")
