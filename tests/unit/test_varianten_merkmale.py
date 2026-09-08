"""Angebots-Merkmale und Varianten-Merkmale gehoeren getrennt.

Zwei Beschwerden vom 03.09.2026, eine Ursache.

**"Wieso kann ich die Entwuerfe nicht live schalten?"** - eBay lehnte mit
Fehler 25129 ab: "no longer support custom values for Groesse". Der Grund stand
in den Listing-Merkmalen: ``Größe: "S, M, L, XL, XXL, XXXL, 4XL, 5XL"``. Eine
Achse, die je Variante verschieden ist, war als EIN Artikelmerkmal eingetragen -
und diese Kommaliste ist fuer eBay ein unerlaubter Eigenwert.

**"Farbe soll gar nicht anklickbar sein"** - ``Farbe`` fehlte in den Merkmalen,
obwohl eBay sie als Pflicht fuehrt. eBay ergaenzte sie selbst und machte daraus
eine Auswahlliste mit einem einzigen Eintrag: Schwarz.

Beides behebt dieselbe Regel: variierende Achsen raus aus den Artikelmerkmalen,
einwertige Achsen rein.
"""
from __future__ import annotations

from app.models import Product
from app.services.golive_service import _aspekte_fuer_varianten, _usable_variants


def _produkt(db, skus, achsen, kennung="vm-1") -> Product:
    p = Product(
        aliexpress_url=f"https://example.invalid/i/{kennung}",
        aliexpress_id=kennung, title_raw="T-Shirt",
        variants={"axes": achsen, "skus": skus},
    )
    db.add(p)
    db.commit()
    return p


def test_variierende_achse_fliegt_aus_den_artikelmerkmalen(db):
    """Der Ausloeser von eBay-Fehler 25129."""
    p = _produkt(db, [
        {"options": {"Farbe": "Schwarz", "Größe": "S"}},
        {"options": {"Farbe": "Schwarz", "Größe": "XXL"}},
    ], {"Farbe": ["Schwarz"], "Größe": ["S", "XXL"]})
    achsen, _ = _usable_variants(p)

    base = {"Größe": "S, M, L, XL, XXL, XXXL, 4XL, 5XL", "Material": "Baumwolle"}
    fertig = _aspekte_fuer_varianten(base, p, achsen)

    assert "Größe" not in fertig, (
        "Die Groesse steht weiter als Artikelmerkmal drin - eBay lehnt die "
        "Kommaliste als Eigenwert ab (Fehler 25129)."
    )
    assert fertig["Material"] == "Baumwolle", "Andere Merkmale wurden mit entfernt"


def test_einwertige_achse_wird_zum_artikelmerkmal(db):
    """Farbe ist ueberall Schwarz - also einmal sagen statt auswaehlen lassen."""
    p = _produkt(db, [
        {"options": {"Farbe": "Schwarz", "Größe": "S"}},
        {"options": {"Farbe": "Schwarz", "Größe": "XXL"}},
    ], {"Farbe": ["Schwarz"], "Größe": ["S", "XXL"]}, "vm-2")
    achsen, _ = _usable_variants(p)

    fertig = _aspekte_fuer_varianten({"Material": "Baumwolle"}, p, achsen)
    assert fertig.get("Farbe") == "Schwarz", (
        "Farbe fehlt als Artikelmerkmal - eBay ergaenzt sie dann selbst und "
        "zeigt dem Kaeufer eine Auswahlliste mit einem einzigen Eintrag."
    )
    assert "Farbe" not in achsen, "Farbe darf keine Auswahl-Achse sein"
    assert achsen == ["Größe"], "Auswaehlbar soll allein die Groesse sein"


def test_vorhandenes_merkmal_wird_nicht_ueberschrieben(db):
    """Was von Hand gepflegt wurde, gilt."""
    p = _produkt(db, [
        {"options": {"Farbe": "Schwarz", "Größe": "S"}},
        {"options": {"Farbe": "Schwarz", "Größe": "L"}},
    ], {"Farbe": ["Schwarz"], "Größe": ["S", "L"]}, "vm-3")
    achsen, _ = _usable_variants(p)

    fertig = _aspekte_fuer_varianten({"Farbe": "Tiefschwarz"}, p, achsen)
    assert fertig["Farbe"] == "Tiefschwarz"


def test_mehrwertige_nebenachse_wird_nicht_zum_merkmal(db):
    """Nur EINDEUTIGE Werte duerfen ans Angebot - sonst waere es geraten."""
    p = _produkt(db, [
        {"options": {"Farbe": "Schwarz", "Material": "Baumwolle", "Größe": "S"}},
        {"options": {"Farbe": "Schwarz", "Material": "Polyester", "Größe": "L"}},
    ], {"Farbe": ["Schwarz"], "Material": ["Baumwolle", "Polyester"],
        "Größe": ["S", "L"]}, "vm-4")
    achsen, _ = _usable_variants(p)

    fertig = _aspekte_fuer_varianten({}, p, achsen)
    assert fertig.get("Farbe") == "Schwarz"
    # Material hat zwei Werte und ist keine Auswahl-Achse geworden -> es waere
    # geraten, sich fuer einen zu entscheiden. Also gar nicht setzen.
    assert "Material" not in fertig


def test_einzelartikel_bleibt_unberuehrt():
    """Ohne Varianten gibt es nichts zu trennen."""
    base = {"Größe": "L", "Farbe": "Rot"}
    assert _aspekte_fuer_varianten(base, None, []) == base


def test_platzhalterwerte_werden_nicht_uebernommen(db):
    """'as picture shows' ist keine Farbe."""
    p = _produkt(db, [
        {"options": {"Farbe": "as picture shows", "Größe": "S"}},
        {"options": {"Farbe": "as picture shows", "Größe": "L"}},
    ], {"Farbe": ["as picture shows"], "Größe": ["S", "L"]}, "vm-5")
    achsen, _ = _usable_variants(p)
    fertig = _aspekte_fuer_varianten({}, p, achsen)
    assert "Farbe" not in fertig
