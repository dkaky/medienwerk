"""Fussball-Kollektion: alles besteht die Pruefung, nichts ist doppelt, nichts ist geschuetzt."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from app.database import SessionLocal
from app.studio.models import MotivIdee
from app.studio.radar import fussball_kollektion as fk
from app.studio.radar import trends

GESCHUETZT = ("bayern", "mia san mia", "dortmund", "bvb", "schalke", "real madrid", "barça", "barcelona",
              "atletico", "juventus", "milan", "inter", "napoli", "roma", "psg", "paris saint", "marseille",
              "liverpool", "manchester", "arsenal", "chelsea", "tottenham", "galatasaray", "fenerbah",
              "beşiktaş", "besiktas", "trabzon", "bundesliga", "premier league", "la liga", "laliga",
              "serie a", "ligue 1", "süper lig", "super lig", "uefa", "fifa", "dfb", "never walk alone",
              "echte liebe", "adidas", "nike", "puma")


@pytest.fixture
def db():
    sitzung = SessionLocal()
    yield sitzung
    sitzung.close()


def _durchlassen(text):
    return SimpleNamespace(allowed=True, reason=None)


def test_kollektion_ist_gross_und_frei_von_vereinen_und_ligen():
    ideen = fk.eintraege()
    assert len(ideen) >= 40
    assert len({i.thema for i in ideen}) == len(ideen)
    for i in ideen:
        text = " ".join([i.thema, i.motiv, i.spruch, i.verkaufswinkel, " ".join(i.suchbegriffe)]).lower()
        treffer = [w for w in GESCHUETZT if re.search(rf"{re.escape(w)}", text)]
        assert not treffer, (i.thema, treffer)
        assert trends.qualitaetsgrund(i, "fussball") is None, (i.thema, trends.qualitaetsgrund(i, "fussball"))
        assert len(i.motiv.split()) <= 40                      # minimalistisch: kurze Bildidee
    assert sum(1 for i in ideen if i.spruch) < len(ideen)       # nicht jedes Motiv hat einen Spruch


def test_laden_besteht_die_echte_rechte_und_motivpruefung_und_verdoppelt_nichts(db):
    erst = fk.lade(db)                                          # echter Rechte-Filter
    assert erst["abgelehnt"] == [], erst["abgelehnt"]
    assert erst["neu"] + erst["aufgefrischt"] == len(fk.eintraege())
    zweit = fk.lade(db)
    assert zweit["neu"] == 0 and zweit["aufgefrischt"] == len(fk.eintraege())
    idee = db.query(MotivIdee).filter(MotivIdee.quelle_plattform == trends.PLATTFORM).first()
    assert json.loads(idee.beschreibung)["kategorie"] == "fussball"
