"""Nachziehen echter eBay-Gebuehren ueber ein ganzes Jahr (sync_ebay_fees).

Der Nachtlauf schaut nur 45 Tage zurueck; fuer 2026 fehlten dadurch ~935 EUR
Gebuehren MIT Bestellbezug (Februar/Maerz praktisch leer). Zwei Gefahren beim
Nachziehen, die hier abgesichert sind:

1. Ein abgeschnittener API-Abruf wuerde ZU NIEDRIGE Gebuehren schreiben.
2. Verkaeufe VOR dem Abrufzeitraum kennen nur einen Teil ihrer Gebuehren —
   sie zu ueberschreiben waere schlechter als der Altwert.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.models import Sale
from app.services import finance_service


class _FakeEbay:
    def __init__(self, txs):
        self._txs = txs
        self.kwargs = None

    async def get_finance_transactions(self, **kwargs):
        self.kwargs = kwargs
        return self._txs


_lfd = iter(range(1, 10_000))


def _sale(db, *, order, preis, wann, fee=None):
    # ebay_transaction_id ist UNIQUE – bei mehreren Verkaeufen je Bestellung
    # braucht jeder seine eigene.
    s = Sale(ebay_transaction_id=f"T-{order}-{next(_lfd)}", ebay_order_id=order,
             quantity=1,
             price_eur=Decimal(str(preis)), sale_date=wann, status="delivered")
    if fee is not None:
        s.fee_eur_actual = Decimal(str(fee))
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _tx(typ, betrag, order, *, fee=None, per_reference=False):
    t = {"transactionType": typ, "transactionDate": "2026-02-10T10:00:00.000Z",
         "amount": {"value": str(betrag), "currency": "EUR"}}
    if per_reference:
        t["references"] = [{"referenceType": "ORDER_ID", "referenceId": order}]
    else:
        t["orderId"] = order
    if fee is not None:
        t["totalFeeAmount"] = {"value": str(fee), "currency": "EUR"}
    return t


@pytest.fixture
def fake(monkeypatch):
    def _setze(txs):
        client = _FakeEbay(txs)
        monkeypatch.setattr(finance_service, "_real_ebay", lambda: client)
        return client
    return _setze


@pytest.mark.asyncio
async def test_zieht_fehlende_gebuehr_nach(db, fake):
    """Februar-Verkauf ohne Gebuehr bekommt sie aus dem Jahresfenster."""
    s = _sale(db, order="01-FEB", preis="30.00",
              wann=datetime(2026, 2, 10, tzinfo=timezone.utc), fee="0.00")
    fake([_tx("SALE", "30.00", "01-FEB", fee="4.20"),
          _tx("NON_SALE_CHARGE", "-2.10", "01-FEB", per_reference=True)])

    r = await finance_service.sync_ebay_fees(
        db, date_from=datetime(2025, 12, 1, tzinfo=timezone.utc),
        nur_ab=datetime(2026, 1, 1, tzinfo=timezone.utc))

    db.refresh(s)
    # Verkaufsprovision 4,20 + Anzeigengebuehr 2,10
    assert s.fee_eur_actual == Decimal("6.30")
    assert r["sales_updated"] == 1


@pytest.mark.asyncio
async def test_vorjahres_verkauf_wird_nicht_ueberschrieben(db, fake):
    """Dessen Gebuehren liegen teils vor dem Fenster -> Altwert behalten."""
    alt = _sale(db, order="01-DEZ", preis="50.00",
                wann=datetime(2025, 12, 20, tzinfo=timezone.utc), fee="12.00")
    fake([_tx("SALE", "50.00", "01-DEZ", fee="1.00")])   # nur ein Teilbetrag

    r = await finance_service.sync_ebay_fees(
        db, date_from=datetime(2025, 12, 1, tzinfo=timezone.utc),
        nur_ab=datetime(2026, 1, 1, tzinfo=timezone.utc))

    db.refresh(alt)
    assert alt.fee_eur_actual == Decimal("12.00")
    assert r["sales_updated"] == 0
    assert r["sales_uebersprungen"] == 1


@pytest.mark.asyncio
async def test_ohne_nur_ab_bleibt_alles_wie_bisher(db, fake):
    """Der Nachtlauf ruft ohne nur_ab -> unveraendertes Verhalten."""
    s = _sale(db, order="01-ALT", preis="10.00",
              wann=datetime(2025, 12, 20, tzinfo=timezone.utc))
    fake([_tx("SALE", "10.00", "01-ALT", fee="1.50")])

    r = await finance_service.sync_ebay_fees(db, days=45)

    db.refresh(s)
    assert s.fee_eur_actual == Decimal("1.50")
    assert r["sales_uebersprungen"] == 0


@pytest.mark.asyncio
async def test_gebuehr_wird_auf_mehrere_verkaeufe_verteilt(db, fake):
    a = _sale(db, order="01-MULTI", preis="30.00",
              wann=datetime(2026, 2, 10, tzinfo=timezone.utc))
    b = _sale(db, order="01-MULTI", preis="10.00",
              wann=datetime(2026, 2, 10, tzinfo=timezone.utc))
    fake([_tx("SALE", "40.00", "01-MULTI", fee="8.00")])

    await finance_session_sync(db)

    db.refresh(a); db.refresh(b)
    assert a.fee_eur_actual == Decimal("6.00")     # 30/40 von 8,00
    assert b.fee_eur_actual == Decimal("2.00")


async def finance_session_sync(db):
    return await finance_service.sync_ebay_fees(
        db, date_from=datetime(2025, 12, 1, tzinfo=timezone.utc),
        nur_ab=datetime(2026, 1, 1, tzinfo=timezone.utc))


@pytest.mark.asyncio
async def test_fenster_wird_an_die_api_durchgereicht(db, fake):
    client = fake([])
    von = datetime(2025, 12, 1, tzinfo=timezone.utc)
    bis = datetime(2026, 12, 31, tzinfo=timezone.utc)

    await finance_service.sync_ebay_fees(db, date_from=von, date_to=bis,
                                         max_pages=60)

    assert client.kwargs["date_from"] == von
    assert client.kwargs["date_to"] == bis
    assert client.kwargs["max_pages"] == 60
    assert "days" not in client.kwargs
