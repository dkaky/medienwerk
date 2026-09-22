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


def test_download_mit_drucken_fuegt_autoprint_skript_ein(db, client):
    order_id = "20-TEST-001"
    try:
        uebernehme(db, [_roh_bestellung(order_id)])
        zeile = db.query(PodOrder).filter_by(channel="ebay", external_id=order_id).first()
        inv = invoice_service.generate_pod_sale_invoice(db, order_id=zeile.id)
        normal = client.get(f"/api/v1/invoices/{inv['id']}/download")
        assert "window.print()" not in normal.text
        gedruckt = client.get(f"/api/v1/invoices/{inv['id']}/download?drucken=true")
        assert "window.print()" in gedruckt.text
        assert "Rechnung" in gedruckt.text  # der eigentliche Beleg bleibt drin
    finally:
        _aufraeumen(db, order_id)


def test_mehrere_drucken_fasst_ausgewaehlte_html_belege_zusammen(db, client):
    order_a, order_b = "20-TEST-001", "20-TEST-002"
    try:
        uebernehme(db, [_roh_bestellung(order_a), _roh_bestellung(order_b)])
        za = db.query(PodOrder).filter_by(channel="ebay", external_id=order_a).first()
        zb = db.query(PodOrder).filter_by(channel="ebay", external_id=order_b).first()
        ia = invoice_service.generate_pod_sale_invoice(db, order_id=za.id)
        ib = invoice_service.generate_pod_sale_invoice(db, order_id=zb.id)
        r = client.get(f"/api/v1/invoices/drucken?ids={ia['id']},{ib['id']}")
        assert r.status_code == 200
        assert r.text.count("page-break-after") == 2
        assert "window.print()" in r.text
        assert order_a in r.text and order_b in r.text
    finally:
        _aufraeumen(db, order_a)
        _aufraeumen(db, order_b)


def test_mehrere_drucken_ohne_treffer_meldet_klaren_fehler(client):
    r = client.get("/api/v1/invoices/drucken?ids=999999999")
    assert r.status_code == 404


def test_verkaufsrechnung_steht_zusammen_mit_den_belegen_in_einer_liste(db):
    order_id = "20-TEST-001"
    try:
        uebernehme(db, [_roh_bestellung(order_id)])
        zeile = db.query(PodOrder).filter_by(channel="ebay", external_id=order_id).first()
        invoice_service.generate_pod_sale_invoice(db, order_id=zeile.id)
        alle = invoice_service.list_invoices(db, type="all")
        treffer = [i for i in alle["invoices"] if i["reference_id"] == order_id]
        assert len(treffer) == 1
        assert treffer[0]["type"] == "pod_sales"
        assert treffer[0]["richtung"] == "einnahme"
        nur_verkauf = invoice_service.list_invoices(db, type="pod_sales")
        assert any(i["reference_id"] == order_id for i in nur_verkauf["invoices"])
    finally:
        _aufraeumen(db, order_id)
