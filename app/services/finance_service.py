"""Steuer/Finanzen: echte eBay-Gebuehren (Finances API) + Jahres-Steuer-Report.

Ziel (Steuererklaerung): pro Bestellung/Produkt die tatsaechliche Einnahme,
die ECHTE eBay-Gebuehr (statt Kalkulation) und der AliExpress-EK ->
Bruttomarge. Export als CSV fuer den Steuerberater.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Listing, OrderAliexpress, Sale
from app.services import pricing

logger = logging.getLogger("app.services.finance")


def _real_ebay():
    from app.integrations.ebay import RealEbayClient
    return RealEbayClient(get_settings())


def _dec(v) -> Decimal | None:
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _utc(wann):
    """Naive Datumswerte als UTC lesen.

    SQLite speichert trotz ``DateTime(timezone=True)`` OHNE Zeitzone – der
    Vergleich mit einem bewussten UTC-Datum wirft sonst TypeError.
    """
    if wann is not None and wann.tzinfo is None:
        return wann.replace(tzinfo=timezone.utc)
    return wann


def _order_ref(t: dict) -> str:
    """Order-Nr.: direktes Feld ODER references[ORDER_ID] (so kommen AD_FEEs).

    MODULWEIT, damit Gebuehren-Sync und Diagnose denselben Bestellbezug sehen.
    Nur ``orderId`` zu pruefen zaehlt die order-bezogenen Anzeigengebuehren
    faelschlich als kontobezogen — bei 943 AD_FEEs verschiebt das die Zahlen um
    Tausende Euro.
    """
    if t.get("orderId"):
        return str(t["orderId"])
    for r in t.get("references") or []:
        if (r.get("referenceType") or "").upper() == "ORDER_ID" and r.get("referenceId"):
            return str(r["referenceId"])
    return ""


async def sync_ebay_fees(db: Session, *, days: int = 90, date_from=None, date_to=None,
                         max_pages: int = 10, nur_ab=None) -> dict:
    """Echte Gebuehren je Order aus der Finances API in Sale.fee_eur_actual schreiben.

    SALE-Transaktionen tragen totalFeeAmount je Order; bei mehreren Sales pro Order
    wird die Gebuehr proportional zum Verkaufspreis verteilt.

    ``date_from``/``date_to`` erlauben ein historisches Fenster statt der letzten
    ``days`` Tage (fuer das einmalige Nachziehen alter Jahrgaenge).

    ``nur_ab`` schuetzt vor Teil-Ueberschreibung: die Gebuehr einer Bestellung faellt
    verteilt ueber Wochen an. Liegt der Verkauf VOR dem Abrufzeitraum, kennt der Lauf
    nur einen Teil davon — ihn zu schreiben waere schlechter als der Altwert. Also
    werden nur Verkaeufe ab ``nur_ab`` angefasst.
    """
    ebay = _real_ebay()
    if date_from is not None:
        txs = await ebay.get_finance_transactions(
            date_from=date_from, date_to=date_to, max_pages=max_pages)
    else:
        txs = await ebay.get_finance_transactions(days=days, max_pages=max_pages)

    # Gebuehr je eBay-Order: SALE.totalFeeAmount (Verkaufsprovision)
    # + NON_SALE_CHARGE (v.a. AD_FEE = Anzeigengebuehr, order-referenziert).
    fee_by_order: dict[str, Decimal] = {}
    for t in txs:
        oid = _order_ref(t)
        if not oid:
            continue  # z.B. Shop-Abo (OTHER_FEES ohne Order) -> keine Order-Zuordnung
        ttype = t.get("transactionType")
        if ttype == "SALE":
            fee = _dec((t.get("totalFeeAmount") or {}).get("value"))
            if fee is not None:
                fee_by_order[oid] = fee_by_order.get(oid, Decimal("0")) + fee
        elif ttype in ("NON_SALE_CHARGE", "FEE"):
            amt = _dec((t.get("amount") or {}).get("value"))
            if amt is not None:
                fee_by_order[oid] = fee_by_order.get(oid, Decimal("0")) + abs(amt)

    sales = db.scalars(select(Sale).where(Sale.ebay_order_id.isnot(None))).all()
    by_order: dict[str, list[Sale]] = {}
    uebersprungen = 0
    for s in sales:
        if nur_ab is not None:
            wann = _utc(s.sale_date or s.created_at)
            if wann is None or wann < _utc(nur_ab):
                uebersprungen += 1
                continue
        by_order.setdefault(str(s.ebay_order_id), []).append(s)

    updated = 0
    for oid, fee in fee_by_order.items():
        group = by_order.get(oid)
        if not group:
            continue
        total_price = sum(Decimal(str(s.price_eur or 0)) for s in group)
        for s in group:
            if total_price > 0:
                share = (Decimal(str(s.price_eur or 0)) / total_price) * fee
            else:
                share = fee / len(group)
            s.fee_eur_actual = share.quantize(Decimal("0.01"))
            updated += 1
    db.commit()
    return {"transactions": len(txs), "orders_with_fees": len(fee_by_order),
            "sales_updated": updated, "sales_uebersprungen": uebersprungen}


async def mark_refunds(db: Session, *, days: int = 90) -> dict:
    """REFUND-Transaktionen der letzten N Tage auf die Sales mappen (status=refunded)."""
    ebay = _real_ebay()
    txs = await ebay.get_finance_transactions(days=days)
    marked = 0
    for t in txs:
        if t.get("transactionType") != "REFUND":
            continue
        oid = str(t.get("orderId") or "")
        if not oid:
            continue
        sale = db.scalar(select(Sale).where(Sale.ebay_order_id == oid))
        if sale is not None and sale.status != "refunded":
            sale.status = "refunded"
            marked += 1
    db.commit()
    return {"refunds_marked": marked}


async def backfill_sales_from_finances(db: Session, *, date_from, date_to,
                                       window_days: int = 85) -> dict:
    """Historische Verkaeufe aus der Finances API rekonstruieren (aelter als die
    90-Tage-Grenze von getOrders). Je SALE-Transaktion entsteht eine Sale mit
    Umsatz, Datum und ECHTER Gebuehr; REFUNDs werden am Sale-Status vermerkt.

    Idempotent ueber die eBay-Order-Nr. Buyer-Adresse liefert die API nicht mehr –
    fuer Umsatz-/Gewinn-/Steuerzahlen ist das vollstaendig ausreichend.
    """
    from datetime import timedelta

    from app.models import Sale as _Sale
    ebay = _real_ebay()
    existing_orders = {str(s.ebay_order_id) for s in db.scalars(select(Sale)).all()
                       if s.ebay_order_id}
    existing_tx = {str(s.ebay_transaction_id) for s in db.scalars(select(Sale)).all()}
    created = refunds = 0
    cur = date_from
    while cur < date_to:
        end = min(cur + timedelta(days=window_days), date_to)
        txs = await ebay.get_finance_transactions(date_from=cur, date_to=end, max_pages=10)
        for t in txs:
            ttype = t.get("transactionType")
            oid = str(t.get("orderId") or "")
            if ttype == "SALE" and oid and oid not in existing_orders:
                tx_id = f"FIN-{t.get('transactionId') or oid}"
                if tx_id in existing_tx:
                    continue
                amount = _dec((t.get("amount") or {}).get("value"))
                fee = _dec((t.get("totalFeeAmount") or {}).get("value"))
                when = None
                try:
                    from datetime import datetime as _dt
                    when = _dt.fromisoformat(str(t.get("transactionDate")).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass
                buyer = ((t.get("buyer") or {}).get("username"))
                db.add(_Sale(
                    ebay_transaction_id=tx_id, ebay_order_id=oid,
                    buyer_name=buyer, quantity=1,
                    price_eur=amount, fee_eur_actual=fee,
                    sale_date=when, status="delivered",
                ))
                existing_orders.add(oid)
                existing_tx.add(tx_id)
                created += 1
            elif ttype == "REFUND" and oid:
                sale = db.scalar(select(_Sale).where(_Sale.ebay_order_id == oid))
                if sale is not None and sale.status != "refunded":
                    sale.status = "refunded"
                    refunds += 1
        db.commit()
        cur = end
    return {"created": created, "refunds_marked": refunds}


def _finanzbericht_cache(year: int) -> dict | None:
    """Gecachten eBay-Finanzbericht lesen (None, wenn er fehlt oder kaputt ist).

    Bewusst KEIN Live-Abruf: der Steuerbericht laeuft synchron und darf nicht an
    ~30 API-Seiten haengen. Der Scheduler frischt den Cache taeglich auf.
    """
    import json
    from pathlib import Path
    p = Path("./data") / f"finance_report_{year}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def tax_report(db: Session, *, year: int) -> dict:
    """Jahres-Report: je Verkauf Einnahme, eBay-Gebuehr (echt/kalkuliert), EK, Bruttomarge.

    Dazu die Betriebsausgaben, die an KEINEM einzelnen Verkauf haengen und deshalb
    in keiner Zeile auftauchen koennen: kontobezogene eBay-Gebuehren (Shop-Abo,
    Einstellgebuehren) und Versandlabel. Quelle ist der gecachte Finanzbericht —
    fehlt er, stehen die Posten auf ``None`` statt auf 0 (Regel 14: nicht raten).
    """
    s = get_settings()
    orders = {o.sale_id: o for o in db.scalars(select(OrderAliexpress)).all() if o.sale_id}
    listings = {l.id: l for l in db.scalars(select(Listing)).all()}

    from app.services.common import VOID_SALE_STATUS

    rows = []
    t_rev = t_fee = t_cost = Decimal("0")
    fee_actual_count = 0
    storno_n = 0
    storno_rev = storno_fee = Decimal("0")
    for sale in db.scalars(select(Sale).order_by(Sale.sale_date, Sale.id)).all():
        when = sale.sale_date or sale.created_at
        if when is None or when.year != year:
            continue
        # Rueckabgewickelt = keine Einnahme. Das Geld ist zurueckgeflossen.
        # Der EK bleibt Betriebsausgabe (steckt in t_purchases, unabhaengig vom Verkauf).
        if (sale.status or "") in VOID_SALE_STATUS:
            storno_n += 1
            storno_rev += Decimal(str(sale.price_eur or 0))
            if sale.fee_eur_actual is not None:
                storno_fee += Decimal(str(sale.fee_eur_actual))
            continue
        listing = listings.get(sale.listing_id) if sale.listing_id else None
        order = orders.get(sale.id)
        revenue = Decimal(str(sale.price_eur or 0))
        if sale.fee_eur_actual is not None:
            fee, fee_src = Decimal(str(sale.fee_eur_actual)), "echt"
            fee_actual_count += 1
        else:
            # Kategorie-genaue Gebuehr (Provision + Anzeigenrate) inkl. 19% MwSt + Fixbetrag
            # inkl. MwSt. Rohes ebay_fee_pct liess Anzeigenrate + MwSt weg -> Gebuehr ~0,9€/
            # Verkauf zu niedrig, Brutto-Marge im Steuerreport zu hoch.
            fee_pct = pricing.effective_fee_pct(
                listing.category_name if listing is not None else None, settings=s)
            fee = (revenue * Decimal(str(fee_pct))
                   + Decimal(str(pricing.ebay_fixed_fee(s)))).quantize(Decimal("0.01")) if revenue else Decimal("0")
            fee_src = "kalkuliert"
        cost = Decimal(str(order.cost_cny)) if (order and order.cost_cny) else None
        margin = (revenue - fee - cost).quantize(Decimal("0.01")) if cost is not None else None
        rows.append({
            "datum": when.strftime("%d.%m.%Y"),
            "ebay_order": sale.ebay_order_id or sale.ebay_transaction_id,
            "produkt": (listing.title_seo if listing else "") or "",
            "menge": sale.quantity or 1,
            "einnahme_eur": float(revenue),
            "ebay_gebuehr_eur": float(fee),
            "gebuehr_quelle": fee_src,
            "einkauf_eur": float(cost) if cost is not None else None,
            "bruttomarge_eur": float(margin) if margin is not None else None,
        })
        t_rev += revenue
        t_fee += fee
        t_cost += cost or Decimal("0")

    # Jahres-Einkauf GESAMT (alle AliExpress-Kaeufe des Jahres, auch ohne Sale-Verknuepfung –
    # z.B. die per Browser importierte Alt-Historie). Fuer die Steuer zaehlt die Summe.
    t_purchases = Decimal("0")
    for o in db.scalars(select(OrderAliexpress)).all():
        when = o.order_date or o.created_at
        if when is not None and when.year == year and o.cost_cny:
            t_purchases += Decimal(str(o.cost_cny))

    # Betriebsausgaben ohne Verkaufsbezug (aus dem gecachten Finanzbericht).
    fb_gesamt = (_finanzbericht_cache(year) or {}).get("gesamt") or {}
    konto_geb = fb_gesamt.get("gebuehren_ohne_bestellbezug")
    versandlabel = fb_gesamt.get("versandlabel")
    weitere = None
    if konto_geb is not None and versandlabel is not None:
        weitere = Decimal(str(konto_geb)) + Decimal(str(versandlabel))
    bruttomarge = t_rev - t_fee - t_purchases

    return {
        "year": year,
        "rows": rows,
        "summary": {
            "verkaeufe": len(rows),
            "einnahmen_eur": float(t_rev),
            "ebay_gebuehren_eur": float(t_fee),
            "davon_echte_gebuehren": fee_actual_count,
            "einkauf_eur": float(t_cost),                      # mit Verkauf verknuepft
            "einkauf_gesamt_eur": float(t_purchases),          # alle Kaeufe des Jahres
            "bruttomarge_eur": float(bruttomarge),
            # Nicht still verschwinden lassen, sondern ausweisen:
            "stornos_ausgeschlossen": storno_n,
            "stornos_eur": float(storno_rev),
            "stornos_gebuehren_eur": float(storno_fee),
            # Ausgaben, die an keinem Verkauf haengen -> eigene Posten:
            "kontobezogene_gebuehren_eur": (float(konto_geb)
                                            if konto_geb is not None else None),
            "versandlabel_eur": (float(versandlabel)
                                 if versandlabel is not None else None),
            "weitere_betriebsausgaben_eur": (float(weitere)
                                             if weitere is not None else None),
            "ergebnis_eur": (float(bruttomarge - weitere)
                             if weitere is not None else None),
            "zusatzkosten_quelle": ("eBay-Finanzbericht" if weitere is not None
                                    else "fehlt – Finanzbericht nicht abgerufen"),
        },
    }


def tax_report_csv(db: Session, *, year: int) -> bytes:
    """Steuer-Report als CSV (Excel-kompatibel, deutsches Format via Semikolon)."""
    rep = tax_report(db, year=year)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Datum", "eBay-Order", "Produkt", "Menge", "Einnahme EUR",
                "eBay-Gebuehr EUR", "Gebuehr-Quelle", "Einkauf EUR", "Bruttomarge EUR"])
    def de(x):
        return str(x).replace(".", ",") if x is not None else ""
    for r in rep["rows"]:
        w.writerow([r["datum"], r["ebay_order"], r["produkt"], r["menge"],
                    de(r["einnahme_eur"]), de(r["ebay_gebuehr_eur"]), r["gebuehr_quelle"],
                    de(r["einkauf_eur"]), de(r["bruttomarge_eur"])])
    sm = rep["summary"]
    w.writerow([])
    w.writerow(["SUMME", "", f"{sm['verkaeufe']} Verkaeufe", "",
                de(sm["einnahmen_eur"]), de(sm["ebay_gebuehren_eur"]),
                f"{sm['davon_echte_gebuehren']} echt",
                de(sm["einkauf_eur"]), ""])
    w.writerow(["EINKAUF GESAMT (Jahr, alle AliExpress-Kaeufe)", "", "", "",
                "", "", "", de(sm["einkauf_gesamt_eur"]), ""])
    w.writerow(["BRUTTOMARGE (Einnahmen - Gebuehren - Einkauf gesamt)", "", "", "",
                "", "", "", "", de(sm["bruttomarge_eur"])])
    # Betriebsausgaben ohne Verkaufsbezug – tauchen in keiner Zeile oben auf.
    w.writerow([])
    fehlt = sm.get("weitere_betriebsausgaben_eur") is None
    w.writerow(["--- WEITERE BETRIEBSAUSGABEN (ohne Verkaufsbezug) ---",
                sm.get("zusatzkosten_quelle", "")])
    w.writerow(["eBay-Gebuehren ohne Bestellbezug (Shop-Abo, Einstellgebuehren)",
                "", "", "", "", de(sm.get("kontobezogene_gebuehren_eur")) if not fehlt
                else "unbekannt", "", "", ""])
    w.writerow(["eBay-Versandlabel", "", "", "", "",
                de(sm.get("versandlabel_eur")) if not fehlt else "unbekannt", "", "", ""])
    w.writerow(["ERGEBNIS (Bruttomarge - weitere Betriebsausgaben)", "", "", "",
                "", "", "", "",
                de(sm.get("ergebnis_eur")) if not fehlt else "unbekannt"])
    if sm.get("stornos_ausgeschlossen"):
        w.writerow([])
        w.writerow([f"NICHT enthalten: {sm['stornos_ausgeschlossen']} stornierte/erstattete "
                    f"Verkaeufe (Geld zurueckgezahlt)", "", "", "",
                    de(sm["stornos_eur"]), de(sm["stornos_gebuehren_eur"]), "", "", ""])
    return buf.getvalue().encode("utf-8-sig")  # BOM -> Excel oeffnet Umlaute korrekt


async def ebay_finance_report(*, year: int, refresh: bool = False) -> dict:
    """eBay-Finanzbericht aus der Finances-API: Verkaeufe (brutto), eBay-Gebuehren,
    Versandlabel, Erstattungen, Netto-Auszahlung – je MONAT + QUARTAL + Jahr.

    GECACHT unter data/finance_report_{year}.json (der Live-Abruf zieht ~1600
    Transaktionen ueber viele API-Seiten -> langsam). Der Scheduler frischt das
    laufende Jahr taeglich auf; ``refresh=True`` erzwingt Neuberechnung."""
    import json
    from datetime import datetime, timezone
    from pathlib import Path
    cache = Path("./data") / f"finance_report_{year}.json"
    if not refresh and cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    ebay = _real_ebay()
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    txs = await ebay.get_finance_transactions(date_from=start, date_to=end, max_pages=30)

    def blk():
        return {"count": 0, "brutto": Decimal("0"), "gebuehren": Decimal("0"),
                "gebuehren_ohne_order": Decimal("0"),
                "versandlabel": Decimal("0"), "erstattung": Decimal("0")}
    months = {m: blk() for m in range(1, 13)}
    for t in txs:
        d = str(t.get("transactionDate") or "")
        if d[:4] != str(year) or len(d) < 7:
            continue
        try:
            b = months[int(d[5:7])]
        except (ValueError, KeyError):
            continue
        typ = t.get("transactionType")
        amt = _dec((t.get("amount") or {}).get("value")) or Decimal("0")
        fee = _dec((t.get("totalFeeAmount") or {}).get("value")) or Decimal("0")
        if typ == "SALE":
            b["count"] += 1
            b["brutto"] += amt + fee        # amount = Netto -> brutto = Netto + Gebuehr
            b["gebuehren"] += fee
        elif typ in ("NON_SALE_CHARGE", "FEE"):
            b["gebuehren"] += abs(amt)
            # Ohne Bestellbezug (Shop-Abo, Einstellgebuehren) landet die Gebuehr an
            # KEINEM Verkauf -> der Steuerbericht kennt sie sonst gar nicht.
            if not _order_ref(t):
                b["gebuehren_ohne_order"] += abs(amt)
        elif typ == "SHIPPING_LABEL":
            b["versandlabel"] += abs(amt)
        elif typ == "REFUND":
            b["erstattung"] += abs(amt)

    def _row(period, b):
        netto = b["brutto"] - b["gebuehren"] - b["versandlabel"] - b["erstattung"]
        return {"period": period, "count": b["count"], "brutto": float(b["brutto"]),
                "gebuehren": float(b["gebuehren"]),
                "gebuehren_ohne_bestellbezug": float(b["gebuehren_ohne_order"]),
                "versandlabel": float(b["versandlabel"]),
                "erstattung": float(b["erstattung"]), "netto": float(netto)}

    monate = [_row(f"{year}-{m:02d}", months[m]) for m in range(1, 13)]
    quartale = []
    for q in range(4):
        qb = blk()
        for m in range(q * 3 + 1, q * 3 + 4):
            for k in qb:
                qb[k] += months[m][k]
        quartale.append(_row(f"{year} Q{q + 1}", qb))
    yb = blk()
    for m in range(1, 13):
        for k in yb:
            yb[k] += months[m][k]
    rep = {"year": year, "transactions": len(txs),
           "monate": monate, "quartale": quartale, "gesamt": _row(f"{year} gesamt", yb),
           "cached_at": datetime.now(timezone.utc).isoformat()}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
        # Die rohen Transaktionen extra zwischenspeichern: der Bericht zeigt nur
        # Summen, aber "worauf beruht diese Zahl?" verlangt die Einzelposten -
        # ohne diese Datei muessten wir dafuer erneut 1600 Zeilen von eBay holen.
        (Path("./data") / f"finance_transactions_{year}.json").write_text(
            json.dumps(txs, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return rep


def _tx_periode(datum: str) -> tuple[int, int]:
    """(Jahr, Monat) aus einem eBay-Transaktionsdatum ('2026-09-17T12:03:00.000Z')."""
    return int(datum[:4]), int(datum[5:7])


def finance_report_details(*, year: int, period: str, kategorie: str) -> dict:
    """Einzelposten, aus denen sich EINE Zahl im Finanzbericht zusammensetzt.

    ``period`` ist ``"{jahr} gesamt"``, ``"{jahr} Q{1-4}"`` oder ``"{jahr}-{monat}"``
    genau wie im Bericht. ``kategorie`` ist brutto/gebuehren/versandlabel/erstattung/netto.
    Liest die im Bericht mitgespeicherten Rohtransaktionen - kein neuer eBay-Aufruf.
    """
    import json
    from pathlib import Path

    cache = Path("./data") / f"finance_transactions_{year}.json"
    if not cache.exists():
        raise FileNotFoundError(
            "Noch keine Einzelposten gespeichert - einmal oben auf 'aktualisieren' klicken, "
            "dann liegen sie vor.")
    txs = json.loads(cache.read_text(encoding="utf-8-sig"))

    if period.endswith("gesamt"):
        monate_gesucht = set(range(1, 13))
    elif " Q" in period:
        q = int(period.rsplit("Q", 1)[1])
        monate_gesucht = set(range(q * 3 - 2, q * 3 + 1))
    else:
        monate_gesucht = {int(period.split("-")[1])}

    zeilen: list[dict] = []
    for t in txs:
        d = str(t.get("transactionDate") or "")
        if d[:4] != str(year) or len(d) < 7:
            continue
        try:
            _, monat = _tx_periode(d)
        except ValueError:
            continue
        if monat not in monate_gesucht:
            continue
        typ = t.get("transactionType")
        amt = _dec((t.get("amount") or {}).get("value")) or Decimal("0")
        fee = _dec((t.get("totalFeeAmount") or {}).get("value")) or Decimal("0")
        oid = _order_ref(t)
        memo = t.get("transactionMemo") or t.get("feeType") or ""

        if kategorie == "brutto" and typ == "SALE":
            zeilen.append({"datum": d[:10], "bezeichnung": f"Verkauf {oid or ''}".strip(),
                           "betrag_eur": float(amt + fee)})
        elif kategorie == "gebuehren":
            if typ == "SALE" and fee:
                zeilen.append({"datum": d[:10],
                               "bezeichnung": f"Verkaufsgebühr, Bestellung {oid or 'unbekannt'}",
                               "betrag_eur": -float(fee)})
            elif typ in ("NON_SALE_CHARGE", "FEE"):
                bez = memo or ("mit Bestellbezug" if oid else "ohne Bestellbezug (z. B. Shop-Abo)")
                zeilen.append({"datum": d[:10], "bezeichnung": f"Gebühr: {bez}",
                               "betrag_eur": -float(abs(amt))})
        elif kategorie == "versandlabel" and typ == "SHIPPING_LABEL":
            zeilen.append({"datum": d[:10], "bezeichnung": f"Versandlabel {oid or ''}".strip(),
                           "betrag_eur": -float(abs(amt))})
        elif kategorie == "erstattung" and typ == "REFUND":
            zeilen.append({"datum": d[:10], "bezeichnung": f"Erstattung {oid or ''}".strip(),
                           "betrag_eur": -float(abs(amt))})
        elif kategorie == "netto" and typ in ("SALE", "NON_SALE_CHARGE", "FEE", "SHIPPING_LABEL", "REFUND"):
            vorzeichen = 1 if typ == "SALE" else -1
            wert = (amt + fee) if typ == "SALE" else abs(amt)
            art = {"SALE": "Verkauf", "NON_SALE_CHARGE": "Gebühr", "FEE": "Gebühr",
                  "SHIPPING_LABEL": "Versandlabel", "REFUND": "Erstattung"}[typ]
            zeilen.append({"datum": d[:10], "bezeichnung": f"{art} {oid or memo or ''}".strip(),
                           "betrag_eur": vorzeichen * float(wert)})

    zeilen.sort(key=lambda z: z["datum"], reverse=True)
    return {"year": year, "period": period, "kategorie": kategorie,
            "anzahl": len(zeilen), "summe_eur": round(sum(z["betrag_eur"] for z in zeilen), 2),
            "zeilen": zeilen[:500]}


async def gebuehren_aufschluesselung(*, year: int) -> dict:
    """DIAGNOSE (nur lesen): eBay-Gebuehren des Jahres nach Art aufgeschluesselt.

    Hintergrund: der Finanzbericht zaehlt ALLE Gebuehren-Transaktionen, der
    Steuerbericht nur die, die an einem Verkauf haengen (``fee_eur_actual``).
    Die Differenz sind kontobezogene Gebuehren (Shop-Abo, Anzeigen ohne
    Bestellbezug) — echte Betriebsausgaben, die sonst niemand absetzt. Diese
    Funktion zeigt, WELCHE das sind, statt nur die Restgroesse zu nennen.

    Schreibt nichts. ``ohne_bestellbezug`` ist die gesuchte Luecke.
    """
    ebay = _real_ebay()
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    txs = await ebay.get_finance_transactions(date_from=start, date_to=end, max_pages=30)

    def _fee_art(t: dict) -> str:
        """Gebuehrenart aus den Feldern, die eBay je nach Transaktionsart nutzt."""
        for feld in ("feeType", "feeJurisdiction", "transactionMemo", "bookingEntry"):
            v = t.get(feld)
            if isinstance(v, str) and v:
                return v
        for ref in (t.get("references") or []):
            # ORDER_ID ist der Bestellbezug, keine Gebuehrenart.
            if (isinstance(ref, dict) and ref.get("referenceType")
                    and str(ref["referenceType"]).upper() != "ORDER_ID"):
                return str(ref["referenceType"])
        return "ohne Kennzeichnung"

    gruppen: dict[str, dict] = {}
    beispiele: list[dict] = []
    felder: set[str] = set()
    sale_fee_mit_order = Decimal("0")

    for t in txs:
        d = str(t.get("transactionDate") or "")
        if d[:4] != str(year):
            continue
        typ = t.get("transactionType")
        oid = _order_ref(t)          # wie im Gebuehren-Sync: inkl. references[ORDER_ID]
        if typ == "SALE":
            fee = _dec((t.get("totalFeeAmount") or {}).get("value")) or Decimal("0")
            if fee and oid:
                sale_fee_mit_order += fee
            continue
        if typ not in ("NON_SALE_CHARGE", "FEE"):
            continue
        betrag = abs(_dec((t.get("amount") or {}).get("value")) or Decimal("0"))
        art = _fee_art(t)
        schluessel = f"{typ} · {art}" + ("" if oid else " · OHNE Bestellbezug")
        g = gruppen.setdefault(schluessel, {"anzahl": 0, "summe": Decimal("0"),
                                            "mit_bestellbezug": bool(oid)})
        g["anzahl"] += 1
        g["summe"] += betrag
        if not oid:
            felder.update(t.keys())
            if len(beispiele) < 5:
                # Kaeuferdaten bewusst raus – fuer die Gebuehrenfrage irrelevant.
                # ``references`` BLEIBT: zeigt bei kontobezogenen Gebuehren, worauf
                # sie sich beziehen (z.B. Rechnungsnummer).
                beispiele.append({k: v for k, v in t.items()
                                  if k not in ("buyer", "orderLineItems")})

    zeilen = [{"art": k, "anzahl": v["anzahl"], "summe_eur": float(v["summe"]),
               "mit_bestellbezug": v["mit_bestellbezug"]}
              for k, v in sorted(gruppen.items(), key=lambda kv: -kv[1]["summe"])]
    ohne = sum(Decimal(str(z["summe_eur"])) for z in zeilen if not z["mit_bestellbezug"])
    mit = sum(Decimal(str(z["summe_eur"])) for z in zeilen if z["mit_bestellbezug"])
    return {
        "year": year,
        "transaktionen": len(txs),
        "verkaufsgebuehren_mit_bestellbezug": float(sale_fee_mit_order),
        "zusatzgebuehren_mit_bestellbezug": float(mit),
        "ohne_bestellbezug": float(ohne),          # <- die gesuchte Luecke
        "arten": zeilen,
        "beispiele_ohne_bestellbezug": beispiele,
        "felder_ohne_bestellbezug": sorted(felder),
    }


def ebay_finance_report_csv(rep: dict) -> bytes:
    """eBay-Finanzbericht (Monat + Quartal + Jahr) als CSV fuer den Steuerberater."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")

    def de(x):
        return f"{float(x):.2f}".replace(".", ",")

    def row(r):
        return [r["period"], r["count"], de(r["brutto"]), de(r["gebuehren"]),
                de(r["versandlabel"]), de(r["erstattung"]), de(r["netto"])]

    w.writerow(["Zeitraum", "Verkaeufe", "Umsatz brutto EUR", "eBay-Gebuehren EUR",
                "Versandlabel EUR", "Erstattungen EUR", "Netto-Auszahlung EUR"])
    w.writerow(["--- MONATLICH ---"])
    for r in rep["monate"]:
        w.writerow(row(r))
    w.writerow([])
    w.writerow(["--- QUARTALSWEISE ---"])
    for r in rep["quartale"]:
        w.writerow(row(r))
    w.writerow([])
    w.writerow(row(rep["gesamt"]))
    return buf.getvalue().encode("utf-8-sig")
