"""Der Aufdruck gehoert in den Titel - samt Stimmung und Zielgruppe.

Nutzerregel vom 03.09.2026, woertlich: "Die titel optimieren. Hier muss der
spruch oder ein teil des spruches im titel stehen. Beim Ernie bert beispiel
waere das lecker bierchen." Und zum zweiten Beispiel: "Da muss der titel
elemente von: Witzig, humor, spruch etc beinhalten und elemente von: Fuer
Vaeter, tochter , frau etc".

Geprueft wird der REGELTEXT im Prompt, nicht die Ausgabe des Modells - ein Test
gegen ein Sprachmodell waere ein Test gegen ein bewegliches Ziel und kostete bei
jedem Lauf Geld. Was hier festgehalten wird: die Regel steht drin, sie nennt
beide Wortsorten, und sie verbietet ausdruecklich das Erfinden.

Das Verbot ist der wichtigere Teil. Der Aufdruck steht oft NUR auf dem Bild und
in keinem Text - ein Modell, das dann einen Spruch erfindet, produziert
Ruecksendungen.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

QUELLE = (Path(__file__).resolve().parents[2]
          / "app" / "integrations" / "llm.py").read_text(encoding="utf-8")

_TREFFER = re.search(
    r"MOTIV-BEKLEIDUNG – DER SPRUCH GEHÖRT IN DEN TITEL.*?(?=\n\nBei mehreren)",
    QUELLE, re.S)
BLOCK = _TREFFER.group(0) if _TREFFER else ""


def test_die_regel_steht_im_prompt():
    assert BLOCK, "Der Regelblock zum Aufdruck fehlt im Titel-Prompt"


def test_der_spruch_muss_in_den_titel():
    assert "MUSS er" in BLOCK
    assert "im Titel stehen" in BLOCK


@pytest.mark.parametrize("wort", ["Witzig", "Lustig", "Humor", "Sprüche", "Sarkasmus"])
def test_stimmungswoerter_sind_genannt(wort):
    assert wort in BLOCK, f"Stimmungswort '{wort}' fehlt in der Regel"


@pytest.mark.parametrize("wort", ["Papa", "Vater", "Ehefrau", "Tochter", "Vatertag"])
def test_zielgruppenwoerter_sind_genannt(wort):
    assert wort in BLOCK, f"Zielgruppenwort '{wort}' fehlt in der Regel"


def test_beide_nutzerbeispiele_stehen_drin():
    """Konkrete Beispiele wirken im Prompt staerker als abstrakte Regeln."""
    assert "Ernie Bert" in BLOCK and "Lecker Bierchen" in BLOCK
    assert "scare me" in BLOCK.lower() or "Töchter" in BLOCK


def test_erfinden_ist_ausdruecklich_verboten():
    """Der wichtigste Teil: der Aufdruck steht oft nur auf dem Bild."""
    assert "wird keiner erfunden" in BLOCK
    assert "rate nicht" in BLOCK
    assert "NUR auf dem Bild" in BLOCK


def test_die_regel_bricht_die_wichtigste_titelregel_nicht():
    """Der Produkttyp bleibt am Anfang - sonst leidet die eBay-Suche."""
    assert "hinter den Produkttyp, nie davor" in BLOCK
    assert "Der Titel MUSS mit dem PRODUKTTYP (Substantiv) beginnen" in QUELLE
