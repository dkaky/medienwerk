"""Tests fuer den Bewirtungs-Eigenbeleg: Quittungsfoto -> fertiges PDF-Blatt.

Der Beleg geht zum Steuerberater bzw. ins Finanzamt – deshalb wird hier geprueft,
dass wirklich JEDE Pflichtangabe (§ 4 Abs. 5 Satz 1 Nr. 2 EStG) auf dem Blatt steht
und der Gesamtbetrag gerechnet und nicht geschaetzt wird (Projektregel 14).

Bewusst OHNE pypdf/Pillow: beide stehen nicht in requirements.txt und waeren in der
CI nicht installiert. Die PDFs werden deshalb direkt ausgewertet.
"""
from __future__ import annotations

import base64
import struct
import zlib

import pytest

from app.services import eigenbeleg_pdf, invoice_service

# Echtes 8x12-Handyfoto-JPEG (Metadaten entfernt) – der Normalfall beim Hochladen.
MINI_JPG = base64.b64decode(
    "/9j/wAARCAAMAAgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUF"
    "BAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdI"
    "SUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJ"
    "ytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREA"
    "AgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2"
    "Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3"
    "uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9sAQwAcHBwcHBwwHBwwRDAwMERcRERERFx0XFxc"
    "XFx0i3R0dHR0dIuLi4uLi4uLp6enp6enw8PDw8Pb29vb29vb29vb/9sAQwEiJCQ4NDhgNDRg5Zt/m+Xl5eXl5eXl5eXl"
    "5eXl5eXl5eXl5eXl5eXl5eXl5eXl5eXl5eXl5eXl5eXl5eXl5eXl/90ABAAB/9oADAMBAAIRAxEAPwDoxS0UUAf/2Q=="
)

PFLICHT = dict(datum="24.07.2026", ort="Restaurant Adler", gastgeber="Aleyna Nur Aydin",
               gaeste="Herr Yilmaz", anlass="Einkaufskonditionen", betrag=86.40,
               trinkgeld=5.60)


def _png(breite: int, hoehe: int, *, alpha: bool = False) -> bytes:
    """Minimales gueltiges PNG bauen – das Projekt hat bewusst keine Bild-Bibliothek."""
    pixel = bytes([200, 120, 60] + ([128] if alpha else []))
    roh = b"".join(b"\x00" + pixel * breite for _ in range(hoehe))

    def chunk(typ: bytes, daten: bytes) -> bytes:
        return (struct.pack(">I", len(daten)) + typ + daten
                + struct.pack(">I", zlib.crc32(typ + daten) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", breite, hoehe, 8, 6 if alpha else 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(roh)) + chunk(b"IEND", b""))


def _sichtbarer_text(pdf: bytes) -> str:
    """Den auf dem Blatt gedruckten Text aus den Seiten-Inhaltsstroemen holen."""
    teile = []
    for stueck in pdf.split(b"stream\n")[1:]:
        roh = stueck.split(b"\nendstream")[0]
        try:
            teile.append(zlib.decompress(roh))
        except zlib.error:
            continue                       # Bilddaten (JPEG) – nicht zlib-komprimiert
    text = b"\n".join(teile).decode("cp1252", "replace")
    # Klammern stehen im PDF maskiert – zum Vergleichen wieder normal machen.
    return text.replace("\\(", "(").replace("\\)", ")")


def _seitenzahl(pdf: bytes) -> int:
    marke = b"/Type /Pages /Count "
    start = pdf.index(marke) + len(marke)
    return int(pdf[start:start + 4].split(b" ")[0])


# --- Das Blatt selbst -----------------------------------------------------

def test_eigenbeleg_ist_ein_gueltiges_pdf():
    pdf = eigenbeleg_pdf.build_eigenbeleg(quittung=MINI_JPG, **PFLICHT)
    assert pdf.startswith(b"%PDF-1.4")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert b"xref" in pdf and b"trailer" in pdf
    assert _seitenzahl(pdf) == 1


def test_alle_pflichtangaben_stehen_auf_dem_blatt():
    """Fehlt eine Pflichtangabe, erkennt das Finanzamt den Beleg nicht an."""
    pdf = eigenbeleg_pdf.build_eigenbeleg(
        quittung=MINI_JPG, art="Geschäftlich (Geschäftspartner/Kunden)", **PFLICHT)
    text = _sichtbarer_text(pdf)
    for pflicht in ("24.07.2026", "Restaurant Adler", "Aleyna Nur Aydin", "Herr Yilmaz",
                    "Einkaufskonditionen", "Geschäftlich"):
        assert pflicht in text, f"{pflicht!r} fehlt auf dem Eigenbeleg"
    for beschriftung in ("Datum Bewirtung", "Ort der Bewirtung", "Gastgeber",
                         "bewirtete Personen", "Genauer Anlass der", "Trinkgeld",
                         "Gesamtbetrag"):
        assert beschriftung in text, f"Zeile {beschriftung!r} fehlt"


def test_gesamtbetrag_wird_gerechnet_nicht_geschaetzt():
    pdf = eigenbeleg_pdf.build_eigenbeleg(quittung=MINI_JPG, **PFLICHT)
    text = _sichtbarer_text(pdf)
    assert "86,40 €" in text and "5,60 €" in text
    assert "92,00 €" in text, "Gesamt = Rechnung + Trinkgeld"


def test_langer_text_wird_umgebrochen_statt_abgeschnitten():
    """Nutzerwunsch: die Kaesten wachsen mit, nichts faellt hinten runter."""
    lang = ("Herr Ahmet Yilmaz (Lieferant Detailing-Zubehör GmbH), Frau Sabine Müller "
            "(Fotostudio Beispiel), Herr Klaus Peter Schmidt (Versandpartner)")
    pdf = eigenbeleg_pdf.build_eigenbeleg(quittung=MINI_JPG, **(PFLICHT | {"gaeste": lang}))
    text = _sichtbarer_text(pdf)
    for wort in lang.replace(",", " ").split():
        assert wort in text, f"{wort!r} ist beim Umbruch verloren gegangen"


def test_keine_unterschriftszeile():
    """Nutzer-Vorgabe: unterschrieben wird direkt auf der aufgeklebten Quittung –
    eine eigene Unterschriftszeile auf dem Blatt waere doppelt."""
    text = _sichtbarer_text(eigenbeleg_pdf.build_eigenbeleg(quittung=MINI_JPG, **PFLICHT))
    assert "Unterschrift" not in text
    assert "Ort, Datum" not in text


def test_kein_gesetzes_kleingedrucktes_auf_dem_blatt():
    """Nutzer-Vorgabe: der Beleg traegt die Angaben, nicht den Kommentar dazu.
    Die 70-%-Rechnung macht der Steuerberater. Geprueft wird mit dem Wert, den das
    Dashboard wirklich schickt – der Prozentsatz steht dort nur im Auswahltext."""
    text = _sichtbarer_text(eigenbeleg_pdf.build_eigenbeleg(
        quittung=MINI_JPG, art="Geschäftlich (Geschäftspartner/Kunden)", **PFLICHT))
    assert "Geschäftlich" in text, "die Art der Bewirtung gehoert weiter aufs Blatt"
    assert "abziehbar" not in text
    assert "70 %" not in text
    assert "Bestandteil dieses Belegs" not in text


def test_ohne_foto_bleibt_die_klebeflaeche_stehen():
    text = _sichtbarer_text(eigenbeleg_pdf.build_eigenbeleg(quittung=None, **PFLICHT))
    assert "aufkleben oder auf R" in text


# --- Quittungs-Foto -------------------------------------------------------

def test_normales_foto_passt_auf_eine_seite():
    pdf = eigenbeleg_pdf.build_eigenbeleg(quittung=_png(600, 800), **PFLICHT)
    assert _seitenzahl(pdf) == 1


def test_langer_kassenbon_kommt_zusaetzlich_gross_auf_seite_2():
    """Nutzerwunsch: ein schmaler Bon waere im Kasten unleserlich klein."""
    pdf = eigenbeleg_pdf.build_eigenbeleg(quittung=_png(120, 1400), **PFLICHT)
    assert _seitenzahl(pdf) == 2
    text = _sichtbarer_text(pdf)
    assert "Originalgröße auf Seite 2" in text
    assert "Bewirtungsquittung" in text


def test_png_mit_transparenz_wird_auf_weiss_verrechnet():
    pdf = eigenbeleg_pdf.build_eigenbeleg(quittung=_png(60, 80, alpha=True), **PFLICHT)
    assert _seitenzahl(pdf) == 1
    assert b"/DeviceRGB" in pdf


def test_jpeg_wandert_unveraendert_ins_pdf():
    """Kein Neukodieren – das Original-Foto bleibt als Beweisstueck erhalten."""
    pdf = eigenbeleg_pdf.build_eigenbeleg(quittung=MINI_JPG, **PFLICHT)
    assert b"/DCTDecode" in pdf
    assert MINI_JPG in pdf


@pytest.mark.parametrize("daten, erwartet", [
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "WEBP"),
    (b"%PDF-1.7\n...", "PDF"),
    (b"nicht mal ein Bild", "Unbekanntes Bildformat"),
    (b"", "kein Foto"),
])
def test_unbrauchbares_bild_meldet_klartext(daten, erwartet):
    """Lieber eine verstaendliche Meldung als ein kaputter Beleg."""
    with pytest.raises(eigenbeleg_pdf.BildFehler) as fehler:
        eigenbeleg_pdf.bild_vorbereiten(daten)
    assert erwartet in str(fehler.value)


def test_umlaute_und_fremde_buchstaben_bleiben_lesbar():
    """"Yılmaz" darf nicht als "Y?lmaz" auf dem Beleg landen."""
    pdf = eigenbeleg_pdf.build_eigenbeleg(
        quittung=None, **(PFLICHT | {"gaeste": "Herr Yılmaz", "ort": "Café Größe"}))
    text = _sichtbarer_text(pdf)
    assert "Yilmaz" in text and "?" not in text
    assert "Café Größe" in text


# --- Endpoint + Ablage ----------------------------------------------------

def _post_eigenbeleg(client, **abweichend):
    daten = {"datum": "2026-07-24", "ort": "Restaurant Adler",
             "gastgeber": "Aleyna Nur Aydin", "gaeste": "Herr Müller (Lieferant XY)",
             "anlass": "Einkaufskonditionen – Gesprächspartner: Herr Müller",
             "betrag": "86.40", "trinkgeld": "5.60",
             "art": "Geschäftlich (Geschäftspartner/Kunden)"}
    daten.update(abweichend)
    return client.post("/api/v1/invoices/bewirtung-eigenbeleg", data=daten,
                       files={"file": ("quittung.jpg", MINI_JPG, "image/jpeg")})


def test_endpoint_legt_den_eigenbeleg_als_ausgabe_ab(client, db):
    r = _post_eigenbeleg(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is True
    assert float(body["amount"]) == pytest.approx(92.00), "Gesamt = Rechnung + Trinkgeld"
    assert body["category"] == invoice_service.BEWIRTUNG_KATEGORIE

    # Das erzeugte Blatt liegt als echter Beleg in der Ablage und ist abrufbar.
    dl = client.get(f"/api/v1/invoices/{body['id']}/download")
    assert dl.status_code == 200
    assert dl.content.startswith(b"%PDF")
    assert "Einkaufskonditionen" in _sichtbarer_text(dl.content)


def test_erfasster_anlass_taucht_in_der_sperrliste_auf(client, db):
    """Der Eigenbeleg-Weg muss die Vorschlags-Historie genauso fuellen wie der
    Weg 'Quittung ist schon beschriftet' – sonst wiederholen sich die Gruende."""
    _post_eigenbeleg(client, anlass="Mindestabnahmemengen – Gesprächspartner: Herr Müller")
    assert invoice_service.recent_bewirtung_anlaesse(db) == [
        "Mindestabnahmemengen – Gesprächspartner: Herr Müller"]


@pytest.mark.parametrize("feld", ["ort", "gastgeber", "gaeste", "anlass"])
def test_fehlende_pflichtangabe_wird_abgelehnt(client, feld):
    r = _post_eigenbeleg(client, **{feld: "   "})
    assert r.status_code == 400
    assert "Pflichtangabe fehlt" in r.json()["detail"]


def test_betrag_muss_groesser_null_sein(client):
    assert _post_eigenbeleg(client, betrag="0").status_code == 400


def test_falsches_dateiformat_gibt_klartext_statt_serverfehler(client):
    r = client.post("/api/v1/invoices/bewirtung-eigenbeleg",
                    data={"datum": "2026-07-24", "ort": "Adler", "gastgeber": "Aydin",
                          "gaeste": "Herr Müller", "anlass": "Einkaufskonditionen",
                          "betrag": "20"},
                    files={"file": ("quittung.webp",
                                    b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp")})
    assert r.status_code == 400
    assert "JPG oder PNG" in r.json()["detail"]
