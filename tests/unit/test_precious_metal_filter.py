"""Nur 925/Echtsilber/Sterlingsilber sind verboten -> „versilbert" (Nutzerregel 28.07.).
Bloßes „Silber" ist erlaubt und bleibt unangetastet."""
from __future__ import annotations

import pytest

from app.precious_metal_filter import (contains_precious_metal_claim, correct_material_specs,
                                       sanitize_description, sanitize_title)


@pytest.mark.parametrize("raw,must_not", [
    ("925 Sterling Silber Halskette Damen", ["925", "sterling"]),
    ("Echtsilber Ring 925er Damen", ["925", "echtsilber", "echt silber"]),
    ("S925 Silver Necklace Women", ["925"]),
    ("Sterling Silver Ring Ladies", ["sterling"]),
    ("Halskette 925 Damen Herz", ["925"]),
])
def test_sanitize_title_replaces_forbidden_with_versilbert(raw, must_not):
    new, changed = sanitize_title(raw)
    assert changed is True
    low = new.lower()
    assert "versilbert" in low
    for bad in must_not:
        assert bad not in low


@pytest.mark.parametrize("ok_title", [
    "Silber Armband Damen Edelstahl",              # bloßes „Silber" -> OK, bleibt
    "Silberne Halskette silberfarben Creolen",
    "Versilberte Kette Herren 60cm",
    "Silber Ring schwarz Herren",
    "Edelstahl Ring silber Damen",
])
def test_sanitize_title_leaves_plain_silber_and_others(ok_title):
    new, changed = sanitize_title(ok_title)
    assert changed is False and new == ok_title


def test_sanitize_title_dedupes_and_80_chars():
    new, _ = sanitize_title("925 Sterling Silber " * 6 + "Kette")
    assert len(new) <= 80
    assert new.lower().split().count("versilbert") == 1


def test_sanitize_title_is_idempotent():
    a, c1 = sanitize_title("925 Sterling Silber Halskette Damen")
    b, c2 = sanitize_title(a)
    assert c1 is True and c2 is False and a == b


def test_correct_material_specs_forbidden_to_versilbert_and_drops_fineness():
    out, changed = correct_material_specs(
        {"Material": "925 Sterling Silber", "Metallreinheit": "925", "Marke": "Markenlos"})
    assert changed is True
    assert out["Material"] == "versilbert"          # Material-Behauptung -> versilbert
    assert "Metallreinheit" not in out              # Feingehalts-Merkmal entfernt
    assert out["Marke"] == "Markenlos"


def test_correct_material_specs_leaves_plain_silber_and_colour():
    specs = {"Material": "Silber", "Farbe": "Silber"}   # bloßes „Silber" bleibt (Material + Farbe)
    out, changed = correct_material_specs(specs)
    assert changed is False and out == specs


def test_sanitize_description_replaces_forbidden():
    d = "925 Sterling Silber Halskette, echtes Silber, tolles Silber-Design.\n\n1x Kette"
    new, changed = sanitize_description(d)
    assert changed is True
    assert "925" not in new and "echtes silber" not in new.lower() and "sterling" not in new.lower()
    assert "versilbert" in new.lower()
    assert "Silber-Design" in new                    # bloßes „Silber" bleibt


def test_sanitize_description_idempotent_and_clean_untouched():
    d = "925 Silber Kette\n\n1x"
    a, c1 = sanitize_description(d)
    b, c2 = sanitize_description(a)
    assert c1 is True and c2 is False and a == b
    clean = "Silber Kette aus versilbertem Edelstahl.\n\n1x"
    assert sanitize_description(clean) == (clean, False)


def test_sanitize_description_drops_false_claim_lines_and_dedupes():
    d = ("Edle Kette. Echtes versilbert-versilbert. Schön.\n"
         "* Stempel: versilbert Sterling (echtes Edelmetall)\n"
         "* Silber-Zertifikat inklusive\n"
         "* Materialtyp: Silber (Sterling versilbert)\n"
         "* Gewicht: 11 g")
    new, changed = sanitize_description(d)
    assert changed is True
    low = new.lower()
    assert "stempel" not in low and "zertifikat" not in low and "edelmetall" not in low
    assert "sterling" not in low
    assert "versilbert-versilbert" not in low and "versilbert versilbert" not in low
    assert "Materialtyp: Silber (versilbert)" in new    # Sterling raus, Klammer sauber
    assert "Gewicht: 11 g" in new                        # echte Angaben bleiben


def test_negated_claims_are_honest_disclaimers_not_violations():
    """Der ehrliche Schmuck-Disclaimer „Modeschmuck, kein Echtgold oder Echtsilber." ist KEIN
    Verstoß und bleibt wörtlich erhalten (Fund 29.07., Listing #447 wurde fälschlich geflaggt)."""
    d = "Goldfarbig und silberfarbig beschichteter Edelstahl. Modeschmuck, kein Echtgold oder Echtsilber."
    assert contains_precious_metal_claim(d) is False
    assert sanitize_description(d) == (d, False)
    assert sanitize_title("Kette Damen, kein Echtsilber") == ("Kette Damen, kein Echtsilber", False)
    # Verneinung schützt NICHT eine echte Behauptung daneben:
    mixed = "925 Sterling Silber Kette – kein Echtgold."
    assert contains_precious_metal_claim(mixed) is True
    new, ch = sanitize_description(mixed)
    assert ch is True and "925" not in new and "kein Echtgold" in new


def test_sanitize_description_keeps_ce_certificate_of_non_jewelry():
    """CE-/GS-Zertifikate OHNE Silber-Bezug (Spielzeug/Elektronik) bleiben unangetastet
    (Fund 29.07.: Wassertimer/STEM-Set wurden fälschlich geflaggt)."""
    d = "Wassertimer mit LCD.\n* CE-Zertifikat und IPX5\n* Batteriebetrieb"
    assert sanitize_description(d) == (d, False)
    assert contains_precious_metal_claim(d) is False


@pytest.mark.parametrize("text,claim", [
    ("925 Sterling Silber", True),
    ("Sterling Ring Damen", True),                       # „Sterling" im Schmuck-Kontext -> Verstoß
    ("Lightning McQueen, Sterling, Smokey Spielzeugauto", False),   # Cars-Figur „Sterling" -> ok
    ("Stempel: 925", True),                              # Stempel IM Silber-Kontext
    ("mit CE-Zertifikat", False),                        # Zertifikat OHNE Silber-Bezug -> ok
    ("Silber-Zertifikat", True),
    ("echtes Edelmetall", True),
    ("Echtsilber Ring", True),
    ("echt Silber", True),
    ("Sterling Silver", True),
    ("S925", True),
    ("Silber Kette", False),                         # bloßes „Silber" -> KEIN Verstoß mehr
    ("silberfarbene Kette", False),
    ("versilbert", False),
    ("Edelstahl schwarz", False),
])
def test_contains_precious_metal_claim(text, claim):
    assert contains_precious_metal_claim(text) is claim


def test_doppelte_woerter_im_motivnamen_bleiben_stehen():
    """Fund 28.08.2026: aus „Pew Pew Madafakas" wurde „Pew Madafakas".

    _tidy reduzierte JEDES doppelte Wort auf eines. Gemeint war "versilbert
    versilbert" - getroffen hat es alles. Der Kaeufer, der nach dem echten
    Motivnamen sucht, fand das Angebot nicht mehr; zugleich verletzt es die Regel,
    dass Motiv- und Lizenznamen woertlich stehen bleiben.
    """
    titel = "T-Shirt Pew Pew Madafakas Unisex Baumwolle Schwarz"

    neu, geaendert = sanitize_title(titel)

    assert neu == titel
    assert geaendert is False, "ohne Silber-Behauptung darf sich nichts aendern"


def test_wiederholung_als_stilmittel_bleibt():
    """„Ha Ha Ha" ist der Witz, nicht ein Fehler."""
    titel = "T-Shirt Ha Ha Ha Lustig Druck Schwarz Baumwolle"

    assert sanitize_title(titel)[0] == titel


def test_doppelter_bandname_bleibt():
    """Eigennamen sind heilig - dieselbe Regel wie bei Boehse Onkelz im Prompt."""
    titel = "Shirt Boehse Onkelz Onkelz Fan Baumwolle Schwarz"

    assert sanitize_title(titel)[0] == titel


def test_doppeltes_ersatzwort_wird_weiterhin_zusammengezogen():
    """Der gemeinte Fall muss weiter funktionieren - dafuer gibt es _dedupe_word."""
    neu, geaendert = sanitize_title("Kette versilbert versilbert Damen")

    assert neu == "Kette versilbert Damen"
    assert geaendert is True


def test_ersatzwort_mit_bindestrich_auch():
    assert sanitize_title("Kette versilbert-versilbert Damen")[0] == "Kette versilbert Damen"


def test_silber_behauptung_wird_weiterhin_ersetzt():
    """Die eigentliche Aufgabe des Filters bleibt unangetastet."""
    neu, geaendert = sanitize_title("Kette 925er Silber Damen Elegant")

    assert "925" not in neu
    assert "versilbert" in neu
    assert geaendert is True
