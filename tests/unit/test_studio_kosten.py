"""Tests der Tages-Kostenbremse.

Diese Bremse ist die einzige Sicherung zwischen einem Programmierfehler und einer
echten Rechnung. Jeder Test hier steht fuer einen Weg, auf dem ohne ihn Geld
abfliessen koennte.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.studio import kosten


@pytest.fixture
def budget(monkeypatch):
    """Setzt ein Tagesbudget von 1,00 USD fuer den Test."""

    def _setzen(betrag: float):
        monkeypatch.setattr(kosten, "tagesbudget", lambda: betrag)

    return _setzen


def test_ohne_budget_wird_nichts_erzeugt(db, budget):
    """Fail-closed: Wer kein Budget setzt, erzeugt nichts.

    Die Umkehrung waere gefaehrlich - ein vergessener Eintrag in der .env
    duerfte nicht bedeuten, dass unbegrenzt erzeugt wird.
    """
    budget(0.0)
    with pytest.raises(kosten.BudgetErschoepft, match="Kein Tagesbudget"):
        kosten.pruefe(db, 0.08)


def test_innerhalb_des_budgets_erlaubt(db, budget):
    budget(1.0)
    kosten.pruefe(db, 0.08)          # wirft nicht


def test_ueber_dem_budget_abgelehnt(db, budget):
    budget(1.0)
    kosten.verbuche(db, provider="openai", kosten_usd=0.95)
    with pytest.raises(kosten.BudgetErschoepft, match="erschoepft"):
        kosten.pruefe(db, 0.08)


def test_verbrauch_summiert_sich(db, budget):
    budget(1.0)
    for _ in range(3):
        kosten.verbuche(db, provider="fal", kosten_usd=0.05)
    assert kosten.verbraucht_heute(db) == pytest.approx(0.15)
    assert kosten.rest_heute(db) == pytest.approx(0.85)


def test_unbekannte_kosten_werden_geschaetzt(db, budget):
    """Ein Anbieter ohne Kostenangabe darf nicht mit null gerechnet werden.

    Sonst waere die Bremse ausgerechnet dort blind, wo am wenigsten bekannt ist.
    """
    budget(1.0)
    gebucht = kosten.verbuche(db, provider="openai", kosten_usd=None)
    assert gebucht > 0
    assert kosten.verbraucht_heute(db) == pytest.approx(gebucht)


def test_platzhalter_kostet_nichts(db, budget):
    budget(1.0)
    assert kosten.verbuche(db, provider="mock", kosten_usd=None) == 0.0


def test_gestern_zaehlt_heute_nicht(db, budget):
    """Das Budget gilt je Tag - der gestrige Verbrauch blockiert heute nicht."""
    budget(1.0)
    gestern = date.today() - timedelta(days=1)
    db.add(kosten.StudioCostLog(tag=gestern, provider="openai", kosten_usd=5.0))
    db.commit()

    assert kosten.verbraucht_heute(db) == pytest.approx(0.0)
    kosten.pruefe(db, 0.5)           # wirft nicht


def test_verbrauch_ueberlebt_neustart(db, budget):
    """Der Zaehler steht in der Datenbank, nicht im Arbeitsspeicher.

    Laege er im Speicher, waere die Bremse nach jedem Neustart wieder offen.
    """
    budget(1.0)
    kosten.verbuche(db, provider="openai", kosten_usd=0.4)
    db.expire_all()                  # simuliert eine frische Sitzung
    assert kosten.verbraucht_heute(db) == pytest.approx(0.4)
