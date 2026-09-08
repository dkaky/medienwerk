"""Nur deutsche und englische Ware - und im Zweifel durchlassen.

Nutzerwunsch vom 03.09.2026: "nur deutsche und englsiche produkte zu
importieren, mit franzoesisch und italienisch kann ich aktuell nichts anfangen."

Die zweite Haelfte dieser Tests wiegt schwerer als die erste. Ein Erkenner, der
bei kurzen AliExpress-Stichwortketten raten muss, wirft brauchbare Ware weg -
und das faellt niemandem auf, sie fehlt einfach. Ein durchgerutschter
franzoesischer Titel dagegen faellt sofort auf und ist von Hand geloescht.
Deshalb: nur bei Beweis ablehnen.
"""
from __future__ import annotations

import pytest

from app.services import sprachfilter


# --- Was abgelehnt werden muss ---------------------------------------------

@pytest.mark.parametrize("text", [
    "T-shirt pour homme avec manches courtes, livraison gratuite",
    "Chemise décontracté pour femme, très bonne qualité, nouvelle taille",
    "Maglietta per uomo con maniche corte, spedizione gratuita",
    "Abbigliamento casuale per donna, molto buona qualità, taglia nuova",
])
def test_franzoesisch_und_italienisch_werden_abgelehnt(text):
    with pytest.raises(sprachfilter.FremdspracheAbgelehnt):
        sprachfilter.pruefe(text)


def test_der_hinweis_sagt_was_zu_tun_ist():
    with pytest.raises(sprachfilter.FremdspracheAbgelehnt) as fehler:
        sprachfilter.pruefe("T-shirt pour homme avec manches courtes sans col")
    text = str(fehler.value)
    assert "Franzoesisch" in text
    assert "deutsche" in text and "englische" in text


# --- Was durchgehen MUSS ---------------------------------------------------

@pytest.mark.parametrize("text", [
    "T-Shirt Herren Kurzarm Baumwolle Übergröße Schwarz",
    "Cotton T-Shirt Men Summer Casual Short Sleeve Plus Size",
    "Motiv-Shirt Papa Ich Versuche Mich Zu Benehmen Unisex Baumwolle",
    "Funny Skeleton Print Tee Shirt for Men and Women",
])
def test_deutsche_und_englische_ware_geht_durch(text):
    sprachfilter.pruefe(text)


@pytest.mark.parametrize("text", [
    "T-Shirt",
    "Nike Air Max 90",
    "3XL 4XL 5XL",
    "",
    "   ",
])
def test_zu_kurz_zum_beurteilen_geht_durch(text):
    """Raten waere schlimmer als durchlassen - siehe Modulkopf."""
    sprachfilter.pruefe(text)


def test_ein_einzelnes_fremdwort_reicht_nicht():
    """"Per" kann ein Name sein, "Coton" steht in mehrsprachigen Beschreibungen."""
    sprachfilter.pruefe("Cotton T-Shirt Men Coton Per Unit Summer")


def test_nichts_uebergeben_stoert_nicht():
    sprachfilter.pruefe()
    sprachfilter.pruefe(None, None)


def test_titel_und_beschreibung_werden_zusammen_gewertet():
    """Ein Titel allein ist oft zu kurz fuer einen Beleg."""
    with pytest.raises(sprachfilter.FremdspracheAbgelehnt):
        sprachfilter.pruefe(
            "T-shirt homme",
            "Livraison gratuite pour cette pièce, avec manches courtes, très bonne qualité")


# --- Die Erkennung selbst --------------------------------------------------

def test_erkenne_nennt_die_sprache_nur_bei_beleg():
    assert sprachfilter.erkenne("T-Shirt Herren Kurzarm mit Baumwolle und Größe") == "de"
    assert sprachfilter.erkenne("Cotton shirt for men with free shipping size") == "en"
    assert sprachfilter.erkenne("pour homme avec manches sans col très") == "fr"
    assert sprachfilter.erkenne("Nike Air Max") is None


def test_erlaubte_sprachen_sind_deutsch_und_englisch():
    assert set(sprachfilter.ERLAUBT) == {"de", "en"}
