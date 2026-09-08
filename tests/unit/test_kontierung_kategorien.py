"""Kategorien der Kontierung — ausgerichtet an den Zeilen der Anlage EUER.

Warum ueberhaupt Kategorien: die Anlage EUER (das Formular ans Finanzamt) hat feste
Zeilen. Man kann dort keine Transaktionsliste abgeben, die Betraege muessen gruppiert
sein. SKR03/SKR04 sind dagegen KEINE Pflicht — es ist der DATEV-Standard, mit dem
Steuerberater arbeiten, und die Nummern hier sind ausdruecklich Vorschlaege.

Mit Wajjahat abgestimmt am 19.08.2026.
"""
from __future__ import annotations

import pytest

from app.services.kontierung_service import KATEGORIEN, _regel_kategorie


class _Buchung:
    def __init__(self, name, betrag):
        self.counterparty_name, self.amount = name, betrag


# --------------------------------------------------------- die abgestimmte Liste
@pytest.mark.parametrize("key", [
    "wareneinkauf", "wareneinkauf_erstattung", "porto_versand", "verpackung",
    "werbung", "it_hosting", "telefon_internet", "kontofuehrung", "reisekosten",
    "bewirtung", "beratung", "fortbildung", "gwg", "sonstige_ausgabe",
])
def test_betriebsausgaben_vorhanden(key):
    assert key in KATEGORIEN, f"{key} fehlt — Zeile der Anlage EUER waere unbesetzt"


@pytest.mark.parametrize("key", ["ebay_auszahlung", "privatentnahme", "privateinlage"])
def test_kein_aufwand_aber_kategorie(key):
    """Mindert keinen Gewinn – braucht trotzdem eine Kategorie, sonst bleibt es
    ewig unkontiert liegen."""
    assert key in KATEGORIEN


def test_umbuchung_wurde_gestrichen():
    """Wajjahat: „Umbuchen eigenes Konto kann raus."""
    assert "umbuchung" not in KATEGORIEN


def test_jede_kategorie_hat_eine_lesbare_bezeichnung():
    for key, meta in KATEGORIEN.items():
        assert meta.get("label"), f"{key} ohne Bezeichnung"


# ------------------------------------------------- offene Punkte sichtbar halten
@pytest.mark.parametrize("key,stichwort", [
    ("bewirtung", "70"),          # nur 70 % abziehbar, Aufteilung noch nicht gebaut
    ("gwg", "800"),               # Grenze, darueber gehoert es ins Anlagevermoegen
    ("steuerzahlung", "Einkommensteuer"),   # KEINE Betriebsausgabe
])
def test_heikle_kategorien_tragen_ihren_hinweis(key, stichwort):
    """Diese drei sind mit dem Steuerberater zu klaeren.

    Der Hinweis steht in den Daten, damit er nicht in einem Chatverlauf verlorengeht.
    """
    assert stichwort in (KATEGORIEN[key].get("hinweis") or "")


def test_unsichere_kontonummer_wird_nicht_erfunden():
    """Lieber leer als geraten (Projektregel 14)."""
    assert KATEGORIEN["verpackung"]["skr03"] is None
    assert "Steuerberater" in KATEGORIEN["verpackung"]["hinweis"]


# --------------------------------------------------------- automatische Zuordnung
@pytest.mark.parametrize("name,betrag,erwartet", [
    ("Temu.com", -25.00, "wareneinkauf"),
    ("Temu.com", 12.00, "wareneinkauf_erstattung"),
    ("Qksource.com", -19.00, "wareneinkauf"),
    ("Autods", -38.80, "it_hosting"),
    ("Aliexpress.com", -9.00, "wareneinkauf"),
])
def test_lieferanten_werden_erkannt(name, betrag, erwartet):
    """Temu (1.605 €), Qksource und AutoDS fielen vorher durch und blieben offen."""
    assert _regel_kategorie(_Buchung(name, betrag)) == erwartet


@pytest.mark.parametrize("name", ["PayPal Europe S.a.r.l. et Cie", "Eurowings Q6m_358"])
def test_mehrdeutiges_bleibt_offen(name):
    """PayPal ist ein ZAHLWEG, keine Kategorie — was dahintersteckt, sagt der Beleg.

    Eurowings koennte Betriebsausgabe oder privat sein. Raten waere hier schlimmer
    als offenlassen: eine falsche Kategorie faellt niemandem mehr auf.
    """
    assert _regel_kategorie(_Buchung(name, -100.00)) is None


# ------------------------------------------------- GbR: Entnahmen je Gesellschafter
@pytest.mark.parametrize("key", ["entnahme_wajjahat", "entnahme_dosyar",
                                 "einlage_wajjahat", "einlage_dosyar"])
def test_eigenes_konto_je_gesellschafter(key):
    """GbR: sonst stimmen am Jahresende die Kapitalkonten und die Gewinnverteilung nicht."""
    assert key in KATEGORIEN


@pytest.mark.parametrize("name,betrag,erwartet", [
    ("Wajjahat Ahmad Akhtar Syed", -450.00, "entnahme_wajjahat"),
    ("Wajjahat Ahmad Akhtar Syed", 300.00, "einlage_wajjahat"),
    ("Dosyar Kaky", -496.28, "entnahme_dosyar"),
    ("Dosyar Kaky", 200.00, "einlage_dosyar"),
    ("Hermes Germany GmbH", -4.90, "porto_versand"),
])
def test_regeln_treffen_die_richtige_person(name, betrag, erwartet):
    """Die Richtung entscheidet: raus = Entnahme, rein = Einlage."""
    assert _regel_kategorie(_Buchung(name, betrag)) == erwartet


def test_gesellschafter_konten_ohne_geratene_nummer():
    """Welche Unterkonten die GbR bekommt, legt der Steuerberater fest."""
    assert KATEGORIEN["entnahme_wajjahat"]["skr03"] is None
    assert "Steuerberater" in KATEGORIEN["entnahme_wajjahat"]["hinweis"]
