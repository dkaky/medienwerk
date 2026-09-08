"""Analytics-/Dashboard-Service: Aggregat-KPIs, Orders, Profit-Zeitreihe, Aktivitaet.

Reine Lesezugriffe fuer das Dashboard. Profit wird als Verkaufspreis minus
Listing-Einkaufskosten (cost_eur) genaehert – ohne eBay-Gebuehren, da diese je
nach Tarif schwanken; die genaue Marge liefert die Pricing-Engine pro Listing.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Invoice, Listing, OrderAliexpress, Product, Sale, TaskLog
from app.services import pricing


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _f(value) -> float | None:
    return float(value) if value is not None else None


def _display_specs(specs) -> dict:
    """Merkmale fuer die Anzeige bereinigen (Herkunftsland China nie zeigen)."""
    from app.spec_filter import strip_forbidden_specs
    return strip_forbidden_specs(specs or {}) or {}


def _gallery_images(product, listing) -> list:
    from app.services import golive_service as _gl
    imgs = _gl.gallery_images(product)
    return imgs or ([listing.image_url] if listing.image_url else [])


def _image_candidates(product) -> list:
    from app.services import golive_service as _gl
    return _gl.image_candidates(product)


def dashboard_summary(db: Session) -> dict:
    """Eine kompakte Kennzahl-Sammlung fuer die Uebersichtsseite."""
    total_listings = db.scalar(select(func.count()).select_from(Listing)) or 0
    active = db.scalar(
        select(func.count()).select_from(Listing).where(Listing.listing_status == "active")
    ) or 0
    drafts = db.scalar(
        select(func.count()).select_from(Listing).where(Listing.listing_status == "draft")
    ) or 0
    # Entwuerfe, deren Upload zu eBay hart gescheitert ist. Die Warteschlange
    # versucht es bei fluechtigen Fehlern selbst erneut; was hier landet, wartet
    # auf eine Entscheidung. Ohne diese Zahl bleibt so ein Entwurf unsichtbar
    # liegen - er sieht in der Liste aus wie jeder andere.
    publish_errors = db.scalar(
        select(func.count()).select_from(Listing)
        .where(Listing.listing_status == "draft", Listing.publish_error.is_not(None))
    ) or 0

    # Lieferfaehigkeit: EINE Zaehlweise fuer alle Stellen.
    #
    # Hier stand frueher eine eigene Abfrage - ein einziges Flag
    # (monitor_status == "out_of_stock"). Das Listing-Cockpit zaehlte anders, das
    # Sold-Out-Center ein drittes Mal, und keine Menge war Teilmenge einer
    # anderen: Die Startseite meldete 3, die Liste dahinter zeigte 7 Zeilen.
    #
    # Jetzt entscheidet lieferstatus() fuer alle. Der Unterschied ist nicht bloss
    # Ordnung: Die alte Abfrage sah weder einzelne tote Varianten noch eigenen
    # Bestand. Ein Artikel mit Ware im eigenen Regal zaehlte als ausverkauft,
    # ein Artikel mit drei toten Groessen gar nicht.
    from app.config import get_settings
    from app.services.listing_match_service import (
        AUS, HANDLUNGSBEDARF, _variant_rows, lieferstatus,
    )

    oos_items = []
    for l in db.scalars(select(Listing).where(Listing.listing_status == "active")):
        produkt = db.get(Product, l.product_id) if l.product_id else None
        vrows = _variant_rows(l, produkt, get_settings()) if produkt else []
        zustand = lieferstatus(l, vrows)
        if zustand not in HANDLUNGSBEDARF:
            continue
        oos_items.append({"listing_id": l.id, "title": (l.title_seo or "")[:80],
                          "ebay_item_id": l.ebay_item_id,
                          "zustand": zustand,
                          "ganz_aus": zustand == AUS})
    oos = len(oos_items)

    sales = db.scalars(select(Sale)).all()
    # Stornierte/erstattete Verkaeufe zaehlen NICHT als Verkauf (weder Anzahl noch
    # Umsatz) – sie werden nur als Info (refunded_eur/by_status) ausgewiesen.
    _void = {"refunded", "cancelled", "canceled", "storniert"}
    valid_sales = [s for s in sales if (s.status or "") not in _void]
    sales_with_revenue = [s for s in valid_sales if s.price_eur is not None]
    revenue = sum(float(s.price_eur) for s in sales_with_revenue)
    refunded = sum(float(s.price_eur) for s in sales
                   if (s.status or "") in _void and s.price_eur is not None)

    # Umsatz seit Jahresbeginn (YTD) separat ausweisen.
    year = _now().year
    revenue_ytd = 0.0
    for s in sales_with_revenue:
        when = s.sale_date or s.created_at
        if when is not None and when.year == year:
            revenue_ytd += float(s.price_eur)

    # BRUTTOGEWINN – simpel: Umsatz − eBay-Gebuehren − AliExpress-Ausgaben.
    # Nur TATSAECHLICHE Zahlen: echte eBay-Gebuehren (fee_eur_actual, inkl.
    # Anzeigen/Promoted) + ALLE erfassten AliExpress-Kaeufe. Kein kalkulierter EK,
    # keine Schaetzung. Solange nicht alle Kaeufe importiert sind, ist der Wert eine
    # Obergrenze und sinkt mit jedem weiteren importierten Kauf.
    fees_real = sum(float(s.fee_eur_actual) for s in sales_with_revenue
                    if getattr(s, "fee_eur_actual", None) is not None)
    fees_real_count = sum(1 for s in sales_with_revenue
                          if getattr(s, "fee_eur_actual", None) is not None)
    ae_orders = db.scalars(select(OrderAliexpress)).all()
    ae_spend = sum(float(o.cost_cny) for o in ae_orders if o.cost_cny is not None)
    ae_purchases_count = sum(1 for o in ae_orders if o.cost_cny is not None)
    profit = revenue - fees_real - ae_spend
    _linked = {o.sale_id for o in ae_orders if o.sale_id and o.cost_cny is not None}
    sales_without_purchase = sum(1 for s in sales_with_revenue if s.id not in _linked)

    # Orders nach Status
    status_counts: dict[str, int] = {}
    for s in sales:
        status_counts[s.status] = status_counts.get(s.status, 0) + 1
    pending_fulfillment = sum(
        v for k, v in status_counts.items()
        if k in ("pending", "needs_manual_review", "alternative_pending",
                 "manual_intervention_required")
    )
    # Fulfillment-Pipeline fuer die Uebersicht: was ist WO im Prozess?
    fulfillment = {
        # noch gar nicht bei AliExpress gekauft (Handlung noetig)
        "to_order": pending_fulfillment,
        # bei AliExpress gekauft/bezahlt, wartet auf Versand (noch kein Tracking)
        "awaiting_shipment": status_counts.get("ordered_aliexpress", 0),
        # unterwegs: Sendungsnummer vorhanden, noch nicht zugestellt
        "in_transit": status_counts.get("tracking", 0),
    }

    tasks_pending = db.scalar(
        select(func.count()).select_from(TaskLog)
        .where(TaskLog.status.in_(["pending", "in_progress"]))
    ) or 0
    tasks_failed = db.scalar(
        select(func.count()).select_from(TaskLog).where(TaskLog.status == "failed")
    ) or 0

    ended = db.scalar(
        select(func.count()).select_from(Listing).where(Listing.listing_status == "ended")
    ) or 0
    return {
        "listings": {"total": int(total_listings), "active": int(active),
                     "drafts": int(drafts), "ended": int(ended),
                     "publish_errors": int(publish_errors),
                     "out_of_stock": int(oos), "out_of_stock_items": oos_items},
        "sales": {
            "count": len(valid_sales),                 # ohne Stornos/Erstattungen
            "voided_count": len(sales) - len(valid_sales),
            "revenue_eur": round(revenue, 2),
            "revenue_ytd_eur": round(revenue_ytd, 2),
            "refunded_eur": round(refunded, 2),
            # BRUTTOGEWINN = Umsatz − eBay-Gebuehren − AliExpress-Ausgaben (nur echte Zahlen).
            "profit_eur": round(profit, 2),
            "fees_eur": round(fees_real, 2),                  # echte eBay-Gebuehren gesamt
            "fees_real_count": int(fees_real_count),
            "ae_spend_eur": round(ae_spend, 2),               # erfasste AliExpress-Ausgaben gesamt
            "ae_purchases_count": int(ae_purchases_count),
            "priced_sales_count": len(sales_with_revenue),
            "sales_without_purchase": int(sales_without_purchase),
            # Durchschnitt nur ueber Verkaeufe MIT Preis (sonst verzerrt durch null-Preise).
            "avg_order_eur": round(revenue / len(sales_with_revenue), 2) if sales_with_revenue else 0.0,
            "pending_fulfillment": int(pending_fulfillment),
            "fulfillment": fulfillment,
            "by_status": status_counts,
        },
        "tasks": {"pending": int(tasks_pending), "failed": int(tasks_failed)},
    }


def empirical_fee_rates(db: Session) -> dict:
    """Median EFFEKTIVE Gebuehrenquote (fee - fixgebuehr) / VK je TOP-Kategorie aus den
    ECHTEN eBay-Gebuehren (Finances API, fee_eur_actual). Datenbasiert -> beruecksichtigt
    die reale, kategorie-individuelle Anzeigengebuehr, die eine pauschale 10 %-Rate NICHT
    trifft (Sale 1151-1153: Schmuck echt ~31-34 %, Pauschal-Schaetzung nur 26 %). Nur
    Kategorien mit >= 5 echten Gebuehren; sonst faellt der Aufrufer auf die Kategorie-
    Provision + Anzeigenrate zurueck."""
    from statistics import median
    # Die ECHTE eBay-Gebuehr (fee_eur_actual) ist brutto (inkl. MwSt) -> der darin enthaltene
    # Fixbetrag ist 0,45 x 1,19 = 0,5355. Zum Herausrechnen der reinen variablen Quote muss
    # genau dieser Brutto-Fixbetrag abgezogen werden (nicht das Netto-0,45).
    fix = pricing.ebay_fixed_fee()
    _void = {"refunded", "cancelled", "canceled", "storniert"}
    rows = db.execute(
        select(Sale.price_eur, Sale.fee_eur_actual, Sale.status, Listing.category_name)
        .join(Listing, Sale.listing_id == Listing.id, isouter=True)
        .where(Sale.fee_eur_actual.isnot(None), Sale.price_eur > 0)).all()
    by_cat: dict[str, list] = {}
    for vk, fee, status, cat in rows:
        vkf = float(vk or 0)
        if vkf <= 0:
            continue
        # Stornierte/erstattete Sales tragen eine unbereinigte (oft ueberhoehte) Gebuehr
        # -> wuerden die Median-Quote verzerren. Gleiche Filterbasis wie die Anzeige.
        if (status or "") in _void:
            continue
        top = (cat or "").split(":")[0].strip() or "?"
        by_cat.setdefault(top, []).append(max(0.0, (float(fee) - fix) / vkf))
    return {c: round(median(v), 4) for c, v in by_cat.items() if len(v) >= 5}


def _order_economics(s: Sale, order, listing, product: Product | None = None, *,
                     fee_pct: float, fixed_fee: float,
                     fee_rate_by_cat: dict | None = None) -> dict:
    """Umsatz/EK/Gebuehr/Gewinn einer Bestellung.

    EK-Reihenfolge (Vorfall Smart-Brille 11.07. – falscher „Verlust"):
    1. ECHTER Einkauf (order.cost_cny: Beleg-Betrag bzw. Bestellzeitpunkt-Kalkulation).
    2. EK der TATSAECHLICH VERKAUFTEN Variante (SKU-Preis + echte Versandkosten) –
       nicht der Listing-EK, der bewusst die TEUERSTE Variante traegt (Preis-Boden!).
       Bei Multivarianten-Produkten lag der Worst-Case-EK sonst weit ueber dem echten
       Einkauf (Smart-Brille: 30,19 € statt ~10 €) -> Gewinn faelschlich negativ.
    3. Listing-EK NUR bei Produkten ohne echte Varianten (dort ist er korrekt).
    4. Multivarianten-Produkt, Variante nicht aufloesbar -> EK UNBEKANNT (None):
       lieber ehrlich „unbekannt" als eine grob falsche Zahl (Nutzerregel:
       Finanz-Kennzahlen nur aus tatsaechlich erfassten Werten).
    """
    from app.config import get_settings
    revenue = float(s.price_eur or 0)
    qty = int(s.quantity or 1)
    actual = float(order.cost_cny) if (order and order.cost_cny and float(order.cost_cny) > 0) else None
    cost = actual
    cost_source = "actual" if actual is not None else None
    skus = ((getattr(product, "variants", None) or {}).get("skus") or []) if product else []
    if cost is None and len(skus) > 1:
        sku = _resolved_sku(product, s.variant_selected, listing)
        price = (sku or {}).get("price")
        if price is not None:
            try:
                _st = get_settings()
                sov = (float(listing.supplier_ship_eur)
                       if listing is not None and listing.supplier_ship_eur is not None else None)
                from app.services.fast_shipping_service import variants_have_eu_warehouse as _vheu
                cost = pricing.effective_cost_bundle(
                    float(price), quantity=qty, settings=_st, ship_override=sov,
                    local=_vheu(getattr(listing, "product", None) if listing is not None else None))
                cost_source = "variant_est"
            except (TypeError, ValueError):
                cost = None
        # Multivariante ohne Aufloesung/Preis -> bewusst None (kein Worst-Case-EK).
    elif cost is None and listing is not None and listing.cost_eur not in (None, 0):
        cost = float(listing.cost_eur) * qty
        cost_source = "listing_est"
    # Echte Gebuehr (Finances API) bevorzugen, sonst KATEGORIE-genaue Schaetzung
    # (Provision je Kategorie + Anzeigenrate). Die flache Pauschale (22%+10%) lag bei
    # Elektronik massiv daneben (Sale 1150: Kopfhoerer = 7% Provision -> Netto zu
    # niedrig angezeigt). Faellt auf die uebergebene Pauschale zurueck, wenn keine
    # Kategorie bekannt ist.
    if getattr(s, "fee_eur_actual", None) is not None:
        fee = round(float(s.fee_eur_actual), 2)
    else:
        cat = getattr(listing, "category_name", None) if listing is not None else None
        top = (cat or "").split(":")[0].strip()
        # 1. Wahl: empirische Median-Quote dieser Kategorie aus ECHTEN Gebuehren
        # (beruecksichtigt die reale Anzeigengebuehr). 2. Wahl: Kategorie-Provision +
        # Anzeigenrate. 3. Wahl: uebergebene Pauschale.
        rate = (fee_rate_by_cat or {}).get(top)
        if rate is None:
            rate = pricing.effective_fee_pct_for_listing(listing, settings=get_settings()) if cat else fee_pct
        fee = round(revenue * rate + fixed_fee, 2) if revenue else 0.0
    profit = round(revenue - cost - fee, 2) if cost is not None else None
    return {
        "revenue": round(revenue, 2),
        "cost": round(cost, 2) if cost is not None else None,
        "cost_is_actual": actual is not None,
        "cost_known": cost is not None,
        "cost_source": cost_source,   # actual | variant_est | listing_est | None
        "fee": fee,
        "fee_is_actual": getattr(s, "fee_eur_actual", None) is not None,
        "profit": profit,
    }


def _resolved_sku(product: Product | None, variant_selected: dict | None,
                  listing: Listing | None = None) -> dict | None:
    """Die VERKAUFTE AliExpress-SKU einer Bestellung finden – mit derselben Logik wie das
    Fulfillment: (1) bereits aufgeloester attr am Sale, (2) gelernte variant_map/Regeln
    (order_service._resolve_variant), (3) Werte-Ueberlappung als letzter Versuch.
    Wichtig fuer 'as picture'-Produkte (Vorfall Motocross-Brille): dort unterscheiden sich
    die eBay-Werte ('Only Goggles (365458)') komplett von den AE-Werten ('as picture') –
    nur attr/gelernte Map treffen."""
    if not product or not variant_selected or not isinstance(variant_selected, dict):
        return None
    skus = (getattr(product, "variants", None) or {}).get("skus") or []
    if not skus:
        return None
    # 1) Sale traegt bereits den aufgeloesten attr (nach Bestellung/manueller Zuordnung)
    attr = variant_selected.get("attr")
    if attr:
        hit = next((s for s in skus if s.get("attr") == attr), None)
        if hit is not None:
            return hit
    # 2) Gelernte Zuordnung + Regel-Matching (bewusst OHNE KI – hier geht es nur ums Bild)
    try:
        from app.services import order_service as _os
        resolved = _os._resolve_variant(product, {k: v for k, v in variant_selected.items()
                                                  if k not in ("attr", "options", "id", "ebay_sku")}, listing)
        r_attr = (resolved or {}).get("attr")
        if r_attr:
            hit = next((s for s in skus if s.get("attr") == r_attr), None)
            if hit is not None:
                return hit
    except Exception:  # noqa: BLE001 – Bild ist nice-to-have, nie einen Request brechen
        pass
    # 3) Werte-Ueberlappung (robust gegen von eBay umbenannte Achsen: Farbe->Duft etc.)
    want = {str(v).strip().lower() for k, v in variant_selected.items()
            if k not in ("attr", "options", "id", "ebay_sku") and v not in (None, "")}
    if not want:
        return None
    best, best_score = None, 0
    for sku in skus:
        have = {str(v).strip().lower() for v in (sku.get("options") or {}).values()}
        score = len(want & have)
        if score > best_score:
            best, best_score = sku, score
    return best if best_score > 0 else None


def _variant_image(product: Product | None, variant_selected: dict | None,
                   listing: Listing | None = None) -> str | None:
    """Bild der VERKAUFTEN Variante (None -> Fallback = Listing-Hauptbild).

    VORRANG: das Bild aus dem EIGENEN eBay-Listing (listing.ebay_variant_images,
    per pull_ebay_variant_images geholt) – der Nutzer erkennt SEINE Variante am
    eigenen Bild ('Only Goggles (365458)' sagt nichts, das Bild alles). Erst danach
    die AliExpress-SKU-Aufloesung als Fallback."""
    if listing is not None and variant_selected and getattr(listing, "ebay_variant_images", None):
        try:
            from app.services.order_service import variant_map_key
            img = (listing.ebay_variant_images or {}).get(variant_map_key(variant_selected))
            if img:
                return img
        except Exception:  # noqa: BLE001 – Bild ist nice-to-have
            pass
    sku = _resolved_sku(product, variant_selected, listing)
    return (sku or {}).get("image") or None


def list_orders(db: Session, *, status: str | None = None, limit: int = 50) -> dict:
    """Chronologischer Verkaufs-/Gewinn-Ledger (neueste zuerst) inkl. Summen ueber ALLE Verkaeufe."""
    from app.config import get_settings
    st = get_settings()
    # Gebuehren-SCHAETZUNG (solange fee_eur_actual aus der Finances-API fehlt):
    # Provision + ANZEIGEN-Rate — die Promoted-Gebuehr fehlte hier und liess Gewinne
    # ~10 % vom VK zu hoch erscheinen (real #1142/#1145: je ~31,4 % + 0,45 €).
    # Pauschale nur als LETZTER Fallback (inkl. MwSt, wie die echten eBay-Gebuehren).
    fee_pct = pricing.effective_fee_pct(None, settings=st)
    fixed_fee = pricing.ebay_fixed_fee(st)
    fee_rate_by_cat = empirical_fee_rates(db)   # datenbasierte Gebuehrenquote je Kategorie (inkl. MwSt)

    order_by_sale = {
        o.sale_id: o for o in db.scalars(select(OrderAliexpress)).all() if o.sale_id
    }
    # Kaufbeleg je Bestellung: damit in der Orders-Liste direkt der Beleg (und die
    # daraus erzeugte Rechnung) verlinkt werden kann – ohne Umweg ueber die Belegablage.
    beleg_by_order: dict[int, Invoice] = {}
    for _inv in db.scalars(
            select(Invoice).where(Invoice.type == "aliexpress_purchase")).all():
        if _inv.order_id is None:
            continue
        # echtes Original schlaegt Platzhalter, falls es beides gibt
        vorhanden = beleg_by_order.get(_inv.order_id)
        if vorhanden is None or (not getattr(vorhanden, "is_original", False)
                                 and getattr(_inv, "is_original", False)):
            beleg_by_order[_inv.order_id] = _inv
    listing_by_id = {l.id: l for l in db.scalars(select(Listing)).all()}
    product_by_id = {p.id: p for p in db.scalars(select(Product)).all()}

    # Nach ECHTEM Verkaufsdatum sortieren (sale_date), nicht nach created_at
    # (= DB-Einfuegezeit; bei Backfill/Import stimmt die Reihenfolge sonst nicht,
    # und aktuelle Verkaeufe rutschen aus dem Limit -> "verschwunden").
    order_key = func.coalesce(Sale.sale_date, Sale.created_at).desc()
    stmt = select(Sale)
    if status and status != "all":
        stmt = stmt.where(Sale.status == status)
    stmt = stmt.order_by(order_key, Sale.id.desc()).limit(min(max(limit, 1), 2000))
    sales = db.scalars(stmt).all()

    # Mehrpositions-Bestellungen erkennen: Positionen je eBay-Order zaehlen, damit
    # das UI z.B. 803+804 als EINE Bestellung (Pos. 1/2, 2/2) gruppieren kann.
    multi_pos: dict[str, int] = {
        oid: cnt for oid, cnt in db.execute(
            select(Sale.ebay_order_id, func.count())
            .where(Sale.ebay_order_id.isnot(None))
            .group_by(Sale.ebay_order_id).having(func.count() > 1)).all()
    }
    pos_seen: dict[str, int] = {}

    items = []
    for s in sales:
        order = order_by_sale.get(s.id)
        listing = listing_by_id.get(s.listing_id) if s.listing_id else None
        product = product_by_id.get(listing.product_id) if listing and listing.product_id else None
        addr = s.delivery_address or {}
        eco = _order_economics(s, order, listing, product, fee_pct=fee_pct,
                               fixed_fee=fixed_fee, fee_rate_by_cat=fee_rate_by_cat)
        item_count = multi_pos.get(s.ebay_order_id or "", 1)
        position = None
        if item_count > 1:
            pos_seen[s.ebay_order_id] = pos_seen.get(s.ebay_order_id, 0) + 1
            position = pos_seen[s.ebay_order_id]
        # Existiert bereits eine AliExpress-Bestellung, ist der Sale NIE mehr "offen" –
        # sonst wirkt er unbestellt, der „Bestellen"-Button erscheint erneut und der
        # Doppelbestellungs-Schutz blockt nur noch (Vorfall Sale 1116). Anzeige korrigieren.
        disp_status = s.status
        if order is not None and order.aliexpress_order_id and s.status in (
                "pending", "needs_manual_review"):
            disp_status = "tracking" if order.tracking_number else "ordered_aliexpress"
        items.append({
            "sale_id": s.id,
            "order_item_count": item_count,
            "order_position": position,
            "ebay_cancel_state": s.ebay_cancel_state,
            "ebay_transaction_id": s.ebay_transaction_id,
            "ebay_order_id": s.ebay_order_id,
            "listing_id": s.listing_id,
            "ebay_item_id": listing.ebay_item_id if listing else None,
            "title": listing.title_seo if listing else None,
            # Artikelbild: Listing-Hauptbild, sonst 1. Produktbild (viele frisch angelegte
            # Listings haben KEIN eigenes image_url -> sonst bliebe die Order bildlos, obwohl
            # das Produkt Bilder hat, z.B. Sale 1116).
            "image": (listing.image_url if listing and listing.image_url else None)
                     or (product.images[0] if product and product.images else None),
            # Bild der tatsaechlich verkauften Variante (Fallback: Artikelbild oben).
            "variant_image": _variant_image(product, s.variant_selected, listing),
            # Fulfillment braucht eine AliExpress-Quelle am Produkt; fehlt sie,
            # bietet das Dashboard "🔗 Quelle zuordnen" (Link einfuegen) an.
            "has_source": bool(product is not None and product.aliexpress_url),
            # Ganz-Listing-Eigenbestand ("__listing__"): selbst versenden, KEINE Quelle noetig.
            "self_stock_listing": bool(
                isinstance(getattr(listing, "self_stock", None), dict)
                and isinstance((listing.self_stock or {}).get("__listing__"), dict)
                and int(((listing.self_stock or {}).get("__listing__") or {}).get("qty") or 0) > 0),
            "buyer_name": s.buyer_name,
            "delivery_city": addr.get("city"),
            "delivery_country": addr.get("country"),
            # Liefer-Check (16.08.): Klartext-Warnung, wenn der Haendler NICHT ins
            # Zielland liefert (nur bei definitiver Absage gesetzt).
            "liefer_warnung": ((getattr(s, "delivery_check", None) or {}).get("warnung")),
            "variant_selected": s.variant_selected,
            "price_eur": eco["revenue"],
            "cost_eur": eco["cost"],
            "cost_is_actual": eco["cost_is_actual"],
            "fee_eur": eco["fee"],
            "fee_is_actual": eco["fee_is_actual"],
            "profit_eur": eco["profit"],
            "quantity": s.quantity,
            "status": disp_status,
            "cancel_reviewed": bool(getattr(s, "cancel_reviewed", False)),
            "ae_paid": bool(getattr(s, "ae_paid", False)),
            "sale_date": s.sale_date or s.created_at,
            "order_pk": order.id if order else None,
            # Beleg/Rechnung zu genau diesem Einkauf (fuer die Knoepfe in der Zeile)
            "beleg_id": (beleg_by_order.get(order.id).id
                         if order and beleg_by_order.get(order.id) else None),
            "beleg_original": bool(order and beleg_by_order.get(order.id)
                                   and beleg_by_order[order.id].is_original),
            "hat_rechnung": bool(order and beleg_by_order.get(order.id)
                                 and getattr(beleg_by_order[order.id], "generated_path", None)),
            "aliexpress_order_id": order.aliexpress_order_id if order else None,
            "tracking_number": order.tracking_number if order else None,
            "tracking_reported": s.status == "tracking",
            # Nur GESCHAETZTES Lieferdatum (Carrier/AliExpress) — ein echtes Ankunftsdatum
            # existiert nicht. Fuer die "Lieferzeit"-Spalte im Dashboard.
            "estimated_delivery": order.estimated_delivery if order else None,
            "fulfillment_status": order.status if order else None,
        })

    # Summen ueber ALLE Verkaeufe (nicht nur die angezeigte Seite) – der Gewinn-Hebel.
    # Gewinn/EK zaehlen NUR fuer Verkaeufe mit bekannter EK (sonst waere die Marge geschoent).
    # Stornierte/erstattete Verkaeufe zaehlen NICHT als Umsatz (wie dashboard_summary).
    _void = {"refunded", "cancelled", "canceled", "storniert"}
    t_rev = t_fee = 0.0
    kc_rev = kc_cost = kc_profit = 0.0
    n = with_cost = 0
    for s in db.scalars(select(Sale)).all():
        if (s.status or "") in _void:
            continue
        _l = listing_by_id.get(s.listing_id) if s.listing_id else None
        eco = _order_economics(s, order_by_sale.get(s.id), _l,
                               product_by_id.get(_l.product_id) if _l and _l.product_id else None,
                               fee_pct=fee_pct, fixed_fee=fixed_fee, fee_rate_by_cat=fee_rate_by_cat)
        t_rev += eco["revenue"]; t_fee += eco["fee"]; n += 1
        if eco["cost_known"]:
            kc_rev += eco["revenue"]; kc_cost += eco["cost"]; kc_profit += eco["profit"]; with_cost += 1
    # Gesamter AliExpress-Einkauf (importierte Kaufhistorie) -> realer Gesamtgewinn.
    ae_spend = 0.0
    for o in order_by_sale.values():
        ae_spend += float(o.cost_cny) if o.cost_cny else 0.0
    for o in db.scalars(select(OrderAliexpress).where(OrderAliexpress.sale_id.is_(None))).all():
        ae_spend += float(o.cost_cny) if o.cost_cny else 0.0

    summary = {
        "count": n,
        "with_cost_count": with_cost,
        "revenue_eur": round(t_rev, 2),
        "fees_eur": round(t_fee, 2),
        "cost_eur": round(kc_cost, 2),                 # nur bekannte EK (je Sale)
        "profit_eur": round(kc_profit, 2),             # Gewinn nur wo EK bekannt
        "profit_revenue_eur": round(kc_rev, 2),        # Umsatzbasis dieses Gewinns
        "margin_pct": round((kc_profit / kc_rev) if kc_rev else 0.0, 4),  # Bruch (Frontend * 100)
        "ae_spend_eur": round(ae_spend, 2),            # gesamter AliExpress-Einkauf (Import)
        "profit_real_eur": round(t_rev - ae_spend - t_fee, 2) if ae_spend else None,  # Umsatz - EK - Gebühr
    }
    return {"orders": items, "count": len(items), "summary": summary}


def profit_timeseries(db: Session, *, days: int = 30) -> dict:
    """Taegliche Umsatz-/Profit-/Order-Reihe der letzten `days` Tage (luecklos)."""
    days = min(max(days, 1), 365)
    start = (_now() - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    # Profit je Verkauf: echte Gebuehr (fee_eur_actual, sonst kalkuliert) + echter
    # AliExpress-EK der VERKNUEPFTEN Bestellung. Keine Listing-EK-Naeherung mehr
    # (heutige/falsche EKs auf alte Verkaeufe = verzerrter Chart).
    from app.config import get_settings as _gs
    _st = _gs()
    fee_rate_by_cat = empirical_fee_rates(db)
    # Vollen Kategorienamen speichern: empirische Quote wird ueber die TOP-Kategorie
    # gelookupt, der Fallback effective_fee_pct braucht aber den vollen Namen (identisch
    # zur Orders-Ledger-Seite _order_economics, sonst weichen Chart und Ledger ab).
    cat_by_listing = {l.id: (getattr(l, "category_name", None) or "")
                      for l in db.scalars(select(Listing)).all()}
    order_cost_by_sale = {
        o.sale_id: float(o.cost_cny) for o in db.scalars(select(OrderAliexpress)).all()
        if o.sale_id and o.cost_cny is not None
    }

    buckets: "OrderedDict[str, dict]" = OrderedDict()
    for i in range(days):
        key = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        buckets[key] = {"date": key, "revenue_eur": 0.0, "profit_eur": 0.0, "orders": 0}

    # Stornierte/erstattete Verkaeufe verfaelschen die Zeitreihe nicht.
    _void = {"refunded", "cancelled", "canceled", "storniert"}
    sales = [s for s in db.scalars(select(Sale).where(Sale.price_eur.isnot(None))).all()
             if (s.status or "") not in _void]
    for s in sales:
        when = s.sale_date or s.created_at
        if when is None:
            continue
        # In UTC normalisieren, damit die Tages-Buckets korrekt sind (naive Werte sind UTC,
        # aware Werte werden konvertiert – nicht nur umetikettiert).
        when = when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)
        if when < start:
            continue
        key = when.strftime("%Y-%m-%d")
        b = buckets.get(key)
        if b is None:
            continue
        price = float(s.price_eur)
        b["revenue_eur"] = round(b["revenue_eur"] + price, 2)
        b["orders"] += 1
        if getattr(s, "fee_eur_actual", None) is not None:
            fee = float(s.fee_eur_actual)
        else:
            # Dieselbe 3-Stufen-Logik wie die Orders-Ledger (_order_economics): echte
            # Gebuehr -> empirische Median-Quote der TOP-Kategorie -> Kategorie-Provision
            # + Anzeigenrate (effective_fee_pct, MwSt-inkl.). Sonst weichen Chart und
            # Ledger fuer denselben Verkauf ab.
            full_cat = cat_by_listing.get(s.listing_id, "")
            top = full_cat.split(":")[0].strip()
            rate = fee_rate_by_cat.get(top)
            if rate is None:
                rate = pricing.effective_fee_pct(full_cat or None, settings=_st)
            fee = price * rate + pricing.ebay_fixed_fee(_st)
        cost = order_cost_by_sale.get(s.id, 0.0)
        b["profit_eur"] = round(b["profit_eur"] + (price - fee - cost), 2)

    series = list(buckets.values())
    return {
        "days": days,
        "series": series,
        "totals": {
            "revenue_eur": round(sum(b["revenue_eur"] for b in series), 2),
            "profit_eur": round(sum(b["profit_eur"] for b in series), 2),
            "orders": sum(b["orders"] for b in series),
        },
    }


def list_ebay_products(db: Session, *, status: str | None = None, limit: int = 1000) -> dict:
    """Produkt-/Listing-Übersicht mit eBay-Bezug (SKU, Item/Offer, Status, Preis, Bestand).

    Default-Limit deckt den gesamten Bestand ab (~400 Listings) – vorher schnitt das
    200er-Limit bei gleichem created_at (Massen-Import) beliebige Listings ab, die
    dadurch im Tab unauffindbar waren (Fall NE39/Listing 181). id-Tiebreaker macht
    die Sortierung deterministisch.
    """
    stmt = select(Listing).order_by(Listing.created_at.desc(), Listing.id.desc()) \
        .limit(min(max(limit, 1), 5000))
    if status and status != "all":
        stmt = stmt.where(Listing.listing_status == status)
    listings = db.scalars(stmt).all()
    products = {p.id: p for p in db.scalars(select(Product)).all()}

    from app.services import golive_service as _gl
    from app.services.listing_match_service import effective_ebay_price
    from app.config import get_settings as _gs
    _st = _gs()
    items = []
    for l in listings:
        p = products.get(l.product_id) if l.product_id else None
        cost = _f(l.cost_eur)
        # Gewinn EINHEITLICH wie Cockpit/Optimierung/Dialog rechnen: kategorie- UND listing-genaue
        # eBay-Gebuehr (Provision + Anzeigenrate des Listings), NICHT die Pauschale von profit_at_price.
        _feep = pricing.effective_fee_pct_for_listing(l, settings=_st)
        _fix = pricing.ebay_fixed_fee(_st)

        def _profit(pr, ek):
            return None if (pr is None or ek is None) else round(pr - pr * _feep - _fix - ek, 2)
        # EINHEITLICH: den ECHTEN eBay-Preis anzeigen (nicht den internen). Vor dem ersten
        # Live-Abgleich faellt es auf den internen Preis zurueck (price_is_live=False).
        _live_single = effective_ebay_price(l, l.ebay_sku or f"AE-{l.id}")
        price = _live_single if _live_single is not None else _f(l.price_eur)
        price_is_live = _live_single is not None
        # Varianten-Preisspanne: bei Multivarianten-Listings ist EIN Preis/Profit/Marge
        # irrefuehrend (jede Variante hat ihren eigenen EK/VK). Spanne mitliefern,
        # damit das UI ehrlich "X,XX–Y,YY €" statt einer falschen Einzelzahl zeigt.
        vcount = 0
        price_min = price_max = None
        profit_min = profit_max = margin_min = margin_max = None
        try:
            vprices = _gl.compute_variant_prices(l, p, _st) if p is not None else []
        except Exception:  # noqa: BLE001 – Anzeige darf nie am Preis-Rechnen scheitern
            vprices = []
        if len(vprices) > 1:
            # Je Variante der ECHTE eBay-Preis (exakt/konservativ), sonst interner Kalkulationspreis.
            rows = []
            for v in vprices:
                # Signatur NUR aus den publizierten Achsen (axis_options), nicht dem rohen options-Dict.
                pr = effective_ebay_price(l, v.get("sku"), v.get("axis_options"))
                if pr is not None:
                    price_is_live = True
                else:
                    pr = round(v["price_eur"], 2) if v.get("price_eur") else None
                rows.append((pr, v.get("ek_eur")))
            vals = [pr for pr, _ek in rows if pr]
            if vals:
                vcount, price_min, price_max = len(vprices), round(min(vals), 2), round(max(vals), 2)
            # Profit-/Marge-Spanne je Variante (VK - Gebuehren - EK je Variante), auf dem echten Preis.
            pv = []
            for pr, ek in rows:
                if pr and ek is not None:
                    prof = _profit(pr, ek)
                    if prof is not None:
                        pv.append((prof, prof / pr))
            if pv:
                profits = [x[0] for x in pv]
                margins = [x[1] for x in pv]
                profit_min, profit_max = round(min(profits), 2), round(max(profits), 2)
                margin_min, margin_max = round(min(margins), 4), round(max(margins), 4)
        items.append({
            "listing_id": l.id,
            "title": l.title_seo,
            "image": (p.images[0] if p and p.images else None) or l.image_url,
            "ebay_sku": l.ebay_sku,
            "ebay_item_id": l.ebay_item_id,
            "ebay_offer_id": l.ebay_draft_id,
            "listing_status": l.listing_status,
            "price_eur": price,
            "price_is_live": price_is_live,   # True = echter eBay-Preis, False = interner Fallback
            "cost_eur": cost,
            # Echter Gewinn: VK - eBay-Gebuehren (kategorie-/listing-genau) - EK, NICHT nur VK - EK
            "profit_eur": _profit(price, cost),
            "variant_count": vcount,          # 0/1 = Einzelpreis, >1 = Spanne
            "price_min_eur": price_min,
            "price_max_eur": price_max,
            "profit_min_eur": profit_min,
            "profit_max_eur": profit_max,
            "margin_min": margin_min,
            "margin_max": margin_max,
            "quantity": l.quantity_available,
            "monitor_status": l.monitor_status,
            "supplier_in_stock": bool(l.supplier_in_stock),
            "category_id": l.category_id,
            "category_name": l.category_name,
            "aliexpress_url": (p.aliexpress_url if p else None),
            "sales_total": l.sales_total,
            "views_30d": l.views_30d,
            "created_at": l.created_at,
            # Publish-Warteschlange: laeuft gerade hoch? / letzter Upload-Fehler
            "publish_queued": bool(l.publish_queued),
            "publish_error": l.publish_error,
        })
    all_listings = db.scalars(select(Listing)).all()
    stats = {
        "total": len(all_listings),
        "active": sum(1 for x in all_listings if x.listing_status == "active"),
        "draft": sum(1 for x in all_listings if x.listing_status == "draft"),
        "ended": sum(1 for x in all_listings if x.listing_status == "ended"),
        # Verkaufte Stueck gesamt (Lebenszeit) ueber ALLE Listings – Summe der
        # eBay-Verkaufszaehler (via '📊 Statistik laden' aktualisiert).
        "sales_total": sum(int(x.sales_total or 0) for x in all_listings),
    }
    return {"products": items, "stats": stats}


def get_listing_detail(db: Session, *, listing_id: int) -> dict | None:
    """Volldetails eines Listings für die Detailansicht (Bilder, Beschreibung, eBay-Link)."""
    l = db.get(Listing, listing_id)
    if l is None:
        return None
    p = db.get(Product, l.product_id) if l.product_id else None
    cost = _f(l.cost_eur)
    # EINHEITLICH: der angezeigte Preis/Gewinn ist der ECHTE eBay-Preis (nicht der interne) –
    # sonst zeigt die Detailansicht als einzige Flaeche eine optimistische Scheinmarge, wenn der
    # interne Preis vom echten eBay-Preis driftet. Vor dem ersten Abgleich Fallback auf intern.
    from app.services.listing_match_service import effective_ebay_price
    from app.config import get_settings as _gs
    _st = _gs()
    _live = effective_ebay_price(l, l.ebay_sku or f"AE-{l.id}")
    price = _live if _live is not None else _f(l.price_eur)
    price_is_live = _live is not None
    # Gewinn kategorie-/listing-genau (Provision + Anzeigenrate), wie Cockpit/Optimierung/Dialog –
    # nicht die Pauschale von profit_at_price.
    _feep = pricing.effective_fee_pct_for_listing(l, settings=_st)
    _fix = pricing.ebay_fixed_fee(_st)
    _profit = None if (price is None or cost is None) else round(price - price * _feep - _fix - cost, 2)
    ebay_url = f"https://www.ebay.de/itm/{l.ebay_item_id}" if l.ebay_item_id else None
    return {
        "listing_id": l.id,
        "title": l.title_seo,
        "description": l.description,
        "listing_status": l.listing_status,
        "price_eur": price,
        "price_is_live": price_is_live,     # echter eBay-Preis vs. interner Fallback
        "internal_price_eur": _f(l.price_eur),
        "cost_eur": cost,
        # Echter Gewinn auf dem ECHTEN eBay-Preis: VK - eBay-Gebuehren (kategorie-genau + fix) - EK
        "profit_eur": _profit,
        "quantity": l.quantity_available,
        "monitor_status": l.monitor_status,
        "supplier_in_stock": bool(l.supplier_in_stock),
        "ebay_sku": l.ebay_sku,
        "ebay_item_id": l.ebay_item_id,
        "ebay_offer_id": l.ebay_draft_id,
        "ebay_url": ebay_url,
        "item_specifics": _display_specs(l.item_specifics),
        "category_id": l.category_id,
        "category_name": l.category_name,
        # Galerie = Standardbilder + Variantenbilder (jede Variante sichtbar).
        "images": _gallery_images(p, l),
        "image_candidates": _image_candidates(p),
        "aliexpress_url": (p.aliexpress_url if p else None),
        "alternatives": (p.alternatives if p else None),
        "variants": (p.variants if p else None),
        "variant_stock": l.variant_stock,
        "variant_source_map": l.variant_source_map,
        "supplier_id": (p.supplier_id if p else None),
        "sales_total": l.sales_total,
        "views_30d": l.views_30d,
        "stats_synced_at": l.stats_synced_at,
        "created_at": l.created_at,
        "last_monitored_at": l.last_monitored_at,
    }


def list_aliexpress_orders(db: Session, *, status: str | None = None, limit: int = 200) -> dict:
    """Alle AliExpress-Bestellungen mit Kosten, Tracking, verknüpftem Verkauf + Kaufbeleg."""
    stmt = select(OrderAliexpress).order_by(OrderAliexpress.created_at.desc()).limit(min(max(limit, 1), 500))
    if status and status != "all":
        stmt = stmt.where(OrderAliexpress.status == status)
    orders = db.scalars(stmt).all()

    receipts = {
        i.order_id: i for i in db.scalars(
            select(Invoice).where(Invoice.type == "aliexpress_purchase")
        ).all() if i.order_id
    }
    items = []
    total_cost = 0.0
    for o in orders:
        cost = _f(o.cost_cny)
        if cost:
            total_cost += cost
        inv = receipts.get(o.id)
        items.append({
            "order_id": o.id,
            "aliexpress_order_id": o.aliexpress_order_id,
            "product_id": o.product_id,
            "quantity": o.quantity,
            "cost_cny": cost,
            "status": o.status,
            "tracking_number": o.tracking_number,
            "tracking_carrier": o.tracking_carrier,
            "sale_id": o.sale_id,
            "order_date": o.order_date or o.created_at,
            "invoice_id": inv.id if inv else None,
            "has_receipt": bool(inv),
        })
    all_orders = db.scalars(select(OrderAliexpress)).all()
    with_receipt = sum(1 for o in all_orders if o.id in receipts)
    return {
        "orders": items,
        "stats": {
            "count": len(all_orders),
            "total_cost_cny": round(total_cost, 2),
            "with_receipt": with_receipt,
            "missing_receipt": max(0, len(all_orders) - with_receipt),
        },
    }


def recent_activity(db: Session, *, limit: int = 25) -> dict:
    """Letzte Task-Log-Eintraege (Audit) fuer den Aktivitaets-Feed."""
    rows = db.scalars(
        select(TaskLog).order_by(TaskLog.created_at.desc()).limit(min(max(limit, 1), 100))
    ).all()
    return {"activity": [{
        "id": t.id,
        "task_type": t.task_type,
        "reference_id": t.reference_id,
        "status": t.status,
        "error_message": t.error_message,
        "created_at": t.created_at,
    } for t in rows]}


# ---------------------------------------------------------------------------
# Portfolio-Analyse (Optimierung -> "Portfolio"): Wo verdient der Shop sein
# Geld wirklich? Konzentration, Trichter-Schubladen, Neuzugaenge, Preisbaender.
# Reine Lesezugriffe; gerechnet wird auf ECHT verknuepften Verkaeufen
# (listing_id gesetzt, keine Stornos) - nicht verknuepfbare Alt-Importe werden
# transparent separat ausgewiesen statt still mitgezaehlt (Regel 14: nie raten).
# ---------------------------------------------------------------------------

_PORTFOLIO_BANDS = ((0, 15), (15, 25), (25, 35), (35, 50), (50, 80), (80, None))
_NEW_LISTING_WEEKS = 8


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    vs = sorted(values)
    mid = len(vs) // 2
    return vs[mid] if len(vs) % 2 else (vs[mid - 1] + vs[mid]) / 2


def get_portfolio_analysis(db: Session) -> dict:
    """Kennzahlen fuer den Portfolio-Reiter unter Optimierung (nur lesen)."""
    valid_sale = Sale.status.notin_(("cancelled", "refunded"))
    sales = db.scalars(select(Sale).where(valid_sale)).all()
    active = db.scalars(select(Listing).where(Listing.listing_status == "active")).all()

    linked = [s for s in sales if s.listing_id is not None and s.price_eur is not None]
    unlinked = [s for s in sales if s.listing_id is None]

    # --- 1) Konzentration: wie viele Listings tragen 50/80/95 % des Umsatzes?
    by_listing: dict[int, dict] = {}
    for s in linked:
        e = by_listing.setdefault(s.listing_id, {"revenue": 0.0, "sales": 0})
        e["revenue"] += float(s.price_eur)
        e["sales"] += 1
    ranked = sorted(by_listing.items(), key=lambda kv: -kv[1]["revenue"])
    linked_revenue = sum(e["revenue"] for _, e in ranked)

    def _listings_for_share(share: float) -> int:
        cum = 0.0
        for i, (_, e) in enumerate(ranked, 1):
            cum += e["revenue"]
            if cum >= linked_revenue * share:
                return i
        return len(ranked)

    titles = {l.id: l for l in active}
    top = []
    for lid, e in ranked[:15]:
        l = titles.get(lid) or db.get(Listing, lid)
        top.append({
            "listing_id": lid,
            "title": (l.title_seo if l else None) or "?",
            "ebay_item_id": l.ebay_item_id if l else None,
            "revenue_eur": round(e["revenue"], 2),
            "sales": e["sales"],
        })

    # --- 2) Trichter-Schubladen ueber die aktiven Listings
    funnel = {"winner": 0, "clicked_no_sale": 0, "shown_no_click": 0, "invisible": 0}
    stats_missing = 0
    for l in active:
        v30 = l.views_30d
        iw = l.impressions_week or 0
        cw = l.clicks_week or 0
        if v30 is None:
            stats_missing += 1
        if (l.sales_total or 0) > 0:
            funnel["winner"] += 1
        elif (v30 or 0) == 0 and iw == 0 and cw == 0:
            funnel["invisible"] += 1
        elif cw == 0 and (v30 or 0) == 0:
            funnel["shown_no_click"] += 1
        else:
            funnel["clicked_no_sale"] += 1

    # --- 3) Neuzugaenge (letzte N Wochen) vs. Altbestand
    cutoff = _now() - timedelta(weeks=_NEW_LISTING_WEEKS)

    def _started(l: Listing):
        d = l.listing_start_date or l.created_at
        if d is not None and d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d

    new_l = [l for l in active if (_started(l) or _now()) >= cutoff]
    old_l = [l for l in active if (_started(l) or _now()) < cutoff]

    def _cohort(ls: list[Listing]) -> dict:
        sold = [l for l in ls if (l.sales_total or 0) > 0]
        return {"listings": len(ls), "sold_any": len(sold),
                "sales_total": sum(l.sales_total or 0 for l in ls)}

    # --- 4) Kategorien nach Umsatz (nur verknuepfte Verkaeufe)
    cats: dict[str, dict] = {}
    for s in linked:
        l = titles.get(s.listing_id) or db.get(Listing, s.listing_id)
        key = (l.category_name if l else None) or "Ohne Kategorie"
        e = cats.setdefault(key, {"revenue": 0.0, "sales": 0})
        e["revenue"] += float(s.price_eur)
        e["sales"] += 1
    active_per_cat: dict[str, int] = {}
    for l in active:
        key = l.category_name or "Ohne Kategorie"
        active_per_cat[key] = active_per_cat.get(key, 0) + 1
    categories = [{
        "category": k, "revenue_eur": round(e["revenue"], 2), "sales": e["sales"],
        "avg_bon_eur": round(e["revenue"] / e["sales"], 2) if e["sales"] else None,
        "active_listings": active_per_cat.get(k, 0),
    } for k, e in sorted(cats.items(), key=lambda kv: -kv[1]["revenue"])[:10]]

    # --- 5) Preisbaender (Bon-Verteilung)
    bands = []
    for lo, hi in _PORTFOLIO_BANDS:
        xs = [float(s.price_eur) for s in sales
              if s.price_eur is not None and float(s.price_eur) >= lo
              and (hi is None or float(s.price_eur) < hi)]
        bands.append({"from_eur": lo, "to_eur": hi, "sales": len(xs),
                      "revenue_eur": round(sum(xs), 2)})

    # --- 6) Gewinner-Profil (aktive Listings mit >=3 Verkaeufen)
    winners = [l for l in active if (l.sales_total or 0) >= 3]
    w_prices = [float(l.price_eur) for l in winners if l.price_eur is not None]
    w_margins = [float(l.price_eur) - float(l.cost_eur)
                 for l in winners if l.price_eur is not None and l.cost_eur is not None]
    winner_cats: dict[str, int] = {}
    for l in winners:
        key = l.category_name or "Ohne Kategorie"
        winner_cats[key] = winner_cats.get(key, 0) + 1

    return {
        "generated_at": _now().isoformat(),
        "basis": {
            "sales_linked": len(linked),
            "revenue_linked_eur": round(linked_revenue, 2),
            "sales_unlinked": len(unlinked),
            "revenue_unlinked_eur": round(sum(float(s.price_eur) for s in unlinked
                                              if s.price_eur is not None), 2),
            "active_listings": len(active),
        },
        "concentration": {
            "listings_with_sale": len(ranked),
            "n_for_50pct": _listings_for_share(0.5) if ranked else 0,
            "n_for_80pct": _listings_for_share(0.8) if ranked else 0,
            "n_for_95pct": _listings_for_share(0.95) if ranked else 0,
            "top": top,
        },
        "funnel": {**funnel, "stats_missing": stats_missing},
        "cohorts": {"new_weeks": _NEW_LISTING_WEEKS,
                    "new": _cohort(new_l), "old": _cohort(old_l)},
        "categories": categories,
        "price_bands": bands,
        "winner_profile": {
            "count": len(winners),
            "price_median_eur": round(_median(w_prices), 2) if w_prices else None,
            "price_min_eur": round(min(w_prices), 2) if w_prices else None,
            "price_max_eur": round(max(w_prices), 2) if w_prices else None,
            "margin_median_eur": round(_median(w_margins), 2) if w_margins else None,
            "top_categories": [{"category": k, "count": n} for k, n
                               in sorted(winner_cats.items(), key=lambda kv: -kv[1])[:8]],
        },
    }
