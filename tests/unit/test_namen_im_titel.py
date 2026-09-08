"""Eigennamen duerfen beim Entwurf nicht verschwinden.

Vorfall 03.08.2026: Aus "Modische Böhse Onkelz Punk Rock Auto-Kopfstützenabdeckung" wurde
"Kopfstützenabdeckung Auto Punk Rock Schwarz 2er Set" – der Bandname war weg, das Listing
damit fuer Fans unauffindbar. Die Regel im grossen Prompt allein reicht nicht, deshalb
setzt der Code fehlende Namen nach.
"""
from __future__ import annotations

import pytest

from app.brand_filter import ensure_names_in_title
from app.config import Settings
from app.integrations.llm import RealLLMClient


# ------------------------------------------------------------- Titel-Sicherheitsnetz
def test_fehlender_name_wird_hinter_den_produkttyp_gesetzt():
    titel, fehlt = ensure_names_in_title(
        "Kopfstützenabdeckung Auto Punk Rock Schwarz 2er Set", ["Böhse Onkelz"])
    assert titel.startswith("Kopfstützenabdeckung Böhse Onkelz ")
    assert fehlt == []


def test_vorhandener_name_wird_nicht_verdoppelt():
    original = "Schlüsselanhänger BT21 Plüsch Tata Cooky Shooky"
    titel, fehlt = ensure_names_in_title(original, ["BT21"])
    assert titel == original and fehlt == []


def test_schreibweise_zaehlt_als_vorhanden():
    original = "Sammelkarten Pokemon Ultra Rare Set"
    titel, _ = ensure_names_in_title(original, ["Pokémon"])
    assert titel == original          # kein zweites, anders geschriebenes Pokemon


def test_produkttyp_bleibt_das_erste_wort():
    titel, _ = ensure_names_in_title("Maske Mesh Atmungsaktiv Halloween", ["Michael Jackson"])
    assert titel.split()[0] == "Maske"
    assert "Michael Jackson" in titel


def test_achtzig_zeichen_werden_nicht_ueberschritten():
    lang = "Kopfstützenabdeckung Auto Schwarz Stabil Polyester Set Zubehör Innenraum Schutz"
    titel, fehlt = ensure_names_in_title(lang, ["Böhse Onkelz"])
    assert len(titel) <= 80
    assert "Böhse Onkelz" in titel and fehlt == []
    assert titel.startswith("Kopfstützenabdeckung Böhse Onkelz")


def test_passt_der_name_gar_nicht_wird_das_gemeldet():
    titel, fehlt = ensure_names_in_title("Kopfstützenabdeckung", ["X" * 80])
    assert fehlt == ["X" * 80]
    assert titel == "Kopfstützenabdeckung"     # lieber unveraendert als kaputt


def test_mehrere_namen():
    titel, fehlt = ensure_names_in_title("Plüschtier Weich 20cm", ["Rocky", "Hail Mary"])
    assert "Rocky" in titel and "Hail Mary" in titel and fehlt == []
    assert titel.split()[0] == "Plüschtier"


def test_leere_eingaben_sind_harmlos():
    assert ensure_names_in_title("Maske Mesh", []) == ("Maske Mesh", [])
    assert ensure_names_in_title("Maske Mesh", None) == ("Maske Mesh", [])
    assert ensure_names_in_title("", ["BT21"]) == ("BT21", [])


# ------------------------------------------------------------- Namenserkennung
class _FakeMessages:
    def __init__(self, text):
        self.text = text

    async def create(self, **kw):
        return type("R", (), {"content": [type("B", (), {"type": "text", "text": self.text})()]})()


def _client(antwort: str):
    c = RealLLMClient(Settings(llm_provider="claude", llm_api_key="x", use_mocks=False))
    c._anthropic_client = lambda: type("C", (), {"messages": _FakeMessages(antwort)})()
    return c


@pytest.mark.asyncio
async def test_erkannte_namen_werden_zurueckgegeben():
    c = _client("Böhse Onkelz")
    namen = await c.extract_names(
        title_raw="Modische Böhse Onkelz Punk Rock Band Auto-Kopfstützenabdeckung, 2 Stück")
    assert namen == ["Böhse Onkelz"]


@pytest.mark.asyncio
async def test_erfundene_namen_fliegen_raus():
    """Fail-closed: was nicht im Rohtitel steht, wird nicht behauptet."""
    c = _client("Böhse Onkelz\nMetallica\nAC/DC")
    namen = await c.extract_names(title_raw="Modische Böhse Onkelz Punk Rock Abdeckung")
    assert namen == ["Böhse Onkelz"]


@pytest.mark.asyncio
async def test_ohne_namen_leere_liste():
    c = _client("")
    assert await c.extract_names(title_raw="Kissenbezug 45x45 cm Polyester") == []


@pytest.mark.asyncio
async def test_aufzaehlungszeichen_werden_entfernt():
    c = _client("- Michael Jackson\n1. Rat Jerky")
    namen = await c.extract_names(title_raw="Abstrakte 28CM Michael Jackson Rat Jerky Puppe")
    assert namen == ["Michael Jackson", "Rat Jerky"]


@pytest.mark.asyncio
async def test_modellfehler_blockiert_nichts():
    c = RealLLMClient(Settings(llm_provider="claude", llm_api_key="x", use_mocks=False))

    def boom():
        raise RuntimeError("kein Netz")
    c._anthropic_client = boom
    assert await c.extract_names(title_raw="Irgendein Titel") == []


@pytest.mark.asyncio
async def test_leerer_rohtitel_fragt_das_modell_nicht():
    c = RealLLMClient(Settings(llm_provider="claude", llm_api_key="x", use_mocks=False))

    def boom():
        raise AssertionError("darf nicht aufgerufen werden")
    c._anthropic_client = boom
    assert await c.extract_names(title_raw="  ") == []


# ------------------------------------------------------------- Varianten in der Beschreibung
from app.brand_filter import ensure_variant_values_in_description as _varianten  # noqa: E402

_FOOTER = "📦 Sobald Ihr Paket unterwegs ist, erhalten Sie eine Sendungsverfolgung."


def test_promi_varianten_werden_ergaenzt():
    """Maske mit Promi-Motiven: das Modell laesst die Namen weg, der Code holt sie rein."""
    d = f"🎭 Gesichtsmaske aus Mesh.\n\nVerschiedene Motive erhältlich.\n\n{_FOOTER}"
    achsen = {"Charakter-Design": ["Hu Ge", "Dilraba", "Liu Yifei", "Fan Bingbing"]}
    out = _varianten(d, achsen)
    for n in achsen["Charakter-Design"]:
        assert n in out
    assert out.index("Hu Ge") < out.index(_FOOTER)      # steht VOR dem Footer
    assert out.rstrip().endswith(_FOOTER.rstrip())


def test_bereits_genannte_varianten_werden_nicht_verdoppelt():
    d = f"Motive: Hu Ge, Dilraba, Liu Yifei und Fan Bingbing.\n\n{_FOOTER}"
    achsen = {"Design": ["Hu Ge", "Dilraba", "Liu Yifei", "Fan Bingbing"]}
    assert _varianten(d, achsen) == d


def test_lange_liste_wird_gekappt_aber_angekuendigt():
    werte = [f"Motiv {i}" for i in range(1, 13)]
    out = _varianten(f"Text\n\n{_FOOTER}", {"Design": werte})
    assert "Motiv 1" in out and "Motiv 8" in out
    assert "Motiv 12" not in out
    assert "12 zur Auswahl" in out


def test_einzelne_option_ist_kein_block():
    d = f"Text\n\n{_FOOTER}"
    assert _varianten(d, {"Farbe": ["A"]}) == d


def test_ohne_footer_wird_angehaengt():
    out = _varianten("Nur Text", {"Design": ["Hu Ge", "Dilraba"]})
    assert out.startswith("Nur Text") and "Hu Ge" in out


def test_ohne_achsen_bleibt_alles_gleich():
    d = f"Text\n\n{_FOOTER}"
    assert _varianten(d, None) == d
    assert _varianten(d, {}) == d


def test_abweichende_schreibweise_wird_nicht_doppelt_eingesetzt():
    """Vorfall 03.08.: bei "Speed and Passion" im Titel wurde "Speed Passion" angehaengt."""
    original = "Vollgesichtsmaske Speed and Passion Celebrity 3D Mesh Atmungsaktiv"
    titel, fehlt = ensure_names_in_title(original, ["Speed Passion"])
    assert titel == original and fehlt == []


def test_bindestrich_schreibweise_zaehlt_als_vorhanden():
    original = "Plüschtier Demon-Slayer Tanjiro Sammelfigur 20cm"
    titel, _ = ensure_names_in_title(original, ["Demon Slayer"])
    assert titel == original


def test_echter_neuer_name_wird_weiterhin_gesetzt():
    titel, _ = ensure_names_in_title("Maske Mesh Atmungsaktiv Kostüm", ["Böhse Onkelz"])
    assert "Böhse Onkelz" in titel
