"""Eingestellte Angebote bearbeiten: Preis, Farben und Groessen je Angebot."""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.studio import angebote, ebay_weg
from app.studio.models import StudioDesign


@pytest.fixture
def db():
    sitzung = SessionLocal()
    yield sitzung
    sitzung.close()


@pytest.fixture
def design(db):
    d = StudioDesign(title="Angebotstest", status="draft", source="test")
    db.add(d)
    db.commit()
    yield d
    from app.studio.models import StudioAngebotOption

    for z in db.query(StudioAngebotOption).filter_by(design_id=d.id):
        db.delete(z)
    db.delete(d)
    db.commit()


def test_ohne_eintrag_gelten_katalogwerte(db, design):
    s, p = get_settings(), ebay_weg.PRODUKTE["tshirt"]
    assert angebote.aktive_farben(db, design.id, p) == ebay_weg.farben(p)
    assert angebote.aktive_groessen(db, design.id, p, s) == ebay_weg.groessen(p, s)
    assert angebote.preis_fuer(db, design.id, p, s) == ebay_weg.preis(p, s)


def test_preis_farben_und_groessen_werden_gespeichert(db, design):
    s, p = get_settings(), ebay_weg.PRODUKTE["tshirt"]
    angebote.setze(db, s, design.id, "tshirt", preis_eur="19,90".replace(",", "."),
                   farben_aus=["Sand", "Rot"], groessen_aus=["3XL"])
    assert angebote.preis_fuer(db, design.id, p, s) == 19.90
    assert "Sand" not in angebote.aktive_farben(db, design.id, p)
    assert "Rot" not in angebote.aktive_farben(db, design.id, p)
    assert "3XL" not in angebote.aktive_groessen(db, design.id, p, s)
    assert "Schwarz" in angebote.aktive_farben(db, design.id, p)


def test_leerer_preis_faellt_auf_standard_zurueck(db, design):
    s, p = get_settings(), ebay_weg.PRODUKTE["tshirt"]
    angebote.setze(db, s, design.id, "tshirt", preis_eur=25, farben_aus=[], groessen_aus=[])
    angebote.setze(db, s, design.id, "tshirt", preis_eur=None, farben_aus=[], groessen_aus=[])
    assert angebote.preis_fuer(db, design.id, p, s) == ebay_weg.preis(p, s)


def test_nicht_alles_abschaltbar_und_preis_geprueft(db, design):
    s, p = get_settings(), ebay_weg.PRODUKTE["tshirt"]
    with pytest.raises(angebote.AngebotFehler):
        angebote.setze(db, s, design.id, "tshirt", preis_eur=None,
                       farben_aus=ebay_weg.farben(p), groessen_aus=[])
    with pytest.raises(angebote.AngebotFehler):
        angebote.setze(db, s, design.id, "tshirt", preis_eur=None, farben_aus=[],
                       groessen_aus=ebay_weg.groessen(p, s))
    for schlecht in (0.10, 5000, "abc"):
        with pytest.raises(angebote.AngebotFehler):
            angebote.setze(db, s, design.id, "tshirt", preis_eur=schlecht, farben_aus=[], groessen_aus=[])
    with pytest.raises(angebote.AngebotFehler):
        angebote.setze(db, s, design.id, "gibtsnicht", preis_eur=None, farben_aus=[], groessen_aus=[])


def test_unbekannte_farben_werden_ignoriert(db, design):
    s = get_settings()
    o = angebote.setze(db, s, design.id, "tshirt", preis_eur=None, farben_aus=["Pink", "Sand"],
                       groessen_aus=[])
    assert o["farben_aus"] == ["Sand"]
