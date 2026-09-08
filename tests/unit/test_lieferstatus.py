"""Eine Zaehlweise fuer "ausverkauft" statt drei.

Der Fehler: Das Wort wurde an drei Stellen verschieden gezaehlt, und KEINE
Menge war Teilmenge einer anderen.

  A) dashboard_summary: ein einziges Flag (monitor_status == "out_of_stock")
  B) Listing-Cockpit:   isOosNoAlt ODER isUnconf, gerechnet auf einer Berichtsdatei
  C) sold_out_center:   je Variante, mit Ausweichquellen-Logik

Folge im Alltag: Die Startseite meldete 3, die Liste dahinter zeigte 7 Zeilen.
Beide hatten recht - sie beantworteten verschiedene Fragen, ohne dass das
irgendwo stand.

Jetzt entscheidet ``lieferstatus()`` fuer alle. Diese Tests halten die
Reihenfolge der Pruefungen fest, denn die IST die fachliche Aussage - besonders
zwei Faelle, die vorher falsch liefen:

* **Eigenbestand schlaegt jedes Lieferantensignal.** _stock_flags kennt
  self_stock nicht. Ein Artikel mit Ware im eigenen Regal galt als komplett
  ausverkauft, obwohl er lieferbar war.
* **Gerettete Varianten zaehlen nicht als ausverkauft.** Wenn eine
  Ausweichquelle liefert, kann der Kunde kaufen - und das ist die einzige
  Frage, die dieser Wert beantwortet.
"""

from __future__ import annotations

import pytest

from app.models import Listing
from app.services.listing_match_service import (
    AUS,
    HANDLUNGSBEDARF,
    LIEFERBAR,
    PAUSIERT,
    TEILWEISE_AUS,
    lieferstatus,
)


def _listing(**over) -> Listing:
    daten = {"title_seo": "Testartikel", "description": "d",
             "listing_status": "active"}
    daten.update(over)
    return Listing(**daten)


# ------------------------------------------------------------ Grundfaelle
def test_ohne_signale_ist_alles_lieferbar():
    assert lieferstatus(_listing(), []) == LIEFERBAR


def test_monitoring_meldet_das_listing_als_tot():
    assert lieferstatus(_listing(monitor_status="out_of_stock"), []) == AUS


# ------------------------------------------------------------ Reihenfolge
def test_pausiert_schlaegt_alles():
    """Wer bewusst angehalten hat, braucht keine Ausverkauft-Meldung dazu."""
    l = _listing(monitor_status="out_of_stock", sales_hold=True)
    assert lieferstatus(l, [{"oos": True, "sellable": False}]) == PAUSIERT


def test_eigenbestand_schlaegt_das_lieferantensignal():
    """DER Fall, der vorher falsch lief.

    _stock_flags kennt self_stock nicht. Ein Artikel mit eigener Ware im Regal
    landete als "komplett ausverkauft" in der Liste - obwohl er lieferbar ist.
    """
    l = _listing(monitor_status="out_of_stock",
                 self_stock={"__listing__": {"qty": 5}})
    assert lieferstatus(l, []) == LIEFERBAR


def test_leerer_eigenbestand_rettet_nicht():
    """qty 0 ist kein Bestand - sonst waere die Pruefung wertlos."""
    l = _listing(monitor_status="out_of_stock",
                 self_stock={"__listing__": {"qty": 0}})
    assert lieferstatus(l, []) == AUS


@pytest.mark.parametrize("kaputt", [
    None, {}, {"__listing__": None}, {"__listing__": {}},
    {"__listing__": {"qty": None}}, {"__listing__": {"qty": "keine Zahl"}},
    "gar kein Objekt",
])
def test_kaputter_eigenbestand_stuerzt_nicht_ab(kaputt):
    """Ein unerwarteter Wert darf die Startseite nicht zerlegen.

    Die Zahl steht auf der ersten Seite, die der Betreiber morgens sieht.
    """
    l = _listing(monitor_status="out_of_stock", self_stock=kaputt)
    assert lieferstatus(l, []) in (AUS, LIEFERBAR, TEILWEISE_AUS)


# ------------------------------------------------------------ Varianten
def test_eine_tote_variante_macht_teilweise_aus():
    l = _listing()
    varianten = [{"oos": True, "sellable": False}, {"oos": False}]
    assert lieferstatus(l, varianten) == TEILWEISE_AUS


def test_gerettete_variante_zaehlt_nicht_als_ausverkauft():
    """Liefert die Ausweichquelle, kann der Kunde kaufen. Mehr fragt der Wert nicht."""
    l = _listing()
    varianten = [{"oos": True, "sellable": True}, {"oos": False}]
    assert lieferstatus(l, varianten) == LIEFERBAR


def test_alle_varianten_tot_ist_ganz_aus():
    l = _listing()
    varianten = [{"oos": True, "sellable": False}, {"oos": True, "sellable": False}]
    assert lieferstatus(l, varianten) == AUS


# ------------------------------------------------------------ Handlungsbedarf
def test_handlungsbedarf_umfasst_genau_zwei_zustaende():
    """Die Kachel zaehlt diese beiden - und die Liste zeigt genau dieselben."""
    assert set(HANDLUNGSBEDARF) == {AUS, TEILWEISE_AUS}
    assert LIEFERBAR not in HANDLUNGSBEDARF
    assert PAUSIERT not in HANDLUNGSBEDARF, \
        "Ein bewusst angehaltenes Listing ist kein Handlungsbedarf"


def test_die_zustaende_sind_paarweise_verschieden():
    """Vier Werte, keine Dublette - sonst faellt die Unterscheidung still weg."""
    assert len({LIEFERBAR, TEILWEISE_AUS, AUS, PAUSIERT}) == 4


# ------------------------------------------------- Kachel gegen Liste
def test_kachel_und_liste_zeigen_dieselbe_menge(client, db):
    """Der eigentliche Fehler: Klick auf "3" landete in einer Liste mit 7 Zeilen.

    Die Zahl der Kachel MUSS der Laenge der mitgelieferten Liste entsprechen -
    sonst ist der Zaehlfehler nur verschoben statt behoben.
    """
    db.add_all([
        _listing(title_seo="lieferbar"),
        _listing(title_seo="tot", monitor_status="out_of_stock"),
        _listing(title_seo="pausiert", monitor_status="out_of_stock", sales_hold=True),
        _listing(title_seo="eigene Ware", monitor_status="out_of_stock",
                 self_stock={"__listing__": {"qty": 3}}),
        _listing(title_seo="beendet", listing_status="ended",
                 monitor_status="out_of_stock"),
    ])
    db.commit()

    d = client.get("/api/v1/dashboard/summary").json()["listings"]
    assert d["out_of_stock"] == len(d["out_of_stock_items"]), \
        "Die Kachel zaehlt anders als die Liste dahinter"

    # Genau EIN Fall bleibt: der tote. Pausiert nicht, Eigenbestand nicht,
    # beendet nicht (nur aktive Listings sind Handlungsbedarf).
    assert d["out_of_stock"] == 1
    assert d["out_of_stock_items"][0]["title"] == "tot"
