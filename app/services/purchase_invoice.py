"""AliExpress-Kaufrechnung aus dem ECHTEN Beleg erzeugen (Belegablage).

Ablauf (Nutzer-Workflow):
1. Kunde kauft auf eBay -> 2. wir kaufen ueber das Programm auf AliExpress ->
3. AliExpress erstellt einen BELEG (Bestelluebersicht als Bild, liegt bereits als
   Original in der Belegablage) -> **hier**: aus diesem Beleg die RECHNUNG nach der
   Vorlage erzeugen -> 4. Rechnung neben dem Beleg ablegen.

BELEG != RECHNUNG: Der Beleg ist das Original von AliExpress und bleibt unangetastet
(``Invoice.file_path``/``is_original``). Die erzeugte Rechnung kommt als ZUSAETZLICHE
Datei daneben (``Invoice.generated_path``) – sie ersetzt das Original nie.

Harte Regeln (Nutzer-Vorgabe + Projektregel 14):
- Die Rechnung ist ein 1:1-Nachbau der AliExpress-Rechnung. Nichts dazuerfinden:
  kein Zusatztext, keine erfundenen Zeilen, kein nachgebautes Logo.
- Der Artikelname ist der AliExpress-Name VOM BELEG – nie der deutsche eBay-Titel.
- Die Betraege sind die vom Beleg – nie aus App-Daten abgeleitet oder geschaetzt.
- Nur Zeilen zeigen, die der Beleg auch hat. Fehlt ein Wert, bleibt die Zeile weg.
- Die dritte Gebuehrenspalte spiegelt den Beleg:
  „Geschaetzte Einfuhrabgaben" -> ``Import Duties`` (wird zum Total ADDIERT),
  „MwSt inbegriffen"/„VAT included" -> ``VAT included`` (im Total bereits ENTHALTEN).
- Geht die Rechnung nicht auf, wird NICHTS geraten: die Rechnung wird nicht erzeugt,
  der Fall kommt als „pruefen" zurueck.
"""
from __future__ import annotations

import base64
import html
import re
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from sqlalchemy.orm import Session

from app.integrations import get_llm_client, get_storage
from app.models import Invoice
from app.retry import PersistentError

# Cent-Toleranz beim Gegenrechnen: AliExpress rundet die Belegzeilen einzeln, dadurch
# weicht die Summe echter Belege um bis zu 1 Cent ab (real: 11,49 - 0,11 + 0,01 = 11,39
# bei ausgewiesenem Total 11,38).
_RUNDUNGS_TOLERANZ = Decimal("0.02")


_MONATE = {
    "jan": 1, "feb": 2, "mar": 3, "mär": 3, "maer": 3, "apr": 4, "may": 5, "mai": 5,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "okt": 10, "nov": 11, "dec": 12, "dez": 12,
}
_MONATE_KURZ = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Kartenmarken -> Zahlart, wie AliExpress sie auf der Rechnung ausweist.
_KARTEN = ("visa", "mastercard", "maestro", "amex", "american express", "jcb", "discover",
           "credit", "debit", "karte", "card")


class BelegUnklar(PersistentError):
    """Beleg nicht auswertbar – bewusst KEINE Rechnung erzeugen, Mensch schaut drauf."""


# --------------------------------------------------------------------------- Helfer
def _dec(value) -> Decimal | None:
    """Zahl aus dem Beleg in Decimal – ohne zu raten (None bleibt None)."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", ".")).quantize(Decimal("0.01"))
    except (ArithmeticError, ValueError):
        return None


def _eur(value: Decimal) -> str:
    """Betrag wie auf der Rechnung: „€ 4.89", Abzuege als „- € 1.32"."""
    return f"- € {abs(value):.2f}" if value < 0 else f"€ {value:.2f}"


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def parse_beleg_datum(text: str | None) -> datetime | None:
    """„11. Aug. 2026" / „Jun 29, 2026" / „2026-08-11" -> datetime (sonst None)."""
    if not text:
        return None
    roh = str(text).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", roh)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d, tzinfo=timezone.utc)
        except ValueError:
            return None
    m = re.search(r"([A-Za-zÄÖÜäöü]{3,})", roh)
    monat = _MONATE.get(m.group(1)[:3].lower()) if m else None
    tag = re.search(r"\b(\d{1,2})\b(?!\d)", roh)
    jahr = re.search(r"\b(20\d{2})\b", roh)
    if not (monat and tag and jahr):
        return None
    try:
        return datetime(int(jahr.group(1)), monat, int(tag.group(1)), tzinfo=timezone.utc)
    except ValueError:
        return None


def _datum_anzeige(dt: datetime | None) -> str:
    return f"{dt.day}. {_MONATE_KURZ[dt.month - 1]} {dt.year}" if dt else ""


def _zahlart(brand: str | None) -> str:
    """VISA/Mastercard/... -> „Kredit-/Debitkarte" (so weist AliExpress es aus).

    Unbekannte Zahlart wird NICHT geraten – dann bleibt die Zeile leer.
    """
    b = (brand or "").strip()
    if not b:
        return ""
    return "Kredit-/Debitkarte" if any(k in b.lower() for k in _KARTEN) else b


def _gebuehr_label(roh: str | None) -> str:
    """Beschriftung der Belegzeile -> Beschriftung auf der Rechnung.

    Der Beleg ist mal deutsch, mal englisch; die Rechnung ist englisch. Uebersetzt
    wird NUR, was bekannt ist – alles andere bleibt woertlich stehen (nichts erfinden).
    """
    t = " ".join(str(roh or "").split()).strip(" :")
    k = t.lower()
    if "einfuhr" in k or "import" in k or "zoll" in k or "customs" in k or "duti" in k:
        return "Import Duties"
    if "mehrwertsteuer" in k or "vat" in k:
        return "VAT included"
    if k in ("steuer", "steuern", "tax", "taxes"):
        return "Tax"
    if "rabatt" in k or "discount" in k:
        return "Discount"
    return t or "Fee"


def bestellnummern_passen(gelesen: str, erwartet: str) -> bool:
    """Gehoert der Beleg zu dieser Bestellung?

    Beim Lesen 16-stelliger Nummern faellt gelegentlich eine Ziffer weg (real:
    ``3072102669952059`` -> ``307210266995205``). Eine fehlende oder abweichende
    Einzelziffer ist deshalb erlaubt – eine voellig andere Bestellnummer nicht.
    Massgeblich fuer die Rechnung bleibt ohnehin die Nummer aus dem System.
    """
    a = "".join(c for c in str(gelesen or "") if c.isdigit())
    b = "".join(c for c in str(erwartet or "") if c.isdigit())
    if not a or not b:
        return True                      # nichts zu vergleichen -> nicht blockieren
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1 or min(len(a), len(b)) < 8:
        return False
    kurz, lang = (a, b) if len(a) <= len(b) else (b, a)
    if len(kurz) == len(lang):           # gleiche Laenge: genau eine Ziffer darf abweichen
        return sum(1 for x, y in zip(kurz, lang) if x != y) <= 1
    # eine Ziffer fehlt: kurz muss als Teilfolge in lang stecken
    i = 0
    for z in lang:
        if i < len(kurz) and kurz[i] == z:
            i += 1
    return i == len(kurz)


def _store_nummer(store_name: str | None) -> str:
    """„Shop1103840307 Store" -> „1103840307". Ohne Ziffernfolge: leer (nicht raten)."""
    m = re.search(r"(\d{6,})", str(store_name or ""))
    return m.group(1) if m else ""


@lru_cache(maxsize=1)
def _logo_data_uri() -> str:
    """Das ECHTE AliExpress-Logo (aus einer Original-Rechnung) als data-URI.

    Muss eingebettet sein: die Rechnung wird als eigenstaendige Datei archiviert und
    darf spaeter nicht von einer externen URL abhaengen (GoBD/Aufbewahrung).
    """
    pfad = Path(__file__).resolve().parent.parent / "static" / "aliexpress-logo.png"
    if not pfad.is_file():
        # Das Logo ging mit dem AliExpress-Abbau (13.09.2026). Rechnungen zu alten
        # Einkaeufen muessen fuer die Buchhaltung trotzdem entstehen - dann ohne Logo.
        return ""
    return "data:image/png;base64," + base64.b64encode(pfad.read_bytes()).decode("ascii")


def _logo_img() -> str:
    uri = _logo_data_uri()
    return f'<img alt="AliExpress" src="{uri}">' if uri else ""


# --------------------------------------------------------------- Beleg -> Rechnung
def normalisiere_beleg(roh: dict) -> dict:
    """Rohe Beleg-Felder pruefen und in die Rechnungsstruktur bringen.

    Rechnet die Summen GEGEN und wirft ``BelegUnklar``, wenn etwas fehlt oder nicht
    aufgeht – lieber keine Rechnung als eine mit erfundener Zahl.
    """
    if roh and roh.get("_fehler"):
        raise BelegUnklar(f"Beleg nicht auslesbar: {roh['_fehler']}")
    if not roh:
        raise BelegUnklar("Beleg konnte nicht ausgelesen werden.")

    posten = []
    for p in (roh.get("items") or []):
        titel = str(p.get("title") or "").strip()
        preis, menge = _dec(p.get("unit_price")), p.get("quantity")
        if not titel or preis is None or not menge:
            raise BelegUnklar("Artikelzeile unvollstaendig (Name, Stueckpreis oder Menge fehlt).")
        posten.append({"title": titel, "properties": str(p.get("properties") or "").strip(),
                       "unit_price": preis, "quantity": int(menge)})
    if not posten:
        raise BelegUnklar("Beleg enthaelt keine Artikelposition.")

    total = _dec(roh.get("total"))
    if total is None:
        raise BelegUnklar("Kein Gesamtbetrag auf dem Beleg gefunden.")

    subtotal = _dec(roh.get("subtotal"))
    if subtotal is None:
        subtotal = sum((p["unit_price"] * p["quantity"] for p in posten), Decimal("0"))
    rabatt, versand = _dec(roh.get("discount")), _dec(roh.get("shipping"))
    zusatz = []
    for z in (roh.get("extra_lines") or []):
        betrag = _dec(z.get("amount"))
        if betrag is None:
            raise BelegUnklar(f"Betragszeile '{z.get('label')}' ohne Betrag.")
        zusatz.append({"label": _gebuehr_label(z.get("label")), "amount": betrag})
    mwst_enthalten = _dec(roh.get("vat_included"))

    # 1. Gegenprobe: der bezahlte Betrag steht auf dem Beleg ein zweites Mal (Block
    #    „Zahlungs methode"). Stimmt er nicht mit dem Gesamtbetrag ueberein, wurde
    #    eine der beiden Zahlen falsch gelesen -> keine Rechnung.
    bezahlt = _dec(roh.get("paid_amount"))
    if bezahlt is not None and abs(bezahlt - total) > _RUNDUNGS_TOLERANZ:
        raise BelegUnklar(
            f"Gesamtbetrag {total:.2f} passt nicht zum bezahlten Betrag {bezahlt:.2f}.")

    # 2. Gegenprobe: die Zeilen muessen den Gesamtbetrag ergeben. Der Versand ist
    #    dabei NICHT immer additiv – manche Belege weisen eine Versandgebuehr aus,
    #    deren „Insgesamt" aber schon der „Gesamtsumme" entspricht (real: Beleg 1649,
    #    23,59 + 0,09 -> Insgesamt 23,59). Beide Lesarten gelten daher als korrekt;
    #    passt keine, wurde etwas falsch gelesen.
    basis = subtotal - (rabatt or Decimal("0")) + sum((z["amount"] for z in zusatz), Decimal("0"))
    varianten = {"mit Versand": basis + (versand or Decimal("0")), "ohne Versand": basis}
    if not any(abs(v - total) <= _RUNDUNGS_TOLERANZ for v in varianten.values()):
        gerechnet = " / ".join(f"{k}: {v:.2f}" for k, v in varianten.items())
        raise BelegUnklar(
            f"Beleg geht nicht auf: {gerechnet} vs. ausgewiesen {total:.2f}.")

    return {
        "order_id": str(roh.get("order_id") or "").strip(),
        "order_date": parse_beleg_datum(roh.get("order_date")),
        "items": posten,
        "store_name": str(roh.get("store_name") or "").strip(),
        "store_number": _store_nummer(roh.get("store_name")),
        "ship_to": str(roh.get("ship_to") or "").strip(),
        "payment_method": _zahlart(roh.get("payment_brand")),
        "subtotal": subtotal, "discount": rabatt, "shipping": versand,
        "extra_lines": zusatz, "vat_included": mwst_enthalten, "total": total,
        "currency": str(roh.get("currency") or "EUR"),
    }



def _summen_block(d: dict, *, gesamtzeile: bool) -> str:
    """Die vier Summenspalten einer Summenzeile – IMMER genau vier.

    Die Original-Rechnung hat unter Unit Price / Quantity / Tax per Unit / Line Total
    exakt vier Felder. Diese Aufteilung wird nie veraendert, damit das Linienbild dem
    Original entspricht. Hat ein Beleg einen Rabatt (fuenfter Wert), steht er als
    zweiter Betrag im selben Feld wie die Zwischensumme – statt eine Spalte zu
    erfinden oder die Trennlinien aufzuloesen.
    """
    def _paar(lbl: str, betrag: Decimal) -> str:
        if gesamtzeile and lbl == "Subtotal":
            lbl = "Subtotal (Items)"
        beschriftung = f"{lbl}:" if (not gesamtzeile or lbl == "Total") else lbl
        return (f'<div class="sum-lbl">{_esc(beschriftung)}</div>'
                f'<div class="sum-amt">{_eur(betrag)}</div>')

    def _feld(paare: list[str]) -> str:
        """Mehrere Betraege im selben Feld untereinander (erster ohne Abstand)."""
        return "".join(p if i == 0 else f'<div class="sum-zweit">{p}</div>'
                       for i, p in enumerate(paare))

    erste = [_paar("Subtotal", d["subtotal"])]
    if d.get("discount") is not None:
        erste.append(_paar("Discount", d["discount"]))

    zweite = [_paar("Shipping Cost", d["shipping"])] if d.get("shipping") is not None else []

    # Dritte Spalte: alle weiteren Belegzeilen (Einfuhrabgaben/Steuer, auch mehrere)
    # und – falls der Beleg ihn ausweist – der Hinweis auf die enthaltene MwSt.
    dritte = [_paar(z["label"], z["amount"]) for z in (d.get("extra_lines") or [])]
    if d.get("vat_included") is not None:
        dritte.append(_paar("VAT included", d["vat_included"]))

    return "".join(f'<td class="sum-cell">{_feld(p)}</td>'
                   for p in (erste, zweite, dritte, [_paar("Total", d["total"])]))


def rendere_rechnung(d: dict, *, empfaenger: list[str]) -> str:
    """Die Rechnung als eigenstaendiges HTML (A4, druckt 1:1 wie die Vorlage)."""
    zeilen = []
    for p in d["items"]:
        eigenschaften = (f'<div class="pprops">Product properties:{_esc(p["properties"])}</div>'
                         if p["properties"] else "")
        zeilen.append(
            f'<tr class="item-row"><td class="item-details">{_esc(p["title"])}{eigenschaften}</td>'
            f'<td class="cell-ctr">{_eur(p["unit_price"])}</td>'
            f'<td class="cell-ctr">{p["quantity"]}</td>'
            f'<td class="cell-ctr">{_eur(Decimal("0.00"))}</td>'
            f'<td class="cell-ctr">{_eur(p["unit_price"] * p["quantity"])}</td></tr>')

    infos = [("Order ID", d["order_id"]), ("Order date", _datum_anzeige(d["order_date"]))]
    if d["store_name"]:
        laden = f'{_esc(d["store_name"])} , Store number: {_esc(d["store_number"])}' \
            if d["store_number"] else _esc(d["store_name"])
        infos.append(("Store name", laden))
    infos += [("Ship to address", d["ship_to"]), ("Payment method", d["payment_method"])]
    info_html = "<br>".join(f"{k}: {v if k == 'Store name' else _esc(v)}"
                            for k, v in infos if v)
    return f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<title>Rechnung {_esc(d["order_id"])}</title>
<style>
*{{box-sizing:border-box}} html,body{{margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
background:#e9ebef;color:#3a3a3a}}
.sheet{{width:210mm;min-height:297mm;margin:0 auto;background:#fff;padding:15mm 14mm;
font-size:12px;line-height:1.45}}
.inv-head{{display:flex;justify-content:space-between;align-items:flex-start;padding-bottom:14px}}
.inv-head img{{height:34px;width:auto;display:block}}
.company{{text-align:right;color:#4a4a4a;font-size:12.5px;line-height:1.7}}
.rule{{border-bottom:1px solid #e5e7eb;margin:0 0 26px}}
.inv-title{{font-size:46px;font-weight:400;color:#3b3b3b;letter-spacing:.5px;margin:2px 0 20px}}
.meta{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:30px}}
.meta .k{{font-weight:700;font-size:12.5px;color:#6b7280;margin-top:16px}}
.meta .k:first-child{{margin-top:0}}
.meta .v{{font-size:13px;color:#111827;font-weight:700;margin-top:3px}}
.meta .right{{text-align:right;font-weight:700;font-size:13px;color:#1f2937;line-height:1.85}}
table{{width:100%;border-collapse:collapse;table-layout:fixed}}
thead th{{text-align:center;font-size:12.5px;font-weight:700;color:#2b2b2b;
border-bottom:1px solid #b9bec6;padding:0 6px 10px}}
thead th.first{{text-align:left}}
tbody>tr>td{{vertical-align:middle;padding:14px 8px}}
.item-row>td{{border-bottom:1px solid #d9dce1}}
.item-row>td+td{{border-left:1px solid #d9dce1}}
.item-details{{vertical-align:top;font-size:12.5px;line-height:1.5}}
.pprops{{color:#6b7280;margin-top:2px}}
.cell-ctr{{text-align:center;white-space:nowrap;font-size:12.5px}}
.order-row>td{{border-bottom:1px solid #d9dce1}}
.order-row>td+td{{border-left:1px solid #d9dce1}}
.order-info{{vertical-align:top;font-size:12.5px;color:#5b6470;line-height:1.85}}
.sum-cell{{vertical-align:top}} .sum-lbl{{color:#5b6470;font-size:12px}}
.sum-zweit{{margin-top:12px}}
.sum-amt{{color:#e62e04;font-weight:600;margin-top:4px;white-space:nowrap}}
.grand-row>td{{border-top:2px solid #9aa1ac;padding-top:16px}}
.grand-row>td+td{{border-left:1px solid #d9dce1}}
.grand-total{{font-size:34px;color:#3b3b3b;font-weight:400;vertical-align:top}}
/* Der Knopf gehoert NICHT zum Beleg: er verschwindet beim Drucken/PDF-Erzeugen,
   das Dokument selbst bleibt dadurch 1:1 wie die Original-Rechnung. */
.pdfbtn{{position:fixed;top:14px;right:14px;z-index:9;font:600 13px/1 -apple-system,
BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;padding:10px 14px;
border:0;border-radius:8px;background:#e62e04;color:#fff;cursor:pointer;
box-shadow:0 4px 14px rgba(230,46,4,.3)}}
@page{{size:A4;margin:0}}
@media print{{body{{background:#fff}} .sheet{{margin:0;min-height:auto}} .pdfbtn{{display:none}}}}
</style></head><body>
<button class="pdfbtn" onclick="window.print()">Als PDF speichern</button>
<div class="sheet">
  <div class="inv-head">
    {_logo_img()}
    <div class="company"><div>Alibaba Group</div><div>969 West Wen Yi Road</div>
    <div>Yu Hang District, Hangzhou, Zhejiang 311121, China</div></div>
  </div>
  <div class="rule"></div>
  <div class="inv-title">INVOICE</div>
  <div class="meta">
    <div class="left">
      <div class="k">Invoice Number</div><div class="v">{_esc(d["order_id"])}</div>
      <div class="k">Date Of Issue</div><div class="v">{_esc(_datum_anzeige(d["order_date"]))}</div>
    </div>
    <div class="right">{"<br>".join(_esc(z) for z in empfaenger if z)}</div>
  </div>
  <table>
    <colgroup><col style="width:46%"><col style="width:13.5%"><col style="width:13.5%">
    <col style="width:13.5%"><col style="width:13.5%"></colgroup>
    <thead><tr><th class="first">Item Details</th><th>Unit Price</th><th>Quantity</th>
    <th>Tax per Unit</th><th>Total</th></tr></thead>
    <tbody>{"".join(zeilen)}</tbody>
    <tbody>
      <tr class="order-row"><td class="order-info">{info_html}</td>
      {_summen_block(d, gesamtzeile=False)}</tr>
      <tr class="grand-row"><td class="grand-total">Total:</td>
      {_summen_block(d, gesamtzeile=True)}</tr>
    </tbody>
  </table>
</div></body></html>"""


# ------------------------------------------------------------------- Orchestrierung
def _lies_belegdatei(storage, file_path: str) -> bytes:
    """Beleg-Bild lesen – auch wenn der Pfad aus der Windows-Zeit stammt.

    Alt-Eintraege stehen als ``data\\invoices\\...`` in der DB; auf dem Linux-VPS ist
    das EIN Dateiname mit Backslashes und schlaegt fehl (live: Download 500). Deshalb
    zusaetzlich die normalisierte Schreibweise und – als letzte Stufe – die Suche nach
    dem Dateinamen in der Belegablage probieren.
    """
    kandidaten = [file_path]
    normalisiert = file_path.replace("\\", "/")
    if normalisiert != file_path:
        kandidaten.append(normalisiert)
    basis = getattr(storage, "base_dir", None)
    name = normalisiert.rsplit("/", 1)[-1]
    if basis is not None:
        # gleicher Monatsordner + Dateiname, unabhaengig vom gespeicherten Prefix
        teile = normalisiert.split("/")
        if len(teile) >= 2:
            kandidaten.append(str(Path(basis) / teile[-2] / name))
        kandidaten.append(str(Path(basis) / name))

    for pfad in kandidaten:
        try:
            return storage.read(pfad)
        except OSError:
            continue
    if basis is not None:                     # letzte Stufe: irgendwo in der Ablage?
        treffer = next(Path(basis).rglob(name), None) if name else None
        if treffer is not None:
            try:
                return storage.read(str(treffer))
            except OSError:
                pass
    raise BelegUnklar(f"Beleg-Datei nicht auffindbar: {file_path}")


def _als_json(daten: dict) -> dict:
    """Beleg-Daten als Audit-Spur speichern (JSON-fest: Decimal/datetime -> Text/Zahl).

    Damit ist spaeter nachvollziehbar, welche Werte aus dem Beleg in die Rechnung
    geflossen sind – ohne das Bild erneut auslesen zu muessen (GoBD).
    """
    def _wert(v):
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, datetime):
            return v.date().isoformat()
        if isinstance(v, dict):
            return {k: _wert(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_wert(x) for x in v]
        return v

    return {**{k: _wert(v) for k, v in daten.items()},
            "gelesen_am": datetime.now(timezone.utc).isoformat()}


def _empfaenger_zeilen() -> list[str]:
    """Empfaenger der AliExpress-Rechnung: die bei AliExpress hinterlegte Firma.

    Nicht ``seller_*`` – das ist die eBay-Shop-Marke und stuende so nie auf einer
    AliExpress-Rechnung.
    """
    from app.config import get_settings

    roh = (get_settings().ae_invoice_recipient or "").replace("\n", "|")
    return [z.strip() for z in roh.split("|") if z.strip()]


def vorlage_fuer_rechnung(db: Session, *, invoice_id: int) -> dict:
    """Werte fuer den Rechnungs-Editor – vorausgefuellt aus dem gelesenen Beleg.

    Der Mensch soll korrigieren, nicht abtippen: was vom Beleg bekannt ist, steht
    schon drin.
    """
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise PersistentError("Beleg nicht gefunden")
    d = inv.receipt_data if isinstance(inv.receipt_data, dict) else {}
    datum = parse_beleg_datum(d.get("order_date")) or inv.invoice_date
    return {
        "invoice_id": inv.id,
        "order_id": str(inv.reference_id or ""),
        "order_date": datum.date().isoformat() if datum else "",
        "store_name": d.get("store_name") or "",
        "ship_to": d.get("ship_to") or "",
        "payment_method": _zahlart(d.get("payment_brand")) or "Kredit-/Debitkarte",
        "items": [{"title": p.get("title") or "", "properties": p.get("properties") or "",
                   "unit_price": p.get("unit_price"), "quantity": p.get("quantity") or 1}
                  for p in (d.get("items") or [])] or [
            {"title": "", "properties": "", "unit_price": None, "quantity": 1}],
        "subtotal": d.get("subtotal"), "discount": d.get("discount"),
        "shipping": d.get("shipping"),
        "extra_lines": d.get("extra_lines") or [],
        "vat_included": d.get("vat_included"),
        "total": d.get("total") if d.get("total") is not None else (
            float(inv.amount) if inv.amount is not None else None),
        "hat_rechnung": bool(inv.generated_path),
    }


def baue_rechnungsdaten(eingabe: dict) -> tuple[dict, str | None]:
    """Formular-Eingaben in die Rechnungsstruktur bringen + Rechen-Warnung.

    Anders als beim automatischen Weg wird hier NICHT abgelehnt, wenn die Summe
    nicht aufgeht: der Mensch hat den Beleg vor sich und entscheidet. Er bekommt
    aber eine deutliche Warnung (Nutzer-Entscheidung 14.08.).
    """
    posten = []
    for p in (eingabe.get("items") or []):
        titel = str(p.get("title") or "").strip()
        preis = _dec(p.get("unit_price"))
        try:
            menge = int(p.get("quantity") or 0)
        except (TypeError, ValueError):
            menge = 0
        if not titel or preis is None or menge <= 0:
            continue
        posten.append({"title": titel, "properties": str(p.get("properties") or "").strip(),
                       "unit_price": preis, "quantity": menge})
    if not posten:
        raise BelegUnklar("Mindestens eine Artikelzeile mit Name, Preis und Menge angeben.")

    total = _dec(eingabe.get("total"))
    if total is None:
        raise BelegUnklar("Bitte den Gesamtbetrag eintragen.")
    subtotal = _dec(eingabe.get("subtotal"))
    if subtotal is None:
        subtotal = sum((p["unit_price"] * p["quantity"] for p in posten), Decimal("0"))

    zusatz = []
    for z in (eingabe.get("extra_lines") or []):
        betrag = _dec(z.get("amount"))
        if betrag is None:
            continue
        zusatz.append({"label": _gebuehr_label(z.get("label")), "amount": betrag})

    rabatt, versand = _dec(eingabe.get("discount")), _dec(eingabe.get("shipping"))
    summe = (subtotal - (rabatt or Decimal("0")) + (versand or Decimal("0"))
             + sum((z["amount"] for z in zusatz), Decimal("0")))
    warnung = None
    if abs(summe - total) > _RUNDUNGS_TOLERANZ:
        warnung = (f"Die Zeilen ergeben {summe:.2f} €, als Gesamtbetrag steht {total:.2f} € — "
                   "bitte gegen den Beleg prüfen.")

    daten = {
        "order_id": str(eingabe.get("order_id") or "").strip(),
        "order_date": parse_beleg_datum(eingabe.get("order_date")),
        "items": posten,
        "store_name": str(eingabe.get("store_name") or "").strip(),
        "store_number": _store_nummer(eingabe.get("store_name")),
        "ship_to": str(eingabe.get("ship_to") or "").strip(),
        "payment_method": str(eingabe.get("payment_method") or "").strip(),
        "subtotal": subtotal, "discount": rabatt, "shipping": versand,
        "extra_lines": zusatz, "vat_included": _dec(eingabe.get("vat_included")),
        "total": total, "currency": "EUR",
    }
    return daten, warnung


def rechnung_von_hand(db: Session, *, invoice_id: int, eingabe: dict,
                      vorschau: bool = True) -> dict:
    """Rechnung aus den Formular-Werten erzeugen – Vorschau oder speichern.

    Gerendert wird mit DEMSELBEN Renderer wie die automatischen Rechnungen; es gibt
    nur eine Vorlage, sonst laufen die beiden Wege mit der Zeit auseinander.
    """
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise PersistentError("Beleg nicht gefunden")
    daten, warnung = baue_rechnungsdaten(eingabe)
    html_doc = rendere_rechnung(daten, empfaenger=_empfaenger_zeilen())
    if vorschau:
        return {"vorschau": True, "html": html_doc, "warnung": warnung}

    datum = daten["order_date"] or inv.invoice_date or datetime.now(timezone.utc)
    abgelegt = get_storage().store(content=html_doc.encode("utf-8"),
                                   period=datum.strftime("%Y-%m"),
                                   file_type="rechnung_aliexpress",
                                   ref_id=str(inv.reference_id or inv.id), ext="html")
    inv.generated_path = abgelegt.file_path
    inv.generated_hash = abgelegt.file_hash
    db.commit()
    return {"vorschau": False, "gespeichert": abgelegt.file_path, "warnung": warnung}


async def lies_und_uebernimm(db: Session, *, invoice_id: int) -> dict:
    """EINEN Beleg auslesen und die Werte uebernehmen – nach manuellem Upload.

    Damit ein von Hand hochgeladener Beleg denselben Stand hat wie die automatisch
    geholten: Betrag und Empfaenger kommen vom Beleg, nicht aus der Kalkulation.
    Unklares wird gemeldet, nicht geraten – der Beleg bleibt trotzdem gespeichert.
    """
    from app.models import OrderAliexpress

    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise PersistentError("Beleg nicht gefunden")
    pfad = inv.file_path
    if not pfad:
        raise BelegUnklar("Zu diesem Eintrag ist keine Datei hinterlegt.")
    db.rollback()                                  # Projektregel 12

    bild = _lies_belegdatei(get_storage(), pfad)   # wirft BelegUnklar
    endung = (pfad.rsplit(".", 1)[-1] or "png").lower()
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp"}.get(endung)
    if mime is None:
        raise BelegUnklar(f"Aus einer .{endung}-Datei lese ich nichts aus – "
                          "der Beleg ist aber gespeichert.")

    roh = await get_llm_client().read_purchase_receipt(image_bytes=bild, media_type=mime)
    if not roh or roh.get("_fehler"):
        raise BelegUnklar((roh or {}).get("_fehler", "Beleg nicht auslesbar"))

    total = _dec(roh.get("total"))
    bezahlt = _dec(roh.get("paid_amount"))
    waehrung = str(roh.get("currency") or "EUR").upper()
    if total is None:
        raise BelegUnklar("Auf dem Beleg war kein Gesamtbetrag lesbar.")
    if bezahlt is not None and abs(bezahlt - total) > _RUNDUNGS_TOLERANZ:
        raise BelegUnklar(f"Gesamtbetrag {total:.2f} passt nicht zum Zahlbetrag {bezahlt:.2f}.")

    inv = db.get(Invoice, invoice_id)
    inv.receipt_data = {**(inv.receipt_data or {}), "total": float(total), "currency": waehrung,
                        "ship_to": roh.get("ship_to"), "store_name": roh.get("store_name"),
                        "order_date": roh.get("order_date"), "items": roh.get("items") or [],
                        "gelesen_am": datetime.now(timezone.utc).isoformat(),
                        "geprueft_am": datetime.now(timezone.utc).isoformat()}
    uebernommen = False
    if waehrung == "EUR":                          # Fremdwaehrung nicht als Euro buchen
        inv.amount = total
        if inv.order_id:
            order = db.get(OrderAliexpress, inv.order_id)
            # "bank" nicht ueberschreiben: das ist der tatsaechlich abgebuchte
            # Betrag und damit die staerkste Quelle. Bei EUR-Kaeufen sind beide
            # ohnehin gleich; weichen sie ab, zaehlt fuer die EUeR der Abfluss.
            if order is not None and (order.cost_source or "") != "bank":
                order.cost_cny = total
                order.cost_source = "receipt"
        uebernommen = True
    db.commit()
    return {"invoice_id": invoice_id, "betrag": float(total), "waehrung": waehrung,
            "betrag_uebernommen": uebernommen, "empfaenger": roh.get("ship_to")}


async def lies_belege_vollstaendig(db: Session, *, limit: int = 50) -> dict:
    """Belege komplett auslesen – vor allem den EMPFAENGER (Lieferadresse).

    Der Empfaenger ist der eBay-Kaeufer und damit der Schluessel, um Einkauf und
    Verkauf zusammenzufuehren (viele Vorgaenge stehen doppelt im System: einmal die
    Einkaufs-Haelfte mit Beleg, einmal die Verkaufs-Haelfte mit Sendungsnummer).

    Nur Belege ohne bereits gelesenen Empfaenger werden angefasst; die Daten landen
    in ``receipt_data`` und werden spaeter auch fuer die Rechnung wiederverwendet.
    """
    from sqlalchemy import select

    from app.models import OrderAliexpress

    # Belege von Einkaeufen OHNE Verkaufsbezug zuerst lesen – nur die braucht die
    # Zuordnung, so gibt es die ersten Treffer sofort statt erst nach Stunden.
    ohne_verkauf = {o.id for o in db.scalars(select(OrderAliexpress)).all() if o.sale_id is None}
    offen: list[tuple[int, int]] = []
    for inv in db.scalars(
            select(Invoice).where(Invoice.type == "aliexpress_purchase")
            .order_by(Invoice.invoice_date.desc().nullslast(), Invoice.id.desc())).all():
        if not (inv.is_original or "original_" in (inv.file_path or "")):
            continue
        daten = inv.receipt_data if isinstance(inv.receipt_data, dict) else {}
        if daten.get("ship_to") or daten.get("lese_fehler"):
            continue
        offen.append((0 if inv.order_id in ohne_verkauf else 1, inv.id))
    offen.sort()
    offen = [inv_id for _, inv_id in offen]

    gelesen, fehler = 0, 0
    for inv_id in offen[:max(1, limit)]:
        inv = db.get(Invoice, inv_id)
        if inv is None:
            continue
        pfad = inv.file_path
        db.rollback()                      # Projektregel 12: nicht ueber den KI-Call halten
        try:
            bild = _lies_belegdatei(get_storage(), pfad)
        except BelegUnklar as exc:
            _merke_lesefehler(db, inv_id, str(exc))
            fehler += 1
            continue
        endung = (pfad.rsplit(".", 1)[-1] or "png").lower()
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "webp": "image/webp"}.get(endung)
        if mime is None:
            _merke_lesefehler(db, inv_id, f"Beleg ist .{endung}")
            fehler += 1
            continue
        roh = await get_llm_client().read_purchase_receipt(image_bytes=bild, media_type=mime)
        if not roh or roh.get("_fehler"):
            _merke_lesefehler(db, inv_id, (roh or {}).get("_fehler", "nicht auslesbar"))
            fehler += 1
            continue
        inv = db.get(Invoice, inv_id)
        inv.receipt_data = {**(inv.receipt_data or {}),
                            "ship_to": roh.get("ship_to"), "store_name": roh.get("store_name"),
                            "order_date": roh.get("order_date"), "items": roh.get("items") or [],
                            "gelesen_am": datetime.now(timezone.utc).isoformat()}
        db.commit()
        gelesen += 1

    return {"gelesen": gelesen, "fehler": fehler, "offen": max(0, len(offen) - gelesen - fehler)}


def _merke_lesefehler(db: Session, inv_id: int, grund: str) -> None:
    """Beleg als unlesbar vermerken – sonst faengt der naechste Lauf wieder hier an."""
    inv = db.get(Invoice, inv_id)
    if inv is not None:
        inv.receipt_data = {**(inv.receipt_data or {}), "lese_fehler": grund[:200]}
        db.commit()


async def pruefe_betraege(db: Session, *, limit: int = 50, jahr: int | None = None,
                          nochmal: bool = False) -> dict:
    """Gespeicherte Einkaufsbetraege gegen den ECHTEN Beleg pruefen und korrigieren.

    Der Beleg ist die Wahrheit: der bisher gespeicherte Betrag stammt aus der
    App-Kalkulation und weicht ab (real: Beleg 15,94 € vs. gespeichert 15,80 €).
    Korrigiert wird an BEIDEN Stellen – am Beleg-Eintrag und am Einkauf
    (``cost_cny``, Grundlage der Gewinnrechnung).

    Sicherungen: nur EUR-Belege (Fremdwaehrung bleibt unangetastet), und der
    Gesamtbetrag muss zum ebenfalls aufgedruckten Zahlbetrag passen. Was nicht
    eindeutig lesbar ist, wird gemeldet statt geraten.
    """
    from sqlalchemy import select

    from app.models import OrderAliexpress

    kandidaten = []
    for inv in db.scalars(
            select(Invoice).where(Invoice.type == "aliexpress_purchase")
            .order_by(Invoice.invoice_date.desc().nullslast(), Invoice.id.desc())).all():
        if jahr is not None and (inv.invoice_date is None or inv.invoice_date.year != jahr):
            continue
        if not (inv.is_original or "original_" in (inv.file_path or "")):
            continue
        # Schon GEPRUEFT? Dann ueberspringen, sonst arbeitet sich ein Lauf nie vor:
        # jeder Durchgang begaenne wieder bei denselben Belegen (Vorfall 14.08.).
        # Achtung: „ausgelesen" (receipt_data aus der Rechnungserstellung) ist NICHT
        # dasselbe wie „geprueft" – solche Belege sollen geprueft werden, kostenlos.
        if not nochmal and isinstance(inv.receipt_data, dict) \
                and inv.receipt_data.get("geprueft_am"):
            continue
        kandidaten.append(inv.id)

    korrigiert, unklar, geprueft = [], [], 0

    def _nicht_lesbar(inv_id: int, grund: str) -> None:
        """Unlesbaren Beleg vermerken – sonst haengt der naechste Lauf wieder daran.

        Real: Beleg 536 ist eine 0x0-Pixel-Datei; ohne Vermerk hat sich der Lauf
        14 Runden lang an genau diesem einen Beleg festgebissen (14.08.).
        Mit ``nochmal=true`` werden sie erneut versucht.
        """
        unklar.append({"invoice_id": inv_id, "grund": grund[:160]})
        eintrag = db.get(Invoice, inv_id)
        if eintrag is not None:
            eintrag.receipt_data = {**(eintrag.receipt_data or {}),
                                    "pruef_fehler": grund[:200],
                                    "geprueft_am": datetime.now(timezone.utc).isoformat()}
            db.commit()

    for inv_id in kandidaten:
        # limit zaehlt ALLE angefassten Belege (nicht nur die geaenderten) – sonst
        # laeuft ein Durchgang unvorhersehbar lange und kostet unnoetig KI-Aufrufe.
        if geprueft + len(unklar) >= max(1, limit):
            break
        inv = db.get(Invoice, inv_id)
        if inv is None:
            continue
        # 1. Kostenlos: liegt der Beleg schon ausgelesen vor, reicht die Audit-Spur.
        daten = inv.receipt_data if isinstance(inv.receipt_data, dict) else None
        total = _dec((daten or {}).get("total"))
        waehrung = str((daten or {}).get("currency") or "EUR").upper()
        if total is None:
            # 2. Sonst den Beleg lesen (Bild -> Vision).
            pfad, alt_betrag = inv.file_path, inv.amount
            db.rollback()                      # Projektregel 12: keine Transaktion offen halten
            try:
                bild = _lies_belegdatei(get_storage(), pfad)
            except BelegUnklar as exc:
                _nicht_lesbar(inv_id, str(exc))
                continue
            endung = (pfad.rsplit(".", 1)[-1] or "png").lower()
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "webp": "image/webp"}.get(endung)
            if mime is None:
                _nicht_lesbar(inv_id, f"Beleg ist .{endung}")
                continue
            roh = await get_llm_client().read_purchase_receipt(image_bytes=bild, media_type=mime)
            if not roh or roh.get("_fehler"):
                _nicht_lesbar(inv_id, (roh or {}).get("_fehler", "nicht auslesbar"))
                continue
            waehrung = str(roh.get("currency") or "EUR").upper()
            total = _dec(roh.get("total"))
            bezahlt = _dec(roh.get("paid_amount"))
            if total is None:
                _nicht_lesbar(inv_id, "kein Gesamtbetrag lesbar")
                continue
            if bezahlt is not None and abs(bezahlt - total) > _RUNDUNGS_TOLERANZ:
                _nicht_lesbar(inv_id, f"Total {total} passt nicht zum Zahlbetrag {bezahlt}")
                continue
            inv = db.get(Invoice, inv_id)      # nach dem Call frisch laden
            # gelesenen Wert merken -> naechster Lauf braucht keine KI mehr
            inv.receipt_data = {**(inv.receipt_data or {}), "total": float(total),
                                "currency": waehrung,
                                "geprueft_am": datetime.now(timezone.utc).isoformat()}
            del alt_betrag

        geprueft += 1
        # Pruef-Stempel: sorgt dafuer, dass der naechste Lauf hier nicht wieder anfaengt.
        inv.receipt_data = {**(inv.receipt_data or {}), "total": float(total),
                            "currency": waehrung,
                            "geprueft_am": datetime.now(timezone.utc).isoformat()}
        if waehrung != "EUR":                  # Fremdwaehrung bewusst nicht anfassen
            db.commit()
            continue
        alt = _dec(inv.amount)
        if alt is not None and abs(alt - total) <= Decimal("0.005"):
            db.commit()
            continue
        korrigiert.append({"invoice_id": inv.id, "referenz": inv.reference_id,
                           "alt": float(alt) if alt is not None else None, "neu": float(total)})
        inv.amount = total
        if inv.order_id:                       # der EK der Bestellung haengt daran
            order = db.get(OrderAliexpress, inv.order_id)
            # "bank" nicht ueberschreiben: das ist der tatsaechlich abgebuchte
            # Betrag und damit die staerkste Quelle. Bei EUR-Kaeufen sind beide
            # ohnehin gleich; weichen sie ab, zaehlt fuer die EUeR der Abfluss.
            if order is not None and (order.cost_source or "") != "bank":
                order.cost_cny = total
                order.cost_source = "receipt"
        db.commit()

    return {"geprueft": geprueft, "korrigiert": len(korrigiert), "unklar": len(unklar),
            "aenderungen": korrigiert, "probleme": unklar[:10],
            "kandidaten_gesamt": len(kandidaten)}


def offene_rechnungen(db: Session, *, jahr: int | None = None) -> dict:
    """Bestandsaufnahme: wo fehlt die Rechnung – und woran liegt es?

    ``bereit``     = Original-Beleg da, Rechnung fehlt -> kann erzeugt werden.
    ``beleg_fehlt``= Kauf ohne Original-Beleg -> erst der Beleg-Abruf von AliExpress.
    """
    from sqlalchemy import select

    bereit: list[int] = []
    beleg_fehlt: list[int] = []
    fertig = 0
    # Neueste Einkaeufe zuerst: sie sind fuer die laufende Buchhaltung die wichtigsten,
    # und ein abgebrochener Lauf hat dann wenigstens die aktuellen erledigt.
    alle = db.scalars(
        select(Invoice).where(Invoice.type == "aliexpress_purchase")
        .order_by(Invoice.invoice_date.desc().nullslast(), Invoice.id.desc())).all()
    for inv in alle:
        if jahr is not None and (inv.invoice_date is None or inv.invoice_date.year != jahr):
            continue
        if inv.generated_path:
            fertig += 1
        elif inv.is_original or "original_" in (inv.file_path or ""):
            bereit.append(inv.id)
        else:
            beleg_fehlt.append(inv.id)
    return {"bereit": bereit, "beleg_fehlt": beleg_fehlt, "fertig": fertig,
            "counts": {"bereit": len(bereit), "beleg_fehlt": len(beleg_fehlt), "fertig": fertig}}


def _klartext(grund: str) -> str:
    """Technische Fehlermeldung in einen Satz uebersetzen, der in der Zeile stehen kann.

    Im Tooltip der Belegablage stand sonst der rohe Python-Fehler
    ("BadRequestError: Error code: 400 - {'type': 'error'...}") — daraus liest
    niemand ab, was zu tun ist. Das Original bleibt in ``problem_technisch``.
    """
    g = grund or ""
    if "Could not process image" in g:
        return ("Die Beleg-Datei ist beschädigt und lässt sich nicht öffnen. "
                "Beleg bei AliExpress neu holen und hochladen.")
    if "overloaded" in g.lower() or "rate limit" in g.lower():
        return "Die Bilderkennung war ausgelastet — der nächste Durchlauf versucht es erneut."
    if "Beleg nicht auslesbar" in g:
        return ("Der Beleg liess sich nicht auslesen (technische Störung beim Lesedienst). "
                "Der nächste Durchlauf versucht es erneut.")
    return g          # Bereits verständlich formulierte Gründe unveraendert lassen


def _problem_merken(db: Session, invoice_id: int, grund: str) -> None:
    """Fehlgrund am Beleg festhalten, damit er in der Belegablage auffindbar bleibt.

    Ohne diesen Stempel steht der Grund nur in der Antwort des Laufs und ist danach
    weg — der Beleg saehe aus wie jeder andere offene. Landet in ``receipt_data``
    (JSON), deshalb ohne Datenbank-Migration.
    """
    try:
        inv = db.get(Invoice, invoice_id)
        if inv is None:
            return
        daten = dict(inv.receipt_data or {})
        klartext = _klartext(grund)
        daten["problem"] = klartext[:500]
        daten["problem_am"] = datetime.now(timezone.utc).isoformat()
        if klartext != grund:                 # Original fuer die Fehlersuche behalten
            daten["problem_technisch"] = grund[:800]
        inv.receipt_data = daten
        db.commit()
    except Exception:  # noqa: BLE001 – ein Stempel darf den Lauf nie stoppen
        db.rollback()


def _problem_loeschen(db: Session, invoice_id: int) -> None:
    """Nach erfolgreicher Rechnung den Fehlstempel entfernen (sonst bleibt es rot)."""
    try:
        inv = db.get(Invoice, invoice_id)
        if inv is None or not (inv.receipt_data or {}).get("problem"):
            return
        daten = {k: v for k, v in dict(inv.receipt_data).items()
                 if k not in ("problem", "problem_am", "problem_technisch")}
        inv.receipt_data = daten
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


async def erzeuge_alle_rechnungen(db: Session, *, jahr: int | None = None,
                                  limit: int = 50) -> dict:
    """Rechnungen fuer offene Kaufbelege erzeugen – in Haeppchen, fehlertolerant.

    Ein unklarer Beleg stoppt den Lauf NICHT: er landet in ``probleme`` und wird beim
    naechsten Lauf erneut versucht. ``limit`` haelt den Lauf kurz (Timeouts, Kosten)
    und macht ihn wiederholbar, bis ``offen`` 0 ist.
    """
    stand = offene_rechnungen(db, jahr=jahr)
    erzeugt, probleme = [], []
    for inv_id in stand["bereit"][:max(0, limit)]:
        try:
            r = await erzeuge_rechnung(db, invoice_id=inv_id)
            erzeugt.append({"invoice_id": inv_id, "total": r.get("total")})
            _problem_loeschen(db, inv_id)
        except BelegUnklar as exc:
            db.rollback()
            _problem_merken(db, inv_id, str(exc))
            probleme.append({"invoice_id": inv_id, "grund": str(exc)})
        except PersistentError as exc:  # noqa: PERF203 – je Beleg getrennt bewerten
            db.rollback()
            _problem_merken(db, inv_id, str(exc))
            probleme.append({"invoice_id": inv_id, "grund": str(exc)})
    return {"erzeugt": len(erzeugt), "probleme": probleme,
            "offen": max(0, stand["counts"]["bereit"] - len(erzeugt)),
            "beleg_fehlt": stand["counts"]["beleg_fehlt"],
            "fertig_vorher": stand["counts"]["fertig"], "details": erzeugt}


async def erzeuge_rechnung(db: Session, *, invoice_id: int, force: bool = False) -> dict:
    """Aus dem abgelegten Original-BELEG die RECHNUNG erzeugen und danebenlegen.

    Idempotent: existiert die Rechnung schon, wird sie nur zurueckgemeldet
    (``force=True`` erzeugt sie neu). Das Original bleibt in jedem Fall unberuehrt.
    """
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise PersistentError("Beleg nicht gefunden")
    if inv.type != "aliexpress_purchase":
        raise PersistentError("Nur AliExpress-Kaufbelege haben eine solche Rechnung")
    if inv.generated_path and not force:
        return {"invoice_id": inv.id, "generated_path": inv.generated_path, "created": False}
    if not (inv.file_path and (inv.is_original or "original_" in (inv.file_path or ""))):
        raise BelegUnklar("Zu diesem Kauf liegt noch kein Original-Beleg von AliExpress vor.")

    storage = get_storage()
    bild = _lies_belegdatei(storage, inv.file_path)
    endung = (inv.file_path.rsplit(".", 1)[-1] or "png").lower()
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp", "gif": "image/gif"}.get(endung)
    if mime is None:
        raise BelegUnklar(f"Beleg liegt als .{endung} vor – daraus lese ich nichts aus.")

    # Projektregel 12: vor dem langsamen KI-Call keine DB-Transaktion offen halten.
    referenz = str(inv.reference_id or "")
    db.rollback()

    roh = await get_llm_client().read_purchase_receipt(image_bytes=bild, media_type=mime)
    daten = normalisiere_beleg(roh)

    # Die Bestellnummer des Belegs muss zum Kauf passen – sonst waere die Rechnung
    # einem fremden Beleg zugeordnet.
    if daten["order_id"] and referenz and not bestellnummern_passen(daten["order_id"], referenz):
        raise BelegUnklar(
            f"Beleg gehoert zu Bestellung {daten['order_id']}, erwartet {referenz}.")
    # Auf der Rechnung steht die Nummer aus dem System: sie stammt direkt von
    # AliExpress und ist verlaesslicher als die aus dem Bild gelesene.
    if referenz:
        daten["order_id"] = referenz

    inv = db.get(Invoice, invoice_id)   # nach dem Call frisch laden und dann kurz schreiben

    html_doc = rendere_rechnung(daten, empfaenger=_empfaenger_zeilen()).encode("utf-8")
    datum = daten["order_date"] or inv.invoice_date or datetime.now(timezone.utc)
    abgelegt = storage.store(content=html_doc, period=datum.strftime("%Y-%m"),
                             file_type="rechnung_aliexpress", ref_id=str(inv.reference_id or inv.id),
                             ext="html")

    inv.generated_path = abgelegt.file_path
    inv.generated_hash = abgelegt.file_hash
    inv.receipt_data = _als_json(daten)
    # Der Betrag der Buchhaltung ist der TATSAECHLICH gezahlte Betrag vom Beleg.
    inv.amount = daten["total"]
    if daten["order_date"] and not inv.invoice_date:
        inv.invoice_date = daten["order_date"]
    # Ein frueherer Fehlschlag ist mit dieser Rechnung erledigt. HIER und nicht nur im
    # Sammellauf: sonst raeumt der Einzel-Knopf „Rechnung erstellen" den Stempel nicht
    # und der Kauf bleibt unter „Problemfaelle" stehen, obwohl die Rechnung dasteht.
    if (inv.receipt_data or {}).get("problem"):
        daten_neu = dict(inv.receipt_data)
        for k in ("problem", "problem_am", "problem_technisch"):
            daten_neu.pop(k, None)
        inv.receipt_data = daten_neu
    db.commit()
    return {"invoice_id": inv.id, "generated_path": inv.generated_path,
            "total": float(daten["total"]), "created": True}
