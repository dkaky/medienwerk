"""Maße dürfen von der KI NIE geändert werden (Vorfall Pokemon-Poster 12.07.:
'55x90cm' -> '90x90cm' halluziniert)."""
from __future__ import annotations

import asyncio

from app.integrations import llm as _llm
from app.integrations.llm import preserves_measurements, repair_spec_measurements
from app.services.product_service import _apply_variant_names


def test_preserves_measurements_blocks_number_changes():
    P = preserves_measurements
    # Umformatierung/Übersetzung erlaubt (Maße gleich):
    assert P("55x90cm Unframed", "55 x 90 cm ohne Rahmen") is True
    assert P("250ML", "250 ml Flasche") is True
    assert P("5PCS", "5 Stück") is True
    assert P("2 x 20mm 50Pcs", "2,0×20mm 50er") is True          # #484-Fall (kein echter Fehler)
    assert P("Rot", "Rot (kräftig)") is True                     # kein Maß -> egal
    # Echte Zahlen-Verfälschung -> blockiert:
    assert P("55x90cm Unframed", "90 x 90 cm") is False          # der reale Vorfall
    assert P("13x18cm", "18x18cm") is False
    assert P("250ml", "500ml") is False
    assert P("5 Stück", "10 Stück") is False
    assert P("40X55cm", "55 x 55 cm") is False
    # #623-Vorfall (13.07.): Germanisierung droppte eine Kante ("30X40cm" -> "40 cm"):
    assert P("30X40cmNoframe", "40 cm") is False                 # gedroppte Breite -> blockiert
    assert P("30X40cmNoframe", "30 x 40 cm") is True             # korrekte WxH -> erlaubt
    assert P("15X20cmNoframe", "20 cm") is False
    assert P("Yellow 4x6cm", "4 x 6 cm") is True                 # Dezimal-Kontext, Maß erhalten


def test_normalize_variants_drops_measurement_hallucination(monkeypatch):
    """Liefert die KI eine geänderte Größe, wird die Umbenennung verworfen (Original bleibt)."""
    class _Parsed:
        class _AM:
            axis = "Größe"
            axis_german = "Größe"
            class _V:
                def __init__(self, old, new): self.old, self.new = old, new
            values = [_V("55x90cm Unframed", "90 x 90 cm"),   # HALLUZINATION -> raus
                      _V("13x18cm Unframed", "13 x 18 cm ohne Rahmen")]  # ok -> bleibt
        axes = [_AM()]

    class _Resp:
        parsed_output = _Parsed()

    class _Client:
        class messages:
            @staticmethod
            async def parse(**kw):
                return _Resp()

    client = _llm.RealLLMClient.__new__(_llm.RealLLMClient)
    client.settings = type("S", (), {"llm_model": "x"})()
    monkeypatch.setattr(client, "_anthropic_client", lambda: _Client())
    out = asyncio.run(client.normalize_variants(
        {"Größe": ["55x90cm Unframed", "13x18cm Unframed"]}, "Poster"))
    vals = out["values"]["Größe"]
    assert "55x90cm Unframed" not in vals                       # verfälschte Umbenennung verworfen
    assert vals["13x18cm Unframed"] == "13 x 18 cm ohne Rahmen"  # saubere bleibt


def test_normalize_variants_drops_invented_names_for_number_labels(monkeypatch):
    """Vorfall Zirkonia-Kette 09.08.: reine Nummern-Varianten ("1", "Style 2") duerfen NICHT
    zu erfundenen Klarnamen ("Gold") werden — Kaeuferin bestellte 'Gold', bekam Silber
    (Ruecksendung). Erlaubt bleibt nur eine Umbenennung, die die Nummer BEHAELT."""
    class _Parsed:
        class _AM:
            axis = "Farbe"
            axis_german = "Farbe"
            class _V:
                def __init__(self, old, new): self.old, self.new = old, new
            values = [_V("1", "Gold"),             # ERFUNDEN -> verworfen
                      _V("Style 2", "Schwarz"),    # ERFUNDEN -> verworfen
                      _V("3", "Design 3")]         # Nummer erhalten -> bleibt
        axes = [_AM()]

    class _Resp:
        parsed_output = _Parsed()

    class _Client:
        class messages:
            @staticmethod
            async def parse(**kw):
                return _Resp()

    client = _llm.RealLLMClient.__new__(_llm.RealLLMClient)
    client.settings = type("S", (), {"llm_model": "x"})()
    monkeypatch.setattr(client, "_anthropic_client", lambda: _Client())
    out = asyncio.run(client.normalize_variants({"Farbe": ["1", "Style 2", "3"]}, "Kette"))
    vals = (out.get("values") or {}).get("Farbe", {})
    assert "1" not in vals and "Style 2" not in vals, "erfundene Namen muessen verworfen sein"
    assert vals.get("3") == "Design 3"


def test_repair_spec_measurements_restores_dropped_dimension_same_name():
    """item_specific 'Größe' hat nur die Breite -> aus dem Roh-Spec das volle Maß wiederherstellen."""
    specs = [{"name": "Größe", "value": "55x90cm"}, {"name": "Material", "value": "Polyester"}]
    out = repair_spec_measurements({"Größe": "90cm", "Material": "Polyester"}, specs)
    assert out["Größe"] == "55x90cm"          # volle Länge×Breite wiederhergestellt
    assert out["Material"] == "Polyester"      # Nicht-Maß unangetastet


def test_repair_spec_measurements_restores_via_measurement_overlap_renamed_key():
    """KI benennt die Achse um (Size->Maße) und droppt die Länge -> per Maß-Überschneidung heilen."""
    specs = [{"name": "Size", "value": "30x40 cm"}]
    out = repair_spec_measurements({"Maße": "40 cm"}, specs)
    assert out["Maße"] == "30x40 cm"


def test_repair_spec_measurements_allows_reformat_and_translation():
    """Korrektes Maß (nur umformatiert/übersetzt) bleibt unangetastet – kein unnötiges Überschreiben."""
    specs = [{"name": "Größe", "value": "55x90cm Unframed"}]
    out = repair_spec_measurements({"Größe": "55 x 90 cm ohne Rahmen"}, specs)
    assert out["Größe"] == "55 x 90 cm ohne Rahmen"


def test_repair_spec_measurements_leaves_non_measurements_and_extras():
    """Ohne Roh-Maß / ohne Treffer nichts erfinden; Nicht-Maß-Werte bleiben."""
    specs = [{"name": "Farbe", "value": "Schwarz"}]
    out = repair_spec_measurements({"Farbe": "Schwarz", "Marke": "Markenlos"}, specs)
    assert out == {"Farbe": "Schwarz", "Marke": "Markenlos"}
    assert repair_spec_measurements({"Größe": "90cm"}, []) == {"Größe": "90cm"}   # keine Quelle


def test_repair_spec_measurements_preserves_count_and_volume():
    specs = [{"name": "Inhalt", "value": "250 ml"}, {"name": "Menge", "value": "5 Stück"}]
    out = repair_spec_measurements({"Inhalt": "500 ml", "Menge": "5 Stück"}, specs)
    assert out["Inhalt"] == "250 ml"     # verfälschtes Volumen -> Original
    assert out["Menge"] == "5 Stück"     # korrekt -> bleibt


def test_apply_variant_names_keeps_original_when_size_dropped():
    """End-to-end: verworfene Größe -> Original-Maß bleibt in axes UND sku-options."""
    variants = {"axes": {"Größe": ["55x90cm Unframed"]},
                "skus": [{"attr": "977:2553#55x90cm Unframed",
                          "options": {"Größe": "55x90cm Unframed"}}]}
    # Mapping OHNE die verfälschte Größe (so wie normalize_variants es jetzt zurückgibt)
    mapping = {"axes": {}, "values": {"Größe": {}}}
    res = _apply_variant_names(variants, mapping)
    assert res["axes"]["Größe"] == ["55x90cm Unframed"]
    assert res["skus"][0]["options"]["Größe"] == "55x90cm Unframed"


# ---------------------------------------------------------- Erfundene Mengen (19.08.)

def test_strip_invented_counts_entfernt_zoro_3er_set():
    """Vorfall Zoro-Ohrringe: Quelle ohne Set-Angabe, KI-Titel behauptet '3er-Set'."""
    quelle = "One Piece Zoro Earrings Anime Cosplay Jewelry gold silver"
    titel = "One Piece Zoro Ohrringe Damen Herren Cosplay 3er-Set Gold Silber Anime Manga"
    neu, weg = _llm.strip_invented_counts(quelle, titel)
    assert "3er" not in neu and "Set" not in neu
    assert "  " not in neu                      # keine doppelten Leerzeichen
    assert weg and "3er-Set" in weg[0]


def test_strip_invented_counts_laesst_belegte_mengen_stehen():
    quelle = "3pcs Zoro earring set anime"
    titel = "Zoro Ohrringe 3er-Set Anime Cosplay"
    neu, weg = _llm.strip_invented_counts(quelle, titel)
    assert neu == titel and weg == []


def test_strip_invented_counts_schont_jahrzehnte_und_masse():
    titel = "Poster 80er Jahre Retro 55x90cm Vintage Deko"
    neu, weg = _llm.strip_invented_counts("Retro poster vintage", titel)
    assert neu == titel and weg == []


def test_strip_invented_counts_mehrere_behauptungen():
    titel = "Socken 2 Paar Baumwolle 5 Stück Set Sport"
    neu, weg = _llm.strip_invented_counts("cotton socks sport", titel)
    assert "Paar" not in neu and "Stück" not in neu
    assert len(weg) >= 2
