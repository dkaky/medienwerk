"""Der Nachtlauf erzeugt Rechnungen aus neu eingesammelten Belegen.

Diese Haelfte der Kette lief bisher nur von Hand. Sie ist rein lokal (der Beleg
liegt schon in der Ablage) und damit unabhaengig davon, WER die Belege eingesammelt
hat — Wajjahat auf dem Mac oder Kaky unter Windows.

Wichtig: ein Fehler hier darf die anderen Punkte des Nachtlaufs (Gebuehren-Sync,
Finanzbericht-Cache) nicht mitreissen.
"""
from __future__ import annotations

import pytest

from app import scheduler as scheduler_module


@pytest.fixture()
def nachtlauf(monkeypatch):
    """Den Nachtlauf ausfuehren und festhalten, was er aufgerufen hat."""
    protokoll = {}

    async def _noop(*a, **k):
        return {}

    # Die uebrigen Punkte stillegen – hier interessiert nur der neue.
    from app.services import ebay_import_service, finance_service, optimization_service
    monkeypatch.setattr(optimization_service, "refresh_click_data", _noop, raising=False)
    monkeypatch.setattr(ebay_import_service, "sync_listing_stats", _noop, raising=False)
    monkeypatch.setattr(finance_service, "sync_ebay_fees", _noop, raising=False)
    monkeypatch.setattr(finance_service, "ebay_finance_report", _noop, raising=False)

    def _setze(ergebnis=None, fehler=None):
        async def _erzeuge(db, *, jahr=None, limit=50):
            protokoll["aufgerufen"] = True
            protokoll["limit"] = limit
            if fehler:
                raise fehler
            return ergebnis or {"erzeugt": 0, "probleme": [], "offen": 0}
        from app.services import purchase_invoice
        monkeypatch.setattr(purchase_invoice, "erzeuge_alle_rechnungen", _erzeuge)
        return protokoll
    return _setze


@pytest.mark.asyncio
async def test_nachtlauf_erzeugt_rechnungen(nachtlauf):
    p = nachtlauf({"erzeugt": 7, "probleme": [], "offen": 3})

    await scheduler_module._daily_performance_job()

    assert p.get("aufgerufen") is True


@pytest.mark.asyncio
async def test_menge_ist_gedeckelt(nachtlauf):
    """Je Beleg eine Bilderkennung -> ohne Deckel laeuft die Nacht in die Kosten."""
    p = nachtlauf()

    await scheduler_module._daily_performance_job()

    assert p["limit"] == 200


@pytest.mark.asyncio
async def test_fehler_reisst_den_nachtlauf_nicht_mit(nachtlauf):
    nachtlauf(fehler=RuntimeError("LLM nicht erreichbar"))

    # Darf NICHT fliegen – der Nachtlauf muss zu Ende laufen.
    await scheduler_module._daily_performance_job()
