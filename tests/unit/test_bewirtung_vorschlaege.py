"""Anlass-Vorschlaege fuer den Bewirtungsbeleg.

Zwei Nutzer-Meldungen vom 03.08.2026:
1. Die Vorschlaege waren halb abgeschnitten – serverseitig bei 70 Zeichen gekappt.
2. Mit Stichwort ("Einkaufspreise und Verhandlungen") kamen Vorschlaege zu ganz anderen
   Themen: das Stichwort war unverbindlich, und die Sperrliste forderte gleichzeitig
   "schlage inhaltlich ANDERE vor" – zwei Anweisungen, die gegeneinander arbeiteten.
"""
from __future__ import annotations

import pytest

from app.config import Settings  # noqa: F401
from app.integrations.llm import _ANLASS_MAX_ZEICHEN, MockLLMClient, RealLLMClient


class _FakeAntwort:
    def __init__(self, text):
        self.content = [type("B", (), {"type": "text", "text": text})()]


class _FakeMessages:
    def __init__(self, text):
        self.text = text
        self.system = None
        self.prompt = None

    async def create(self, *, model, max_tokens, system, messages):
        self.system = system
        self.prompt = messages[0]["content"]
        return _FakeAntwort(self.text)


class _FakeClient:
    def __init__(self, text):
        self.messages = _FakeMessages(text)


def _client(antwort: str):
    """RealLLMClient, dessen Modell eine feste Antwort liefert."""
    c = RealLLMClient(Settings(llm_provider="claude", llm_api_key="x", use_mocks=False))
    fake = _FakeClient(antwort)
    c._anthropic_client = lambda: fake        # type: ignore[method-assign]
    return c, fake


# ------------------------------------------------------- Problem 1: abgeschnittene Texte
@pytest.mark.asyncio
async def test_zu_langer_vorschlag_wird_verworfen_nicht_gekuerzt():
    lang = ("Verhandlung ueber Einkaufskonditionen und Staffelpreise fuer die neue "
            "Herbstkollektion sowie kuenftige Rahmenbedingungen")
    assert len(lang) > _ANLASS_MAX_ZEICHEN
    c, _ = _client(f"{lang}\nEinkaufskonditionen und Staffelpreise\nLieferzeiten und Versandkosten")
    res = await c.suggest_bewirtung_anlaesse()
    assert lang not in res["vorschlaege"]
    # nichts Halbes: kein Vorschlag ist ein Praefix des zu langen Satzes
    assert not any(lang.startswith(v) for v in res["vorschlaege"])
    assert res["vorschlaege"] == ["Einkaufskonditionen und Staffelpreise",
                                  "Lieferzeiten und Versandkosten"]


@pytest.mark.asyncio
async def test_vorschlaege_sind_immer_vollstaendig():
    c, _ = _client("Einkaufskonditionen und Staffelpreise\n"
                   "Jahresmengen und Rabattstaffeln\n"
                   "Zahlungsziele und Skonto")
    res = await c.suggest_bewirtung_anlaesse()
    assert len(res["vorschlaege"]) == 3
    for v in res["vorschlaege"]:
        assert len(v) <= _ANLASS_MAX_ZEICHEN
        assert not v.endswith((",", "-", "–", "und", "fuer", "für"))


@pytest.mark.asyncio
async def test_alle_zu_lang_meldet_ehrlich_statt_halber_saetze():
    lang = "W" * (_ANLASS_MAX_ZEICHEN + 5)
    c, _ = _client(f"{lang}\n{lang}x\n{lang}y")
    res = await c.suggest_bewirtung_anlaesse()
    assert res["vorschlaege"] == []
    assert "selbst eintragen" in res["hinweis"]


# ------------------------------------------------------- Problem 2: Stichwort wirkungslos
@pytest.mark.asyncio
async def test_stichwort_wird_bindend_uebergeben():
    c, fake = _client("Einkaufspreise und Verhandlungsspielraum\nStaffelpreise\nZahlungsziele")
    await c.suggest_bewirtung_anlaesse(hinweis="Einkaufspreise und Verhandlungen")
    assert "STICHWORT" in fake.messages.prompt
    assert "Einkaufspreise und Verhandlungen" in fake.messages.prompt
    # Systemregel: Thema ist gesetzt, nur der Blickwinkel variiert
    assert "GENAU DIESES Thema" in fake.messages.system
    assert "verschiedene geschaeftliche Themen" not in fake.messages.system


@pytest.mark.asyncio
async def test_ohne_stichwort_bleiben_drei_verschiedene_themen():
    c, fake = _client("Einkaufskonditionen\nVersandwege und Laufzeiten\nSortimentserweiterung")
    await c.suggest_bewirtung_anlaesse()
    assert "verschiedene geschaeftliche Themen" in fake.messages.system
    assert "STICHWORT" not in fake.messages.prompt


@pytest.mark.asyncio
async def test_sperrliste_verdraengt_das_stichwort_nicht():
    """Der Kern des Fehlers: 'schlage inhaltlich ANDERE vor' trieb das Modell vom
    Stichwort weg, sobald man ein zweites Mal auf den Knopf drueckte."""
    c, fake = _client("Einkaufspreise Rahmenkonditionen\nMengenstaffeln\nZahlungsziele")
    await c.suggest_bewirtung_anlaesse(
        hinweis="Einkaufspreise und Verhandlungen",
        vermeiden=["Einkaufskonditionen und Staffelpreise"])
    p = fake.messages.prompt
    assert "SELBEN Thema" in p
    assert "inhaltlich ANDERE" not in p


@pytest.mark.asyncio
async def test_ohne_stichwort_bleibt_die_sperrliste_streng():
    c, fake = _client("Versandwege\nSortiment\nRetouren")
    await c.suggest_bewirtung_anlaesse(vermeiden=["Einkaufskonditionen und Staffelpreise"])
    assert "inhaltlich ANDERE" in fake.messages.prompt


@pytest.mark.asyncio
async def test_ausgewogenheitsliste_stoert_das_stichwort_nicht():
    c, fake = _client("Einkaufspreise Rahmen\nMengenstaffeln\nZahlungsziele")
    await c.suggest_bewirtung_anlaesse(hinweis="Einkaufspreise",
                                       zuletzt_benutzt=["Versandwege und Laufzeiten"])
    assert "Versandwege und Laufzeiten" not in fake.messages.prompt


# ------------------------------------------------------- Offline-Fallback (ohne Modell)
@pytest.mark.asyncio
async def test_fallback_beruecksichtigt_das_stichwort():
    """Ohne erreichbares Modell sortieren die Vorlagen wenigstens nach Stichwort."""
    m = MockLLMClient()
    ohne = (await m.suggest_bewirtung_anlaesse())["vorschlaege"]
    mit = (await m.suggest_bewirtung_anlaesse(hinweis="Versand"))["vorschlaege"]
    assert len(mit) == 3
    assert "versand" in mit[0].lower(), f"Stichwort-Treffer nicht vorn: {mit}"
    assert "versand" not in ohne[0].lower()      # ohne Stichwort steht er nicht vorn


@pytest.mark.asyncio
async def test_fallback_liefert_immer_etwas():
    m = MockLLMClient()
    res = await m.suggest_bewirtung_anlaesse(hinweis="völlig unbekanntes Thema xyz")
    assert len(res["vorschlaege"]) == 3
