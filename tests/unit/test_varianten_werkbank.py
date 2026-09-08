"""Varianten-Werkbank: die drei Stufen lesen -> planen -> ausfuehren.

Der Server kann hier seit jeher mehr, als die Oberflaeche zeigte: fuenfzehn
Varianten-Adressen, davon hatten genau zwei einen Knopf. Bericht,
Sammelreparatur, Achsen- und Bildreparatur waren nur per curl erreichbar - also
praktisch gar nicht.

Beim Nachruesten kommt es auf die Reihenfolge an. Die Sammelreparatur aendert
LIVE-Listings bei eBay. Der Server schuetzt davor mit ``dry_run=True`` als
Vorgabe; die Oberflaeche darf diese Vorgabe nicht stillschweigend umgehen,
sondern muss sie sichtbar machen. Deshalb wird hier geprueft:

* Der Ausfuehren-Knopf ist im Markup GESPERRT. Freigeschaltet wird er erst,
  nachdem ein Trockenlauf gezeigt hat, was passieren wuerde.
* Der Trockenlauf fragt ausdruecklich ``dry_run=true`` ab, der echte Lauf
  ``dry_run=false``. Beide Male steht es in der Adresse, nicht im Vertrauen auf
  eine Vorgabe.
* Der echte Lauf und die Einzelreparatur fragen vorher nach.
"""

from __future__ import annotations

import re
from pathlib import Path

SEITE = Path(__file__).resolve().parents[2] / "app" / "static" / "index.html"


def _quelle() -> str:
    return SEITE.read_text(encoding="utf-8")


def test_ausfuehren_knopf_startet_gesperrt():
    """Im Markup muss disabled stehen - sonst reicht ein Fehlklick nach dem Laden."""
    q = _quelle()
    knopf = re.search(r'<button[^>]*id="varLaufBtn"[^>]*>', q)
    assert knopf, "Ausfuehren-Knopf nicht gefunden"
    assert "disabled" in knopf.group(0), "Der Knopf darf nicht freigeschaltet starten"


def test_trockenlauf_und_echter_lauf_sagen_es_ausdruecklich():
    """dry_run steht in beiden Adressen - nie im Vertrauen auf die Server-Vorgabe."""
    q = _quelle()
    assert "bulk-repair-variants?scope=${scope}&dry_run=true" in q
    assert "bulk-repair-variants?scope=${scope}&dry_run=false" in q


def test_nur_der_trockenlauf_schaltet_frei():
    """Freigeschaltet wird ausschliesslich in variantenPlan, nirgends sonst."""
    q = _quelle()
    frei = [i for i, z in enumerate(q.split("\n"), 1)
            if 'varLaufBtn").disabled = false' in z]
    assert len(frei) == 1, f"Freischaltung an {len(frei)} Stellen - erwartet genau eine"

    plan_start = q.index("async function variantenPlan")
    plan_ende = q.index("async function variantenLauf")
    assert 'varLaufBtn").disabled = false' in q[plan_start:plan_ende], \
        "Die Freischaltung gehoert in den Trockenlauf"


def test_beide_veraendernden_wege_fragen_nach():
    """Was bei eBay etwas aendert, fragt vorher - Sammellauf wie Einzelreparatur."""
    q = _quelle()
    for name in ("variantenLauf", "bilderReparieren"):
        start = q.index(f"async function {name}")
        kopf = q[start:start + 900]
        assert "confirm(" in kopf, f"{name} fragt nicht nach"


def test_der_bericht_aendert_nichts():
    """Der Einstieg ist ein GET. Ein Aufruf beim Oeffnen des Reiters darf nie schreiben."""
    q = _quelle()
    start = q.index("async function ladeVarianten")
    ende = q.index("function zeichneVariantenKopf")
    rumpf = q[start:ende]
    assert 'api("GET"' in rumpf
    assert 'api("POST"' not in rumpf, "Der Bericht darf nichts veraendern"
