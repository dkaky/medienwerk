"""Belege: Dateiablage ohne Kollisionen + endgueltiges Loeschen selbst erfasster Ausgaben.

Zwei Anliegen, die zusammengehoeren:
- Jeder Beleg braucht seine EIGENE Datei. Vorher ergab sich der Pfad nur aus Typ,
  ref_id und Monat – bei manuellen Ausgaben ist ref_id die Kategorie, also lagen zwei
  Bewirtungsbelege desselben Monats auf derselben Datei und der zweite hat das Foto
  des ersten ueberschrieben (Datenverlust, gefunden 27.07.).
- Der Papierkorb loescht endgueltig (Nutzer-Entscheidung): Zeile UND Datei. Er darf
  deshalb nur dort greifen, wo Loeschen auch haelt – bei selbst erfassten Ausgaben.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.integrations.storage import LocalInvoiceStorage
from app.models import Invoice, OrderAliexpress, Product
from app.retry import PersistentError
from app.services import invoice_service

BEWIRTUNG = "Bewirtungsbeleg (§ 4 Abs. 5 EStG)"


# --- Ablage: jeder Beleg seine eigene Datei -------------------------------

def test_zwei_belege_derselben_kategorie_ueberschreiben_sich_nicht(tmp_path):
    """Kernfall: zwei Restaurantbesuche im selben Monat. Frueher zeigte hinterher
    BEIDE Eintraege dasselbe (zuletzt hochgeladene) Foto."""
    st = LocalInvoiceStorage(tmp_path)
    gemeinsam = dict(period="2026-07", file_type="betriebsausgabe",
                     ref_id="Bewirtungsbeleg __ 4 Abs. 5 E", ext="jpg")
    a = st.store(content=b"FOTO-QUITTUNG-ADLER", **gemeinsam)
    b = st.store(content=b"FOTO-QUITTUNG-BISTRO", **gemeinsam)

    assert a.file_path != b.file_path, "jeder Beleg braucht seine eigene Datei"
    assert st.read(a.file_path) == b"FOTO-QUITTUNG-ADLER", "das erste Foto muss erhalten bleiben"
    assert st.read(b.file_path) == b"FOTO-QUITTUNG-BISTRO"


def test_identischer_inhalt_wird_weiter_dedupliziert(tmp_path):
    """Die Dedup darf durch die Umstellung nicht verloren gehen."""
    st = LocalInvoiceStorage(tmp_path)
    gemeinsam = dict(period="2026-07", file_type="betriebsausgabe", ref_id="Abo", ext="pdf")
    a = st.store(content=b"RECHNUNG", **gemeinsam)
    b = st.store(content=b"RECHNUNG", **gemeinsam)
    assert a.file_path == b.file_path
    assert b.deduped is True


def test_datei_loeschen(tmp_path):
    st = LocalInvoiceStorage(tmp_path)
    f = st.store(content=b"x", period="2026-07", file_type="betriebsausgabe",
                 ref_id="Abo", ext="pdf")
    assert st.delete(f.file_path) is True
    assert not Path(f.file_path).exists()
    assert st.delete(f.file_path) is False, "zweimal loeschen ist kein Fehler"


def test_loeschen_bleibt_in_der_belegablage(tmp_path):
    """Sicherheitsnetz: nie ausserhalb des Beleg-Ordners loeschen."""
    st = LocalInvoiceStorage(tmp_path / "belege")
    fremd = tmp_path / "wichtig.txt"
    fremd.write_text("nicht anfassen")
    with pytest.raises(ValueError):
        st.delete(str(fremd))
    assert fremd.exists()


# --- Papierkorb im Dashboard ----------------------------------------------

def _ausgabe(client, *, betrag="48.00", inhalt=b"FOTO-A", kategorie=BEWIRTUNG):
    r = client.post("/api/v1/invoices/expense",
                    data={"datum": "2026-07-24", "amount": betrag, "category": kategorie,
                          "description": "Bewirtungsbeleg · Anlass: Einkaufskonditionen"},
                    files={"file": ("quittung.jpg", inhalt, "image/jpeg")})
    assert r.status_code == 201, r.text
    return r.json()


def test_zwei_bewirtungsbelege_behalten_ihr_eigenes_foto(client):
    """Ende zu Ende: zweimal im selben Monat essen, zwei verschiedene Quittungen."""
    a = _ausgabe(client, inhalt=b"FOTO-ADLER")
    b = _ausgabe(client, betrag="61.50", inhalt=b"FOTO-BISTRO")
    assert client.get(f"/api/v1/invoices/{a['id']}/download").content == b"FOTO-ADLER"
    assert client.get(f"/api/v1/invoices/{b['id']}/download").content == b"FOTO-BISTRO"


def test_selbst_erfasste_ausgabe_wird_komplett_geloescht(client, db):
    beleg = _ausgabe(client)
    pfad = db.get(Invoice, beleg["id"]).file_path
    assert Path(pfad).exists()

    r = client.delete(f"/api/v1/invoices/{beleg['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    assert r.json()["file_deleted"] is True

    assert db.get(Invoice, beleg["id"]) is None, "die Zeile muss weg sein"
    assert not Path(pfad).exists(), "die hochgeladene Datei muss weg sein"
    assert client.get(f"/api/v1/invoices/{beleg['id']}/download").status_code == 404


def test_loeschen_trifft_nur_den_gewaehlten_beleg(client, db):
    a = _ausgabe(client, inhalt=b"FOTO-ADLER")
    b = _ausgabe(client, betrag="61.50", inhalt=b"FOTO-BISTRO")
    client.delete(f"/api/v1/invoices/{a['id']}")
    assert db.get(Invoice, b["id"]) is not None
    assert client.get(f"/api/v1/invoices/{b['id']}/download").content == b"FOTO-BISTRO"


def test_aliexpress_kaufbeleg_laesst_sich_nicht_loeschen(client, db):
    """Der wuerde beim naechsten Bestell-Import sowieso wieder angelegt – und ohne ihn
    fehlt der Einkauf in der Gewinnrechnung. Deshalb serverseitig gesperrt."""
    from decimal import Decimal

    p = Product(aliexpress_url="https://de.aliexpress.com/item/del.html", aliexpress_id="aedel")
    db.add(p)
    db.flush()
    order = OrderAliexpress(aliexpress_order_id="AE-DEL-1", product_id=p.id, quantity=1,
                            cost_cny=Decimal("12.00"), status="ordered")
    db.add(order)
    db.commit()
    beleg = invoice_service.record_purchase_invoice(db, order_id=order.id)

    r = client.delete(f"/api/v1/invoices/{beleg['id']}")
    assert r.status_code == 400
    assert "Nur selbst erfasste Ausgaben" in r.json()["detail"]
    assert db.get(Invoice, beleg["id"]) is not None, "der Beleg muss stehen bleiben"


def test_verkaufsrechnung_laesst_sich_nicht_loeschen(client, db):
    from decimal import Decimal

    from app.models import Listing, Sale
    p = Product(aliexpress_url="https://de.aliexpress.com/item/sale.html", aliexpress_id="aesale")
    db.add(p)
    db.flush()
    listing = Listing(product_id=p.id, title_seo="Kette", description="d",
                      listing_status="active", price_eur=Decimal("19.99"))
    db.add(listing)
    db.flush()
    sale = Sale(ebay_transaction_id="TX-DEL-1", listing_id=listing.id, buyer_name="Max",
                price_eur=Decimal("19.99"), quantity=1, status="pending")
    db.add(sale)
    db.commit()
    beleg = invoice_service.generate_sale_invoice(db, sale_id=sale.id)

    assert client.delete(f"/api/v1/invoices/{beleg['id']}").status_code == 400
    assert db.get(Invoice, beleg["id"]) is not None


def test_unbekannter_beleg_gibt_404(client):
    assert client.delete("/api/v1/invoices/999999").status_code == 404


def test_geteilte_altdatei_wird_nicht_unter_dem_anderen_beleg_weggeloescht(client, db):
    """Altbestand: vor der Umstellung konnten sich zwei Ausgaben eine Datei teilen.
    Wird eine davon geloescht, muss die Datei fuer die andere stehen bleiben."""
    a = _ausgabe(client, inhalt=b"GETEILTES-FOTO")
    zweiter = db.get(Invoice, _ausgabe(client, betrag="12.00", inhalt=b"ANDERES")["id"])
    geteilt = db.get(Invoice, a["id"]).file_path
    zweiter.file_path = geteilt          # Zustand von frueher nachstellen
    db.commit()

    r = client.delete(f"/api/v1/invoices/{a['id']}")
    assert r.status_code == 200
    assert r.json()["file_deleted"] is False, "Datei gehoert noch dem anderen Beleg"
    assert Path(geteilt).exists()
    assert client.get(f"/api/v1/invoices/{zweiter.id}/download").content == b"GETEILTES-FOTO"


def test_geloeschte_ausgabe_faellt_aus_liste_und_summen(client, db):
    a = _ausgabe(client, betrag="48.00")
    _ausgabe(client, betrag="61.50", inhalt=b"FOTO-B")
    vorher = client.get("/api/v1/invoices/list").json()
    assert vorher["stats"]["other_expenses"]["sum"] == pytest.approx(109.50)

    client.delete(f"/api/v1/invoices/{a['id']}")
    nachher = client.get("/api/v1/invoices/list").json()
    assert nachher["stats"]["other_expenses"]["sum"] == pytest.approx(61.50)
    assert a["id"] not in [i["id"] for i in nachher["invoices"]]
    assert db.scalar(select(Invoice).where(Invoice.id == a["id"])) is None


def test_service_meldet_klartext_statt_absturz(db):
    with pytest.raises(PersistentError, match="nicht gefunden"):
        invoice_service.delete_manual_expense(db, invoice_id=424242)


# --- Datei austauschen (Reparatur der ueberschriebenen Quittungen) ---------

def _tausche(client, invoice_id, inhalt, name="neu.jpg"):
    return client.post(f"/api/v1/invoices/{invoice_id}/replace-file",
                       files={"file": (name, inhalt, "image/jpeg")})


def test_datei_austauschen_laesst_den_eintrag_unangetastet(client, db):
    beleg = _ausgabe(client, betrag="48.00", inhalt=b"FALSCHES-FOTO")
    r = _tausche(client, beleg["id"], b"RICHTIGES-FOTO")
    assert r.status_code == 200, r.text
    assert r.json()["replaced"] is True

    inv = db.get(Invoice, beleg["id"])
    assert float(inv.amount) == pytest.approx(48.00), "Betrag darf sich nicht aendern"
    assert "Anlass: Einkaufskonditionen" in (inv.note or ""), "Anlass bleibt stehen"
    assert inv.invoice_number == beleg["invoice_number"]
    assert client.get(f"/api/v1/invoices/{beleg['id']}/download").content == b"RICHTIGES-FOTO"


def test_alte_datei_verschwindet_beim_austauschen(client, db):
    beleg = _ausgabe(client, inhalt=b"FALSCHES-FOTO")
    alt = db.get(Invoice, beleg["id"]).file_path
    _tausche(client, beleg["id"], b"RICHTIGES-FOTO")
    assert not Path(alt).exists()


def test_reparatur_dreier_belege_die_sich_eine_datei_teilten(client, db):
    """Der echte Vorfall: drei Bewirtungsbelege zeigten alle dasselbe Foto, weil der
    letzte Upload die frueheren ueberschrieben hatte. Beim Nachtragen der richtigen
    Quittungen darf die geteilte Datei nicht unter den anderen weggezogen werden."""
    ids = [_ausgabe(client, betrag=f"4{n}.00", inhalt=f"UPLOAD-{n}".encode())["id"]
           for n in (1, 2, 3)]
    geteilt = db.get(Invoice, ids[2]).file_path
    for i in ids:                       # Zustand von vor dem Fix nachstellen
        db.get(Invoice, i).file_path = geteilt
    db.commit()
    assert all(client.get(f"/api/v1/invoices/{i}/download").content
               == b"UPLOAD-3" for i in ids), "Ausgangslage: alle zeigen dasselbe"

    _tausche(client, ids[0], b"QUITTUNG-1")
    _tausche(client, ids[1], b"QUITTUNG-2")

    assert client.get(f"/api/v1/invoices/{ids[0]}/download").content == b"QUITTUNG-1"
    assert client.get(f"/api/v1/invoices/{ids[1]}/download").content == b"QUITTUNG-2"
    assert client.get(f"/api/v1/invoices/{ids[2]}/download").content == b"UPLOAD-3", \
        "der dritte Beleg behaelt sein Foto"
    assert len({db.get(Invoice, i).file_path for i in ids}) == 3, "jetzt drei eigene Dateien"


def test_dieselbe_datei_nochmal_hochladen_bricht_nichts(client, db):
    beleg = _ausgabe(client, inhalt=b"FOTO")
    r = _tausche(client, beleg["id"], b"FOTO")
    assert r.status_code == 200
    assert r.json()["old_file_deleted"] is False, "Datei ist ja dieselbe – nicht loeschen"
    assert client.get(f"/api/v1/invoices/{beleg['id']}/download").content == b"FOTO"


def test_austauschen_nur_bei_selbst_erfassten_ausgaben(client, db):
    from decimal import Decimal

    p = Product(aliexpress_url="https://de.aliexpress.com/item/rep.html", aliexpress_id="aerep")
    db.add(p)
    db.flush()
    order = OrderAliexpress(aliexpress_order_id="AE-REP-1", product_id=p.id, quantity=1,
                            cost_cny=Decimal("5.00"), status="ordered")
    db.add(order)
    db.commit()
    beleg = invoice_service.record_purchase_invoice(db, order_id=order.id)
    r = _tausche(client, beleg["id"], b"FOTO")
    assert r.status_code == 400
    assert "selbst erfassten Ausgaben" in r.json()["detail"]


def test_austauschen_lehnt_fremde_dateitypen_ab(client):
    beleg = _ausgabe(client)
    r = client.post(f"/api/v1/invoices/{beleg['id']}/replace-file",
                    files={"file": ("quittung.txt", b"kein Bild", "text/plain")})
    assert r.status_code == 400
    assert "PDF, JPG, PNG" in r.json()["detail"]


def test_austauschen_bei_unbekanntem_beleg_gibt_404(client):
    assert _tausche(client, 999999, b"FOTO").status_code == 404
