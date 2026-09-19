"""Echte Produktfotos: Dynamic-Mockups-Client und Fotoplan - netzwerkfrei."""
from __future__ import annotations

import json

import httpx
import pytest

from app.integrations.dynamic_mockups import DynamicMockupsClient, MockupFehler
from app.studio import mockup_plan as mp


def _vorlage(farbe=True):
    v = {"mockup_uuid": "m-1", "motiv_objekt": "so-motiv"}
    if farbe:
        v["farb_objekt"] = "so-farbe"
    return v


def _alle_vorlagen():
    textil = {"mann": _vorlage(), "frau": _vorlage(), "vorne": _vorlage()}
    return {"tshirt": textil, "polo": textil, "oversize": textil, "hoodie": textil,
            "tasse": {"vorne": _vorlage(False), "seite": _vorlage(False)}}


# --------------------------------------------------------------------------
# Fotoplan
# --------------------------------------------------------------------------
def test_sieben_farben_alle_reine_baumwolle():
    assert [f.name for f in mp.FARBEN] == ["Weiß", "Schwarz", "Navy", "Rot",
                                          "Royalblau", "Flaschengrün", "Sand"]
    assert all(f.material == "100 % Baumwolle" for f in mp.FARBEN)


def test_sparsamer_plan_ergibt_38_bilder():
    assert mp.bilder_je_motiv() == 38
    vorlagen = _alle_vorlagen()
    summe = 0
    for key, textil in (("tshirt", True), ("polo", True), ("oversize", True),
                        ("hoodie", True), ("tasse", False)):
        plan = mp.plane(key, textil=textil, vorlagen=vorlagen, hauptfarbe="Weiß")
        assert plan.fehlt == []
        summe += len(plan.auftraege)
    assert summe == 38


def test_mann_und_frau_nur_in_der_hauptfarbe():
    plan = mp.plane("hoodie", textil=True, vorlagen=_alle_vorlagen(), hauptfarbe="Schwarz")
    mann = [a for a in plan.auftraege if a.ansicht == "mann"]
    assert len(mann) == 1 and mann[0].farbe.name == "Schwarz"
    assert len([a for a in plan.auftraege if a.ansicht == "vorne"]) == 7
    assert plan.auftraege[0].label == "hoodie-mann-Black"


def test_fehlende_vorlagen_werden_gemeldet_statt_erfunden():
    vorlagen = {"tshirt": {"mann": _vorlage(), "vorne": _vorlage(farbe=False)}}
    plan = mp.plane("tshirt", textil=True, vorlagen=vorlagen, hauptfarbe="Weiß")
    assert any("'frau' fehlt" in f for f in plan.fehlt)
    assert any("keine Farbebene" in f for f in plan.fehlt)
    vorne = [a for a in plan.auftraege if a.ansicht == "vorne"]
    assert len(vorne) == 1 and vorne[0].farbe is None   # ein ehrliches statt acht falscher


def test_vorlagendatei(tmp_path):
    assert mp.lade_vorlagen(tmp_path / "gibtsnicht.json") == {}
    datei = tmp_path / "v.json"
    datei.write_text(json.dumps(_alle_vorlagen()), encoding="utf-8")
    assert mp.lade_vorlagen(datei)["tasse"]["vorne"]["mockup_uuid"] == "m-1"


def test_unbekannte_farbe():
    with pytest.raises(ValueError):
        mp.farbe("Pink")


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------
async def test_rendern_schickt_motiv_als_datei_und_farbe_an_die_farbebene(tmp_path):
    motiv = tmp_path / "motiv.png"
    motiv.write_bytes(b"\x89PNG-bild")
    gesehen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen["url"] = str(request.url)
        gesehen["schluessel"] = request.headers.get("x-api-key")
        gesehen["inhalt"] = request.content
        return httpx.Response(200, json={"success": True,
                                         "data": {"export_path": "https://cdn.dm/x.jpg"}})

    c = DynamicMockupsClient("geheim", transport=httpx.MockTransport(handler))
    try:
        adresse = await c.rendere(mockup_uuid="m-1", motiv_objekt="so-motiv", motiv_datei=motiv,
                                  farb_objekt="so-farbe", farbe_hex="#1F2A44", label="t-vorne-Navy")
    finally:
        await c.aclose()

    assert adresse == "https://cdn.dm/x.jpg"
    assert gesehen["url"] == "https://app.dynamicmockups.com/api/v1/renders"
    assert gesehen["schluessel"] == "geheim"
    inhalt = gesehen["inhalt"]
    assert b'name="smart_objects[0][asset][file]"' in inhalt and b"\x89PNG-bild" in inhalt
    assert b'name="smart_objects[1][uuid]"' in inhalt and b"so-farbe" in inhalt
    assert b'name="smart_objects[1][color]"' in inhalt and b"#1F2A44" in inhalt


async def test_abgelehnter_schluessel_wird_klar_gemeldet(tmp_path):
    motiv = tmp_path / "m.png"
    motiv.write_bytes(b"x")
    c = DynamicMockupsClient("falsch", transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"message": "Unauthenticated."})))
    try:
        with pytest.raises(MockupFehler, match="DYNAMIC_MOCKUPS_API_KEY"):
            await c.rendere(mockup_uuid="m", motiv_objekt="s", motiv_datei=motiv)
    finally:
        await c.aclose()


async def test_vorlagen_aus_allen_katalogen():
    gesehen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"success": True, "data": [{"uuid": "m-1", "name": "Hoodie Man"}]})

    c = DynamicMockupsClient("k", transport=httpx.MockTransport(handler))
    try:
        vorlagen = await c.vorlagen(name="hoodie")
    finally:
        await c.aclose()
    assert vorlagen[0]["uuid"] == "m-1"
    assert gesehen["params"] == {"include_all_catalogs": "true", "name": "hoodie"}


def test_ohne_schluessel_kein_client():
    with pytest.raises(MockupFehler):
        DynamicMockupsClient("")
