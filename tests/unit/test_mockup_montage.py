"""Lokale Mockup-Montage - mit kuenstlichen Vorlagen, ohne Netz und ohne Kosten."""
from __future__ import annotations

import os

import numpy as np
import pytest
from PIL import Image

from app.studio import mockup_montage as mm

GRUEN = (0, 177, 64)


def _vorlage(pfad, *, transparent=False, groesse=(400, 600)):
    """Grauer Hintergrund (oder transparent), gruener 'Rumpf' mit Helligkeitsverlauf als Falte."""
    w, h = groesse
    if transparent:
        arr = np.zeros((h, w, 4), np.uint8)
        arr[..., :3] = GRUEN            # wie GPT Image: gruene Werte auch im Durchsichtigen
    else:
        arr = np.full((h, w, 4), 255, np.uint8)
        arr[..., :3] = 200
    for y in range(120, 520):
        faktor = 0.75 + 0.25 * np.cos((y - 120) / 40.0)   # Falten
        arr[y, 100:300, :3] = (np.array(GRUEN) * faktor).astype(np.uint8)
        arr[y, 100:300, 3] = 255
    Image.fromarray(arr, "RGBA").save(pfad)
    return pfad


def _motiv(pfad):
    bild = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    bild.paste((220, 20, 20, 255), (40, 40, 160, 160))
    bild.save(pfad)
    return pfad


def test_maske_trennt_stoff_vom_hintergrund(tmp_path):
    v = mm.lade_vorlage(_vorlage(tmp_path / "t.png"), lange_kante=600)
    assert v.maske[300, 200] > 0.95          # Rumpf
    assert v.maske[50, 50] < 0.05            # Hintergrund
    assert 0.25 < float(np.mean(v.maske > 0.5)) < 0.40


def test_transparente_vorlage_nutzt_den_alphakanal(tmp_path):
    v = mm.lade_vorlage(_vorlage(tmp_path / "t.png", transparent=True), lange_kante=600)
    assert v.maske[50, 50] < 0.05            # gruen, aber durchsichtig -> kein Stoff
    assert tuple(v.rgb[50, 50].round()) == mm.HINTERGRUND


def test_einfaerben_behaelt_falten_und_hintergrund(tmp_path):
    v = mm.lade_vorlage(_vorlage(tmp_path / "t.png"), lange_kante=600)
    schwarz = mm.einfaerben(v, "#1B1B1B")
    assert tuple(schwarz[50, 50].round()) == (200, 200, 200)       # Hintergrund unberuehrt
    stoff = schwarz[120:520, 150]
    assert stoff.max() < 80 and stoff[:, 0].max() - stoff[:, 0].min() > 5   # dunkel, mit Falten
    weiss = mm.einfaerben(v, "#FFFFFF")
    assert weiss[300, 200].min() > 150                               # hell


def test_motiv_sitzt_auf_dem_stoff_und_nicht_daneben(tmp_path):
    v = mm.lade_vorlage(_vorlage(tmp_path / "t.png"), lange_kante=600)
    feld = mm.druckfeld(v, "tshirt")
    assert 100 <= feld.mitte_x - feld.breite // 2 and feld.mitte_x + feld.breite // 2 <= 300
    bild = np.asarray(mm.montiere(v, Image.open(_motiv(tmp_path / "m.png")), "#FFFFFF", feld))
    rot = (bild[..., 0] > 150) & (bild[..., 1] < 90)
    ys, xs = np.nonzero(rot)
    assert len(xs) > 100
    assert xs.min() >= 100 and xs.max() < 300 and ys.min() >= 120


def test_tasse_wird_gewoelbt(tmp_path):
    streifen = np.zeros((10, 100, 4), np.float32)
    streifen[:, ::10] = 255
    gewoelbt = mm._zylinder(streifen)
    mitte = np.count_nonzero(gewoelbt[0, 40:60, 0])
    rand = np.count_nonzero(gewoelbt[0, 0:20, 0])
    assert rand >= mitte                  # am Rand gestaucht: dichtere Linien


def test_rendern_je_farbe_mit_zwischenspeicher(tmp_path):
    ordner = tmp_path / "vorlagen"
    ordner.mkdir()
    for ansicht in ("vorne", "mann"):
        _vorlage(ordner / f"tshirt-{ansicht}.png")
    motiv = _motiv(tmp_path / "m.png")
    ziel = tmp_path / "out"

    erst = mm.rendere(motiv, produkt="tshirt", textil=True, farben=[("Schwarz", "#1B1B1B"), ("Weiß", "#FFFFFF")],
                      ziel_ordner=ziel, ordner=ordner)
    assert set(erst) == {"Schwarz", "Weiß"} and all(len(p) == 2 for p in erst.values())
    zeit = os.path.getmtime(erst["Schwarz"][0])

    zweit = mm.rendere(motiv, produkt="tshirt", textil=True, farben=[("Schwarz", "#1B1B1B")],
                       ziel_ordner=ziel, ordner=ordner)
    assert zweit["Schwarz"] == erst["Schwarz"] and os.path.getmtime(zweit["Schwarz"][0]) == zeit
    fehlt = mm.fehlende_vorlagen("tshirt", textil=True, ordner=ordner)
    assert f"tshirt: Vorlage 'frau' fehlt ({ordner / 'tshirt-frau.png'})" in fehlt
    assert any("'mann_hinten'" in f for f in fehlt)


def test_ohne_gruen_klare_meldung(tmp_path):
    Image.new("RGB", (100, 100), (200, 200, 200)).save(tmp_path / "grau.png")
    with pytest.raises(mm.MontageFehler, match="kein gruener Stoff"):
        mm.lade_vorlage(tmp_path / "grau.png")


def test_flacher_hoodie_druckt_unter_der_kapuze(tmp_path):
    v = mm.lade_vorlage(_vorlage(tmp_path / "t.png"), lange_kante=600)
    assert mm.druckfeld(v, "hoodie", "vorne").oben_y > mm.druckfeld(v, "hoodie", "mann").oben_y
    assert mm.druckfeld(v, "hoodie", "mann") == mm.druckfeld(v, "hoodie")


def test_leere_vorlage_in_farbe(tmp_path):
    ordner = tmp_path / "vorlagen"
    ordner.mkdir()
    _vorlage(ordner / "polo-mann.png")
    ziel = mm.rendere_leer("polo", "mann", "Navy", "#1F2A44", ziel_ordner=tmp_path / "out",
                           ordner=ordner, lange_kante=300)
    bild = np.asarray(Image.open(ziel).convert("RGB"), np.float32)
    assert bild[150, 100, 2] > bild[150, 100, 1]           # Stoff ist blau, nicht gruen
    with pytest.raises(mm.MontageFehler, match="'frau' fehlt"):
        mm.rendere_leer("polo", "frau", "Navy", "#1F2A44", ziel_ordner=tmp_path / "out", ordner=ordner)


def test_farbcode_fuer_dateinamen():
    assert mm.farbcode("Weiß") == "weiss" and mm.farbcode("Flaschengrün") == "flaschengrun"


def _rot(pfad):
    bild = np.asarray(Image.open(pfad).convert("RGB"))
    return int(((bild[..., 0] > 150) & (bild[..., 1] < 90)).sum())


def test_rueckseite_eigenes_motiv_und_leere_seite(tmp_path):
    ordner = tmp_path / "vorlagen"
    ordner.mkdir()
    for ansicht in mm.ANSICHTEN_TEXTIL:
        _vorlage(ordner / f"tshirt-{ansicht}.png")
    motiv = _motiv(tmp_path / "m.png")
    art = dict(produkt="tshirt", textil=True, farben=[("Weiß", "#FFFFFF")], ordner=ordner, lange_kante=300)

    beide = mm.rendere(motiv, hinten=motiv, ziel_ordner=tmp_path / "a", **art)["Weiß"]
    assert [p.name.split("-")[1] for p in beide] == list(mm.ANSICHTEN_TEXTIL)
    assert all(_rot(p) > 50 for p in beide)

    nur_hinten = mm.rendere(None, hinten=motiv, ziel_ordner=tmp_path / "b", **art)["Weiß"]
    assert [p.name.split("-")[1] for p in nur_hinten] == [
        "hinten", "mann_hinten", "frau_hinten", "vorne", "mann", "frau"]
    assert _rot(nur_hinten[0]) > 50 and _rot(nur_hinten[3]) == 0 and "-leer" in nur_hinten[3].name

    nur_vorne = mm.rendere(motiv, ziel_ordner=tmp_path / "c", **art)["Weiß"]
    assert nur_vorne[0].name.split("-")[1] == "vorne" and "-leer" in nur_vorne[3].name


def test_tasse_nimmt_das_motiv_der_rueckseite_wenn_vorne_leer(tmp_path):
    ordner = tmp_path / "vorlagen"
    ordner.mkdir()
    for ansicht in mm.ANSICHTEN_TASSE:
        _vorlage(ordner / f"tasse-{ansicht}.png")
    fotos = mm.rendere(None, hinten=_motiv(tmp_path / "m.png"), produkt="tasse", textil=False,
                       farben=[("Weiß", "#FFFFFF")], ziel_ordner=tmp_path / "t", ordner=ordner,
                       lange_kante=300)["Weiß"]
    assert len(fotos) == 2 and all(_rot(p) > 20 for p in fotos)
