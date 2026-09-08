"""Der Weg vom Motiv zur Druckdatei: vergroessern, nachzeichnen, nichts erfinden.

Das Herzstueck ist die Trennung zwischen "sagen" und "tun". Der Druckcheck sagt,
dass ein Motiv zu klein ist UND um welchen Faktor es wachsen muesste. Vergroessert
wird aber erst, wenn jemand es ausdruecklich verlangt - sonst waere die Pruefung
im Umrechner wertlos, an der bisher jedes erzeugte Motiv haengen blieb.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from app.studio import produktweg
from app.studio.postprocess import hochskalierer as hs
from app.studio.postprocess import vektor
from app.studio.postprocess.umrechner import zielformat


class _Motiv:
    """Ein Motiv, wie der Produktweg es erwartet - ohne Datenbank."""

    def __init__(self, dateiname: str, design_id: int = 1) -> None:
        self.id = design_id
        self.title = "Testmotiv"
        self.image_url = f"/studio/bilder/{dateiname}"


def _lege_bild(ordner: Path, name: str, groesse: tuple[int, int]) -> Path:
    """Ein Bild mit echtem Inhalt - ein leeres wuerde beim Trimmen verschwinden."""
    bild = Image.new("RGBA", groesse, (0, 0, 0, 0))
    rand = max(2, groesse[0] // 8)
    for x in range(rand, groesse[0] - rand):
        for y in range(rand, groesse[1] - rand):
            bild.putpixel((x, y), (200, 60, 40, 255))
    pfad = ordner / name
    bild.save(pfad, "PNG")
    return pfad


def _lege_buntes_bild(ordner: Path, name: str, groesse: tuple[int, int]) -> Path:
    """Ein Bild mit vielen Farbstufen.

    Fuer den Vergleich der Nachzeichen-Stufen zwingend: Auf einer einzigen
    Farbflaeche liefern alle Stufen genau einen Pfad, und die Dateigroessen
    unterscheiden sich nur noch um Rundungsrauschen (gemessen: 342 gegen 461
    Bytes, in der FALSCHEN Richtung). Erst ein Verlauf mit Formen darin macht
    sichtbar, was die Stufen tun.
    """
    breite, hoehe = groesse
    bild = Image.new("RGBA", groesse, (0, 0, 0, 255))
    for x in range(breite):
        for y in range(hoehe):
            bild.putpixel((x, y), (
                int(255 * x / breite),
                int(255 * y / hoehe),
                (x * y) % 256,
                255,
            ))
    pfad = ordner / name
    bild.save(pfad, "PNG")
    return pfad


# --------------------------------------------------------------------------
# Vergroessern
# --------------------------------------------------------------------------
def test_vergroessern_bleibt_proportional():
    """Ungleiche Faktoren sind der Fehler, an dem 13 Altmotive kaputtgingen."""
    bild = Image.new("RGBA", (400, 600))
    e = hs.vergroessere(bild, ziel_breite=1200)
    assert e.bild.size == (1200, 1800)
    assert e.faktor == pytest.approx(3.0)
    verhaeltnis_vorher = 400 / 600
    verhaeltnis_nachher = e.bild.width / e.bild.height
    assert verhaeltnis_nachher == pytest.approx(verhaeltnis_vorher)


def test_grosses_motiv_wird_nicht_verkleinert():
    """Verkleinern waere ein Verlust ohne Not - das Platzieren macht es ohnehin."""
    bild = Image.new("RGBA", (3000, 3000))
    e = hs.vergroessere(bild, ziel_breite=1000)
    assert e.faktor == 1.0
    assert e.bild.size == (3000, 3000)
    assert e.verfahren == "unveraendert"


def test_starke_vergroesserung_wird_als_weich_gemeldet():
    """Eiserne Regel 3: Der Nutzer soll sehen, was aufgeblasen wurde."""
    bild = Image.new("RGBA", (500, 500))
    sanft = hs.vergroessere(bild, ziel_breite=1000)      # Faktor 2
    kraeftig = hs.vergroessere(bild, ziel_breite=3000)   # Faktor 6
    assert not sanft.weich
    assert kraeftig.weich
    assert "weich" in kraeftig.hinweis


def test_unsinnige_zielgroesse_wird_abgelehnt():
    bild = Image.new("RGBA", (100, 100))
    with pytest.raises(hs.SkalierFehler):
        hs.vergroessere(bild, ziel_breite=0)
    with pytest.raises(hs.SkalierFehler):
        hs.vergroessere(bild, ziel_breite=hs.MAX_KANTE + 1)


def test_noetige_breite_folgt_der_druckrechnung():
    """150 DPI ueber 15 Zoll sind 2250 Pixel - die Zahl, an der alles haengt."""
    assert hs.noetige_breite(150, 15.0) == 2250
    assert hs.noetige_breite(300, 9.0) == 2700


# --------------------------------------------------------------------------
# Druckcheck: sagen statt tun
# --------------------------------------------------------------------------
def test_zu_kleines_motiv_nennt_den_faktor(tmp_path):
    """Ein blosses 'geht nicht' traf JEDES erzeugte Motiv und half niemandem."""
    _lege_bild(tmp_path, "klein.png", (1024, 1024))
    e = produktweg.pruefe(_Motiv("klein.png"), produkttyp_key="tshirt",
                          bildordner=tmp_path)
    assert not e.moeglich
    assert e.mit_vergroesserung
    assert e.noetige_breite == 2250
    assert e.faktor and e.faktor > 2


def test_grosses_motiv_braucht_keine_vergroesserung(tmp_path):
    _lege_bild(tmp_path, "gross.png", (4500, 5400))
    e = produktweg.pruefe(_Motiv("gross.png"), produkttyp_key="tshirt",
                          bildordner=tmp_path)
    assert e.moeglich
    assert not e.mit_vergroesserung


def test_druckdatei_ohne_erlaubnis_haelt_an(tmp_path):
    """Der Kern: Vergroessern passiert NIE von selbst."""
    _lege_bild(tmp_path, "klein.png", (1024, 1024))
    with pytest.raises(produktweg.MotivFehler) as fehler:
        produktweg.erzeuge_druckdatei(_Motiv("klein.png"), produkttyp_key="tshirt",
                                      bildordner=tmp_path)
    # Der Fehlertext muss den Ausweg nennen, sonst ist er eine Sackgasse.
    assert "hochskalieren" in str(fehler.value)


def test_druckdatei_mit_erlaubnis_erreicht_die_druckgroesse(tmp_path):
    _lege_bild(tmp_path, "klein.png", (1024, 1024))
    datei = produktweg.erzeuge_druckdatei(_Motiv("klein.png"), produkttyp_key="tshirt",
                                          bildordner=tmp_path, hochskalieren=True)
    ziel = zielformat("textil")
    with Image.open(datei) as bild:
        assert bild.size == ziel.groesse
    # Der Faktor steht im Dateinamen - einer Druckdatei soll man ansehen,
    # dass sie aufgeblasen wurde.
    assert "__x" in datei.name


# --------------------------------------------------------------------------
# SVG
# --------------------------------------------------------------------------
def test_svg_entsteht_und_zaehlt_seine_pfade(tmp_path):
    pytest.importorskip("vtracer")
    quelle = _lege_bild(tmp_path, "motiv.png", (256, 256))
    ergebnis = vektor.zeichne_nach(quelle, tmp_path / "motiv.svg")
    assert ergebnis.pfad.is_file()
    assert ergebnis.pfade >= 1
    assert ergebnis.pfad.read_text(encoding="utf-8").lstrip().startswith("<?xml")
    assert not ergebnis.zu_gross


def test_die_stufen_unterscheiden_sich_wirklich(tmp_path):
    """Sonst waere die Wahl eine Attrappe.

    Gemessen am echten Bergmotiv (08.09.2026): fein 11.206 Pfade, plakativ
    1.193, schnitt 5. Hier reicht die Reihenfolge - die genauen Zahlen haengen
    am Bild.
    """
    pytest.importorskip("vtracer")
    quelle = _lege_buntes_bild(tmp_path, "bunt.png", (192, 192))
    fein = vektor.zeichne_nach(quelle, tmp_path / "fein.svg", stufe_key="fein")
    plakativ = vektor.zeichne_nach(quelle, tmp_path / "plakativ.svg", stufe_key="plakativ")
    schnitt = vektor.zeichne_nach(quelle, tmp_path / "schnitt.svg", stufe_key="schnitt")
    assert fein.pfade > plakativ.pfade > schnitt.pfade
    assert schnitt.bytes < fein.bytes


def test_unbekannte_stufe_wird_abgelehnt(tmp_path):
    with pytest.raises(vektor.VektorFehler):
        vektor.zeichne_nach(tmp_path / "egal.png", tmp_path / "egal.svg",
                            stufe_key="gibtsnicht")


def test_fehlende_bilddatei_meldet_klartext(tmp_path):
    with pytest.raises(vektor.VektorFehler) as fehler:
        vektor.zeichne_nach(tmp_path / "fehlt.png", tmp_path / "fehlt.svg")
    assert "nicht gefunden" in str(fehler.value).lower()


# --------------------------------------------------------------------------
# Stilfeld
# --------------------------------------------------------------------------
def test_stil_wird_angehaengt():
    from app.studio.generation import motivregeln

    assert motivregeln.mit_stil("Dackel", "Comic") == "Dackel. Stil: Comic"
    assert motivregeln.mit_stil("Dackel", None) == "Dackel"
    assert motivregeln.mit_stil("Dackel", "   ") == "Dackel"


def test_stil_ist_keine_hintertuer_am_motivart_filter():
    """Ohne diese Pruefung waere das Stilfeld der Weg an der Sperre vorbei."""
    from app.studio.generation import motivregeln

    with pytest.raises(motivregeln.MotivartFehler):
        motivregeln.mit_stil("Dackel", "auf einem T-Shirt, getragen von einem Model")


def test_zu_langer_stil_wird_abgelehnt():
    """Ein langer Stil verdraengt sonst das eigentliche Motiv."""
    from app.studio.generation import motivregeln

    with pytest.raises(motivregeln.MotivartFehler):
        motivregeln.mit_stil("Dackel", "x" * (motivregeln.MAX_STIL + 1))
