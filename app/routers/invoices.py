"""Bereich 3: Belegablage-Endpoints (Spec Kap. 3.3)."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.integrations import get_storage
from app.models import Invoice
from app.retry import PersistentError
from app.schemas import (
    InvoiceAttachRequest,
    InvoiceAttachResponse,
    InvoiceSearchRequest,
    InvoiceSearchResponse,
)
from app.services import invoice_service, purchase_invoice

router = APIRouter(prefix="/api/v1/invoices", tags=["Bereich 3 – Belegablage"])


@router.get("/list")
def list_invoices(type: str = "all", db: Session = Depends(get_db)):
    """Alle Belege mit Details + Aggregat (Belege-Seite)."""
    return invoice_service.list_invoices(db, type=type)


@router.post("/belegzeilen-nachtragen")
def belegzeilen_nachtragen(jahr: int | None = None, anwenden: bool = False,
                           db: Session = Depends(get_db)):
    """Bestellungen ohne Eintrag in der Belegablage zaehlen – und auf Wunsch anlegen.

    Ohne ``anwenden`` reine Vorschau (nach Jahr aufgeschluesselt). Stornierte und
    geloeschte Kaeufe bleiben aussen vor.
    """
    return invoice_service.fehlende_belegzeilen(db, jahr=jahr, anwenden=anwenden)


@router.post("/backfill")
def backfill(db: Session = Depends(get_db)):
    """Fehlende Belege fuer ALLE Verkaeufe und Kaeufe nachtragen."""
    return invoice_service.backfill_invoices(db)


# --- Verkaufsrechnungen fuer eigene, bei eBay verkaufte Motive (Studio/POD) -----
# Stehen zusammen mit den Belegen in EINER Liste (siehe /list) - entstehen automatisch
# bei jedem Bestellabgleich; dieser Knopf holt Fehlendes nach.
@router.post("/pod-verkauf/nachtragen")
def pod_verkaufsrechnungen_nachtragen(db: Session = Depends(get_db)):
    """Fuer jede bezahlte POD-Bestellung ohne Rechnung eine erzeugen (Nachtrag/Reparatur)."""
    return invoice_service.generate_missing_pod_sale_invoices(db)


# --- Original-Belege (echte Downloads vom lokalen Backfill-Client) ---
# Auth: NICHT per Login-Session, sondern per X-Backfill-Token-Header (AuthMiddleware
# laesst nur /originals/* mit gueltigem Token durch).
_ALLOWED_ORIG_EXT = {"png", "jpg", "jpeg", "webp", "pdf"}


@router.get("/originals/todo")
def originals_todo(tage: int | None = None, db: Session = Depends(get_db)):
    """Arbeitsliste: Kaeufe/Verkaeufe ohne echten Original-Beleg (fuer den Backfill-Client).

    Standard: nur die letzten Tage (``BELEG_FENSTER_TAGE``). Aeltere Kaeufe ohne Beleg
    bekommen bei AliExpress keinen mehr – sie jedes Mal anzufahren kostet nur Zeit.
    ``tage=0`` schaut wieder ueber alles (einmaliger Aufhol-Lauf)."""
    fenster = invoice_service.BELEG_FENSTER_TAGE if tage is None else (tage or None)
    return invoice_service.list_originals_todo(db, tage=fenster)


@router.post("/originals/discover-orders")
def discover_orders(body: dict = Body(...), db: Session = Depends(get_db)):
    """Fehlende AliExpress-Bestellungen aus der gescrapten Bestellliste anlegen (Backfill-Client)."""
    return invoice_service.discover_aliexpress_orders(db, body.get("orders") or [])


@router.post("/originals/attach", status_code=201)
async def originals_attach(
    invoice_type: str = Form(...),
    file: UploadFile = File(...),
    order_id: int | None = Form(None),
    sale_id: int | None = Form(None),
    amount: float | None = Form(None),
    invoice_number: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Einen ECHTEN Original-Beleg (AE-Bild / eBay-PDF) ablegen (vom lokalen Backfill-Client)."""
    if invoice_type not in ("aliexpress_purchase", "ebay_sales"):
        raise HTTPException(status_code=400, detail="invoice_type ungueltig")
    if order_id is None and sale_id is None and not invoice_number:
        raise HTTPException(status_code=400, detail="order_id, sale_id oder invoice_number noetig")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Leere Datei")
    ext = ((file.filename or "").rsplit(".", 1)[-1] or "").lower()
    if ext not in _ALLOWED_ORIG_EXT:
        ext = "pdf" if invoice_type == "ebay_sales" else "png"
    try:
        return invoice_service.attach_original_receipt(
            db, file_bytes=data, ext=ext, invoice_type=invoice_type,
            order_id=order_id, sale_id=sale_id, amount=amount,
            invoice_number=invoice_number)
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/expense", status_code=201)
async def add_expense(
    datum: str = Form(...),
    amount: float = Form(...),
    category: str = Form(...),
    description: str = Form(""),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    """Manuelle Betriebsausgabe erfassen (AutoDS-Abo, KI/API, Hosting, Temu, ...) – optional mit Beleg."""
    from datetime import datetime, timezone
    try:
        when = datetime.strptime(datum, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="Datum muss im Format YYYY-MM-DD sein")
    if amount is None or amount <= 0:
        raise HTTPException(status_code=400, detail="Betrag muss groesser 0 sein")
    data = ext = None
    if file is not None and file.filename:
        data = await file.read()
        ext = ((file.filename or "").rsplit(".", 1)[-1] or "pdf").lower()
        if ext not in _ALLOWED_ORIG_EXT:
            ext = "pdf"
    return invoice_service.add_manual_expense(
        db, when=when, amount=amount, category=category, description=description,
        file_bytes=data or None, ext=ext)


@router.post("/sync-fees")
async def sync_fees(days: int = 90, db: Session = Depends(get_db)):
    """Echte eBay-Gebuehren je Order aus der Finances API holen (Steuer/Bruttomarge)."""
    from app.services import finance_service
    try:
        return await finance_service.sync_ebay_fees(db, days=days)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Gebuehren-Sync fehlgeschlagen: {exc}")


@router.get("/tax-report")
def tax_report(year: int | None = None, format: str = "json", db: Session = Depends(get_db)):
    """Jahres-Steuer-Report: Einnahmen − echte eBay-Gebuehren − EK = Bruttomarge je Verkauf."""
    from datetime import datetime, timezone

    from fastapi.responses import Response

    from app.services import finance_service
    y = year or datetime.now(timezone.utc).year
    if format == "csv":
        data = finance_service.tax_report_csv(db, year=y)
        return Response(content=data, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=steuer-report-{y}.csv"})
    return finance_service.tax_report(db, year=y)


@router.post("/bewirtung-vorschlaege")
async def bewirtung_vorschlaege(body: dict = Body(...), db: Session = Depends(get_db)):
    """Drei Formulierungs-Vorschlaege fuer den Anlass eines Bewirtungsbelegs (zur Auswahl).

    Reine Formulierhilfe: schreibt NICHTS in die DB. Der Nutzer waehlt den Vorschlag, der
    zum tatsaechlichen Treffen passt – das Programm kann das nicht wissen und behauptet es
    auch nicht.

    Ziel ist eine ausgewogene Auswahl, KEINE Wiederholungsfreiheit: derselbe Anlass darf
    wiederkommen, nur nicht mehrmals am Stueck. Hart gesperrt sind deshalb nur die in
    dieser Sitzung schon gezeigten Vorschlaege (`vermeiden`) und die Anlaesse der letzten
    beiden Bewirtungen; alles Aeltere geht als weicher Hinweis mit.
    """
    from app.integrations import get_llm_client

    # DB-Lesezugriff VOR dem LLM-Call – keine offene Transaktion ueber den langsamen
    # Netz-Call halten (Projektregel 12: SQLite-Schreibsperre).
    benutzt = invoice_service.recent_bewirtung_anlaesse(db, limit=15)
    gezeigt = [str(v) for v in (body.get("vermeiden") or []) if str(v or "").strip()]
    try:
        # Gaeste werden bewusst NICHT uebergeben: der Anlass soll nur das Thema nennen,
        # die Namen stehen handschriftlich auf der Quittung bzw. in der eigenen Zeile
        # des Eigenbelegs (Nutzer-Vorgabe 27.07.).
        return await get_llm_client().suggest_bewirtung_anlaesse(
            ort=(body.get("ort") or ""),
            art=(body.get("art") or ""), hinweis=(body.get("hinweis") or ""),
            vermeiden=(gezeigt + benutzt[:2]), zuletzt_benutzt=benutzt[2:])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Vorschläge fehlgeschlagen: {exc}")


@router.post("/bewirtung-eigenbeleg", status_code=201)
async def bewirtung_eigenbeleg(
    datum: str = Form(...),
    ort: str = Form(...),
    gastgeber: str = Form(...),
    gaeste: str = Form(...),
    anlass: str = Form(...),
    betrag: float = Form(...),
    trinkgeld: float = Form(0.0),
    art: str = Form(""),
    notiz: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Eigenbeleg Gastronomie erzeugen: Quittungsfoto + Pflichtangaben -> fertiges PDF.

    Fuer Quittungen, auf denen kein Platz war, die Angaben handschriftlich zu
    ergaenzen. Das erzeugte PDF wird direkt als Betriebsausgabe abgelegt und ist
    ueber /invoices/{id}/download abrufbar (ausdrucken + unterschreiben).
    """
    from datetime import datetime, timezone

    from app.services.eigenbeleg_pdf import BildFehler

    try:
        when = datetime.strptime(datum, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="Datum muss im Format YYYY-MM-DD sein")
    if betrag is None or betrag <= 0:
        raise HTTPException(status_code=400, detail="Rechnungsbetrag muss groesser 0 sein")
    if trinkgeld and trinkgeld < 0:
        raise HTTPException(status_code=400, detail="Trinkgeld darf nicht negativ sein")
    fehlend = [name for name, wert in (("Ort", ort), ("Gastgeber", gastgeber),
                                       ("Gäste", gaeste), ("Anlass", anlass))
               if not (wert or "").strip()]
    if fehlend:
        raise HTTPException(status_code=400,
                            detail="Pflichtangabe fehlt: " + ", ".join(fehlend))
    quittung = await file.read()
    if not quittung:
        raise HTTPException(status_code=400, detail="Das Foto der Quittung fehlt")
    try:
        return invoice_service.add_bewirtung_eigenbeleg(
            db, when=when, ort=ort.strip(), gastgeber=gastgeber.strip(),
            gaeste=gaeste.strip(), anlass=anlass.strip(), betrag=betrag,
            trinkgeld=trinkgeld or 0.0, art=art.strip(), quittung=quittung,
            notiz=notiz.strip())
    except BildFehler as exc:
        # Klartext fuer den Nutzer (falsches Dateiformat o.ae.), kein Server-Fehler.
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/tax-export")
def tax_export(year: int | None = None, db: Session = Depends(get_db)):
    """Steuerberater-Paket (ZIP): alle Original-Belege + Index-CSV + Summen-Datei je Jahr."""
    from datetime import datetime, timezone

    from fastapi.responses import Response

    y = year or datetime.now(timezone.utc).year
    data = invoice_service.tax_export_zip(db, year=y)
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=steuer-export-{y}.zip"})


@router.get("/ebay-finance-report")
async def ebay_finance(year: int | None = None, format: str = "csv",
                       refresh: bool = False, db: Session = Depends(get_db)):
    """eBay-Finanzbericht (Monat/Quartal/Jahr) aus der Finances-API – Einnahmen-Nachweis.
    Gecacht; refresh=true erzwingt Neuberechnung (langsam)."""
    from datetime import datetime, timezone

    from fastapi.responses import Response

    from app.services import finance_service
    y = year or datetime.now(timezone.utc).year
    try:
        rep = await finance_service.ebay_finance_report(year=y, refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Finanzbericht fehlgeschlagen: {exc}")
    if format == "csv":
        return Response(content=finance_service.ebay_finance_report_csv(rep),
                        media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=ebay-finanzbericht-{y}.csv"})
    return rep


@router.get("/ebay-finance-report/details")
async def ebay_finance_details(period: str, kategorie: str, year: int | None = None):
    """Einzelposten hinter EINER Zahl im eBay-Finanzbericht (Klick auf eine Kachel/Zelle).

    Liest aus den beim letzten Bericht mitgespeicherten Rohtransaktionen - kein neuer
    eBay-Aufruf, also schnell. Ohne vorherigen Bericht (noch nie geladen/aktualisiert)
    kommt eine verstaendliche Fehlermeldung statt eines leeren Ergebnisses.
    """
    from datetime import datetime, timezone

    from app.services import finance_service
    y = year or datetime.now(timezone.utc).year
    if kategorie not in ("brutto", "gebuehren", "versandlabel", "erstattung", "netto"):
        raise HTTPException(status_code=422, detail=f"Unbekannte Kategorie '{kategorie}'.")
    try:
        return finance_service.finance_report_details(year=y, period=period, kategorie=kategorie)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/ebay-gebuehren-aufschluesselung")
async def ebay_gebuehren_aufschluesselung(year: int | None = None):
    """DIAGNOSE (nur lesen): eBay-Gebuehren nach Art – welche haengen an keiner Bestellung?

    Antwortet auf die Frage, woraus die Differenz zwischen Finanzbericht und
    Steuerbericht besteht. Schreibt nichts, aendert nichts."""
    from datetime import datetime, timezone

    from app.services import finance_service
    y = year or datetime.now(timezone.utc).year
    try:
        return await finance_service.gebuehren_aufschluesselung(year=y)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Aufschluesselung fehlgeschlagen: {exc}")


# Hier lagen bis 08.09.2026 fuenf Adressen des Beleg-Helfers: Er holte die
# ORIGINAL-Kaufbelege von AliExpress ueber einen angemeldeten Chrome auf dem
# Rechner des Nutzers, weil der Server an so eine Anmeldung nicht herankommt.
# Medienwerk kauft nichts ein - die Kosten entstehen bei der Bilderzeugung und
# beim Druckdienstleister, und beide stellen ihre Belege selbst zu.


@router.post("/gebuehren-nachziehen")
async def gebuehren_nachziehen(year: int | None = None, db: Session = Depends(get_db)):
    """Echte eBay-Gebuehren EINES JAHRES nachziehen (der Nachtlauf schaut nur 45 Tage).

    Fuer 2026 fehlten dadurch ~935 EUR Gebuehren mit Bestellbezug — im Februar und
    Maerz stand faktisch nichts. Das Fenster beginnt einen Monat VOR dem Jahr, damit
    Gebuehren zu Januar-Bestellungen vollstaendig erfasst sind; geschrieben wird aber
    nur auf Verkaeufe ab dem 1.1. (``nur_ab``), damit Vorjahres-Verkaeufe nicht mit
    Teilbetraegen ueberschrieben werden.
    """
    from datetime import datetime, timezone

    from app.services import finance_service
    y = year or datetime.now(timezone.utc).year
    jahresanfang = datetime(y, 1, 1, tzinfo=timezone.utc)
    try:
        return await finance_service.sync_ebay_fees(
            db,
            date_from=datetime(y - 1, 12, 1, tzinfo=timezone.utc),
            date_to=datetime(y, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
            max_pages=60,          # ~12.000 Transaktionen; bricht sonst LAUT ab
            nur_ab=jahresanfang)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Nachziehen fehlgeschlagen: {exc}")


@router.post("/generate/sale/{sale_id}", status_code=201)
def generate_sale(sale_id: int, db: Session = Depends(get_db)):
    """Verkaufsrechnung fuer eine eBay-Sale erzeugen (idempotent; USt-Ausweis ab Stichtag)."""
    try:
        return invoice_service.generate_sale_invoice(db, sale_id=sale_id)
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/generate/purchase/{order_id}", status_code=201)
def generate_purchase(order_id: int, db: Session = Depends(get_db)):
    """AliExpress-Kaufbeleg fuer eine Bestellung ablegen (idempotent)."""
    try:
        return invoice_service.record_purchase_invoice(db, order_id=order_id)
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/belege/vollstaendig-lesen")
async def belege_vollstaendig_lesen(limit: int = 50, db: Session = Depends(get_db)):
    """Belege komplett auslesen (v.a. den Empfaenger) – Grundlage fuer die Zuordnung."""
    return await purchase_invoice.lies_belege_vollstaendig(db, limit=limit)


# „Einkauf und Verkauf zuordnen" ist am 08.09.2026 entfallen. Es fuehrte die
# AliExpress-Bestellung mit dem passenden eBay-Verkauf zusammen - zwei Haelften
# desselben Vorgangs. Bei eigenen Motiven gibt es diese Zweiteilung nicht: Der
# Druckdienstleister produziert erst auf die Bestellung hin, es gibt keinen
# vorgelagerten Einkauf, der zugeordnet werden muesste.


@router.post("/belege/betraege-pruefen")
async def betraege_pruefen(limit: int = 50, jahr: int | None = None, nochmal: bool = False,
                           db: Session = Depends(get_db)):
    """Gespeicherte Einkaufsbetraege gegen die echten Belege pruefen und korrigieren.

    Bereits gepruefte Belege werden uebersprungen (``nochmal=true`` erzwingt sie).
    """
    return await purchase_invoice.pruefe_betraege(db, limit=limit, jahr=jahr, nochmal=nochmal)


@router.get("/rechnungen/stand")
def rechnungen_stand(jahr: int | None = None, db: Session = Depends(get_db)):
    """Wie viele Kaufbelege haben schon eine Rechnung – und was fehlt noch?"""
    return purchase_invoice.offene_rechnungen(db, jahr=jahr)["counts"]


@router.post("/rechnungen/erzeugen")
async def rechnungen_erzeugen(jahr: int | None = None, limit: int = 50,
                              db: Session = Depends(get_db)):
    """Offene Kaufbelege in Rechnungen umsetzen (haeppchenweise, wiederholbar)."""
    return await purchase_invoice.erzeuge_alle_rechnungen(db, jahr=jahr, limit=limit)


@router.post("/{invoice_id}/rechnung", status_code=201)
async def rechnung_aus_beleg(invoice_id: int, force: bool = False,
                             db: Session = Depends(get_db)):
    """Aus dem AliExpress-BELEG die RECHNUNG erzeugen und danebenlegen (idempotent).

    Der Original-Beleg bleibt unveraendert. Ist der Beleg nicht eindeutig auswertbar,
    kommt 422 – dann wird bewusst KEINE Rechnung mit geratenen Zahlen erzeugt.
    """
    try:
        return await purchase_invoice.erzeuge_rechnung(db, invoice_id=invoice_id, force=force)
    except purchase_invoice.BelegUnklar as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{invoice_id}/rechnung/vorlage")
def rechnung_vorlage(invoice_id: int, db: Session = Depends(get_db)):
    """Vorausgefuellte Werte fuer den Rechnungs-Editor (aus dem gelesenen Beleg)."""
    try:
        return purchase_invoice.vorlage_fuer_rechnung(db, invoice_id=invoice_id)
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{invoice_id}/rechnung/erstellen")
def rechnung_erstellen(invoice_id: int, vorschau: bool = True, body: dict = Body(...),
                       db: Session = Depends(get_db)):
    """Rechnung aus den Formular-Werten erzeugen (Vorschau) bzw. ablegen.

    Geht die Rechnung nicht auf, wird nur GEWARNT – der Mensch hat den Beleg vor
    sich und entscheidet selbst (anders als der automatische Weg).
    """
    try:
        return purchase_invoice.rechnung_von_hand(db, invoice_id=invoice_id, eingabe=body,
                                                  vorschau=vorschau)
    except purchase_invoice.BelegUnklar as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{invoice_id}/beleg-auslesen")
async def beleg_auslesen(invoice_id: int, db: Session = Depends(get_db)):
    """Einen (gerade hochgeladenen) Beleg auslesen und Betrag/Empfaenger uebernehmen."""
    try:
        return await purchase_invoice.lies_und_uebernimm(db, invoice_id=invoice_id)
    except purchase_invoice.BelegUnklar as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{invoice_id}/rechnung/upload", status_code=201)
async def rechnung_hochladen(invoice_id: int, file: UploadFile = File(...),
                             db: Session = Depends(get_db)):
    """Eine Rechnung von Hand hinterlegen (falls der Generator gerade nicht kann).

    Der BELEG bleibt unangetastet – die Rechnung liegt wie immer daneben.
    """
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Beleg nicht gefunden")
    daten = await file.read()
    if not daten:
        raise HTTPException(status_code=400, detail="Leere Datei")
    ext = ((file.filename or "").rsplit(".", 1)[-1] or "").lower()
    if ext not in ("pdf", "html", "htm", "png", "jpg", "jpeg", "webp"):
        raise HTTPException(status_code=400,
                            detail="Nur PDF, HTML oder Bild (PNG/JPG) möglich")
    datum = inv.invoice_date or datetime.now(timezone.utc)
    stored = get_storage().store(content=daten, period=datum.strftime("%Y-%m"),
                                 file_type="rechnung_aliexpress",
                                 ref_id=str(inv.reference_id or inv.id), ext=ext)
    inv.generated_path = stored.file_path
    inv.generated_hash = stored.file_hash
    db.commit()
    return {"invoice_id": inv.id, "gespeichert": stored.file_path, "hochgeladen": True}


@router.get("/{invoice_id}/rechnung")
def rechnung_anzeigen(invoice_id: int, db: Session = Depends(get_db)):
    """Die erzeugte Rechnung anzeigen (nicht den Beleg – der laeuft ueber /download)."""
    inv = db.get(Invoice, invoice_id)
    if inv is None or not inv.generated_path:
        raise HTTPException(status_code=404, detail="Zu diesem Beleg gibt es noch keine Rechnung")
    try:
        data = get_storage().read(inv.generated_path)
    except OSError as exc:
        raise HTTPException(status_code=404, detail=f"Rechnungsdatei fehlt: {exc}")
    name = f"Rechnung_{inv.reference_id or inv.id}.html"
    return Response(content=data, media_type="text/html; charset=utf-8",
                    headers={"Content-Disposition": f'inline; filename="{name}"'})


_AUTOPRINT_SKRIPT = b"<script>window.addEventListener('load',()=>setTimeout(()=>window.print(),150));</script>"


@router.get("/{invoice_id}/download")
def download(invoice_id: int, db: Session = Depends(get_db), drucken: bool = False):
    """Belegdatei herunterladen/anzeigen (HTML-Rechnung / TXT-Beleg).

    ``drucken=true``: bei einer HTML-Rechnung oeffnet sich sofort der Druckdialog
    (der Beleg selbst bleibt unveraendert, das Skript wird nur der Antwort beigefuegt)."""
    try:
        data, name, ctype = invoice_service.read_invoice_file(db, invoice_id=invoice_id)
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if drucken and ctype.startswith("text/html") and b"</body>" in data:
        data = data.replace(b"</body>", _AUTOPRINT_SKRIPT + b"</body>", 1)
    inline = ctype.startswith("text/html") or ctype.startswith("image/") or ctype == "application/pdf"
    disposition = "inline" if inline else "attachment"
    return Response(content=data, media_type=ctype,
                    headers={"Content-Disposition": f'{disposition}; filename="{name}"'})


@router.get("/drucken")
def mehrere_drucken(ids: str, db: Session = Depends(get_db)):
    """Mehrere Belege zu EINEM Druckvorgang zusammenfassen (Sammeldruck der Auswahl).

    Nur HTML-Belege (selbst erzeugte Rechnungen) lassen sich zusammenfuegen - Fotos/PDFs
    von Hand hochgeladener Kaufbelege bleiben aussen vor und werden separat genannt,
    damit nichts stillschweigend fehlt."""
    try:
        invoice_ids = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=422, detail="ids muss eine Liste von Zahlen sein, z. B. 1,2,3")
    if not invoice_ids:
        raise HTTPException(status_code=422, detail="Keine Belege ausgewaehlt.")
    seiten: list[bytes] = []
    uebersprungen: list[str] = []
    for iid in invoice_ids:
        try:
            data, name, ctype = invoice_service.read_invoice_file(db, invoice_id=iid)
        except PersistentError:
            uebersprungen.append(str(iid))
            continue
        if not ctype.startswith("text/html"):
            uebersprungen.append(name)
            continue
        koerper = data.split(b"<body>", 1)
        koerper = koerper[1].rsplit(b"</body>", 1)[0] if len(koerper) == 2 else data
        seiten.append(b'<section style="page-break-after:always">' + koerper + b"</section>")
    if not seiten:
        raise HTTPException(status_code=404, detail="Keine der ausgewaehlten Belege lassen sich drucken "
                            "(nur selbst erzeugte Rechnungen, keine hochgeladenen Fotos/PDFs).")
    hinweis = (f'<p style="color:#b00;font-family:sans-serif">Nicht enthalten (eigene Datei, bitte '
              f'einzeln oeffnen): {", ".join(uebersprungen)}</p>' if uebersprungen else "")
    seite = (b'<!doctype html><html lang="de"><head><meta charset="utf-8"><title>Belege drucken</title></head>'
            b"<body>" + hinweis.encode("utf-8") + b"".join(seiten) + _AUTOPRINT_SKRIPT + b"</body></html>")
    return Response(content=seite, media_type="text/html; charset=utf-8")


@router.post("/{invoice_id}/replace-file")
async def replace_invoice_file(
    invoice_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Die Datei eines selbst erfassten Belegs austauschen (Eintrag bleibt bestehen).

    Fuer nachtraeglich korrigierte Quittungsfotos – u.a. Reparatur des Ueberschreib-Bugs,
    bei dem sich Belege derselben Kategorie eines Monats eine Datei geteilt haben.
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Leere Datei")
    ext = ((file.filename or "").rsplit(".", 1)[-1] or "").lower()
    if ext not in _ALLOWED_ORIG_EXT:
        raise HTTPException(
            status_code=400,
            detail="Nur PDF, JPG, PNG oder WEBP – bitte eine andere Datei waehlen.")
    try:
        return invoice_service.replace_expense_file(
            db, invoice_id=invoice_id, file_bytes=data, ext=ext)
    except PersistentError as exc:
        code = 404 if "nicht gefunden" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc))


@router.delete("/kauf/{invoice_id}")
def delete_purchase(invoice_id: int, db: Session = Depends(get_db)):
    """Stornierten Wareneinkauf entfernen (Zeile + Beleg + erzeugte Rechnung).

    Setzt die Bestellung auf ``cancelled`` – sonst legt der naechste Abgleich den
    Beleg wieder an und das Loeschen haelt nicht. Die Sicherheitsabfrage sitzt im
    Dashboard; was entfernt wurde, steht im Aktivitaets-Log.
    """
    try:
        return invoice_service.delete_purchase_invoice(db, invoice_id=invoice_id)
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/{invoice_id}")
def delete_invoice(invoice_id: int, db: Session = Depends(get_db)):
    """Einen selbst erfassten Beleg endgueltig loeschen (Zeile + hochgeladene Datei).

    Nur manuelle Betriebsausgaben – siehe invoice_service.delete_manual_expense.
    Die Sicherheitsabfrage sitzt im Dashboard; hier wird ohne Rueckfrage geloescht.
    """
    try:
        return invoice_service.delete_manual_expense(db, invoice_id=invoice_id)
    except PersistentError as exc:
        code = 404 if "nicht gefunden" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc))


@router.post("/attach/{order_id}", response_model=InvoiceAttachResponse, status_code=201)
async def attach(order_id: int, body: InvoiceAttachRequest, db: Session = Depends(get_db)):
    try:
        return await invoice_service.attach_invoices(
            db, order_id=order_id, bank_transaction_id=body.bank_transaction_id
        )
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/search", response_model=InvoiceSearchResponse)
def search(body: InvoiceSearchRequest, db: Session = Depends(get_db)):
    results = invoice_service.search_invoices(
        db, date_from=body.date_from, date_to=body.date_to,
        amount_min=body.amount_min, amount_max=body.amount_max, type=body.type,
    )
    return {"results": results}
