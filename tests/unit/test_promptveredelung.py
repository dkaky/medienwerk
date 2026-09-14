"""Die Promptveredelung - vor allem das, was sie NICHT tut.

Der Knopf "Prompt veredeln" schiebt einen fremden Text zwischen Betreiber und
Bilderzeugung. Drei Dinge muessen darum sitzen, und sie sind hier der
Schwerpunkt:

1. **Die Sicherungen laufen auch gegen die AUSGABE.** Ein Sprachmodell, das
   "auf einem T-Shirt" oder eine Marke hineinschreibt, kaeme sonst durch: die
   Pruefung in der Erzeugung sieht nur noch den veredelten Text.
2. **Ohne Sprachmodell gibt es trotzdem ein Ergebnis** - und der Bericht sagt,
   dass es das schwaechere ist. Der Probebetrieb ist der Normalfall beim
   Ausprobieren.
3. **Es wird nichts erzeugt.** Kein Bild, kein Datenbankeintrag, kein Budget.
"""
from __future__ import annotations

import asyncio

import pytest

from app.studio.generation import promptveredelung as pv


class _Modell:
    """Ein Sprachmodell, das eine vorgegebene Antwort liefert."""

    def __init__(self, antwort: str) -> None:
        self.antwort = antwort
        self.aufrufe: list[dict] = []

    async def veredle_prompt(self, *, idee, regelwerk, ziel="textil"):
        self.aufrufe.append({"idee": idee, "ziel": ziel, "regelwerk": regelwerk})
        return self.antwort


class _Stumm:
    """Ein Anbieter, der ausfaellt - der haeufigste Grund fuer den Regelweg."""

    async def veredle_prompt(self, *, idee, regelwerk, ziel="textil"):
        raise RuntimeError("Anbieter nicht erreichbar")


def _lauf(*args, **kwargs):
    return asyncio.run(pv.veredle(*args, **kwargs))


# --- Das Regelwerk als einzige Quelle ---------------------------------------

def test_stilkatalog_kommt_aus_dem_dokument():
    """Der Katalog wird gelesen, nicht im Code gepflegt - sonst zwei Staende."""
    katalog = pv.stile()
    assert "vintage-retro" in katalog
    assert "flat-vector" in katalog
    name, baustein = katalog["vintage-retro"]
    assert "Vintage" in name
    assert "1970" in baustein


def test_regelwerk_traegt_den_technikzusatz():
    """Das Dokument geht als Systemanweisung raus - der Zusatz muss drinstehen."""
    assert "kein Mockup" in pv.regelwerk()
    assert "PROMPT" in pv.regelwerk() and "BERICHT" in pv.regelwerk()


# --- Harte Grenzen gegen die EINGABE ----------------------------------------

def test_marke_bricht_ab_und_erzeugt_keinen_prompt():
    e = _lauf("nike logo im vintage stil", llm=_Modell("PROMPT\nirgendwas"))
    assert e.abbruch is True
    assert e.prompt == ""
    assert any("Abbruch" in z for z in e.bericht)


def test_ware_im_bild_bricht_ab_mit_formulierungshilfe():
    e = _lauf("dackel auf einem t-shirt", llm=_Modell("PROMPT\nirgendwas"))
    assert e.abbruch is True
    # Der Text von motivregeln traegt den Vorschlag - er muss durchgereicht
    # werden, sonst steht da nur "geht nicht".
    assert any("Versuch es so" in z for z in e.bericht)


def test_motiv_fuer_ein_shirt_geht_durch():
    """Die haeufigste Uebersperrung: 'fuer' ist richtig, 'auf' ist der Fehler."""
    e = _lauf("dackel mit sonnenbrille fuer ein t-shirt", llm=_Stumm())
    assert e.abbruch is False
    assert e.prompt


def test_zu_wenig_idee_wird_nicht_erfunden():
    e = _lauf("ab", llm=_Modell("PROMPT\nirgendwas"))
    assert e.abbruch is True
    assert e.prompt == ""


# --- Harte Grenzen gegen die AUSGABE ----------------------------------------

def test_modell_darf_keine_ware_hineinschreiben():
    """Saubere Eingabe, verdorbene Ausgabe - hier faellt sie sonst niemandem auf."""
    modell = _Modell("PROMPT\nDackel auf einem T-Shirt, flach gezeichnet\n\n"
                     "STIL\nflat-vector\n\nBERICHT\n- Ergaenzt: nichts\n")
    e = _lauf("dackel mit sonnenbrille", llm=modell)
    assert e.abbruch is True
    assert e.prompt == ""
    assert any("Ware im Bild" in z for z in e.bericht)


def test_modell_darf_keine_marke_hineinschreiben():
    modell = _Modell("PROMPT\nSportschuh im Stil von Adidas, flach gezeichnet\n\n"
                     "STIL\nflat-vector\n\nBERICHT\n- Ergaenzt: Marke\n")
    e = _lauf("sportschuh minimalistisch", llm=modell)
    assert e.abbruch is True
    assert "Modell" in " ".join(e.bericht)


def test_unbekannter_stil_faellt_auf_die_vorgabe():
    modell = _Modell("PROMPT\nDackel, klare Konturen\n\nSTIL\nbarock-glitzer\n\n"
                     "BERICHT\n- Ergaenzt: nichts\n")
    e = _lauf("dackel", llm=modell)
    assert e.abbruch is False
    assert e.stil == pv.STANDARDSTIL
    assert any("steht nicht im Katalog" in z for z in e.bericht)


# --- Technikzusatz ----------------------------------------------------------

def test_zusatz_wird_angehaengt_wenn_das_modell_ihn_vergisst():
    modell = _Modell("PROMPT\nDackel mit Sonnenbrille, flache Vektorillustration\n\n"
                     "STIL\nflat-vector\n\nBERICHT\n- Ergaenzt: Stil\n")
    e = _lauf("dackel", llm=modell)
    assert "kein Mockup" in e.prompt
    assert any("Technikzusatz" in z for z in e.bericht)


def test_zusatz_wird_nicht_verdoppelt():
    from app.studio.generation import motivregeln

    fertig = motivregeln.schaerfe("Dackel mit Sonnenbrille, flache Vektorillustration")
    modell = _Modell(f"PROMPT\n{fertig}\n\nSTIL\nflat-vector\n\nBERICHT\n- nichts\n")
    e = _lauf("dackel", llm=modell)
    assert e.prompt.count("kein Mockup") == 1


# --- Der Notweg ohne Sprachmodell -------------------------------------------

def test_ohne_modell_gibt_es_trotzdem_einen_prompt():
    e = _lauf("dackel mit sonnenbrille, retro", llm=_Stumm())
    assert e.abbruch is False
    assert e.quelle == "regeln"
    assert "dackel mit sonnenbrille" in e.prompt.lower()
    assert "kein Mockup" in e.prompt


def test_notweg_sagt_dass_er_der_schwaechere_ist():
    """Sonst sieht ein regelbasierter Prompt aus wie ein geschaerfter."""
    e = _lauf("dackel", llm=_Stumm())
    assert any("Ohne Sprachmodell" in z for z in e.bericht)


def test_leere_modellantwort_faellt_auf_den_regelweg():
    e = _lauf("dackel", llm=_Modell(""))
    assert e.quelle == "regeln"
    assert e.prompt


@pytest.mark.parametrize("eingabe,erwartet", [
    ("dackel im retro stil", "vintage-retro"),
    ("katze als strichzeichnung", "line-art"),
    ("suesser baer kawaii", "kawaii"),
    ("totenkopf tattoo", "tattoo-oldschool"),
    ("berg aquarell", "aquarell"),
    ("dackel", pv.STANDARDSTIL),
])
def test_stilwahl_des_notwegs(eingabe, erwartet):
    assert pv.waehle_stil(eingabe) == erwartet


# --- Format und Aufloesung --------------------------------------------------

@pytest.mark.parametrize("ziel,masse", [
    ("textil", (1024, 1024)),
    ("poster_2_3", (1024, 1536)),
    ("tasse", (1536, 1024)),
    ("gibtsnicht", (1024, 1024)),
])
def test_masse_je_ziel(ziel, masse):
    assert pv.masse(ziel) == masse


def test_textil_bekommt_die_aufloesungswarnung():
    """1024 px auf 15 Zoll sind 68 DPI - der Umrechner weist das ab."""
    e = _lauf("dackel mit sonnenbrille", ziel="textil", llm=_Stumm())
    zeilen = " ".join(e.bericht)
    assert "DPI" in zeilen
    assert "150" in zeilen


def test_warnung_rechnet_mit_den_echten_werten_des_umrechners():
    from app.studio.postprocess import umrechner

    textil = umrechner.FORMATE["textil"]
    reicht = int(textil.min_dpi * textil.druckbreite_zoll)
    assert pv.aufloesungshinweis(reicht, "textil") is None
    assert pv.aufloesungshinweis(reicht - 1, "textil") is not None


def test_unbekanntes_ziel_warnt_nicht_statt_zu_werfen():
    assert pv.aufloesungshinweis(1024, "gibtsnicht") is None


def test_format_aus_der_modellantwort_schlaegt_das_ziel():
    modell = _Modell("PROMPT\nDackel, klare Konturen\n\nFORMAT\nlandscape - Tasse\n\n"
                     "STIL\nflat-vector\n\nBERICHT\n- nichts\n")
    e = _lauf("dackel", ziel="textil", llm=modell)
    assert (e.breite, e.hoehe) == (1536, 1024)


# --- Zerlegen ---------------------------------------------------------------

def test_zerlege_findet_alle_bloecke():
    teile = pv.zerlege("PROMPT\nein Prompt\n\nFORMAT\nsquare\n\nSTIL\nkawaii\n\n"
                       "BERICHT\n- Ergaenzt: Farbe\n- Warnung: Text pruefen\n")
    assert teile["PROMPT"] == "ein Prompt"
    assert teile["STIL"] == "kawaii"
    assert "Warnung" in teile["BERICHT"]


def test_zerlege_vertraegt_fehlende_bloecke():
    teile = pv.zerlege("PROMPT\nnur der Prompt")
    assert teile["PROMPT"] == "nur der Prompt"
    assert "STIL" not in teile


# --- Der Endpunkt -----------------------------------------------------------

def test_endpunkt_veredelt_und_erzeugt_nichts(monkeypatch):
    """Der Knopf darf keine Ressource anlegen - kein Bild, kein Entwurf.

    Deshalb 200 statt 201: es entsteht nichts, was man danach abrufen koennte.
    """
    from fastapi.testclient import TestClient

    from app.config import get_settings

    monkeypatch.setenv("STUDIO_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "")
    get_settings.cache_clear()
    try:
        from app.main import app

        with TestClient(app) as c:
            vorher = c.get("/api/v1/studio/designs").json()
            antwort = c.post("/api/v1/studio/prompt/veredeln",
                             json={"idee": "dackel mit sonnenbrille, retro"})
            assert antwort.status_code == 200
            daten = antwort.json()
            assert daten["prompt"]
            assert daten["abbruch"] is False
            assert daten["bericht"]
            # Nichts entstanden.
            assert c.get("/api/v1/studio/designs").json() == vorher
    finally:
        get_settings.cache_clear()


def test_endpunkt_meldet_abbruch_mit_200_und_leerem_prompt(monkeypatch):
    """Ein Abbruch ist kein Serverfehler - der Bericht ist das Ergebnis."""
    from fastapi.testclient import TestClient

    from app.config import get_settings

    monkeypatch.setenv("STUDIO_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "")
    get_settings.cache_clear()
    try:
        from app.main import app

        with TestClient(app) as c:
            daten = c.post("/api/v1/studio/prompt/veredeln",
                           json={"idee": "nike logo auf einem t-shirt"}).json()
            assert daten["abbruch"] is True
            assert daten["prompt"] == ""
            assert daten["bericht"]
    finally:
        get_settings.cache_clear()


# --- Was der Aufruf mitbekommt ----------------------------------------------

def test_regelwerk_geht_ans_modell():
    """Ohne Regelwerk im Aufruf waere das Modell ein beliebiger Textverbesserer."""
    modell = _Modell("PROMPT\nDackel, klare Konturen\n\nSTIL\nflat-vector\n\nBERICHT\n- nichts\n")
    _lauf("dackel", ziel="tasse", llm=modell)
    assert modell.aufrufe[0]["ziel"] == "tasse"
    assert "Stilkatalog" in modell.aufrufe[0]["regelwerk"]
