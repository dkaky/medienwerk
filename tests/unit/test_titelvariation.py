"""Nicht alle Titel duerfen mit demselben Wort anfangen (Shop-Import 27.08.2026).

Der System-Prompt verlangt seit jeher, das Einstiegs-Keyword zwischen aehnlichen
Artikeln zu variieren. Befolgen konnte das Modell die Regel nie: jedes Produkt
entsteht in einem EIGENEN Aufruf, der die anderen nicht kennt — jeder Aufruf haelt
sich fuer den ersten.

Beim Import aus einem T-Shirt-Shop fingen deshalb alle zehn Titel mit "T-Shirt" an.
Zehn eigene Angebote, die in der eBay-Suche gegeneinander laufen statt gegen fremde.

Die Loesung ist nicht eine schaerfere Regel, sondern Information: die schon
vergebenen Anfaenge gehen in den Prompt.
"""
from __future__ import annotations

import asyncio

import pytest

from app.integrations import llm as _llm
from app.services.product_service import vergebene_titelanfaenge


def _db_mit(titel):
    """Minimale DB-Attrappe: execute() liefert die Titel als Ein-Spalten-Zeilen."""
    class _DB:
        def execute(self, q):
            return [(t,) for t in titel]
    return _DB()


async def _fertig(t):
    return t


def _client(monkeypatch, gesehen, titel="Motiv-Shirt Katze Baumwolle"):
    class _Gen:
        title_seo = titel
        description_clean = "\U0001F455 Hook"
        item_specifics = []
        warnings = []
        strategic_note = None

    async def _fake_openai(user):
        gesehen["user"] = user
        return _Gen()

    c = _llm.RealLLMClient.__new__(_llm.RealLLMClient)
    c.settings = type("S", (), {"llm_model": "x", "llm_provider": "openai"})()
    monkeypatch.setattr(c, "_openai_generate", _fake_openai)
    monkeypatch.setattr(c, "_expand_title_if_short", _fertig)
    monkeypatch.setattr(c, "_fix_title_word_order", _fertig)
    return c


def test_haeufigster_anfang_steht_vorn():
    db = _db_mit(["T-Shirt Rot", "T-Shirt Blau", "Hoodie Schwarz", "T-Shirt Gruen"])

    assert vergebene_titelanfaenge(db)[0] == "T-Shirt"


def test_jeder_anfang_nur_einmal():
    db = _db_mit(["T-Shirt A", "T-Shirt B", "T-Shirt C"])

    assert vergebene_titelanfaenge(db) == ["T-Shirt"]


def test_kurze_woerter_zaehlen_nicht_als_einstieg():
    """'3er' oder '2x' sind Mengenangaben, keine Produktwoerter."""
    db = _db_mit(["3er Set Tassen", "2x Kabel USB"])

    assert vergebene_titelanfaenge(db) == []


def test_satzzeichen_am_ersten_wort_stoert_nicht():
    db = _db_mit(["Hoodie, Schwarz Warm", "Hoodie Blau"])

    assert vergebene_titelanfaenge(db) == ["Hoodie"]


def test_leere_titel_kippen_nichts():
    db = _db_mit(["", None, "Hoodie Blau"])

    assert vergebene_titelanfaenge(db) == ["Hoodie"]


def test_liste_wird_gedeckelt():
    """Mehr als eine Handvoll hilft dem Modell nicht und kostet nur Tokens."""
    db = _db_mit([f"Wort{i} Rest" for i in range(40)])

    assert len(vergebene_titelanfaenge(db)) <= 15


def test_eigenes_produkt_zaehlt_beim_auffrischen_nicht_als_vergeben():
    """Sonst wandert der Titel bei jedem Neu-Import weiter weg (Pruefung 28.08.2026).

    Beim Auffrischen wird derselbe Entwurf ueberschrieben. Zaehlt sein eigener
    Titelanfang als "schon vergeben", muss das Modell jedes Mal ein neues Wort
    suchen - und landet mit jedem Durchlauf bei einem unpassenderen.
    """
    class _DB:
        def execute(self, q):
            # Die Attrappe kann nicht filtern; wir pruefen den gebauten Filter.
            return [("T-Shirt Rot",)]

    class _Zaehl:
        def __init__(self):
            self.filter_aufrufe = 0

        def execute(self, q):
            self.filter_aufrufe = str(q).count("product_id !=")
            return []

    z = _Zaehl()
    vergebene_titelanfaenge(z, ausser_produkt=7)

    assert z.filter_aufrufe == 1, "product_id muss ausgeschlossen werden"


def test_ohne_ausser_produkt_kein_zusatzfilter():
    class _Zaehl:
        def __init__(self):
            self.text = ""

        def execute(self, q):
            self.text = str(q)
            return []

    z = _Zaehl()
    vergebene_titelanfaenge(z)

    assert "product_id !=" not in z.text


def test_anfaenge_landen_im_prompt(monkeypatch):
    """Der Kern: ohne diesen Abschnitt kann die Regel gar nicht greifen."""
    gesehen = {}
    c = _client(monkeypatch, gesehen)

    asyncio.run(c.generate_listing(
        title_raw="Cotton Cat Tee", description_raw="x", category_guess=None,
        vergebene_anfaenge=["T-Shirt", "Hoodie"]))

    text = gesehen["user"]
    assert "BEREITS VERGEBENE TITELANFAENGE" in text
    assert "- T-Shirt" in text and "- Hoodie" in text


def test_ohne_vergebene_anfaenge_kein_abschnitt(monkeypatch):
    """Beim ersten Produkt gibt es nichts zu meiden - dann auch kein Prompt-Ballast."""
    gesehen = {}
    c = _client(monkeypatch, gesehen, titel="T-Shirt Katze Baumwolle")

    asyncio.run(c.generate_listing(
        title_raw="Cotton Cat Tee", description_raw="x", category_guess=None))

    assert "BEREITS VERGEBENE TITELANFAENGE" not in gesehen["user"]


def test_leere_eintraege_erzeugen_keinen_abschnitt(monkeypatch):
    """Ein leerer String ist kein vergebener Anfang."""
    gesehen = {}
    c = _client(monkeypatch, gesehen)

    asyncio.run(c.generate_listing(
        title_raw="Cotton Cat Tee", description_raw="x", category_guess=None,
        vergebene_anfaenge=["", None, "  "]))

    assert "BEREITS VERGEBENE TITELANFAENGE" not in gesehen["user"]


@pytest.mark.parametrize("titel,erwartet", [
    # Beim ersten Bekleidungs-Import entstanden genau diese beiden Anfaenge, weil der
    # Umsortierer nur Schmuck- und Deko-Woerter kannte.
    ("Sport Avete Cotto Il Razzo T-Shirt", "Sport"),
    ("Baumwoll T-Shirt We Do Recover Unisex", "Baumwoll"),
    ("Freizeit Shirt Herren Schwarz", "Freizeit"),
    # Im zweiten Import kamen englische Anpreisungen und deutsche Adjektive dazu,
    # die die Liste noch nicht kannte.
    ("Special T-Shirt Männer Aufdruck Schwarz", "Special"),
    ("Kreatives Motiv-Shirt Pew Madafakas", "Kreatives"),
    ("Lustiges Shirt Faultier Humor Zitat", "Lustiges"),
    # Gegenproben: GANZE Woerter sind Produkte und duerfen nicht ausloesen.
    ("Baumwolltuch Set Weich Gross", None),
    ("Sporttasche Gross Schwarz Reise", None),
    ("Kreativset Basteln Kinder Gross", None),
    ("Shirt Funny Faultier Humor Zitat", None),
    # Und die guten Anfaenge aus dem echten Lauf bleiben unangetastet.
    ("T-Shirt Herren Baumwolle Kurzarm", None),
    ("Herrenshirt Baumwolle Schwarz Druck", None),
    ("Motiv-Shirt Papa Unisex Baumwolle", None),
    ("Oberteil Herren T-Shirt Baumwolle", None),
])
def test_beschreibender_anfang_wird_erkannt(titel, erwartet):
    """Material- und Zweckwoerter am Titelanfang kosten Sichtbarkeit in der eBay-Suche."""
    from app.integrations.llm import leading_descriptor

    assert leading_descriptor(titel) == erwartet


def test_mock_vertraegt_den_neuen_parameter():
    """Sonst kippt jeder Test und jeder Probebetrieb, der ueber die Attrappe laeuft."""
    m = _llm.MockLLMClient()

    gen = asyncio.run(m.generate_listing(
        title_raw="Cotton Cat Tee", description_raw="x", category_guess=None,
        vergebene_anfaenge=["T-Shirt"]))

    assert gen.title_seo


@pytest.mark.parametrize("ein,erwartet", [
    # Nutzerregel 29.08.2026: "Overgroessen hoert sich falsch an, hier dann lieber den
    # englischen begriff der aber auch in deutschland etabliert ist, oversize benutzen".
    # "Overgroessen" ist ein Zwitter aus englisch oversize und deutsch Groessen - kein
    # Wort, nach dem jemand sucht.
    ("Overgrößen T-Shirt Unisex Baumwolle", "Oversize T-Shirt Unisex Baumwolle"),
    ("Overgroessen Shirt Herren Schwarz", "Oversize Shirt Herren Schwarz"),
    ("Over Größen Hoodie Baumwolle", "Oversize Hoodie Baumwolle"),
    # "Oversized" bleibt (Nutzerentscheidung 29.08.2026) - korrektes Englisch und im
    # deutschen Modehandel ein gesuchter Begriff, anders als die Mischform.
    ("T-Shirt Oversized Schwarz Druck", "T-Shirt Oversized Schwarz Druck"),
    ("T-Shirt Oversize Schwarz Druck", "T-Shirt Oversize Schwarz Druck"),
    # Etablierte deutsche Begriffe bleiben stehen:
    ("T-Shirt Übergröße 5XL Schwarz", "T-Shirt Übergröße 5XL Schwarz"),
    ("T-Shirt Plus Size Baumwolle Unisex", "T-Shirt Plus Size Baumwolle Unisex"),
    ("Große Auswahl an Größen für Herren", "Große Auswahl an Größen für Herren"),
])
def test_oversize_schreibweise(ein, erwartet):
    from app.integrations.llm import korrigiere_schreibweise

    assert korrigiere_schreibweise(ein) == erwartet
