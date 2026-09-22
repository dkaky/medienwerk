"""Bereich 3: Belegablage (Spec Kap. 5.3).

AliExpress-Rechnung + eBay-Rechnung abrufen -> strukturiert ablegen
(/invoices/{YYYY-MM}/{type}_{id}.pdf, SHA256-Dedup) -> DB-Eintrag ->
Bank-Matching ueber Betrag.
"""
from __future__ import annotations

import csv
import html
import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations import get_ebay_client, get_storage
from app.models import BankTransaction, Invoice, Listing, OrderAliexpress, Product, Sale
from app.retry import PersistentError
from app.services.common import VOID_SALE_STATUS, task_log


def _period(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m")


async def attach_invoices(db: Session, *, order_id: int,
                          bank_transaction_id: int | None = None) -> dict:
    """Belege fuer eine Bestellung abrufen, ablegen und (optional) der Bank zuordnen."""
    order = db.get(OrderAliexpress, order_id)
    if order is None:
        raise PersistentError("Order nicht gefunden")
    sale = db.get(Sale, order.sale_id) if order.sale_id else None

    storage = get_storage()
    ebay = get_ebay_client()
    period = _period()
    created: list[Invoice] = []

    with task_log(db, task_type="invoice_attach", reference_id=order_id) as tl:
        # Step 1: AliExpress-Rechnung (Mock: PDF-Bytes; real: Selenium-Download)
        ae_amount = order.cost_cny or Decimal("0")
        ae_pdf = f"AliExpress Rechnung Order {order.aliexpress_order_id}".encode()
        ae_file = storage.store(
            content=ae_pdf, period=period, file_type="aliexpress_purchase",
            ref_id=str(order.aliexpress_order_id or order.id),
        )
        if not ae_file.deduped:
            created.append(Invoice(
                type="aliexpress_purchase",
                reference_id=str(order.aliexpress_order_id or order.id),
                invoice_number=f"AE_{order.aliexpress_order_id}",
                invoice_date=datetime.now(timezone.utc),
                amount=ae_amount, currency="CNY",
                file_path=ae_file.file_path, file_hash=ae_file.file_hash,
                sale_id=order.sale_id, order_id=order.id,
            ))

        # Step 2: eBay-Rechnung (ueber Fulfillment-API)
        if sale and sale.ebay_order_id:
            eb = await ebay.get_order(sale.ebay_order_id)
            eb_pdf = f"eBay Rechnung {eb.transaction_id}".encode()
            eb_file = storage.store(
                content=eb_pdf, period=period, file_type="ebay_sales",
                ref_id=str(sale.ebay_transaction_id),
            )
            if not eb_file.deduped:
                created.append(Invoice(
                    type="ebay_sales",
                    reference_id=str(sale.ebay_transaction_id),
                    invoice_number=f"eBay_{sale.ebay_transaction_id}",
                    invoice_date=datetime.now(timezone.utc),
                    amount=Decimal(str(eb.invoice_amount)), currency=eb.currency,
                    file_path=eb_file.file_path, file_hash=eb_file.file_hash,
                    sale_id=sale.id, order_id=order.id,
                ))

        for inv in created:
            db.add(inv)
        db.flush()

        # Step 5: Bank-Matching (explizit angegeben oder per Betrag/Datum)
        bank_match = None
        bank_tx = None
        if bank_transaction_id is not None:
            bank_tx = db.get(BankTransaction, bank_transaction_id)
        elif sale and sale.price_eur is not None:
            # NIE gegen den Kontist-Spiegel raten: der Betrag-Blindmatch (ohne Datum/
            # Vorzeichen/Gegenpartei) wuerde sonst beliebige gleichhohe Kontobuchungen
            # stempeln und den AliExpress-Abgleich vergiften (Review-Fund 11.08.).
            bank_tx = db.scalar(
                select(BankTransaction)
                .where(BankTransaction.amount == sale.price_eur)
                .where(BankTransaction.status == "pending")
                .where(or_(BankTransaction.bank_ref.is_(None),
                           BankTransaction.bank_ref.not_like("kontist:%")))
            )
        if bank_tx is not None:
            ids = list(bank_tx.invoices or [])
            ids.extend(inv.id for inv in created)
            bank_tx.invoices = ids
            bank_tx.status = "matched"
            bank_tx.sale_id = sale.id if sale else bank_tx.sale_id
            bank_tx.order_id = order.id
            bank_match = {"id": bank_tx.id, "status": "matched"}

        tl.result_data = {"invoice_count": len(created)}
        db.commit()

    return {
        "order_id": order.id,
        "invoices": [
            {"type": inv.type, "file_path": inv.file_path, "amount": inv.amount}
            for inv in created
        ],
        "bank_match": bank_match,
    }


# ==========================================================================
# Verkaufsrechnungen (eBay) + Kaufbelege – erzeugen & ablegen
#
# Umsatzsteuer: bis zum Stichtag UST_REGELBESTEUERUNG_AB § 19 (Kleinunternehmer,
# keine USt), ab dann Regelbesteuerung mit ausgewiesener USt. Schon erzeugte
# Rechnungen werden nie umgeschrieben - sie sind so ausgestellt.
# ==========================================================================
def _eur(value) -> str:
    return f"{Decimal(str(value or 0)):.2f} €".replace(".", ",")


def _next_sale_invoice_number(db: Session, *, typen: tuple[str, ...] = ("ebay_sales",)) -> str:
    s = get_settings()
    # ebay_sales (Dropshipping-Aera) und pod_sales (eigene Motive) teilen sich EINE
    # fortlaufende Nummerierung - keine Luecken/Doppelungen, falls beide je Belege haben.
    count = db.scalar(
        select(func.count()).select_from(Invoice).where(Invoice.type.in_(typen))
    ) or 0
    return f"{s.invoice_number_prefix}-{datetime.now(timezone.utc).year}-{count + 1:04d}"


def _ust_ab():
    """Stichtag der Regelbesteuerung als date - oder None, wenn nicht gesetzt/ungueltig."""
    from datetime import date as _date

    roh = (get_settings().ust_regelbesteuerung_ab or "").strip()
    try:
        return _date.fromisoformat(roh) if roh else None
    except ValueError:
        return None


def regelbesteuert(zeitpunkt: datetime | None) -> bool:
    """Gilt fuer einen Verkauf an diesem Tag die Regelbesteuerung (USt-Ausweis)?"""
    ab = _ust_ab()
    if ab is None:
        return False
    tag = (zeitpunkt or datetime.now(timezone.utc)).date()
    return tag >= ab


def _render_sale_invoice_html(*, number: str, date: datetime, sale: Sale, listing: Listing | None,
                              mit_ust: bool = False) -> str:
    s = get_settings()
    title = (listing.title_seo if listing else None) or "Artikel"
    qty = sale.quantity or 1
    total = Decimal(str(sale.price_eur or 0))
    unit = (total / qty) if qty else total
    addr = sale.delivery_address or {}
    buyer_lines = [sale.buyer_name or "Käufer"]
    for k in ("street", "address", "postal", "postcode", "zip", "city", "country"):
        if addr.get(k):
            buyer_lines.append(str(addr[k]))
    buyer = "<br>".join(html.escape(x) for x in buyer_lines)
    # Eine .env-Zeile kann keinen Zeilenumbruch tragen, deshalb trennt "|" die
    # Anschriftzeilen - dieselbe Schreibweise wie bei AE_INVOICE_RECIPIENT.
    seller_addr = "<br>".join(
        html.escape(teil.strip())
        for teil in s.seller_address.replace("|", "\n").split("\n")
        if teil.strip()
    )
    tax_line = f"<div>Steuernr./USt-IdNr.: {html.escape(s.seller_tax_id)}</div>" if s.seller_tax_id else ""
    leistung = (sale.sale_date or date).strftime("%d.%m.%Y")
    if mit_ust:
        satz = Decimal(str(s.ust_satz))
        netto = (total / (1 + satz)).quantize(Decimal("0.01"))
        ust = total - netto
        prozent = f"{satz * 100:.0f}"
        summen = (f'<tfoot><tr><td colspan="4" class="n">Nettobetrag</td><td class="n">{_eur(netto)}</td></tr>'
                  f'<tr><td colspan="4" class="n">zzgl. {prozent} % USt</td><td class="n">{_eur(ust)}</td></tr>'
                  f'<tr class="tot"><td colspan="4" class="n">Gesamtbetrag (brutto)</td>'
                  f'<td class="n">{_eur(total)}</td></tr></tfoot></table>')
        hinweis = (f'<div class="note">Alle Preise inkl. {prozent} % Umsatzsteuer. '
                   f'Leistungsdatum: {leistung}.</div>')
    else:
        summen = (f'<tfoot><tr class="tot"><td colspan="4" class="n">Gesamtbetrag</td>'
                  f'<td class="n">{_eur(total)}</td></tr></tfoot></table>')
        hinweis = ('<div class="note">Gemäß § 19 UStG (Kleinunternehmerregelung) wird keine '
                   f'Umsatzsteuer berechnet und ausgewiesen. Leistungsdatum: {leistung}.</div>')
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<title>Rechnung {html.escape(number)}</title>
<style>body{{font-family:Arial,sans-serif;color:#111;max-width:760px;margin:24px auto;padding:0 20px}}
h1{{font-size:22px}} .row{{display:flex;justify-content:space-between;gap:20px}}
table{{width:100%;border-collapse:collapse;margin-top:22px}} th,td{{border-bottom:1px solid #ccc;padding:8px;text-align:left}}
td.n,th.n{{text-align:right}} .tot{{font-weight:bold}} .muted{{color:#666;font-size:12px}}
.note{{margin-top:18px;padding:10px 12px;background:#f5f5f5;border-radius:6px;font-size:13px}}</style></head>
<body>
<div class="row"><div><b>{html.escape(s.seller_name)}</b><br>{seller_addr}{tax_line}
{('<div>'+html.escape(s.seller_email)+'</div>') if s.seller_email else ''}</div>
<div style="text-align:right"><h1>Rechnung</h1>
<div>Nr.: <b>{html.escape(number)}</b></div>
<div>Datum: {date.strftime('%d.%m.%Y')}</div>
<div class="muted">Transaktion: {html.escape(sale.ebay_transaction_id or '')}</div></div></div>
<div style="margin-top:26px"><div class="muted">Rechnung an</div>{buyer}</div>
<table><thead><tr><th>Pos.</th><th>Beschreibung</th><th class="n">Menge</th><th class="n">Einzel</th><th class="n">Betrag</th></tr></thead>
<tbody><tr><td>1</td><td>{html.escape(title)}</td><td class="n">{qty}</td><td class="n">{_eur(unit)}</td><td class="n">{_eur(total)}</td></tr></tbody>
{summen}
{hinweis}
<div class="muted" style="margin-top:24px">Vielen Dank für Ihren Einkauf.</div>
</body></html>"""


def generate_sale_invoice(db: Session, *, sale_id: int) -> dict:
    """Erzeugt die Verkaufsrechnung (HTML) fuer eine eBay-Sale und legt sie ab.

    Ab dem Stichtag der Regelbesteuerung mit USt-Ausweis, davor nach § 19.

    Idempotent: existiert bereits eine Verkaufsrechnung zur Transaktion, wird sie
    zurueckgegeben statt neu erzeugt.
    """
    sale = db.get(Sale, sale_id)
    if sale is None:
        raise PersistentError("Sale nicht gefunden")

    ref = str(sale.ebay_transaction_id or sale.id)
    existing = db.scalar(
        select(Invoice).where(Invoice.type == "ebay_sales", Invoice.reference_id == ref)
    )
    if existing is not None:
        return _invoice_dict(existing) | {"created": False}

    listing = db.get(Listing, sale.listing_id) if sale.listing_id else None
    now = datetime.now(timezone.utc)
    number = _next_sale_invoice_number(db)
    content = _render_sale_invoice_html(number=number, date=now, sale=sale, listing=listing,
                                        mit_ust=regelbesteuert(sale.sale_date or now)).encode("utf-8")
    stored = get_storage().store(content=content, period=_period(now),
                                 file_type="ebay_sales", ref_id=ref, ext="html")
    inv = Invoice(
        type="ebay_sales", reference_id=ref, invoice_number=number, invoice_date=now,
        amount=Decimal(str(sale.price_eur or 0)), currency="EUR",
        file_path=stored.file_path, file_hash=stored.file_hash,
        sale_id=sale.id, order_id=None,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return _invoice_dict(inv) | {"created": True}


def _render_pod_sale_invoice_html(*, number: str, date: datetime, order,
                                  positionen: list[dict], kaeufer: dict, mit_ust: bool = False) -> str:
    """Wie ``_render_sale_invoice_html``, aber fuer eine POD-Bestellung (Studio) mit
    mehreren Positionen statt genau einer ``Sale``-Zeile."""
    s = get_settings()
    total = Decimal(str(order.sale_total_eur or 0))
    buyer_lines = [kaeufer.get("name") or "Käufer"]
    for k in ("strasse", "plz", "ort", "land"):
        if kaeufer.get(k):
            buyer_lines.append(str(kaeufer[k]))
    buyer = "<br>".join(html.escape(x) for x in buyer_lines if x)
    seller_addr = "<br>".join(
        html.escape(teil.strip())
        for teil in s.seller_address.replace("|", "\n").split("\n")
        if teil.strip()
    )
    tax_line = f"<div>Steuernr./USt-IdNr.: {html.escape(s.seller_tax_id)}</div>" if s.seller_tax_id else ""
    leistung = (order.ordered_at or date).strftime("%d.%m.%Y")

    # Einzelpreis je Position ueber den Mengenanteil aus dem Gesamtbetrag geschaetzt -
    # eBay liefert den Positionspreis nicht separat in der Bestellliste, nur die Summe.
    gesamt_menge = sum(max(1, int(p.get("menge") or 1)) for p in positionen) or 1
    zeilen = []
    for n, p in enumerate(positionen, start=1):
        menge = max(1, int(p.get("menge") or 1))
        anteil = (total * menge / gesamt_menge).quantize(Decimal("0.01"))
        einzel = (anteil / menge).quantize(Decimal("0.01"))
        titel = p.get("titel") or "Artikel"
        zeilen.append(f'<tr><td>{n}</td><td>{html.escape(titel)}</td><td class="n">{menge}</td>'
                      f'<td class="n">{_eur(einzel)}</td><td class="n">{_eur(anteil)}</td></tr>')
    zeilen_html = "".join(zeilen) or '<tr><td colspan="5">Keine Positionen bekannt</td></tr>'

    if mit_ust:
        satz = Decimal(str(s.ust_satz))
        netto = (total / (1 + satz)).quantize(Decimal("0.01"))
        ust = total - netto
        prozent = f"{satz * 100:.0f}"
        summen = (f'<tfoot><tr><td colspan="4" class="n">Nettobetrag</td><td class="n">{_eur(netto)}</td></tr>'
                  f'<tr><td colspan="4" class="n">zzgl. {prozent} % USt</td><td class="n">{_eur(ust)}</td></tr>'
                  f'<tr class="tot"><td colspan="4" class="n">Gesamtbetrag (brutto)</td>'
                  f'<td class="n">{_eur(total)}</td></tr></tfoot></table>')
        hinweis = (f'<div class="note">Alle Preise inkl. {prozent} % Umsatzsteuer. '
                   f'Leistungsdatum: {leistung}.</div>')
    else:
        summen = (f'<tfoot><tr class="tot"><td colspan="4" class="n">Gesamtbetrag</td>'
                  f'<td class="n">{_eur(total)}</td></tr></tfoot></table>')
        hinweis = ('<div class="note">Gemäß § 19 UStG (Kleinunternehmerregelung) wird keine '
                   f'Umsatzsteuer berechnet und ausgewiesen. Leistungsdatum: {leistung}.</div>')
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<title>Rechnung {html.escape(number)}</title>
<style>body{{font-family:Arial,sans-serif;color:#111;max-width:760px;margin:24px auto;padding:0 20px}}
h1{{font-size:22px}} .row{{display:flex;justify-content:space-between;gap:20px}}
table{{width:100%;border-collapse:collapse;margin-top:22px}} th,td{{border-bottom:1px solid #ccc;padding:8px;text-align:left}}
td.n,th.n{{text-align:right}} .tot{{font-weight:bold}} .muted{{color:#666;font-size:12px}}
.note{{margin-top:18px;padding:10px 12px;background:#f5f5f5;border-radius:6px;font-size:13px}}</style></head>
<body>
<div class="row"><div><b>{html.escape(s.seller_name)}</b><br>{seller_addr}{tax_line}
{('<div>'+html.escape(s.seller_email)+'</div>') if s.seller_email else ''}</div>
<div style="text-align:right"><h1>Rechnung</h1>
<div>Nr.: <b>{html.escape(number)}</b></div>
<div>Datum: {date.strftime('%d.%m.%Y')}</div>
<div class="muted">eBay-Bestellung: {html.escape(order.external_id or '')}</div></div></div>
<div style="margin-top:26px"><div class="muted">Rechnung an</div>{buyer}</div>
<table><thead><tr><th>Pos.</th><th>Beschreibung</th><th class="n">Menge</th><th class="n">Einzel</th><th class="n">Betrag</th></tr></thead>
<tbody>{zeilen_html}</tbody>
{summen}
{hinweis}
<div class="muted" style="margin-top:24px">Vielen Dank für Ihren Einkauf.</div>
</body></html>"""


def generate_pod_sale_invoice(db: Session, *, order_id: int) -> dict:
    """Erzeugt die Verkaufsrechnung (HTML) fuer eine eigene POD-Bestellung (Studio-Motiv,
    bei eBay verkauft) und legt sie ab - separat von den Wareneinkaeufen in der Belegablage.

    Ab dem Stichtag der Regelbesteuerung mit USt-Ausweis, davor nach § 19.
    Idempotent: existiert bereits eine Verkaufsrechnung zur Bestellung, wird sie
    zurueckgegeben statt neu erzeugt.
    """
    from app.studio.models import PodOrder

    order = db.get(PodOrder, order_id)
    if order is None:
        raise PersistentError("Bestellung nicht gefunden")

    ref = str(order.external_id or order.id)
    existing = db.scalar(
        select(Invoice).where(Invoice.type == "pod_sales", Invoice.reference_id == ref)
    )
    if existing is not None:
        return _invoice_dict(existing) | {"created": False}

    try:
        positionen = json.loads(order.positionen_json or "[]")
    except ValueError:
        positionen = []
    try:
        kaeufer = json.loads(order.kaeufer_json or "{}")
    except ValueError:
        kaeufer = {}

    now = datetime.now(timezone.utc)
    number = _next_sale_invoice_number(db, typen=("ebay_sales", "pod_sales"))
    content = _render_pod_sale_invoice_html(
        number=number, date=now, order=order, positionen=positionen, kaeufer=kaeufer,
        mit_ust=regelbesteuert(order.ordered_at or now)).encode("utf-8")
    stored = get_storage().store(content=content, period=_period(order.ordered_at or now),
                                 file_type="pod_sales", ref_id=ref, ext="html")
    positions_kurz = "; ".join(f"{p.get('menge', 1)}x {p.get('titel', '')}".strip()
                               for p in positionen)[:480] or order.note
    inv = Invoice(
        type="pod_sales", reference_id=ref, invoice_number=number,
        invoice_date=order.ordered_at or now,
        amount=Decimal(str(order.sale_total_eur or 0)), currency="EUR",
        note=positions_kurz, file_path=stored.file_path, file_hash=stored.file_hash,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return _invoice_dict(inv) | {"created": True}


def generate_missing_pod_sale_invoices(db: Session, *, limit: int = 2000) -> dict:
    """Fuer JEDE bezahlte POD-Bestellung, die noch keine Verkaufsrechnung hat, eine erzeugen.

    Wird nach jedem Bestellabgleich aufgerufen (automatisch, bei jedem neuen Verkauf) -
    und heilt nebenbei fehlende Rechnungen aus Zeiten nach, in denen der Automatismus
    noch nicht bestand. Stornierte/schwebende Bestellungen zaehlen nicht als Verkauf.
    """
    from app.studio.models import PodOrder

    vorhandene_refs = {
        r for (r,) in db.execute(
            select(Invoice.reference_id).where(Invoice.type == "pod_sales")
        ).all() if r
    }
    faellig = db.scalars(
        select(PodOrder).where(PodOrder.status.notin_(("cancelled", "pending")))
        .order_by(PodOrder.id).limit(limit)
    ).all()
    erzeugt = uebersprungen = fehler = 0
    for order in faellig:
        ref = str(order.external_id or order.id)
        if ref in vorhandene_refs:
            uebersprungen += 1
            continue
        try:
            r = generate_pod_sale_invoice(db, order_id=order.id)
            erzeugt += 1 if r.get("created") else 0
            uebersprungen += 0 if r.get("created") else 1
        except Exception:  # noqa: BLE001 - eine kaputte Bestellung darf die anderen nicht stoppen
            db.rollback()
            fehler += 1
    return {"erzeugt": erzeugt, "uebersprungen": uebersprungen, "fehler": fehler}


def backfill_invoices(db: Session, *, limit: int = 5000) -> dict:
    """Erzeugt fehlende Belege fuer ALLE Verkaeufe und alle Kaeufe.

    Idempotent – vorhandene Belege werden uebersprungen. Fuer den Erst-Nachtrag der
    importierten Alt-Verkaeufe und als Reparatur, falls die Automatik mal aussetzte.
    """
    from app.models import OrderAliexpress as _Order, Sale as _Sale
    sale_created = sale_skipped = sale_err = 0
    for sid in db.scalars(select(_Sale.id).order_by(_Sale.id)).all()[:limit]:
        try:
            r = generate_sale_invoice(db, sale_id=sid)
            sale_created += 1 if r.get("created") else 0
            sale_skipped += 0 if r.get("created") else 1
        except Exception:  # noqa: BLE001
            db.rollback()
            sale_err += 1
    ord_created = ord_skipped = ord_err = 0
    for oid in db.scalars(select(_Order.id).order_by(_Order.id)).all()[:limit]:
        try:
            r = record_purchase_invoice(db, order_id=oid)
            ord_created += 1 if r.get("created") else 0
            ord_skipped += 0 if r.get("created") else 1
        except Exception:  # noqa: BLE001
            db.rollback()
            ord_err += 1
    return {
        "sales": {"created": sale_created, "skipped": sale_skipped, "errors": sale_err},
        "purchases": {"created": ord_created, "skipped": ord_skipped, "errors": ord_err},
    }


def record_purchase_invoice(db: Session, *, order_id: int) -> dict:
    """Legt den AliExpress-Kaufbeleg zu einer Bestellung ab (idempotent)."""
    order = db.get(OrderAliexpress, order_id)
    if order is None:
        raise PersistentError("Order nicht gefunden")
    # Storniert oder bewusst geloescht: kein Beleg. Ohne diese Sperre kaeme ein von
    # Hand geloeschter Kauf beim naechsten /backfill wieder zurueck.
    if (order.status or "") in VOID_SALE_STATUS:
        return {"order_id": order.id, "created": False, "storniert": True}
    if str(order.aliexpress_order_id or "") in geloeschte_ae_nummern(db):
        return {"order_id": order.id, "created": False, "geloescht": True}
    ref = str(order.aliexpress_order_id or order.id)
    existing = db.scalar(
        select(Invoice).where(Invoice.type == "aliexpress_purchase", Invoice.reference_id == ref)
    )
    if existing is not None:
        return _invoice_dict(existing) | {"created": False}

    now = datetime.now(timezone.utc)
    receipt = (
        f"AliExpress-Kaufbeleg\nBestellnummer: {order.aliexpress_order_id}\n"
        f"Produkt-ID: {order.product_id}\nMenge: {order.quantity}\n"
        f"Einkaufskosten: {order.cost_cny} EUR\nDatum: {now.strftime('%d.%m.%Y')}\n"
    ).encode("utf-8")
    stored = get_storage().store(content=receipt, period=_period(now),
                                 file_type="aliexpress_purchase", ref_id=ref, ext="txt")
    inv = Invoice(
        type="aliexpress_purchase", reference_id=ref,
        invoice_number=f"AE_{order.aliexpress_order_id or order.id}", invoice_date=now,
        amount=order.cost_cny or Decimal("0"), currency="EUR",
        file_path=stored.file_path, file_hash=stored.file_hash,
        sale_id=order.sale_id, order_id=order.id,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return _invoice_dict(inv) | {"created": True}


def attach_original_receipt(db: Session, *, file_bytes: bytes, ext: str,
                            invoice_type: str, order_id: int | None = None,
                            sale_id: int | None = None, amount=None,
                            invoice_number: str | None = None) -> dict:
    """ORIGINAL-Beleg (Download) ablegen und an Order/Sale haengen.

    Fuer AliExpress-Kaufbelege (Bild, inkl. Rabatten/MwSt) und eBay-Rechnungs-PDFs
    („Rechnung fuer Ihre Unterlagen"). Ersetzt den ggf. vorhandenen GENERIERTEN
    Platzhalter-Beleg derselben Referenz – die Originale sind buchhaltungsfuehrend.
    Aktualisiert bei ``amount`` auch den Betrag (echte Zahlen NACH Rabatt).
    """
    from decimal import Decimal as _D
    order = db.get(OrderAliexpress, order_id) if order_id else None
    # Manuell (ausserhalb der App) bestellte Sales haben KEINE Order-Zeile -> beim
    # AliExpress-Kaufbeleg eine anlegen, damit der echte EK (amount) am Verkauf haengt
    # und der Orders-Gewinn stimmt (Vorfall Smart-Brille 11.07.: EK unbekannt).
    if order is None and sale_id and invoice_type == "aliexpress_purchase":
        order = db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == sale_id))
        if order is None:
            sale = db.get(Sale, sale_id)
            if sale is not None:
                order = OrderAliexpress(
                    sale_id=sale_id,
                    product_id=(sale.listing.product_id if sale.listing else None),
                    quantity=sale.quantity or 1, status="ordered",
                    order_date=datetime.now(timezone.utc))
                db.add(order)
                db.flush()
    ref = (str(order.aliexpress_order_id) if order and order.aliexpress_order_id
           else (invoice_number or f"sale{sale_id}"))
    now = datetime.now(timezone.utc)
    stored = get_storage().store(content=file_bytes, period=_period(now),
                                 file_type=f"original_{invoice_type}", ref_id=ref, ext=ext)
    inv = db.scalar(select(Invoice).where(Invoice.type == invoice_type,
                                          Invoice.reference_id == ref))
    if inv is None and sale_id:
        inv = db.scalar(select(Invoice).where(Invoice.type == invoice_type,
                                              Invoice.sale_id == sale_id))
    if inv is None:
        inv = Invoice(type=invoice_type, reference_id=ref,
                      invoice_number=invoice_number or f"ORIG_{ref}",
                      invoice_date=now, currency="EUR",
                      sale_id=sale_id or (order.sale_id if order else None),
                      order_id=order_id)
        db.add(inv)
    inv.file_path = stored.file_path
    inv.file_hash = stored.file_hash
    inv.is_original = True   # echtes Download-Original, kein Platzhalter mehr
    # Ein frueherer Fehlschlag galt der ALTEN Datei. Bleibt der Stempel stehen, haengt
    # der Beleg weiter unter „Problemfaelle", obwohl das Problem behoben ist – genau
    # genau das ist am 18.08. passiert, nachdem der Beleg von Hand nachgereicht
    # hatte.
    if (inv.receipt_data or {}).get("problem"):
        inv.receipt_data = {k: v for k, v in dict(inv.receipt_data).items()
                            if k not in ("problem", "problem_am", "problem_technisch")}
    if amount is not None:
        inv.amount = _D(str(amount))
    # "bank" nicht ueberschreiben: der abgebuchte Betrag ist die staerkste Quelle.
    # Der Beleg nennt bei Fremdwaehrung-Kaeufen einen umgerechneten Euro-Wert; fuer
    # die EUeR zaehlt aber, was tatsaechlich vom Konto abgeflossen ist.
    if order is not None and amount is not None and (order.cost_source or "") != "bank":
        order.cost_cny = _D(str(amount))   # echter EK nach Rabatten
        order.cost_source = "receipt"      # Beleg-Wert: Backfill fasst das nie mehr an
    db.commit()
    db.refresh(inv)
    return _invoice_dict(inv) | {"stored": stored.file_path, "original": True}


def _invoice_dict(inv: Invoice) -> dict:
    return {
        "id": inv.id,
        "type": inv.type,
        "category": inv.category,
        "invoice_number": inv.invoice_number,
        "date": inv.invoice_date.isoformat() if inv.invoice_date else None,
        "amount": float(inv.amount) if inv.amount is not None else None,
        "currency": inv.currency,
        "reference_id": inv.reference_id,
        "note": inv.note,
        "sale_id": inv.sale_id,
        "order_id": inv.order_id,
        "has_file": bool(inv.file_path),
        "is_original": _is_original(inv),
        # aus dem Beleg erzeugte Rechnung vorhanden? (eigener Knopf in der Belegablage)
        "has_rechnung": bool(getattr(inv, "generated_path", None)),
        # Warum die Rechnung NICHT erzeugt werden konnte (rote Zeile + eigene Kachel).
        # Nur relevant, solange keine Rechnung existiert – sonst ist es Vergangenheit.
        "problem": (None if getattr(inv, "generated_path", None)
                    else ((inv.receipt_data or {}).get("problem") or None)),
    }


def list_invoices(db: Session, *, type: str = "all", limit: int = 3000) -> dict:
    """Alle Belege mit Details + Aggregat (fuer die Belege-Seite).

    Angezeigtes Datum = TRANSAKTIONSDATUM (Verkaufs-/Bestelldatum), NICHT das
    Erstell-/Backfill-Datum des Belegs (sonst steht bei allen der Backfill-Tag)."""
    # Alles zusammen: Verkaufsrechnungen (Einnahmen) UND Belege (Ausgaben) in EINER
    # Liste, per Filter/Kachel trennbar - keine eigene Seite mehr dafuer.
    stmt = select(Invoice)
    if type and type != "all":
        stmt = stmt.where(Invoice.type == type)
    stmt = stmt.limit(min(max(limit, 1), 3000))
    invoices = db.scalars(stmt).all()
    sale_ids = {i.sale_id for i in invoices if i.sale_id}
    order_ids = {i.order_id for i in invoices if i.order_id}
    sdate = {s.id: (s.sale_date or s.created_at) for s in
             db.scalars(select(Sale).where(Sale.id.in_(sale_ids))).all()} if sale_ids else {}
    # Bestellungen komplett laden (nicht nur das Datum): fuer die Belegliste brauchen wir
    # ausserdem den Produktnamen, damit bei AliExpress-Kaeufen nicht die nackte Bestell-
    # nummer in der Referenz-Spalte steht (die steht schon in der Beleg-Nr).
    orders = {o.id: o for o in
              db.scalars(select(OrderAliexpress).where(OrderAliexpress.id.in_(order_ids))).all()} \
        if order_ids else {}
    # Fallback-Titel aus dem Produkt (title_raw = AliExpress-Originaltitel), falls die
    # Bestellung selbst keinen mitbekommen hat (z.B. Alt-Bestellungen ohne invoice_data).
    prod_ids = {o.product_id for o in orders.values() if o.product_id}
    ptitle = {p.id: p.title_raw for p in
              db.scalars(select(Product).where(Product.id.in_(prod_ids))).all()} if prod_ids else {}

    def _produktname(order) -> str | None:
        if order is None:
            return None
        titel = ((order.invoice_data or {}).get("title") or "").strip()
        if not titel and order.product_id:
            titel = (ptitle.get(order.product_id) or "").strip()
        return titel or None

    items = []
    for i in invoices:
        d = _invoice_dict(i)
        d["richtung"] = "einnahme" if i.type in ("ebay_sales", "pod_sales") else "ausgabe"
        if i.type == "ebay_sales":
            tx = sdate.get(i.sale_id)
            d["date"] = tx.isoformat() if tx is not None else None
        elif i.type == "aliexpress_purchase":
            # order_date OHNE created_at-Fallback: undatierte Bestellungen sollen nicht mit
            # heutigem Datum nach oben rutschen, sondern (Datum unbekannt) nach unten.
            order = orders.get(i.order_id)
            tx = order.order_date if order is not None else None
            d["date"] = tx.isoformat() if tx is not None else None
            d["product_name"] = _produktname(order)
        items.append(d)
    items.sort(key=lambda x: x.get("date") or "", reverse=True)
    return {"invoices": items, "stats": invoice_summary(db)}


def invoice_summary(db: Session) -> dict:
    """Anzahl + Summe je Belegtyp (Verkauf EUR, Einkauf CNY)."""
    def agg(t):
        rows = db.scalars(select(Invoice).where(Invoice.type == t)).all()
        return {"count": len(rows), "sum": round(sum(float(r.amount or 0) for r in rows), 2)}
    sales = agg("ebay_sales")
    pod_sales = agg("pod_sales")
    purchases = agg("aliexpress_purchase")
    other = agg("betriebsausgabe")
    # Wieviele Sales/Orders haben noch KEINEN Beleg? (Luecken-Anzeige)
    n_sales = db.scalar(select(func.count()).select_from(Sale)) or 0
    n_orders = db.scalar(select(func.count()).select_from(OrderAliexpress)) or 0
    # Wieviele haben ein ECHTES Original (nicht nur Platzhalter)? -> Vollstaendigkeit
    all_inv = db.scalars(select(Invoice)).all()
    sales_orig = sum(1 for i in all_inv if i.type == "ebay_sales" and _is_original(i))
    purch_orig = sum(1 for i in all_inv if i.type == "aliexpress_purchase" and _is_original(i))
    # Belege, aus denen sich KEINE Rechnung erzeugen liess -> eigene Filter-Kachel.
    problems = sum(1 for i in all_inv
                   if not getattr(i, "generated_path", None)
                   and (i.receipt_data or {}).get("problem"))
    return {
        "sales_invoices": sales, "purchase_invoices": purchases,
        "pod_sales_invoices": pod_sales,
        "income_total": round(sales["sum"] + pod_sales["sum"], 2),
        "sales_total": n_sales, "orders_total": n_orders,
        "sales_missing": max(0, int(n_sales) - sales["count"]),
        # „Kaeufe ohne Beleg" = Kaufbeleg-Zeilen ohne echtes Original. Genau das zeigt
        # auch die Liste beim Klick auf die Kachel.
        # FRUEHER: Bestellzeilen minus Belegzeilen -> 301 statt 42. Unsinn, weil
        # OrderAliexpress hunderte Zeilen OHNE AliExpress-Bestellnummer enthaelt
        # (Verkaeufe, die nie bestellt wurden) und mehrere Positionen sich EINE
        # Bestellnummer und damit EINEN Beleg teilen. Kachel und Liste meinten
        # Verschiedenes (Meldung 18.08.: "wenn ich drauf klicke, sind das viel weniger").
        "purchases_missing": max(0, purchases["count"] - purch_orig),
        "sales_originals": sales_orig, "purchase_originals": purch_orig,
        "other_expenses": other,
        "expenses_total": round(purchases["sum"] + other["sum"], 2),
        "problems": problems,
    }


# Wie weit der Beleg-Sammler zurueckschaut. Der AliExpress-Beleg erscheint ~1 Tag
# nach der Bestellung und verschwindet nach einigen Monaten wieder (gemessen 16.08.).
# Bei einem aelteren Kauf OHNE Beleg ist er praktisch immer endgueltig weg – ihn
# trotzdem jedes Mal anzufahren kostet ~4 s pro Bestellung und bringt nichts.
# 14 Tage decken auch eine zweiwoechige Abwesenheit ab.
BELEG_FENSTER_TAGE = 14


def list_originals_todo(db: Session, *, tage: int | None = BELEG_FENSTER_TAGE) -> dict:
    """Kaeufe/Verkaeufe OHNE echten Original-Beleg – Arbeitsliste fuer den Backfill-Client.

    ``tage`` begrenzt auf die juengsten Kaeufe (None = alle, fuer einen einmaligen
    Aufhol-Lauf). eBay: je Order einmal, nicht storniert/erstattet.
    """
    grenze = (datetime.now(timezone.utc) - timedelta(days=tage)) if tage else None
    def _has_orig(i) -> bool:
        return bool(getattr(i, "is_original", False)) or bool(i.file_path and "original_" in i.file_path)

    origs = [i for i in db.scalars(select(Invoice)).all() if _has_orig(i)]
    # AE-Belege haengen an der aliexpress_order_id (reference_id) – mehrere Positionen
    # teilen sich EINE AE-Order -> per Referenz deduplizieren, nicht per order_id.
    ae_done = {str(i.reference_id) for i in origs if i.type == "aliexpress_purchase" and i.reference_id}
    # eBay-Originale haengen oft am Platzhalter (reference_id = ebay_transaction_id, NICHT
    # "EBAY_<oid>") -> "erledigt"-Set ueber sale.ebay_order_id aufloesen, sonst bliebe die
    # Order fuer immer offen und wuerde bei jedem Lauf neu geladen.
    ebay_done_orders: set[str] = set()
    for i in origs:
        if i.type != "ebay_sales":
            continue
        if i.reference_id and str(i.reference_id).startswith("EBAY_"):
            ebay_done_orders.add(str(i.reference_id)[5:])
        if i.sale_id:
            s0 = db.get(Sale, i.sale_id)
            if s0 and s0.ebay_order_id:
                ebay_done_orders.add(str(s0.ebay_order_id))

    ae: list[dict] = []
    seen_ae: set[str] = set()
    for o in db.scalars(select(OrderAliexpress).order_by(OrderAliexpress.id.desc())).all():
        aoid = str(o.aliexpress_order_id or "")
        if not aoid or aoid.startswith("EBAY-") or aoid in seen_ae:
            continue
        # FRUEHER: nur "shipped"/"delivered" – Annahme „Beleg existiert erst ab Versand".
        # Am 16.08.2026 gegen AliExpress gemessen: die Annahme war FALSCH und teuer.
        # Bestellungen im Status "ordered" (14./15.08.) bieten den Beleg bereits an;
        # aeltere abgeschlossene bieten ihn NICHT MEHR (Mai/Juli: kein Beleg-Knopf).
        # Das Fenster ist also FRUEH — der Filter hat genau die Kaeufe zurueckgehalten,
        # bei denen der Beleg noch zu holen war, bis er unwiederbringlich weg war.
        # Deshalb jetzt: alles vorlegen ausser rueckabgewickelt. Ein Kauf ohne Beleg
        # kostet den Sammler ~4 s und wird sauber uebersprungen; ein verpasster Beleg
        # ist dauerhaft verloren.
        if (o.status or "") in VOID_SALE_STATUS:
            continue
        if grenze is not None:
            wann = o.order_date or o.created_at
            if wann is not None:
                if wann.tzinfo is None:          # SQLite liefert ohne Zeitzone
                    wann = wann.replace(tzinfo=timezone.utc)
                if wann < grenze:
                    continue                     # zu alt – Beleg ist dort nicht mehr zu holen
        seen_ae.add(aoid)
        if aoid in ae_done:
            continue
        ae.append({"order_id": o.id, "aliexpress_order_id": aoid})

    seen: set[str] = set()
    ebay: list[dict] = []
    for s in db.scalars(select(Sale).order_by(Sale.sale_date.desc())).all():
        oid = str(s.ebay_order_id or "")
        if not oid or oid in seen:
            continue
        if s.status in ("cancelled", "refunded"):
            continue
        seen.add(oid)
        if oid in ebay_done_orders:
            continue
        ebay.append({"sale_id": s.id, "ebay_order_id": oid})

    return {"aliexpress": ae, "ebay": ebay,
            "counts": {"aliexpress": len(ae), "ebay": len(ebay), "total": len(ae) + len(ebay)}}


def discover_aliexpress_orders(db: Session, orders: list) -> dict:
    """Aus der gescrapten AliExpress-Bestellliste FEHLENDE Bestellungen anlegen
    (nur order_id + Datum + Status 'delivered' -> werden Beleg-Kandidaten). So kommen
    die Alt-Bestellungen (Feb–April, vor dem Tool-Zeitraum) ins System, damit ihre
    echten Belege + Einkaufskosten erfasst werden koennen. Kosten setzt der Beleg
    spaeter (echter EUR-Betrag), hier NUR anlegen."""
    existing = {str(o.aliexpress_order_id) for o in db.scalars(select(OrderAliexpress)).all()
                if o.aliexpress_order_id}
    geloescht = geloeschte_ae_nummern(db)      # bewusst entfernt -> nicht wieder anlegen
    created = 0
    for od in (orders or []):
        aoid = str((od or {}).get("aliexpress_order_id") or "").strip()
        if not aoid or aoid.startswith("EBAY-") or aoid in existing or aoid in geloescht:
            continue
        when = None
        d = (od or {}).get("order_date")
        if d:
            try:
                when = datetime.fromisoformat(str(d))
            except (ValueError, TypeError):
                when = None
        db.add(OrderAliexpress(aliexpress_order_id=aoid, order_date=when, status="delivered"))
        existing.add(aoid)
        created += 1
    db.commit()
    return {"created": created, "total_received": len(orders or [])}


def _is_original(inv) -> bool:
    return bool(getattr(inv, "is_original", False)) or bool(inv.file_path and "original_" in inv.file_path)


def _de_amount(v) -> str:
    try:
        return f"{float(v):.2f}".replace(".", ",")
    except (TypeError, ValueError):
        return ""


def _safe_name(s, maxlen: int = 70) -> str:
    return re.sub(r"[^\w.,\- ]", "_", str(s or "")).strip()[:maxlen]


def tax_export_zip(db: Session, *, year: int) -> bytes:
    """Steuerberater-Paket (ZIP) fuer ein Jahr: alle Original-Belege sauber benannt
    (belege/Einnahmen, belege/Ausgaben) + index.csv (EUeR-Stil) + zusammenfassung.txt.

    Betraege NUR aus tatsaechlich erfassten Werten – Einnahmen = Verkaufspreis,
    Ausgaben = erfasster AliExpress-EK (order.cost_cny) + echte eBay-Gebuehren. Fehlt
    ein Wert, steht dort nichts (keine Schaetzung, [[keine-schaetzungen-zahlen]])."""
    from app.services import finance_service
    rep = finance_service.tax_report(db, year=year)
    storage = get_storage()

    invs = db.scalars(select(Invoice)).all()
    by_sale: dict[int, list] = {}
    by_order: dict[int, list] = {}
    for i in invs:
        if i.type == "ebay_sales" and i.sale_id:
            by_sale.setdefault(i.sale_id, []).append(i)
        elif i.type == "aliexpress_purchase" and i.order_id:
            by_order.setdefault(i.order_id, []).append(i)

    def _best(cands):
        orig = [c for c in cands if _is_original(c)]
        return orig[0] if orig else (cands[0] if cands else None)

    index_rows: list[tuple] = []
    files: list[tuple[str, bytes]] = []
    listings = {l.id: l for l in db.scalars(select(Listing)).all()}

    # ---- Einnahmen (eBay-Verkaeufe) ----
    for sale in db.scalars(select(Sale).order_by(Sale.sale_date, Sale.id)).all():
        when = sale.sale_date or sale.created_at
        if when is None or when.year != year or sale.status in ("cancelled", "refunded"):
            continue
        inv = _best(by_sale.get(sale.id, []))
        listing = listings.get(sale.listing_id) if sale.listing_id else None
        betrag = float(sale.price_eur or 0)
        datum = when.strftime("%Y-%m-%d")
        bez = (((listing.title_seo if listing else "") or "")[:45] + " · " + (sale.buyer_name or "")).strip(" ·")
        belegnr = (inv.invoice_number if inv else "") or ""
        fname = ""
        if inv and inv.file_path:
            ext = inv.file_path.rsplit(".", 1)[-1].lower()
            fname = f"{datum}_{_safe_name(belegnr or sale.ebay_order_id)}_{_de_amount(betrag)}EUR.{ext}"
            try:
                files.append((f"belege/Einnahmen/{fname}", storage.read(inv.file_path)))
            except Exception:  # noqa: BLE001
                fname = "(Datei fehlt)"
        index_rows.append((datum, "Einnahme", bez, _de_amount(betrag), "EUR",
                           belegnr, sale.ebay_order_id or "", fname,
                           "ja" if (inv and _is_original(inv)) else "nein"))

    # ---- Ausgaben (AliExpress-Kaeufe) ----
    seen_ae: set[str] = set()
    for o in db.scalars(select(OrderAliexpress).order_by(OrderAliexpress.order_date)).all():
        when = o.order_date or o.created_at
        aoid = str(o.aliexpress_order_id or "")
        if (when is None or when.year != year or not aoid
                or aoid.startswith("EBAY-") or aoid in seen_ae):
            continue
        seen_ae.add(aoid)
        inv = _best(by_order.get(o.id, []))
        betrag = float(o.cost_cny) if o.cost_cny is not None else None
        datum = when.strftime("%Y-%m-%d")
        belegnr = (inv.invoice_number if inv else "") or f"AE_{aoid}"
        fname = ""
        if inv and inv.file_path:
            ext = inv.file_path.rsplit(".", 1)[-1].lower()
            amt = _de_amount(betrag) if betrag is not None else "NA"
            fname = f"{datum}_{_safe_name(belegnr)}_{amt}EUR.{ext}"
            try:
                files.append((f"belege/Ausgaben/{fname}", storage.read(inv.file_path)))
            except Exception:  # noqa: BLE001
                fname = "(Datei fehlt)"
        index_rows.append((datum, "Ausgabe", f"AliExpress {aoid}",
                           _de_amount(betrag) if betrag is not None else "", "EUR",
                           belegnr, aoid, fname, "ja" if (inv and _is_original(inv)) else "nein"))

    # ---- Sonstige Betriebsausgaben (manuell erfasst: Abos, KI/API, Hosting, Temu, ...) ----
    t_betriebs = 0.0
    for inv in db.scalars(select(Invoice).where(Invoice.type == "betriebsausgabe")
                          .order_by(Invoice.invoice_date)).all():
        when = inv.invoice_date
        if when is None or when.year != year:
            continue
        betrag = float(inv.amount) if inv.amount is not None else None
        if betrag is not None:
            t_betriebs += betrag
        datum = when.strftime("%Y-%m-%d")
        belegnr = inv.invoice_number or ""
        fname = ""
        if inv.file_path:
            ext = inv.file_path.rsplit(".", 1)[-1].lower()
            amt = _de_amount(betrag) if betrag is not None else "NA"
            fname = f"{datum}_{_safe_name(belegnr)}_{amt}EUR.{ext}"
            try:
                files.append((f"belege/Ausgaben/{fname}", storage.read(inv.file_path)))
            except Exception:  # noqa: BLE001
                fname = "(Datei fehlt)"
        # Volltext (note) bevorzugt – bei Bewirtungsbelegen muessen Anlass + Teilnehmer
        # UNGEKUERZT ins Steuerberater-Paket (sonst fehlen Pflichtangaben).
        _detail = (inv.note or inv.reference_id or "").strip()
        bez = f"{inv.category or 'Sonstiges'}: {_detail}".strip(": ")
        index_rows.append((datum, "Ausgabe", bez,
                           _de_amount(betrag) if betrag is not None else "", "EUR",
                           belegnr, inv.category or "", fname,
                           "ja" if _is_original(inv) else "nein"))

    index_rows.sort(key=lambda r: (r[0], r[1]))
    idx = io.StringIO()
    w = csv.writer(idx, delimiter=";")
    w.writerow(["Datum", "Art", "Bezeichnung", "Betrag", "Waehrung",
                "Beleg-Nr", "Referenz", "Dateiname", "Original"])
    for r in index_rows:
        w.writerow(r)

    su = rep["summary"]
    n_inc = sum(1 for r in index_rows if r[1] == "Einnahme")
    n_inc_o = sum(1 for r in index_rows if r[1] == "Einnahme" and r[8] == "ja")
    n_exp = sum(1 for r in index_rows if r[1] == "Ausgabe")
    n_exp_o = sum(1 for r in index_rows if r[1] == "Ausgabe" and r[8] == "ja")
    # eBay-Gebuehren OHNE Bestellbezug (Shop-Abo, Einstellgebuehren) und Versandlabel
    # haengen an keinem Verkauf und fehlten deshalb in der Gewinnrechnung.
    konto_geb = su.get("kontobezogene_gebuehren_eur")
    versandlabel = su.get("versandlabel_eur")
    weitere = su.get("weitere_betriebsausgaben_eur")
    if weitere is None:
        # Nicht raten: Posten als unbekannt ausweisen und den Gewinn als
        # "vor diesen Ausgaben" kennzeichnen, statt sie still auf 0 zu setzen.
        zusatz_zeilen = (
            f"eBay-Gebuehren kontobezogen:  {'unbekannt':>14}       "
            f"({su.get('zusatzkosten_quelle', '')})\n"
            f"eBay-Versandlabel:            {'unbekannt':>14}\n")
        gewinn_label = "Gewinn VOR diesen Posten:"
    else:
        zusatz_zeilen = (
            f"eBay-Gebuehren kontobezogen:    {_de_amount(konto_geb):>12} EUR   "
            f"(Shop-Abo, Einstellgebuehren)\n"
            f"eBay-Versandlabel:              {_de_amount(versandlabel):>12} EUR\n")
        gewinn_label = "Gewinn (Ueberschuss):"
    gewinn = (su["einnahmen_eur"] - su["ebay_gebuehren_eur"]
              - su["einkauf_gesamt_eur"] - t_betriebs - (weitere or 0.0))
    summ = (
        f"STEUER-EXPORT {year} — {_besteuerung_im_jahr(year)} ({get_settings().seller_name})\n"
        f"Nur tatsaechlich erfasste Werte, nichts geschaetzt.\n"
        f"{'='*52}\n\n"
        f"Einnahmen (eBay-Verkaeufe):     {_de_amount(su['einnahmen_eur']):>12} EUR   ({su['verkaeufe']} Verkaeufe)\n"
        f"Ausgaben eBay-Gebuehren:        {_de_amount(su['ebay_gebuehren_eur']):>12} EUR   (davon {su['davon_echte_gebuehren']} echt)\n"
        f"{zusatz_zeilen}"
        f"Ausgaben AliExpress-Einkauf:    {_de_amount(su['einkauf_gesamt_eur']):>12} EUR\n"
        f"Sonstige Betriebsausgaben:      {_de_amount(t_betriebs):>12} EUR\n"
        f"{'-'*52}\n"
        f"{gewinn_label:<31} {_de_amount(gewinn):>12} EUR\n\n"
        + (f"Nicht enthalten: {su['stornos_ausgeschlossen']} stornierte/erstattete "
           f"Verkaeufe ueber {_de_amount(su['stornos_eur'])} EUR — das Geld wurde\n"
           f"zurueckgezahlt, es ist keine Einnahme.\n\n"
           if su.get("stornos_ausgeschlossen") else "")
        + f"Belege-Vollstaendigkeit (Original-Dokumente vorhanden):\n"
        f"  Einnahmen-Belege: {n_inc_o}/{n_inc}\n"
        f"  Ausgaben-Belege:  {n_exp_o}/{n_exp}\n\n"
        f"Enthalten: index.csv (alle Buchungen) + belege/Einnahmen + belege/Ausgaben.\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        base = f"steuer-export-{year}"
        z.writestr(f"{base}/index.csv", "﻿" + idx.getvalue())
        z.writestr(f"{base}/zusammenfassung.txt", summ)
        for arc, data in files:
            z.writestr(f"{base}/{arc}", data)
    return buf.getvalue()


def add_manual_expense(db: Session, *, when: datetime, amount, category: str,
                       description: str = "", file_bytes: bytes | None = None,
                       ext: str | None = None) -> dict:
    """Manuelle Betriebsausgabe erfassen (AutoDS-Abo, Claude/API, Hosting, Temu, Domain, ...).

    Optional mit Beleg-Datei (Rechnung/Screenshot). Fliesst in EUeR/Steuer-Export als
    Ausgabe ein. Nur echte, vom Nutzer eingegebene Werte."""
    ref = (description or category or "Betriebsausgabe")[:100]
    stored = None
    if file_bytes:
        stored = get_storage().store(content=file_bytes, period=_period(when),
                                     file_type="betriebsausgabe",
                                     ref_id=(_safe_name(category or ref, 30) or "ausgabe"),
                                     ext=(ext or "pdf"))
    inv = Invoice(type="betriebsausgabe", category=category, reference_id=ref,
                  note=(description or None),   # voller Text (ungekuerzt): Bewirtungs-Pflichtangaben etc.
                  invoice_date=when, amount=Decimal(str(amount)), currency="EUR",
                  file_path=(stored.file_path if stored else None),
                  file_hash=(stored.file_hash if stored else None),
                  is_original=bool(stored))
    db.add(inv)
    db.flush()
    inv.invoice_number = f"AUSG-{inv.id}"
    db.commit()
    db.refresh(inv)
    return _invoice_dict(inv) | {"created": True}


# Kategorie-Name wie im Dashboard-Auswahlfeld – daran haengt die Sperrliste unten.
BEWIRTUNG_KATEGORIE = "Bewirtungsbeleg (§ 4 Abs. 5 EStG)"


def bewirtung_pflichtangaben(*, anlass: str, gesamt, art: str = "", ort: str = "",
                             gastgeber: str = "", gaeste: str = "", betrag=None,
                             trinkgeld=None, notiz: str = "") -> str:
    """Die Pflichtangaben (§ 4 Abs. 5 EStG) als EIN Text fuer `Invoice.note`.

    FORMAT NICHT AENDERN, ohne beides mitzuziehen: `recent_bewirtung_anlaesse`
    (unten) schneidet den Anlass an " · " und "Anlass: " heraus, und der
    Steuerberater-Export uebernimmt den Text ungekuerzt. Das Dashboard baut
    denselben Text fuer den Weg "Quittung ist schon beschriftet".
    """
    teile: list[str] = []
    if art:
        teile.append(f"Art: {art}")
    if ort:
        teile.append(f"Ort: {ort}")
    if gastgeber:
        teile.append(f"Gastgeber: {gastgeber}")
    if gaeste:
        teile.append(f"Gäste: {gaeste}")
    teile.append(f"Anlass: {anlass}")
    if betrag is not None:
        teile.append(f"Rechnungsbetrag: {_de_amount(betrag)} €")
    if trinkgeld is not None:
        teile.append(f"Trinkgeld: {_de_amount(trinkgeld)} €")
    teile.append(f"Gesamt: {_de_amount(gesamt)} €")
    if notiz:
        teile.append(f"Notiz: {notiz}")
    return "Bewirtungsbeleg · " + " · ".join(teile)


def add_bewirtung_eigenbeleg(db: Session, *, when: datetime, ort: str, gastgeber: str,
                             gaeste: str, anlass: str, betrag: float,
                             trinkgeld: float = 0.0, art: str = "",
                             quittung: bytes, notiz: str = "") -> dict:
    """Eigenbeleg Gastronomie erzeugen und als Betriebsausgabe ablegen.

    Fuer den Fall, dass auf der Restaurant-Quittung kein Platz fuer die
    Pflichtangaben war: das Programm baut das Blatt, klebt das Quittungsfoto
    hinein und legt das fertige PDF als Beleg ab. Abgelegt wird der GESAMTbetrag
    (Rechnung + Trinkgeld) – gerechnet, nicht geschaetzt.
    """
    from app.services.eigenbeleg_pdf import build_eigenbeleg

    gesamt = float(betrag or 0) + float(trinkgeld or 0)
    # PDF zuerst bauen (reine Rechenarbeit), erst danach schreiben – die DB-Sperre
    # soll nicht ueber den Bildaufbau gehalten werden (Projektregel 12).
    pdf = build_eigenbeleg(datum=when.strftime("%d.%m.%Y"), ort=ort, gastgeber=gastgeber,
                           gaeste=gaeste, anlass=anlass, betrag=float(betrag or 0),
                           trinkgeld=float(trinkgeld or 0), art=art, quittung=quittung)
    beschreibung = bewirtung_pflichtangaben(
        anlass=anlass, gesamt=gesamt, art=art, ort=ort, gastgeber=gastgeber,
        gaeste=gaeste, betrag=betrag, trinkgeld=trinkgeld, notiz=notiz)
    return add_manual_expense(db, when=when, amount=gesamt,
                              category=BEWIRTUNG_KATEGORIE, description=beschreibung,
                              file_bytes=pdf, ext="pdf") | {"eigenbeleg": True}


def replace_expense_file(db: Session, *, invoice_id: int, file_bytes: bytes,
                         ext: str | None = None) -> dict:
    """Die Datei eines selbst erfassten Belegs austauschen, Eintrag bleibt bestehen.

    Gebaut fuer die Reparatur des Ueberschreib-Bugs (siehe storage.store): dort haben
    sich Belege derselben Kategorie eines Monats eine Datei geteilt, der spaetere Upload
    hat das fruehere Foto ersetzt. Damit der Nutzer Datum/Betrag/Anlass nicht neu
    eintippen muss, wird hier nur die Datei ersetzt.

    Die ALTE Datei wird nur entfernt, wenn kein anderer Beleg mehr auf sie zeigt – im
    Altbestand kann sie noch einem zweiten Eintrag gehoeren.
    """
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise PersistentError("Beleg nicht gefunden")
    if inv.type != "betriebsausgabe":
        raise PersistentError(
            "Die Datei laesst sich nur bei selbst erfassten Ausgaben austauschen.")
    if not file_bytes:
        raise PersistentError("Die hochgeladene Datei ist leer")

    alt = inv.file_path
    stored = get_storage().store(
        content=file_bytes, period=_period(inv.invoice_date),
        file_type="betriebsausgabe",
        ref_id=(_safe_name(inv.category or inv.reference_id or "ausgabe", 30) or "ausgabe"),
        ext=(ext or "pdf"))
    inv.file_path = stored.file_path
    inv.file_hash = stored.file_hash
    inv.is_original = True
    db.commit()
    db.refresh(inv)

    alt_entfernt = False
    if alt and alt != stored.file_path:
        noch_benutzt = db.scalar(select(func.count()).select_from(Invoice)
                                 .where(Invoice.file_path == alt)) or 0
        if not noch_benutzt:
            try:
                alt_entfernt = get_storage().delete(alt)
            except Exception:  # noqa: BLE001 – neue Datei haengt schon dran, das zaehlt
                alt_entfernt = False
    return _invoice_dict(inv) | {"replaced": True, "old_file_deleted": alt_entfernt}


def fehlende_belegzeilen(db: Session, *, jahr: int | None = None,
                         anwenden: bool = False) -> dict:
    """Bestellungen OHNE Eintrag in der Belegablage finden (und auf Wunsch anlegen).

    Warum es die gibt: die Belegablage kam spaeter als die ersten Bestellungen. Fuer
    diese Kaeufe existiert schlicht keine Zeile — sie tauchen in der Liste nicht auf,
    zaehlen aber in der Kachel „Kaeufe ohne Beleg". Kachel und Liste meinten deshalb
    Verschiedenes (Meldung 18.08.: „301, beim Klick sind es viel weniger").

    Ohne ``anwenden`` wird nur gezaehlt — nach Jahr, damit man sieht, was man sich
    einhandelt. ``jahr`` grenzt auf einen Jahrgang ein.

    Stornierte und bewusst geloeschte Kaeufe bleiben aussen vor: fuer sie soll es
    gerade KEINE Zeile geben.
    """
    geloescht = geloeschte_ae_nummern(db)
    mit_zeile = {str(i.reference_id) for i in
                 db.scalars(select(Invoice)
                            .where(Invoice.type == "aliexpress_purchase")).all()
                 if i.reference_id}

    nach_jahr: dict[str, int] = {}
    kandidaten: list[OrderAliexpress] = []
    for o in db.scalars(select(OrderAliexpress)).all():
        aoid = str(o.aliexpress_order_id or "")
        if not aoid or aoid.startswith("EBAY-"):
            continue                      # Platzhalter, kein echter AliExpress-Kauf
        if aoid in mit_zeile or aoid in geloescht:
            continue
        if (o.status or "") in VOID_SALE_STATUS:
            continue
        wann = o.order_date or o.created_at
        j = str(wann.year) if wann is not None else "ohne Datum"
        nach_jahr[j] = nach_jahr.get(j, 0) + 1
        if jahr is None or (wann is not None and wann.year == jahr):
            kandidaten.append(o)

    ergebnis = {"ohne_zeile_gesamt": sum(nach_jahr.values()),
                "nach_jahr": dict(sorted(nach_jahr.items())),
                "betroffen": len(kandidaten), "jahr": jahr, "angelegt": 0}
    if not anwenden:
        return ergebnis

    for o in kandidaten:
        try:
            r = record_purchase_invoice(db, order_id=o.id)
            if r.get("created"):
                ergebnis["angelegt"] += 1
        except PersistentError:            # einzelner Ausreisser stoppt den Lauf nicht
            db.rollback()
    return ergebnis


def geloeschte_ae_nummern(db: Session) -> set[str]:
    """AliExpress-Bestellnummern, die bewusst geloescht wurden.

    Ohne diese Sperre waere jedes Loeschen wirkungslos: ``record_purchase_invoice``
    und ``discover_aliexpress_orders`` legen fehlende Bestellungen/Belege von selbst
    wieder an. Quelle ist das Aktivitaets-Log – es ist ohnehin die GoBD-Spur.
    """
    from app.models import TaskLog
    raus: set[str] = set()
    for t in db.scalars(select(TaskLog).where(TaskLog.task_type == "beleg_geloescht")).all():
        nr = str((t.result_data or {}).get("aliexpress_order_id") or "")
        if nr:
            raus.add(nr)
    return raus


def delete_purchase_invoice(db: Session, *, invoice_id: int) -> dict:
    """Stornierten Wareneinkauf entfernen: Zeile, Beleg-Datei UND erzeugte Rechnung.

    Fall aus der Praxis: der Kunde storniert, wir stornieren daraufhin den
    AliExpress-Kauf. Dann hat es diesen Einkauf nie gegeben – er darf weder in der
    Belegablage stehen noch als Ausgabe zaehlen.

    KOMPLETT, nicht nur ausgeblendet (Nutzer-Vorgabe 18.08.): Beleg-Zeile, Beleg-Datei,
    erzeugte Rechnung UND die Bestellung selbst. Eine zugeordnete Kontobuchung wird
    vorher geloest und wieder als offener Posten gefuehrt – sonst zeigt sie ins Leere
    und gilt trotzdem als erledigt.

    Damit es HAELT, reicht Loeschen allein nicht: sowohl ``record_purchase_invoice``
    als auch ``discover_aliexpress_orders`` (gescrapte AliExpress-Bestellliste) legen
    Fehlendes automatisch wieder an. Beide fragen deshalb ``geloeschte_ae_nummern()``.

    Die Sicherheitsabfrage sitzt im Dashboard; was entfernt wurde, bleibt als
    TaskLog ``beleg_geloescht`` nachvollziehbar (GoBD).
    """
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise PersistentError("Beleg nicht gefunden")
    if inv.type != "aliexpress_purchase":
        raise PersistentError("Dieser Weg ist nur fuer Wareneinkaeufe.")

    order = db.get(OrderAliexpress, inv.order_id) if inv.order_id else None
    ae_nr = str((order.aliexpress_order_id if order else None) or inv.reference_id or "")
    info = {"id": inv.id, "invoice_number": inv.invoice_number,
            "reference_id": inv.reference_id,
            "aliexpress_order_id": ae_nr,          # Sperre gegen Wiederanlegen
            "amount": float(inv.amount) if inv.amount is not None else None,
            "order_id": inv.order_id,
            "hatte_beleg": bool(inv.is_original),
            "hatte_rechnung": bool(inv.generated_path)}
    pfade = [p for p in (inv.file_path, inv.generated_path) if p]

    with task_log(db, task_type="beleg_geloescht", reference_id=str(inv.id)) as tl:
        # Kontobuchung loesen, bevor die Bestellung faellt: sie behaelt sonst einen
        # Verweis ins Leere und gilt weiter als "zugeordnet", obwohl es nichts mehr gibt.
        if order is not None:
            for tx in db.scalars(select(BankTransaction)
                                 .where(BankTransaction.order_id == order.id)).all():
                tx.order_id = None
                tx.invoices = [i for i in (tx.invoices or []) if i != inv.id]
                if tx.status == "matched" and not tx.invoices:
                    tx.status = "pending"      # wieder offener Posten, nicht still "erledigt"
        db.delete(inv)
        if order is not None:
            db.delete(order)                   # komplett weg – nicht nur storniert
            info["bestellung_geloescht"] = True
        tl.result_data = info
        db.commit()

    # Dateien erst NACH dem Commit – und nur, wenn kein anderer Beleg sie noch nutzt
    # (Altbestand teilte sich Dateien, siehe delete_manual_expense).
    storage = get_storage()
    for pfad in pfade:
        noch_benutzt = db.scalar(
            select(func.count()).select_from(Invoice)
            .where(or_(Invoice.file_path == pfad, Invoice.generated_path == pfad)))
        if noch_benutzt:
            continue
        try:
            storage.delete(pfad)
        except Exception:  # noqa: BLE001 – Datei weg/nicht loeschbar: Zeile ist das Wesentliche
            pass
    return {**info, "geloescht": True}


def delete_manual_expense(db: Session, *, invoice_id: int) -> dict:
    """Einen SELBST ERFASSTEN Beleg endgueltig loeschen – Datenbankzeile UND Datei.

    Bewusst nur `betriebsausgabe`: AliExpress-Kaufbelege und Verkaufsrechnungen leiten
    sich aus Bestellungen bzw. Verkaeufen ab. Ein Loeschen wuerde die Einkaufs-/
    Umsatzzahlen verfaelschen und beim naechsten Bestell-Import oder `/backfill`
    ohnehin rueckgaengig gemacht (`record_purchase_invoice` legt fehlende Belege neu
    an, erkannt an der AliExpress-Bestellnummer). Nutzer-Entscheidung 27.07.:
    Papierkorb nur dort, wo Loeschen auch haelt.

    Endgueltig, nicht storniert – ebenfalls ausdrueckliche Nutzer-Entscheidung. Die
    Sicherheitsabfrage sitzt im Dashboard.
    """
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise PersistentError("Beleg nicht gefunden")
    if inv.type != "betriebsausgabe":
        raise PersistentError(
            "Nur selbst erfasste Ausgaben lassen sich loeschen. Dieser Beleg gehoert zu "
            "einer Bestellung bzw. einem Verkauf und wuerde beim naechsten Abgleich "
            "wieder angelegt.")
    pfad = inv.file_path
    info = {"id": inv.id, "invoice_number": inv.invoice_number,
            "amount": float(inv.amount) if inv.amount is not None else None,
            "category": inv.category}
    db.delete(inv)
    db.commit()

    # Datei erst NACH dem Commit anfassen. Und nur, wenn sie kein anderer Beleg mehr
    # benutzt: im Altbestand (vor dem Hash im Dateinamen, siehe storage.store) konnten
    # sich zwei Ausgaben derselben Kategorie eines Monats eine Datei teilen.
    datei_weg = False
    if pfad:
        noch_benutzt = db.scalar(select(func.count()).select_from(Invoice)
                                 .where(Invoice.file_path == pfad)) or 0
        if not noch_benutzt:
            try:
                datei_weg = get_storage().delete(pfad)
            except Exception:  # noqa: BLE001 – Zeile ist weg, das zaehlt; Datei ist Kosmetik
                datei_weg = False
    return info | {"deleted": True, "file_deleted": datei_weg}


def recent_bewirtung_anlaesse(db: Session, *, limit: int = 15) -> list[str]:
    """Die zuletzt erfassten Bewirtungs-Anlaesse (neueste zuerst, ohne Dubletten).

    Dient als Sperrliste fuer die Anlass-Vorschlaege: derselbe Satz soll nicht mehrfach
    im Beleg-Ordner stehen. Gelesen wird aus `note` – dort liegt der Pflichtangaben-Text
    im Format "Bewirtungsbeleg · … · Anlass: X · …" (siehe Dashboard).
    """
    stmt = (select(Invoice.note)
            .where(Invoice.type == "betriebsausgabe", Invoice.category.like("Bewirtung%"),
                   Invoice.note.is_not(None))
            .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
            .limit(limit * 2))
    out: list[str] = []
    seen: set[str] = set()
    for (note,) in db.execute(stmt).all():
        for teil in str(note or "").split(" · "):
            if not teil.startswith("Anlass: "):
                continue
            anlass = teil[len("Anlass: "):].strip()
            key = anlass.lower()
            if anlass and key not in seen:
                seen.add(key)
                out.append(anlass)
            break
    return out[:limit]


def read_invoice_file(db: Session, *, invoice_id: int) -> tuple[bytes, str, str]:
    """Rohdaten + Dateiname + Content-Type eines Belegs (fuer Download)."""
    inv = db.get(Invoice, invoice_id)
    if inv is None or not inv.file_path:
        raise PersistentError("Beleg/Datei nicht gefunden")
    # Alt-Eintraege haben Windows-Pfade ('data\invoices\...') und waren auf dem Linux-VPS
    # nicht mehr abrufbar (Download lief in 500). Derselbe robuste Leser wie beim
    # Rechnungs-Erzeugen holt sie wieder hoch.
    from app.services.purchase_invoice import _lies_belegdatei

    try:
        data = _lies_belegdatei(get_storage(), inv.file_path)
    except Exception as exc:  # noqa: BLE001 – als sauberes 404 melden, nicht als 500
        raise PersistentError(f"Belegdatei nicht auffindbar: {exc}")
    ext = (inv.file_path.rsplit(".", 1)[-1] or "bin").lower()
    ctype = {"html": "text/html; charset=utf-8", "txt": "text/plain; charset=utf-8",
             "pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg",
             "jpeg": "image/jpeg", "webp": "image/webp"}.get(ext, "application/octet-stream")
    name = f"{inv.invoice_number or ('beleg_' + str(inv.id))}.{ext}"
    return data, name, ctype


def search_invoices(db: Session, *, date_from=None, date_to=None,
                    amount_min=None, amount_max=None, type: str = "all") -> list[dict]:
    """POST .../invoices/search – Belege nach Datum/Betrag/Referenz/Typ filtern."""
    stmt = select(Invoice)
    if date_from is not None:
        stmt = stmt.where(Invoice.invoice_date >= datetime.combine(date_from, datetime.min.time()))
    if date_to is not None:
        stmt = stmt.where(Invoice.invoice_date <= datetime.combine(date_to, datetime.max.time()))
    if amount_min is not None:
        stmt = stmt.where(Invoice.amount >= amount_min)
    if amount_max is not None:
        stmt = stmt.where(Invoice.amount <= amount_max)
    if type and type != "all":
        stmt = stmt.where(Invoice.type == type)

    rows = db.scalars(stmt.order_by(Invoice.invoice_date.desc())).all()
    return [
        {
            "id": inv.id,
            "type": inv.type,
            "invoice_number": inv.invoice_number,
            "date": inv.invoice_date.date() if inv.invoice_date else None,
            "file_path": inv.file_path,
        }
        for inv in rows
    ]


def _besteuerung_im_jahr(jahr: int) -> str:
    """Kopfzeile des Steuerexports: welche Besteuerung galt in diesem Jahr?"""
    from datetime import timedelta

    ab = _ust_ab()
    prozent = f"{get_settings().ust_satz * 100:.0f}"
    if ab is None or jahr < ab.year:
        return "§19 Kleinunternehmer"
    if jahr > ab.year or (ab.month, ab.day) == (1, 1):
        return f"Regelbesteuerung, {prozent} % USt"
    bis = ab - timedelta(days=1)
    return f"§19 Kleinunternehmer bis {bis:%d.%m.}, ab {ab:%d.%m.} Regelbesteuerung ({prozent} % USt)"
