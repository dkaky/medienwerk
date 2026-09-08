"""Problembelege bleiben auffindbar (Fehlgrund am Beleg statt nur im Lauf-Ergebnis).

Vorher stand der Grund nur in der Antwort von ``erzeuge_alle_rechnungen`` und war
danach weg — der Beleg sah aus wie jeder andere offene. Jetzt: Stempel in
``receipt_data`` (kein DB-Umbau), eigene Filter-Kachel, farbige Zeile.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import Invoice
from app.retry import PersistentError
from app.services import invoice_service, purchase_invoice
from app.services.purchase_invoice import BelegUnklar


def _beleg(db, *, original=True, generated=None):
    inv = Invoice(type="aliexpress_purchase", reference_id="3030303030",
                  invoice_number="AE_3030303030", is_original=original,
                  file_path="data/invoices/original_x.png" if original else None,
                  invoice_date=datetime(2026, 6, 1, tzinfo=timezone.utc),
                  generated_path=generated)
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def test_grund_wird_am_beleg_gespeichert(db):
    inv = _beleg(db)

    purchase_invoice._problem_merken(db, inv.id, "Gesamtbetrag 0.00 passt nicht zu 9.83.")

    db.refresh(inv)
    assert "0.00" in inv.receipt_data["problem"]
    assert inv.receipt_data["problem_am"]


def test_bestehende_belegdaten_bleiben_erhalten(db):
    """Der Stempel darf die ausgelesenen Beleg-Werte nicht ueberschreiben."""
    inv = _beleg(db)
    inv.receipt_data = {"total": 9.83, "geprueft_am": "2026-06-02T10:00:00+00:00"}
    db.commit()

    purchase_invoice._problem_merken(db, inv.id, "unklar")

    db.refresh(inv)
    assert inv.receipt_data["total"] == 9.83
    assert inv.receipt_data["geprueft_am"]
    assert inv.receipt_data["problem"] == "unklar"


def test_erfolg_loescht_den_stempel(db):
    """Sonst bleibt die Zeile rot, obwohl die Rechnung inzwischen da ist."""
    inv = _beleg(db)
    inv.receipt_data = {"total": 9.83, "problem": "alt", "problem_am": "irgendwann"}
    db.commit()

    purchase_invoice._problem_loeschen(db, inv.id)

    db.refresh(inv)
    assert "problem" not in inv.receipt_data
    assert "problem_am" not in inv.receipt_data
    assert inv.receipt_data["total"] == 9.83


def test_liste_liefert_das_problem_aus(db):
    inv = _beleg(db)
    inv.receipt_data = {"problem": "Beleg unklar"}
    db.commit()

    (eintrag,) = [i for i in invoice_service.list_invoices(db)["invoices"]
                  if i["id"] == inv.id]

    assert eintrag["problem"] == "Beleg unklar"


def test_mit_rechnung_kein_problem_mehr(db):
    """Existiert die Rechnung, ist ein alter Stempel Vergangenheit -> nicht anzeigen."""
    inv = _beleg(db, generated="data/invoices/rechnung_x.html")
    inv.receipt_data = {"problem": "alter Fehler"}
    db.commit()

    (eintrag,) = [i for i in invoice_service.list_invoices(db)["invoices"]
                  if i["id"] == inv.id]

    assert eintrag["problem"] is None
    assert invoice_service.invoice_summary(db)["problems"] == 0


def test_zaehler_fuer_die_kachel(db):
    a = _beleg(db)
    a.receipt_data = {"problem": "x"}
    b = _beleg(db)
    b.receipt_data = {"problem": "y"}
    _beleg(db)                      # ohne Problem
    db.commit()

    assert invoice_service.invoice_summary(db)["problems"] == 2


@pytest.mark.asyncio
async def test_lauf_stempelt_und_raeumt_auf(db, monkeypatch):
    """Durchlauf: erst scheitern (Stempel), dann gelingen (Stempel weg)."""
    inv = _beleg(db)

    async def _scheitert(db_, *, invoice_id, force=False):
        raise BelegUnklar("Gesamtbetrag 0.00 passt nicht zum bezahlten Betrag 9.83.")

    monkeypatch.setattr(purchase_invoice, "erzeuge_rechnung", _scheitert)
    r = await purchase_invoice.erzeuge_alle_rechnungen(db, limit=5)

    assert r["probleme"] and r["erzeugt"] == 0
    db.refresh(inv)
    assert inv.receipt_data["problem"]
    assert invoice_service.invoice_summary(db)["problems"] == 1

    async def _klappt(db_, *, invoice_id, force=False):
        return {"invoice_id": invoice_id, "total": 9.83}

    monkeypatch.setattr(purchase_invoice, "erzeuge_rechnung", _klappt)
    await purchase_invoice.erzeuge_alle_rechnungen(db, limit=5)

    db.refresh(inv)
    assert "problem" not in (inv.receipt_data or {})


@pytest.mark.asyncio
async def test_auch_persistent_error_wird_gestempelt(db, monkeypatch):
    inv = _beleg(db)

    async def _kaputt(db_, *, invoice_id, force=False):
        raise PersistentError("Beleg liegt als .pdf vor")

    monkeypatch.setattr(purchase_invoice, "erzeuge_rechnung", _kaputt)
    await purchase_invoice.erzeuge_alle_rechnungen(db, limit=5)

    db.refresh(inv)
    assert ".pdf" in inv.receipt_data["problem"]


def test_stempel_stoppt_den_lauf_nie(db):
    """Auch bei unbekannter Beleg-ID darf nichts fliegen."""
    purchase_invoice._problem_merken(db, 999999, "egal")
    purchase_invoice._problem_loeschen(db, 999999)


def test_kaputte_datei_wird_verstaendlich(db):
    """Im Tooltip stand sonst der rohe Python-Fehler — daraus liest niemand ab, was zu tun ist."""
    inv = _beleg(db)

    purchase_invoice._problem_merken(db, inv.id,
        "Beleg nicht auslesbar: BadRequestError: Error code: 400 - {'type': 'error', "
        "'error': {'type': 'invalid_request_error', 'message': 'Could not process image'}}")

    db.refresh(inv)
    assert inv.receipt_data["problem"] == (
        "Die Beleg-Datei ist beschädigt und lässt sich nicht öffnen. "
        "Beleg bei AliExpress neu holen und hochladen.")
    assert "BadRequestError" not in inv.receipt_data["problem"]
    # Original bleibt fuer die Fehlersuche erhalten
    assert "BadRequestError" in inv.receipt_data["problem_technisch"]


def test_verstaendliche_gruende_bleiben_wie_sie_sind(db):
    inv = _beleg(db)
    grund = "Beleg gehoert zu Bestellung 3073777373502059, erwartet 3073777737502059."

    purchase_invoice._problem_merken(db, inv.id, grund)

    db.refresh(inv)
    assert inv.receipt_data["problem"] == grund
    assert "problem_technisch" not in inv.receipt_data


def test_ausgelastet_sagt_dass_es_von_allein_weitergeht(db):
    inv = _beleg(db)

    purchase_invoice._problem_merken(db, inv.id, "Beleg nicht auslesbar: Overloaded")

    db.refresh(inv)
    assert "ausgelastet" in inv.receipt_data["problem"]
    assert "erneut" in inv.receipt_data["problem"]


def test_erfolg_raeumt_auch_den_technischen_text_weg(db):
    inv = _beleg(db)
    inv.receipt_data = {"total": 5.0, "problem": "x", "problem_am": "y",
                        "problem_technisch": "z"}
    db.commit()

    purchase_invoice._problem_loeschen(db, inv.id)

    db.refresh(inv)
    assert set(inv.receipt_data) == {"total"}
