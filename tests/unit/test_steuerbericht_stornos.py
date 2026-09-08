"""Stornierte/erstattete Verkaeufe sind keine Einnahme (finance_service.tax_report).

Bis 15.08.2026 zaehlte der Steuerbericht sie als Umsatz mit (2026: 46 Stueck,
1.365,20 EUR) — als einzige Ansicht im Projekt. Folge: Steuer auf Geld, das
zurueckgezahlt wurde.

Wichtig: die Stornos duerfen nicht STILL verschwinden. Eine Zahl, die einfach
kleiner wird, ist fuer den Steuerberater nicht pruefbar — sie wird ausgewiesen.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.models import Sale
from app.services import finance_service
from app.services.common import VOID_SALE_STATUS

_WANN = datetime(2026, 5, 5, tzinfo=timezone.utc)
_lfd = iter(range(1, 10_000))


def _sale(db, *, preis="100.00", fee="20.00", status="delivered"):
    n = next(_lfd)
    s = Sale(ebay_transaction_id=f"T-{n}", ebay_order_id=f"01-{n}", quantity=1,
             price_eur=Decimal(preis), fee_eur_actual=Decimal(fee),
             sale_date=_WANN, status=status)
    db.add(s)
    db.commit()
    return s


def test_storno_zaehlt_nicht_als_einnahme(db):
    _sale(db, preis="100.00", fee="20.00")
    _sale(db, preis="30.00", fee="6.00", status="refunded")

    sm = finance_service.tax_report(db, year=2026)["summary"]

    assert sm["einnahmen_eur"] == pytest.approx(100.00)
    assert sm["verkaeufe"] == 1


@pytest.mark.parametrize("status", sorted(VOID_SALE_STATUS))
def test_alle_storno_schreibweisen(db, status):
    """eBay/POD Shop nutzen mehrere Bezeichnungen — jede muss greifen."""
    _sale(db, preis="50.00", status=status)

    sm = finance_service.tax_report(db, year=2026)["summary"]

    assert sm["einnahmen_eur"] == pytest.approx(0.0)
    assert sm["stornos_ausgeschlossen"] == 1


def test_storno_wird_ausgewiesen_nicht_verschluckt(db):
    _sale(db, preis="100.00", fee="20.00")
    _sale(db, preis="30.00", fee="6.00", status="cancelled")
    _sale(db, preis="12.20", fee="2.00", status="refunded")

    sm = finance_service.tax_report(db, year=2026)["summary"]

    assert sm["stornos_ausgeschlossen"] == 2
    assert sm["stornos_eur"] == pytest.approx(42.20)
    assert sm["stornos_gebuehren_eur"] == pytest.approx(8.00)


def test_gebuehr_des_stornos_faellt_mit_weg(db):
    _sale(db, preis="100.00", fee="20.00")
    _sale(db, preis="30.00", fee="6.00", status="refunded")

    sm = finance_service.tax_report(db, year=2026)["summary"]

    assert sm["ebay_gebuehren_eur"] == pytest.approx(20.00)


def test_storno_taucht_in_keiner_zeile_auf(db):
    _sale(db, preis="100.00")
    _sale(db, preis="30.00", status="cancelled")

    rows = finance_service.tax_report(db, year=2026)["rows"]

    assert len(rows) == 1
    assert rows[0]["einnahme_eur"] == pytest.approx(100.00)


def test_csv_weist_die_stornos_aus(db):
    _sale(db, preis="100.00")
    _sale(db, preis="30.00", fee="6.00", status="refunded")

    text = finance_service.tax_report_csv(db, year=2026).decode("utf-8-sig")

    assert "NICHT enthalten" in text
    assert "30,0" in text


def test_ohne_stornos_keine_zusatzzeile(db):
    _sale(db, preis="100.00")

    text = finance_service.tax_report_csv(db, year=2026).decode("utf-8-sig")
    sm = finance_service.tax_report(db, year=2026)["summary"]

    assert "NICHT enthalten" not in text
    assert sm["stornos_ausgeschlossen"] == 0


def test_zip_nennt_die_stornos(db):
    import io
    import zipfile

    from app.services import invoice_service
    _sale(db, preis="100.00")
    _sale(db, preis="30.00", status="cancelled")

    data = invoice_service.tax_export_zip(db, year=2026)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        text = z.read("steuer-export-2026/zusammenfassung.txt").decode("utf-8")

    assert "Nicht enthalten" in text
    assert "keine Einnahme" in text


def test_zip_summe_passt_jetzt_zur_belegliste(db):
    """Der Widerspruch im Export: Summenzeile nannte 865, index.csv listete 819."""
    import io
    import zipfile

    from app.services import invoice_service
    _sale(db, preis="100.00")
    _sale(db, preis="30.00", status="refunded")

    data = invoice_service.tax_export_zip(db, year=2026)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        text = z.read("steuer-export-2026/zusammenfassung.txt").decode("utf-8")
        index = z.read("steuer-export-2026/index.csv").decode("utf-8-sig")

    einnahme_zeilen = [r for r in index.splitlines()[1:] if ";Einnahme;" in r]
    assert "(1 Verkaeufe)" in text
    assert len(einnahme_zeilen) == 1
