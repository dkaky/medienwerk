"""Verkleinerte Motive - und das Original bleibt unangetastet.

Anlass (03.09.2026): Die Motivliste zog 106 MB und 153 Megapixel fuer 45
Kacheln, entpackt rund 0,6 GB. Der Browser blieb beim Zeichnen stehen.

Zwei Dinge muessen dauerhaft stimmen und stehen deshalb hier fest: der Druckweg
bekommt weiterhin die volle Aufloesung, und die Durchsichtigkeit ueberlebt das
Verkleinern. Beides faellt sonst erst beim Druck auf.
"""
from __future__ import annotations

import pytest
from PIL import Image

from app.config import get_settings
from app.studio import vorschau


@pytest.fixture
def lager(tmp_path):
    """Ein Bilderlager mit einem grossen und einem kleinen Motiv."""
    wurzel = tmp_path / "bilder"
    (wurzel / "repariert").mkdir(parents=True)

    # Gross wie eine echte Druckdatei, mit einem durchsichtigen Streifen links.
    gross = Image.new("RGBA", (4500, 5400), (200, 30, 40, 255))
    durchsichtig = Image.new("RGBA", (400, 5400), (0, 0, 0, 0))
    gross.paste(durchsichtig, (0, 0))
    gross.save(wurzel / "repariert" / "druck.png")

    Image.new("RGBA", (180, 220), (10, 90, 200, 255)).save(wurzel / "klein.png")
    return wurzel


def test_grosses_motiv_wird_verkleinert(lager):
    ziel = vorschau.hole(lager / "repariert" / "druck.png", breite=400, wurzel=lager)
    assert ziel != lager / "repariert" / "druck.png"
    with Image.open(ziel) as b:
        assert b.width == 400
        assert b.height == 480          # Seitenverhaeltnis bleibt (4500:5400)


def test_durchsichtigkeit_ueberlebt(lager):
    """Ein Druckmotiv OHNE durchsichtigen Grund ist unbrauchbar.

    Als JPEG waere die Vorschau kleiner - und der durchsichtige Hintergrund
    waere schwarz. Genau den muss man an einem Motiv sehen koennen.
    """
    ziel = vorschau.hole(lager / "repariert" / "druck.png", breite=400, wurzel=lager)
    with Image.open(ziel) as b:
        assert b.mode == "RGBA"
        assert b.getpixel((2, 100))[3] == 0, "Durchsichtigkeit ging verloren"


def test_kleines_motiv_bleibt_das_original(lager):
    """Verkleinern waere hier teurer als Ausliefern."""
    quelle = lager / "klein.png"
    assert vorschau.hole(quelle, breite=400, wurzel=lager) == quelle


def test_zweiter_aufruf_rechnet_nicht_neu(lager):
    quelle = lager / "repariert" / "druck.png"
    erst = vorschau.hole(quelle, breite=400, wurzel=lager)
    stempel = erst.stat().st_mtime_ns
    zweit = vorschau.hole(quelle, breite=400, wurzel=lager)
    assert zweit == erst
    assert zweit.stat().st_mtime_ns == stempel, "Vorschau wurde unnoetig neu gerechnet"


def test_neueres_original_erneuert_die_vorschau(lager):
    import time

    quelle = lager / "repariert" / "druck.png"
    alt = vorschau.hole(quelle, breite=400, wurzel=lager)
    vorher = alt.read_bytes()
    time.sleep(0.05)
    Image.new("RGBA", (4500, 5400), (0, 200, 0, 255)).save(quelle)
    neu = vorschau.hole(quelle, breite=400, wurzel=lager)
    assert neu.read_bytes() != vorher, "Veraltete Vorschau wurde weitergereicht"


def test_original_bleibt_unveraendert(lager):
    """Der Druckweg braucht die vollen 4500 Pixel."""
    quelle = lager / "repariert" / "druck.png"
    vorschau.hole(quelle, breite=200, wurzel=lager)
    with Image.open(quelle) as b:
        assert b.size == (4500, 5400)


@pytest.mark.parametrize("wunsch,erwartet", [
    (None, None), (0, None), (-5, None), ("quatsch", None),
    (200, 200), (400, 400), (900, 900), (410, 400), (5000, 900),
])
def test_breiten_werden_auf_erlaubte_werte_gezogen(wunsch, erwartet):
    """Sonst legt ein Aufruf mit tausend Breiten tausend Dateien an."""
    assert vorschau.erlaubte_breite(wunsch) == erwartet


def test_kaputte_datei_liefert_das_original(lager):
    """Eine fehlende Vorschau darf nie heissen, dass gar nichts angezeigt wird."""
    kaputt = lager / "kaputt.png"
    kaputt.write_bytes(b"kein bild")
    assert vorschau.hole(kaputt, breite=400, wurzel=lager) == kaputt


# --- ueber die Route -------------------------------------------------------

def test_route_liefert_verkleinert(client, monkeypatch, lager):
    monkeypatch.setattr(get_settings(), "studio_image_dir", str(lager))
    antwort = client.get("/studio/bilder/repariert/druck.png?breite=400")
    assert antwort.status_code == 200
    assert len(antwort.content) < 400_000, "Vorschau ist nicht kleiner geworden"


def test_route_ohne_breite_liefert_das_original(client, monkeypatch, lager):
    """Ohne Angabe muss die volle Druckdatei rausgehen."""
    monkeypatch.setattr(get_settings(), "studio_image_dir", str(lager))
    voll = client.get("/studio/bilder/repariert/druck.png")
    klein = client.get("/studio/bilder/repariert/druck.png?breite=200")
    assert voll.status_code == klein.status_code == 200
    assert len(voll.content) > len(klein.content)
