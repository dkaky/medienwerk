"""Tests: Einkaufs- und Verkaufs-Haelfte desselben Vorgangs zusammenfuehren."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.models import Invoice, Listing, OrderAliexpress, Product, Sale
from app.services import order_merge


def _verkauf(db, *, name: str, tage_zurueck: int = 3, tx: str = "TX-1") -> Sale:
    p = Product(aliexpress_url=f"https://de.aliexpress.com/item/{tx}.html", aliexpress_id="ae" + tx)
    db.add(p); db.flush()
    listing = Listing(product_id=p.id, title_seo="Artikel", description="d",
                      listing_status="active", price_eur=Decimal("19.99"))
    db.add(listing); db.flush()
    sale = Sale(ebay_transaction_id=tx, listing_id=listing.id, buyer_name=name,
                price_eur=Decimal("19.99"), quantity=1, status="pending",
                sale_date=datetime.now(timezone.utc) - timedelta(days=tage_zurueck))
    db.add(sale); db.flush()
    # Verkaufs-Haelfte: haengt am Verkauf, hat Tracking, aber KEINE Bestellnummer
    db.add(OrderAliexpress(sale_id=sale.id, quantity=1, status="shipped",
                           tracking_number="TRACK" + tx, tracking_carrier="DHL",
                           order_date=sale.sale_date))
    db.commit()
    return sale


def _einkauf(db, *, empfaenger: str, ref: str, tage_zurueck: int = 2) -> OrderAliexpress:
    """Einkaufs-Haelfte: Bestellnummer + Beleg, aber ohne Verkaufsbezug."""
    order = OrderAliexpress(aliexpress_order_id=ref, quantity=1, status="delivered",
                            cost_cny=Decimal("9.99"),
                            order_date=datetime.now(timezone.utc) - timedelta(days=tage_zurueck))
    db.add(order); db.flush()
    db.add(Invoice(type="aliexpress_purchase", reference_id=ref, order_id=order.id,
                   is_original=True, file_path="/tmp/x.png",
                   receipt_data={"ship_to": empfaenger, "total": 9.99}))
    db.commit()
    return order


def test_eindeutiges_paar_wird_zusammengefuehrt(db):
    sale = _verkauf(db, name="Michael Dummann", tx="TX-A")
    kauf = _einkauf(db, empfaenger="Michael Dummann, Am Schlossgraben 14a", ref="3074619661272059")

    vorschau = order_merge.fuehre_zusammen(db)
    assert vorschau["vorschau"] is True and vorschau["wuerde_verknuepfen"] == 1

    r = order_merge.fuehre_zusammen(db, anwenden=True)
    assert r["verknuepft"] == 1
    db.refresh(kauf)
    assert kauf.sale_id == sale.id           # Einkauf haengt jetzt am Verkauf
    assert kauf.tracking_number == "TRACKTX-A"   # Sendungsnummer uebernommen
    # der Verkauf gehoert nur noch EINEM Datensatz
    haelften = [o for o in db.query(OrderAliexpress).all() if o.sale_id == sale.id]
    assert len(haelften) == 1


def test_umlaute_und_bindestriche_stoeren_nicht(db):
    """Der Beleg schreibt „Vorname Nachname, Strasse" – Umlaute kommen dort teils
    ohne Punkte an, eBay schreibt sie mit. Beides muss zusammenfinden."""
    _verkauf(db, name="Jürgen Müller-Lüdenscheidt", tx="TX-B")
    _einkauf(db, empfaenger="Jurgen Muller-Ludenscheidt, Hauptstr. 3", ref="3074619661272060")
    assert order_merge.fuehre_zusammen(db)["wuerde_verknuepfen"] == 1


def test_namensreihenfolge_ist_egal(db):
    _verkauf(db, name="Dummann Michael", tx="TX-B2")
    _einkauf(db, empfaenger="Michael Dummann, Am Schlossgraben 14a", ref="3074619661272070")
    assert order_merge.fuehre_zusammen(db)["wuerde_verknuepfen"] == 1


def test_mehrdeutiges_wird_nicht_geraten(db):
    """Zwei Verkaeufe an denselben Kaeufer im Zeitfenster -> nicht zuordnen."""
    _verkauf(db, name="Anna Schmidt", tx="TX-C1", tage_zurueck=3)
    _verkauf(db, name="Anna Schmidt", tx="TX-C2", tage_zurueck=4)
    _einkauf(db, empfaenger="Anna Schmidt, Weg 1", ref="3074619661272061")

    r = order_merge.fuehre_zusammen(db)
    assert r["wuerde_verknuepfen"] == 0
    assert any("nicht eindeutig" in u["grund"] for u in r["unklar"])


def test_zu_alter_einkauf_wird_nicht_zugeordnet(db):
    """Ein Einkauf Monate nach dem Verkauf gehoert nicht dazu."""
    _verkauf(db, name="Peter Lang", tx="TX-D", tage_zurueck=200)
    _einkauf(db, empfaenger="Peter Lang, Gasse 2", ref="3074619661272062", tage_zurueck=1)
    r = order_merge.fuehre_zusammen(db)
    assert r["wuerde_verknuepfen"] == 0
    assert any("Zeitfenster" in u["grund"] for u in r["unklar"])


def test_ohne_gelesenen_empfaenger_keine_zuordnung(db):
    _verkauf(db, name="Klara Weiss", tx="TX-E")
    order = OrderAliexpress(aliexpress_order_id="3074619661272063", quantity=1,
                            status="delivered", order_date=datetime.now(timezone.utc))
    db.add(order); db.flush()
    db.add(Invoice(type="aliexpress_purchase", reference_id="3074619661272063",
                   order_id=order.id, is_original=True, file_path="/tmp/y.png",
                   receipt_data={"total": 5.0}))       # kein ship_to
    db.commit()
    r = order_merge.fuehre_zusammen(db)
    assert r["wuerde_verknuepfen"] == 0
    assert any("Empfaenger" in u["grund"] for u in r["unklar"])


def test_verkauf_ganz_ohne_bestell_datensatz_wird_gefunden(db):
    """Viele Verkaeufe haben gar keine Verkaufs-Haelfte – auch die muessen
    ihren Einkauf bekommen (sonst bleiben 388 Verkaeufe aussen vor)."""
    p = Product(aliexpress_url="https://de.aliexpress.com/item/solo.html", aliexpress_id="aesolo")
    db.add(p); db.flush()
    listing = Listing(product_id=p.id, title_seo="Solo", description="d",
                      listing_status="active", price_eur=Decimal("14.99"))
    db.add(listing); db.flush()
    sale = Sale(ebay_transaction_id="TX-SOLO", listing_id=listing.id, buyer_name="Ute Oberreich",
                price_eur=Decimal("14.99"), quantity=1, status="pending",
                sale_date=datetime.now(timezone.utc) - timedelta(days=3))
    db.add(sale)
    db.commit()
    kauf = _einkauf(db, empfaenger="Ute Oberreich, Musterweg 5", ref="3074619661272099")

    r = order_merge.fuehre_zusammen(db, anwenden=True)
    assert r["verknuepft"] == 1
    db.refresh(kauf)
    assert kauf.sale_id == sale.id


def test_bereits_versorgter_verkauf_wird_nicht_erneut_zugeordnet(db):
    """Haengt schon ein echter Einkauf am Verkauf, ist er tabu.

    (sale_id ist in der DB eindeutig – ein Verkauf gehoert immer genau einem
    Bestell-Datensatz, deshalb hier direkt ein vollstaendiger Datensatz.)
    """
    p = Product(aliexpress_url="https://de.aliexpress.com/item/dopp.html", aliexpress_id="aedopp")
    db.add(p); db.flush()
    listing = Listing(product_id=p.id, title_seo="Doppelt", description="d",
                      listing_status="active", price_eur=Decimal("19.99"))
    db.add(listing); db.flush()
    sale = Sale(ebay_transaction_id="TX-DOPP", listing_id=listing.id, buyer_name="Doppelt Belegt",
                price_eur=Decimal("19.99"), quantity=1, status="pending",
                sale_date=datetime.now(timezone.utc) - timedelta(days=3))
    db.add(sale); db.flush()
    db.add(OrderAliexpress(sale_id=sale.id, aliexpress_order_id="3074619661272111",
                           quantity=1, status="delivered"))
    db.commit()
    _einkauf(db, empfaenger="Doppelt Belegt, Weg 9", ref="3074619661272112")

    r = order_merge.fuehre_zusammen(db)
    assert r["wuerde_verknuepfen"] == 0


def test_einzelner_nachname_reicht_nicht(db):
    """„Repp" allein ist als Schluessel zu schwach – mehrere Kaeufer koennen so heissen."""
    _verkauf(db, name="Repp", tx="TX-EIN")
    _einkauf(db, empfaenger="Repp, Bahnhofstr. 2", ref="3074619661272120")
    r = order_merge.fuehre_zusammen(db)
    assert r["wuerde_verknuepfen"] == 0
    assert any("Namensteil" in u["grund"] for u in r["unklar"])
