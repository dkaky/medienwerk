"""Trend-Radar: Websuche -> gepruefte, eigene Motivvorschlaege - netzwerkfrei.

Kern: Vorschlaege sind Text. Marken fliegen vorher raus, eine verworfene Idee
kommt nicht als "neu" zurueck, kein Suchvolumen wird erfunden, und erst
"Erzeugen" macht ein Bild.
"""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.database import SessionLocal
from app.studio.models import MotivIdee, StudioDesign
from app.studio.radar import ideen, nutzen, trends

HEUTE = date(2026, 9, 13)


def _eintrag(thema, motiv=None, **over):
    d = {"thema": thema, "warum": f"{thema} steht an (Beleg).", "zeitraum": "Sept–Okt",
         "zielgruppe": "Erwachsene",
         "motiv": motiv or f"Ein detailreich gezeichneter Fuchs mit Angel am See zum Thema {thema}, "
                           "Schilf im Vordergrund, Sonnenuntergang, klare Konturen.",
         "stil": "Retro-Linienzeichnung", "farben": ["Orange", "Dunkelblau", "Creme"],
         "spruch": "Petri Heil", "suchbegriffe": [f"{thema} Shirt", "Angler Geschenk"],
         "quellen": ["https://beispiel.de/trend"]}
    d.update(over)
    return d


class _FakeOpenAI:
    def __init__(self, eintraege, urls=("https://quelle.de/a",)):
        text = "Hier die Ergebnisse:\n```json\n" + json.dumps(eintraege, ensure_ascii=False) + "\n```"
        annot = [SimpleNamespace(type="url_citation", url=u) for u in urls]
        self._antwort = SimpleNamespace(output_text=text, output=[
            SimpleNamespace(type="message", content=[SimpleNamespace(annotations=annot)])])
        self.gesehen = {}
        self.responses = SimpleNamespace(create=self._create)

    async def _create(self, **kw):
        self.gesehen.update(kw)
        return self._antwort


def _filter(text):
    return SimpleNamespace(allowed="Adidas" not in text, reason="Markenname")


def _s(**over):
    return Settings(_env_file=None, openai_api_key="k", trend_modell="gpt-4.1-mini",
                    trend_anzahl=12, **over)


@pytest.fixture
def db():
    sitzung = SessionLocal()
    yield sitzung
    sitzung.close()


async def _lauf(db, eintraege, **over):
    async def zaehle(begriff):
        return 4321
    return await trends.lauf(db, s=_s(), client=_FakeOpenAI(eintraege), zaehle_angebote=zaehle,
                             filter_check=_filter, heute=HEUTE, kostenbremse=False, **over)


def test_zerlegen_mit_codezaun_und_vorrede():
    text = 'Gern!\n```json\n[{"thema": "Angeln", "motiv": "Fuchs"}, {"thema": ""}, 3]\n```'
    assert trends.zerlege(text) == [{"thema": "Angeln", "motiv": "Fuchs"}]
    assert trends.zerlege("keine Liste") == []


async def test_lauf_legt_gepruefte_vorschlaege_ohne_bild_ab(db):
    b = await _lauf(db, [_eintrag("Angeln Herbst"), _eintrag("Adidas Retro"), _eintrag("Oktoberfest")])

    assert b["neu"] == 2 and b["vorschlaege"] == 3
    assert b["abgelehnt"] == [{"thema": "Adidas Retro", "grund": "Rechtefilter: Markenname"}]
    liste = ideen.liste(db, quelle="trend")
    assert [i.thema for i in liste] == ["Angeln Herbst", "Oktoberfest"]     # nach Rang
    erste = liste[0]
    assert erste.platz == 1 and erste.signal is None                        # kein erfundenes Volumen
    assert erste.status == "neu" and erste.design_id is None
    assert "Eigenstaendige, detailreiche Illustration" in erste.eigener_prompt
    assert 'Schriftzug "Petri Heil"' in erste.eigener_prompt
    d = json.loads(erste.beschreibung)
    assert d["ebay_angebote"] == 4321 and d["quellen"] == ["https://beispiel.de/trend"]
    assert db.query(StudioDesign).count() == 0                               # kein Bild


async def test_websuche_wird_mit_deutschem_standort_aufgerufen(db):
    client = _FakeOpenAI([_eintrag("Angeln")])
    await trends.lauf(db, s=_s(), client=client, zaehle_angebote=None, filter_check=_filter,
                      heute=HEUTE, kostenbremse=False)
    werkzeug = client.gesehen["tools"][0]
    assert werkzeug["type"] == "web_search" and werkzeug["user_location"]["country"] == "DE"
    assert client.gesehen["model"] == "gpt-4.1-mini"
    assert "13.09.2026" in client.gesehen["input"] and "KEINE Marken" in client.gesehen["input"]


async def test_verworfene_idee_kommt_nicht_als_neu_zurueck(db):
    await _lauf(db, [_eintrag("Angeln Herbst")])
    idee = ideen.liste(db, quelle="trend")[0]
    ideen.setze_status(db, idee.id, "verworfen")

    b = await _lauf(db, [_eintrag("Angeln Herbst")])

    assert b["neu"] == 0 and b["aufgefrischt"] == 1
    assert ideen.liste(db, quelle="trend") == []
    assert db.get(MotivIdee, idee.id).status == "verworfen"


async def test_shop_und_trend_bleiben_getrennt(db):
    db.add(MotivIdee(quelle_plattform="ebay", quelle_shop="x", fremdtitel="Fremder Titel", status="neu"))
    db.commit()
    await _lauf(db, [_eintrag("Angeln")])
    assert [i.quelle_plattform for i in ideen.liste(db, quelle="shop")] == ["ebay"]
    assert [i.quelle_plattform for i in ideen.liste(db, quelle="trend")] == ["trend"]


async def test_leere_antwort_ist_ein_klarer_fehler(db):
    kaputt = _FakeOpenAI([])
    kaputt._antwort.output_text = "Leider nichts gefunden."
    with pytest.raises(trends.TrendFehler):
        await trends.lauf(db, s=_s(), client=kaputt, filter_check=_filter, heute=HEUTE,
                          kostenbremse=False)


async def test_ohne_schluessel_keine_suche(db):
    with pytest.raises(trends.TrendFehler, match="OPENAI_API_KEY"):
        await trends.recherchiere(api_key="", modell="gpt-4.1-mini", anzahl=5, heute=HEUTE)


def test_motivart_sperre_greift_auch_bei_trends():
    t = trends.aus_eintrag(_eintrag("Angeln", motiv="Logo auf einem T-Shirt mit Angel"))
    assert trends.schutzgrund(t, _filter).startswith("Motivart")


async def test_erst_erzeugen_macht_ein_bild(db, tmp_path):
    await _lauf(db, [_eintrag("Angeln Herbst")])
    idee = ideen.liste(db, quelle="trend")[0]

    e = nutzen.erzeuge_aus_idee(db, idee, anbieter="mock", bildordner=tmp_path)

    assert e["anbieter"] == "mock" and e["kosten_usd"] == 0
    design = db.query(StudioDesign).one()
    assert design.title == "Angeln Herbst" and (tmp_path / design.image_url.split("/")[-1]).is_file()
    db.refresh(idee)
    assert idee.status == "uebernommen" and idee.design_id == design.id
