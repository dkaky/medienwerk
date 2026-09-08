"""Marken im Listing: echte behalten, erfundene abfangen.

Hintergrund: Beim Shop-Import am 03.08.2026 wurden Lizenznamen (BT21, Michael Jackson) aus
den Titeln entfernt, gleichzeitig trugen mehrere Entwuerfe die Marke "Bandai" - die stammte
aus dem Marken-Feld des Haendlers, der seinen GANZEN Shop pauschal so deklariert hatte.
Beides darf nicht mehr passieren: echte Lizenz rein, Pauschal-Marke raus.
"""
from __future__ import annotations

from app.brand_filter import MARKENLOS, source_haystack, verify_brand


def _quelle(*teile):
    return source_haystack(*teile)


# ------------------------------------------------------------- echte Marken bleiben stehen
def test_marke_aus_dem_rohtitel_bleibt():
    q = _quelle("BT21 Chimmy Cooky Koya Shooky Tata Anime-Schlüsselanhänger", "")
    specs, hinweis = verify_brand({"Marke": "BT21", "Material": "PVC"}, quelle=q)
    assert specs["Marke"] == "BT21"
    assert hinweis is None


def test_mehrwortmarke_bleibt():
    q = _quelle("Neue One Piece 90x150CM Polyester Pirate Flag", "")
    specs, hinweis = verify_brand({"Marke": "One Piece"}, quelle=q)
    assert specs["Marke"] == "One Piece"
    assert hinweis is None


def test_marke_nur_in_der_beschreibung_reicht():
    q = _quelle("Plüschpuppe 30cm", "Offizielles Chiikawa Merchandise")
    specs, _ = verify_brand({"Marke": "Chiikawa"}, quelle=q)
    assert specs["Marke"] == "Chiikawa"


def test_haendler_markenfeld_ist_kein_beleg():
    """Der reale Vorfall: Shop 1103475254 deklarierte seinen GANZEN Katalog als "Bandai",
    inkl. Michael-Jackson-Puppe. Das Feld darf keine Markenangabe rechtfertigen."""
    q = _quelle("Neu eingetroffen: Abstrakte 28CM Michael Jackson Rat Jerky Puppe", "")
    specs, hinweis = verify_brand({"Marke": "Bandai"}, quelle=q)
    assert specs["Marke"] == MARKENLOS
    assert hinweis is not None


def test_person_als_lizenzname_bleibt():
    q = _quelle("2026 Neuer Stil Michael Jackson Cosplay Requisiten-Puppe", "")
    specs, _ = verify_brand({"Marke": "Michael Jackson"}, quelle=q)
    assert specs["Marke"] == "Michael Jackson"


def test_schreibweise_egal():
    """Pokémon/POKEMON/poke-mon sind dieselbe Marke."""
    q = _quelle("Neue Pokémon Sammelkarten", "")
    for variante in ("Pokemon", "POKÉMON", "poke-mon"):
        specs, hinweis = verify_brand({"Marke": variante}, quelle=q)
        assert specs["Marke"] == variante, variante
        assert hinweis is None


# ------------------------------------------------------------- erfundene Marken fliegen raus
def test_fremde_marke_wird_ersetzt():
    """'Bandai' hat mit einer Chiikawa-Plueschpuppe nichts zu tun."""
    q = _quelle("Chiikawa Plüschpuppe Hachiware Usagi Momonga Kawaii Anime", "")
    specs, hinweis = verify_brand({"Marke": "Bandai", "Material": "Plüsch"}, quelle=q)
    assert specs["Marke"] == MARKENLOS
    assert specs["Material"] == "Plüsch"          # andere Merkmale unangetastet
    assert hinweis and "Bandai" in hinweis


def test_leere_quelle_laesst_keine_marke_durch():
    specs, hinweis = verify_brand({"Marke": "Bandai"}, quelle="")
    assert specs["Marke"] == MARKENLOS
    assert hinweis is not None


def test_markenlos_bleibt_unangetastet():
    specs, hinweis = verify_brand({"Marke": MARKENLOS}, quelle="")
    assert specs["Marke"] == MARKENLOS
    assert hinweis is None


def test_neutrale_werte_sind_keine_behauptung():
    for wert in ("Nicht zutreffend", "No Brand", "Generic", "Markenlos"):
        specs, hinweis = verify_brand({"Marke": wert}, quelle="")
        assert specs["Marke"] == wert, wert
        assert hinweis is None


# ------------------------------------------------------------- Randfaelle
def test_ohne_marken_merkmal_passiert_nichts():
    specs, hinweis = verify_brand({"Material": "Edelstahl"}, quelle="irgendwas")
    assert specs == {"Material": "Edelstahl"}
    assert hinweis is None


def test_schluessel_schreibweise_egal():
    q = _quelle("BT21 Anhänger", "")
    specs, _ = verify_brand({"marke": "BT21"}, quelle=q)
    assert specs["marke"] == "BT21"
    specs, hinweis = verify_brand({"MARKE": "Bandai"}, quelle=q)
    assert specs["MARKE"] == MARKENLOS and hinweis is not None


def test_listenwert_wird_geprueft():
    q = _quelle("BT21 Anhänger", "")
    specs, hinweis = verify_brand({"Marke": ["Bandai"]}, quelle=q)
    assert specs["Marke"] == MARKENLOS and hinweis is not None


def test_nicht_dict_bleibt_unveraendert():
    assert verify_brand(None, quelle="x") == (None, None)
    assert verify_brand([1, 2], quelle="x") == ([1, 2], None)


def test_haystack_nimmt_titel_und_beschreibung_auf():
    q = source_haystack("Titel BT21", "Beschreibung Sanrio")
    assert "bt21" in q and "sanrio" in q


# --- Der Aufdruck ist keine Marke (Nutzerregel 28.08.2026) --------------------
# "die tshirts sind alle markenlos das bitte merken". In drei von zwanzig Entwuerfen
# stand der Spruch vom Shirt als Marke: "The Lesbian Agenda", "We Do Recover",
# "Fluffy Cat". Die bisherige Pruefung liess sie durch, weil sie nur fragt, OB der
# Wert in der Quelle steht - ein Aufdruck steht dort natuerlich auch.

def _marke(wert, titel):
    neu, hinweis = verify_brand({"Marke": wert}, quelle=source_haystack(titel),
                                roh_titel=titel)
    return neu["Marke"], hinweis


def test_aufdruck_in_anfuehrungszeichen_ist_keine_marke():
    ist, hinweis = _marke(
        "The Lesbian Agenda",
        'Unisex T-Shirt mit Aufdruck „The Lesbian Agenda Weekly Schedule“, O-Ausschnitt')

    assert ist == MARKENLOS
    assert hinweis and "Aufdruck" in hinweis


def test_motivname_ohne_anfuehrungszeichen_ist_auch_keine_marke():
    """Der Lieferant setzt nicht immer Anfuehrungszeichen - "Kaffee-Katze" stand blank
    im Titel und landete trotzdem als Marke im Entwurf."""
    ist, _ = _marke(
        "Kaffee-Katze",
        "100% Baumwolle To-Do-Liste Kaffee-Katze O-Ausschnitt Kurzarm-T-Shirt mit Aufdruck")

    assert ist == MARKENLOS


def test_lizenzware_behaelt_ihre_marke():
    """Gegenregel des Projekts: echte Lizenznamen GEHOEREN ins Listing."""
    ist, _ = _marke("Pokemon", "Pokemon Pikachu Sammelfigur original lizenziert, 10 cm")

    assert ist == "Pokemon"


def test_markenbekleidung_mit_lizenzhinweis_bleibt():
    """Sonst wuerde die Regel auch echte Markenware entwerten."""
    ist, _ = _marke("Adidas", "Adidas Herren T-Shirt Originals lizenziert, Baumwolle")

    assert ist == "Adidas"


def test_regel_gilt_nur_fuer_bekleidung():
    """Ein Akkuschrauber ist kein T-Shirt - die Marke bleibt."""
    ist, _ = _marke("Bosch", "Bosch Akkuschrauber 12V Set mit Koffer und Bits")

    assert ist == "Bosch"


def test_abschaltbar_ueber_die_einstellung(monkeypatch):
    """Wer doch Markenbekleidung listet, setzt BEKLEIDUNG_MARKENLOS=false."""
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "bekleidung_markenlos", False, raising=False)

    ist, _ = _marke("Eigenmarke", "Eigenmarke Herren T-Shirt Baumwolle Schwarz")

    assert ist == "Eigenmarke", "ohne die Regel greift nur noch die Quellen-Pruefung"


def test_ohne_rohtitel_verhaelt_es_sich_wie_vorher():
    """Aeltere Aufrufe ohne roh_titel duerfen sich nicht anders verhalten."""
    neu, _ = verify_brand({"Marke": "Pokemon"}, quelle=source_haystack("Pokemon Figur"))

    assert neu["Marke"] == "Pokemon"
