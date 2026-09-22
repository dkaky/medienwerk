"""Kostenbelege fuer eBay-Gebuehren - selbst erzeugte Ersatzbelege, KEINE eBay-Originale.

Ein Beleg je Gebuehrentransaktion aus der Finances API (Verkaufsgebuehr je Bestellung
oder kontobezogene Gebuehr wie Shop-Abo), idempotent ueber die eBay-Transaktions-ID.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import Invoice
from app.services import invoice_service

JAHR = 2031  # unbenutztes Testjahr, damit echte Daten nie beruehrt werden


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture
def transaktionsdatei(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    yield tmp_path / "data" / f"finance_transactions_{JAHR}.json"


def _schreibe(pfad, txs):
    pfad.write_text(json.dumps(txs, ensure_ascii=False), encoding="utf-8")


def _aufraeumen(db):
    db.query(Invoice).filter_by(type="ebay_kosten").filter(
        Invoice.reference_id.like("TX-%")).delete(synchronize_session=False)
    db.commit()


def test_ohne_gecachte_transaktionen_passiert_nichts(db, transaktionsdatei):
    ergebnis = invoice_service.generate_missing_ebay_kosten_belege(db, year=JAHR)
    assert ergebnis == {"erzeugt": 0, "uebersprungen": 0, "fehler": 0}


def test_verkaufsgebuehr_und_kontobezogene_gebuehr_bekommen_je_einen_beleg(db, transaktionsdatei):
    _schreibe(transaktionsdatei, [
        {"transactionId": "TX-1", "transactionType": "SALE", "transactionDate": "2031-03-01T10:00:00.000Z",
         "amount": {"value": "17.75"}, "totalFeeAmount": {"value": "2.15"},
         "references": [{"referenceType": "ORDER_ID", "referenceId": "20-AAA"}]},
        {"transactionId": "TX-2", "transactionType": "NON_SALE_CHARGE", "transactionDate": "2031-03-05T09:00:00.000Z",
         "amount": {"value": "-9.90"}, "feeType": "Shop-Abo"},
        {"transactionId": "TX-3", "transactionType": "SHIPPING_LABEL", "transactionDate": "2031-03-06T09:00:00.000Z",
         "amount": {"value": "-4.50"}},
        {"transactionId": "TX-4", "transactionType": "REFUND", "transactionDate": "2031-03-07T09:00:00.000Z",
         "amount": {"value": "-14.90"}},
    ])
    try:
        erg = invoice_service.generate_missing_ebay_kosten_belege(db, year=JAHR)
        assert erg == {"erzeugt": 2, "uebersprungen": 0, "fehler": 0}
        belege = db.query(Invoice).filter(
            Invoice.type == "ebay_kosten", Invoice.reference_id.in_(["TX-1", "TX-2"])).all()
        assert {float(b.amount) for b in belege} == {2.15, 9.90}
        verkauf = next(b for b in belege if b.reference_id == "TX-1")
        assert "20-AAA" in (verkauf.note or "")
        assert verkauf.invoice_number.startswith("K-")
        # Versandlabel und Erstattung sind keine Gebuehren -> kein Beleg dafuer.
        assert db.query(Invoice).filter_by(type="ebay_kosten", reference_id="TX-3").count() == 0
        assert db.query(Invoice).filter_by(type="ebay_kosten", reference_id="TX-4").count() == 0
    finally:
        _aufraeumen(db)


def test_ist_idempotent(db, transaktionsdatei):
    _schreibe(transaktionsdatei, [
        {"transactionId": "TX-1", "transactionType": "SALE", "transactionDate": "2031-03-01T10:00:00.000Z",
         "amount": {"value": "17.75"}, "totalFeeAmount": {"value": "2.15"}},
    ])
    try:
        erst = invoice_service.generate_missing_ebay_kosten_belege(db, year=JAHR)
        assert erst["erzeugt"] == 1
        zweit = invoice_service.generate_missing_ebay_kosten_belege(db, year=JAHR)
        assert zweit == {"erzeugt": 0, "uebersprungen": 1, "fehler": 0}
        assert db.query(Invoice).filter_by(type="ebay_kosten", reference_id="TX-1").count() == 1
    finally:
        _aufraeumen(db)


def test_gebuehr_ohne_betrag_wird_uebersprungen(db, transaktionsdatei):
    _schreibe(transaktionsdatei, [
        {"transactionId": "TX-1", "transactionType": "SALE", "transactionDate": "2031-03-01T10:00:00.000Z",
         "amount": {"value": "17.75"}, "totalFeeAmount": {"value": "0.00"}},
    ])
    erg = invoice_service.generate_missing_ebay_kosten_belege(db, year=JAHR)
    assert erg["erzeugt"] == 0


def test_beleg_ist_als_ersatzbeleg_gekennzeichnet_und_wirkt_als_ausgabe(db, transaktionsdatei):
    _schreibe(transaktionsdatei, [
        {"transactionId": "TX-1", "transactionType": "SALE", "transactionDate": "2031-03-01T10:00:00.000Z",
         "amount": {"value": "17.75"}, "totalFeeAmount": {"value": "2.15"}},
    ])
    try:
        invoice_service.generate_missing_ebay_kosten_belege(db, year=JAHR)
        alle = invoice_service.list_invoices(db, type="all")
        zeile = next(i for i in alle["invoices"] if i["reference_id"] == "TX-1")
        assert zeile["richtung"] == "ausgabe"
        inv_id = zeile["id"]
        daten, name, ctype = invoice_service.read_invoice_file(db, invoice_id=inv_id)
        assert b"KEINE eBay-Originalrechnung" in daten
    finally:
        _aufraeumen(db)
