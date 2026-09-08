"""Tests: ECHTE AliExpress-Order-Summe als EK (statt Schaetzung).

Vorfall #1142/#1145: ds.order.create liefert keine Betraege -> Schaetzung wurde als
'echter' EK gespeichert; real kamen Versand + versteckte Steuer dazu (8,88 € vs. 11,72 €).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from app.services import order_service


class _AeOk:
    async def get_order_detail(self, oid):
        return {"total": 13.37, "product": 7.38, "shipping": 1.99, "tax": 5.49,
                "currency": "USD", "order_status": "PLACE_ORDER_SUCCESS"}


class _AeEur:
    async def get_order_detail(self, oid):
        return {"total": 11.72, "currency": "EUR", "order_status": "FINISH"}


class _AeZero:
    async def get_order_detail(self, oid):
        return {"total": 0, "currency": "USD", "order_status": "FINISH"}


class _AeCancelled:
    async def get_order_detail(self, oid):
        return {"total": 13.37, "currency": "USD", "order_status": "IN_CANCEL"}


class _AeUnpaid:
    async def get_order_detail(self, oid):
        return {"total": 13.37, "currency": "USD", "order_status": "WAIT_BUYER_PAY"}


class _AeBoom:
    async def get_order_detail(self, oid):
        raise RuntimeError("API down")


class _AeUnknownCur:
    async def get_order_detail(self, oid):
        return {"total": 99.0, "currency": "CNY", "order_status": "FINISH"}


def test_real_cost_usd_converted_with_config_rate():
    cost = asyncio.run(order_service._real_order_cost_eur(_AeOk(), "307531"))
    # 13.37 USD * 0.8765 = 11.72 EUR (der real bezahlte Betrag aus dem Vorfall)
    assert cost == Decimal("11.72")


def test_real_cost_eur_passthrough():
    assert asyncio.run(order_service._real_order_cost_eur(_AeEur(), "1")) == Decimal("11.72")


def test_real_cost_zero_total_returns_none():
    assert asyncio.run(order_service._real_order_cost_eur(_AeZero(), "1")) is None


def test_real_cost_api_error_returns_none_not_raise():
    assert asyncio.run(order_service._real_order_cost_eur(_AeBoom(), "1")) is None


def test_real_cost_unknown_currency_returns_none():
    """Unbekannte Waehrung NIE raten (Geld-Zahl) -> None -> Schaetzung uebernimmt."""
    assert asyncio.run(order_service._real_order_cost_eur(_AeUnknownCur(), "1")) is None


def test_real_cost_client_without_method_returns_none():
    """Mock-Client ohne get_order_detail (z.B. Tests/Dev) -> None, kein Crash."""
    assert asyncio.run(order_service._real_order_cost_eur(object(), "1")) is None


def test_real_cost_no_order_id_returns_none():
    assert asyncio.run(order_service._real_order_cost_eur(_AeOk(), None)) is None


def test_real_cost_cancelled_or_unpaid_returns_none():
    """FAIL-CLOSED (Review-Fund): stornierte/nie bezahlte Orders liefern NIE einen EK —
    sonst bekaeme ein erstatteter Kauf volle Kosten und drueckt den Gewinn falsch."""
    assert asyncio.run(order_service._real_order_cost_eur(_AeCancelled(), "1")) is None
    assert asyncio.run(order_service._real_order_cost_eur(_AeUnpaid(), "1")) is None


def test_real_cost_missing_status_returns_none():
    """Unbekannter/fehlender Status -> None (fail-closed, Schaetzung uebernimmt)."""
    class _NoStatus:
        async def get_order_detail(self, oid):
            return {"total": 13.37, "currency": "USD"}
    assert asyncio.run(order_service._real_order_cost_eur(_NoStatus(), "1")) is None


def test_backfill_protects_manual_receipt_and_void(db, monkeypatch):
    """Backfill ueberschreibt NIE manuelle Korrekturen/Beleg-Werte und fasst stornierte
    Sales nicht an; Schaetzungen werden korrigiert (Review-Funde 13.07.)."""
    from app.models import OrderAliexpress, Sale

    def _mk(tid, status, cost, source, oid):
        s = Sale(ebay_transaction_id=tid, status=status, price_eur=Decimal("19.95"))
        db.add(s); db.flush()
        o = OrderAliexpress(sale_id=s.id, quantity=1, status="ordered",
                            aliexpress_order_id=oid, cost_cny=Decimal(str(cost)),
                            cost_source=source)
        db.add(o); db.commit()
        return s, o

    _, o_est = _mk("bf-a", "delivered", 8.88, "estimate", "111")   # Schaetzung -> korrigieren
    _, o_man = _mk("bf-b", "delivered", 12.54, "manual", "222")    # manuell -> NIE anfassen
    _, o_rec = _mk("bf-c", "delivered", 9.99, "receipt", "333")    # Beleg -> NIE anfassen
    _, o_void = _mk("bf-d", "refunded", 8.88, "estimate", "444")   # storniert -> skip

    async def fake_real(ae, ae_id):
        return Decimal("11.72")
    monkeypatch.setattr(order_service, "_real_order_cost_eur", fake_real)
    monkeypatch.setattr(order_service.asyncio, "sleep", lambda *_: _noop())

    res = asyncio.run(order_service.backfill_real_order_costs(db))
    db.refresh(o_est); db.refresh(o_man); db.refresh(o_rec); db.refresh(o_void)
    assert float(o_est.cost_cny) == 11.72 and o_est.cost_source == "api"
    assert float(o_man.cost_cny) == 12.54      # unangetastet
    assert float(o_rec.cost_cny) == 9.99       # unangetastet
    assert float(o_void.cost_cny) == 8.88      # storniert -> unangetastet
    assert res["updated"] == 1 and res["skipped_protected"] == 2 and res["skipped_void"] == 1


async def _noop():
    return None
