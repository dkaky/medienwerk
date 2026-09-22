"""Die selbst gebaute Verkaufsrechnung wird durch die ECHTE eBay-Rechnung (PDF) ersetzt.

Nutzt eine Attrappe statt eines echten Browsers/eBay-Zugriffs (app.studio.ebay_rechnungen
wird gepatcht) - das echte Zusammenspiel ist von Hand gegen den Live-Account geprueft.
"""
from __future__ import annotations

import asyncio

import pytest

from app.database import SessionLocal
from app.models import Invoice
from app.services import invoice_service
from app.studio.bestellimport import uebernehme
from app.studio.models import PodOrder

_MINI_PDF = b"%PDF-1.4\n%mini\n"


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.close()


def _roh_bestellung(order_id):
    return {
        "orderId": order_id,
        "creationDate": "2026-09-20T11:31:25.000Z",
        "orderPaymentStatus": "PAID",
        "orderFulfillmentStatus": "NOT_STARTED",
        "pricingSummary": {"total": {"value": "19.90"}},
        "lineItems": [{"title": "T-Shirt Original-Test", "quantity": 1,
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


def test_ersetzt_die_ersatzrechnung_durch_das_echte_pdf(db, monkeypatch):
    order_id = "20-ORIG-001"
    try:
        uebernehme(db, [_roh_bestellung(order_id)])
        zeile = db.query(PodOrder).filter_by(channel="ebay", external_id=order_id).first()
        erst = invoice_service.generate_pod_sale_invoice(db, order_id=zeile.id)
        assert erst["created"] is True

        async def fake_rechnungen_pdf(nummern, **kw):
            assert nummern == [order_id]
            return {order_id: _MINI_PDF}

        from app.studio import ebay_rechnungen
        monkeypatch.setattr(ebay_rechnungen, "rechnungen_pdf", fake_rechnungen_pdf)

        ergebnis = asyncio.run(invoice_service.hole_original_pod_rechnungen(db))
        assert ergebnis == {"geholt": 1, "fehlgeschlagen": 0, "fehler": []}

        inv = db.query(Invoice).filter_by(type="pod_sales", reference_id=order_id).one()
        assert inv.is_original is True
        assert inv.file_path.endswith(".pdf")
        daten, name, ctype = invoice_service.read_invoice_file(db, invoice_id=inv.id)
        assert daten == _MINI_PDF
        assert ctype == "application/pdf"
    finally:
        _aufraeumen(db, order_id)


def test_ist_idempotent_kein_zweiter_abruf(db, monkeypatch):
    order_id = "20-ORIG-002"
    try:
        uebernehme(db, [_roh_bestellung(order_id)])
        zeile = db.query(PodOrder).filter_by(channel="ebay", external_id=order_id).first()
        invoice_service.generate_pod_sale_invoice(db, order_id=zeile.id)

        rufe = []

        async def fake_rechnungen_pdf(nummern, **kw):
            rufe.append(nummern)
            return {order_id: _MINI_PDF}

        from app.studio import ebay_rechnungen
        monkeypatch.setattr(ebay_rechnungen, "rechnungen_pdf", fake_rechnungen_pdf)

        asyncio.run(invoice_service.hole_original_pod_rechnungen(db))
        asyncio.run(invoice_service.hole_original_pod_rechnungen(db))
        assert len(rufe) == 1  # zweiter Lauf hat schon nichts mehr offen -> kein Browser-Start
    finally:
        _aufraeumen(db, order_id)


def test_fehlschlag_laesst_die_ersatzrechnung_unangetastet(db, monkeypatch):
    order_id = "20-ORIG-003"
    try:
        uebernehme(db, [_roh_bestellung(order_id)])
        zeile = db.query(PodOrder).filter_by(channel="ebay", external_id=order_id).first()
        erst = invoice_service.generate_pod_sale_invoice(db, order_id=zeile.id)
        alter_pfad = erst["id"]

        async def fake_rechnungen_pdf(nummern, **kw):
            from app.studio.ebay_rechnungen import RechnungFehler
            return {order_id: RechnungFehler("nicht angemeldet")}

        from app.studio import ebay_rechnungen
        monkeypatch.setattr(ebay_rechnungen, "rechnungen_pdf", fake_rechnungen_pdf)

        ergebnis = asyncio.run(invoice_service.hole_original_pod_rechnungen(db))
        assert ergebnis["geholt"] == 0
        assert ergebnis["fehlgeschlagen"] == 1
        inv = db.query(Invoice).filter_by(type="pod_sales", reference_id=order_id).one()
        assert inv.is_original is False
        assert inv.id == alter_pfad  # die Ersatzrechnung ist noch da, unveraendert
    finally:
        _aufraeumen(db, order_id)


def test_ohne_faellige_bestellungen_wird_kein_browser_gestartet(db, monkeypatch):
    aufgerufen = False

    async def fake_rechnungen_pdf(nummern, **kw):
        nonlocal aufgerufen
        aufgerufen = True
        return {}

    from app.studio import ebay_rechnungen
    monkeypatch.setattr(ebay_rechnungen, "rechnungen_pdf", fake_rechnungen_pdf)

    # Es gibt evtl. andere pod_sales ohne Original in der echten DB (z. B. das Live-Konto) -
    # hier zaehlt nur: wenn es NICHTS Faelliges gibt, wird rechnungen_pdf nicht gerufen.
    vorhandene = db.query(Invoice).filter_by(type="pod_sales", is_original=False).count()
    if vorhandene == 0:
        asyncio.run(invoice_service.hole_original_pod_rechnungen(db))
        assert aufgerufen is False
