"""Der Lagerort gehoert zu DER Variante, deren Preis gerechnet wird.

Uebernommen aus dem Ursprungssystem (Fund dort am 30.08.2026); unsere Kopie
hatte die alte Fassung behalten - Nutzerauftrag vom 03.09.2026: "Auch die sachen
hier anwenden wie gesagt mengenabgleich war nur eine sache."

Der Fehler: Die Ueberwachung fragte ``variants_have_eu_warehouse`` - "hat
IRGENDEINE SKU ein EU-Lager?". Bei einem gemischten Produkt (eine Variante aus
Deutschland, der Rest aus China) faellt damit der Pauschalzoll weg, obwohl der
Preis von einer China-Variante stammt. Ergebnis: Einkaufspreis zu niedrig, Marge
zu hoch, Gefahr zu billig zu verkaufen.

Der Repricing-Preis ist der HOECHSTE Variantenpreis. Also muss auch der Lagerort
von genau dieser Variante kommen.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.monitoring_service import _supplier_price, _supplier_price_and_origin


def _scrape(skus, produktpreis=99.0):
    return SimpleNamespace(variants={"skus": skus}, price_cny=produktpreis)


def test_teuerste_variante_aus_china_zaehlt_als_china():
    """Der gemeldete Fehlerfall."""
    s = _scrape([
        {"price": "5.00", "ship_from": "Deutschland"},
        {"price": "9.08", "ship_from": "China"},
    ])
    preis, lokal = _supplier_price_and_origin(s)
    assert preis == 9.08, "Der hoechste Variantenpreis ist die Rechenbasis"
    assert lokal is False, (
        "Die teuerste Variante kommt aus China - der Zollzuschlag muss bleiben. "
        "Ein any() ueber alle SKUs haette sie als lokal gewertet."
    )


def test_teuerste_variante_aus_der_eu_zaehlt_als_lokal():
    s = _scrape([
        {"price": "5.00", "ship_from": "China"},
        {"price": "9.08", "ship_from": "Deutschland"},
    ])
    preis, lokal = _supplier_price_and_origin(s)
    assert preis == 9.08
    assert lokal is True


def test_alle_aus_der_eu():
    s = _scrape([
        {"price": "5.00", "ship_from": "Deutschland"},
        {"price": "9.08", "ship_from": "Spanien"},
    ])
    assert _supplier_price_and_origin(s)[1] is True


def test_ohne_variantenpreis_zaehlt_nur_wenn_ALLE_aus_der_eu_kommen():
    """Der Produktpreis gehoert zu keiner einzelnen Variante.

    Dann laesst sich nicht sagen, welche gemeint ist - also nur lokal, wenn es
    gar keine China-Variante gibt.
    """
    gemischt = _scrape([{"ship_from": "Deutschland"}, {"ship_from": "China"}], 42.0)
    preis, lokal = _supplier_price_and_origin(gemischt)
    assert preis == 42.0
    assert lokal is False

    nur_eu = _scrape([{"ship_from": "Deutschland"}, {"ship_from": "Polen"}], 42.0)
    assert _supplier_price_and_origin(nur_eu)[1] is True


def test_ohne_varianten_ist_nichts_lokal():
    """Altbestand ohne ship_from zaehlt konservativ als nicht lokal."""
    preis, lokal = _supplier_price_and_origin(_scrape([], 12.0))
    assert preis == 12.0 and lokal is False


def test_kaputte_preise_stoeren_nicht():
    s = _scrape([
        {"price": "keine Zahl", "ship_from": "China"},
        {"price": None, "ship_from": "China"},
        {"price": "7.50", "ship_from": "Deutschland"},
    ])
    preis, lokal = _supplier_price_and_origin(s)
    assert preis == 7.50 and lokal is True


def test_alter_aufrufweg_liefert_weiterhin_nur_den_preis():
    """_supplier_price bleibt fuer bestehende Aufrufer unveraendert."""
    s = _scrape([{"price": "3.00", "ship_from": "China"},
                 {"price": "8.00", "ship_from": "China"}])
    assert _supplier_price(s) == 8.00
