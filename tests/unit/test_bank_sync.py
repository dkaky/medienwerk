"""Kontist-Bank-Sync + AliExpress-Abgleich: Spiegel-Idempotenz und Matching-Regeln."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.integrations import kontist
from app.models import BankTransaction, OrderAliexpress
from app.services import bank_sync_service


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _tx(i: int, cents: int, name: str = "AliExpress", days_ago: float = 0,
        purpose: str = "") -> dict:
    return {"id": f"tx-{i}", "amount": cents, "name": name,
            "valutaDate": _iso(days_ago), "bookingDate": _iso(days_ago),
            "purpose": purpose}


def _order(db, cost: str, days_ago: float = 1, ae_id: str | None = "800100",
           status: str = "ordered") -> OrderAliexpress:
    o = OrderAliexpress(aliexpress_order_id=ae_id, cost_cny=Decimal(cost),
                        order_date=datetime.now(timezone.utc) - timedelta(days=days_ago),
                        status=status)
    db.add(o)
    db.commit()
    return o


def _run_sync(db, monkeypatch, txs: list[dict]) -> dict:
    async def fake_fetch(max_pages: int = 40):
        return txs
    monkeypatch.setattr(kontist, "fetch_transactions", fake_fetch)
    return asyncio.run(bank_sync_service.sync_bank_transactions(db))


def test_sync_mirrors_and_is_idempotent(db, monkeypatch):
    txs = [_tx(1, -1234), _tx(2, 5000, name="eBay")]
    r1 = _run_sync(db, monkeypatch, txs)
    r2 = _run_sync(db, monkeypatch, txs)
    assert r1 == {"fetched": 2, "new": 2}
    assert r2 == {"fetched": 2, "new": 0}
    rows = list(db.scalars(select(BankTransaction)))
    assert len(rows) == 2
    by_ref = {r.bank_ref: r for r in rows}
    assert by_ref["kontist:tx-1"].amount == Decimal("-12.34")
    assert by_ref["kontist:tx-2"].amount == Decimal("50.00")


def test_exact_match_links_order(db, monkeypatch):
    o = _order(db, "12.34", days_ago=1)
    _run_sync(db, monkeypatch, [_tx(1, -1234), _tx(2, -500, name="REWE")])
    r = bank_sync_service.match_aliexpress_orders(db)
    assert r == {"checked": 1, "matched": 1}
    tx = db.scalar(select(BankTransaction).where(BankTransaction.bank_ref == "kontist:tx-1"))
    assert tx.status == "matched" and tx.order_id == o.id
    assert tx.match_info["order_ids"] == [o.id]
    assert tx.match_info["rule"] == "exact"


def test_ambiguous_amount_stays_open(db, monkeypatch):
    _order(db, "9.99", days_ago=1, ae_id="800111")
    _order(db, "9.99", days_ago=1, ae_id="800222")
    _run_sync(db, monkeypatch, [_tx(1, -999)])
    r = bank_sync_service.match_aliexpress_orders(db)
    assert r["matched"] == 0, "mehrdeutig darf NIE automatisch zugeordnet werden"


def test_cart_combo_match(db, monkeypatch):
    o1 = _order(db, "5.00", days_ago=1, ae_id="800111")
    o2 = _order(db, "7.50", days_ago=1, ae_id="800222")
    _run_sync(db, monkeypatch, [_tx(1, -1250)])
    r = bank_sync_service.match_aliexpress_orders(db)
    assert r["matched"] == 1
    tx = db.scalar(select(BankTransaction).where(BankTransaction.status == "matched"))
    assert sorted(tx.match_info["order_ids"]) == sorted([o1.id, o2.id])
    assert tx.match_info["rule"] == "combo"


def test_multiposition_order_matches_as_group(db, monkeypatch):
    # Mehrpositions-Bestellung: zwei Zeilen teilen sich EINE AliExpress-Order-ID,
    # die Bank bucht die Gesamtsumme ab.
    o1 = _order(db, "3.00", days_ago=1, ae_id="800999")
    o2 = _order(db, "4.00", days_ago=1, ae_id="800999")
    _run_sync(db, monkeypatch, [_tx(1, -700)])
    r = bank_sync_service.match_aliexpress_orders(db)
    assert r["matched"] == 1
    tx = db.scalar(select(BankTransaction).where(BankTransaction.status == "matched"))
    assert sorted(tx.match_info["order_ids"]) == sorted([o1.id, o2.id])
    assert tx.match_info["rule"] == "exact"


def test_exact_vs_combo_ambiguity_stays_open(db, monkeypatch):
    # Sammelzahlung X+Y trifft denselben Betrag wie Einzelbestellung Z ->
    # stufenuebergreifend mehrdeutig, darf NICHT zugeordnet werden.
    _order(db, "5.00", days_ago=1, ae_id="800111")
    _order(db, "7.50", days_ago=1, ae_id="800222")
    _order(db, "12.50", days_ago=1, ae_id="800333")
    _run_sync(db, monkeypatch, [_tx(1, -1250)])
    assert bank_sync_service.match_aliexpress_orders(db)["matched"] == 0


def test_second_run_keeps_combo_match_and_blocks_reuse(db, monkeypatch):
    # Nach einem Combo-Match darf ein zweiter Lauf mit neuer gleichhoher Buchung
    # die bereits zugeordneten Bestellungen NICHT erneut vergeben.
    _order(db, "5.00", days_ago=1, ae_id="800111")
    _order(db, "7.50", days_ago=1, ae_id="800222")
    _run_sync(db, monkeypatch, [_tx(1, -1250)])
    assert bank_sync_service.match_aliexpress_orders(db)["matched"] == 1
    first = db.scalar(select(BankTransaction).where(BankTransaction.status == "matched"))
    info_before = dict(first.match_info)
    _run_sync(db, monkeypatch, [_tx(1, -1250), _tx(2, -1250)])
    assert bank_sync_service.match_aliexpress_orders(db)["matched"] == 0
    db.refresh(first)
    assert first.status == "matched" and first.match_info == info_before


def test_two_debits_one_order_matches_only_once(db, monkeypatch):
    _order(db, "9.99", days_ago=1, ae_id="800111")
    _run_sync(db, monkeypatch, [_tx(1, -999, days_ago=0.5), _tx(2, -999)])
    assert bank_sync_service.match_aliexpress_orders(db)["matched"] == 1
    rec = bank_sync_service.bank_reconciliation(db)
    assert rec["ali_matched"] == 1 and len(rec["ali_open"]) == 1


def test_resync_preserves_existing_match(db, monkeypatch):
    o = _order(db, "12.34", days_ago=1)
    txs = [_tx(1, -1234)]
    _run_sync(db, monkeypatch, txs)
    assert bank_sync_service.match_aliexpress_orders(db)["matched"] == 1
    r = _run_sync(db, monkeypatch, txs)   # naechtlicher Folge-Sync, gleiche Daten
    assert r["new"] == 0
    tx = db.scalar(select(BankTransaction).where(BankTransaction.bank_ref == "kontist:tx-1"))
    assert tx.status == "matched" and tx.order_id == o.id
    assert tx.match_info["order_ids"] == [o.id]


def test_crowded_checkout_window_stays_open(db, monkeypatch):
    # 13 offene Gruppen am selben Tag: Kombinatorik nicht vollstaendig pruefbar
    # -> auch ein exakt passender Betrag bleibt offen (fail-open).
    for i in range(13):
        _order(db, f"{10 + i}.00", days_ago=1, ae_id=f"80{i:04d}")
    _run_sync(db, monkeypatch, [_tx(1, -1200)])
    assert bank_sync_service.match_aliexpress_orders(db)["matched"] == 0


def test_exact_match_outside_combo_window_still_works(db, monkeypatch):
    # Abbuchung 5 Tage nach der Bestellung: fuer Kombis zu spaet (Checkout-Fenster
    # 2 Tage), der eindeutige Einzeltreffer greift trotzdem.
    o = _order(db, "12.34", days_ago=5)
    _run_sync(db, monkeypatch, [_tx(1, -1234)])
    assert bank_sync_service.match_aliexpress_orders(db)["matched"] == 1
    tx = db.scalar(select(BankTransaction).where(BankTransaction.status == "matched"))
    assert tx.order_id == o.id


def test_reconciliation_hides_old_history_from_list(db, monkeypatch):
    _order(db, "9.00", days_ago=90, ae_id="800555")   # Historie: zaehlt, wird nicht gelistet
    _order(db, "7.00", days_ago=10, ae_id="800666")   # juengst: wird gelistet
    _run_sync(db, monkeypatch, [])
    rec = bank_sync_service.bank_reconciliation(db)
    assert rec["orders_unmatched_total"] == 2
    assert [g["aliexpress_order_id"] for g in rec["orders_unmatched"]] == ["800666"]


def test_non_aliexpress_debit_is_ignored(db, monkeypatch):
    _order(db, "12.34", days_ago=1)
    _run_sync(db, monkeypatch, [_tx(1, -1234, name="Amazon")])
    r = bank_sync_service.match_aliexpress_orders(db)
    assert r == {"checked": 0, "matched": 0}


def test_date_window_excludes_old_orders(db, monkeypatch):
    _order(db, "12.34", days_ago=30)
    _run_sync(db, monkeypatch, [_tx(1, -1234)])
    assert bank_sync_service.match_aliexpress_orders(db)["matched"] == 0


class _FakeEbay:
    def __init__(self, payouts):
        self._payouts = payouts

    async def get_payouts(self, *, days: int = 90, max_pages: int = 5):
        return self._payouts


def _payout(i: int, value: str, days_ago: float, status: str = "SUCCEEDED") -> dict:
    return {"payoutId": f"po-{i}", "payoutStatus": status,
            "amount": {"value": value, "currency": "EUR"},
            "payoutDate": _iso(days_ago)}


def _run_payouts(db, monkeypatch, tmp_path, payouts: list[dict]) -> dict:
    # Der Payout-Sync baut wie finance_service den ECHTEN Client direkt
    # (Factory ist auf dem VPS bewusst gemockt) -> Klasse + Creds patchen.
    from app.config import get_settings
    from app.integrations import ebay as ebay_mod
    s = get_settings()
    monkeypatch.setattr(s, "ebay_client_id", "test-cid")
    monkeypatch.setattr(s, "ebay_refresh_token", "test-rt")
    monkeypatch.setattr(ebay_mod, "RealEbayClient", lambda settings: _FakeEbay(payouts))
    monkeypatch.setattr(bank_sync_service, "PAYOUT_CACHE", tmp_path / "po.json")
    return asyncio.run(bank_sync_service.sync_ebay_payouts(db))


def test_payout_matches_ebay_credit(db, monkeypatch, tmp_path):
    _run_sync(db, monkeypatch, [_tx(1, 12345, name="eBay Commerce", days_ago=1)])
    r = _run_payouts(db, monkeypatch, tmp_path, [_payout(1, "123.45", days_ago=2)])
    assert r == {"payouts": 1, "payouts_matched": 1, "payouts_open": 0}
    tx = db.scalar(select(BankTransaction).where(BankTransaction.bank_ref == "kontist:tx-1"))
    assert tx.status == "matched" and tx.match_info["kind"] == "ebay_payout"
    assert tx.match_info["payout_id"] == "po-1"
    # Folgelauf: bereits zugeordnet zaehlt weiter als wiedergefunden, nichts doppelt
    r2 = _run_payouts(db, monkeypatch, tmp_path, [_payout(1, "123.45", days_ago=2)])
    assert r2 == {"payouts": 1, "payouts_matched": 1, "payouts_open": 0}
    cache = bank_sync_service.ebay_payout_summary()
    assert cache["payouts_matched"] == 1 and cache["payouts_open"] == []


def test_payout_without_credit_alerts_after_grace(db, monkeypatch, tmp_path):
    r = _run_payouts(db, monkeypatch, tmp_path, [
        _payout(1, "50.00", days_ago=9),   # alt + fehlt -> Alarm
        _payout(2, "60.00", days_ago=1),   # jung -> Karenz
    ])
    assert r["payouts_open"] == 1
    cache = bank_sync_service.ebay_payout_summary()
    assert [p["payout_id"] for p in cache["payouts_open"]] == ["po-1"]


def test_equal_payouts_one_credit_stays_open(db, monkeypatch, tmp_path):
    # Zwei gleichhohe Auszahlungen, eine Gutschrift: mehrdeutig -> offen lassen.
    _run_sync(db, monkeypatch, [_tx(1, 5000, name="eBay Commerce", days_ago=4)])
    r = _run_payouts(db, monkeypatch, tmp_path, [
        _payout(1, "50.00", days_ago=9), _payout(2, "50.00", days_ago=9)])
    assert r["payouts_matched"] == 0 and r["payouts_open"] == 2
    tx = db.scalar(select(BankTransaction).where(BankTransaction.bank_ref == "kontist:tx-1"))
    assert tx.status == "pending"


def test_one_payout_two_equal_credits_stays_open(db, monkeypatch, tmp_path):
    # Umkehr-Mehrdeutigkeit: eine Auszahlung, zwei gleichhohe Gutschriften.
    _run_sync(db, monkeypatch, [_tx(1, 5000, name="eBay Commerce", days_ago=1),
                                _tx(2, 5000, name="eBay S.a r.l.", days_ago=2)])
    r = _run_payouts(db, monkeypatch, tmp_path, [_payout(1, "50.00", days_ago=3)])
    assert r["payouts_matched"] == 0
    assert all(t.status == "pending" for t in db.scalars(select(BankTransaction)))


def test_failed_payout_neither_matches_nor_alarms(db, monkeypatch, tmp_path):
    # Gescheiterte Auszahlung: kein Geldeingang zu erwarten -> darf weder
    # alarmieren noch die Ersatz-Auszahlung als Rivale blockieren.
    _run_sync(db, monkeypatch, [_tx(1, 8750, name="eBay Commerce", days_ago=1)])
    r = _run_payouts(db, monkeypatch, tmp_path, [
        _payout(1, "87.50", days_ago=9, status="TERMINAL_FAILED"),
        _payout(2, "87.50", days_ago=2)])
    assert r == {"payouts": 1, "payouts_matched": 1, "payouts_open": 0}


def test_customer_transfer_with_ebay_purpose_not_pooled(db, monkeypatch, tmp_path):
    # Kunden-Ueberweisung mit "eBay" im Verwendungszweck ist KEINE Auszahlung.
    _run_sync(db, monkeypatch, [
        _tx(1, 4567, name="Max Mustermann", days_ago=1, purpose="eBay Artikel 123")])
    r = _run_payouts(db, monkeypatch, tmp_path, [_payout(1, "45.67", days_ago=8)])
    assert r["payouts_matched"] == 0 and r["payouts_open"] == 1


def test_payout_ignores_non_ebay_credit(db, monkeypatch, tmp_path):
    _run_sync(db, monkeypatch, [_tx(1, 12345, name="Finanzamt", days_ago=1)])
    r = _run_payouts(db, monkeypatch, tmp_path, [_payout(1, "123.45", days_ago=9)])
    assert r["payouts_matched"] == 0 and r["payouts_open"] == 1


def _run_archive(db, monkeypatch, tmp_path, calls: list | None = None) -> dict:
    async def fake_csv(date_from: str, date_to: str) -> str:
        if calls is not None:
            calls.append(date_from)
        return "Datum;Name;Betrag\n2026-01-01;Test;-1,00\n"
    monkeypatch.setattr(kontist, "fetch_transactions_csv", fake_csv)
    monkeypatch.setattr(bank_sync_service, "ARCHIVE_DIR", tmp_path / "archiv")
    return asyncio.run(bank_sync_service.archive_bank_csv(db))


def test_archive_freezes_closed_months(db, monkeypatch, tmp_path):
    _run_sync(db, monkeypatch, [_tx(1, -1000, days_ago=65)])   # Startmonat ~2 zurueck
    r = _run_archive(db, monkeypatch, tmp_path)
    assert r["archiv_monate"] >= 3 and r["archiv_neu"] >= 2
    manifest = bank_sync_service._load_manifest()
    frozen_names = {e["file"] for e in manifest}
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    current = f"kontist_{now.year:04d}-{now.month:02d}.csv"
    assert current not in frozen_names, "laufender Monat darf NICHT eingefroren sein"
    assert (tmp_path / "archiv" / current).exists()
    status = bank_sync_service.archive_status()
    assert status["monate"] == r["archiv_monate"]
    assert status["eingefroren"] == r["archiv_neu"]


def test_archive_never_refetches_frozen_months(db, monkeypatch, tmp_path):
    _run_sync(db, monkeypatch, [_tx(1, -1000, days_ago=65)])
    _run_archive(db, monkeypatch, tmp_path)
    calls: list = []
    r2 = _run_archive(db, monkeypatch, tmp_path, calls=calls)
    assert r2["archiv_neu"] == 0
    assert len(calls) == 1, "nur der laufende Monat darf neu geholt werden"
    assert "archiv_warnungen" not in r2


def test_archive_detects_tampering(db, monkeypatch, tmp_path):
    _run_sync(db, monkeypatch, [_tx(1, -1000, days_ago=65)])
    _run_archive(db, monkeypatch, tmp_path)
    frozen_file = sorted((tmp_path / "archiv").glob("kontist_*.csv"))[0]
    frozen_file.write_text("MANIPULIERT", encoding="utf-8")
    r = _run_archive(db, monkeypatch, tmp_path)
    assert any(frozen_file.name in w for w in r.get("archiv_warnungen", [])), \
        "Manipulation an eingefrorener Datei muss gemeldet werden"


def test_reconciliation_lists_open_items(db, monkeypatch):
    _order(db, "9.00", days_ago=4, ae_id="800333")   # alt + ohne Abbuchung -> offen
    _order(db, "8.00", days_ago=1, ae_id="800444")   # jung -> Karenz, NICHT gelistet
    _run_sync(db, monkeypatch, [
        _tx(1, -555),                    # AliExpress-Abbuchung ohne Bestellung
        _tx(2, 300),                     # AliExpress-Erstattung
        _tx(3, -999, name="Hosting"),    # fremd -> taucht nirgends auf
    ])
    rec = bank_sync_service.bank_reconciliation(db)
    assert rec["mirrored"] == 3
    assert rec["ali_total"] == 1 and rec["ali_matched"] == 0
    assert [t["amount_eur"] for t in rec["ali_open"]] == [-5.55]
    assert [g["aliexpress_order_id"] for g in rec["orders_unmatched"]] == ["800333"]
    assert [t["amount_eur"] for t in rec["refunds"]] == [3.0]
