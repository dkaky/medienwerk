"""Welche Kaeufe der Beleg-Sammler vorgelegt bekommt (list_originals_todo).

Die Arbeitsliste liess frueher nur "shipped"/"delivered" durch — Annahme:
„AliExpress erstellt den Beleg erst ab Versand". Am 16.08.2026 direkt gegen
AliExpress gemessen: FALSCH und teuer.

* Bestellungen im Status "ordered" (14./15.08.) boten den Beleg BEREITS an.
* Aeltere abgeschlossene Bestellungen (Mai/Juli) boten ihn NICHT MEHR an.

Das Fenster ist also FRUEH. Der Filter hielt genau die Kaeufe zurueck, bei denen
der Beleg noch zu holen war — bis er unwiederbringlich weg war. Ein Kauf ohne
Beleg kostet den Sammler ~4 s und wird sauber uebersprungen; ein verpasster Beleg
ist dauerhaft verloren. Deshalb: im Zweifel vorlegen.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import OrderAliexpress
from app.services import invoice_service

_lfd = iter(range(1, 10_000))


def _kauf(db, *, status, ref=None):
    # Datum RELATIV zu heute, nicht fest. Vorher stand hier der 15.08.2026 - am
    # 29.08.2026 war der Kauf damit 15 Tage alt und fiel aus dem 14-Tage-Fenster
    # von list_originals_todo. Sechs Tests kippten, ohne dass sich eine Zeile Code
    # geaendert haette: der Test alterte, nicht das Programm.
    o = OrderAliexpress(aliexpress_order_id=ref or f"307{next(_lfd):013d}",
                        status=status,
                        order_date=datetime.now(timezone.utc) - timedelta(days=1))
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _refs(db):
    todo = invoice_service.list_originals_todo(db)
    return {t["aliexpress_order_id"] for t in todo["aliexpress"]}


def test_frisch_bestellt_wird_vorgelegt(db):
    """Der eigentliche Fund: 38 Kaeufe vom 14./15.08. fehlten komplett."""
    o = _kauf(db, status="ordered")

    assert o.aliexpress_order_id in _refs(db)


@pytest.mark.parametrize("status", ["ordered", "shipped", "delivered", "processing"])
def test_alle_lebenden_stati_werden_vorgelegt(db, status):
    o = _kauf(db, status=status)

    assert o.aliexpress_order_id in _refs(db)


@pytest.mark.parametrize("status", ["cancelled", "canceled", "refunded", "storniert"])
def test_rueckabgewickelte_nicht(db, status):
    """Dort gibt es nichts zu holen – nur verlorene Zeit."""
    o = _kauf(db, status=status)

    assert o.aliexpress_order_id not in _refs(db)


def test_unbekannter_status_wird_vorgelegt(db):
    """Im Zweifel hinschauen: ein verpasster Beleg ist dauerhaft weg."""
    o = _kauf(db, status="irgendwas_neues_von_aliexpress")

    assert o.aliexpress_order_id in _refs(db)


def test_ebay_platzhalter_bleiben_draussen(db):
    o = _kauf(db, status="ordered", ref="EBAY-12345")

    assert o.aliexpress_order_id not in _refs(db)


# --------------------------------------------------------- Zeitfenster (14 Tage)
def _kauf_mit_datum(db, *, tage_alt, status="ordered"):
    o = OrderAliexpress(aliexpress_order_id=f"307{next(_lfd):013d}", status=status,
                        order_date=datetime.now(timezone.utc) - timedelta(days=tage_alt))
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def test_frischer_kauf_ist_drin(db):
    o = _kauf_mit_datum(db, tage_alt=2)

    assert o.aliexpress_order_id in _refs(db)


def test_alter_kauf_faellt_raus(db):
    """Bei AliExpress ist der Beleg dort laengst weg – jeder Besuch kostet nur ~4 s."""
    o = _kauf_mit_datum(db, tage_alt=60)

    assert o.aliexpress_order_id not in _refs(db)


def test_grenze_liegt_bei_14_tagen(db):
    drin = _kauf_mit_datum(db, tage_alt=13)
    draussen = _kauf_mit_datum(db, tage_alt=15)

    refs = _refs(db)
    assert drin.aliexpress_order_id in refs
    assert draussen.aliexpress_order_id not in refs


def test_aufholen_sieht_wieder_alles(db):
    """tage=None fuer einen einmaligen Aufhol-Lauf."""
    alt = _kauf_mit_datum(db, tage_alt=90)

    todo = invoice_service.list_originals_todo(db, tage=None)

    assert alt.aliexpress_order_id in {t["aliexpress_order_id"] for t in todo["aliexpress"]}


def test_ohne_datum_wird_vorgelegt(db):
    """Nicht datierbar -> im Zweifel hinschauen, ein verpasster Beleg ist weg."""
    o = OrderAliexpress(aliexpress_order_id=f"307{next(_lfd):013d}", status="ordered")
    db.add(o)
    db.commit()

    assert o.aliexpress_order_id in _refs(db)


# ------------------------------------- Bestellungen ohne Eintrag in der Belegablage
def test_zaehlt_nach_jahr_ohne_etwas_anzulegen(db):
    """Vorschau: erst sehen, was man sich einhandelt."""
    from app.models import Invoice
    _kauf_mit_datum(db, tage_alt=5)                       # 2026
    alt = OrderAliexpress(aliexpress_order_id=f"307{next(_lfd):013d}", status="delivered",
                          order_date=datetime(2025, 3, 1, tzinfo=timezone.utc))
    db.add(alt); db.commit()

    r = invoice_service.fehlende_belegzeilen(db)

    assert r["ohne_zeile_gesamt"] == 2
    assert r["nach_jahr"]["2025"] == 1
    assert r["angelegt"] == 0
    assert db.scalar(invoice_service.select(invoice_service.func.count())
                     .select_from(Invoice)) == 0


def test_legt_nur_das_gewaehlte_jahr_an(db):
    """Nur die Kaeufe dieses Jahres sind fuer die Buchhaltung interessant."""
    from app.models import Invoice
    neu = _kauf_mit_datum(db, tage_alt=5)
    alt = OrderAliexpress(aliexpress_order_id=f"307{next(_lfd):013d}", status="delivered",
                          order_date=datetime(2025, 3, 1, tzinfo=timezone.utc))
    db.add(alt); db.commit()

    r = invoice_service.fehlende_belegzeilen(db, jahr=datetime.now(timezone.utc).year,
                                             anwenden=True)

    assert r["angelegt"] == 1
    zeilen = db.scalars(invoice_service.select(Invoice)).all()
    assert [z.reference_id for z in zeilen] == [neu.aliexpress_order_id]


def test_stornierte_und_geloeschte_bleiben_draussen(db):
    _kauf_mit_datum(db, tage_alt=3, status="cancelled")

    r = invoice_service.fehlende_belegzeilen(db)

    assert r["ohne_zeile_gesamt"] == 0


def test_ebay_platzhalter_zaehlen_nicht(db):
    o = OrderAliexpress(aliexpress_order_id="EBAY-999", status="delivered",
                        order_date=datetime.now(timezone.utc))
    db.add(o); db.commit()

    assert invoice_service.fehlende_belegzeilen(db)["ohne_zeile_gesamt"] == 0


def test_kachel_zaehlt_dasselbe_wie_die_liste(db):
    """Die Kachel „Kaeufe ohne Beleg" muss zeigen, was der Klick darauf auftut.

    Frueher: Bestellzeilen minus Belegzeilen -> 301, beim Klick 22. Der Fehler kam
    aus OrderAliexpress: hunderte Zeilen ohne AliExpress-Bestellnummer, und mehrere
    Positionen teilen sich EINE Nummer und damit EINEN Beleg.
    """
    from app.models import Invoice

    o = _kauf_mit_datum(db, tage_alt=3)
    # Zwei Positionen derselben Bestellung -> trotzdem nur EIN Beleg
    db.add(OrderAliexpress(aliexpress_order_id=o.aliexpress_order_id, status="shipped",
                           order_date=datetime.now(timezone.utc)))
    # Verkauf, der nie bei AliExpress bestellt wurde -> gar keine Bestellnummer
    db.add(OrderAliexpress(aliexpress_order_id=None, status="pending"))
    db.add(Invoice(type="aliexpress_purchase", reference_id=o.aliexpress_order_id,
                   invoice_number=f"AE_{o.aliexpress_order_id}", is_original=False))
    db.commit()

    st = invoice_service.invoice_summary(db)
    liste = invoice_service.list_invoices(db, type="aliexpress_purchase")
    ohne_original = sum(1 for i in liste["invoices"] if not i["is_original"])

    assert st["purchases_missing"] == ohne_original == 1
