"""Tests: aus dem AliExpress-BELEG die RECHNUNG erzeugen (1:1-Nachbau).

Die Werte in den Fixtures stammen aus ECHTEN Belegen des Shops (Schlappen-Kauf vom
11.08.2026 und Fussball-Figur vom 29.06.2026) – damit die Tests genau die Faelle
abdecken, die live vorkommen.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.models import Invoice, OrderAliexpress
from app.services import purchase_invoice as pi

# Echter Beleg 11.08.2026: Einfuhrabgaben werden zum Total ADDIERT.
BELEG_EINFUHR = {
    "order_id": "3075412823902059", "order_date": "11. Aug. 2026",
    "items": [{"title": "Summer Fashion Slippers For Men", "properties": "brown,44-45",
               "unit_price": 4.75, "quantity": 1}],
    "store_name": "Shop1103840307 Store", "ship_to": "Andreas K., Zum Sonnenborn 11",
    "payment_brand": "VISA", "subtotal": 4.75, "discount": None, "shipping": 1.76,
    "extra_lines": [{"label": "Geschätzte Einfuhrabgaben", "amount": 3.57}],
    "vat_included": None, "total": 10.08, "currency": "EUR",
}

# Echter Beleg 29.06.2026: Rabatt + MwSt ist im Total bereits ENTHALTEN.
BELEG_VAT = {
    "order_id": "3074262643882059", "order_date": "Jun 29, 2026",
    "items": [{"title": "Fussball-Modellpuppen Cartoon", "properties": "01",
               "unit_price": 11.49, "quantity": 1}],
    "store_name": "Shop1105117815 Store", "ship_to": "Ruzica B., Bahnhofsplatz / 1",
    "payment_brand": "VISA", "subtotal": 11.49, "discount": 0.11, "shipping": 0.01,
    "extra_lines": [], "vat_included": 1.82, "total": 11.38, "currency": "EUR",
}

EMPFAENGER = ["Kaky & Syed Gbr", "Johannisstr. 46", "58452 Witten", "DE365839535"]


def test_einfuhrabgaben_werden_addiert_und_gehen_auf():
    d = pi.normalisiere_beleg(BELEG_EINFUHR)
    assert d["total"] == Decimal("10.08")
    assert d["extra_lines"][0]["label"] == "Import Duties"
    assert d["payment_method"] == "Kredit-/Debitkarte"
    assert d["store_number"] == "1103840307"


def test_vat_ist_im_total_enthalten_und_wird_nicht_addiert():
    # 11.49 - 0.11 + 0.01 = 11.39 ~ 11.38 (Cent-Rundung von AliExpress) und die
    # MwSt von 1.82 darf NICHT obendrauf gerechnet werden.
    d = pi.normalisiere_beleg(BELEG_VAT)
    assert d["total"] == Decimal("11.38")
    assert d["vat_included"] == Decimal("1.82")
    assert d["discount"] == Decimal("0.11")


def test_beleg_der_nicht_aufgeht_wird_abgelehnt():
    with pytest.raises(pi.BelegUnklar):
        pi.normalisiere_beleg({**BELEG_EINFUHR, "total": 99.99})


def test_unvollstaendiger_beleg_wird_abgelehnt_statt_geraten():
    with pytest.raises(pi.BelegUnklar):        # kein Gesamtbetrag
        pi.normalisiere_beleg({**BELEG_EINFUHR, "total": None})
    with pytest.raises(pi.BelegUnklar):        # Artikel ohne Stueckpreis
        pi.normalisiere_beleg({**BELEG_EINFUHR,
                               "items": [{"title": "X", "unit_price": None, "quantity": 1}]})
    with pytest.raises(pi.BelegUnklar):        # gar keine Position
        pi.normalisiere_beleg({**BELEG_EINFUHR, "items": []})
    with pytest.raises(pi.BelegUnklar):        # leerer Beleg (Vision lieferte nichts)
        pi.normalisiere_beleg({})


def test_rechnung_zeigt_aliexpress_titel_und_beleg_betraege():
    html = pi.rendere_rechnung(pi.normalisiere_beleg(BELEG_EINFUHR), empfaenger=EMPFAENGER)
    assert "Summer Fashion Slippers For Men" in html   # AliExpress-Name, nicht eBay-Titel
    assert "€ 10.08" in html and "€ 3.57" in html
    assert "Import Duties" in html
    assert "Product properties:brown,44-45" in html
    assert "3075412823902059" in html
    assert "Kaky &amp; Syed Gbr" in html
    # nichts dazuerfunden: kein Zusatztext, keine Rabattzeile ohne Rabatt auf dem Beleg
    assert "Discount" not in html
    assert "VAT included" not in html


def test_rechnung_spiegelt_vat_label_statt_import_duties():
    html = pi.rendere_rechnung(pi.normalisiere_beleg(BELEG_VAT), empfaenger=EMPFAENGER)
    assert "VAT included" in html and "€ 1.82" in html
    assert "Import Duties" not in html
    assert "Discount" in html and "€ 0.11" in html


def test_datum_wird_aus_beiden_beleg_schreibweisen_gelesen():
    assert pi.parse_beleg_datum("11. Aug. 2026").date().isoformat() == "2026-08-11"
    assert pi.parse_beleg_datum("Jun 29, 2026").date().isoformat() == "2026-06-29"
    assert pi.parse_beleg_datum("28. Feb. 2026").date().isoformat() == "2026-02-28"
    assert pi.parse_beleg_datum("2026-08-11").date().isoformat() == "2026-08-11"
    assert pi.parse_beleg_datum("unlesbar") is None


def _beleg_invoice(db, tmp_path, *, ref="3075412823902059") -> Invoice:
    """Ein abgelegter ORIGINAL-Beleg (Bild), wie ihn der Backfill-Client speichert."""
    order = OrderAliexpress(aliexpress_order_id=ref, quantity=1, status="delivered")
    db.add(order)
    db.flush()
    datei = tmp_path / f"original_aliexpress_purchase_{ref}_abc.png"
    datei.write_bytes(b"\x89PNG\r\n\x1a\n-beleg-bild")
    inv = Invoice(type="aliexpress_purchase", reference_id=ref, invoice_number=f"AE_{ref}",
                  amount=Decimal("9.99"), currency="EUR", file_path=str(datei),
                  file_hash="hash-original", is_original=True, order_id=order.id)
    db.add(inv)
    db.commit()
    return inv


@pytest.mark.asyncio
async def test_rechnung_wird_neben_dem_beleg_abgelegt_original_bleibt(db, tmp_path):
    inv = _beleg_invoice(db, tmp_path)
    beleg_pfad, beleg_hash = inv.file_path, inv.file_hash
    beleg_bytes = (tmp_path / beleg_pfad.rsplit("/", 1)[-1]).read_bytes()

    r = await pi.erzeuge_rechnung(db, invoice_id=inv.id)
    assert r["created"] is True

    db.refresh(inv)
    # Das Original ist unangetastet – Pfad, Hash, Datei-Inhalt, Original-Flag
    assert inv.file_path == beleg_pfad and inv.file_hash == beleg_hash
    assert (tmp_path / beleg_pfad.rsplit("/", 1)[-1]).read_bytes() == beleg_bytes
    assert inv.is_original is True
    # Die Rechnung liegt als EIGENE Datei daneben
    assert inv.generated_path and inv.generated_path != beleg_pfad
    assert "rechnung_aliexpress" in inv.generated_path
    # Der Betrag kommt jetzt vom Beleg (Mock liefert 10.08), nicht mehr der Altwert
    assert inv.amount == Decimal("10.08")
    # Audit-Spur: was auf dem Beleg stand
    assert inv.receipt_data["total"] == 10.08
    assert inv.receipt_data["items"][0]["quantity"] == 1


@pytest.mark.asyncio
async def test_rechnung_ist_idempotent(db, tmp_path):
    inv = _beleg_invoice(db, tmp_path)
    erst = await pi.erzeuge_rechnung(db, invoice_id=inv.id)
    nochmal = await pi.erzeuge_rechnung(db, invoice_id=inv.id)
    assert erst["created"] is True and nochmal["created"] is False
    assert erst["generated_path"] == nochmal["generated_path"]


@pytest.mark.asyncio
async def test_ohne_original_beleg_keine_rechnung(db, tmp_path):
    """Platzhalter-Beleg (TXT, kein Original) -> bewusst keine Rechnung."""
    order = OrderAliexpress(aliexpress_order_id="AE-PLATZ", quantity=1, status="ordered")
    db.add(order)
    db.flush()
    datei = tmp_path / "aliexpress_purchase_AE-PLATZ_x.txt"
    datei.write_text("AliExpress-Kaufbeleg\nBestellnummer: AE-PLATZ\n")
    inv = Invoice(type="aliexpress_purchase", reference_id="AE-PLATZ", file_path=str(datei),
                  is_original=False, order_id=order.id)
    db.add(inv)
    db.commit()
    with pytest.raises(pi.BelegUnklar):
        await pi.erzeuge_rechnung(db, invoice_id=inv.id)


@pytest.mark.asyncio
async def test_beleg_fremder_bestellung_wird_abgelehnt(db, tmp_path):
    """Der Mock liefert Bestellung ...2059 – haengt der Beleg an einer anderen
    Bestellnummer, darf daraus KEINE Rechnung entstehen (Verwechslungsschutz)."""
    inv = _beleg_invoice(db, tmp_path, ref="9999999999999")
    with pytest.raises(pi.BelegUnklar):
        await pi.erzeuge_rechnung(db, invoice_id=inv.id)


@pytest.mark.asyncio
async def test_backfill_ueberspringt_fertige_und_meldet_fehlende_belege(db, tmp_path):
    """Massenlauf: erzeugt nur Offenes, zaehlt Kaeufe ohne Original-Beleg separat."""
    mit_beleg = _beleg_invoice(db, tmp_path)
    ohne_beleg = Invoice(type="aliexpress_purchase", reference_id="AE-OHNE",
                         file_path=None, is_original=False)
    db.add(ohne_beleg)
    db.commit()

    stand = pi.offene_rechnungen(db)
    assert stand["counts"] == {"bereit": 1, "beleg_fehlt": 1, "fertig": 0}

    r = await pi.erzeuge_alle_rechnungen(db)
    assert r["erzeugt"] == 1 and r["offen"] == 0
    assert r["beleg_fehlt"] == 1 and r["probleme"] == []

    # zweiter Lauf erzeugt nichts neu
    wieder = await pi.erzeuge_alle_rechnungen(db)
    assert wieder["erzeugt"] == 0
    db.refresh(mit_beleg)
    assert mit_beleg.generated_path


@pytest.mark.asyncio
async def test_backfill_laeuft_trotz_unklarem_beleg_weiter(db, tmp_path):
    """Ein Problem-Beleg darf den Lauf nicht abbrechen – er wird nur gemeldet."""
    _beleg_invoice(db, tmp_path)                       # geht durch
    _beleg_invoice(db, tmp_path, ref="1111111111111")  # Nummer passt nicht -> Problem
    r = await pi.erzeuge_alle_rechnungen(db)
    assert r["erzeugt"] == 1
    assert len(r["probleme"]) == 1 and "1111111111111" in r["probleme"][0]["grund"]


def test_llm_fehler_wird_im_klartext_gemeldet():
    """Scheitert das Auslesen, muss der GRUND sichtbar sein – sonst ist im
    Massenlauf nicht zu erkennen, woran es lag."""
    with pytest.raises(pi.BelegUnklar, match="Zeitueberschreitung"):
        pi.normalisiere_beleg({"_fehler": "APITimeoutError: Zeitueberschreitung"})


def test_alte_windows_pfade_werden_aufgeloest(tmp_path):
    """Alt-Belege stehen als 'data\\invoices\\...' in der DB (Windows-Zeit)."""
    class _Storage:
        base_dir = tmp_path
        def read(self, pfad):
            return Path(pfad).read_bytes()

    ordner = tmp_path / "2026-07"
    ordner.mkdir()
    (ordner / "original_aliexpress_purchase_307.png").write_bytes(b"BELEG")
    roh = "data\\invoices\\2026-07\\original_aliexpress_purchase_307.png"
    assert pi._lies_belegdatei(_Storage(), roh) == b"BELEG"

    with pytest.raises(pi.BelegUnklar, match="nicht auffindbar"):
        pi._lies_belegdatei(_Storage(), "data\\invoices\\2026-07\\gibtsnicht.png")


def test_beleg_json_wird_geprueft_und_freitext_abgewiesen():
    """Die Antwort des Modells muss echtes Beleg-JSON sein – Freitext oder ein
    kaputtes Schema darf NIE als Beleg-Daten durchgehen."""
    from app.integrations.llm import parse_receipt_json

    gut = parse_receipt_json('```json\n{"order_id":"307","total":10.08,'
                             '"items":[{"title":"Slipper","unit_price":4.75,"quantity":1}]}\n```')
    assert gut["order_id"] == "307" and gut["total"] == 10.08
    assert gut["extra_lines"] == [] and gut["vat_included"] is None   # nichts erfunden

    assert "_fehler" in parse_receipt_json("Ich kann den Beleg leider nicht lesen.")
    assert "_fehler" in parse_receipt_json("")

    # Unlesbarer Betrag wird zu None (nicht geraten) – und ohne Gesamtbetrag
    # entsteht garantiert keine Rechnung.
    unlesbar = parse_receipt_json(
        '{"total": "keine Zahl", "items":[{"title":"X","unit_price":1.0,"quantity":1}]}')
    assert unlesbar["total"] is None
    with pytest.raises(pi.BelegUnklar, match="Kein Gesamtbetrag"):
        pi.normalisiere_beleg(unlesbar)


def test_empfaenger_ist_die_aliexpress_firma_nicht_die_ebay_marke(monkeypatch):
    """Auf der AliExpress-Rechnung steht die dort hinterlegte Firma mit USt-IdNr.,
    nicht die Shop-Marke aus seller_name.

    Geprueft wird das Verhalten, nicht ein bestimmter Firmenname: die Angaben
    kommen aus der Konfiguration, damit im Code keine Firmendaten stehen.
    """
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("AE_INVOICE_RECIPIENT", "Testfirma|Teststr. 1|12345 Teststadt|DE999999999")
    monkeypatch.setenv("SELLER_NAME", "Test-Shopmarke")

    zeilen = pi._empfaenger_zeilen()

    assert zeilen[0] == "Testfirma"
    assert "DE999999999" in zeilen
    assert "Test-Shopmarke" not in zeilen      # die Shop-Marke gehoert hier NICHT hin
    get_settings.cache_clear()


def test_empfaenger_leer_wenn_nicht_konfiguriert(monkeypatch):
    """Ohne Konfiguration wird nichts geraten - lieber leer als eine falsche Firma."""
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("AE_INVOICE_RECIPIENT", "")
    assert pi._empfaenger_zeilen() == []
    get_settings.cache_clear()


def test_beleg_download_findet_alte_windows_pfade(db, tmp_path):
    """Belegablage-Download muss Alt-Eintraege wieder anzeigen (vorher 500)."""
    from app.services import invoice_service

    ordner = tmp_path / "2026-07"
    ordner.mkdir()
    (ordner / "original_aliexpress_purchase_999.png").write_bytes(b"\x89PNG\r\n\x1a\nX")
    inv = Invoice(type="aliexpress_purchase", reference_id="999", invoice_number="AE_999",
                  file_path="data\\invoices\\2026-07\\original_aliexpress_purchase_999.png",
                  is_original=True)
    db.add(inv)
    db.commit()

    from app.integrations import get_storage
    get_storage().base_dir = tmp_path      # Ablage auf das Testverzeichnis zeigen
    data, name, ctype = invoice_service.read_invoice_file(db, invoice_id=inv.id)
    assert data.startswith(b"\x89PNG") and ctype == "image/png" and name.endswith(".png")


def test_summenspalten_bleiben_immer_vier_auch_mit_rabatt():
    """Das Linienbild der Original-Rechnung hat genau vier Summenfelder. Ein
    Rabatt darf keine fuenfte Spalte erzeugen (sonst verschieben sich die Linien)."""
    ohne = pi.rendere_rechnung(pi.normalisiere_beleg(BELEG_EINFUHR), empfaenger=EMPFAENGER)
    mit = pi.rendere_rechnung(pi.normalisiere_beleg(BELEG_VAT), empfaenger=EMPFAENGER)
    assert ohne.count('class="sum-cell"') == mit.count('class="sum-cell"') == 8   # 2 Zeilen x 4
    assert "sum-grid" not in mit          # kein verschachteltes Raster mehr
    assert "Discount" in mit and "€ 0.11" in mit


def test_spaltenkopf_heisst_total_nicht_line_total():
    html = pi.rendere_rechnung(pi.normalisiere_beleg(BELEG_EINFUHR), empfaenger=EMPFAENGER)
    assert "<th>Total</th>" in html
    assert "Line Total" not in html


def test_pdf_knopf_erscheint_nicht_im_ausdruck():
    """Der Knopf ist Bedienhilfe, kein Beleginhalt – im PDF darf er nicht auftauchen."""
    html = pi.rendere_rechnung(pi.normalisiere_beleg(BELEG_EINFUHR), empfaenger=EMPFAENGER)
    assert 'class="pdfbtn"' in html
    assert ".pdfbtn{display:none}" in html.replace(" ", "")


# Echter Beleg 11.07.2026 (live-Fund): ZWEI 'Steuer'-Zeilen, eine davon negativ,
# zusaetzlich der MwSt-Hinweis. Daran ist der erste Massenlauf gescheitert.
BELEG_STEUER = {
    "order_id": "3074690578872059", "order_date": "11. Juli 2026",
    "items": [{"title": "Neue Smart Bluetooth V5.3 Sonnen", "properties": "Bk-Bk",
               "unit_price": 6.39, "quantity": 1}],
    "store_name": "TopSmart Life Store", "ship_to": "Enrico Andre, Schleifstr. / 4",
    "payment_brand": "VISA", "subtotal": 6.39, "discount": 0.13, "shipping": 1.99,
    "extra_lines": [{"label": "Steuer", "amount": 4.89}, {"label": "Steuer", "amount": -1.32}],
    "vat_included": 1.89, "total": 11.82, "currency": "EUR",
}


def test_mehrere_steuerzeilen_auch_negative_gehen_auf():
    # 6.39 - 0.13 + 1.99 + 4.89 - 1.32 = 11.82
    d = pi.normalisiere_beleg(BELEG_STEUER)
    assert d["total"] == Decimal("11.82")
    assert [z["amount"] for z in d["extra_lines"]] == [Decimal("4.89"), Decimal("-1.32")]
    assert d["vat_included"] == Decimal("1.89")


def test_steuerzeilen_stehen_in_der_rechnung_und_layout_bleibt_vierspaltig():
    html = pi.rendere_rechnung(pi.normalisiere_beleg(BELEG_STEUER), empfaenger=EMPFAENGER)
    assert "€ 4.89" in html and "- € 1.32" in html
    assert "Tax" in html and "VAT included" in html and "€ 1.89" in html
    assert html.count('class="sum-cell"') == 8      # weiterhin 2 Zeilen x 4 Felder


def test_fehlende_steuerzeile_faellt_auf_statt_still_falsch_zu_rechnen():
    """Ohne die zweite Steuerzeile geht der Beleg nicht auf -> keine Rechnung."""
    ohne = {**BELEG_STEUER, "extra_lines": [{"label": "Steuer", "amount": 4.89}]}
    with pytest.raises(pi.BelegUnklar, match="geht nicht auf"):
        pi.normalisiere_beleg(ohne)


def test_betraege_werden_auch_aus_text_und_deutschem_format_gelesen():
    """Das Modell liefert Betraege real auch als Satz oder mit Komma – daran ist
    ein ganzer Lauf gescheitert (22 Belege, 'Beleg-JSON ungueltig')."""
    from app.integrations.llm import _ReceiptSchema, _beleg_zahl

    assert _beleg_zahl("1,89€ Mehrwertsteuer inbegriffen") == 1.89
    assert _beleg_zahl("1.234,56€") == 1234.56
    assert _beleg_zahl("-1,32€") == -1.32
    assert _beleg_zahl("keine Angabe") is None
    assert _beleg_zahl(None) is None

    r = _ReceiptSchema.model_validate_json(
        '{"vat_included":"1,89€ Mehrwertsteuer inbegriffen","total":"11,82€",'
        '"items":[{"title":"X","properties":null,"unit_price":"6,39€","quantity":1}]}')
    assert r.vat_included == 1.89 and r.total == 11.82
    assert r.items[0].unit_price == 6.39


def test_versand_muss_nicht_additiv_sein():
    """Echter Beleg 1649: Gesamtsumme 23,59 + Versand 0,09, Insgesamt aber 23,59 –
    der Versand steckt dort schon in der Gesamtsumme."""
    beleg = {"order_id": "3071920815792059", "order_date": "29. Apr. 2026",
             "items": [{"title": "R11C Smart Ring Männer Frauen Di", "properties": "Silver,12",
                        "unit_price": 23.59, "quantity": 1}],
             "store_name": "Sinbeda Direct Store", "ship_to": "Ute Horschig, Plovdiver Str. / 18",
             "payment_brand": "VISA", "paid_amount": 23.59,
             "subtotal": 23.59, "discount": None, "shipping": 0.09,
             "extra_lines": [], "vat_included": 3.77, "total": 23.59}
    d = pi.normalisiere_beleg(beleg)
    assert d["total"] == Decimal("23.59") and d["shipping"] == Decimal("0.09")


def test_bezahlter_betrag_ist_die_gegenprobe():
    """Weicht der Gesamtbetrag vom bezahlten Betrag ab, wurde etwas falsch
    gelesen -> keine Rechnung."""
    with pytest.raises(pi.BelegUnklar, match="bezahlten Betrag"):
        pi.normalisiere_beleg({**BELEG_EINFUHR, "paid_amount": 99.00})
    # passender bezahlter Betrag stoert nicht
    assert pi.normalisiere_beleg({**BELEG_EINFUHR, "paid_amount": 10.08})["total"] == Decimal("10.08")


def test_verlesene_bestellnummer_wird_toleriert_fremde_nicht():
    assert pi.bestellnummern_passen("3072102669952059", "3072102669952059")
    assert pi.bestellnummern_passen("307210266995205", "3072102669952059")   # Ziffer fehlt
    assert pi.bestellnummern_passen("3072102669952058", "3072102669952059")  # eine falsch
    assert not pi.bestellnummern_passen("1111111111111111", "3072102669952059")
    assert not pi.bestellnummern_passen("30721", "3072102669952059")


@pytest.mark.asyncio
async def test_rechnung_nutzt_die_bestellnummer_aus_dem_system(db, tmp_path):
    """Auch wenn der Beleg leicht verlesen wird, steht auf der Rechnung die
    verlaessliche Nummer aus dem System."""
    inv = _beleg_invoice(db, tmp_path, ref="3075412823902059")
    await pi.erzeuge_rechnung(db, invoice_id=inv.id)
    db.refresh(inv)
    assert inv.receipt_data["order_id"] == "3075412823902059"


@pytest.mark.asyncio
async def test_betragspruefung_korrigiert_beleg_und_einkauf(db, tmp_path):
    """Der Beleg ist die Wahrheit: weicht der gespeicherte Betrag ab, wird er an
    BEIDEN Stellen korrigiert (Beleg-Eintrag + EK der Bestellung)."""
    inv = _beleg_invoice(db, tmp_path)          # gespeichert: 9.99, Mock-Beleg: 10.08
    r = await pi.pruefe_betraege(db, limit=5)
    assert r["korrigiert"] == 1
    assert r["aenderungen"][0]["alt"] == 9.99 and r["aenderungen"][0]["neu"] == 10.08
    db.refresh(inv)
    assert inv.amount == Decimal("10.08")
    order = db.get(OrderAliexpress, inv.order_id)
    assert order.cost_cny == Decimal("10.08") and order.cost_source == "receipt"


@pytest.mark.asyncio
async def test_betragspruefung_laesst_stimmende_werte_in_ruhe(db, tmp_path):
    inv = _beleg_invoice(db, tmp_path)
    inv.amount = Decimal("10.08")
    db.commit()
    r = await pi.pruefe_betraege(db, limit=5)
    assert r["korrigiert"] == 0 and r["geprueft"] == 1


@pytest.mark.asyncio
async def test_betragspruefung_fasst_fremdwaehrung_nicht_an(db, tmp_path):
    """USD-Belege bleiben unangetastet – ihr Betrag ist kein Euro-Wert."""
    inv = _beleg_invoice(db, tmp_path)
    inv.receipt_data = {"total": 33.57, "currency": "USD"}
    inv.amount = Decimal("29.42")
    db.commit()
    r = await pi.pruefe_betraege(db, limit=5)
    assert r["korrigiert"] == 0
    db.refresh(inv)
    assert inv.amount == Decimal("29.42")


@pytest.mark.asyncio
async def test_betragspruefung_nutzt_vorhandene_auslesung_ohne_ki(db, tmp_path):
    """Steht der Beleg-Betrag schon in der Audit-Spur, wird er direkt genutzt."""
    inv = _beleg_invoice(db, tmp_path)
    inv.receipt_data = {"total": 12.34, "currency": "EUR"}
    db.commit()
    r = await pi.pruefe_betraege(db, limit=5)
    assert r["korrigiert"] == 1 and r["aenderungen"][0]["neu"] == 12.34
    db.refresh(inv)
    assert inv.amount == Decimal("12.34")


@pytest.mark.asyncio
async def test_betragspruefung_arbeitet_sich_vor(db, tmp_path):
    """Ein zweiter Lauf darf nicht wieder bei denselben Belegen anfangen –
    sonst erreicht ein Massenlauf nie die hinteren (Vorfall 14.08.)."""
    a = _beleg_invoice(db, tmp_path, ref="3075412823902059")
    b = _beleg_invoice(db, tmp_path, ref="3075412823902060")

    erst = await pi.pruefe_betraege(db, limit=1)
    assert erst["geprueft"] == 1
    zweit = await pi.pruefe_betraege(db, limit=1)
    assert zweit["geprueft"] == 1
    db.refresh(a); db.refresh(b)
    # beide sind nun geprueft (jeder genau einmal)
    assert a.receipt_data and b.receipt_data
    # dritter Lauf findet nichts mehr
    assert (await pi.pruefe_betraege(db, limit=5))["geprueft"] == 0


@pytest.mark.asyncio
async def test_unlesbarer_beleg_blockiert_den_lauf_nicht(db, tmp_path, monkeypatch):
    """Ein kaputter Beleg bekommt einen Vermerk und wird beim naechsten Lauf
    uebersprungen – sonst haengt der Massenlauf ewig an ihm (Vorfall: Beleg 536,
    eine 0x0-Pixel-Datei, 14 Runden lang immer wieder versucht)."""
    kaputt = _beleg_invoice(db, tmp_path, ref="3075412823902099")
    from app.integrations import get_llm_client

    async def _fehler(*a, **k):
        return {"_fehler": "BadRequestError: Could not process image"}
    monkeypatch.setattr(get_llm_client(), "read_purchase_receipt", _fehler)

    r = await pi.pruefe_betraege(db, limit=5)
    assert r["unklar"] == 1
    db.refresh(kaputt)
    assert "Could not process image" in kaputt.receipt_data["pruef_fehler"]
    # zweiter Lauf fasst ihn nicht mehr an
    assert (await pi.pruefe_betraege(db, limit=5))["unklar"] == 0


def test_orders_liste_verlinkt_den_beleg_des_einkaufs(db, tmp_path):
    """In der Orders-Liste muss je Verkauf der Beleg des zugehoerigen Einkaufs
    verlinkt sein – sonst muss man ihn in der Belegablage suchen."""
    from app.models import Listing, Product, Sale
    from app.services import analytics_service

    p = Product(aliexpress_url="https://de.aliexpress.com/item/x1.html", aliexpress_id="aex1")
    db.add(p); db.flush()
    listing = Listing(product_id=p.id, title_seo="Testartikel", description="d",
                      listing_status="active", price_eur=Decimal("19.99"))
    db.add(listing); db.flush()
    sale = Sale(ebay_transaction_id="TX-BELEG-1", listing_id=listing.id, buyer_name="Max",
                price_eur=Decimal("19.99"), quantity=1, status="pending")
    db.add(sale); db.flush()
    order = OrderAliexpress(sale_id=sale.id, aliexpress_order_id="3075412823902059",
                            quantity=1, status="delivered")
    db.add(order); db.flush()
    datei = tmp_path / "original_aliexpress_purchase_x.png"
    datei.write_bytes(b"\x89PNG\r\n\x1a\nx")
    db.add(Invoice(type="aliexpress_purchase", reference_id="3075412823902059",
                   file_path=str(datei), is_original=True, order_id=order.id,
                   generated_path=str(tmp_path / "rechnung.html")))
    db.commit()

    zeile = next(o for o in analytics_service.list_orders(db)["orders"]
                 if o["sale_id"] == sale.id)
    assert zeile["beleg_id"] is not None
    assert zeile["beleg_original"] is True
    assert zeile["hat_rechnung"] is True


def test_orders_zeile_ohne_beleg_meldet_das(db):
    """Verkauf ohne Einkaufs-Beleg: kein Beleg-Verweis (Knopf bleibt blass)."""
    from app.models import Listing, Product, Sale
    from app.services import analytics_service

    p = Product(aliexpress_url="https://de.aliexpress.com/item/x2.html", aliexpress_id="aex2")
    db.add(p); db.flush()
    listing = Listing(product_id=p.id, title_seo="Ohne Beleg", description="d",
                      listing_status="active", price_eur=Decimal("9.99"))
    db.add(listing); db.flush()
    sale = Sale(ebay_transaction_id="TX-BELEG-2", listing_id=listing.id, buyer_name="Erika",
                price_eur=Decimal("9.99"), quantity=1, status="pending")
    db.add(sale)
    db.commit()

    zeile = next(o for o in analytics_service.list_orders(db)["orders"]
                 if o["sale_id"] == sale.id)
    assert zeile["beleg_id"] is None and zeile["hat_rechnung"] is False


@pytest.mark.asyncio
async def test_manuell_hochgeladener_beleg_wird_ausgelesen(db, tmp_path):
    """Nach dem Hand-Upload muss der Beleg denselben Stand haben wie die
    automatisch geholten: Betrag vom BELEG, auch am Einkauf der Bestellung."""
    inv = _beleg_invoice(db, tmp_path)          # gespeichert 9.99, Mock-Beleg 10.08
    r = await pi.lies_und_uebernimm(db, invoice_id=inv.id)
    assert r["betrag"] == 10.08 and r["betrag_uebernommen"] is True
    db.refresh(inv)
    assert inv.amount == Decimal("10.08")
    assert inv.receipt_data["ship_to"]          # Empfaenger fuer die Zuordnung
    order = db.get(OrderAliexpress, inv.order_id)
    assert order.cost_cny == Decimal("10.08") and order.cost_source == "receipt"


@pytest.mark.asyncio
async def test_unlesbarer_upload_meldet_klar_und_behaelt_die_datei(db, tmp_path):
    inv = _beleg_invoice(db, tmp_path)
    pfad = inv.file_path
    from app.integrations import get_llm_client

    async def _fehler(*a, **k):
        return {"_fehler": "Could not process image"}
    import pytest as _pytest
    monkey = _pytest.MonkeyPatch()
    monkey.setattr(get_llm_client(), "read_purchase_receipt", _fehler)
    try:
        with pytest.raises(pi.BelegUnklar):
            await pi.lies_und_uebernimm(db, invoice_id=inv.id)
    finally:
        monkey.undo()
    db.refresh(inv)
    assert inv.file_path == pfad                # Datei bleibt gespeichert


def test_rechnung_kann_von_hand_hinterlegt_werden(client, db, tmp_path):
    """Hand-Upload der Rechnung legt sie NEBEN den Beleg – das Original bleibt."""
    inv = _beleg_invoice(db, tmp_path, ref="3075412823902077")
    beleg_pfad = inv.file_path

    antwort = client.post(f"/api/v1/invoices/{inv.id}/rechnung/upload",
                          files={"file": ("rechnung.pdf", b"%PDF-1.4 test", "application/pdf")})
    assert antwort.status_code == 201, antwort.text
    db.expire_all()
    frisch = db.get(Invoice, inv.id)
    assert frisch.generated_path and frisch.generated_path.endswith(".pdf")
    assert frisch.file_path == beleg_pfad       # Beleg unangetastet


def test_generator_warnt_statt_zu_blockieren(db, tmp_path):
    """Der Mensch hat den Beleg vor sich: geht die Rechnung nicht auf, wird
    gewarnt, aber gespeichert (Nutzer-Entscheidung 14.08.)."""
    inv = _beleg_invoice(db, tmp_path, ref="3075412823902088")
    eingabe = {"order_id": "3075412823902088", "order_date": "2026-08-11",
               "items": [{"title": "Testartikel", "unit_price": 4.75, "quantity": 1}],
               "subtotal": 4.75, "shipping": 1.76, "total": 99.00}   # geht nicht auf
    r = pi.rechnung_von_hand(db, invoice_id=inv.id, eingabe=eingabe, vorschau=False)
    assert r["warnung"] and "99.00" in r["warnung"]
    db.refresh(inv)
    assert inv.generated_path                      # trotzdem gespeichert


def test_generator_nutzt_dieselbe_vorlage(db, tmp_path):
    """Von Hand erstellte Rechnungen sehen aus wie die automatischen – eine Vorlage."""
    inv = _beleg_invoice(db, tmp_path, ref="3075412823902089")
    r = pi.rechnung_von_hand(db, invoice_id=inv.id, vorschau=True, eingabe={
        "order_id": "3075412823902089", "order_date": "2026-08-11",
        "items": [{"title": "Summer Fashion Slippers For Men", "properties": "brown,44-45",
                   "unit_price": 4.75, "quantity": 1}],
        "subtotal": 4.75, "shipping": 1.76,
        "extra_lines": [{"label": "Geschätzte Einfuhrabgaben", "amount": 3.57}],
        "total": 10.08})
    assert r["warnung"] is None
    assert "INVOICE" in r["html"] and "Import Duties" in r["html"]
    assert "<th>Total</th>" in r["html"] and "€ 10.08" in r["html"]


def test_generator_verlangt_artikel_und_gesamtbetrag(db, tmp_path):
    inv = _beleg_invoice(db, tmp_path, ref="3075412823902090")
    with pytest.raises(pi.BelegUnklar, match="Artikelzeile"):
        pi.rechnung_von_hand(db, invoice_id=inv.id, eingabe={"total": 5}, vorschau=True)
    with pytest.raises(pi.BelegUnklar, match="Gesamtbetrag"):
        pi.rechnung_von_hand(db, invoice_id=inv.id, vorschau=True, eingabe={
            "items": [{"title": "X", "unit_price": 1.0, "quantity": 1}]})


def test_vorlage_ist_aus_dem_beleg_vorausgefuellt(db, tmp_path):
    inv = _beleg_invoice(db, tmp_path, ref="3075412823902091")
    inv.receipt_data = {"ship_to": "Andreas K., Zum Sonnenborn 11", "store_name": "Shop123 Store",
                        "order_date": "11. Aug. 2026", "total": 10.08,
                        "items": [{"title": "Slipper", "unit_price": 4.75, "quantity": 1}]}
    db.commit()
    v = pi.vorlage_fuer_rechnung(db, invoice_id=inv.id)
    assert v["ship_to"].startswith("Andreas") and v["order_date"] == "2026-08-11"
    assert v["items"][0]["title"] == "Slipper" and v["total"] == 10.08


def test_dashboard_wird_nicht_veraltet_ausgeliefert(client):
    """Ohne no-cache zeigt der Browser nach einem Deploy die ALTE Oberflaeche –
    real passiert: der Rechnungs-Generator war live, aber unsichtbar."""
    # "/klassisch" ist am 25.08.2026 entfallen - die Zweitkopie der Oberflaeche
    # gibt es nicht mehr, Hell/Dunkel macht "/" selbst.
    antwort = client.get("/")
    assert antwort.status_code == 200
    assert "no-cache" in antwort.headers.get("cache-control", "")
