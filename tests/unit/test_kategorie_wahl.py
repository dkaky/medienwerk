"""Kategoriewahl im Bearbeiten-Fenster.

Vorgeschichte: Die Bausteine lagen seit jeher im eBay-Client - Vorschlaege mit
Klartext, Pflichtmerkmale einer Kategorie. Es fehlte die Verdrahtung, und im
Formular stand zur Kategorie nur ein Hinweistext. Wer einen Artikel einstellen
wollte, konnte die Kategorie nicht waehlen.

Zwei Dinge sind beim Nachruesten heikel und werden hier festgehalten:

* **Nicht raten.** ``build_aspects`` fuellt fehlende Pflichtmerkmale mit dem
  ersten erlaubten Wert oder "Sonstige". Beim Veroeffentlichen ist das ein
  Notnagel; im Formular waere es eine Luege - es dichtete dem Betreiber eine
  Marke an, die er nie eingegeben hat.
* **Nicht behaupten.** Die Taxonomie-Aufrufe verschlucken jede Ausnahme und geben
  ``[]`` zurueck. Ein abgelaufener Zugang ist damit von "keine Pflichtmerkmale"
  nicht zu unterscheiden. Die Oberflaeche darf deshalb keine Gewissheit
  vortaeuschen.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.models import Listing
from app.services import category_service as cs

SEITE = Path(__file__).resolve().parents[2] / "app" / "static" / "index.html"


# ---------------------------------------------------------------- Pruefung
@pytest.mark.parametrize("wert", ["0", "00", "", "   ", "abc", "15687x", None])
def test_untaugliche_kategorie_wird_abgewiesen(wert):
    """Vor allem die '0': genau dieser Wert ging frueher an eBay (Fehler 25002)."""
    with pytest.raises(cs.KategorieFehler):
        cs.pruefe_kategorie_id(wert)


def test_gueltige_kategorie_kommt_sauber_zurueck():
    assert cs.pruefe_kategorie_id(" 15687 ") == "15687"
    assert cs.pruefe_kategorie_id(15687) == "15687"


# ---------------------------------------------------------------- Anzeigename
def test_name_traegt_den_obersten_zweig_vorne():
    """Die Provisionsrechnung liest nur den Text VOR dem ersten Doppelpunkt.

    Ein blosser Blattname liesse sie still auf die Pauschale fallen - deshalb
    steht der oberste Zweig vorne.
    """
    name = cs.name_aus_vorschlag({
        "name": "Herren-T-Shirts",
        "pfad": "Kleidung & Accessoires > Herren > Herrenbekleidung",
    })
    assert name == "Kleidung & Accessoires: Herren-T-Shirts"
    assert name.split(":")[0].strip() == "Kleidung & Accessoires"


def test_ohne_pfad_lieber_kein_name():
    """Eine Luecke ist ehrlicher als ein halber Name, auf den sich Geld stuetzt."""
    assert cs.name_aus_vorschlag({"name": "Herren-T-Shirts", "pfad": ""}) is None
    assert cs.name_aus_vorschlag({"name": "", "pfad": "Kleidung > Herren"}) is None
    assert cs.name_aus_vorschlag({}) is None


# ---------------------------------------------------------------- Dienst
def test_vorschlaege_nennen_ihre_herkunft():
    """Im Probebetrieb darf die Oberflaeche nicht so tun, als kaeme das von eBay."""
    aus = asyncio.run(cs.vorschlaege("Angler T-Shirt"))
    assert aus["quelle"] == "attrappe"          # Tests laufen mit USE_MOCKS=true
    assert all({"id", "name", "pfad"} <= set(v) for v in aus["vorschlaege"])


def test_merkmale_trennen_pflicht_von_kuer():
    aus = asyncio.run(cs.merkmale("15687"))
    assert aus["category_id"] == "15687"
    assert aus["pflicht_anzahl"] == sum(1 for m in aus["merkmale"] if m["required"])
    assert aus["pflicht_anzahl"] >= 1
    # Ein Pflichtmerkmal ohne Werteliste muss vorkommen (Fall "freier Text") -
    # ein Formular, das nur Auswahllisten kennt, faellt sonst erst live auf.
    assert any(m["required"] and not m["values"] for m in aus["merkmale"])


# ---------------------------------------------------------------- Adressen
# ---------------------------------------------------------------- Oberflaeche
def test_formular_fuellt_die_merkmale_nie_selbst():
    """Der Merkmals-Helfer darf ed_specs LESEN, aber nie beschreiben.

    Frueher hiess die Pruefung hier "das Wort build_aspects darf nicht
    vorkommen" - und schlug am eigenen Kommentar an, der die Regel erklaert.
    Das ist die falsche Frage. Die richtige lautet: wird das Merkmalsfeld
    angefasst? Ein Zuweisen von ed_specs waere genau das Auffuellen, das im
    Formular nichts zu suchen hat.
    """
    q = SEITE.read_text(encoding="utf-8")
    rumpf = q[q.index("async function katMerkmale"):q.index("async function katSpeichern")]

    assert '$("ed_specs").value' in rumpf, "die vorhandenen Merkmale werden gelesen"
    for schreibend in ('ed_specs").value =', 'ed_specs").value+=', 'ed_specs").value +='):
        assert schreibend not in rumpf, f"ed_specs wird beschrieben: {schreibend}"
    assert "nicht geraten" in rumpf, "die Regel gehoert sichtbar in den Text"


def test_leere_vorschlagsliste_behauptet_nichts():
    """Leer heisst nicht 'es gibt keine' - der Client verschluckt Fehler zu []."""
    q = SEITE.read_text(encoding="utf-8")
    start = q.index("async function katVorschlaege")
    ende = q.index("function katWaehlen")
    rumpf = q[start:ende]
    assert "nicht antwortet" in rumpf, "Der Zweifel muss im Text stehen"
