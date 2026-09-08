"""Titel muessen mit dem PRODUKT beginnen, nicht mit einer Eigenschaft.

eBay gewichtet die ersten Titelwoerter am staerksten, und Kaeufer tippen den Artikel
ein ("Vase"), nicht das Adjektiv ("edle"). Vorgabe des Verkaeufers 14.07.:
"Edle Schwarze Vase Deko" -> "Vase Deko Edel Schwarz".
"""
from __future__ import annotations

from app.integrations.llm import leading_descriptor


def test_beschreibender_anfang_wird_erkannt():
    """Adjektive, Farben, Zielgruppen und Zahl-Angaben am Anfang -> Treffer."""
    for titel in (
        "Edle Schwarze Vase Deko Japanisch",
        "Japanische Vase Schwarz Deko",
        "Schwarze Vase",
        "Hochwertiges Mikrofaser Auto-Waschset 9-teilig",
        "Damen Halskette Silber Edelstahl",
        "Herren Armband Leder Braun",
        "Gold Kette Herren Edelstahl 60cm",
        "925er Silber Halskette Damen Anhaenger",
        "3-teiliges Buersten-Set Auto",
        "18K Vergoldete Kette Herren",
    ):
        assert leading_descriptor(titel) is not None, f"nicht erkannt: {titel}"


def test_produkt_am_anfang_wird_durchgelassen():
    """Titel, die korrekt mit dem Produkt starten -> kein Treffer."""
    for titel in (
        "Vase Deko Edel Schwarz Japanisch",
        "Halskette Damen Silber 925er Anhaenger",
        "Auto-Waschset Mikrofaser 9-teilig mit Handschuh & Buerste",
        "Syrien Syria Adler Saladin Halskette Anhaenger Edelstahl Gold Silber 45cm",
        "BDSM Triskele Halskette Ring Anhaenger Edelstahl Gold Silber Schwarz Punk",
    ):
        assert leading_descriptor(titel) is None, f"faelschlich erkannt: {titel}"


def test_zusammengesetzte_produktwoerter_sind_keine_adjektive():
    """Nur GANZE Woerter zaehlen: 'Edelstahl'/'Schwarzlicht'/'Rotwein' sind Produkte,
    keine beschreibenden Anfaenge – sonst wuerde der Guard echte Titel zerschiessen."""
    assert leading_descriptor("Edelstahl Halskette Gold Herren") is None
    assert leading_descriptor("Schwarzlicht Lampe UV 395nm Party") is None
    assert leading_descriptor("Rotwein Glaeser 6er Set Kristall") is None
    assert leading_descriptor("Goldbarren Nachbildung Deko 10g") is None


def test_leere_und_kaputte_eingaben():
    assert leading_descriptor("") is None
    assert leading_descriptor(None) is None
    assert leading_descriptor("   ") is None
