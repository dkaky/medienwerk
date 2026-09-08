"""Speichern schickt nur, was sich wirklich geaendert hat.

Der Fehler: ``saveEdit`` schickte Titel, Beschreibung und Merkmale IMMER mit,
den Preis fast immer - das Feld ist vorbefuellt. Serverseitig gilt "nicht leer =
geaendert" (golive_service.py:2620), also loeste jeder Speichern-Klick bei einem
Live-Listing einen vollen eBay-Push aus.

Bei einem Live-Listing MIT VARIANTEN scheiterte der immer: der Varianten-Riegel
(golive_service.py:2661) verweigert einen einzelnen Preis, weil er alle Varianten
gleichsetzen und die Preisstaffel zerstoeren wuerde. Ergebnis: solche Listings
liessen sich ueber das Bearbeiten-Fenster GAR NICHT aendern, nicht einmal der
Titel.

Die Falle beim Beheben: Als Bezugspunkt liegt ``window._lastDetail`` nahe - aber
``runAiEdit`` UEBERSCHREIBT das mit dem KI-Vorschlag, bevor das Formular
gezeichnet wird. Ein Vergleich dagegen faende nach einem Vorschlag "nichts
geaendert", und die KI-Arbeit ginge nie raus. Deshalb gibt es eine getrennte
Abschrift des Server-Standes, die der KI-Weg nicht anfasst - und deshalb ist
``test_ki_vorschlag_geht_trotzdem_raus`` der wichtigste Test in dieser Datei.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

SEITE = Path(__file__).resolve().parents[2] / "app" / "static" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node nicht installiert")

# Das Formular als Attrappe: jedes Feld ist ein Wert, den der Test setzt.
_KOPF = """
const formular = {};
const gesendet = [];
let hinweis = null;
function $(id) { return id in formular ? { value: formular[id] } : null; }
function toast(t) { hinweis = t; }
function esc(s) { return String(s); }
function openListing() {} function loadProducts() {}
async function api(methode, weg, rumpf) {
  gesendet.push({ methode, weg, rumpf });
  if (weg.endsWith("/category")) return { hinweise: [] };
  return { changed: Object.keys(rumpf || {}), pushed_to_ebay: false };
}
async function katSpeichern() { return null; }
"""

_FUSS = """
const eingabe = JSON.parse(process.argv[2]);
Object.assign(formular, eingabe.formular);
window._serverStand = eingabe.serverStand;
saveEdit(1).then(() => {
  const edit = gesendet.find(g => g.weg.endsWith("/edit"));
  console.log(JSON.stringify({
    gesendet: edit ? Object.keys(edit.rumpf).sort() : null,
    aufrufe: gesendet.length,
    hinweis,
  }));
});
"""


def _funktionen() -> str:
    q = SEITE.read_text(encoding="utf-8")
    start = q.index("    // Merkmale vergleichbar machen")
    ende = q.index("    // -------- Produkt-Ideen --------")
    return "const window = {};\n" + q[start:ende] + "\nfunction parseSpecs(text) {\n" + \
        q[q.index("      const o = {};"):q.index("      return o;\n    }") + len("      return o;\n    }")] + "\n"


def _speichern(formular: dict, server_stand: dict) -> dict:
    skript = _KOPF + _funktionen() + _FUSS
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(skript)
        pfad = f.name
    try:
        lauf = subprocess.run(
            ["node", pfad, json.dumps({"formular": formular, "serverStand": server_stand})],
            capture_output=True, text=True, encoding="utf-8")
    finally:
        Path(pfad).unlink(missing_ok=True)
    assert lauf.returncode == 0, f"JavaScript-Fehler:\n{lauf.stderr[:900]}"
    return json.loads(lauf.stdout)


_STAND = {"title": "Angler T-Shirt", "description": "Ein Shirt.",
          "price_eur": 19.99, "item_specifics": {"Marke": "Ohne", "Farbe": "Blau"}}


def _formular(**over) -> dict:
    basis = {"ed_title": _STAND["title"], "ed_desc": _STAND["description"],
             "ed_price": "19.99", "ed_specs": "Marke: Ohne\nFarbe: Blau",
             "ed_category": ""}
    basis.update(over)
    return basis


def test_ohne_aenderung_geht_nichts_raus():
    """Der Kern des Fehlers: ein Klick ohne Aenderung loeste einen eBay-Push aus."""
    aus = _speichern(_formular(), _STAND)
    assert aus["aufrufe"] == 0
    assert "Nichts geändert" in (aus["hinweis"] or "")


def test_nur_der_titel_wird_geschickt():
    """Der Fall, an dem Live-Varianten-Listings scheiterten.

    Der Preis darf NICHT mitgehen - sonst schlaegt der Varianten-Riegel zu, und
    eine reine Titelaenderung wird unmoeglich.
    """
    aus = _speichern(_formular(ed_title="Angler T-Shirt NEU"), _STAND)
    assert aus["gesendet"] == ["title"]


def test_unveraenderter_preis_geht_nicht_mit():
    aus = _speichern(_formular(ed_desc="Anderer Text"), _STAND)
    assert "price_eur" not in (aus["gesendet"] or [])


def test_geaenderter_preis_geht_mit():
    aus = _speichern(_formular(ed_price="24.90"), _STAND)
    assert aus["gesendet"] == ["price_eur"]


def test_rundungsrauschen_ist_keine_aenderung():
    """19.99 gegen 19.990000000000002 darf keinen eBay-Push ausloesen."""
    aus = _speichern(_formular(), {**_STAND, "price_eur": 19.990000000000002})
    assert aus["aufrufe"] == 0


def test_zeilenreihenfolge_der_merkmale_ist_keine_aenderung():
    """Dieselben Merkmale in anderer Reihenfolge sind dieselben Merkmale."""
    aus = _speichern(_formular(ed_specs="Farbe: Blau\nMarke: Ohne"), _STAND)
    assert aus["aufrufe"] == 0


def test_echte_merkmalsaenderung_geht_mit():
    aus = _speichern(_formular(ed_specs="Marke: Ohne\nFarbe: Rot"), _STAND)
    assert aus["gesendet"] == ["item_specifics"]


def test_ki_vorschlag_geht_trotzdem_raus():
    """DER wichtigste Fall dieser Datei.

    runAiEdit fuellt das Formular mit Titel und Beschreibung des Vorschlags und
    ueberschreibt dabei window._lastDetail. Waere _lastDetail der Bezugspunkt,
    faende der Vergleich hier "nichts geaendert" - der Vorschlag ginge verloren.
    Die getrennte Abschrift des Server-Standes verhindert genau das.
    """
    aus = _speichern(
        _formular(ed_title="Angler T-Shirt — Nur noch ein Wurf",
                  ed_desc="Von der KI neu formuliert."),
        _STAND)
    assert set(aus["gesendet"]) == {"title", "description"}


def test_kategorie_allein_reicht_zum_speichern():
    """Nur die Kategorie gewaehlt: es muss trotzdem gespeichert werden."""
    aus = _speichern(_formular(ed_category="15687"), _STAND)
    assert aus["aufrufe"] >= 1
