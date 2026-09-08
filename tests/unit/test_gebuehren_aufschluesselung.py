"""Diagnose der eBay-Gebuehren nach Art (finance_service.gebuehren_aufschluesselung).

Kernfrage: welche Gebuehren haengen an KEINER Bestellung? Genau die fehlen im
Steuerbericht (der rechnet je Verkauf) und sind damit nicht abgesetzte
Betriebsausgaben. Die Funktion darf nichts schreiben.
"""
from __future__ import annotations

import pytest

from app.services import finance_service


class _FakeEbay:
    """Liefert feste Transaktionen statt der echten Finances-API."""

    def __init__(self, txs):
        self._txs = txs
        self.aufrufe = 0

    async def get_finance_transactions(self, **kwargs):
        self.aufrufe += 1
        return self._txs


def _tx(typ, betrag, *, datum="2026-03-04T10:00:00.000Z", order=None,
        fee=None, **extra):
    t = {"transactionType": typ, "transactionDate": datum,
         "amount": {"value": str(betrag), "currency": "EUR"}}
    if order:
        t["orderId"] = order
    if fee is not None:
        t["totalFeeAmount"] = {"value": str(fee), "currency": "EUR"}
    t.update(extra)
    return t


@pytest.fixture
def fake(monkeypatch):
    def _setze(txs):
        client = _FakeEbay(txs)
        monkeypatch.setattr(finance_service, "_real_ebay", lambda: client)
        return client
    return _setze


@pytest.mark.asyncio
async def test_trennt_gebuehren_mit_und_ohne_bestellbezug(fake):
    fake([
        _tx("SALE", "20.00", order="01-111", fee="5.00"),
        _tx("NON_SALE_CHARGE", "-1.50", order="01-111", feeType="AD_FEE"),
        _tx("NON_SALE_CHARGE", "-39.95", feeType="STORE_SUBSCRIPTION"),
        _tx("FEE", "-12.30", feeType="AD_FEE"),
    ])
    r = await finance_service.gebuehren_aufschluesselung(year=2026)

    assert r["verkaufsgebuehren_mit_bestellbezug"] == pytest.approx(5.00)
    assert r["zusatzgebuehren_mit_bestellbezug"] == pytest.approx(1.50)
    # Das ist die gesuchte Luecke: 39,95 Shop-Abo + 12,30 Anzeigen.
    assert r["ohne_bestellbezug"] == pytest.approx(52.25)


@pytest.mark.asyncio
async def test_benennt_die_gebuehrenart(fake):
    fake([_tx("NON_SALE_CHARGE", "-39.95", feeType="STORE_SUBSCRIPTION")])
    r = await finance_service.gebuehren_aufschluesselung(year=2026)

    (art,) = r["arten"]
    assert "STORE_SUBSCRIPTION" in art["art"]
    assert "OHNE Bestellbezug" in art["art"]
    assert art["anzahl"] == 1
    assert art["summe_eur"] == pytest.approx(39.95)


@pytest.mark.asyncio
async def test_art_faellt_auf_references_zurueck(fake):
    """Ohne feeType nutzt eBay teils nur references -> trotzdem benennbar."""
    fake([_tx("FEE", "-3.00",
              references=[{"referenceType": "INVOICE", "referenceId": "X"}])])
    r = await finance_service.gebuehren_aufschluesselung(year=2026)

    assert "INVOICE" in r["arten"][0]["art"]


@pytest.mark.asyncio
async def test_bestellbezug_auch_aus_references(fake):
    """eBay haengt AD_FEEs NICHT an orderId, sondern an references[ORDER_ID].

    Nur ``orderId`` zu pruefen wuerde sie als kontobezogen zaehlen — bei 943
    Anzeigengebuehren verschiebt das die Steuerzahlen um Tausende Euro.
    """
    fake([_tx("NON_SALE_CHARGE", "-2.10", feeType="AD_FEE",
              references=[{"referenceType": "ORDER_ID", "referenceId": "01-999"}])])
    r = await finance_service.gebuehren_aufschluesselung(year=2026)

    assert r["ohne_bestellbezug"] == pytest.approx(0.0)
    assert r["zusatzgebuehren_mit_bestellbezug"] == pytest.approx(2.10)
    assert r["arten"][0]["mit_bestellbezug"] is True
    assert "OHNE Bestellbezug" not in r["arten"][0]["art"]


@pytest.mark.asyncio
async def test_order_id_ist_keine_gebuehrenart(fake):
    """Der Bestellbezug darf nicht als Gebuehrenart durchgehen."""
    fake([_tx("NON_SALE_CHARGE", "-2.10",
              references=[{"referenceType": "ORDER_ID", "referenceId": "01-999"}])])
    r = await finance_service.gebuehren_aufschluesselung(year=2026)

    assert "ORDER_ID" not in r["arten"][0]["art"]


@pytest.mark.asyncio
async def test_sale_gebuehr_zaehlt_mit_references_bestellbezug(fake):
    fake([_tx("SALE", "20.00", fee="5.00",
              references=[{"referenceType": "ORDER_ID", "referenceId": "01-777"}])])
    r = await finance_service.gebuehren_aufschluesselung(year=2026)

    assert r["verkaufsgebuehren_mit_bestellbezug"] == pytest.approx(5.00)


@pytest.mark.asyncio
async def test_fremdes_jahr_wird_ignoriert(fake):
    fake([
        _tx("NON_SALE_CHARGE", "-10.00", datum="2025-12-31T23:00:00.000Z"),
        _tx("NON_SALE_CHARGE", "-7.00", datum="2026-01-02T09:00:00.000Z"),
    ])
    r = await finance_service.gebuehren_aufschluesselung(year=2026)

    assert r["ohne_bestellbezug"] == pytest.approx(7.00)


@pytest.mark.asyncio
async def test_beispiele_ohne_kaeuferdaten(fake):
    """Fuer die Gebuehrenfrage sind Kaeuferdaten irrelevant -> nicht mit ausliefern."""
    fake([_tx("NON_SALE_CHARGE", "-5.00", feeType="AD_FEE",
              buyer={"username": "kunde123"})])
    r = await finance_service.gebuehren_aufschluesselung(year=2026)

    (beispiel,) = r["beispiele_ohne_bestellbezug"]
    assert "buyer" not in beispiel
    assert beispiel["transactionType"] == "NON_SALE_CHARGE"


@pytest.mark.asyncio
async def test_erstattungen_zaehlen_nicht_als_gebuehr(fake):
    fake([
        _tx("REFUND", "-25.00", order="01-222"),
        _tx("SHIPPING_LABEL", "-4.20"),
        _tx("NON_SALE_CHARGE", "-8.00", feeType="AD_FEE"),
    ])
    r = await finance_service.gebuehren_aufschluesselung(year=2026)

    assert r["ohne_bestellbezug"] == pytest.approx(8.00)
    assert len(r["arten"]) == 1
