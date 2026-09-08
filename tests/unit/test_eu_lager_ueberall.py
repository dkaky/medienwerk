"""Jede Kostenrechnung muss wissen, wo das Lager liegt (Durchsicht 30.08.2026).

``effective_cost(preis, local=False)`` schlaegt China-Versand UND die Zollpauschale
(3,57 EUR) auf. Bei Ware aus einem EU-Lager - erkennbar an ``ship_from`` in den
Varianten - ist beides falsch.

Der Fehler ist besonders unangenehm, weil nichts abstuerzt: der Einkaufspreis sieht
plausibel aus und ist um 3,57 EUR zu hoch. Folge im Testbestand: die Marge wirkte wie
4 % statt 20 %, und jedes Listing wurde als "price_changed" gemeldet - dreissig
Fehlalarme, die zu unnoetigen Preiserhoehungen gefuehrt haetten.

Gefunden wurde er einzeln: erst in der Ueberwachung, dann im Recherche-Import, dann
bei den VARIANTEN-Preisen (was der Kaeufer je Groesse zahlt), zuletzt in der
Versand-Pflege. Vier Runden Einzelfunde - deshalb hier eine Regel statt einer weiteren
Einzelreparatur.

Dieser Test liest den Quelltext. Das ist unueblich, aber die Alternative waere, jede
der Schreibstellen einzeln nachzustellen - und genau das haette den fuenften Fund
wieder nicht verhindert.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[2]
APP = WURZEL / "app"

# Stellen, die cost_eur aus einem Rohpreis schreiben, aber bewusst KEIN local kennen
# koennen - dort liegen gar keine Variantendaten vor. Jede Ausnahme braucht eine
# Begruendung im Code (siehe die Kommentare an den Stellen selbst).
AUSNAHMEN = {
    # (Datei, Begruendung)
    "app/routers/pricing.py": "Handrechner: der Mensch gibt die Kosten selbst ein",
}


def _quelldateien():
    for p in sorted(APP.rglob("*.py")):
        rel = p.relative_to(WURZEL).as_posix()
        if rel in AUSNAHMEN:
            continue
        yield rel, p.read_text(encoding="utf-8")


def test_jede_cost_eur_zuweisung_kennt_das_lager():
    """Wer cost_eur aus einem Rohpreis berechnet, muss das Lager beruecksichtigen."""
    fehlend = []
    for rel, text in _quelldateien():
        for m in re.finditer(r"\.cost_eur\s*=\s*", text):
            # Die ganze Anweisung einsammeln (kann ueber mehrere Zeilen gehen).
            rest = text[m.end():m.end() + 600]
            anweisung = rest.split("\n\n")[0]
            # Nur Zuweisungen, die WIRKLICH rechnen. Ein durchgereichtes
            # breakdown.cost_eur hat die Entscheidung schon hinter sich.
            if not re.search(r"effective_cost\s*\(", anweisung):
                continue
            if "local=" not in anweisung:
                zeile = text[:m.start()].count("\n") + 1
                fehlend.append(f"{rel}:{zeile}")

    assert not fehlend, (
        "Diese Stellen berechnen cost_eur ohne EU-Lager-Pruefung und schlagen damit "
        "Zoll und China-Versand auf Ware aus Deutschland auf:\n  " + "\n  ".join(fehlend))


def test_die_erkennung_selbst_funktioniert():
    """Gegenprobe: ohne sie waere der Test oben nur Formsache."""
    from app.services.fast_shipping_service import has_eu_warehouse

    assert has_eu_warehouse(["Deutschland"]) is True
    assert has_eu_warehouse(["China"]) is False
    assert has_eu_warehouse([None, "Deutschland"]) is True, "eine EU-SKU genuegt"
    assert has_eu_warehouse([]) is False, "ohne Angabe konservativ: wie China rechnen"


@pytest.mark.parametrize("lokal,erwartet", [(True, 9.08), (False, 12.65)])
def test_der_unterschied_betraegt_die_zollpauschale(lokal, erwartet):
    """Konkret, damit die Zahl im Test steht und nicht nur in der Erklaerung."""
    from app.config import get_settings
    from app.services import pricing

    s = get_settings()
    ist = pricing.effective_cost(9.08, settings=s, ship_override=0.0, local=lokal)

    assert ist == pytest.approx(erwartet, abs=0.01)
    assert round(12.65 - 9.08, 2) == s.customs_fee_eur, "die Luecke IST der Zoll"
