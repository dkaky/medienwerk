"""Zwei Nutzerregeln vom 03.09.2026.

**Groessen klein nach gross.** Woertlich: "Lass die anordnung der groessen immer
vom kleinsten zum groessten sein, die reihenfolge ist auch wichtig wenn der
kunde bestellen will soll diese nicht kreuz und quer sein." AliExpress liefert
die Varianten in beliebiger Folge ("4XL, 5XL, S, M, XXL, XXXL, L, XL") - und
genau so stand es im Ausklapper beim Kaeufer.

**Mindestverkaufspreis 19,95 EUR.** Woertlich: "ich will die tshirts fuer
mindestens 19,95 verkaufen ... also gilt nun die regel mindestens 19,95 EUR,
und dann noch die zwei anderen regeln mit 4 EUR und 20%." Drei Boeden, der
hoechste gewinnt.
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.services import groessen, pricing


# --- Reihenfolge -----------------------------------------------------------

def test_buchstabengroessen_klein_nach_gross():
    """Die echten Werte aus dem Bestand, in der Folge, die AliExpress liefert."""
    roh = ["4XL", "5XL", "S", "M", "XXL", "XXXL", "L", "XL"]
    assert groessen.sortiere(roh) == ["S", "M", "L", "XL", "XXL", "XXXL", "4XL", "5XL"]


def test_sortieren_schreibt_werte_nicht_um():
    """``sortiere`` ordnet, es normalisiert nicht - das sind zwei Aufgaben.

    Im Weg zu eBay laeuft die Umwandlung vorher (``_normalisiere_groessen``);
    danach sortiert diese Funktion bereits gueltige Werte. Wuerde sie zusaetzlich
    umschreiben, waere nicht mehr absehbar, wo ein Wert seine Form aendert.
    """
    assert groessen.sortiere(["XXL", "S"]) == ["S", "XXL"]
    # XXL und 2XL landen trotzdem an derselben Stelle - der Sortierschluessel
    # normalisiert intern, nur die Ausgabe bleibt unangetastet.
    assert groessen.sortierschluessel("XXL") == groessen.sortierschluessel("2XL")


def test_alphabetisch_waere_falsch():
    """Der Beweis, dass ein blosses sorted() nicht genuegt."""
    roh = ["XL", "S", "M", "L", "XXL"]
    assert sorted(roh) != groessen.sortiere(roh)
    assert groessen.sortiere(roh) == ["S", "M", "L", "XL", "XXL"]


def test_zahlengroessen_nach_zahlwert():
    assert groessen.sortiere(["44", "38", "52", "40"]) == ["38", "40", "44", "52"]


def test_zahlen_stehen_hinter_buchstaben():
    """Buchstabe und Zahl zu mischen waere geraten - 44 gegen L sagt nichts."""
    assert groessen.sortiere(["40", "M", "38", "L"]) == ["M", "L", "38", "40"]


def test_unbekanntes_steht_hinten():
    ergebnis = groessen.sortiere(["Tall L", "M", "S"])
    assert ergebnis[:2] == ["S", "M"]
    assert ergebnis[-1] == "Tall L"


# --- Nur sortieren, was sicher einzuordnen ist -----------------------------

def test_massangaben_bleiben_in_ihrer_reihenfolge():
    """Die Achse "Groesse" traegt nicht immer Konfektionsgroessen.

    Bei Stoffbahnen stehen dort Masse. Alphabetisch waere "100 cm" vor "35 cm" -
    eine Reihenfolge, die manchmal stimmt und dadurch schlimmer ist als die
    ursprungliche: sie sieht sortiert aus.
    """
    masse = ["35 x 100 cm", "40 x 100 cm", "40 x 500 cm", "40 x 200 cm"]
    assert groessen.sortiere_sicher(masse) == masse


def test_konfektionsgroessen_werden_sortiert():
    assert groessen.sortiere_sicher(["XL", "S", "M"]) == ["S", "M", "XL"]


def test_ein_unbekannter_wert_haelt_die_ganze_liste_an():
    """Halb sortiert waere irrefuehrend - dann lieber gar nicht."""
    gemischt = ["XL", "S", "Sondermass 3"]
    assert groessen.sortiere_sicher(gemischt) == gemischt


def test_zahlengroessen_gelten_als_sicher():
    assert groessen.sortiere_sicher(["44", "38", "40"]) == ["38", "40", "44"]


def test_gemischte_schreibweisen_landen_nebeneinander():
    """XXL und 2XL sind dasselbe und duerfen nicht auseinanderfallen."""
    ergebnis = groessen.sortiere(["2XL", "S", "XXL", "M"])
    assert ergebnis[:2] == ["S", "M"]
    assert set(ergebnis[2:]) == {"2XL", "XXL"}, "Gleich grosse Werte stehen getrennt"


def test_leere_liste_stoert_nicht():
    assert groessen.sortiere([]) == []
    assert groessen.sortiere(None) == []


def test_ebay_bekommt_sortierte_groessen(db):
    """Der Weg bis in die eBay-Nutzlast."""
    from app.models import Product
    from app.services.golive_service import _normalisiere_groessen, _usable_variants

    p = Product(
        aliexpress_url="https://example.invalid/i/sort", aliexpress_id="sort-1",
        title_raw="T-Shirt",
        variants={"axes": {"Größe": ["4XL", "S", "XXL", "M"]}, "skus": [
            {"options": {"Größe": "4XL"}}, {"options": {"Größe": "S"}},
            {"options": {"Größe": "XXL"}}, {"options": {"Größe": "M"}}]},
    )
    db.add(p)
    db.commit()

    achsen, varianten = _usable_variants(p)
    fertig = _normalisiere_groessen(achsen, varianten)
    werte = groessen.sortiere(v["options"]["Größe"] for v in fertig)
    assert werte == ["S", "M", "2XL", "4XL"]


# --- Mindestverkaufspreis --------------------------------------------------

def test_mindestpreis_gilt():
    """Ein billiges Shirt darf nicht unter 19,95 EUR rausgehen."""
    s = get_settings()
    b = pricing.upload_breakdown_from_cny(2.0, settings=s, local=True)
    assert b.rounded_price_eur >= 19.95, (
        f"Preis {b.rounded_price_eur} liegt unter dem Mindestpreis"
    )


def test_teurer_artikel_wird_nicht_auf_den_mindestpreis_gedrueckt():
    """Der Boden hebt an - er senkt nie."""
    s = get_settings()
    b = pricing.upload_breakdown_from_cny(60.0, settings=s, local=True)
    assert b.rounded_price_eur > 19.95


def test_alle_drei_boeden_halten_gleichzeitig():
    """Marge 20 %, Gewinn 4 EUR, Preis 19,95 EUR - keiner darf den anderen brechen."""
    s = get_settings()
    for cny in (1.0, 5.0, 12.0, 25.0):
        b = pricing.upload_breakdown_from_cny(cny, settings=s, local=True)
        assert b.rounded_price_eur >= 19.95, f"Mindestpreis verletzt bei {cny}"
        assert b.profit_eur >= s.upload_min_profit_eur - 0.01, (
            f"Mindestgewinn verletzt bei {cny}: {b.profit_eur}")
        assert b.margin_pct >= s.upload_margin_pct - 0.005, (
            f"Zielmarge verletzt bei {cny}: {b.margin_pct}")


def test_mindestpreis_laesst_sich_abschalten(monkeypatch):
    """Andere Warengruppen brauchen ihn vielleicht nicht."""
    s = get_settings()
    monkeypatch.setattr(s, "min_price_eur", 0.0)
    b = pricing.upload_breakdown_from_cny(2.0, settings=s, local=True)
    assert b.rounded_price_eur < 19.95, "Ohne Regel darf der Preis wieder fallen"


@pytest.mark.parametrize("ware", ["3.0", "6.99", "9.08", "13.99", "25.00", "49.00"])
def test_alle_rechenwege_kennen_denselben_mindestpreis(ware):
    """Der Mindestpreis darf die Preismodelle nicht auseinandertreiben.

    Erster Versuch am 03.09.2026 war ein EIGENES Feld nur fuer den Upload-Weg.
    Ergebnis: 18,95 aus dem einen Modell gegen 19,95 aus dem anderen - genau die
    Uneinigkeit, die am 29.08.2026 abgeschafft wurde, als im Dashboard neben
    einem kalkulierten Preis von 18,95 dauerhaft ein Vorschlag von 23,95 stand.

    Deshalb liegt der Boden in ``min_price_eur``, das ``compute_price`` schon
    auswertet - damit haengen alle Wege am selben Wert.
    """
    from decimal import Decimal

    alt = pricing.price_from_cny(Decimal(ware), local=True)
    neu = pricing.upload_breakdown_from_cny(Decimal(ware), local=True)
    assert alt.rounded_price_eur == neu.rounded_price_eur, (
        f"Ware {ware}: die Wege sind sich uneinig "
        f"({alt.rounded_price_eur} gegen {neu.rounded_price_eur})")


def test_preis_endet_auf_der_cent_endung():
    """19,95 passt zur Endung - der Boden darf sie nicht zerschiessen."""
    s = get_settings()
    b = pricing.upload_breakdown_from_cny(2.0, settings=s, local=True)
    assert round(b.rounded_price_eur % 1, 2) == pytest.approx(s.price_cents or 0.95)
