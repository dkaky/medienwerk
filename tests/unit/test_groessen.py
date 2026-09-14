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
