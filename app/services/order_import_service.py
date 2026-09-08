"""Import der AliExpress-/AutoDS-Kaufhistorie aus CSV/Excel-Export.

Die AliExpress-Open-Platform-API liefert KEINE manuell/ueber AutoDS getaetigten
Kaeufe (nur API-eigene Bestellungen). Deshalb Import per Datei-Export:
AutoDS (empfohlen – enthaelt eBay-Order + Einkaufspreis + Tracking) oder AliExpress
"My Orders". Spalten werden per Fuzzy-Matching automatisch erkannt.
"""
from __future__ import annotations

import csv
import io
import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderAliexpress, Sale
from app.retry import PersistentError

# Feld -> moegliche Spaltenueberschriften (normalisiert, lowercase)
_SYNONYMS = {
    "ali_order_id": ["aliexpress order id", "aliexpress order", "ae order id", "ae order",
                     "supplier order id", "supplier order", "order id", "order number",
                     "order no", "bestellnummer", "aliexpress order number", "source order id"],
    "ebay_order_id": ["ebay order id", "ebay order", "ebay order number", "sales record number",
                      "ebay sales record", "marketplace order id", "order id ebay"],
    "date": ["order date", "purchase date", "date ordered", "bestelldatum", "datum", "date",
             "gmt create", "created", "order time", "buy date"],
    "product": ["product title", "product name", "item title", "product", "title", "item",
                "produkt", "artikel", "name"],
    "cost": ["product cost", "order cost", "total cost", "supplier cost", "cost of goods",
             "buy price", "einkaufspreis", "einkauf", "cogs", "aliexpress price",
             "supplier price", "item cost", "cost", "paid", "amount paid"],
    "sale_price": ["sale price", "sold price", "sell price", "ebay price", "verkaufspreis",
                   "sold for", "item sale price"],
    "quantity": ["quantity", "qty", "menge", "count", "units", "item quantity"],
    "tracking": ["tracking number", "tracking no", "tracking code", "sendungsnummer",
                 "logistics tracking", "tracking"],
}


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (h or "").strip().lower())
    # (mehrfach-Spaces bleiben – Vergleich erfolgt via 'in')


def _map_headers(headers: list[str]) -> dict:
    """field -> tatsaechliche Spaltenueberschrift (beste Uebereinstimmung)."""
    norm = {h: _norm(h) for h in headers}
    mapping: dict[str, str] = {}
    for field, syns in _SYNONYMS.items():
        best = None
        for syn in syns:  # geordnet: spezifisch -> generisch
            for h, nh in norm.items():
                if h in mapping.values():
                    continue
                if nh == syn or (len(syn) >= 4 and syn in nh):
                    best = h
                    break
            if best:
                break
        if best:
            mapping[field] = best
    return mapping


def _num(value) -> Decimal | None:
    """Tolerante Zahl-Erkennung: '€12,50' / '1.234,56' / '1,234.56' / '12.50'."""
    if value is None:
        return None
    s = re.sub(r"[^\d,.\-]", "", str(value))
    if not s or s in {"-", ".", ","}:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) else s.replace(",", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _detect_delimiter(sample: str) -> str:
    counts = {d: sample.count(d) for d in (";", "\t", ",")}
    return max(counts, key=counts.get) if any(counts.values()) else ","


_DE_MONTHS = {"jan": 1, "feb": 2, "mär": 3, "mar": 3, "apr": 4, "mai": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dez": 12}


def _parse_de_date(s: str):
    """'30. Jun 2026' -> datetime (UTC) oder None."""
    from datetime import datetime, timezone
    m = re.match(r"(\d{1,2})\.\s*([A-Za-zäöü]{3})\w*\s+(\d{4})", (s or "").strip())
    if not m:
        return None
    mon = _DE_MONTHS.get(m.group(2).lower())
    if not mon:
        return None
    try:
        return datetime(int(m.group(3)), mon, int(m.group(1)), tzinfo=timezone.utc)
    except ValueError:
        return None


def import_browser_orders(db: Session, rows: list[dict]) -> dict:
    """Bestellungen aus der Browser-Extraktion (aliexpress.com/p/order) importieren.

    Row-Felder (kompakt): r=Bestellnr, d=Datum, s=Status, st=Store, ti=Titel,
    v=Variante, u=Einzelpreis, q=Menge, g=Gesamt EUR. Idempotent über Bestellnr.
    """
    created = updated = invoiced = skipped = 0
    for row in rows:
        ref = str(row.get("r") or "").strip()
        if not ref:
            skipped += 1
            continue
        order = db.scalar(select(OrderAliexpress).where(
            OrderAliexpress.aliexpress_order_id == ref))
        is_new = order is None
        if is_new:
            order = OrderAliexpress(aliexpress_order_id=ref, status="imported")
            db.add(order)
        total = _num(row.get("g")) or _num(row.get("u"))
        if total is not None:
            order.cost_cny = total
        qty = _num(row.get("q"))
        if qty is not None:
            order.quantity = int(qty)
        when = _parse_de_date(row.get("d") or "")
        if when is not None:
            order.order_date = when
        status = (row.get("s") or "").lower()
        if is_new:
            if "abgeschlossen" in status or "geliefert" in status:
                order.status = "delivered"
            elif "ausstehend" in status or "versandt" in status:
                order.status = "shipped"
        order.invoice_data = {"title": row.get("ti"), "store": row.get("st"),
                              "variant": row.get("v"), "unit_eur": row.get("u"),
                              "status_raw": row.get("s"), "source": "browser"}
        db.flush()
        created += 1 if is_new else 0
        updated += 0 if is_new else 1
        try:
            from app.services import invoice_service
            r = invoice_service.record_purchase_invoice(db, order_id=order.id)
            invoiced += 1 if r.get("created") else 0
        except Exception:  # noqa: BLE001
            pass
    db.commit()
    return {"created": created, "updated": updated, "invoices_created": invoiced,
            "skipped": skipped}


def import_orders_csv(db: Session, *, content: bytes, link_sales: bool = True) -> dict:
    """CSV-Kaufhistorie importieren -> OrderAliexpress-Records + Kaufbelege.

    Idempotent ueber die AliExpress-Order-Nr. Verknuepft – wenn moeglich – mit der
    passenden eBay-Sale (ueber ebay_order_id), sonst sale_id=None.
    """
    text = content.decode("utf-8-sig", errors="replace")
    if not text.strip():
        raise PersistentError("Datei ist leer.")
    delim = _detect_delimiter(text[:8192])
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    headers = reader.fieldnames or []
    mapping = _map_headers(headers)
    if not any(k in mapping for k in ("ali_order_id", "ebay_order_id")) or "cost" not in mapping:
        raise PersistentError(
            "Spalten nicht erkannt. Nötig sind mindestens eine Bestellnummer-Spalte "
            f"(AliExpress oder eBay) und eine Kosten-Spalte. Gefundene Spalten: {headers}")

    # eBay-Order -> Sale-Lookup (fuer Verknuepfung)
    sale_by_ebay: dict[str, Sale] = {}
    if link_sales:
        for s in db.scalars(select(Sale)).all():
            if s.ebay_order_id:
                sale_by_ebay.setdefault(str(s.ebay_order_id), s)

    created = updated = linked = invoiced = skipped = 0
    for i, row in enumerate(reader):
        aoid = (row.get(mapping.get("ali_order_id", ""), "") or "").strip()
        eoid = (row.get(mapping.get("ebay_order_id", ""), "") or "").strip()
        cost = _num(row.get(mapping.get("cost", "")))
        if not aoid and not eoid:
            skipped += 1
            continue
        ref = aoid or f"EBAY-{eoid}-{i}"  # ohne AE-Nr. synthetische, stabile Referenz

        order = db.scalar(select(OrderAliexpress).where(OrderAliexpress.aliexpress_order_id == ref))
        is_new = order is None
        if is_new:
            order = OrderAliexpress(aliexpress_order_id=ref, status="imported")
            db.add(order)

        if cost is not None:
            order.cost_cny = cost
        qty = _num(row.get(mapping.get("quantity", "")))
        if qty is not None:
            order.quantity = int(qty)
        tracking = (row.get(mapping.get("tracking", ""), "") or "").strip()
        if tracking:
            order.tracking_number = tracking
            if order.status == "imported":
                order.status = "shipped"

        # Verknuepfung mit eBay-Sale
        if link_sales and eoid and order.sale_id is None:
            sale = sale_by_ebay.get(eoid)
            if sale is not None:
                order.sale_id = sale.id
                linked += 1

        db.flush()
        created += 1 if is_new else 0
        updated += 0 if is_new else 1

        # Kaufbeleg ablegen (best effort)
        try:
            from app.services import invoice_service
            r = invoice_service.record_purchase_invoice(db, order_id=order.id)
            invoiced += 1 if r.get("created") else 0
        except Exception:  # noqa: BLE001
            pass

    db.commit()
    return {
        "columns_detected": mapping,
        "created": created,
        "updated": updated,
        "linked_to_sale": linked,
        "invoices_created": invoiced,
        "skipped": skipped,
    }
