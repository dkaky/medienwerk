"""Das Aufgabenband der Startseite - geprueft mit echtem JavaScript.

Warum node und nicht Python: Die Logik LEBT in index.html. Sie in Python
nachzubauen hiesse, eine zweite Wahrheit zu pflegen - und die weicht ab, sobald
jemand nur die Oberflaeche anfasst. Der Test zieht deshalb den echten Quelltext
aus der Seite und laesst ihn laufen.

Geprueft wird das Verhalten, auf das es im Alltag ankommt:

* Kacheln mit 0 verschwinden. Eine Startseite, die jeden Morgen fuenf Nullen
  zeigt, gewoehnt einem das Hinsehen ab - dann faellt die Sieben auch nicht auf.
* Ohne Aufgaben steht ein Satz da, und er unterscheidet zwischen "alles
  abgearbeitet" und "noch nie ein Listing angelegt".
* Eine unvollstaendige Serverantwort darf die Startseite nicht zerlegen.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

SEITE = Path(__file__).resolve().parents[2] / "app" / "static" / "index.html"

# Attrappen fuer alles, was die Funktion sonst aus der Seite holt.
_KOPF = """
const gemerkt = { html: null };
function $(id) {
  return id === "heuteAufgaben" ? { set innerHTML(v) { gemerkt.html = v; } } : null;
}
function esc(s) { return String(s); }
function showView() {}
function filterProducts() {}
function zeigeNurFehler() {}
function loadOrders() {}
"""

_FUSS = """
const eingabe = JSON.parse(process.argv[2]);
zeichneHeute(eingabe);
const h = gemerkt.html || "";
console.log(JSON.stringify({
  kacheln: (h.match(/heute-kachel/g) || []).length,
  text: h.replace(/<[^>]+>/g, " ").replace(/[ ]+/g, " ").trim(),
}));
"""


def _logik_aus_der_seite() -> str:
    """Den Baustein aus index.html schneiden - zwischen zwei festen Marken."""
    quelle = SEITE.read_text(encoding="utf-8")
    start = quelle.index("const HEUTE_AUFGABEN")
    ende = quelle.index("    // ---------- Einstellungen: Zugaenge ----------")
    return quelle[start:ende]


def _zeichne(summary: dict) -> dict:
    skript = _KOPF + _logik_aus_der_seite() + _FUSS
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(skript)
        pfad = f.name
    try:
        lauf = subprocess.run(["node", pfad, json.dumps(summary)],
                              capture_output=True, text=True, encoding="utf-8")
    finally:
        Path(pfad).unlink(missing_ok=True)
    assert lauf.returncode == 0, f"JavaScript-Fehler:\n{lauf.stderr[:900]}"
    return json.loads(lauf.stdout)


pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node nicht installiert")


def _summary(**over) -> dict:
    basis = {
        "listings": {"total": 10, "drafts": 0, "publish_errors": 0, "out_of_stock": 0},
        "sales": {"fulfillment": {"to_order": 0}},
        "tasks": {"failed": 0},
    }
    for schluessel, wert in over.items():
        bereich, feld = schluessel.split("__")
        if bereich == "fulfillment":
            basis["sales"]["fulfillment"][feld] = wert
        else:
            basis[bereich][feld] = wert
    return basis


def test_ohne_aufgaben_keine_kacheln():
    """Alles erledigt -> ein Satz statt einer Reihe Nullen."""
    aus = _zeichne(_summary())
    assert aus["kacheln"] == 0
    assert "Nichts zu tun" in aus["text"]


def test_frischer_laden_wird_anders_angesprochen():
    """Kein Listing ist etwas anderes als nichts zu tun - der Satz sagt, was fehlt."""
    aus = _zeichne(_summary(listings__total=0))
    assert aus["kacheln"] == 0
    assert "Noch keine Listings" in aus["text"]


def test_nur_die_kachel_mit_inhalt_erscheint():
    aus = _zeichne(_summary(listings__drafts=3))
    assert aus["kacheln"] == 1
    assert "Entwürfe warten" in aus["text"]
    assert "3" in aus["text"]
    assert "Upload fehlgeschlagen" not in aus["text"]


def test_upload_fehler_steht_vor_den_anderen():
    """Ein gescheiterter Upload ist die dringendste Lage: der Entwurf haengt fest."""
    aus = _zeichne(_summary(listings__drafts=4, listings__publish_errors=2))
    assert aus["kacheln"] == 2
    assert aus["text"].index("Upload fehlgeschlagen") < aus["text"].index("Entwürfe warten")


def test_alle_aufgaben_gleichzeitig():
    aus = _zeichne(_summary(listings__drafts=4, listings__publish_errors=2,
                            listings__out_of_stock=1, fulfillment__to_order=7,
                            tasks__failed=3))
    # Bestellen, Quellen und Aufgabenprotokoll gehoerten zur AliExpress-Zeit und sind entfernt.
    assert aus["kacheln"] == 2
    for wort in ("Upload fehlgeschlagen", "Entwürfe warten"):
        assert wort in aus["text"]
    for wort in ("Zu bestellen", "Quelle ausverkauft", "Aufgaben fehlgeschlagen", "AliExpress"):
        assert wort not in aus["text"]


def test_unvollstaendige_antwort_zerlegt_die_startseite_nicht():
    """Faellt ein Feld weg, darf die Seite nicht weiss bleiben.

    Die Startseite ist das Erste, was der Betreiber sieht. Ein Fehler hier
    kostet den Zugang zu allem anderen.
    """
    for luecke in ({}, {"listings": {}}, {"sales": {}}, {"listings": {"drafts": 2}}):
        aus = _zeichne(luecke)
        assert isinstance(aus["kacheln"], int)
