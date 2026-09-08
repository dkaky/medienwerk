"""Tests: Abbuchung + Bestellung am selben Tag mit demselben Betrag."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.models import BankTransaction, OrderAliexpress
from app.services import bank_sync_service


def _buchung(db, *, betrag: float, tage: int = 0, ref: str = "kontist:1") -> BankTransaction:
    tx = BankTransaction(bank_ref=ref, amount=Decimal(str(-abs(betrag))),
                         counterparty_name="Aliexpress.com", status="pending",
                         transaction_date=datetime.now(timezone.utc) - timedelta(days=tage))
    db.add(tx); db.commit()
    return tx


def _bestellung(db, *, betrag: float, tage: int = 0, ae: str = "3074619661272059"):
    o = OrderAliexpress(aliexpress_order_id=ae, quantity=1, status="delivered",
                        cost_cny=Decimal(str(betrag)),
                        order_date=datetime.now(timezone.utc) - timedelta(days=tage))
    db.add(o); db.commit()
    return o


def test_gleicher_tag_gleicher_betrag_wird_zugeordnet(db):
    tx = _buchung(db, betrag=77.42, tage=3)
    o = _bestellung(db, betrag=77.42, tage=3)

    vorschau = bank_sync_service.match_gleicher_tag(db)
    assert vorschau["wuerde_zuordnen"] == 1 and vorschau["zugeordnet"] == 0

    r = bank_sync_service.match_gleicher_tag(db, anwenden=True)
    assert r["zugeordnet"] == 1
    db.refresh(tx)
    assert tx.status == "matched" and tx.order_id == o.id
    assert tx.match_info["rule"] == "tag_betrag"


def test_ein_tag_versatz_ist_erlaubt(db):
    """AliExpress datiert in anderer Zeitzone als die Bank."""
    _buchung(db, betrag=25.83, tage=4)
    _bestellung(db, betrag=25.83, tage=3)
    assert bank_sync_service.match_gleicher_tag(db)["wuerde_zuordnen"] == 1


def test_zu_grosser_zeitabstand_wird_nicht_zugeordnet(db):
    _buchung(db, betrag=25.83, tage=10)
    _bestellung(db, betrag=25.83, tage=3)
    assert bank_sync_service.match_gleicher_tag(db)["wuerde_zuordnen"] == 0


def test_gleicher_betrag_am_selben_tag_doppelt_bleibt_offen(db):
    """Zwei Buchungen und zwei Bestellungen ueber denselben Betrag am selben Tag:
    welche zu welcher gehoert, ist nicht entscheidbar -> offen lassen."""
    _buchung(db, betrag=12.09, tage=2, ref="kontist:a")
    _buchung(db, betrag=12.09, tage=2, ref="kontist:b")
    _bestellung(db, betrag=12.09, tage=2, ae="3074619661272061")
    _bestellung(db, betrag=12.09, tage=2, ae="3074619661272062")
    r = bank_sync_service.match_gleicher_tag(db)
    assert r["wuerde_zuordnen"] == 0 and r["mehrdeutig"] >= 2


def test_abweichender_betrag_wird_nicht_zugeordnet(db):
    _buchung(db, betrag=15.94, tage=2)
    _bestellung(db, betrag=15.80, tage=2)      # 14 Cent daneben
    assert bank_sync_service.match_gleicher_tag(db)["wuerde_zuordnen"] == 0


def test_bereits_zugeordnete_buchung_wird_nicht_angefasst(db):
    tx = _buchung(db, betrag=30.00, tage=2)
    tx.status = "matched"; db.commit()
    _bestellung(db, betrag=30.00, tage=2)
    assert bank_sync_service.match_gleicher_tag(db)["geprueft"] == 0
