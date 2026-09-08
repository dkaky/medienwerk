"""Stornierten Wareneinkauf loeschen + Problem-Stempel nach Nachreichen.

Zwei Faelle aus der Praxis (18.08.2026):

1. Der Kunde storniert, wir stornieren daraufhin den AliExpress-Kauf. Dann darf der
   Einkauf weder in der Belegablage stehen noch als Ausgabe zaehlen. Ein blosses
   Loeschen der Zeile HAELT NICHT: ``record_purchase_invoice`` erkennt die Bestellung
   an der AliExpress-Nummer und legt den Beleg beim naechsten Abgleich wieder an.
   Deshalb wird die Bestellung mit storniert.

2. Ein Beleg lag unter „Problemfaelle", er wurde von Hand nachgereicht — und
   er blieb trotzdem rot stehen. Der Fehlstempel galt der ALTEN Datei.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.models import Invoice, OrderAliexpress
from app.retry import PersistentError
from app.services import invoice_service

_lfd = iter(range(1, 10_000))


def _kauf(db, *, betrag="12.50", status="shipped"):
    ref = f"307{next(_lfd):013d}"
    o = OrderAliexpress(aliexpress_order_id=ref, status=status,
                        cost_cny=Decimal(betrag),
                        order_date=datetime.now(timezone.utc))
    db.add(o)
    db.commit()
    db.refresh(o)
    inv = Invoice(type="aliexpress_purchase", reference_id=ref,
                  invoice_number=f"AE_{ref}", invoice_date=datetime.now(timezone.utc),
                  amount=Decimal(betrag), currency="EUR", order_id=o.id,
                  is_original=True, file_path=f"invoices/2026-08/original_{ref}.png")
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return o, inv


# ------------------------------------------------------------------ 1) Loeschen
def test_zeile_ist_weg(db):
    o, inv = _kauf(db)

    invoice_service.delete_purchase_invoice(db, invoice_id=inv.id)

    assert db.get(Invoice, inv.id) is None


def test_bestellung_ist_komplett_weg(db):
    """Nutzer-Vorgabe 18.08.: geloescht heisst geloescht, nicht ausgeblendet."""
    o, inv = _kauf(db)
    oid = o.id

    invoice_service.delete_purchase_invoice(db, invoice_id=inv.id)

    assert db.get(OrderAliexpress, oid) is None


def test_beleg_kommt_nicht_zurueck(db):
    """Der eigentliche Beweis: nach dem Loeschen den Abgleich laufen lassen."""
    o, inv = _kauf(db)
    ref = str(o.aliexpress_order_id)
    invoice_service.delete_purchase_invoice(db, invoice_id=inv.id)

    # Die Bestellung gibt es nicht mehr – also kann auch kein Beleg entstehen.
    assert db.scalar(
        invoice_service.select(Invoice).where(
            Invoice.reference_id == ref)) is None


def test_faellt_aus_der_beleg_arbeitsliste(db):
    o, inv = _kauf(db)
    ref = str(o.aliexpress_order_id)
    invoice_service.delete_purchase_invoice(db, invoice_id=inv.id)

    todo = invoice_service.list_originals_todo(db)

    assert ref not in {t["aliexpress_order_id"] for t in todo["aliexpress"]}


def test_scraper_legt_sie_nicht_wieder_an(db):
    """discover_aliexpress_orders sieht die Nummer in der AliExpress-Liste weiter –
    ohne Sperre kaeme der geloeschte Kauf beim naechsten Sammellauf zurueck."""
    o, inv = _kauf(db)
    ref = str(o.aliexpress_order_id)
    invoice_service.delete_purchase_invoice(db, invoice_id=inv.id)

    invoice_service.discover_aliexpress_orders(
        db, [{"aliexpress_order_id": ref, "order_date": "2026-08-01T00:00:00+00:00"}])

    assert db.scalar(invoice_service.select(OrderAliexpress).where(
        OrderAliexpress.aliexpress_order_id == ref)) is None


def test_kontobuchung_wird_geloest_und_wieder_offen(db):
    """Sonst zeigt sie ins Leere und gilt trotzdem als erledigt."""
    from app.models import BankTransaction
    o, inv = _kauf(db)
    tx = BankTransaction(amount=Decimal("-12.50"), status="matched",
                         order_id=o.id, invoices=[inv.id])
    db.add(tx)
    db.commit()

    invoice_service.delete_purchase_invoice(db, invoice_id=inv.id)

    db.refresh(tx)
    assert tx.order_id is None
    assert tx.invoices == []
    assert tx.status == "pending"


def test_verkaufsrechnung_laesst_sich_so_nicht_loeschen(db):
    """Nur Wareneinkaeufe – eine Einnahme verschwindet nicht per Knopfdruck."""
    inv = Invoice(type="ebay_sales", reference_id="X1", invoice_number="R-1",
                  invoice_date=datetime.now(timezone.utc), amount=Decimal("9.99"))
    db.add(inv)
    db.commit()

    with pytest.raises(PersistentError):
        invoice_service.delete_purchase_invoice(db, invoice_id=inv.id)


def test_loeschen_wird_protokolliert(db):
    """GoBD: was entfernt wurde, muss nachvollziehbar bleiben."""
    from app.models import TaskLog
    o, inv = _kauf(db, betrag="33.00")
    nummer = inv.invoice_number

    invoice_service.delete_purchase_invoice(db, invoice_id=inv.id)

    logs = db.scalars(
        invoice_service.select(TaskLog).where(TaskLog.task_type == "beleg_geloescht")).all()
    assert len(logs) == 1
    assert logs[0].result_data["invoice_number"] == nummer
    assert logs[0].result_data["amount"] == 33.0


# --------------------------------------------------- 2) Problem-Stempel loeschen
def test_nachgereichter_beleg_raeumt_den_problem_stempel(db):
    o, inv = _kauf(db)
    inv.receipt_data = {"problem": "Die Beleg-Datei ist beschädigt …",
                        "problem_am": "2026-08-16T00:00:00+00:00",
                        "problem_technisch": "BadRequestError …"}
    db.commit()

    invoice_service.attach_original_receipt(
        db, file_bytes=b"neuer beleg", ext="png",
        invoice_type="aliexpress_purchase", order_id=o.id)

    db.refresh(inv)
    assert not (inv.receipt_data or {}).get("problem")


def test_faellt_damit_aus_der_problem_kachel(db):
    o, inv = _kauf(db)
    inv.receipt_data = {"problem": "kaputt"}
    db.commit()
    assert invoice_service.invoice_summary(db)["problems"] == 1

    invoice_service.attach_original_receipt(
        db, file_bytes=b"neuer beleg", ext="png",
        invoice_type="aliexpress_purchase", order_id=o.id)

    assert invoice_service.invoice_summary(db)["problems"] == 0


def test_andere_beleg_daten_bleiben_erhalten(db):
    """Nur der Fehlstempel geht – die ausgelesenen Werte nicht."""
    o, inv = _kauf(db)
    inv.receipt_data = {"total": 12.5, "problem": "kaputt", "problem_am": "x"}
    db.commit()

    invoice_service.attach_original_receipt(
        db, file_bytes=b"neuer beleg", ext="png",
        invoice_type="aliexpress_purchase", order_id=o.id)

    db.refresh(inv)
    assert inv.receipt_data == {"total": 12.5}


@pytest.mark.asyncio
async def test_erzeugte_rechnung_raeumt_den_stempel(db, tmp_path):
    """Auch ueber den Einzel-Knopf „Rechnung erstellen" – nicht nur im Sammellauf.

    Laeuft gegen die ECHTE Erzeugung (Mock-LLM aus conftest), nicht gegen eine
    Attrappe – sonst wuerde der Test nur sich selbst bestaetigen.
    """
    from app.services import purchase_invoice as pi

    ref = "3075412823902059"          # die Nummer, die der Mock-Beleg traegt
    order = OrderAliexpress(aliexpress_order_id=ref, quantity=1, status="delivered")
    db.add(order)
    db.flush()
    datei = tmp_path / f"original_aliexpress_purchase_{ref}_abc.png"
    datei.write_bytes(b"\x89PNG\r\n\x1a\n-beleg-bild")
    inv = Invoice(type="aliexpress_purchase", reference_id=ref, invoice_number=f"AE_{ref}",
                  amount=Decimal("9.99"), currency="EUR", file_path=str(datei),
                  file_hash="hash-original", is_original=True, order_id=order.id,
                  receipt_data={"problem": "war mal kaputt", "problem_am": "x"})
    db.add(inv)
    db.commit()
    assert invoice_service.invoice_summary(db)["problems"] == 1

    r = await pi.erzeuge_rechnung(db, invoice_id=inv.id)

    assert r["created"] is True
    db.refresh(inv)
    assert not (inv.receipt_data or {}).get("problem")
    assert invoice_service.invoice_summary(db)["problems"] == 0
