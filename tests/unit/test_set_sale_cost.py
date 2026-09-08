"""Tests fuer den manuellen EK-Eintrag (Doppelklick auf EK in den Verkaeufen).

set_sale_actual_cost setzt den ECHTEN Einkaufspreis (EUR) am Verkauf, ohne Beleg-Datei —
identisch zum Beleg-Upload, nur ohne Datei/Invoice. Der Orders-Gewinn wird damit real."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Listing, OrderAliexpress, Product, Sale
from app.retry import PersistentError
from app.services import analytics_service, order_service


def _mk_sale(db, *, with_listing=True):
    l = None
    if with_listing:
        p = Product(aliexpress_url="https://de.aliexpress.com/item/ek.html", aliexpress_id="ek1",
                    variants={"skus": [{"id": "S", "attr": "1:1#one", "price": "5.0",
                                        "options": {"Farbe": "one"}}]})
        db.add(p); db.flush()
        l = Listing(product_id=p.id, title_seo="Artikel", description="d", listing_status="active",
                    price_eur=Decimal("19.95"), cost_eur=Decimal("7.00"))
        db.add(l); db.flush()
    s = Sale(ebay_transaction_id="ek-a", listing_id=(l.id if l else None), status="delivered",
             quantity=1, price_eur=Decimal("19.95"), fee_eur_actual=Decimal("3.00"))
    db.add(s); db.commit()
    return s, l


def test_set_cost_creates_order_and_sets_actual_ek(db):
    s, l = _mk_sale(db)
    res = order_service.set_sale_actual_cost(db, sale_id=s.id, amount_eur=10.48)
    assert res["cost_eur"] == 10.48 and res["cost_is_actual"] is True
    order = db.scalar(
        select(OrderAliexpress).where(OrderAliexpress.sale_id == s.id))
    assert order is not None and float(order.cost_cny) == 10.48
    # Economics sehen den echten EK jetzt als 'actual'
    eco = analytics_service._order_economics(s, order, l, None, fee_pct=0.22, fixed_fee=0.45)
    assert eco["cost"] == 10.48 and eco["cost_is_actual"] is True


def test_set_cost_updates_existing_order(db):
    s, l = _mk_sale(db)
    order = OrderAliexpress(sale_id=s.id, quantity=1, status="ordered", cost_cny=Decimal("99.00"))
    db.add(order); db.commit()
    order_service.set_sale_actual_cost(db, sale_id=s.id, amount_eur="8,90")  # Komma toleriert
    db.refresh(order)
    assert float(order.cost_cny) == 8.90
    # KEINE zweite Order angelegt
    orders = db.scalars(
        select(OrderAliexpress).where(OrderAliexpress.sale_id == s.id)).all()
    assert len(orders) == 1


def test_set_cost_rejects_non_positive(db):
    s, _ = _mk_sale(db)
    for bad in (0, -5, "abc", None):
        with pytest.raises(PersistentError):
            order_service.set_sale_actual_cost(db, sale_id=s.id, amount_eur=bad)


def test_set_cost_unknown_sale_raises(db):
    with pytest.raises(PersistentError):
        order_service.set_sale_actual_cost(db, sale_id=999999, amount_eur=5.0)


def test_set_cost_works_without_listing(db):
    """Manuell (ausserhalb der App) bestellter Verkauf ohne Listing -> trotzdem EK setzbar."""
    s, _ = _mk_sale(db, with_listing=False)
    res = order_service.set_sale_actual_cost(db, sale_id=s.id, amount_eur=4.20)
    assert res["cost_eur"] == 4.20


def test_set_cost_on_open_sale_without_order_is_rejected(db):
    """GELD-SICHERHEIT: EK auf einem noch OFFENEN Verkauf ohne Bestellung darf KEINEN
    Phantom-Bestellsatz anlegen (der wuerde das Fulfillment dauerhaft blockieren)."""
    s, _ = _mk_sale(db)
    s.status = "pending"
    db.commit()
    with pytest.raises(PersistentError):
        order_service.set_sale_actual_cost(db, sale_id=s.id, amount_eur=10.48)
    # KEIN Bestell-Datensatz angelegt -> Fulfillment bleibt moeglich
    assert db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == s.id)) is None


def test_set_cost_on_open_sale_with_existing_order_updates(db):
    """Offener Verkauf, aber Bestellung existiert bereits (z.B. Claim) -> EK darf korrigiert
    werden; es wird NUR aktualisiert, kein Phantom angelegt."""
    s, _ = _mk_sale(db)
    s.status = "needs_manual_review"
    order = OrderAliexpress(sale_id=s.id, quantity=1, status="ordered",
                            aliexpress_order_id="AE123", cost_cny=Decimal("9.00"))
    db.add(order); db.commit()
    order_service.set_sale_actual_cost(db, sale_id=s.id, amount_eur=7.50)
    db.refresh(order)
    assert float(order.cost_cny) == 7.50
