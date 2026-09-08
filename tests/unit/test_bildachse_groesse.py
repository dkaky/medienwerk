"""Die Groesse ist keine Bild-Achse.

Nutzerbefund vom 03.09.2026, woertlich: "brauchen tshirts nicht alle einzeln ein
bild je variante, weil es ja nur groessen sind die unterschiedlich sind. Jetzt
sind 8 mal das gleiche bild im listing drin, weil jedes der varianten ein
eigenes hauptbild hat."

Ausgeloest hat es eine an sich richtige Regel in ``_image_axis``: "gibt es nur
EINE Achse, variieren die Bilder zwangslaeufig nach ihr". Die stimmt fuer Achsen
wie "Stil 1..24". Seit die einwertige Farbe korrekt aus den Achsen fliegt, bleibt
bei Bekleidung aber nur noch die Groesse uebrig - und wurde damit zur Bild-Achse.

Die Gegenprobe ist genauso wichtig: bei einer ECHTEN Bild-Achse (Farbe, Stil)
muessen die Variantenbilder weiterhin vergeben werden. Sonst waere aus einem zu
viel ein zu wenig geworden.
"""
from __future__ import annotations

from app.services.golive_service import _image_axis


def _var(optionen: dict, bild: str) -> dict:
    return {"options": optionen, "image": bild}


def test_reine_groessenachse_bekommt_keine_bildachse():
    """Der gemeldete Fall: acht Groessen, achtmal dasselbe Bild."""
    varianten = [_var({"Größe": g}, "https://example.invalid/motiv.png")
                 for g in ("S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL")]
    assert _image_axis(["Größe"], ["Größe"], {}, varianten) is None


def test_groesse_wird_uebersprungen_wenn_eine_farbe_daneben_steht():
    varianten = [
        _var({"Farbe": "Schwarz", "Größe": "S"}, "https://example.invalid/schwarz.png"),
        _var({"Farbe": "Schwarz", "Größe": "L"}, "https://example.invalid/schwarz.png"),
        _var({"Farbe": "Weiß", "Größe": "S"}, "https://example.invalid/weiss.png"),
        _var({"Farbe": "Weiß", "Größe": "L"}, "https://example.invalid/weiss.png"),
    ]
    assert _image_axis(["Farbe", "Größe"], ["Farbe", "Größe"], {}, varianten) == "Farbe"


def test_echte_bildachse_bleibt_erhalten():
    """Gegenprobe: aus 'zu viele Bilder' darf nicht 'gar keine' werden."""
    varianten = [
        _var({"Stil": "Adler"}, "https://example.invalid/adler.png"),
        _var({"Stil": "Wolf"}, "https://example.invalid/wolf.png"),
    ]
    assert _image_axis(["Stil"], ["Stil"], {}, varianten) == "Stil"


def test_farbachse_ohne_bilder_wird_trotzdem_erkannt():
    """Fallback ueber den Namen - auch wenn an den Varianten kein Bild haengt."""
    varianten = [
        {"options": {"Farbe": "Rot", "Größe": "S"}},
        {"options": {"Farbe": "Blau", "Größe": "L"}},
    ]
    assert _image_axis(["Farbe", "Größe"], ["Farbe", "Größe"], {}, varianten) == "Farbe"


def test_englische_schreibweise_zaehlt_auch():
    varianten = [_var({"Size": s}, "https://example.invalid/eins.png") for s in ("S", "L")]
    assert _image_axis(["Size"], ["Size"], {}, varianten) is None


def test_umbenannte_groessenachse_wird_erkannt():
    """eBay benennt Achsen je Kategorie um - der Name muss danach geprueft werden."""
    varianten = [_var({"Größe": g}, "https://example.invalid/eins.png") for g in ("S", "L")]
    assert _image_axis(["Groesse"], ["Größe"], {"Groesse": "Größe"}, varianten) is None
