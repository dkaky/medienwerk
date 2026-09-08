"""Die Zuordnung Variante <-> AliExpress-SKU entsteht beim Import und bleibt.

Nutzerwunsch vom 03.09.2026: "damit nicht das selbe passiert wie im ebay
projekt, bei jedem import genau merken welche variante zu dem aliexpress
aequivalent passt ... und wissen genau beim bestellen was schon angeklickt sein
muss."

Vorher wurde die Kennung erst BEIM VEROEFFENTLICHEN festgeschrieben; bis dahin
leitete der Code sie aus der POSITION ab (V1, V2, V3 in der Reihenfolge von
AliExpress). Faellt eine Variante weg oder sortiert die Quelle um, ruecken alle
nachfolgenden eine Nummer hoch - und Preis, Bestand und Bestellung landen auf
der falschen Ware.

Der wichtigste Test hier ist ``test_vorhandene_kennungen_bleiben``: Eine
Umnummerierung waere schlimmer als gar keine Kennung, weil eine bereits
verkaufte Variante danach auf andere Ware zeigt.
"""
from __future__ import annotations

from app.services import variantenkennung


def _v(attr, groesse):
    return {"attr": attr, "options": {"Größe": groesse}, "price": "9.08"}


def test_jede_variante_bekommt_eine_kennung():
    daten = {"skus": [_v("5:a", "S"), _v("5:b", "M"), _v("5:c", "L")]}
    fertig, neu = variantenkennung.vergib(daten, "AE-123")
    assert neu == 3
    assert [s["ebay_sku"] for s in fertig["skus"]] == ["AE-123-V1", "AE-123-V2", "AE-123-V3"]


def test_vorhandene_kennungen_bleiben():
    """NIE umnummerieren - sonst zeigt eine verkaufte Variante auf andere Ware."""
    daten = {"skus": [
        {**_v("5:a", "S"), "ebay_sku": "AE-123-V7"},
        _v("5:b", "M"),
    ]}
    fertig, neu = variantenkennung.vergib(daten, "AE-123")
    assert neu == 1
    assert fertig["skus"][0]["ebay_sku"] == "AE-123-V7", "Vorhandene Kennung wurde geaendert"
    assert fertig["skus"][1]["ebay_sku"] not in ("AE-123-V7",)


def test_neue_variante_bekommt_die_naechste_freie_nummer():
    daten = {"skus": [
        {**_v("5:a", "S"), "ebay_sku": "AE-123-V1"},
        {**_v("5:b", "M"), "ebay_sku": "AE-123-V2"},
        _v("5:c", "L"),
    ]}
    fertig, _ = variantenkennung.vergib(daten, "AE-123")
    assert fertig["skus"][2]["ebay_sku"] == "AE-123-V3"


def test_luecke_wird_gefuellt():
    daten = {"skus": [
        {**_v("5:a", "S"), "ebay_sku": "AE-123-V1"},
        {**_v("5:c", "L"), "ebay_sku": "AE-123-V3"},
        _v("5:b", "M"),
    ]}
    fertig, _ = variantenkennung.vergib(daten, "AE-123")
    assert fertig["skus"][2]["ebay_sku"] == "AE-123-V2"


def test_zweiter_import_aendert_nichts():
    """Wiederholter Import darf die Zuordnung nicht durcheinanderbringen."""
    daten = {"skus": [_v("5:a", "S"), _v("5:b", "M")]}
    erst, _ = variantenkennung.vergib(daten, "AE-123")
    zweit, neu = variantenkennung.vergib(erst, "AE-123")
    assert neu == 0
    assert [s["ebay_sku"] for s in zweit["skus"]] == [s["ebay_sku"] for s in erst["skus"]]


def test_umsortierte_quelle_verschiebt_nichts():
    """Der eigentliche Fehlerfall: AliExpress liefert die Varianten anders herum."""
    daten = {"skus": [_v("5:a", "S"), _v("5:b", "M"), _v("5:c", "L")]}
    fertig, _ = variantenkennung.vergib(daten, "AE-123")
    vorher = {s["attr"]: s["ebay_sku"] for s in fertig["skus"]}

    gedreht = {"skus": list(reversed(fertig["skus"]))}
    nachher_daten, neu = variantenkennung.vergib(gedreht, "AE-123")
    nachher = {s["attr"]: s["ebay_sku"] for s in nachher_daten["skus"]}

    assert neu == 0
    assert vorher == nachher, "Die Zuordnung ist beim Umsortieren verrutscht"


def test_zuordnung_nennt_die_aliexpress_kennung():
    """Was beim Bestellen gebraucht wird: welche Optionen anklicken."""
    daten = {"skus": [_v("5:200000990;14:-1", "S"), _v("5:200000991;14:-1", "M")]}
    fertig, _ = variantenkennung.vergib(daten, "AE-123")
    paare = variantenkennung.zuordnung(fertig)
    assert paare["AE-123-V1"] == "5:200000990;14:-1"
    assert paare["AE-123-V2"] == "5:200000991;14:-1"


def test_fehlende_werden_gemeldet():
    daten = {"skus": [{**_v("5:a", "S"), "ebay_sku": "AE-123-V1"}, _v("5:b", "M")]}
    assert len(variantenkennung.fehlende(daten)) == 1
    fertig, _ = variantenkennung.vergib(daten, "AE-123")
    assert variantenkennung.fehlende(fertig) == []


def test_kaputte_eingaben_stoeren_nicht():
    assert variantenkennung.vergib(None, "AE-1") == (None, 0)
    assert variantenkennung.vergib({}, "AE-1") == ({}, 0)
    assert variantenkennung.vergib({"skus": []}, "AE-1") == ({"skus": []}, 0)
    assert variantenkennung.vergib({"skus": [_v("5:a", "S")]}, "")[1] == 0
    assert variantenkennung.zuordnung(None) == {}


def test_kennung_bleibt_unter_der_ebay_grenze():
    lang = "AE-" + "9" * 60
    fertig, _ = variantenkennung.vergib({"skus": [_v("5:a", "S")]}, lang)
    assert len(fertig["skus"][0]["ebay_sku"]) <= variantenkennung.MAX_LAENGE
