"""Kontierung: Regel-Zuordnung, Manuell-Schutz, Jahres-Summen."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.models import BankTransaction
from app.services import kontierung_service


def _bt(db, i: int, amount: str, name: str, days_ago: float = 1,
        purpose: str = "", kontierung: str | None = None,
        source: str | None = None) -> BankTransaction:
    tx = BankTransaction(
        bank_ref=f"kontist:kt-{i}",
        transaction_date=datetime.now(timezone.utc) - timedelta(days=days_ago),
        amount=Decimal(amount), counterparty_name=name,
        description=purpose or None, status="pending",
        kontierung=kontierung, kontierung_source=source)
    db.add(tx)
    db.commit()
    return tx


def test_rules_assign_expected_categories(db):
    a = _bt(db, 1, "-12.34", "AliExpress")
    b = _bt(db, 2, "3.00", "ALIPAY EUROPE")
    c = _bt(db, 3, "150.00", "eBay Commerce UK")
    d = _bt(db, 4, "-2.00", "Kontist GmbH")
    e = _bt(db, 5, "45.67", "Max Mustermann", purpose="eBay Artikel 123")
    r = kontierung_service.kontiere_neue(db)
    assert r == {"kontiert_neu": 4, "unkontiert": 1}
    assert (a.kontierung, b.kontierung, c.kontierung, d.kontierung) == (
        "wareneinkauf", "wareneinkauf_erstattung", "ebay_auszahlung", "kontofuehrung")
    assert all(t.kontierung_source == "regel" for t in (a, b, c, d))
    # Verwendungszweck zaehlt NICHT (Kunden-Ueberweisung bleibt unkontiert)
    assert e.kontierung is None
    # Zweiter Lauf: nichts Neues, keine Doppelarbeit
    assert kontierung_service.kontiere_neue(db) == {"kontiert_neu": 0, "unkontiert": 1}


def test_manual_kontierung_never_overwritten(db):
    tx = _bt(db, 1, "-12.34", "AliExpress",
             kontierung="privatentnahme", source="manuell")
    kontierung_service.kontiere_neue(db)
    db.refresh(tx)
    assert tx.kontierung == "privatentnahme"
    assert tx.kontierung_source == "manuell"


def test_summary_sums_by_category_and_year(db):
    _bt(db, 1, "-10.00", "AliExpress")
    _bt(db, 2, "-5.50", "AliExpress")
    _bt(db, 3, "100.00", "eBay Commerce UK")
    _bt(db, 4, "-9.99", "Unbekannter Laden")            # bleibt unkontiert
    _bt(db, 5, "-99.00", "AliExpress", days_ago=400)    # Vorjahr: nicht im Summary
    kontierung_service.kontiere_neue(db)
    s = kontierung_service.kontierung_summary(db)
    by_key = {k["kategorie"]: k for k in s["kategorien"]}
    assert by_key["wareneinkauf"]["anzahl"] == 2
    assert by_key["wareneinkauf"]["summe_eur"] == -15.5
    assert by_key["wareneinkauf"]["skr03"] == "3200"
    assert by_key["ebay_auszahlung"]["summe_eur"] == 100.0
    assert s["unkontiert_anzahl"] == 1
    assert s["unkontiert_summe_eur"] == -9.99
    assert "Vorschlaege" in s["skr_hinweis"]


def test_summary_respects_year_filter(db):
    _bt(db, 1, "-99.00", "AliExpress", days_ago=400)
    kontierung_service.kontiere_neue(db)
    prev = datetime.now(timezone.utc).year - 1
    s = kontierung_service.kontierung_summary(db, year=prev)
    assert s["jahr"] == prev
    assert {k["kategorie"] for k in s["kategorien"]} == {"wareneinkauf"}
