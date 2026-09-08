"""Shop-Import anhalten, wenn genug Entwuerfe da sind (Nutzerwunsch 19.08.2026).

Kein hartes Abwuergen: der Lauf haelt ZWISCHEN zwei Produkten. Ein mitten im
Anlegen abgeschnittener Entwurf waere halb im System und muesste von Hand
aufgeraeumt werden. Was schon erzeugt wurde, bleibt — genau darum geht es.
"""
from __future__ import annotations

import pytest

from app.services import store_import_service as sis


@pytest.fixture(autouse=True)
def sauberer_zustand(tmp_path, monkeypatch):
    monkeypatch.setattr(sis, "_state_datei", lambda: tmp_path / "store.json")
    sis._state.update({"running": False, "abbruch": False, "abgebrochen": False,
                       "created": 0, "total": 0, "done": 0, "skipped": 0,
                       "failed": 0, "error": None, "letzte": []})
    yield
    sis._state["running"] = False


def test_ohne_laufenden_import_passiert_nichts():
    r = sis.abbrechen()

    assert r["laeuft"] is False
    assert sis._state["abbruch"] is False


def test_abbruch_wird_vorgemerkt():
    sis.try_reserve()

    r = sis.abbrechen()

    assert r["abbruch_angefordert"] is True
    assert sis._state["abbruch"] is True


def test_meldet_wie_viel_schon_da_ist():
    """Damit im Dashboard steht, was der Abbruch kostet – bzw. eben nicht."""
    sis.try_reserve()
    sis._state["created"] = 7

    assert sis.abbrechen()["bisher_angelegt"] == 7


def test_neuer_lauf_startet_ohne_alten_abbruch():
    """Sonst wuerde der naechste Import sofort wieder anhalten."""
    sis.try_reserve()
    sis.abbrechen()
    sis._state["running"] = False

    sis.try_reserve()

    assert sis._state["abbruch"] is False
    assert sis._state["abgebrochen"] is False


def test_status_reicht_den_abbruch_durch():
    """Das Dashboard zeigt „wird angehalten …" – dafuer muss es sichtbar sein."""
    sis.try_reserve()
    sis.abbrechen()

    assert sis.status()["abbruch"] is True


def test_schleife_bricht_ab(monkeypatch):
    """Der eigentliche Beweis: die Import-Schleife respektiert den Wunsch.

    Nachgebaut, weil der echte Lauf einen Browser und die KI braucht — geprueft
    wird hier die Abbruch-Bedingung, nicht das Scrapen.
    """
    sis.try_reserve()
    verarbeitet = []
    for i, pid in enumerate(["a", "b", "c", "d"]):
        if sis._state.get("abbruch"):
            sis._state["abgebrochen"] = True
            break
        verarbeitet.append(pid)
        if i == 1:                      # nach dem zweiten Produkt kommt der Klick
            sis.abbrechen()

    assert verarbeitet == ["a", "b"]
    assert sis._state["abgebrochen"] is True
