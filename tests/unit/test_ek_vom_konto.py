"""Der echte Einkaufspreis kommt vom KONTO, nicht aus einer Umrechnung.

Fund am 20.08.2026: ein Kauf stand mit 12,63 EUR in der Belegablage, abgebucht
wurden 12,58 EUR. Ursache: AliExpress meldet USD, das Programm rechnet mit einem
FEST eingetragenen Kurs (0,8765) um — die Bank nimmt ihren Tageskurs. Differenz
0,4 %, hochgerechnet auf den Jahres-Einkauf rund 46 EUR.

Zwei Folgen, beide hier abgesichert:
1. Der Abgleich scheiterte an der 2-Cent-Toleranz — bei Fremdwaehrung kann unser
   Euro-Wert gar nicht auf den Cent stimmen.
2. Sobald die Abbuchung zugeordnet ist, gilt IHR Betrag. Fuer die EUeR zaehlt
   ohnehin, was tatsaechlich abgeflossen ist (Abfluss-Prinzip).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.models import BankTransaction, OrderAliexpress
from app.services import bank_sync_service as bs

_lfd = iter(range(1, 10_000))


def _kauf(db, *, eur, tage_alt=0, quelle="api"):
    o = OrderAliexpress(
        aliexpress_order_id=f"307{next(_lfd):013d}", status="shipped",
        cost_cny=Decimal(str(eur)), cost_source=quelle,
        order_date=datetime.now(timezone.utc) - timedelta(days=tage_alt))
    db.add(o); db.commit(); db.refresh(o)
    return o


def _abbuchung(db, *, eur, zweck="Kartenzahlung", tage_alt=0):
    tx = BankTransaction(
        bank_ref=f"kontist:{next(_lfd)}", status="pending",
        amount=-abs(Decimal(str(eur))), counterparty_name="Aliexpress.com",
        description=zweck,
        transaction_date=datetime.now(timezone.utc) - timedelta(days=tage_alt))
    db.add(tx); db.commit(); db.refresh(tx)
    return tx


# ------------------------------------------------------- Toleranz bei Fremdwaehrung
def test_eurokauf_bleibt_auf_zwei_cent_genau():
    """Bei Euro gibt es nichts zu schaetzen – da darf nicht aufgeweicht werden."""
    tx = BankTransaction(amount=Decimal("-12.58"), description="Kartenzahlung")

    assert bs._betrags_toleranz(tx, Decimal("12.58")) == Decimal("0.02")


def test_fremdwaehrung_bekommt_luft():
    """Der Originalbetrag steht im Verwendungszweck – daran erkennt man den Fall."""
    tx = BankTransaction(amount=Decimal("-12.58"), description="[14.41 USD]")

    tol = bs._betrags_toleranz(tx, Decimal("12.58"))

    assert tol > Decimal("0.05")          # die reale Differenz von 5 Cent passt rein
    assert tol < Decimal("0.30")          # aber nicht so viel, dass alles passt


def test_der_reale_fall_wuerde_jetzt_zusammenfinden(db):
    """12,63 geschaetzt vs. 12,58 abgebucht — genau der Kauf vom 20.08."""
    o = _kauf(db, eur="12.63")
    tx = _abbuchung(db, eur="12.58", zweck="AliExpress [14.41 USD]")

    r = bs.match_gleicher_tag(db, anwenden=True)

    assert r["zugeordnet"] == 1
    db.refresh(o)
    assert db.get(BankTransaction, tx.id).order_id == o.id


def test_ohne_fremdwaehrung_faellt_er_durch(db):
    """Ohne den Hinweis auf Fremdwaehrung bleibt es streng – sonst raet man."""
    _kauf(db, eur="12.63")
    _abbuchung(db, eur="12.58", zweck="Kartenzahlung")

    assert bs.match_gleicher_tag(db, anwenden=True)["zugeordnet"] == 0


# --------------------------------------------------- echter Betrag statt Schaetzung
def test_ek_wird_auf_den_kontobetrag_korrigiert(db):
    o = _kauf(db, eur="12.63")
    _abbuchung(db, eur="12.58", zweck="AliExpress [14.41 USD]")

    r = bs.match_gleicher_tag(db, anwenden=True)

    db.refresh(o)
    assert Decimal(str(o.cost_cny)) == Decimal("12.58")
    assert o.cost_source == "bank"
    assert r["ek_korrigiert"] == 1


def test_handkorrektur_bleibt_unangetastet(db):
    """Was ein Mensch gesetzt hat, ueberschreibt keine Automatik."""
    o = _kauf(db, eur="12.63", quelle="manual")
    _abbuchung(db, eur="12.58", zweck="AliExpress [14.41 USD]")

    bs.match_gleicher_tag(db, anwenden=True)

    db.refresh(o)
    assert Decimal(str(o.cost_cny)) == Decimal("12.63")
    assert o.cost_source == "manual"


def test_bei_mehreren_bestellungen_wird_nichts_aufgeteilt(db):
    """Eine Abbuchung ueber zwei Bestellungen aufzuteilen waere geraten."""
    a = _kauf(db, eur="10.00")
    b = _kauf(db, eur="10.00")
    _abbuchung(db, eur="20.00", zweck="AliExpress [22.9 USD]")

    r = bs.match_gleicher_tag(db, anwenden=True)

    db.refresh(a); db.refresh(b)
    assert Decimal(str(a.cost_cny)) == Decimal("10.00")
    assert Decimal(str(b.cost_cny)) == Decimal("10.00")
    assert r.get("ek_korrigiert", 0) == 0


def test_backfill_fasst_bankwerte_nicht_mehr_an():
    """Sonst ueberschriebe die naechtliche Schaetzung den echten Betrag."""
    import inspect

    from app.services import order_service
    quelle = inspect.getsource(order_service.backfill_real_order_costs)
    assert '"bank"' in quelle, "Bank-Wert ist im Backfill nicht geschuetzt"


# --------------------------------------------------- Beleg schlaegt Konto nicht mehr
async def test_beleg_ueberschreibt_den_kontobetrag_nicht(db):
    """Der Beleg weist USD aus, die Bank hat Euro abgebucht.

    Frueher hat die Belegpruefung den Bank-Wert wieder ueberschrieben – der
    korrigierte Betrag waere beim naechsten Lauf still verloren gegangen.
    """
    from datetime import datetime as _dt

    from app.models import Invoice
    from app.services import purchase_invoice as pi

    o = _kauf(db, eur="12.58", quelle="bank")
    inv = Invoice(type="aliexpress_purchase", is_original=True, order_id=o.id,
                  # amount weicht ab -> die Pruefung laeuft wirklich in den
                  # Korrektur-Zweig (sonst steigt sie vorher aus und der Test
                  # wuerde auch ohne die Sicherung gruen sein).
                  reference_id="3070000000000001", amount=Decimal("12.00"),
                  invoice_date=_dt(2026, 8, 20), file_path="original_x.png",
                  receipt_data={"total": 12.63, "currency": "EUR"})
    db.add(inv); db.commit()

    r = await pi.pruefe_betraege(db, limit=5)

    db.refresh(o)
    assert Decimal(str(o.cost_cny)) == Decimal("12.58"), "Konto-Betrag wurde ueberschrieben"
    assert o.cost_source == "bank"
    assert r["geprueft"] >= 1


def test_nachgereichter_beleg_ueberschreibt_das_konto_nicht(db):
    """Auch der Sammler, der Belege nachtraegt, faellt nicht hinter das Konto zurueck."""
    from app.services import invoice_service

    o = _kauf(db, eur="12.58", quelle="bank")

    invoice_service.attach_original_receipt(
        db, file_bytes=b"\x89PNG\r\n\x1a\n-test", ext="png",
        invoice_type="aliexpress_purchase", order_id=o.id, amount=12.63,
        invoice_number="3070000000000002")

    db.refresh(o)
    assert Decimal(str(o.cost_cny)) == Decimal("12.58")
    assert o.cost_source == "bank"
