"""Schema-Aufrufe muessen zum eingestellten Anbieter gehen (Shop-Import 27.08.2026).

Von vierzehn Aufrufen im RealLLMClient fragten sieben nie nach ``llm_provider`` und
bauten immer den Anthropic-Client. Im Original faellt das nicht auf - dort steht
LLM_PROVIDER=claude. Hier laeuft OpenAI, und ein OpenAI-Schluessel an Anthropics
Schnittstelle ergibt 401.

Sichtbar wurde es als "normalize_variants failed", zehnmal beim Shop-Import, waehrend
die Entwuerfe sonst normal entstanden - der Hauptpfad hat die Weiche naemlich. Folge
war kein Absturz, sondern eine stille Verschlechterung: Variantennamen blieben
englisch ("black" statt "Schwarz").
"""
from __future__ import annotations

import asyncio

import pytest

from app.integrations import llm as _llm


class _Schema:
    """Platzhalter - die Attrappen geben ein fertiges Objekt zurueck."""


def _client(anbieter):
    c = _llm.RealLLMClient.__new__(_llm.RealLLMClient)
    c.settings = type("S", (), {"llm_model": "x", "llm_provider": anbieter})()
    return c


def _openai_attrappe(gesehen, ergebnis="von openai"):
    class _Nachricht:
        parsed = ergebnis

    class _Wahl:
        message = _Nachricht()

    class _Antwort:
        choices = [_Wahl()]

    class _Fake:
        class beta:
            class chat:
                class completions:
                    @staticmethod
                    async def parse(**kw):
                        gesehen.update(kw)
                        return _Antwort()
    return _Fake()


def _anthropic_attrappe(gesehen, ergebnis="von anthropic"):
    class _Antwort:
        parsed_output = ergebnis

    class _Fake:
        class messages:
            @staticmethod
            async def parse(**kw):
                gesehen.update(kw)
                return _Antwort()
    return _Fake()


def test_openai_eingestellt_also_openai_gefragt(monkeypatch):
    c = _client("openai")
    gesehen = {}
    monkeypatch.setattr(c, "_openai_client", lambda: _openai_attrappe(gesehen))
    monkeypatch.setattr(c, "_anthropic_client", lambda: pytest.fail(
        "Anthropic wurde gebaut, obwohl OpenAI eingestellt ist"))

    out = asyncio.run(c._parse_structured(
        system="S", user="U", schema=_Schema, max_tokens=99))

    assert out == "von openai"
    assert gesehen["response_format"] is _Schema
    assert gesehen["messages"][0]["role"] == "system", "System-Text gehoert in die Nachrichtenliste"
    assert gesehen["messages"][0]["content"] == "S"
    assert gesehen["messages"][1]["content"] == "U"


def test_claude_eingestellt_also_anthropic_gefragt(monkeypatch):
    c = _client("claude")
    gesehen = {}
    monkeypatch.setattr(c, "_anthropic_client", lambda: _anthropic_attrappe(gesehen))
    monkeypatch.setattr(c, "_openai_client", lambda: pytest.fail(
        "OpenAI wurde gebaut, obwohl Claude eingestellt ist"))

    out = asyncio.run(c._parse_structured(
        system="S", user="U", schema=_Schema, max_tokens=99))

    assert out == "von anthropic"
    assert gesehen["output_format"] is _Schema
    assert gesehen["system"] == "S", "Anthropic nimmt den System-Text als eigenes Feld"
    assert gesehen["max_tokens"] == 99


def test_ohne_angabe_bleibt_es_beim_alten_verhalten(monkeypatch):
    """Aeltere Test-Attrappen der Settings haben kein llm_provider - das darf nicht krachen."""
    c = _llm.RealLLMClient.__new__(_llm.RealLLMClient)
    c.settings = type("S", (), {"llm_model": "x"})()
    gesehen = {}
    monkeypatch.setattr(c, "_anthropic_client", lambda: _anthropic_attrappe(gesehen))

    out = asyncio.run(c._parse_structured(system="S", user="U", schema=_Schema))

    assert out == "von anthropic"


def test_normalize_variants_geht_jetzt_ueber_openai(monkeypatch):
    """Der konkrete Fall aus dem Shop-Import: Variantennamen eindeutschen."""
    class _Wert:
        def __init__(self, old, new):
            self.old, self.new = old, new

    class _Achse:
        axis = "Farbe"
        axis_german = "Farbe"
        values = [_Wert("black", "Schwarz")]

    class _Geparst:
        axes = [_Achse()]

    c = _client("openai")
    monkeypatch.setattr(c, "_openai_client",
                        lambda: _openai_attrappe({}, ergebnis=_Geparst()))
    monkeypatch.setattr(c, "_anthropic_client", lambda: pytest.fail(
        "normalize_variants greift immer noch fest nach Anthropic"))

    out = asyncio.run(c.normalize_variants({"Farbe": ["black"]}, "T-Shirt"))

    assert out["values"]["Farbe"]["black"] == "Schwarz"


def test_match_variant_geht_jetzt_ueber_openai(monkeypatch):
    """Die Zuordnung Bestellung -> Lieferantenvariante haengt am selben Aufruf."""
    class _Geparst:
        attr = "14:193#black"
        confidence = 0.9
        reasoning = "Farbe passt"

    c = _client("openai")
    monkeypatch.setattr(c, "_openai_client",
                        lambda: _openai_attrappe({}, ergebnis=_Geparst()))
    monkeypatch.setattr(c, "_anthropic_client", lambda: pytest.fail(
        "match_variant greift immer noch fest nach Anthropic"))

    out = asyncio.run(c.match_variant(
        ebay_selection={"Farbe": "Schwarz"},
        ali_variants=[{"attr": "14:193#black", "options": {"Farbe": "black"}}]))

    assert out["attr"] == "14:193#black"
    assert out["confidence"] == pytest.approx(0.9)


def test_kein_schema_zurueck_kippt_nichts(monkeypatch):
    """Liefert das Modell nichts Verwertbares, gilt der bisherige Notbehelf."""
    c = _client("openai")
    monkeypatch.setattr(c, "_openai_client", lambda: _openai_attrappe({}, ergebnis=None))

    out = asyncio.run(c.normalize_variants({"Farbe": ["black"]}, "T-Shirt"))

    assert out == {}
