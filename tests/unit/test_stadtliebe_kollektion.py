"""Stadtliebe-Kollektion: besteht die Pruefung, minimalistisch, ohne Vereinsbezug."""
from __future__ import annotations

import re

import pytest

from app.database import SessionLocal
from app.studio.radar import stadtliebe_kollektion as sk
from app.studio.radar import trends

VEREINSBEZUG = ("bayern", "mia san mia", "bvb", "borussia", "schalke", "königsblau", "koenigsblau",
                "werder", "rb leipzig", "rasenball", "galatasaray", "fenerbah", "beşiktaş", "besiktas",
                "kadıköy", "kadikoy", "trabzonspor", "samsunspor", "real madrid", "atletico", "atlético",
                "barça", "blaugrana", "juventus", "juve", "inter", "rossoneri", "nerazzurri", "psg",
                "paris saint", "olympique", "ssc", "azzurri", "sgs", "fc", "sv", "04", "09", "1899",
                "1900", "1905", "1907", "1909", "1967", "stadion", "allianz", "westfalen", "camp nou",
                "bernabéu", "bernabeu", "san siro", "maradona", "vélodrome", "velodrome", "parc des princes")


@pytest.fixture
def db():
    sitzung = SessionLocal()
    yield sitzung
    sitzung.close()


def test_alle_staedte_mit_mehreren_minimalistischen_motiven():
    ideen = sk.eintraege()
    staedte = {i.thema.split(":")[0] for i in ideen}
    assert staedte >= {"München", "Dortmund", "Gelsenkirchen", "Bremen", "Leipzig", "Istanbul", "Trabzon",
                       "Samsun", "Madrid", "Barcelona", "Turin", "Mailand", "Paris", "Marseille", "Neapel"}
    assert len(ideen) >= 30 and len({i.thema for i in ideen}) == len(ideen)
    for i in ideen:
        assert len(i.farben) == 2 and len(i.motiv.split()) <= 35       # minimalistisch
        text = " ".join([i.thema, i.motiv, i.spruch, i.verkaufswinkel, " ".join(i.suchbegriffe)]).lower()
        treffer = [w for w in VEREINSBEZUG if re.search(rf"(?<![\w]){re.escape(w)}(?![\w])", text)]
        assert not treffer, (i.thema, treffer)
        assert trends.qualitaetsgrund(i, "stadtliebe") is None, (i.thema, trends.qualitaetsgrund(i, "stadtliebe"))


def test_laden_besteht_die_echte_pruefung_und_verdoppelt_nichts(db):
    erst = sk.lade(db)
    assert erst["abgelehnt"] == [], erst["abgelehnt"]
    zweit = sk.lade(db)
    assert zweit["neu"] == 0 and zweit["aufgefrischt"] == len(sk.eintraege())


def test_stadtliebe_ist_eigene_kategorie_und_nicht_im_mix():
    assert "stadtliebe" in trends.KATEGORIEN
    assert "STADTLIEBE" in trends.anweisung(6, trends.date(2026, 9, 16), "stadtliebe")
    assert "STADTLIEBE" not in trends.anweisung(6, trends.date(2026, 9, 16), "mix")
