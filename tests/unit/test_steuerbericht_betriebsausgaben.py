"""Betriebsausgaben OHNE Verkaufsbezug im Steuerbericht.

Shop-Abo, Einstellgebuehren und Versandlabel haengen an keiner Bestellung. Der
Steuerbericht rechnet je Verkauf und kannte sie deshalb gar nicht — fuer 2026
fehlten so 325,89 EUR Gebuehren + 168,48 EUR Versandlabel als Ausgaben.

Quelle ist der gecachte Finanzbericht. Fehlt er, muessen die Posten ``None``
sein und NICHT 0,00 — sonst sieht eine Luecke wie eine Null aus (Regel 14).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.models import Sale
from app.services import finance_service


@pytest.fixture
def ohne_cache(tmp_path, monkeypatch):
    """Ein LEERES ./data - fuer die Tests, die "kein Cache vorhanden" pruefen.

    Vorher liefen diese Tests im ECHTEN Projektverzeichnis und lasen dort
    ``data/finance_report_2026.json``. Solange die Datei nicht existierte, ging
    das gut. Am 04.09.2026 um 03:00 legte der naechtliche Finanzjob sie zum
    ersten Mal an - und drei Tests kippten, ohne dass am Code etwas geaendert
    worden waere. Ein Test, der vom Zustand des Arbeitsverzeichnisses abhaengt,
    prueft nicht, was er zu pruefen vorgibt.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    return tmp_path


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Schreibt einen Finanzbericht-Cache in ein isoliertes ./data."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()

    def _schreibe(year=2026, *, ohne_bestellbezug=325.89, versandlabel=168.48):
        gesamt = {"period": f"{year} gesamt", "count": 1, "brutto": 100.0,
                  "gebuehren": 30.0, "versandlabel": versandlabel,
                  "erstattung": 0.0, "netto": 70.0}
        if ohne_bestellbezug is not None:
            gesamt["gebuehren_ohne_bestellbezug"] = ohne_bestellbezug
        (tmp_path / "data" / f"finance_report_{year}.json").write_text(
            json.dumps({"year": year, "gesamt": gesamt}), encoding="utf-8")
    return _schreibe


def _sale(db, preis="100.00", fee="20.00"):
    s = Sale(ebay_transaction_id="T-1", ebay_order_id="01-1", quantity=1,
             price_eur=Decimal(preis), fee_eur_actual=Decimal(fee),
             sale_date=datetime(2026, 5, 5, tzinfo=timezone.utc), status="delivered")
    db.add(s)
    db.commit()
    return s


def test_uebernimmt_beide_posten(db, cache):
    cache()
    _sale(db)

    sm = finance_service.tax_report(db, year=2026)["summary"]

    assert sm["kontobezogene_gebuehren_eur"] == pytest.approx(325.89)
    assert sm["versandlabel_eur"] == pytest.approx(168.48)
    assert sm["weitere_betriebsausgaben_eur"] == pytest.approx(494.37)
    assert sm["zusatzkosten_quelle"] == "eBay-Finanzbericht"


def test_ergebnis_zieht_die_weiteren_ausgaben_ab(db, cache):
    cache()
    _sale(db, preis="100.00", fee="20.00")

    sm = finance_service.tax_report(db, year=2026)["summary"]

    assert sm["bruttomarge_eur"] == pytest.approx(80.00)      # 100 - 20 - 0 EK
    assert sm["ergebnis_eur"] == pytest.approx(80.00 - 494.37)


def test_ohne_cache_unbekannt_statt_null(db, ohne_cache):
    """Eine fehlende Zahl darf nicht wie 0,00 EUR aussehen."""
    _sale(db)

    sm = finance_service.tax_report(db, year=2026)["summary"]

    assert sm["kontobezogene_gebuehren_eur"] is None
    assert sm["versandlabel_eur"] is None
    assert sm["weitere_betriebsausgaben_eur"] is None
    assert sm["ergebnis_eur"] is None
    assert "fehlt" in sm["zusatzkosten_quelle"]


def test_alter_cache_ohne_neues_feld_bleibt_unbekannt(db, cache):
    """Cache von vor dieser Aenderung -> lieber unbekannt als falsch."""
    cache(ohne_bestellbezug=None)
    _sale(db)

    sm = finance_service.tax_report(db, year=2026)["summary"]

    assert sm["kontobezogene_gebuehren_eur"] is None
    assert sm["ergebnis_eur"] is None


def test_bruttomarge_bleibt_unveraendert(db, cache):
    """Die Bruttomarge behaelt ihre Bedeutung (Verkaufsebene) – nur ERGEBNIS ist neu."""
    cache()
    _sale(db, preis="100.00", fee="20.00")

    ohne = finance_service.tax_report(db, year=2026)["summary"]["bruttomarge_eur"]
    assert ohne == pytest.approx(80.00)


def test_csv_weist_die_posten_aus(db, cache):
    cache()
    _sale(db)

    text = finance_service.tax_report_csv(db, year=2026).decode("utf-8-sig")

    assert "WEITERE BETRIEBSAUSGABEN" in text
    assert "325,89" in text
    assert "168,48" in text
    assert "ERGEBNIS" in text


def test_csv_schreibt_unbekannt_statt_null(db, ohne_cache):
    _sale(db)

    text = finance_service.tax_report_csv(db, year=2026).decode("utf-8-sig")

    assert "unbekannt" in text
    assert "0,0" not in text.split("WEITERE BETRIEBSAUSGABEN")[1]


def test_zip_zusammenfassung_enthaelt_die_posten(db, cache):
    """Die zusammenfassung.txt im Steuerberater-ZIP ist der Uebergabepunkt."""
    import zipfile
    from app.services import invoice_service
    cache()
    _sale(db)

    data = invoice_service.tax_export_zip(db, year=2026)
    with zipfile.ZipFile(__import__("io").BytesIO(data)) as z:
        text = z.read("steuer-export-2026/zusammenfassung.txt").decode("utf-8")

    assert "kontobezogen" in text
    assert "325,89" in text
    assert "Versandlabel" in text
    assert "168,48" in text
    assert "Gewinn (Ueberschuss)" in text


def test_zip_ohne_cache_kennzeichnet_den_gewinn(db, ohne_cache):
    """Ohne die Zahlen darf der Gewinn nicht so aussehen, als waere er vollstaendig."""
    import zipfile
    from app.services import invoice_service
    _sale(db)

    data = invoice_service.tax_export_zip(db, year=2026)
    with zipfile.ZipFile(__import__("io").BytesIO(data)) as z:
        text = z.read("steuer-export-2026/zusammenfassung.txt").decode("utf-8")

    assert "unbekannt" in text
    assert "Gewinn VOR diesen Posten" in text
