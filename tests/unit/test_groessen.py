"""Groessen in eBays Schreibweise - und nichts dazuerfinden.

Vorfall vom 03.09.2026: Zwei Entwuerfe scheiterten mit eBay-Fehler 25129,
"the product aspects for this category no longer support custom values for
Groesse". Ursache waren ``XXL`` und ``XXXL``: eBay fuehrt in Kategorie 15687
nur ``2XL`` und ``3XL``. Dazu Klammerzusaetze aus den Lieferantendaten wie
``S (Small)``.

Die zweite Haelfte dieser Tests ist die wichtigere: Was sich NICHT sicher
zuordnen laesst, muss unveraendert bleiben. Eine Umwandlung, die raet, verkauft
irgendwann die falsche Groesse - und das faellt erst beim Kaeufer auf.
"""
from __future__ import annotations

import pytest

from app.services import groessen


# --- Was eBay ablehnte und jetzt passt -------------------------------------

@pytest.mark.parametrize("roh,erwartet", [
    ("XXL", "2XL"),
    ("XXXL", "3XL"),
    ("XXXXL", "4XL"),
    ("xxl", "2XL"),
    ("XXL (Extra Extra Large)", "2XL"),
    ("XXXL (Extra Extra Extra Large)", "3XL"),
    ("S (Small)", "S"),
    ("M (Medium)", "M"),
    ("L (Large)", "L"),
    ("XL (Extra Large)", "XL"),
    ("2 XL", "2XL"),
    ("Large", "L"),
    ("One Size", "Einheitsgröße"),
])
def test_bekannte_formen_werden_umgeschrieben(roh, erwartet):
    assert groessen.normalisiere(roh) == erwartet


@pytest.mark.parametrize("wert", ["S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL",
                                  "XS", "2XS", "38", "44", "Einheitsgröße"])
def test_gueltige_werte_bleiben_unangetastet(wert):
    assert groessen.normalisiere(wert) == wert


def test_alle_werte_aus_dem_bestand_gehen_danach_durch():
    """Genau die Werte, die heute in der Datenbank stehen."""
    bestand = ["4XL", "5XL", "S", "M", "XXL", "XXXL", "L", "XL",
               "S (Small)", "M (Medium)", "L (Large)", "XL (Extra Large)",
               "XXL (Extra Extra Large)", "XXXL (Extra Extra Extra Large)"]
    assert groessen.unbekannte(bestand) == []


# --- Was NICHT geraten werden darf -----------------------------------------

@pytest.mark.parametrize("wert", [
    "40/42",            # Doppelgroesse - nicht in eBays Liste, aber auch nicht ratbar
    "Kindergröße 128",
    "Tall L",
    "Unbekannt",
    "",
])
def test_unsicheres_bleibt_stehen(wert):
    """Lieber eine nachvollziehbare Ablehnung als eine falsche Groesse."""
    assert groessen.normalisiere(wert) == wert.strip()


def test_zahlengroesse_wird_nicht_zu_buchstaben():
    """Aus 40 darf niemals M werden - das waere ein falsch verkaufter Artikel."""
    assert groessen.normalisiere("40") == "40"


def test_nur_groessenachsen_werden_angefasst():
    """Eine Farbe namens "XXL" darf die Umwandlung nicht treffen."""
    assert groessen.normalisiere_achse("Farbe", "XXL") == "XXL"
    assert groessen.normalisiere_achse("Größe", "XXL") == "2XL"


@pytest.mark.parametrize("name,erwartet", [
    ("Größe", True), ("größe", True), ("Groesse", True), ("Size", True),
    ("Farbe", False), ("Material", False), ("Stil", False),
])
def test_groessenachse_wird_erkannt(name, erwartet):
    assert groessen.ist_groessen_achse(name) is erwartet


# --- Der Weg zu eBay -------------------------------------------------------

def test_varianten_gehen_normalisiert_zu_ebay(db):
    """Kurz vor dem Bau der eBay-Daten muessen die Groessen stimmen.

    Die Umwandlung sitzt bewusst NICHT in ``_usable_variants``: das gibt die
    Original-Datensaetze zurueck, in die ``append_corrected_variants`` die
    vergebene eBay-SKU zurueckschreibt. Auf Kopien lief diese Rueckschreibung
    ins Leere (03.09.2026).
    """
    from app.models import Product
    from app.services.golive_service import _normalisiere_groessen, _usable_variants

    p = Product(
        aliexpress_url="https://example.invalid/i/1", aliexpress_id="ug-1",
        title_raw="T-Shirt",
        variants={
            "axes": {"Farbe": ["Schwarz"], "Größe": ["XXL", "XXXL", "S"]},
            "skus": [
                {"options": {"Farbe": "Schwarz", "Größe": "XXL"}, "price": "9"},
                {"options": {"Farbe": "Schwarz", "Größe": "XXXL"}, "price": "9"},
                {"options": {"Farbe": "Schwarz", "Größe": "S (Small)"}, "price": "9"},
            ],
        },
    )
    db.add(p)
    db.commit()

    achsen, varianten = _usable_variants(p)
    assert achsen == ["Größe"], "Die einfarbige Achse gehoert nicht zu eBay"

    fertig = _normalisiere_groessen(achsen, varianten)
    werte = sorted(v["options"]["Größe"] for v in fertig)
    assert werte == ["2XL", "3XL", "S"]
    assert groessen.unbekannte(werte) == []


def test_produktdaten_bleiben_unveraendert(db):
    """Die Lieferantendaten duerfen nicht ueberschrieben werden.

    Sonst geht die Zuordnung zur Quelle verloren - und beim naechsten Abgleich
    stimmt keine SKU mehr.
    """
    from app.models import Product
    from app.services.golive_service import _normalisiere_groessen, _usable_variants

    p = Product(
        aliexpress_url="https://example.invalid/i/2", aliexpress_id="ug-2",
        title_raw="T-Shirt",
        variants={
            "axes": {"Größe": ["XXL", "S"]},
            "skus": [
                {"options": {"Größe": "XXL"}, "price": "9"},
                {"options": {"Größe": "S"}, "price": "9"},
            ],
        },
    )
    db.add(p)
    db.commit()

    achsen, varianten = _usable_variants(p)
    _normalisiere_groessen(achsen, varianten)
    roh = [s["options"]["Größe"] for s in p.variants["skus"]]
    assert "XXL" in roh, "Die Umwandlung hat die Quelldaten ueberschrieben"


def test_usable_variants_liefert_die_originale(db):
    """Nachgelagerter Code schreibt in diese Objekte zurueck.

    ``append_corrected_variants`` traegt dort die vergebene eBay-SKU ein. Gibt
    ``_usable_variants`` Kopien zurueck, landet die SKU im Nichts - genau das
    brach am 03.09.2026 zwei Tests, nachdem die Umwandlung zu frueh ansetzte.
    """
    from app.models import Product
    from app.services.golive_service import _usable_variants

    p = Product(
        aliexpress_url="https://example.invalid/i/3", aliexpress_id="ug-3",
        title_raw="T-Shirt",
        variants={"axes": {"Größe": ["S", "L"]}, "skus": [
            {"options": {"Größe": "S"}}, {"options": {"Größe": "L"}}]},
    )
    db.add(p)
    db.commit()

    _, varianten = _usable_variants(p)
    varianten[0]["ebay_sku"] = "AE-TEST-V1"
    assert p.variants["skus"][0].get("ebay_sku") == "AE-TEST-V1", (
        "_usable_variants gibt Kopien zurueck - Rueckschreibungen gehen verloren"
    )
