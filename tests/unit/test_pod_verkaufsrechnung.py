"""Automatische Verkaufsrechnungen fuer eigene, bei eBay verkaufte Motive (Studio/POD).

Getrennt von der Belegablage (die zeigt nur Ausgaben) und von den alten
Dropshipping-"Sale"-Rechnungen (ebay_sales) - eigener Typ "pod_sales".
"""
from __future__ import annotations

import json

import pytest

from app.database import SessionLocal
from app.models import Invoice
from app.services import invoice_service
from app.studio.bestellimport import _kaeufer, uebernehme
from app.studio.models import PodOrder


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.close()


def _roh_bestellung(order_id="20-TEST-001", bezahlt=True):
    return {
        "orderId": order_id,
        "creationDate": "2026-09-20T11:31:25.000Z",
        "orderPaymentStatus": "PAID" if bezahlt else "PENDING",
        "orderFulfillmentStatus": "NOT_STARTED",
        "pricingSummary": {"total": {"value": "19.90"}},
        "lineItems": [{"title": "T-Shirt Erst Kaffee Dann reden", "quantity": 1,
                       "sku": "MW-35-tshirt-White-M", "legacyItemId": "298683500075"}],
        "fulfillmentStartInstructions": [{"shippingStep": {"shipTo": {
            "fullName": "Erika Musterfrau",
            "contactAddress": {"addressLine1": "Musterstr. 1", "postalCode": "12345",
                               "city": "Musterstadt", "countryCode": "DE"},
            "email": "erika@example.com",
        }}}],
    }


def _aufraeumen(db, order_id):
    db.query(Invoice).filter_by(type="pod_sales", reference_id=order_id).delete()
    db.query(PodOrder).filter_by(channel="ebay", external_id=order_id).delete()
    db.commit()


def test_kaeufer_wird_aus_der_rohen_bestellung_gelesen():
    k = _kaeufer(_roh_bestellung())
    assert k["name"] == "Erika Musterfrau"
    assert k["plz"] == "12345" and k["ort"] == "Musterstadt"
    assert k["email"] == "erika@example.com"


def test_uebernehme_speichert_kaeufer_und_positionen(db):
    order_id = "20-TEST-001"
    try:
        uebernehme(db, [_roh_bestellung(order_id)])
        zeile = db.query(PodOrder).filter_by(channel="ebay", external_id=order_id).first()
        assert zeile is not None
        kaeufer = json.loads(zeile.kaeufer_json)
        assert kaeufer["name"] == "Erika Musterfrau"
        positionen = json.loads(zeile.positionen_json)
        assert positionen[0]["titel"] == "T-Shirt Erst Kaffee Dann reden"
    finally:
        _aufraeumen(db, order_id)


def test_rechnung_entsteht_automatisch_und_ist_idempotent(db):
    order_id = "20-TEST-001"
    try:
        uebernehme(db, [_roh_bestellung(order_id)])
        zeile = db.query(PodOrder).filter_by(channel="ebay", external_id=order_id).first()
        erst = invoice_service.generate_pod_sale_invoice(db, order_id=zeile.id)
        assert erst["created"] is True
        assert erst["type"] == "pod_sales"
        zweit = invoice_service.generate_pod_sale_invoice(db, order_id=zeile.id)
        assert zweit["created"] is False
        assert zweit["id"] == erst["id"]
        inv = db.get(Invoice, erst["id"])
        assert float(inv.amount) == 19.90
        assert inv.file_path and "erika" not in inv.file_path.lower()  # kein Name im Dateipfad
    finally:
        _aufraeumen(db, order_id)


def test_stornierte_und_wartende_bestellungen_bekommen_keine_rechnung(db):
    order_id = "20-TEST-001"
    try:
        roh = _roh_bestellung(order_id, bezahlt=False)
        uebernehme(db, [roh])
        zeile = db.query(PodOrder).filter_by(channel="ebay", external_id=order_id).first()
        assert zeile.status == "pending"
        ergebnis = invoice_service.generate_missing_pod_sale_invoices(db)
        assert ergebnis["erzeugt"] == 0
        assert db.query(Invoice).filter_by(type="pod_sales", reference_id=order_id).count() == 0
    finally:
        _aufraeumen(db, order_id)


def test_nachtragen_erzeugt_fuer_alle_faelligen_bestellungen_genau_eine_rechnung(db):
    order_id = "20-TEST-001"
    try:
        uebernehme(db, [_roh_bestellung(order_id)])
        erg1 = invoice_service.generate_missing_pod_sale_invoices(db)
        assert erg1["erzeugt"] == 1
        erg2 = invoice_service.generate_missing_pod_sale_invoices(db)
        assert erg2["erzeugt"] == 0
        assert erg2["uebersprungen"] >= 1
    finally:
        _aufraeumen(db, order_id)


def test_liste_zeigt_nur_pod_sales_und_belege_zeigt_sie_nicht(db):
    order_id = "20-TEST-001"
    try:
        uebernehme(db, [_roh_bestellung(order_id)])
        zeile = db.query(PodOrder).filter_by(channel="ebay", external_id=order_id).first()
        invoice_service.generate_pod_sale_invoice(db, order_id=zeile.id)
        liste = invoice_service.list_pod_sale_invoices(db)
        assert liste["anzahl"] >= 1
        assert any(i["reference_id"] == order_id for i in liste["invoices"])
        belege = invoice_service.list_invoices(db, type="all")
        assert all(i.get("reference_id") != order_id or i.get("type") != "pod_sales"
                   for i in belege["invoices"])
    finally:
        _aufraeumen(db, order_id)
