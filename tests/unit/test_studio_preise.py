"""Preise je Produktart: im Studio einstellbar, wirken sofort auf neue Angebote."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.studio import ebay_weg, preise, router


def test_ohne_einstellung_gilt_der_katalogpreis(db):
    p = ebay_weg.produkt("tshirt")
    assert ebay_weg.preis(p, Settings()) == round(p.preis_eur, 2)
    zeile = next(z for z in preise.uebersicht(db, Settings()) if z["key"] == "tshirt")
    assert zeile["eigener_preis"] is False and zeile["preis_eur"] == zeile["standard_eur"]


def test_eingestellter_preis_gilt_sofort_fuer_angebote(db):
    assert preise.setze(db, "kids_tshirt", 16.5) == 16.5
    assert ebay_weg.preis(ebay_weg.produkt("kids_tshirt"), Settings()) == 16.5
    assert ebay_weg.preis(ebay_weg.produkt("tshirt"), Settings()) == 14.90     # andere unberuehrt
    zeile = next(z for z in preise.uebersicht(db, Settings()) if z["key"] == "kids_tshirt")
    assert zeile["eigener_preis"] and zeile["preis_eur"] == 16.5 and zeile["standard_eur"] == 14.90


def test_studio_preis_schlaegt_die_env_einstellung(db):
    s = Settings(ebay_preise="polo=25.00")
    p = ebay_weg.produkt("polo")
    assert ebay_weg.preis(p, s) == 25.00
    preise.setze(db, "polo", 19.9)
    assert ebay_weg.preis(p, s) == 19.9


def test_erneut_setzen_aendert_statt_zu_verdoppeln(db):
    preise.setze(db, "hoodie", 39.9)
    preise.setze(db, "hoodie", 41.5)
    assert preise.eingestellt(db) == {"hoodie": 41.5}


def test_zuruecksetzen_bringt_den_standard_zurueck(db):
    preise.setze(db, "tasse", 9.9)
    preise.zuruecksetzen(db, "tasse")
    assert preise.eingestellt(db) == {}
    assert ebay_weg.preis(ebay_weg.produkt("tasse"), Settings()) == 11.90


@pytest.mark.parametrize("wert", [0, -3, 0.5, 1000, "abc", None])
def test_unbrauchbare_preise_werden_abgelehnt(db, wert):
    with pytest.raises(preise.PreisFehler):
        preise.setze(db, "tshirt", wert)
    assert preise.eingestellt(db) == {}


def test_unbekannte_produktart_wird_abgelehnt(db):
    with pytest.raises(preise.PreisFehler):
        preise.setze(db, "sturmhaube", 10)
    with pytest.raises(preise.PreisFehler):
        preise.zuruecksetzen(db, "sturmhaube")


def test_endpunkte_lesen_setzen_und_zuruecksetzen(db):
    stand = router.preise_stand(db)
    assert {p["key"] for p in stand["produkte"]} == set(ebay_weg.PRODUKTE)
    assert router.preis_setzen("oversize", {"preis_eur": 37.5}, db)["preis_eur"] == 37.5
    assert next(p for p in router.preise_stand(db)["produkte"] if p["key"] == "oversize")["preis_eur"] == 37.5
    with pytest.raises(HTTPException) as fehler:
        router.preis_setzen("oversize", {"preis_eur": 0}, db)
    assert fehler.value.status_code == 400
    assert router.preis_zuruecksetzen("oversize", db)["zurueckgesetzt"] is True
