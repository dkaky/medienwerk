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
         "zielgruppe": "Hobbyangler, die gemeinsame Herbsttouren verschenken",
         "kaufmoment": "Ein Freund kauft es als persoenliches Geschenk vor der gemeinsamen Herbsttour.",
         "verkaufswinkel": "Der ruhige Pausenmoment am Wasser ersetzt die uebliche Pose mit grossem Fang.",
         "motiv": motiv or f"Ein ausdrucksstarker Fuchs sitzt zum Thema {thema} seitlich auf einer "
                           "kleinen Uferkiste und haelt eine gebogene Angel. Ein einzelner runder "
                           "Wasserkringel und drei Schilfhalme rahmen die Figur, waehrend ein Blatt "
                           "auf der Krempe seines Hutes den Herbst andeutet. Die kompakte dreieckige "
                           "Komposition bleibt aus der Entfernung lesbar, besitzt klare Konturen und "
                           "eine geschlossene freigestellte Silhouette ohne Landschaftshintergrund.",
         "stil": "Retro-Linienzeichnung", "farben": ["Orange", "Dunkelblau", "Creme"],
         "spruch": "Ruhe am Haken", "produkt": "T-Shirt, alternativ Hoodie",
         "druckhinweis": "Kompakte Zentralform mit breiten Konturen und ohne feine Verlaeufe.",
         "risiko": "Saisonfenster ist kurz; den Spruch vor Veroeffentlichung nochmals pruefen.",
         "suchbegriffe": [f"{thema} Shirt", "Angler Geschenk", "Angelurlaub Pullover"],
         "quellen": ["https://beispiel.de/trend"]}
    d.update(over)
    return d


class _FakeOpenAI:
    def __init__(self, eintraege, urls=("https://quelle.de/a",)):
        text = "Hier die Ergebnisse:\n```json\n" + json.dumps(eintraege, ensure_ascii=False) + "\n```"
        annot = [SimpleNamespace(type="url_citation", url=u) for u in urls]
        self._antwort = SimpleNamespace(output_text=text, output=[
            SimpleNamespace(type="web_search_call",
                            action=SimpleNamespace(sources=[SimpleNamespace(url=u) for u in urls]),
                            content=[]),
            SimpleNamespace(type="message", action=None,
                            content=[SimpleNamespace(annotations=annot)])])
        self.gesehen = {}
        self.responses = SimpleNamespace(create=self._create)

    async def _create(self, **kw):
        self.gesehen.update(kw)
        return self._antwort


def _filter(text):
    return SimpleNamespace(allowed="Adidas" not in text, reason="Markenname")


def _s(**over):
    return Settings(_env_file=None, openai_api_key="k", trend_modell="gpt-5.5",
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


def test_zerlegen_versteht_strukturiertes_empfehlungsobjekt():
    eintrag = _eintrag("Angelrunde Herbst")
    text = json.dumps({"empfehlungen": [eintrag]}, ensure_ascii=False)

    assert trends.zerlege(text) == [eintrag]


async def test_lauf_legt_gepruefte_vorschlaege_ohne_bild_ab(db):
    b = await _lauf(db, [_eintrag("Angeln Herbst"), _eintrag("Adidas Retro"), _eintrag("Oktoberfest")])

    assert b["neu"] == 2 and b["vorschlaege"] == 3
    assert b["abgelehnt"] == [{"thema": "Adidas Retro", "grund": "Rechtefilter: Markenname"}]
    liste = ideen.liste(db, quelle="trend")
    assert [i.thema for i in liste] == ["Angeln Herbst", "Oktoberfest"]     # nach Rang
    erste = liste[0]
    assert erste.platz == 1 and erste.signal is None                        # kein erfundenes Volumen
    assert erste.status == "neu" and erste.design_id is None
    assert "Eigenstaendige, reduzierte realistische Print-Illustration" in erste.eigener_prompt
    assert 'Schriftzug "Ruhe am Haken"' in erste.eigener_prompt
    d = json.loads(erste.beschreibung)
    assert d["ebay_angebote"] == 4321 and d["quellen"] == ["https://beispiel.de/trend"]
    assert d["kaufmoment"].startswith("Ein Freund")
    assert d["verkaufswinkel"].startswith("Der ruhige Pausenmoment")
    assert d["druckhinweis"].startswith("Kompakte Zentralform")
    assert d["kategorie"] == "mix"
    assert db.query(StudioDesign).count() == 0                               # kein Bild


async def test_websuche_wird_mit_deutschem_standort_aufgerufen(db):
    client = _FakeOpenAI([_eintrag("Angeln")])
    await trends.lauf(db, s=_s(), client=client, zaehle_angebote=None, filter_check=_filter,
                      heute=HEUTE, kostenbremse=False)
    werkzeug = client.gesehen["tools"][0]
    assert werkzeug["type"] == "web_search" and werkzeug["user_location"]["country"] == "DE"
    assert client.gesehen["model"] == "gpt-5.5"
    assert "13.09.2026" in client.gesehen["input"] and "KEINE Marken" in client.gesehen["input"]
    assert client.gesehen["tools"][0]["search_context_size"] == "high"
    assert client.gesehen["text"]["format"]["type"] == "json_schema"
    assert client.gesehen["text"]["format"]["schema"]["properties"]["empfehlungen"]["minItems"] == 12
    assert client.gesehen["include"] == ["web_search_call.action.sources"]
    assert client.gesehen["store"] is False
    assert client.gesehen["reasoning"] == {"effort": "medium"}


async def test_sport_sucht_breit_aber_gibt_keine_clubmerkmale_aus(db):
    client = _FakeOpenAI([_eintrag("Torwartfokus", kategorie="sport")])
    await trends.lauf(db, s=_s(), client=client, zaehle_angebote=None,
                      filter_check=_filter, heute=HEUTE, kostenbremse=False,
                      kategorie="sport")

    auftrag = client.gesehen["input"]
    assert "ALLER relevanten nationalen Ligen" in auftrag
    assert "Basketball" in auftrag and "Eishockey" in auftrag
    assert "NUR intern als Nachfragesignal" in auftrag
    assert "niemals Namen, Logos, Wappen" in auftrag


def test_ideen_sind_kurz_realistisch_und_nicht_ueberladen():
    auftrag = trends.anweisung(6, HEUTE, "gothic")

    assert "Genau ein Hauptmotiv" in auftrag
    assert "hoechstens ein kleines Nebenelement" in auftrag
    assert "Motiv 18-45 Woerter" in auftrag
    assert "Keine Cartoon-" in auftrag


def test_animierter_stil_fliegt_ausserhalb_von_anime_raus():
    eintrag = _eintrag("Rabenwache", kategorie="gothic", stil="niedlicher Comic Cartoon")
    assert trends.qualitaetsgrund(trends.aus_eintrag(eintrag), "gothic") == (
        "Stil wirkt zu animiert statt realistisch"
    )


def test_anime_bleibt_als_eigene_erwachsene_kategorie_erlaubt():
    eintrag = _eintrag("Nachtlaeufer", kategorie="anime",
                       stil="erwachsene realistische Anime-Illustration")
    assert trends.qualitaetsgrund(trends.aus_eintrag(eintrag), "anime") is None


def test_zeitlose_stilidee_braucht_keine_erfundene_aktuelle_quelle():
    eintrag = _eintrag("Samtmotte", kategorie="gothic", zeitraum="Immergrün",
                       quellen=[], stil="realistische Siebdruckillustration")
    assert trends.qualitaetsgrund(trends.aus_eintrag(eintrag), "gothic") is None


def test_aktueller_astronomie_anlass_braucht_einen_beleg():
    eintrag = _eintrag("Saturnkante", kategorie="astronomie", zeitraum="Oktober 2026",
                       quellen=[], stil="realistische Editorial-Illustration")
    assert trends.qualitaetsgrund(trends.aus_eintrag(eintrag), "astronomie") == (
        "kein pruefbarer Quellenbeleg"
    )


async def test_quelle_wird_der_richtigen_empfehlung_zugeordnet(db):
    ohne_links = _eintrag(
        "Angelrunde Herbst", quellen=[],
        warum=("Der Termin steht im aktuellen Kalender. "
               "([Beleg](https://quelle.de/bericht?utm_source=openai))"),
    )
    client = _FakeOpenAI([ohne_links], urls=("https://andere-quelle.de/allgemein",))

    await trends.lauf(db, s=_s(), client=client, zaehle_angebote=None,
                      filter_check=_filter, heute=HEUTE, kostenbremse=False)

    gespeichert = ideen.liste(db, quelle="trend")[0]
    beschreibung = json.loads(gespeichert.beschreibung)
    assert beschreibung["quellen"] == ["https://quelle.de/bericht"]
    assert beschreibung["warum"] == "Der Termin steht im aktuellen Kalender."


async def test_generische_empfehlung_wird_nicht_abgelegt(db):
    beliebig = _eintrag("Herbstlandschaft", zielgruppe="Erwachsene")

    bericht = await _lauf(db, [beliebig])

    assert bericht["neu"] == 0
    assert bericht["abgelehnt"] == [
        {"thema": "Herbstlandschaft", "grund": "Qualitaet: Zielgruppe ist zu allgemein"}
    ]
    assert ideen.liste(db, quelle="trend") == []


def test_knapper_bildaufbau_reicht_wenn_die_gesamte_empfehlung_substanz_hat():
    eintrag = _eintrag(
        "Pilzsammler Pause",
        motiv=("Ein Dachs kniet neben einem Korb mit drei Pfifferlingen. Ein Farnbogen "
               "fasst die kompakte Figur ein; breite Linien und eine ruhige Silhouette "
               "halten das Motiv auch aus Entfernung klar lesbar."),
    )

    assert 20 <= len(eintrag["motiv"].split()) < 45
    assert trends.qualitaetsgrund(trends.aus_eintrag(eintrag)) is None


def test_einzelnes_ebay_listing_ist_kein_nachfragebeleg():
    eintrag = _eintrag(
        "Pilzsammler Pause",
        quellen=["https://www.ebay.de/itm/123456789"],
    )

    assert trends.qualitaetsgrund(trends.aus_eintrag(eintrag)) == (
        "nur einzelne Produktanzeigen statt eines Nachfragebelegs"
    )


def test_alte_empfehlungen_bekommen_ihre_eigene_quelle_zurueck(db):
    idee = MotivIdee(
        quelle_plattform="trend", quelle_shop="websuche", fremd_id="alt",
        fremdtitel="", thema="Alt", status="neu", beschreibung=json.dumps({
            "warum": ("Der konkrete Anlass ist belegt. "
                      "([Quelle](https://beispiel.de/anlass?utm_source=openai))"),
            "quellen": ["https://falsche-sammelquelle.de"],
        }),
    )
    db.add(idee)
    db.commit()

    assert trends.repariere_gespeicherte_quellen(db) == 1
    db.refresh(idee)
    daten = json.loads(idee.beschreibung)
    assert daten["warum"] == "Der konkrete Anlass ist belegt."
    assert daten["quellen"] == ["https://beispiel.de/anlass"]
    assert idee.quelle_url == "https://beispiel.de/anlass"


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
