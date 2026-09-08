"""Tests fuer die DSGVO-Anonymisierung bei eBay-Account-Deletion."""
from __future__ import annotations

from app.models import OrderAliexpress, Sale
from app.services.deletion_service import (
    anonymize_buyer,
    extract_deletion_target,
)


def test_extract_deletion_target_reads_username_and_userid():
    payload = {"notification": {"data": {"username": "max_muster", "userId": "U42"}}}
    target = extract_deletion_target(payload)
    assert target == {"username": "max_muster", "user_id": "U42"}


def test_extract_deletion_target_handles_missing_fields():
    assert extract_deletion_target({}) == {"username": None, "user_id": None}
    assert extract_deletion_target({"notification": {}}) == {"username": None, "user_id": None}


def _make_sale(db, *, buyer_name, buyer_email, with_order=True):
    sale = Sale(
        ebay_transaction_id=f"tx-{buyer_name}",
        buyer_name=buyer_name,
        buyer_email=buyer_email,
        delivery_address={"street": "Hauptstr. 1", "city": "Berlin"},
        price_eur=19,
        status="delivered",
    )
    db.add(sale)
    db.flush()
    if with_order:
        order = OrderAliexpress(
            sale_id=sale.id,
            delivery_name=buyer_name,
            delivery_address={"street": "Hauptstr. 1", "city": "Berlin"},
            status="delivered",
        )
        db.add(order)
    db.commit()
    return sale


def test_anonymize_buyer_removes_pii_keeps_financials(db):
    sale = _make_sale(db, buyer_name="max_muster", buyer_email="max@example.com")
    summary = anonymize_buyer(db, username="max_muster", user_id="U1")

    assert summary["sales_anonymized"] == 1
    assert summary["orders_anonymized"] == 1

    db.refresh(sale)
    assert sale.buyer_email is None
    assert sale.delivery_address is None
    assert sale.buyer_name.startswith("[geloescht")
    # Buchhalterisch relevante Felder bleiben erhalten (Aufbewahrungspflicht).
    assert sale.ebay_transaction_id == "tx-max_muster"
    assert float(sale.price_eur) == 19.0

    assert sale.order is not None
    assert sale.order.delivery_address is None
    assert sale.order.delivery_name.startswith("[geloescht")


def test_anonymize_buyer_matches_by_email(db):
    sale = _make_sale(db, buyer_name="ResolvedName", buyer_email="target@example.com")
    summary = anonymize_buyer(db, username="target@example.com", user_id=None)
    assert summary["sales_anonymized"] == 1
    db.refresh(sale)
    assert sale.buyer_email is None


def test_anonymize_buyer_leaves_other_buyers_untouched(db):
    other = _make_sale(db, buyer_name="someone_else", buyer_email="other@example.com")
    anonymize_buyer(db, username="max_muster", user_id="U1")
    db.refresh(other)
    assert other.buyer_email == "other@example.com"
    assert other.delivery_address is not None


def test_anonymize_buyer_without_identifier_is_noop(db):
    sale = _make_sale(db, buyer_name="max_muster", buyer_email="max@example.com")
    summary = anonymize_buyer(db, username=None, user_id=None)
    assert summary["matched"] is False
    assert summary["sales_anonymized"] == 0
    db.refresh(sale)
    assert sale.buyer_email == "max@example.com"
