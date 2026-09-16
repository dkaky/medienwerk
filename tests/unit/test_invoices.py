"""Tests fuer die Belege-Funktion (Verkaufsrechnung mit/ohne USt + Kaufbeleg)."""
from __future__ import annotations

from decimal import Decimal

from app.models import Listing, OrderAliexpress, Product, Sale
from app.services import invoice_service


def _sale(db, *, tx="TX-INV-1", price="19.99"):
    p = Product(aliexpress_url=f"https://de.aliexpress.com/item/{tx}.html", aliexpress_id="ae" + tx)
    db.add(p)
    db.flush()
    listing = Listing(product_id=p.id, title_seo="Frankreich Halskette Edelstahl",
                      description="d", listing_status="active", price_eur=Decimal(price))
    db.add(listing)
    db.flush()
    sale = Sale(ebay_transaction_id=tx, listing_id=listing.id, buyer_name="Max Muster",
                delivery_address={"street": "Weg 1", "postal": "50667", "city": "Köln", "country": "DE"},
                price_eur=Decimal(price), quantity=1, status="pending")
    db.add(sale)
    db.commit()
    return sale


def test_generate_sale_invoice_is_idempotent_and_shows_vat(db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "ust_regelbesteuerung_ab", "2026-01-01")
    sale = _sale(db)
    r = invoice_service.generate_sale_invoice(db, sale_id=sale.id)
    assert r["created"] is True
    assert r["type"] == "ebay_sales"
    # Praefix kommt aus der Konfiguration, nicht fest im Test verdrahtet
    from app.config import get_settings
    assert r["invoice_number"].startswith(f"{get_settings().invoice_number_prefix}-")
    assert r["currency"] == "EUR"

    # zweiter Aufruf erzeugt NICHT neu (idempotent)
    again = invoice_service.generate_sale_invoice(db, sale_id=sale.id)
    assert again["created"] is False
    assert again["invoice_number"] == r["invoice_number"]

    # Regelbesteuert: Netto + 19 % USt + Brutto, kein § 19-Hinweis
    data, name, ctype = invoice_service.read_invoice_file(db, invoice_id=r["id"])
    text = data.decode("utf-8")
    assert "Nettobetrag" in text and "16,80 €" in text
    assert "zzgl. 19 % USt" in text and "3,19 €" in text
    assert "Gesamtbetrag (brutto)" in text and "19,99 €" in text
    assert "§ 19" not in text and "Leistungsdatum" in text
    assert "text/html" in ctype
    assert name.endswith(".html")


def test_record_purchase_invoice(db):
    p = Product(aliexpress_url="https://de.aliexpress.com/item/ord.html", aliexpress_id="aeord")
    db.add(p)
    db.flush()
    order = OrderAliexpress(aliexpress_order_id="AE12345", product_id=p.id, quantity=1,
                            cost_cny=Decimal("40.00"), status="ordered")
    db.add(order)
    db.commit()
    r = invoice_service.record_purchase_invoice(db, order_id=order.id)
    assert r["created"] is True
    assert r["type"] == "aliexpress_purchase"
    assert r["currency"] == "EUR"          # nur EUR, kein CNY mehr
    assert float(r["amount"]) == 40.0


def _purchase(db, *, order_id="AE-P-1", title=None, title_raw=None):
    p = Product(aliexpress_url=f"https://de.aliexpress.com/item/{order_id}.html",
                aliexpress_id="ae" + order_id, title_raw=title_raw)
    db.add(p)
    db.flush()
    order = OrderAliexpress(aliexpress_order_id=order_id, product_id=p.id, quantity=1,
                            cost_cny=Decimal("24.69"), status="ordered",
                            invoice_data=({"title": title} if title else None))
    db.add(order)
    db.commit()
    invoice_service.record_purchase_invoice(db, order_id=order.id)
    return order_id


def _aliexpress_row(db, order_id):
    rows = invoice_service.list_invoices(db)["invoices"]
    return next(i for i in rows if i["type"] == "aliexpress_purchase"
               and i["reference_id"] == order_id)


def test_belegliste_zeigt_aliexpress_produktnamen(db):
    """Nutzerwunsch: in der Referenz-Spalte der Produktname statt der nackten
    Bestellnummer (die steht schon in der Beleg-Nr)."""
    _purchase(db, order_id="AE-NAME-1",
              title="Auto Detailing Set Mikrofasertücher 12er Pack")
    row = _aliexpress_row(db, "AE-NAME-1")
    assert row["product_name"] == "Auto Detailing Set Mikrofasertücher 12er Pack"


def test_produktname_faellt_auf_produkttitel_zurueck(db):
    """Alt-Bestellung ohne invoice_data-Titel -> Originaltitel aus dem Produkt."""
    _purchase(db, order_id="AE-NAME-2", title=None,
              title_raw="Edelstahl Halskette Frankreich Anhänger")
    row = _aliexpress_row(db, "AE-NAME-2")
    assert row["product_name"] == "Edelstahl Halskette Frankreich Anhänger"


def test_produktname_leer_wenn_kein_titel_bekannt(db):
    """Ohne jeden Titel bleibt product_name leer -> Frontend zeigt die Bestellnummer."""
    _purchase(db, order_id="AE-NAME-3", title=None, title_raw=None)
    row = _aliexpress_row(db, "AE-NAME-3")
    assert row["product_name"] is None
    assert row["reference_id"] == "AE-NAME-3"


def test_invoice_summary_tracks_missing(db):
    sale = _sale(db, tx="TX-INV-2")
    st = invoice_service.invoice_summary(db)
    assert st["sales_total"] >= 1
    assert st["sales_missing"] >= 1          # noch keine Rechnung
    invoice_service.generate_sale_invoice(db, sale_id=sale.id)
    st2 = invoice_service.invoice_summary(db)
    assert st2["sales_invoices"]["count"] >= 1
    assert st2["sales_invoices"]["sum"] >= 19.99


# --- Bewirtungsbeleg: Anlass-Vorschlaege ----------------------------------
# Das Programm liefert VORSCHLAEGE zum Auswaehlen (der Nutzer weiss, was wirklich war).
# Wichtigste Zusage an den Nutzer: nicht immer derselbe Grund.

def test_recent_bewirtung_anlaesse_liest_anlass_aus_pflichtangaben(db):
    from datetime import datetime, timezone

    from app.models import Invoice
    for i, anlass in enumerate(["Besprechung der Einkaufskonditionen mit Herrn Müller",
                                "Abstimmung der Versandzeiten mit Frau Yilmaz",
                                "Besprechung der Einkaufskonditionen mit Herrn Müller"]):
        db.add(Invoice(type="betriebsausgabe", category="Bewirtungsbeleg (§ 4 Abs. 5 EStG)",
                       invoice_date=datetime(2026, 7, 10 + i, tzinfo=timezone.utc),
                       amount=Decimal("48.00"), currency="EUR",
                       note=f"Bewirtungsbeleg · Ort: Adler · Gäste: X · Anlass: {anlass} · Gesamt: 48,00 €"))
    # andere Ausgabe-Kategorie taucht NICHT in der Sperrliste auf
    db.add(Invoice(type="betriebsausgabe", category="Server / Hosting / Domain",
                   invoice_date=datetime(2026, 7, 15, tzinfo=timezone.utc),
                   amount=Decimal("9.00"), currency="EUR", note="Hetzner VPS Juli"))
    db.commit()

    anlaesse = invoice_service.recent_bewirtung_anlaesse(db)
    assert anlaesse[0] == "Besprechung der Einkaufskonditionen mit Herrn Müller"  # neueste zuerst
    assert "Abstimmung der Versandzeiten mit Frau Yilmaz" in anlaesse
    assert len(anlaesse) == 2, "Dubletten und Fremd-Kategorien gehoeren nicht in die Sperrliste"
    assert not any("Hetzner" in a for a in anlaesse)


def test_bewirtung_vorschlaege_endpoint_liefert_auswahl(client):
    r = client.post("/api/v1/invoices/bewirtung-vorschlaege",
                    json={"ort": "Restaurant Adler"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["vorschlaege"]) >= 2, "es muss eine AUSWAHL sein, keine Vorgabe"
    # konkretes Thema; keine Pauschal-Floskel, die das Finanzamt ablehnt
    for v in body["vorschlaege"]:
        assert v.strip().lower() not in {"geschäftsessen", "arbeitsessen", "kundenpflege"}
    assert len(set(body["vorschlaege"])) == len(body["vorschlaege"])


def test_bewirtung_vorschlaege_nennen_keine_namen(client):
    """Nutzer-Vorgabe (27.07.): der Anlass nennt NUR das Thema. Wer bewirtet wurde,
    steht handschriftlich auf der Quittung bzw. in der eigenen Zeile des Eigenbelegs –
    im Anlass waere der Name doppelt. Gilt auch, wenn Gaeste mitgeschickt werden."""
    v = client.post("/api/v1/invoices/bewirtung-vorschlaege",
                    json={"gaeste": "Herr Müller (Lieferant XY)",
                          "ort": "Restaurant Adler"}).json()["vorschlaege"]
    assert v
    for vorschlag in v:
        assert "Müller" not in vorschlag, f"Name im Anlass: {vorschlag}"
        assert "Lieferant XY" not in vorschlag, f"Firma im Anlass: {vorschlag}"
        assert "Gesprächspartner" not in vorschlag, f"Namens-Anhang im Anlass: {vorschlag}"


def test_bewirtung_vorschlaege_sind_kurz_genug_fuers_quittungsfeld(client):
    """Nutzerwunsch: der Anlass wird von Hand in ein kleines Feld auf der Restaurant-
    Quittung geschrieben. Lange Saetze passen dort nicht hinein – deshalb ein knappes
    Thema statt eines ausformulierten Satzes."""
    v = client.post("/api/v1/invoices/bewirtung-vorschlaege", json={}).json()["vorschlaege"]
    assert v
    for vorschlag in v:
        assert len(vorschlag) <= 50, f"zu lang zum Abschreiben ({len(vorschlag)}): {vorschlag}"
        assert not vorschlag.lower().startswith(("besprechung ", "abstimmung ", "gespräch ")), \
            f"Verb-Vorspann kostet nur Platz: {vorschlag}"


def test_bewirtung_vorschlaege_wiederholen_sich_nicht(client):
    """Nutzerwunsch: beim zweiten Klick kommen ANDERE Gruende."""
    erste = client.post("/api/v1/invoices/bewirtung-vorschlaege",
                        json={}).json()["vorschlaege"]
    zweite = client.post("/api/v1/invoices/bewirtung-vorschlaege",
                         json={"vermeiden": erste}).json()["vorschlaege"]
    assert not (set(erste) & set(zweite))


def _bewirtung_beleg(db, *, tag: int, anlass: str):
    from datetime import datetime, timezone

    from app.models import Invoice
    db.add(Invoice(type="betriebsausgabe", category="Bewirtungsbeleg (§ 4 Abs. 5 EStG)",
                   invoice_date=datetime(2026, 7, tag, tzinfo=timezone.utc),
                   amount=Decimal("60.00"), currency="EUR",
                   note=f"Bewirtungsbeleg · Anlass: {anlass} · Gesamt: 60,00 €"))
    db.commit()


def test_bewirtung_zuletzt_benutzter_anlass_kommt_nicht_direkt_wieder(client, db):
    """Nicht mehrmals am Stueck derselbe Grund – der zuletzt erfasste ist gesperrt."""
    frei = client.post("/api/v1/invoices/bewirtung-vorschlaege",
                       json={}).json()["vorschlaege"]
    _bewirtung_beleg(db, tag=20, anlass=frei[0])

    neu = client.post("/api/v1/invoices/bewirtung-vorschlaege",
                      json={}).json()["vorschlaege"]
    assert frei[0] not in neu


async def test_bewirtung_frueherer_anlass_ist_nur_nachrangig_nicht_gesperrt():
    """Ausgewogen, nicht wiederholungsfrei (Nutzerwunsch): ein frueher benutzter Grund
    rutscht nur ans Ende der Auswahl – gesperrt ist er nicht."""
    from app.integrations.llm import MockLLMClient
    llm = MockLLMClient()

    alle = (await llm.suggest_bewirtung_anlaesse())["vorschlaege"]
    frueher = alle[0]

    # Solange es unbenutzte Themen gibt, kommt das bekannte hinten an -> nicht in den Top 3.
    nachrangig = (await llm.suggest_bewirtung_anlaesse(
        zuletzt_benutzt=[frueher]))["vorschlaege"]
    assert frueher not in nachrangig

    # Sind die anderen Themen durch, darf es wiederkommen (kein Verbot, keine Sackgasse).
    rest = (await llm.suggest_bewirtung_anlaesse(
        vermeiden=nachrangig, zuletzt_benutzt=[frueher]))["vorschlaege"]
    wieder = (await llm.suggest_bewirtung_anlaesse(
        vermeiden=(nachrangig + rest), zuletzt_benutzt=[frueher]))["vorschlaege"]
    assert frueher in wieder


def test_bewirtung_vorschlaege_bleiben_auch_bei_vielen_belegen_erhalten(client, db):
    """Keine Sackgasse: auch wenn jedes Thema schon einmal im Beleg-Ordner stand,
    kommen weiter Vorschlaege (Wiederholung ist erlaubt)."""
    gesehen: list[str] = []
    for tag in range(1, 12):
        v = client.post("/api/v1/invoices/bewirtung-vorschlaege",
                        json={}).json()["vorschlaege"]
        assert v, "es muessen immer Vorschlaege kommen"
        gesehen.append(v[0])
        _bewirtung_beleg(db, tag=tag, anlass=v[0])
    # Themen wiederholen sich ueber die Zeit – aber nie zweimal direkt hintereinander.
    assert len(set(gesehen)) < len(gesehen), "Wiederholung ist ausdruecklich erlaubt"
    assert all(a != b for a, b in zip(gesehen, gesehen[1:])), "aber nicht am Stueck"


def test_anschrift_wird_an_den_strichen_umgebrochen(db, monkeypatch):
    """Eine .env-Zeile kann keinen Zeilenumbruch tragen.

    Deshalb trennt "|" die Anschriftzeilen. Ohne diese Umsetzung stuende die
    komplette Anschrift samt Strichen in einer Zeile auf jeder Rechnung.
    """
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(
        s, "seller_address",
        "Inhaber: Aleyna Nur Aydin | Hauptstraße 439 | 53639 Königswinter",
    )
    sale = _sale(db, tx="TX-INV-ADR")
    r = invoice_service.generate_sale_invoice(db, sale_id=sale.id)
    data, _, _ = invoice_service.read_invoice_file(db, invoice_id=r["id"])
    html = data.decode("utf-8")
    assert "Inhaber: Aleyna Nur Aydin<br>Hauptstraße 439<br>53639 Königswinter" in html
    assert "|" not in html.split("<h1>")[0]


def test_verkauf_vor_dem_stichtag_bleibt_paragraph19(db, monkeypatch):
    from datetime import datetime, timezone

    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "ust_regelbesteuerung_ab", "2026-09-16")
    sale = _sale(db, tx="TX-ALT-19")
    sale.sale_date = datetime(2026, 8, 1, tzinfo=timezone.utc)
    db.commit()
    r = invoice_service.generate_sale_invoice(db, sale_id=sale.id)
    data, _, _ = invoice_service.read_invoice_file(db, invoice_id=r["id"])
    assert "§ 19 UStG" in data.decode("utf-8") and "USt</td>" not in data.decode("utf-8")


def test_steuerexport_nennt_die_besteuerung_im_jahr(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "ust_regelbesteuerung_ab", "2026-09-16")
    assert invoice_service._besteuerung_im_jahr(2025) == "§19 Kleinunternehmer"
    assert "ab 16.09. Regelbesteuerung (19 % USt)" in invoice_service._besteuerung_im_jahr(2026)
    assert invoice_service._besteuerung_im_jahr(2027) == "Regelbesteuerung, 19 % USt"
