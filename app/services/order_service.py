"""Bereich 2: Auftragsabwicklung (Spec Kap. 5.2).

IPN-Eingang -> Sale buchen -> Listing/Product-Match -> Buyer-Validierung
-> AliExpress-Bestellung (Selenium) -> Tracking. Mit Error-Handling fuer
ProductNotFound (Bildsuche), OutOfStock und Captcha.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger("app.services.order")

from app.config import get_settings
from app.integrations import get_aliexpress_client
from app.integrations.aliexpress import (OrderRejectedError, OutOfStockError,
                                         ProductNotFoundError)
from app.models import Listing, OrderAliexpress, Product, Sale
from app.retry import PersistentError
from app.services.common import task_log

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Vereinfachte PLZ-Pruefung je Land (Spec Kap. 5.2 Step 4).
_POSTCODE_RE = {
    "DE": re.compile(r"^\d{5}$"),
    "AT": re.compile(r"^\d{4}$"),
    "US": re.compile(r"^\d{5}(-\d{4})?$"),
}


def ingest_ipn(db: Session, payload: dict) -> Sale:
    """Step 1+2: IPN-Payload in die Sales-Tabelle buchen (idempotent ueber tx_id)."""
    tx_id = payload.get("transaction_id") or payload.get("ebay_transaction_id")
    if not tx_id:
        raise PersistentError("IPN ohne transaction_id")

    existing = db.scalar(select(Sale).where(Sale.ebay_transaction_id == tx_id))
    if existing is not None:
        return existing  # bereits verarbeitet

    # Step 3: Listing-Match ueber eBay item_number
    item_id = payload.get("item_number") or payload.get("ebay_item_id")
    listing = (
        db.scalar(select(Listing).where(Listing.ebay_item_id == str(item_id)))
        if item_id
        else None
    )

    # Verkaufspreis aus Payload, sonst Listing-Preis (fuer Umsatz-/Profit-KPIs).
    price_eur = _to_decimal(payload.get("price") or payload.get("total") or payload.get("amount"))
    if price_eur is None and listing is not None:
        price_eur = listing.price_eur

    sale = Sale(
        ebay_transaction_id=str(tx_id),
        ebay_order_id=payload.get("order_id"),
        listing_id=listing.id if listing else None,
        buyer_name=payload.get("buyer_name"),
        buyer_email=payload.get("buyer_email"),
        delivery_address=payload.get("delivery_address"),
        quantity=int(payload.get("quantity", 1)),
        variant_selected=payload.get("variant"),
        price_eur=price_eur,
        sale_date=datetime.now(timezone.utc),
        status="pending",
    )
    db.add(sale)
    db.commit()
    db.refresh(sale)

    # Automatik: § 19-Verkaufsrechnung sofort erzeugen (darf IPN nicht brechen).
    try:
        from app.services import invoice_service
        invoice_service.generate_sale_invoice(db, sale_id=sale.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("auto sale-invoice failed", extra={"sale_id": sale.id, "error": str(exc)})
    return sale


def _to_decimal(value) -> Decimal | None:
    """Tolerante Zahl->Decimal-Konvertierung fuer IPN-Felder (str/float/None)."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError, TypeError):
        return None


# AE-Order-Status, deren Summe als EK zaehlt (bezahlt/in Abwicklung/abgeschlossen).
# FAIL-CLOSED: unbekannte Status (storniert, unbezahlt, Risk-Control, eingefroren)
# liefern None -> Schaetzung uebernimmt. Verhindert, dass stornierte/nie bezahlte
# Orders volle Kosten als EK bekommen (Review-Fund 13.07.).
_PAID_ORDER_STATUSES = {
    "PLACE_ORDER_SUCCESS", "WAIT_SELLER_SEND_GOODS", "SELLER_PART_SEND_GOODS",
    "WAIT_BUYER_ACCEPT_GOODS", "FUND_PROCESSING", "FINISH",
}


async def _real_order_cost_eur(ae, aliexpress_order_id) -> Decimal | None:
    """ECHTE Order-Summe in EUR (trade.ds.order.get, USD->EUR per Config-Kurs).

    Best-effort: liefert None statt zu werfen (der Bestell-Flow darf an der
    Kosten-Abfrage nie scheitern; dann greift die Schaetzung als Fallback)."""
    detail_fn = getattr(ae, "get_order_detail", None)
    if not detail_fn or not aliexpress_order_id:
        return None
    try:
        d = await detail_fn(str(aliexpress_order_id))
        status = str(d.get("order_status") or "").upper()
        if status not in _PAID_ORDER_STATUSES:
            logger.info("order detail: Status %s nicht bezahlt/aktiv -> kein EK-Uebernahme", status or "?")
            return None
        total = float(d.get("total") or 0)
        if total <= 0:
            return None
        cur = str(d.get("currency") or "USD").upper()
        s = get_settings()
        if cur == "EUR":
            eur = total
        elif cur == "USD":
            eur = total * float(getattr(s, "usd_to_eur_rate", 0.8765) or 0.8765)
        else:
            logger.warning("order detail: unbekannte Waehrung %s -> Schaetzung", cur)
            return None
        return Decimal(str(round(eur, 2)))
    except Exception as exc:  # noqa: BLE001 – Kosten-Lookup ist best-effort
        logger.warning("get_order_detail fehlgeschlagen: %s", str(exc)[:120])
        return None


def validate_buyer(sale: Sale) -> list[str]:
    """Step 4: Buyer-Daten validieren. Gibt Liste der Probleme zurueck (leer = ok)."""
    problems: list[str] = []
    if not sale.buyer_name or len(sale.buyer_name) < 2:
        problems.append("buyer_name fehlt/zu kurz")
    if sale.buyer_email and not _EMAIL_RE.match(sale.buyer_email):
        problems.append("buyer_email ungueltig")
    addr = sale.delivery_address or {}
    country = (addr.get("country") or "DE").upper()
    postcode = str(addr.get("postal") or addr.get("postcode") or "").strip()
    # Laender-Praefix vor der PLZ ("A-1010", "AT 1010", "D-50667") ist kein Fehler —
    # dieselbe Normalisierung wie beim AliExpress-Adressaufbau (Fall Bestellung 1256, AT).
    m = re.fullmatch(r"[A-Za-z]{1,3}[-\s]+(.*\d.*)", postcode)
    if m:
        postcode = m.group(1).strip()
    rule = _POSTCODE_RE.get(country)
    if rule and not rule.match(postcode):
        problems.append(f"PLZ '{postcode}' ungueltig fuer {country}")
    return problems


async def _ensure_product_variants(db: Session, product: Product | None) -> list:
    """Varianten (skus) des Produkts sicherstellen – bei leerer DB-Spalte LIVE nachladen.

    Produkte aus dem Bildsuche-Auto-Match wurden ohne ``scrape_product`` angelegt,
    ihre ``variants``-Spalte ist leer -> Varianten-Modal war leer und die
    Blindkauf-Sperre lief ins Leere. Hier wird bei Bedarf einmal frisch gescrapt
    und persistiert (inkl. Slot-0-Metadaten). Scheitert der Abruf, kommt [] zurueck
    – die Aufrufer behandeln das als "Varianten unbekannt" (KEIN Blindkauf).
    """
    if product is None:
        return []
    skus = ((product.variants or {}).get("skus") or [])
    if skus or not product.aliexpress_url:
        return skus
    ae = get_aliexpress_client()
    try:
        scraped = await ae.scrape_product(product.aliexpress_url)
    except Exception as exc:  # noqa: BLE001 – Live-Abruf darf UI/Fulfillment nicht crashen
        logger.warning("variant live-load failed",
                       extra={"product_id": product.id, "error": str(exc)[:150]})
        return []
    v = getattr(scraped, "variants", None) or None
    if not v:
        return []
    product.variants = v
    flag_modified(product, "variants")
    if getattr(scraped, "price_cny", None):
        product.price_cny = scraped.price_cny
    if getattr(scraped, "supplier_id", None) and not product.supplier_id:
        product.supplier_id = scraped.supplier_id
    # Slot-0-Metadaten (variant_count, Preis, Bestand) mit auffrischen – best effort.
    try:
        from app.services import supplier_service as ss
        alt = ss.ensure_primary_slot(product)
        if alt.get("sources"):
            alt["sources"][0] = ss._slot_from_scrape(
                scraped, product.aliexpress_url, alt["sources"][0].get("added") or "auto")
            ss._recalc_averages(alt)
            product.alternatives = alt
            flag_modified(product, "alternatives")
    except Exception:  # noqa: BLE001
        pass
    db.commit()
    return ((product.variants or {}).get("skus") or [])


_OPEN_SALE_STATES = ("pending", "needs_manual_review", "alternative_pending",
                     "manual_intervention_required")


def _stock_int(value) -> int | None:
    """SKU-Bestand robust zu int parsen ('100', '1,000', 5) – None = unbekannt."""
    if value is None:
        return None
    try:
        s = str(value).strip().replace(",", "").replace(" ", "")
        return int(float(s)) if s else None
    except (TypeError, ValueError):
        return None


def _single_sku_fallback(variant: dict | None, skus: list) -> tuple[dict | None, dict | None]:
    """EINDEUTIGKEITS-FALLBACK: hat die Quelle GENAU EINE SKU, ist die Bestellung eindeutig.

    Gilt ausdruecklich AUCH fuer VARIANTENLOSE Produkte, deren einzige SKU gar kein
    ``sku_attr`` traegt (AliExpress liefert dann nur eine sku_id -> unser attr ist
    None). Frueher verlangte der Fallback hier ein wahres ``attr``, sprang also
    nicht an, und die Blindkauf-Sperre stoppte die Bestellung mit "Variante nicht
    eindeutig zuordenbar" - eine Sackgasse, denn auch der Varianten-Dialog haette
    nichts anzubieten.

    Uebernommen aus dem Ursprungssystem (dort behoben nach Vorfall Sale 1371);
    unsere Kopie hatte die alte Fassung mit der ``attr``-Bedingung behalten.

    Rueckgabe: ``(variant, single_only)``. ``single_only`` ist die aufgeloeste SKU
    (oder None) und dient als Beleg "es gibt nichts zu raten" - die Aufrufer finden
    damit Preis und Bestand ohne attr-Vergleich und duerfen die Blindkauf-Sperre
    ueberspringen. Kein Blindkauf-Risiko: es existiert nur diese eine Variante.
    """
    if not (variant or {}).get("attr") and len(skus or []) == 1:
        only = skus[0]
        return ({"attr": only.get("attr"), "options": only.get("options") or {},
                 "id": only.get("id")}, only)
    return variant, None


def _assert_variant_stock(db: Session, sale: Sale, skus: list, variant: dict | None,
                          *, prefix: str = "") -> None:
    """BESTANDS-PREFLIGHT (Vorfall Sale 1124): Reicht der bekannte Varianten-Bestand nicht
    fuer die bestellte Menge, wuerde AliExpress nur kryptisch DELIVERY_METHOD_NOT_EXIST
    melden (keine Versandart fuer die Menge) -> VOR dem Geld-Call klar stoppen.

    Blockt NUR bei positivem Signal (Bestand bekannt UND zu klein) – unbekannter Bestand
    blockt nie. Die Zahl stammt aus dem letzten Scrape/Monitor-Lauf (Cache), darum steht
    der Hinweis auf Neu-Abgleich in der Meldung."""
    if not (variant and variant.get("attr") and skus):
        return
    hit = next((s for s in skus if s.get("attr") == variant["attr"]), None)
    need = int(sale.quantity or 1)
    stock_n = _stock_int((hit or {}).get("stock"))
    if stock_n is not None and stock_n < need:
        sale.status = "needs_manual_review"
        db.commit()
        raise PersistentError(
            f"{prefix}⛔ Lieferanten-Bestand reicht nicht: Variante hat nur {stock_n} Stueck, "
            f"benoetigt {need}. Optionen: Ausweich-Quelle verknuepfen (Preis-Check → "
            f"🔗 Quellen), Quelle aktualisieren und erneut versuchen, oder Kaeufer "
            f"kontaktieren (Teillieferung/Storno klaeren; Rest ggf. manuell auf AliExpress "
            f"bestellen). Hinweis: Die Bestandszahl stammt aus dem letzten Abgleich – hat "
            f"der Lieferant nachgefuellt, im Preis-Check synchronisieren oder bewusst "
            f"'Trotzdem bestellen'.")


async def _abort_if_ebay_cancelled(db: Session, sale: Sale) -> None:
    """GELD-GUARD: Direkt vor dem Einkauf den Live-Stornostatus bei eBay pruefen.

    Faengt den Fall "Kaeufer hat soeben storniert, Sync lief noch nicht" ab
    (Vorfall Sale 806). Laeuft nur auf der Betriebs-Instanz (monitor_push_real)
    – die Dev-Kopie fragt das echte eBay nicht. Abruf-Fehler blockieren nicht
    (best effort), nur ein POSITIVER Storno-Befund stoppt den Kauf.
    """
    if not get_settings().monitor_push_real or not sale.ebay_order_id:
        return
    try:
        state = await _real_ebay().get_order_cancel_state(sale.ebay_order_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cancel-state check failed", extra={"sale_id": sale.id,
                                                           "error": str(exc)[:150]})
        return
    if state == "CANCELED":
        sale.status = "cancelled"
        sale.ebay_cancel_state = state
        db.commit()
        raise PersistentError("eBay meldet: Bestellung wurde STORNIERT – es wird nicht eingekauft.")
    if state == "IN_PROGRESS":
        sale.ebay_cancel_state = state
        db.commit()
        raise PersistentError("Kaeufer hat eine Storno-Anfrage gestellt – erst auf eBay "
                              "klaeren, dann bestellen.")


def _claim_order_slot(db: Session, *, sale: Sale, product: Product,
                      delivery_name: str, delivery_address: dict,
                      source_aliexpress_id: str | None) -> OrderAliexpress:
    """DOPPELBESTELLUNGS-LOCK: OrderAliexpress-Zeile als Claim VOR dem Geld-Call.

    UNIQUE(sale_id) macht das Insert atomar: der zweite parallele Aufruf (manueller
    Klick + 5-Min-Poll auf demselben Loop, Retry nach Commit-Fehler, Doppelklick)
    laeuft in IntegrityError und bestellt NICHT noch einmal.
    """
    claim = OrderAliexpress(
        sale_id=sale.id,
        product_id=product.id,
        source_aliexpress_id=source_aliexpress_id,
        variant_selected=sale.variant_selected,
        quantity=sale.quantity or 1,
        delivery_name=delivery_name,
        delivery_address=delivery_address,
        order_date=datetime.now(timezone.utc),
        status="ordering",
    )
    db.add(claim)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise PersistentError("Fuer diesen Verkauf laeuft bereits eine Bestellung "
                              "(Doppelbestellungs-Schutz).")
    return claim


def _rollback_claim(db: Session, claim: OrderAliexpress) -> None:
    """Claim entfernen – NUR wenn SICHER keine AliExpress-Order angelegt wurde
    (Ablehnung/OOS/NotFound). Sale bleibt danach bestellbar."""
    try:
        db.delete(claim)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def _mark_claim_uncertain(db: Session, sale: Sale, claim: OrderAliexpress,
                          tl, exc: BaseException) -> None:
    """Bestellung mit UNKLAREM Ausgang: Claim behalten (blockt Doppelkauf), Sale zur
    manuellen Pruefung, Alarm ins Audit-Log. Order KANN bei AliExpress existieren."""
    try:
        claim.status = "verify_needed"
        sale.status = "needs_manual_review"
        if tl is not None:
            tl.result_data = {"verify_needed": True, "sale_id": sale.id,
                              "error": str(exc)[:200],
                              "hint": "AliExpress-Konto pruefen: existiert die Bestellung? "
                                      "Order-ID nachtragen ODER Claim freigeben."}
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    logger.error("AE-Bestellung UNKLAR – Claim behalten", extra={
        "sale_id": sale.id, "error": str(exc)[:200]})


_CLAIM_STALE_MINUTES = 15


def _claim_is_stale(claim: OrderAliexpress) -> bool:
    """True, wenn ein 'ordering'-Claim aelter als die Karenz ist (haengengeblieben)."""
    when = claim.order_date or claim.created_at
    if when is None:
        return True
    from datetime import timedelta
    w = when if getattr(when, "tzinfo", None) else when.replace(tzinfo=timezone.utc)
    return w < datetime.now(timezone.utc) - timedelta(minutes=_CLAIM_STALE_MINUTES)


def sweep_stale_claims(db: Session) -> dict:
    """Verwaiste 'ordering'-Claims (Crash/CancelledError/App-Neustart vor dem Ergebnis)
    aufraeumen: Sale -> needs_manual_review + Alarm. KEIN Auto-Delete (Order koennte
    existieren!). Laeuft beim App-Start und periodisch im Scheduler."""
    stale = db.scalars(select(OrderAliexpress).where(
        OrderAliexpress.status == "ordering",
        OrderAliexpress.aliexpress_order_id.is_(None))).all()
    swept = 0
    for claim in stale:
        if not _claim_is_stale(claim):
            continue
        sale = db.get(Sale, claim.sale_id) if claim.sale_id else None
        if sale is None:
            continue
        _mark_claim_uncertain(db, sale, claim, None,
                              RuntimeError("verwaister ordering-Claim beim Sweep"))
        swept += 1
    if swept:
        logger.warning("stale claims swept", extra={"count": swept})
    return {"swept": swept}


def _find_source_slot(product: Product, source_aliexpress_id: str) -> dict:
    alt = product.alternatives if isinstance(product.alternatives, dict) else {}
    slot = next((s for s in (alt.get("sources") or [])
                 if str(s.get("aliexpress_id")) == str(source_aliexpress_id)), None)
    if slot is None or not slot.get("url"):
        raise PersistentError("Gewaehlte Quelle ist nicht (mehr) in den Anbieter-Slots.")
    return slot


async def _route_to_variant_alt_source(db: Session, *, sale: Sale, listing, product,
                                       ae) -> dict | None:
    """AUTO-ROUTING auf die verknuepfte Ausweich-VARIANTE (Nutzer-Projekt 11.07.).

    Greift NUR wenn ALLES zutrifft (sonst None = normaler Pfad):
    1. Der Nutzer hat im Preis-Check fuer die verkaufte Primaer-Variante eine Ausweich-
       Quelle MIT konkreter Ziel-Variante verknuepft (Dict-Eintrag; Legacy-Strings ohne
       Ziel-Variante routen NIE automatisch – das waere Blindkauf).
    2. Die Primaer-Variante ist regelbasiert EINDEUTIG aufloesbar (ohne KI, deterministisch).
    3. Ein FRISCHER Scrape der Hauptquelle beweist: genau diese Variante/Menge ist dort
       nicht lieferbar (Produkt weg / in_stock False / attr weg / Bestand < Menge).
       Scrape-Fehler oder unklarer Befund -> KEIN Routing (fail-safe; der normale Pfad
       blockt dann ggf. ueber den Bestands-Preflight, und der Mensch entscheidet).
    Der Alt-Pfad laeuft danach durch den unveraenderten expliziten Quellen-Zweig und erbt
    damit jede Sicherung (frischer Alt-Scrape, Preflight, Verlust-Sperre, Claim)."""
    from app.services.supplier_service import parse_variant_alt
    vmap = listing.variant_source_map if listing is not None else None
    if not vmap or not sale.variant_selected or product is None or not product.aliexpress_url:
        return None
    resolved = _resolve_variant(product, sale.variant_selected, listing)
    attr = (resolved or {}).get("attr")
    if not attr or attr not in vmap:
        # Diagnose (Sale 1259): sichtbar machen, WARUM trotz Verknuepfung nicht geroutet wird.
        logger.info("alt-routing uebersprungen: Kaeufer-Variante nicht eindeutig aufloesbar "
                    "oder nicht verknuepft", extra={"sale_id": sale.id,
                                                    "resolved_attr": attr,
                                                    "verknuepft": list(vmap)[:5]})
        return None
    ref = parse_variant_alt(vmap.get(attr))
    if ref is None or not ref.get("sku_attr"):
        logger.info("alt-routing uebersprungen: Legacy-Verknuepfung ohne Ziel-Variante",
                    extra={"sale_id": sale.id, "attr": attr})
        return None                     # Legacy (ganze Quelle) -> nie automatisch routen
    if not ref.get("sku_id"):
        # Kein Drift-Anker gespeichert (z.B. Snapshot war gekappt) -> NIE automatisch
        # routen (Blindkauf-Risiko, fail-closed); der normale Pfad blockt per
        # Bestands-Preflight und der Nutzer bestellt bewusst per 💡-Quellen.
        logger.info("alt-routing uebersprungen: kein sku_id-Anker gespeichert "
                    "(Verknuepfung im Preis-Check neu setzen)",
                    extra={"sale_id": sale.id, "attr": attr})
        return None
    if str(ref["source"]) == str(product.aliexpress_id):
        return None                     # zeigt auf die Hauptquelle selbst -> sinnlos
    # FRISCHER Primaer-Scrape: OOS nur bei POSITIVEM Befund.
    try:
        scraped = await ae.scrape_product(product.aliexpress_url)
    except (OutOfStockError, ProductNotFoundError):
        return {"alt_id": ref["source"], "alt_attr": ref["sku_attr"],
                "alt_sku_id": ref.get("sku_id"), "primary_attr": attr,
                "reason": "Hauptquelle weg/ausverkauft"}
    except Exception:  # noqa: BLE001 – unklare Datenlage -> kein Routing
        return None
    need = int(sale.quantity or 1)
    fresh = ((getattr(scraped, "variants", None) or {}).get("skus") or [])
    hit = next((s for s in fresh if s.get("attr") == attr), None)
    if not getattr(scraped, "in_stock", True) and (
            not fresh or any(_stock_int(s.get("stock")) is not None for s in fresh)):
        # Produkt-Level-OOS zaehlt nur als POSITIVER Befund, wenn im frischen SKU-Satz
        # ueberhaupt ein Bestand parsebar war: parse_product setzt in_stock=False auch
        # bei fehlendem/unlesbarem Bestandsfeld (Felddrift) – das ist UNKLARE Datenlage
        # -> unten per-SKU (stock=None => kein Routing, fail-safe).
        reason = "Hauptquelle ausverkauft"
    elif fresh and hit is None:
        reason = "Variante nicht mehr in der Hauptquelle"
    else:
        stock_n = _stock_int((hit or {}).get("stock"))
        if stock_n is None or stock_n >= need:
            logger.info("alt-routing uebersprungen: Hauptquelle lieferbar/Bestand unbekannt",
                        extra={"sale_id": sale.id, "attr": attr, "stock": stock_n})
            return None                 # lieferbar oder unbekannt -> normaler Pfad
        reason = f"Primaer-Bestand {stock_n} < benoetigt {need}"
    logger.info("auto-routing to alt variant", extra={
        "sale_id": sale.id, "primary_attr": attr, "alt": ref["source"], "reason": reason})
    return {"alt_id": ref["source"], "alt_attr": ref["sku_attr"],
            "alt_sku_id": ref.get("sku_id"), "primary_attr": attr, "reason": reason}


def _ebay_net_proceeds(sale: Sale, settings, listing: Listing | None = None) -> float | None:
    """eBay-Netto = VK − Gebuehren (echte fee_eur_actual, sonst Standardrate 22%+fix).
    VK aus dem Sale (tatsaechlicher Verkaufspreis), sonst Listing-Preis als Fallback."""
    vk = float(sale.price_eur) if sale.price_eur is not None else (
        float(listing.price_eur) if (listing is not None and listing.price_eur) else None)
    if not vk:
        return None
    if getattr(sale, "fee_eur_actual", None) is not None:
        fee = float(sale.fee_eur_actual)
    else:
        from app.services import pricing as _pricing
        cat = listing.category_name if listing is not None else None
        fee = vk * _pricing.effective_fee_pct_for_listing(listing, settings=settings) + _pricing.ebay_fixed_fee(settings)
    return round(vk - fee, 2)


def _order_cost_estimate(sku_price, listing: Listing | None, settings) -> float | None:
    """Erwarteter AliExpress-Einkauf VOR dem Kauf: echter Variantenpreis (inkl.
    Versand/Zoll-Basis) wenn bekannt, sonst der kalkulierte Listing-EK."""
    from app.services import pricing as _pricing
    if sku_price is not None:
        try:
            from app.services.fast_shipping_service import variants_have_eu_warehouse as _vheu
            return round(_pricing.effective_cost(
                sku_price, settings=settings,
                local=_vheu(getattr(listing, "product", None) if listing else None)), 2)
        except (TypeError, ValueError):
            pass
    if listing is not None and listing.cost_eur:
        return float(listing.cost_eur)
    return None


def _resolve_self_stock_attr(sale, listing, product) -> str | None:
    """Attr der verkauften Variante, FALLS sie EIGENBESTAND mit Menge > 0 ist – sonst None.
    Aufloesung OHNE KI/Scrape (nur gespeicherte Produkt-Varianten), damit die Fulfillment-
    Entscheidung „selbst versenden statt bei AliExpress kaufen" schnell + deterministisch ist."""
    ss = getattr(listing, "self_stock", None) or {}
    if not ss:
        return None
    # Ganz-Listing-Eigenbestand ("__listing__", Sentinel wie in listing_match_service): gilt fuer
    # JEDEN Verkauf dieses Listings (keine Variante/Quelle) – selbst versenden, kein AliExpress-Kauf.
    _all = ss.get("__listing__")
    if isinstance(_all, dict):
        try:
            if int(_all.get("qty") or 0) > 0:
                return "__listing__"
        except (TypeError, ValueError):
            pass
    if product is None:
        return None
    skus = (getattr(product, "variants", None) or {}).get("skus") or []
    vs = sale.variant_selected if isinstance(sale.variant_selected, dict) else {}
    attr = vs.get("attr") if (vs.get("attr") in ss) else None
    if attr is None and vs:
        try:
            m = resolve_selection_against_skus(vs, skus, listing=listing,
                                               source_id=product.aliexpress_id)
        except Exception:  # noqa: BLE001
            m = None
        if m and m.get("attr") in ss:
            attr = m.get("attr")
    if attr is None and not vs and len(skus) == 1 and skus[0].get("attr") in ss:
        attr = skus[0].get("attr")
    if attr is None:
        return None
    try:
        return attr if int((ss.get(attr) or {}).get("qty") or 0) > 0 else None
    except (TypeError, ValueError):
        return None


# Bekannte AliExpress-Ablehnungscodes -> Klartext MIT Selbsthilfe-Weg (Nutzer-Vorgabe
# 16.08.: Fehlermeldungen muessen ohne Fachwissen verstaendlich sein und sagen, was der
# Nutzer SELBST tun kann). Der rohe Fehlertext bleibt als Diagnose-Anhang erhalten.
_ABLEHNUNG_KLARTEXT: list[tuple[str, str]] = [
    ("P-TRADE-SKU-UNSALEABLE",
     "Die gewaehlte Variante ist beim Verkaeufer gerade NICHT bestellbar (Variante "
     "deaktiviert oder ihr Lager ist leer) – das Produkt selbst existiert noch. "
     "Das kannst du selbst beheben: Bestellung oeffnen, eine andere Variante oder "
     "ueber 💡 eine Ausweich-Quelle waehlen und erneut auf Bestellen klicken."),
    ("P-TRADE-ITEM-UNSALEABLE",
     "Der Artikel ist beim Verkaeufer gerade nicht bestellbar (pausiert oder vom "
     "Verkauf genommen). Das kannst du selbst beheben: ueber 💡 eine Ausweich-Quelle "
     "waehlen oder den Artikel auf AliExpress pruefen und spaeter erneut bestellen."),
    ("INVENTORY_HOLD_ERROR",
     "Der Bestand beim Verkaeufer reicht fuer diese Menge nicht aus. Das kannst du "
     "selbst beheben: kleinere Menge bestellen oder ueber 💡 eine Ausweich-Quelle "
     "waehlen."),
    ("DELIVERY_ADDRESS",
     "AliExpress hat die LIEFERADRESSE nicht akzeptiert. Das kannst du selbst "
     "beheben: in der Bestellung Strasse, PLZ und Ort pruefen (Tippfehler, fehlende "
     "Hausnummer) und erneut bestellen."),
    ("PLACE_ORDER_LIMIT",
     "AliExpress bremst gerade unser Konto (zu viele Bestellversuche kurz "
     "hintereinander). Das kannst du selbst beheben: ein paar Minuten warten und die "
     "Bestellung dann erneut ausloesen."),
]


def _klartext_ablehnung(raw: str) -> str:
    """Rohen AliExpress-Ablehnungstext in eine verstaendliche Meldung uebersetzen.

    Unbekannte Codes werden unveraendert durchgereicht (dann fehlt schlicht die
    Uebersetzung – lieber roh als falsch)."""
    for code, text in _ABLEHNUNG_KLARTEXT:
        if code.lower() in (raw or "").lower():
            return f"{text}\n\n[Technische Details: {raw[:900]}]"
    return raw[:1200]


async def fulfill_sale(db: Session, *, sale_id: int, approve_variant: bool = True,
                       override_delivery_name: str | None = None,
                       source_aliexpress_id: str | None = None,
                       sku_attr: str | None = None, force: bool = False,
                       allow_alt_routing: bool = False) -> dict:
    """Step 5+6: AliExpress-Bestellung ausloesen und Tracking erfassen.

    ``source_aliexpress_id``: optional eine AUSWEICH-Quelle (Slot 2/3) statt der
    Hauptquelle bestellen – vom Nutzer im Quellen-Vergleich explizit gewaehlt.
    ``sku_attr``: optional die exakte AliExpress-Variante (aus dem Quellen-/
    Varianten-Modal) – wird gegen die Quelle validiert und am Listing gelernt.
    ``allow_alt_routing``: NUR der manuelle Bestell-Klick (Router) setzt True –
    dann darf auf die im Preis-Check verknuepfte Ausweich-VARIANTE geroutet werden,
    wenn die Primaer-Variante nachweislich ausverkauft ist. Hintergrund-Fulfillment
    (auto_fulfill_due/IPN) bleibt beim Default False: die Maschine wechselt NIE
    selbststaendig die Geldquelle.
    """
    sale = db.get(Sale, sale_id)
    if sale is None:
        raise PersistentError("Sale nicht gefunden")

    # STORNO-SPERRE: fuer stornierte/erstattete Verkaeufe wird NIE eingekauft.
    if sale.status in ("cancelled", "refunded"):
        raise PersistentError("Verkauf ist storniert/erstattet – es wird nicht bestellt.")

    # Idempotenz: existiert bereits eine Bestellung, NICHT erneut bei AliExpress
    # ordern (Schutz gegen Doppelbestellung durch Background-Task + Retry).
    existing = db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == sale_id))
    if existing is not None:
        if existing.status == "verify_needed" and not existing.aliexpress_order_id:
            raise PersistentError(
                "Bestellung mit unklarem Ausgang haengt offen – erst im AliExpress-Konto "
                "pruefen. Danach im Orders-Tab Order-ID nachtragen oder Bestellung freigeben.")
        if existing.status == "ordering" and not existing.aliexpress_order_id:
            # Verwaister Claim (Crash/CancelledError vor dem Ergebnis)? Nach Karenz
            # freigeben – die Order KANN existieren, daher zur manuellen Pruefung.
            if _claim_is_stale(existing):
                _mark_claim_uncertain(db, sale, existing, None,
                                      RuntimeError("verwaister ordering-Claim (Timeout/Crash)"))
                raise PersistentError(
                    "Vorheriger Bestellversuch haengt (App-Neustart/Abbruch). Im AliExpress-"
                    "Konto pruefen, ob die Bestellung existiert, dann Order-ID nachtragen "
                    "oder freigeben.")
            raise PersistentError("Bestellung laeuft bereits (Doppelbestellungs-Schutz).")
        # SELBSTHEILUNG: steht der Sale faelschlich noch "offen", obwohl eine echte Bestellung
        # existiert -> Status korrigieren (sonst zeigt das UI weiter „noch zu bestellen").
        if existing.aliexpress_order_id and sale.status in _OPEN_SALE_STATES:
            sale.status = "tracking" if existing.tracking_number else "ordered_aliexpress"
            db.commit()
        return {
            "sale_id": sale.id,
            "order_id": existing.id,
            "aliexpress_order_id": existing.aliexpress_order_id,
            "tracking_placeholder": existing.tracking_number or "pending",
            "status": "ordered",
        }

    # Step 4: Validierung (override_delivery_name kann Adressfehler heilen)
    problems = validate_buyer(sale)
    if problems and not override_delivery_name:
        sale.status = "needs_manual_review"
        db.commit()
        raise PersistentError("Buyer-Validierung fehlgeschlagen: " + "; ".join(problems))

    listing = db.get(Listing, sale.listing_id) if sale.listing_id else None
    product = db.get(Product, listing.product_id) if listing and listing.product_id else None

    # EIGENBESTAND-GUARD ZUERST – noch VOR dem "kein Product"-Fehler, damit er auch fuer Listings
    # OHNE AliExpress-Quelle greift (Ganz-Listing-Eigenbestand "__listing__" fuer Temu-/not_found-
    # Importe, die man selbst auf Lager hat). Ist die verkaufte Variante selbst gelagert (Menge > 0)?
    # Dann NICHT bei AliExpress bestellen (sonst Doppelkauf) – als "selbst versenden" markieren und
    # die eigene Menge um die verkaufte Stueckzahl senken.
    _ss_attr = _resolve_self_stock_attr(sale, listing, product) if listing else None
    if _ss_attr and not force:
        ss = dict(listing.self_stock or {})
        entry = dict(ss.get(_ss_attr) or {})
        try:
            have = int(entry.get("qty") or 0)
        except (TypeError, ValueError):
            have = 0
        entry["qty"] = max(0, have - int(sale.quantity or 1))
        ss[_ss_attr] = entry
        listing.self_stock = ss
        flag_modified(listing, "self_stock")
        sale.status = "self_shipped"
        db.commit()
        return {"sale_id": sale.id, "status": "self_stock",
                "self_stock_remaining": entry["qty"],
                "message": (f"Eigenbestand – selbst versenden (kein AliExpress-Kauf); "
                            f"{entry['qty']} Stueck verbleiben.")}

    # Ab hier wird bei AliExpress bestellt -> ohne Product/Quelle nicht moeglich.
    if product is None:
        raise PersistentError("Kein Product/Listing zur Sale zugeordnet")

    # GELD-GUARD: Live-Stornostatus direkt vor dem Einkauf (Fall Sale 806).
    await _abort_if_ebay_cancelled(db, sale)

    ae = get_aliexpress_client()
    delivery_name = override_delivery_name or sale.buyer_name or "Empfaenger"
    delivery_address = sale.delivery_address or {}

    with task_log(db, task_type="fulfill", reference_id=sale_id) as tl:
        # AUTO-ROUTING auf die verknuepfte Ausweich-VARIANTE (nur manueller Klick,
        # nur wenn keine explizite Quelle/Variante gewaehlt wurde; Details am Helper).
        # force ("Trotzdem bestellen") unterdrueckt das Routing ebenfalls: force skippt
        # Preflight/Verlust-Sperre – ein force-Retry nach VERLUST-STOPP soll die eben
        # gezeigte Quelle bestellen, nicht ungeschuetzt die Geldquelle wechseln.
        routed = None
        if allow_alt_routing and not force and not source_aliexpress_id and not sku_attr:
            routed = await _route_to_variant_alt_source(db, sale=sale, listing=listing,
                                                        product=product, ae=ae)
            if routed:
                source_aliexpress_id = routed["alt_id"]
                sku_attr = routed["alt_attr"]
        # Quelle bestimmen: Hauptquelle ODER explizit gewaehlter Ausweich-Slot.
        order_url = product.aliexpress_url
        src_used: str | None = None
        sku_price = None
        if source_aliexpress_id and str(source_aliexpress_id) != str(product.aliexpress_id):
            slot = _find_source_slot(product, source_aliexpress_id)
            order_url = slot["url"]
            src_used = str(source_aliexpress_id)
            try:
                scraped_src = await ae.scrape_product(order_url)
            except Exception as exc:  # noqa: BLE001
                raise PersistentError(f"Ausweich-Quelle nicht ladbar: {str(exc)[:150]}")
            skus = ((getattr(scraped_src, "variants", None) or {}).get("skus") or [])
            # Produkt-Level-OOS blockt NUR, wenn die GEWAEHLTE Variante nicht rettet:
            # parse_product setzt in_stock=False auch bei unlesbarem Bestandsfeld
            # (Felddrift). Der Nutzer klickt hier BEWUSST eine konkrete Variante
            # (Sale 1259) — deren Bestand zaehlt; endgueltig entscheidet ohnehin
            # AliExpress beim Kauf (INVENTORY_HOLD, sicher abgelehnt = nicht bestellt).
            if not getattr(scraped_src, "in_stock", True):
                chosen = (next((s for s in skus if s.get("attr") == sku_attr), None)
                          if sku_attr else None)
                chosen_stock = _stock_int((chosen or {}).get("stock"))
                if chosen is None or (chosen_stock is not None
                                      and chosen_stock < int(sale.quantity or 1)):
                    raise PersistentError(
                        "Ausweich-Quelle ist ausverkauft (gewaehlte Variante nicht "
                        "lieferbar). Andere Variante/Quelle waehlen.")
        else:
            skus = await _ensure_product_variants(db, product)

        # Variante aufloesen: expliziter sku_attr > gelernte Map/Regeln > KI (nur Hauptquelle)
        variant = None
        if sku_attr:
            sku = next((s for s in skus if s.get("attr") == sku_attr), None)
            if sku is None:
                if routed:
                    sale.status = "needs_manual_review"
                    db.commit()
                    raise PersistentError(
                        "Ausweich-Variante existiert nicht mehr in der Quelle – im "
                        "Preis-Check neu verknuepfen (🔗 Quellen).")
                raise PersistentError("Gewaehlte Variante (attr) nicht in der Quelle gefunden – "
                                      "Quelle neu laden und erneut waehlen.")
            # SKU-ID-PIN (nur Auto-Routing, FAIL-CLOSED): der attr existiert noch, koennte
            # nach einem Haendler-Edit aber ANDERE Ware bezeichnen. Die beim Verknuepfen
            # gemerkte sku_id muss zur frisch gescrapten passen – fehlt die frische ID
            # oder weicht sie ab: KEIN Kauf (geroutet wird nur mit gespeichertem Anker).
            if routed and (sku.get("id") is None
                           or str(sku.get("id")) != str(routed["alt_sku_id"])):
                sale.status = "needs_manual_review"
                db.commit()
                raise PersistentError(
                    "Ausweich-Variante nicht verifizierbar (SKU-ID fehlt oder weicht ab) – "
                    "im Preis-Check neu verknuepfen, dann erneut bestellen.")
            variant = {"attr": sku.get("attr"), "options": sku.get("options") or {},
                       "id": sku.get("id")}
            # Legacy-Key nur bei der Hauptquelle schreiben (sku_attrs sind quellen-spezifisch).
            _learn_variant(listing, src_used or product.aliexpress_id,
                           sale.variant_selected, sku_attr,
                           write_legacy=(src_used is None))
        elif approve_variant:
            if src_used is None:
                variant, _vsrc = await resolve_variant_smart(
                    db, product=product, listing=listing, variant_selected=sale.variant_selected)
            else:
                variant = resolve_selection_against_skus(
                    sale.variant_selected, skus,
                    listing=listing, source_id=src_used)

        # EINDEUTIGKEITS-FALLBACK: Produkt/Quelle hat GENAU EINE Variante -> die bestellen,
        # auch wenn der Kaeufer nichts gewaehlt hat (Einzel-Listing) oder die Aufloesung leer
        # blieb. AliExpress verlangt auch bei Ein-SKU-Produkten die sku_id; ohne sie kommt
        # 'SKU_NOT_EXIST' (Vorfall Sale 1116, Detailing-Pinsel-Set). Kein Blindkauf-Risiko,
        # da es nur EINE Variante gibt.
        variant, single_only = _single_sku_fallback(variant, skus)

        # BLINDKAUF-SPERRE 1: Quelle HAT Varianten, aber weder Regeln noch KI konnten
        # die Kaeufer-Auswahl eindeutig zuordnen -> NICHT bestellen (AliExpress wuerde
        # sonst irgendeine Default-Variante liefern).
        #
        # ``single_only`` ist der Beleg "es gibt nichts zu raten": genau EINE SKU. Dann
        # waere die Sperre eine Sackgasse - auch der Varianten-Dialog haette nichts
        # anzubieten.
        has_skus = bool(skus)
        if (has_skus and sale.variant_selected and not (variant or {}).get("attr")
                and single_only is None):
            sale.status = "needs_manual_review"
            db.commit()
            raise PersistentError(
                f"Variante nicht eindeutig zuordenbar: eBay-Auswahl {sale.variant_selected} "
                f"passt auf keine AliExpress-Variante (auch die KI ist unsicher) – im "
                f"Orders-Tab '🧩 Variante wählen' klicken (wird für dieses Listing gemerkt).")
        # BLINDKAUF-SPERRE 2 (neu): Kaeufer hat eine Variante gewaehlt, aber die Quelle
        # liefert KEINE Varianten (Abruf fehlgeschlagen oder falsches/variantenloses
        # Produkt) -> ohne sku_attr wuerde AliExpress die Default-Variante schicken.
        if sale.variant_selected and not has_skus:
            sale.status = "needs_manual_review"
            db.commit()
            raise PersistentError(
                "Kaeufer hat eine Variante gewaehlt, aber die AliExpress-Quelle liefert "
                "keine Varianten (Abruf fehlgeschlagen oder falsche Quelle) – Quelle im "
                "Preis-Check pruefen, dann erneut bestellen.")

        if (variant and (variant.get("attr") or single_only is not None)) and has_skus:
            if single_only is not None:
                hit = single_only          # variantenlos: die eine SKU, ohne attr-Vergleich
            else:
                hit = next((s for s in skus if s.get("attr") == variant["attr"]), None)
            if hit is not None and hit.get("price") is not None:
                try:
                    sku_price = float(hit["price"])
                except (TypeError, ValueError):
                    sku_price = None
            # BESTANDS-PREFLIGHT (Vorfall Sale 1124, Details am Helper). force ("Trotzdem
            # bestellen") umgeht ihn bewusst – die Bestandszahl ist ein Cache-Wert; hat der
            # Lieferant nachgefuellt, darf der Nutzer uebersteuern (ein echter Ausverkauf
            # scheitert dann sauber bei AliExpress, Claim wird zurueckgerollt).
            if not force:
                _assert_variant_stock(db, sale, skus, variant)

        # VERLUST-SPERRE (Fall HTC NE40 / Sale 808): NIE mit Verlust einkaufen.
        # eBay-Netto (VK − Gebuehren) muss den AliExpress-Einkauf decken. Sonst STOPP
        # (needs_manual_review) statt Kauf – der Nutzer sucht eine guenstigere Quelle
        # oder bestellt bewusst mit force=True ("Trotzdem bestellen").
        if not force:
            _settings = get_settings()
            # PREIS-PFLICHT (nur Auto-Routing): ohne frischen Alt-SKU-Preis wuerde die
            # Verlust-Sperre mit dem (billigeren) Primaer-EK rechnen und eine teurere
            # Alt-Bestellung durchwinken. Regel: keine Schaetzungen bei Geld.
            if routed and sku_price is None:
                sale.status = "needs_manual_review"
                db.commit()
                raise PersistentError(
                    "Ausweich-Variante ohne Preis in der Quelle – Verlust-Pruefung nicht "
                    "moeglich. Im Preis-Check Quelle neu laden/verknuepfen oder bewusst "
                    "per 💡 Quellen mit 'Trotzdem bestellen' ordern.")
            cost_now = _order_cost_estimate(sku_price, listing, _settings)
            net = _ebay_net_proceeds(sale, _settings, listing)
            if cost_now is not None and net is not None and cost_now >= net:
                sale.status = "needs_manual_review"
                db.commit()
                raise PersistentError(
                    f"⛔ VERLUST-STOPP: AliExpress-Einkauf {cost_now:.2f} € liegt bei/ueber "
                    f"dem eBay-Netto {net:.2f} € (VK {float(sale.price_eur or 0):.2f} € minus "
                    f"Gebuehren) – NICHT bestellt. Guenstigere Quelle im Preis-Check suchen "
                    f"oder bewusst 'Trotzdem bestellen'.")

        # LIEFER-CHECK (Nutzerauftrag 16.08.): liefert die GEWAEHLTE Quelle ins
        # Zielland des Kaeufers? Nur eine DEFINITIVE Absage stoppt (fail-open bei
        # unbekannt); force ("Trotzdem bestellen") uebersteuert bewusst. Die konkret
        # bestellte Varianten-SKU wird mitgeprueft (Versand ist SKU-abhaengig).
        if not force:
            from app.services import delivery_check_service
            await delivery_check_service.preflight_bestellung(
                db, sale, product_id=(src_used or product.aliexpress_id),
                sku_id=(variant or {}).get("id"),
                ist_hauptquelle=(src_used is None))

        # TOCTOU-GUARD: zwischen den Storno-Checks oben und hier lagen lange awaits
        # (scrape). Status FRISCH aus der DB lesen (Spalten-Select umgeht die Identity
        # Map), damit ein parallel eingetroffener Storno/Refund noch gesehen wird.
        fresh = db.scalar(select(Sale.status, Sale.ebay_cancel_state)
                          .where(Sale.id == sale.id))
        if fresh and fresh[0] in ("cancelled", "refunded"):
            raise PersistentError("Verkauf wurde inzwischen storniert/erstattet – kein Einkauf.")
        if fresh and fresh[1] == "IN_PROGRESS":
            raise PersistentError("Storno-Anfrage des Kaeufers offen – erst auf eBay klaeren.")

        # DOPPELBESTELLUNGS-LOCK: Claim VOR dem Geld-Call committen.
        order = _claim_order_slot(db, sale=sale, product=product,
                                  delivery_name=delivery_name,
                                  delivery_address=delivery_address,
                                  source_aliexpress_id=src_used)
        # GELD-CALL: KEIN automatisches Retry! retry_async wuerde ds.order.create nach
        # einem Timeout/5xx erneut senden, obwohl die Order beim ersten Versuch schon
        # angelegt sein kann -> Doppelbestellung. Genau EIN Versuch.
        try:
            placed = await ae.place_order(
                url=order_url,
                variant=variant,
                quantity=sale.quantity or 1,
                delivery_name=delivery_name,
                delivery_address=delivery_address,
            )
        except ProductNotFoundError as exc:
            _rollback_claim(db, order)   # sicher nicht bestellt
            images = product.images or []
            alternatives = await ae.find_alternative(images[0]) if images else []
            sale.status = "alternative_pending"
            tl.result_data = {"alternatives": alternatives}
            db.commit()
            # Grund mitgeben (z.B. PRODUCT_NOT_EXIST): so weiss der Nutzer, dass die Quelle weg ist.
            reason = str(exc)[:200].strip()
            raise PersistentError(
                "Produkt nicht (mehr) verfuegbar – Ausweich-Quelle verknuepfen oder Alternativprodukt "
                "waehlen." + (f" ({reason})" if reason and "http" not in reason else "")
            )
        except OutOfStockError:
            _rollback_claim(db, order)   # sicher nicht bestellt
            sale.status = "manual_intervention_required"
            db.commit()
            raise PersistentError("Ausverkauft – manuelle Intervention erforderlich")
        except OrderRejectedError as exc:
            _rollback_claim(db, order)   # SICHER nicht bestellt -> spaeter erneut moeglich
            sale.status = "needs_manual_review"
            db.commit()
            # 1200 statt 200 Zeichen: sonst werden Klartext-Hinweis + Diagnose-Anhaenge
            # ([Adress-Versuche], [Positionen]) abgeschnitten (Vorfall Sale 1256).
            raise PersistentError(
                f"AliExpress hat die Bestellung abgelehnt: {_klartext_ablehnung(str(exc))}")
        except BaseException as exc:  # OrderUncertainError, CancelledError, alles Unklare
            # AUSGANG UNKLAR: Order KANN existieren -> Claim NICHT loeschen, sondern als
            # 'verify_needed' behalten (blockt Doppelkauf), Sale zur manuellen Pruefung.
            _mark_claim_uncertain(db, sale, order, tl, exc)
            raise PersistentError(
                "Bestellung mit UNKLAREM Ausgang (Timeout/Netz/Serverfehler) – es wurde "
                "NICHT automatisch erneut gesendet. Bitte im AliExpress-Konto pruefen, ob "
                "die Bestellung existiert, und im Orders-Tab die Order-ID nachtragen bzw. "
                "die Bestellung freigeben.")

        # GELD IST AUSGEGEBEN: die Order-ID SOFORT persistieren (VOR jedem weiteren
        # Netz-Call). Ein Abbruch/CancelledError im Kosten-Lookup darf die bezahlte
        # Order-ID nie verlieren — sonst wirkt der Sale unbestellt (Doppelbestell-Risiko).
        order.aliexpress_order_id = placed.aliexpress_order_id
        order.status = "ordered"
        db.commit()

        # Einkaufskosten erfassen: ECHTE Order-Summe (trade.ds.order.get) hat Vorrang —
        # sie enthaelt den echten Versand + die versteckte Steuer/Gebuehr, die die
        # Schaetzung nicht kennt (Vorfall #1142: geschaetzt 8,88 €, real 11,72 €).
        # Kein offener DB-Write waehrend des Lookups (soeben committet).
        cost = placed.cost_cny if (placed.cost_cny and placed.cost_cny > 0) else None
        cost_src = "api" if cost is not None else None
        if cost is None:
            cost = await _real_order_cost_eur(ae, placed.aliexpress_order_id)
            cost_src = "api" if cost is not None else None
        if cost is None and sku_price:
            from app.services import pricing as _pricing
            # Fallback-SCHAETZUNG (Bundle: Versandaufschlag einmal, nicht je Stueck).
            from app.services.fast_shipping_service import variants_have_eu_warehouse as _vheu
            cost = Decimal(str(_pricing.effective_cost_bundle(
                sku_price, quantity=sale.quantity or 1,
                local=_vheu(getattr(listing, "product", None) if listing is not None else None))))
            cost_src = "estimate"
        if cost is None and listing is not None and listing.cost_eur:
            cost = Decimal(str(listing.cost_eur)) * (sale.quantity or 1)   # konservativer Fallback
            cost_src = "estimate"

        order.cost_cny = cost
        order.cost_source = cost_src
        db.flush()

        # Step 6: Tracking abfragen (in Produktion 24h spaeter als eigener Task)
        tracking = await ae.get_tracking(placed.aliexpress_order_id)
        order.tracking_number = tracking.tracking_number
        order.tracking_carrier = tracking.carrier
        order.estimated_delivery = tracking.estimated_delivery
        if tracking.tracking_number:
            order.status = "shipped"

        sale.status = "ordered_aliexpress"
        tl.result_data = {"order_id": order.id, "aliexpress_order_id": placed.aliexpress_order_id,
                          "source_aliexpress_id": src_used,
                          "auto_routed": bool(routed),
                          "routed_reason": (routed or {}).get("reason")}
        db.commit()

        # Automatik: AliExpress-Kaufbeleg ablegen (best effort).
        try:
            from app.services import invoice_service
            invoice_service.record_purchase_invoice(db, order_id=order.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto purchase-invoice failed",
                           extra={"order_id": order.id, "error": str(exc)})

        return {
            "sale_id": sale.id,
            "order_id": order.id,
            "aliexpress_order_id": placed.aliexpress_order_id,
            "tracking_placeholder": tracking.tracking_number or "pending",
            "status": "ordered",
            # Transparenz: wurde automatisch auf die verknuepfte Ausweich-Variante geroutet?
            "source_aliexpress_id": src_used,
            "auto_routed": bool(routed),
            "routed_from_attr": (routed or {}).get("primary_attr"),
            "routed_reason": (routed or {}).get("reason"),
        }


def _ae_order_id_still_unique(db: Session) -> bool:
    """True, wenn auf orders_aliexpress.aliexpress_order_id noch ein UNIQUE-Index liegt
    (Migration scripts/migrate_group_orders.py noch nicht gelaufen). Nur SQLite."""
    from sqlalchemy import text
    try:
        rows = db.execute(text("PRAGMA index_list('orders_aliexpress')")).fetchall()
        for r in rows:
            name, unique = r[1], r[2]
            if not unique:
                continue
            cols = db.execute(text(f"PRAGMA index_info('{name}')")).fetchall()
            if any(c[2] == "aliexpress_order_id" for c in cols):
                return True
    except Exception:  # noqa: BLE001 – z.B. Nicht-SQLite: Guard nicht anwenden
        return False
    return False


async def fulfill_ebay_order(db: Session, *, ebay_order_id: str) -> dict:
    """Mehrpositions-Bestellung: ALLE offenen Positionen einer eBay-Order zusammen
    bei AliExpress bestellen (ein Freigabe-Klick statt N Einzelbestellungen).

    * Bereits bestellte Positionen (Fall Sale 804) werden uebersprungen.
    * ALLES-ODER-NICHTS bei der Varianten-Aufloesung: ist auch nur EINE Position
      mehrdeutig, wird die ganze Gruppe NICHT bestellt (kein Teil-Blindkauf).
    * Positionen desselben Haendlers laufen in EINEM ds.order.create (eine
      AliExpress-Order); verschiedene Haendler ergeben getrennte Orders, aber
      weiterhin einen einzigen Freigabe-Schritt.
    """
    sales = db.scalars(select(Sale).where(Sale.ebay_order_id == ebay_order_id)
                       .order_by(Sale.id)).all()
    if not sales:
        raise PersistentError("Keine Verkaeufe zu dieser eBay-Bestellung gefunden")
    existing_by_sale = {
        o.sale_id: o for o in db.scalars(select(OrderAliexpress).where(
            OrderAliexpress.sale_id.in_([s.id for s in sales]))).all()
    }
    # Nur wirklich offene Positionen bestellen: bereits bestellte, stornierte,
    # erstattete und storno-angefragte Positionen werden NICHT eingekauft. Eine
    # stornierte Schwester-Position blockiert die gueltigen Positionen NICHT.
    open_sales, held = [], []
    for s in sales:
        if s.id in existing_by_sale or s.status not in ("pending", "needs_manual_review"):
            continue
        if s.ebay_cancel_state in ("CANCELED", "IN_PROGRESS"):
            held.append(s.id)   # Storno(-Anfrage) offen -> nicht mitbestellen
            continue
        open_sales.append(s)
    skipped = sorted(existing_by_sale.keys())
    if not open_sales:
        msg = "Keine offenen Positionen – bereits bestellt/abgeschlossen."
        if held:
            msg = f"Positionen {held} haben offene Stornos – erst auf eBay klaeren."
        raise PersistentError(msg)

    # MIGRATIONS-GUARD: Solange orders_aliexpress.aliexpress_order_id noch UNIQUE ist,
    # kann eine gebuendelte Order (mehrere Positionen, EINE AE-ID) NACH dem Geld-Call
    # nicht gespeichert werden -> AE-ID ginge verloren. Dann gar nicht erst bestellen.
    if _ae_order_id_still_unique(db):
        raise PersistentError(
            "Sammelbestellung nicht moeglich: Datenbank-Migration fehlt "
            "(scripts/migrate_group_orders.py ausfuehren). Einzeln bestellen geht.")

    lead = open_sales[0]
    problems = validate_buyer(lead)
    if problems:
        raise PersistentError("Buyer-Validierung fehlgeschlagen: " + "; ".join(problems))
    # GELD-GUARD: Live-Stornostatus der eBay-Order direkt vor dem Einkauf.
    await _abort_if_ebay_cancelled(db, lead)

    ae = get_aliexpress_client()
    delivery_name = lead.buyer_name or "Empfaenger"
    delivery_address = lead.delivery_address or {}

    # Phase 1 – VORBEREITEN (noch kein Geld): Quellen + Varianten aller Positionen.
    prepared: list[dict] = []
    for sale in open_sales:
        listing = db.get(Listing, sale.listing_id) if sale.listing_id else None
        product = db.get(Product, listing.product_id) if listing and listing.product_id else None
        if product is None or not product.aliexpress_url:
            raise PersistentError(f"Position #{sale.id}: keine AliExpress-Quelle – erst "
                                  f"'🔗 Quelle zuordnen', dann Gruppe bestellen.")
        skus = await _ensure_product_variants(db, product)
        variant = None
        if sale.variant_selected:
            variant, _vsrc = await resolve_variant_smart(
                db, product=product, listing=listing, variant_selected=sale.variant_selected)
            if not skus:
                sale.status = "needs_manual_review"
                db.commit()
                raise PersistentError(f"Position #{sale.id}: Quelle liefert keine Varianten "
                                      f"(Abruf fehlgeschlagen/falsche Quelle) – im Preis-Check "
                                      f"pruefen. Gruppe NICHT bestellt.")
            variant, single_only = _single_sku_fallback(variant, skus)
            if not (variant or {}).get("attr") and single_only is None:
                sale.status = "needs_manual_review"
                db.commit()
                raise PersistentError(f"Position #{sale.id}: Variante nicht eindeutig – "
                                      f"'🧩 Variante' klicken, dann Gruppe erneut bestellen. "
                                      f"Gruppe NICHT bestellt.")
        # BESTANDS-PREFLIGHT (Vorfall Sale 1124, wie fulfill_sale): Bestand der Variante
        # muss die Menge decken – sonst lehnt AliExpress die GANZE Haendler-Gruppe mit
        # DELIVERY_METHOD_NOT_EXIST ab. All-or-nothing: eine kaputte Position stoppt die
        # Gruppe VOR jedem Claim/Geld-Call. KEIN Auto-Routing in Gruppen (andere Quelle =
        # eigene AE-Order; die Gruppe hat zudem keine Verlust-Sperre) – stattdessen klarer
        # Hinweis, die Position EINZELN zu bestellen (dort greift das Routing).
        try:
            _assert_variant_stock(db, sale, skus, variant,
                                  prefix=f"Position #{sale.id} (Gruppe NICHT bestellt): ")
        except PersistentError as exc:
            from app.services.supplier_service import parse_variant_alt
            ref = parse_variant_alt((listing.variant_source_map or {})
                                    .get((variant or {}).get("attr"))) if listing else None
            if ref and ref.get("sku_attr") and ref.get("sku_id"):
                # Nur versprechen, was das Routing wirklich tut (Dict-Link MIT Anker).
                raise PersistentError(
                    f"{exc} Hinweis: Fuer diese Variante ist eine Ausweich-Variante "
                    f"verknuepft – die Position EINZELN bestellen ('🛒'), dann wird "
                    f"automatisch dort geordert; danach die Gruppe erneut.")
            if ref:
                raise PersistentError(
                    f"{exc} Hinweis: Fuer diese Variante ist eine Ausweich-QUELLE ohne "
                    f"konkrete Ziel-Variante verknuepft (kein Auto-Routing) – im "
                    f"Preis-Check ('🔗 Quellen') die Ziel-Variante waehlen, dann die "
                    f"Position einzeln bestellen.")
            raise
        prepared.append({"sale": sale, "listing": listing, "product": product,
                         "variant": variant, "skus": skus})

    # Phase 2 – je Haendler EINE AliExpress-Order (Server buendelt gleiche Stores).
    groups: dict[str, list[dict]] = {}
    for p in prepared:
        key = str(p["product"].supplier_id or f"produkt-{p['product'].id}")
        groups.setdefault(key, []).append(p)

    from app.services import pricing as _pricing
    results: list[dict] = []
    errors: list[str] = []
    ae_ids: list[str] = []
    uncertain_ids: list[str] = []
    with task_log(db, task_type="fulfill_group", reference_id=lead.id) as tl:
        for key, grp in groups.items():
            claimed: list[tuple[dict, OrderAliexpress]] = []
            for p in grp:
                try:
                    claimed.append((p, _claim_order_slot(
                        db, sale=p["sale"], product=p["product"],
                        delivery_name=delivery_name, delivery_address=delivery_address,
                        source_aliexpress_id=None)))
                except PersistentError as exc:
                    errors.append(f"#{p['sale'].id}: {exc}")
            if not claimed:
                continue
            items = [{"url": p["product"].aliexpress_url, "variant": p["variant"],
                      "quantity": p["sale"].quantity or 1} for p, _ in claimed]
            try:
                placed_list = await ae.place_order_multi(
                    items=items, delivery_name=delivery_name,
                    delivery_address=delivery_address)
            except OrderRejectedError as exc:
                # SICHER nicht bestellt -> Claims gefahrlos entfernen, Rest weiter.
                for _, claim in claimed:
                    _rollback_claim(db, claim)
                errors.append(f"Haendler-Gruppe {key} abgelehnt: {str(exc)[:150]}")
                continue
            except BaseException as exc:  # Uncertain/Timeout/CancelledError
                # AUSGANG UNKLAR: Order KANN existieren -> Claims NICHT loeschen,
                # sondern als verify_needed behalten (blockt Doppelkauf), Sale-Review.
                for p, claim in claimed:
                    _mark_claim_uncertain(db, p["sale"], claim, None, exc)
                errors.append(f"Haendler-Gruppe {key} UNKLAR (im AliExpress-Konto pruefen): "
                              f"{str(exc)[:120]}")
                continue
            # GELD IST AUSGEGEBEN: zuerst ALLE Order-IDs persistieren (VOR jedem weiteren
            # Netz-Call) — ein Abbruch im Kosten-Lookup darf keine bezahlte Order-ID
            # verlieren (sonst Doppelbestell-Risiko beim Retry).
            for placed in placed_list:
                ae_id = str(placed.get("aliexpress_order_id") or "")
                if ae_id:
                    ae_ids.append(ae_id)
                if placed.get("mapping_uncertain"):
                    uncertain_ids.extend(str(x) for x in placed.get("all_order_ids") or [])
                for idx in placed.get("item_indexes") or []:
                    p, claim = claimed[idx]
                    claim.aliexpress_order_id = ae_id
                    claim.status = "ordered"
                    p["sale"].status = "ordered_aliexpress"
                    results.append({"sale_id": p["sale"].id, "aliexpress_order_id": ae_id})
            db.commit()

            # DANACH (best-effort): echte Order-Summe holen und Kosten setzen. Deckt eine
            # AE-Order mehrere Positionen, wird proportional zur Schaetzung verteilt;
            # fehlen Schaetzungen, wird gleich verteilt (NIE 0,00-EK fuer eine Position).
            for placed in placed_list:
                ae_id = str(placed.get("aliexpress_order_id") or "")
                idxs = list(placed.get("item_indexes") or [])
                if not idxs:
                    continue
                real_total = await _real_order_cost_eur(ae, ae_id) if ae_id else None
                est_costs: dict[int, Decimal | None] = {}
                for idx in idxs:
                    p, claim = claimed[idx]
                    cost = None
                    v = p["variant"] or {}
                    if v.get("attr"):
                        hit = next((s for s in p["skus"] if s.get("attr") == v["attr"]), None)
                        if hit is not None and hit.get("price") is not None:
                            try:
                                from app.integrations.aliexpress_api import has_eu_warehouse as _heu
                                cost = Decimal(str(_pricing.effective_cost_bundle(
                                    float(hit["price"]), quantity=p["sale"].quantity or 1,
                                    local=_heu([hit.get("ship_from")]))))
                            except (TypeError, ValueError):
                                cost = None
                    if cost is None and p["listing"] is not None and p["listing"].cost_eur:
                        cost = Decimal(str(p["listing"].cost_eur)) * (p["sale"].quantity or 1)
                    est_costs[idx] = cost
                src: dict[int, str | None] = {i: ("estimate" if est_costs[i] is not None else None)
                                              for i in idxs}
                if real_total is not None:
                    if len(idxs) == 1 or not all(est_costs[i] is not None for i in idxs):
                        # eine Position ODER unvollstaendige Schaetzungen -> gleich verteilen
                        per = Decimal(str(round(float(real_total) / len(idxs), 2)))
                        for i in idxs:
                            est_costs[i] = per
                    else:
                        base = sum(est_costs[i] for i in idxs)
                        if base > 0:
                            for i in idxs:
                                est_costs[i] = Decimal(str(round(
                                    float(real_total) * float(est_costs[i] / base), 2)))
                        else:
                            per = Decimal(str(round(float(real_total) / len(idxs), 2)))
                            for i in idxs:
                                est_costs[i] = per
                    # Rundungs-Rest auf die letzte Position (Summe == echte Summe)
                    drift = real_total - sum(est_costs[i] for i in idxs)
                    est_costs[idxs[-1]] += drift
                    for i in idxs:
                        src[i] = "api"
                for idx in idxs:
                    p, claim = claimed[idx]
                    claim.cost_cny = est_costs[idx]
                    claim.cost_source = src[idx]
            db.commit()
            # Belege je Position (best effort)
            for p, claim in claimed:
                if claim.aliexpress_order_id:
                    try:
                        from app.services import invoice_service
                        invoice_service.record_purchase_invoice(db, order_id=claim.id)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("group purchase-invoice failed",
                                       extra={"order_id": claim.id, "error": str(exc)})
        if uncertain_ids:
            logger.warning("group order mapping uncertain",
                           extra={"ebay_order_id": ebay_order_id, "ids": uncertain_ids})
        tl.result_data = {"ebay_order_id": ebay_order_id, "ordered": results,
                          "skipped": skipped, "errors": errors,
                          "aliexpress_order_ids": ae_ids,
                          "mapping_uncertain_ids": uncertain_ids}
    if not results and errors:
        raise PersistentError(" | ".join(errors)[:400])
    return {"ebay_order_id": ebay_order_id, "ordered": results,
            "skipped_already_ordered": skipped, "aliexpress_order_ids": ae_ids,
            "errors": errors, "groups": len(groups),
            "mapping_uncertain_ids": uncertain_ids}


async def refresh_tracking(db: Session, *, order_id: int) -> dict:
    """GET .../orders/{order_id}/tracking – Live-Tracking abrufen/aktualisieren."""
    order = db.get(OrderAliexpress, order_id)
    if order is None:
        raise PersistentError("Order nicht gefunden")
    ae = get_aliexpress_client()
    tracking = await ae.get_tracking(order.aliexpress_order_id or "")
    num = tracking.tracking_number
    # DE-Lieferungen: NUR eine FINALE Zusteller-Nummer (DHL/Hermes) hinterlegen.
    # Provisorische AliExpress/Cainiao-Nummern werden NICHT gespeichert, und eine
    # vorhandene finale Nummer wird nie mit None/provisorisch ueberschrieben
    # (Nutzerregel 2026-07-05 — die DHL-Nummer folgt dort sicher).
    if _is_final_tracking(num):
        order.tracking_number = num
        order.tracking_carrier = carrier_from_tracking(num) or tracking.carrier
    else:
        # AUSLAND (Vorfall Sales 1266/1274/1277, 19.08.): dort folgt oft NIE eine
        # deutsche Endzusteller-Nummer — die Cainiao-/Standard-Nummer ist die
        # einzige trackbare und wird als beste verfuegbare gespeichert.
        sale = db.get(Sale, order.sale_id) if order.sale_id else None
        if num and _sale_ausland(sale):
            if not order.tracking_number:
                order.tracking_number = num
                order.tracking_carrier = (carrier_from_tracking(num) or tracking.carrier
                                          or "AliExpress Standard")
            elif (num != order.tracking_number
                  and not _is_final_tracking(order.tracking_number)
                  and not _wirkt_cainiao(tracking.carrier)):
                # ZUSTELLER-UEBERGABE (Nutzer-Hinweis 19.08.: AT-Pakete laufen final
                # ueber Oesterreichische Post/Express One): meldet AliExpress spaeter
                # eine NEUE Nummer mit lokalem Carrier-Namen, ist das die Endzusteller-
                # Nummer -> upgraden. Eine finale Nummer wird nie verschlechtert.
                order.tracking_number = num
                order.tracking_carrier = (carrier_from_tracking(num) or tracking.carrier
                                          or order.tracking_carrier)
    order.estimated_delivery = tracking.estimated_delivery
    db.commit()
    return {
        "order_id": order.id,
        "tracking_number": order.tracking_number,
        "carrier": order.tracking_carrier,
        "status": tracking.status,
        "last_update": order.updated_at,
        "estimated_delivery": tracking.estimated_delivery,
    }


# --------------------------------------------------------------------------
# Auto-Fulfillment: eBay-Bestellungen holen + Tracking an eBay zurueckmelden
# --------------------------------------------------------------------------
def _real_ebay():
    """Echter eBay-Client (unabhaengig von MOCK_EBAY) – wie bei golive."""
    from app.config import get_settings
    from app.integrations.ebay import RealEbayClient
    return RealEbayClient(get_settings())


def _norm_opt(value) -> str:
    """Optionswert normalisieren: 'Spain' == 'spain', '14X135Cm' ~ '14x135cm'."""
    import re
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


# Listings werden mit DEUTSCHEN Varianten-Namen erstellt (LLM uebersetzt), die
# AliExpress-SKUs bleiben englisch -> beim Bestellen zurueck-uebersetzen.
_VARIANT_DE_EN = {
    "schwarz": "black", "weiss": "white", "weis": "white", "rot": "red",
    "blau": "blue", "gruen": "green", "grun": "green", "gelb": "yellow",
    "grau": "gray", "grey": "gray",   # brit./amerik. Schreibweise vereinheitlichen
    "silber": "silver", "golden": "gold", "rosa": "pink",
    "lila": "purple", "violett": "purple", "braun": "brown", "beige": "beige",
    "tuerkis": "cyan", "turkis": "cyan", "orange": "orange", "bunt": "multicolor",
    "durchsichtig": "clear", "transparent": "clear", "klein": "small",
    "mittel": "medium", "gross": "large", "laenge": "length", "groesse": "size",
    "ki": "ai",   # "Schwarz KI" -> "black ai" (Uebersetzer-Kopfhoerer u.ae.)
}


def _translate_norm(value) -> str:
    """Normalisieren UND deutsche Varianten-Woerter ins Englische uebersetzen."""
    import re
    words = re.findall(r"[a-zäöüß]+|\d+", str(value or "").lower())
    out = []
    for w in words:
        w = (w.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
             .replace("ß", "ss"))
        out.append(_VARIANT_DE_EN.get(w, w))
    return "".join(out)


# Interne Aufloesungs-Felder, die NICHT zum eBay-Auswahl-Schluessel gehoeren.
# ``variant_locked`` markiert eine vom MENSCHEN bestaetigte Zuordnung (set_sale_variant) – die
# Auto-Korrektur beim Sync fasst sie NIE an.
_RESOLVED_KEYS = ("attr", "options", "id", "ebay_sku", "variant_locked")


def _attr_from_ebay_variation_sku(listing, product, ebay_sku,
                                  variant_selected: dict | None = None) -> dict | None:
    """DETERMINISTISCH statt Raten: unsere Publish-SKU ``{base}-V{i}`` kodiert die POSITION der
    AliExpress-Variante (V1..Vn == _usable_variants[0..n-1]). Wir haben das Listing selbst
    veroeffentlicht, also ist die Zuordnung eindeutig. Gibt {attr, options, id} zurueck oder None
    (fremde/klassische Listings mit anderem SKU-Schema -> Fallback auf Score-Matching).

    GELD-SCHUTZ: Fuer UNSER Listing ist die Position ({base}-V{i}) die Wahrheit. Wird ein
    ``variant_selected``-Dict uebergeben, wird die Position NUR verworfen, wenn die Kaeuferwerte sie
    STRIKT widerlegen – d.h. ein anderer SKU trifft die Kaeuferauswahl klar besser (verschobene/
    getauschte Variante). Bei nichtssagenden/gleichen AliExpress-Namen (kein Wert-Signal, Fall
    #1161) GILT die Position. ``variant_selected=None`` liefert die reine Positions-Decodierung."""
    if not (ebay_sku and product is not None and listing is not None):
        return None
    base = getattr(listing, "ebay_sku", None) or f"AE-{listing.id}"
    m = re.match(rf"^{re.escape(base)}-V(\d+)$", str(ebay_sku))
    if not m:
        return None
    from app.services.golive_service import _usable_variants
    _, uvars = _usable_variants(product)
    i = int(m.group(1)) - 1
    if not (0 <= i < len(uvars)):
        return None
    v = uvars[i]
    cand = {"attr": v.get("attr"), "options": v.get("options") or {}, "id": v.get("id")}
    if variant_selected is not None and _position_contradicted(variant_selected, uvars, cand["attr"]):
        return None      # Kaeuferwerte treffen eine ANDERE Variante klar besser -> Position verworfen
    return cand


def variant_map_key(variant_selected: dict) -> str:
    """Stabiler Schluessel der eBay-Auswahl fuer listing.variant_map.

    Ignoriert die internen Aufloesungs-Felder (attr/options/id), damit der
    Schluessel gleich bleibt, egal ob die Auswahl schon aufgeloest wurde – so
    laesst sich eine gelernte Zuordnung spaeter ueber denselben Key korrigieren.
    """
    return "|".join(sorted(
        _norm_opt(v) for k, v in (variant_selected or {}).items()
        if k not in _RESOLVED_KEYS and _norm_opt(v)))


def _sku_hit(sku: dict) -> dict:
    return {"attr": sku.get("attr"), "options": sku.get("options") or {},
            "id": sku.get("id")}


def _vmap_lookup(listing: Listing | None, source_id, variant_selected) -> str | None:
    """Gelernte Zuordnung lesen – QUELLEN-SPEZIFISCH (sku_attrs gelten nur je Quelle!).

    Neue Keys sind mit der AliExpress-Produkt-ID der Quelle genamespaced
    ("{aliexpress_id}|{auswahl}"); Alt-Eintraege ohne Namespace bleiben als
    Fallback lesbar (wurden gegen die damalige Hauptquelle gelernt).
    """
    if listing is None:
        return None
    vmap = listing.variant_map or {}
    key = variant_map_key(variant_selected)
    if not key:
        return None
    if source_id:
        hit = vmap.get(f"{source_id}|{key}")
        if hit:
            return hit
    return vmap.get(key)


def _learn_variant(listing: Listing | None, source_id, variant_selected, attr: str,
                   *, write_legacy: bool = True) -> bool:
    """Zuordnung am Listing lernen: quellen-genamespaced + optional Legacy-Key.

    GELD-SICHERHEIT: Der Legacy-Key (ohne Quellen-Namespace) wird von der Hauptquelle
    gelesen. Beim Lernen fuer eine AUSWEICH-Quelle darf er NICHT geschrieben werden –
    sonst wuerde ein Ausweich-sku_attr die Hauptquellen-Aufloesung vergiften
    (Mehrdeutigkeits-Sperre ausgehebelt -> falsche Ware). Aufrufer committet.
    """
    if listing is None:
        return False
    pairs = {k: v for k, v in (variant_selected or {}).items()
             if k not in _RESOLVED_KEYS} if isinstance(variant_selected, dict) else {}
    key = variant_map_key(pairs or variant_selected or {})
    if not key:
        return False
    vmap = dict(listing.variant_map or {})
    if source_id:
        vmap[f"{source_id}|{key}"] = attr
    if write_legacy:
        vmap[key] = attr      # setzt ODER ueberschreibt (Korrektur/Legacy-Kompatibilitaet)
    listing.variant_map = vmap
    return True


def _sel_pairs(variant_selected: dict) -> list:
    """eBay-Kaeuferwerte als (normalisiert, uebersetzt)-Paare – ohne interne Aufloesungs-Felder."""
    return [(_norm_opt(v), _translate_norm(v))
            for k, v in (variant_selected or {}).items()
            if k not in _RESOLVED_KEYS and _norm_opt(v)]


def _value_overlap_score(sel_pairs: list, sku: dict) -> int:
    """Wert-Ueberlappung eines SKU mit der eBay-Auswahl (exakt=3 > Teiltreffer=1, je Optionswert
    hoechstens ein Treffer). Gemeinsame Basis fuer Score-Matching UND Konsistenzpruefung."""
    score = 0
    for val in (sku.get("options") or {}).values():
        nv, tv = _norm_opt(val), _translate_norm(val)
        for sn, st in sel_pairs:
            if nv == sn or tv == st:
                score += 3
                break
            if nv in sn or sn in nv or tv in st or st in tv:
                score += 1
                break
    return score


def _variant_consistent_with_buyer(variant_selected: dict, skus: list, candidate_attr) -> bool:
    """Stuetzen die KAEUFERWERTE einen Kandidaten POSITIV? Geld-Schutz gegen falsche Ware.

    Die vom Kaeufer auf eBay gewaehlten Achsenwerte sind die Wahrheit (sie stehen in
    ``variant_selected`` neben dem aufgeloesten attr). Der Kandidat gilt nur als vertrauenswuerdig,
    wenn die Kaeuferwerte ihn TRAGEN und ihn kein anderer SKU schlaegt:
    - ``cand_score > 0``: irgendein Kaeuferwert passt auf den Kandidaten. FEHLT jedes Signal
      (alle 0), ist das genau die Signatur einer Umstellung – der gewaehlte Wert/das Vokabular ist
      aus der Quelle verschwunden (geloeschte Variante, Hauptquellen-Swap auf fremdes Vokabular).
      Dann NICHT die Position blind trauen, sondern sauber zurueckfallen (manuelle Zuordnung).
    - ``cand_score >= best_other``: kein ANDERER SKU passt besser. Gleichstand darf die Position /
      eine gelernte Zuordnung aufloesen (Nutzerwunsch: mehrdeutige Werte deterministisch waehlen),
      aber ein streng besserer Konkurrent verrät eine falsche/verschobene Zuordnung.
    Ohne Kaeuferwerte (leeres ``sp``) gibt es NICHTS zum Gegenpruefen -> nicht blind baken.
    """
    if not (isinstance(variant_selected, dict) and skus and candidate_attr):
        return False
    sp = _sel_pairs(variant_selected)
    if not sp:
        return False   # keine Kaeuferwerte zum Gegenpruefen -> nicht blind trauen
    cand = next((s for s in skus if s.get("attr") == candidate_attr), None)
    if cand is None:
        return False
    cand_score = _value_overlap_score(sp, cand)
    if cand_score == 0:
        return False   # Kaeuferwerte stuetzen den Kandidaten GAR NICHT (Vokabular weg -> Umstellung)
    best_other = max((_value_overlap_score(sp, s) for s in skus
                      if s.get("attr") != candidate_attr), default=0)
    return cand_score >= best_other


def _position_contradicted(variant_selected: dict, skus: list, candidate_attr) -> bool:
    """Widerlegen die Kaeuferwerte die POSITION? Nur True, wenn ein ANDERER SKU die Kaeuferwerte
    STRIKT besser trifft (dann zeigt die Publish-Position auf falsche Ware – verschobene/getauschte
    Variante). Fehlt jedes Wert-Signal (nichtssagende/gleiche AliExpress-Namen, Fall #1161) ODER
    passt der Kandidat am besten, gilt die Position: wir haben das Listing selbst veroeffentlicht,
    die Position ({base}-V{i}) ist fuer UNSER Listing die Wahrheit. (Schwaecher als
    _variant_consistent_with_buyer: dort ist 0-Signal misstrauisch, hier vertraut die Position –
    Nutzer-Entscheidung 15.07., er verifiziert vor dem Kauf.)"""
    if not (isinstance(variant_selected, dict) and skus and candidate_attr):
        return False
    sp = _sel_pairs(variant_selected)
    if not sp:
        return False
    cand = next((s for s in skus if s.get("attr") == candidate_attr), None)
    if cand is None:
        return False
    cand_score = _value_overlap_score(sp, cand)
    best_other = max((_value_overlap_score(sp, s) for s in skus
                      if s.get("attr") != candidate_attr), default=0)
    return best_other > cand_score


def _options_value_set(options) -> set:
    """Normalisierte Optionswerte (achsen-unabhaengig) – Anker fuer den Integritaets-Vergleich."""
    return {_norm_opt(v) for v in (options or {}).values() if _norm_opt(v)}


def _baked_attr_intact(variant_selected: dict, skus: list) -> bool:
    """Ist der beim Import GEBACKENE attr in DIESER Quelle noch dieselbe Ware? Geld-Integritaets-Anker.

    Der Bake traegt einen Options-Snapshot (``variant_selected["options"]``). Wie die SKU-ID-Pin
    beim Auto-Routing gilt der attr nur, wenn die Quelle denselben attr MIT denselben Optionswerten
    fuehrt. Das faengt zuverlaessig ab, ohne den deterministischen Gleichstands-Bruch zu verlieren:
    - Hauptquellen-Swap / geloeschte / verschobene Variante NACH dem Import (attr fehlt oder die
      Optionswerte weichen ab),
    - Ausweichquellen-Kollision (gleicher attr-String, andere Ware) – die Optionswerte weichen ab.
    Ohne Snapshot (Alt-/Legacy-Bake ohne options) faellt die Pruefung auf die Kaeuferwert-Konsistenz
    zurueck (attr-Existenz allein genuegt NICHT)."""
    if not (isinstance(variant_selected, dict) and skus):
        return False
    attr = variant_selected.get("attr")
    if not attr:
        return False
    cur = next((s for s in skus if s.get("attr") == attr), None)
    if cur is None:
        return False
    baked = _options_value_set(variant_selected.get("options"))
    if not baked:
        return _variant_consistent_with_buyer(variant_selected, skus, attr)
    return baked == _options_value_set(cur.get("options"))


def _match_selection_to_skus(variant_selected: dict, skus: list) -> dict | None:
    """Score-Matching der eBay-Auswahl gegen eine SKU-Liste (exakt=3 > Teiltreffer=1).

    Mit MEHRDEUTIGKEITS-SPERRE: Steckt der Siegerwert im Namen einer anderen Variante
    ("Black" in "Black AI"), wird NICHT geraten -> None (manuelle Zuordnung).
    """
    sel_pairs = _sel_pairs(variant_selected)
    best, best_score, tie = None, 0, False
    for idx, sku in enumerate(skus):
        score = _value_overlap_score(sel_pairs, sku)
        if score > best_score:
            best, best_score, tie = idx, score, False
        elif score == best_score and score > 0:
            tie = True
    if best is None or best_score == 0 or tie:
        return None
    if _attr_is_prefix_ambiguous(skus, skus[best].get("attr")):
        return None  # mehrdeutig -> manuelle Zuordnung
    return _sku_hit(skus[best])


def resolve_selection_against_skus(variant_selected, skus: list, *,
                                   listing: Listing | None = None,
                                   source_id=None) -> dict | None:
    """eBay-Auswahl gegen die SKUs einer BELIEBIGEN Quelle (Slot 2/3) aufloesen.

    Nur gelernte Map (quellen-genamespaced!) + Regel-Matching – bewusst OHNE KI
    und OHNE Legacy-Fallback fremder Quellen: ein fuer Quelle A gelernter sku_attr
    darf nie blind auf Quelle B angewandt werden (falsche Ware).
    Rueckgabe: aufgeloeste Variante mit attr ODER None (-> Mensch ordnet zu).
    """
    if not (isinstance(variant_selected, dict) and variant_selected and skus):
        return None
    # Bereits DETERMINISTISCH aufgeloest (beim Import aus unserer Publish-SKU '{base}-V{i}'):
    # den gebackenen attr nur nehmen, wenn er in DIESER Quelle unveraendert existiert (gleicher
    # attr + gleiche Optionswerte, Integritaets-Anker). Fuer die Hauptquelle greift das direkt;
    # fuer Ausweichquellen scheitert es sauber (fremde Ware / ZUFAELLIGE attr-Kollision "14:2"
    # existiert dort, meint aber anderes) -> Fallback auf variant_map/Score-Matching.
    if variant_selected.get("attr") and _baked_attr_intact(variant_selected, skus):
        return _sku_hit(next(s for s in skus if s.get("attr") == variant_selected["attr"]))
    # Gelernte (quellen-genamespaced) Zuordnung: der Mensch/die KI hat DIESE eBay-Auswahl bewusst
    # auf diesen attr gelegt – auch wenn die Optionstexte nicht ueberlappen. Die attr-Existenz in
    # der Quelle ist die Sperre; ein Quellen-Swap wird ueber _invalidate_variant_links gepflegt.
    key = variant_map_key(variant_selected)
    if listing is not None and source_id and key:
        mapped = (listing.variant_map or {}).get(f"{source_id}|{key}")
        if mapped:
            for sku in skus:
                if sku.get("attr") == mapped:
                    return _sku_hit(sku)
    return _match_selection_to_skus(variant_selected, skus)


def _resolve_variant(product: Product | None, variant_selected,
                     listing: Listing | None = None) -> dict | None:
    """eBay-Variantenauswahl ({Achse: Wert}) auf die AliExpress-Variante (sku_attr) abbilden.

    Aufloesungs-Reihenfolge ("das System denkt mit"):
    1. Gelernte Zuordnung des Listings (variant_map) — vom Nutzer einmal bestaetigt.
    2. Score-Matching, normalisiert UND uebersetzt (Schwarz==Black, Weiss==White, KI==AI).
       Exakter Treffer zaehlt mehr als Teil-Treffer.
    3. MEHRDEUTIGKEITS-SPERRE: Steckt der Siegerwert im Namen einer anderen Variante
       ("Black" in "Black AI"), wird NICHT geraten -> manuelle Zuordnung (einmalig),
       sonst wuerde ggf. die falsche Produktversion bestellt.
    """
    if not variant_selected:
        return None
    skus = ((product.variants or {}).get("skus") or []) if product else []
    if isinstance(variant_selected, dict) and variant_selected.get("attr"):
        # Bereits aufgeloest (Import-Bake ODER manuell). Direkt nur weiterverwenden, wenn die
        # Variante in der AKTUELLEN Quelle unveraendert existiert (Integritaets-Anker: gleicher attr
        # + gleiche Optionswerte). Ein Hauptquellen-Swap / Variantenloeschen NACH dem Import darf
        # keine falsche Ware bestellen; menschlich bestaetigte Zuordnungen kehren sicher ueber die
        # gelernte variant_map (Schritt 1) zurueck.
        if not skus or _baked_attr_intact(variant_selected, skus):
            return variant_selected
        variant_selected = {k: v for k, v in variant_selected.items()
                            if k not in _RESOLVED_KEYS}     # veraltet -> unten neu aufloesen
    if not isinstance(variant_selected, dict) or not skus:
        return variant_selected if isinstance(variant_selected, dict) else None

    # 1) Gelernte Zuordnung des Listings (quellen-genamespaced, Legacy-Fallback): bewusst gesetzt,
    #    gilt auch ohne Wert-Ueberlappung; die attr-Existenz in der Quelle ist die Sperre.
    mapped_attr = _vmap_lookup(listing, product.aliexpress_id if product else None,
                               variant_selected)
    if mapped_attr:
        for sku in skus:
            if sku.get("attr") == mapped_attr:
                return _sku_hit(sku)

    # 2+3) Score-Matching mit Uebersetzung + Mehrdeutigkeits-Sperre
    hit = _match_selection_to_skus(variant_selected, skus)
    if hit is None:
        return variant_selected  # kein/mehrdeutiger Treffer -> manuelle Zuordnung
    return hit


def _attr_is_prefix_ambiguous(skus: list, chosen_attr) -> bool:
    """True, wenn der Optionswert der gewaehlten Variante Praefix/Teil des Wertes
    einer ANDEREN Variante ist (z.B. "Black" bei existierendem "Black AI").

    Solche Faelle darf WEDER die Regel-Ebene NOCH die KI automatisch entscheiden –
    der Kaeufer koennte die Basis- ODER die Zusatz-Version meinen (geldwirksam:
    falsche Ware). -> immer manuelle Zuordnung durch den Menschen.
    """
    chosen = next((s for s in (skus or []) if s.get("attr") == chosen_attr), None)
    if chosen is None:
        return False
    chosen_vals = [_translate_norm(v) for v in (chosen.get("options") or {}).values()]
    for sku in skus:
        if sku.get("attr") == chosen_attr:
            continue
        for val in (sku.get("options") or {}).values():
            other = _translate_norm(val)
            for cv in chosen_vals:
                if cv and other and cv != other and cv in other:
                    return True
    return False


async def variant_options(db: Session, *, sale_id: int) -> dict:
    """Alle AliExpress-Varianten zum Sale-Listing (fuer die manuelle Zuordnung im UI).

    Ist die variants-Spalte leer (Bildsuche-Auto-Match hat nie gescrapt), werden
    die Varianten LIVE von AliExpress nachgeladen und persistiert – vorher war
    das Modal fuer solche Produkte leer ("Keine AliExpress-Varianten am Produkt").
    """
    sale = db.get(Sale, sale_id)
    if sale is None:
        raise PersistentError("Sale nicht gefunden")
    listing = db.get(Listing, sale.listing_id) if sale.listing_id else None
    product = db.get(Product, listing.product_id) if listing and listing.product_id else None
    had_skus = bool(((product.variants or {}).get("skus") or []) if product else [])
    skus = await _ensure_product_variants(db, product) if product else []
    note = None
    refreshed = bool(skus) and not had_skus
    if product is not None and not skus:
        note = ("Varianten-Abruf fehlgeschlagen oder Produkt hat keine Varianten – "
                "Quelle im Preis-Check pruefen (🔗 Quellen) und erneut versuchen.")
    # Warnhinweis bei unbestaetigtem Bildsuche-Match: Zuordnung koennte auf dem
    # FALSCHEN Produkt gelernt werden (Fall NE39/Produkt 206).
    alt = (product.alternatives if product is not None
           and isinstance(product.alternatives, dict) else {}) or {}
    suspect_source = (str(alt.get("matched") or "").startswith("image")
                      and not alt.get("confirmed"))
    # Beste VORWAHL fuer die manuelle Zuordnung (wie im "🔗 Quellen"-Dialog): zuerst die in DIESER
    # Quelle intakte Auflösung (Bake/Score); fehlt sie, FUELLT die deterministische Position aus
    # unserer Publish-SKU ({base}-V{i}) die Luecke – gerade bei nichtssagenden/gleichen AliExpress-
    # Namen (Wert-Matching unmoeglich, Fall #1161/#1159). Nur Vorschlag – der Nutzer bestaetigt per
    # Klick; die Position ueberschreibt NIE eine bereits verifizierte Auflösung.
    vs = sale.variant_selected if isinstance(sale.variant_selected, dict) else {}
    # Deterministische Position aus unserer Publish-SKU ({base}-V{i}) – IMMER berechnen: als Vorwahl
    # bei Luecke UND als KORREKTUR-Hinweis, falls eine bestehende (evtl. versehentlich falsche)
    # Zuordnung davon abweicht. Braucht die am Sale gespeicherte ebay_sku.
    position_attr = None
    if vs and skus:
        _eb = vs.get("ebay_sku")
        _det = _attr_from_ebay_variation_sku(listing, product, _eb) if _eb else None
        _pa = (_det or {}).get("attr")
        if _pa and any(s.get("attr") == _pa for s in skus):
            position_attr = _pa
    suggested_attr = None
    suggested_position = False
    if vs and skus:
        _r = resolve_selection_against_skus(
            vs, skus, listing=listing,
            source_id=(product.aliexpress_id if product else None))
        suggested_attr = (_r or {}).get("attr")
        # Luecke (keine verifizierte Auflösung) -> Position fuellt sie (ueberschreibt nie eine
        # verifizierte Auflösung).
        if not suggested_attr and position_attr:
            suggested_attr, suggested_position = position_attr, True
    # KORREKTUR-Anker: die Position NUR als Korrektur-Hinweis anbieten, wenn die aktuell markierte
    # Variante NICHT durch die Kaeuferwerte gedeckt ist. Ist der Kaeuferwert eindeutig (echter Wert-
    # Match), bleibt es dabei – sonst wuerde ein Positions-Drift (delete_variant/Quellen-Swap) eine
    # korrekte, wertverifizierte Zuordnung faelschlich anzweifeln (Geld-Schutz).
    _hi = suggested_attr or vs.get("attr")
    _verified = bool(_hi and _variant_consistent_with_buyer(vs, skus, _hi))
    correction_attr = (position_attr if (position_attr and position_attr != _hi and not _verified
                                         and not vs.get("variant_locked")) else None)
    # Alle hinterlegten Quellen-Slots (ohne Netz-Call, nur der gespeicherte Stand). Das UI
    # blendet damit im Varianten-Dialog eine Quellen-Umschaltung ein: die Varianten unten
    # kommen aus der HAUPTQUELLE — eine Ausweich-Quelle kann guenstiger sein. Slot 0 = primaer.
    _alt_all = (product.alternatives if product is not None
                and isinstance(product.alternatives, dict) else {}) or {}
    sources = [{
        "aliexpress_id": str(s.get("aliexpress_id") or ""),
        "title": (s.get("title") or "")[:80],
        "url": s.get("url"),
        "price_eur": s.get("price_eur"),
        "in_stock": s.get("in_stock"),
        "is_primary": i == 0,
    } for i, s in enumerate(_alt_all.get("sources") or []) if s.get("aliexpress_id")]
    return {
        "sale_id": sale_id,
        "listing_id": sale.listing_id,
        "sources": sources,
        "selected": sale.variant_selected,
        "suggested_attr": suggested_attr,
        "suggested_position": suggested_position,
        "correction_attr": correction_attr,
        "ali_url": product.aliexpress_url if product else None,
        "refreshed": refreshed,
        "note": note,
        "source_unconfirmed": suspect_source,
        "options": [{
            "attr": s.get("attr"),
            "name": " · ".join(str(v) for v in (s.get("options") or {}).values()) or s.get("attr"),
            "price": s.get("price"),
            "stock": s.get("stock"),
            "image": s.get("image"),   # Varianten-Bild (entscheidend bei "as picture")
        } for s in skus if s.get("attr")],
    }


def set_sale_variant(db: Session, *, sale_id: int, attr: str) -> dict:
    """Nutzer ordnet die eBay-Auswahl einer AliExpress-Variante zu — MIT Lerneffekt:
    Die Zuordnung wird am Listing gespeichert (variant_map), damit sich alle
    kuenftigen Sales dieses Listings mit derselben Auswahl automatisch aufloesen."""
    sale = db.get(Sale, sale_id)
    if sale is None:
        raise PersistentError("Sale nicht gefunden")
    listing = db.get(Listing, sale.listing_id) if sale.listing_id else None
    product = db.get(Product, listing.product_id) if listing and listing.product_id else None
    sku = next((s for s in ((product.variants or {}).get("skus") or [])
                if s.get("attr") == attr), None) if product else None
    if sku is None:
        raise PersistentError("Variante (attr) nicht im AliExpress-Produkt gefunden")

    # eBay-Achsen-Paare bewahren (nur die internen Aufloesungs-Felder ersetzen),
    # damit der variant_map-Schluessel stabil bleibt und eine falsche Zuordnung
    # spaeter ueber DENSELBEN Knopf KORRIGIERT (ueberschrieben) werden kann.
    # Gelernt wird quellen-genamespaced (sku_attrs gelten nur je Quelle).
    ebay_pairs = {k: v for k, v in (sale.variant_selected or {}).items()
                  if k not in _RESOLVED_KEYS} if isinstance(sale.variant_selected, dict) else {}
    learned = _learn_variant(listing, product.aliexpress_id if product else None,
                             ebay_pairs or sale.variant_selected or {}, attr)

    # Die eBay-Publish-SKU BEWAHREN (interne Metadaten, NICHT Teil des Map-Schluessels): sie wird
    # fuer die positions-basierte Vorwahl/Korrektur gebraucht – bisher ging sie beim Zuordnen
    # verloren, wodurch die Positions-Logik danach nie mehr griff.
    _eb = sale.variant_selected.get("ebay_sku") if isinstance(sale.variant_selected, dict) else None
    # variant_locked=True: der Mensch hat bewusst zugeordnet -> die Auto-Korrektur beim Sync
    # ueberschreibt das NIE (sonst wuerde der Scheduler die Korrektur alle 5 min zurueckdrehen).
    sale.variant_selected = {**ebay_pairs, **({"ebay_sku": _eb} if _eb else {}), "attr": attr,
                             "options": sku.get("options") or {}, "id": sku.get("id"),
                             "variant_locked": True}
    if sale.status == "needs_manual_review":
        sale.status = "pending"
    db.commit()
    return {"sale_id": sale_id, "attr": attr,
            "options": sku.get("options") or {}, "learned_for_listing": learned}


async def backfill_real_order_costs(db: Session, *, only_estimated: bool = True,
                                    limit: int = 2000) -> dict:
    """ECHTE Order-Summen (trade.ds.order.get) fuer bestehende Bestellungen nachziehen.

    Hintergrund: ds.order.create liefert keine Betraege -> historisch wurde die
    SCHAETZUNG als EK gespeichert (ohne echten Versand + versteckte Steuer) -> Gewinne
    systematisch zu optimistisch. Diese Funktion holt je Order die reale Summe und
    korrigiert ``cost_cny``. Schutzregeln (Review 13.07.):
    - ``only_estimated`` (Default): Werte mit cost_source manual/receipt/bank
      (Nutzer-Korrektur, Beleg, Konto-Abbuchung) werden NIE ueberschrieben.
    - Storno/Erstattung: Sales mit Status cancelled/refunded werden uebersprungen (die
      Order-Summe waere dort kein echter Netto-EK).
    - Nur Einzel-Position-Orders (eine Order = ein Sale); geteilte werden gemeldet.
    - Status-Whitelist via _real_order_cost_eur (nie bezahlte/stornierte AE-Orders -> None).

    SQLite-sicher: Liste zuerst lesen, je Order Netz-Call OHNE offene Transaktion,
    dann kurzer Write+Commit."""
    ae = get_aliexpress_client()
    rows = db.scalars(select(OrderAliexpress).where(
        OrderAliexpress.aliexpress_order_id.isnot(None)).order_by(OrderAliexpress.id)).all()
    # Orders je AE-ID gruppieren (geteilte Orders erkennen)
    by_ae: dict[str, list] = {}
    for o in rows:
        by_ae.setdefault(str(o.aliexpress_order_id), []).append(o)
    updated = skipped_shared = skipped_protected = skipped_void = unchanged = failed = 0
    changes = []
    _void = {"cancelled", "refunded", "canceled", "storniert"}
    for ae_id, group in list(by_ae.items())[:max(1, int(limit))]:
        if len(group) > 1:
            skipped_shared += 1
            continue
        o = group[0]
        # "bank" ist der TATSAECHLICH abgebuchte Betrag – die staerkste Quelle,
        # noch vor dem Beleg (bei Fremdwaehrung weist der Beleg USD aus).
        if only_estimated and (o.cost_source or "") in ("manual", "receipt", "bank"):
            skipped_protected += 1
            continue
        sale = db.get(Sale, o.sale_id) if o.sale_id else None
        if sale is not None and (sale.status or "") in _void:
            skipped_void += 1
            continue
        await asyncio.sleep(0.4)   # API-Rate-Limit schonen (JEDE Iteration mit Call)
        real = await _real_order_cost_eur(ae, ae_id)
        if real is None:
            failed += 1
            continue
        old = float(o.cost_cny or 0)
        if abs(old - float(real)) < 0.02:
            if not o.cost_source:
                o.cost_source = "api"      # Wert bestaetigt -> als API-verifiziert markieren
                db.commit()
            unchanged += 1
            continue
        o.cost_cny = real
        o.cost_source = "api"
        db.commit()
        updated += 1
        changes.append({"sale_id": o.sale_id, "old": old, "new": float(real)})
    logger.info("backfill_real_order_costs: %s aktualisiert, %s unveraendert, %s geteilt, "
                "%s geschuetzt, %s storniert, %s Fehler",
                updated, unchanged, skipped_shared, skipped_protected, skipped_void, failed)
    return {"updated": updated, "unchanged": unchanged, "skipped_shared": skipped_shared,
            "skipped_protected": skipped_protected, "skipped_void": skipped_void,
            "failed": failed, "changes": changes}


def set_sale_actual_cost(db: Session, *, sale_id: int, amount_eur) -> dict:
    """Tatsaechlichen Einkaufspreis (EK, EUR nach Rabatten) manuell am Verkauf hinterlegen —
    OHNE Beleg-Datei. Belege selbst laufen ueber den separaten Backfill; hier korrigiert der
    Nutzer nur die Zahl (Orders-Gewinn wird real statt Schaetzung).

    Setzt ``OrderAliexpress.cost_cny`` (wird systemweit als EUR-EK gelesen). Rein lokaler
    DB-Schreibvorgang (kein Netz-Call).

    GELD-SICHERHEIT (Review-Fund): fuer einen noch OFFENEN (bestellbaren) Verkauf ohne
    Bestell-Datensatz wird KEIN Phantom-"ordered"-Satz angelegt — der wuerde den
    Fulfillment-Idempotenz-Guard (siehe fulfill_sale) ausloesen und die echte AliExpress-
    Bestellung dauerhaft still blockieren. In dem Fall: klare Ablehnung ("erst bestellen").
    Existiert bereits eine Bestellung -> nur ``cost_cny`` korrigieren (Normalfall nach dem
    Bestellen). Terminaler Verkauf ohne Order (z.B. manuell ausserhalb der App bestellt und
    auf versendet/geliefert gesetzt) -> Satz anlegen, damit der echte EK haengt."""
    sale = db.get(Sale, sale_id)
    if sale is None:
        raise PersistentError("Sale nicht gefunden")
    amount = _to_decimal(amount_eur)
    if amount is None or amount <= 0:
        raise PersistentError("Betrag muss groesser als 0 sein")
    order = db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == sale_id))
    if order is None:
        if sale.status in _OPEN_SALE_STATES:
            raise PersistentError(
                "EK erst nach dem Bestellen korrigierbar – dieser Verkauf ist noch offen. "
                "Zuerst bei AliExpress bestellen, danach den tatsaechlichen EK eintragen.")
        order = OrderAliexpress(
            sale_id=sale_id,
            product_id=(sale.listing.product_id if sale.listing else None),
            quantity=sale.quantity or 1, status="ordered",
            order_date=datetime.now(timezone.utc))
        db.add(order)
        db.flush()
    order.cost_cny = amount   # echter EK (EUR, nach Rabatten) — Feldname historisch "cny"
    order.cost_source = "manual"   # Nutzer-Korrektur: der Backfill fasst das NIE mehr an
    db.commit()
    return {"sale_id": sale_id, "cost_eur": float(amount), "cost_is_actual": True}


# Ab dieser KI-Konfidenz wird die Variante automatisch uebernommen (sonst manuell).
_LLM_VARIANT_MIN_CONF = 0.8


async def resolve_variant_smart(db: Session, *, product: Product | None,
                                listing: Listing | None, variant_selected) -> tuple[dict | None, str]:
    """Varianten-Aufloesung in 3 Stufen (das System denkt mit):
    1. Regelbasiert (gelernte variant_map + Uebersetzung/Score) — kostenlos, sofort.
    2. KI-Agent (Claude): ordnet mehrdeutige/kryptische Namen semantisch zu.
    3. Bei KI-Erfolg >= Konfidenzschwelle: uebernehmen UND am Listing lernen
       (variant_map), sodass alle kuenftigen Sales dieses Listings kostenlos matchen.
    Rueckgabe: (variante|None, quelle) mit quelle in {rule, llm, none}."""
    v = _resolve_variant(product, variant_selected, listing=listing)
    if (v or {}).get("attr"):
        return v, "rule"

    skus = ((product.variants or {}).get("skus") or []) if product else []
    if not (isinstance(variant_selected, dict) and variant_selected and skus):
        return None, "none"

    ali = [{"attr": s.get("attr"),
            "name": " · ".join(str(x) for x in (s.get("options") or {}).values()) or s.get("attr"),
            "price": s.get("price")} for s in skus if s.get("attr")]
    from app.integrations import get_llm_client
    try:
        m = await get_llm_client().match_variant(
            ebay_selection=variant_selected, ali_variants=ali,
            product_title=(listing.title_seo if listing else "") or "")
    except Exception as exc:  # noqa: BLE001 – KI-Fehler -> manuelle Zuordnung
        logger.warning("variant KI-match failed", extra={"error": str(exc)[:150]})
        m = {"attr": "", "confidence": 0.0}

    attr = m.get("attr") or ""
    try:
        conf = float(m.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if attr and conf >= _LLM_VARIANT_MIN_CONF:
        sku = next((s for s in skus if s.get("attr") == attr), None)
        # GELD-SICHERHEIT: Auch die KI darf Praefix-Mehrdeutigkeit (Black vs. Black AI)
        # NICHT automatisch entscheiden -> solche Faelle immer manuell (kein Auto-Kauf,
        # kein Auto-Lernen einer womoeglich falschen Zuordnung).
        if sku is not None and not _attr_is_prefix_ambiguous(skus, attr):
            if listing is not None and isinstance(variant_selected, dict):
                _learn_variant(listing, product.aliexpress_id if product else None,
                               variant_selected, attr)   # gelernt (Aufrufer committet)
            logger.info("variant per KI zugeordnet", extra={
                "attr": attr, "confidence": conf, "reason": (m.get("reasoning") or "")[:120]})
            return {"attr": attr, "options": sku.get("options") or {}, "id": sku.get("id")}, "llm"
    return None, "none"


def _order_lines(order: dict) -> list[dict]:
    """getOrders-Order -> je lineItem ein Feld-Dict (Kaeuferadresse, SKU, Variante, Betrag)."""
    oid = order.get("orderId") or ""
    legacy = order.get("legacyOrderId") or oid
    created = order.get("creationDate")
    ship = (((order.get("fulfillmentStartInstructions") or [{}])[0] or {}).get("shippingStep") or {}).get("shipTo") or {}
    ca = ship.get("contactAddress") or {}
    addr = {
        "street": " ".join(x for x in [ca.get("addressLine1"), ca.get("addressLine2")] if x) or None,
        "city": ca.get("city"),
        "province": ca.get("stateOrProvince"),
        "postal": ca.get("postalCode"),
        "country": ca.get("countryCode"),
        "phone": (ship.get("primaryPhone") or {}).get("phoneNumber"),
    }
    buyer_name = ship.get("fullName") or (order.get("buyer") or {}).get("username")
    buyer_email = ship.get("email")
    # Storno-/Zahlungsstatus (Order-Ebene) – wurde frueher ignoriert, dadurch blieben
    # auf eBay stornierte Bestellungen hier ewig "pending" (Vorfall Sale 806).
    cancel_state = (order.get("cancelStatus") or {}).get("cancelState") \
        or (order.get("cancelStatus") or {}).get("state")
    payment_status = order.get("orderPaymentStatus")
    order_ful = order.get("orderFulfillmentStatus")
    out = []
    for li in order.get("lineItems") or []:
        aspects = {a.get("name"): a.get("value")
                   for a in (li.get("variationAspects") or []) if a.get("name")}
        out.append({
            "tx_id": f"{legacy}-{li.get('lineItemId')}",
            "ebay_order_id": oid,
            "ebay_line_item_id": li.get("lineItemId"),
            "item_id": str(li.get("legacyItemId") or ""),
            "sku": li.get("sku"),
            "buyer_name": buyer_name,
            "buyer_email": buyer_email,
            "delivery_address": addr,
            "quantity": int(li.get("quantity") or 1),
            "variant_selected": aspects or None,
            # ECHTER Kaeuferbetrag inkl. Versandanteil: li.total; Fallback Artikel+Versand.
            # (Nur lineItemCost unterschlug den 3€-Versand -> Umsatz/Gewinn zu niedrig.)
            "price_eur": (_to_decimal((li.get("total") or {}).get("value"))
                          or _to_decimal((li.get("lineItemCost") or {}).get("value"))),
            "sale_date": _parse_iso(created),
            "cancel_state": cancel_state,
            "payment_status": payment_status,
            # eBay-Versandstatus: pro Position (genauer) mit Fallback auf Order-Ebene.
            # NOT_STARTED | IN_PROGRESS | FULFILLED.
            "fulfillment_status": li.get("lineItemFulfillmentStatus") or order_ful,
        })
    return out


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _match_listing(db: Session, sku: str | None, item_id: str | None) -> Listing | None:
    if sku:
        found = db.scalar(select(Listing).where(Listing.ebay_sku == sku))
        if found:
            return found
        base = re.sub(r"-V\d+$", "", sku)  # Varianten-SKU -> Basis-SKU
        if base != sku:
            found = db.scalar(select(Listing).where(Listing.ebay_sku == base))
            if found:
                return found
    if item_id:
        found = db.scalar(select(Listing).where(Listing.ebay_item_id == str(item_id)))
        if found:
            return found
    return None


def _apply_cancel_state(db: Session, sale: Sale, f: dict) -> str | None:
    """Storno-/Refund-Status von eBay auf eine BESTEHENDE Sale anwenden.

    * NUR cancelState == "CANCELED" storniert (IN_PROGRESS = nur Anfrage -> Flag).
    * Offene Sales ohne AliExpress-Einkauf -> status "cancelled" (Aktionen weg).
    * Bereits eingekaufte Sales werden NIE umgestellt – nur geflaggt + Task-Alarm
      ("storniert NACH Einkauf": Nutzer entscheidet, kein Auto-Storno bei AliExpress).
    * FULLY_REFUNDED ohne Storno (Erstattung) -> status "refunded" (nur offene Sales).
    Rueckgabe: "cancelled" | "refunded" | "flagged" | None. Aufrufer committet.
    """
    state = f.get("cancel_state")
    pay = f.get("payment_status")
    result = None

    # Roh-Zustand mitfuehren; abgelehnte/zurueckgezogene Anfrage loescht das Flag.
    if state in ("CANCELED", "IN_PROGRESS") and sale.ebay_cancel_state != state:
        sale.ebay_cancel_state = state
        result = "flagged"
    elif state == "NONE_REQUESTED" and sale.ebay_cancel_state == "IN_PROGRESS":
        sale.ebay_cancel_state = None
        result = "flagged"

    if sale.status in ("cancelled", "refunded"):
        return None   # bereits verbucht -> kein Doppelzaehlen

    has_order = db.scalar(select(OrderAliexpress.id)
                          .where(OrderAliexpress.sale_id == sale.id)) is not None
    if state == "CANCELED":
        if sale.status in _OPEN_SALE_STATES and not has_order:
            sale.status = "cancelled"
            return "cancelled"
        if has_order or sale.status in ("ordered_aliexpress", "tracking", "delivered"):
            # Geld ist schon ausgegeben -> NUR Alarm, Status bleibt (Nutzer entscheidet).
            if result == "flagged":
                with task_log(db, task_type="cancel_after_purchase",
                              reference_id=sale.id) as tl:
                    tl.result_data = {"ebay_order_id": sale.ebay_order_id,
                                      "status": sale.status,
                                      "hint": "eBay-Storno NACH AliExpress-Einkauf – "
                                              "manuell pruefen (kein Auto-Storno)."}
                logger.warning("eBay-Storno NACH Einkauf", extra={"sale_id": sale.id})
            return "flagged" if result else None
    elif pay == "FULLY_REFUNDED" and sale.status in _OPEN_SALE_STATES and not has_order:
        sale.status = "refunded"
        return "refunded"
    return result


async def sync_ebay_orders(db: Session, *, days: int = 30, max_orders: int = 200,
                           ebay=None) -> dict:
    """eBay-Bestellungen holen: neue als Sale (pending) anlegen UND bestehende
    aktualisieren (Storno-/Refund-Abgleich). Idempotent.

    Loest NUR das Anlegen/Markieren aus – die (geldrelevante) AliExpress-Bestellung
    passiert erst nach manueller Freigabe via fulfill_sale. ``ebay`` optional injizierbar
    (z.B. vom Voll-Import), sonst der echte Client.
    """
    from datetime import timedelta
    since = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    ebay = ebay or _real_ebay()
    orders = await ebay.list_all_orders(since=since, max_orders=max_orders)
    created = cancelled = refunded = flagged = marked_shipped = 0
    _vimg_listing_ids: set[int] = set()   # Listings neuer Varianten-Sales ohne eBay-Bild-Cache
    _liefercheck_sales: list[int] = []    # neue AUSLANDS-Sales -> Liefer-Check nach dem Commit
    for order in orders:
        for f in _order_lines(order):
            if not f["ebay_line_item_id"]:
                continue
            # Dedup ueber den STABILEN natuerlichen Schluessel (order_id, line_item_id) –
            # NICHT nur ueber tx_id: die tx_id-Formate variieren (altes Order-Level-Aggregat
            # "13-14581-54353" vs. Line-Item-composite "13-...-54353-1008..."), wodurch ein
            # Re-Import sonst Duplikate anlegt (Vorfall 06./07.07., mehrfach). tx_id nur Fallback.
            existing = db.scalar(select(Sale).where(
                Sale.ebay_order_id == f["ebay_order_id"],
                Sale.ebay_line_item_id == f["ebay_line_item_id"]))
            if existing is None:
                existing = db.scalar(select(Sale).where(Sale.ebay_transaction_id == f["tx_id"]))
            if existing is not None:
                outcome = _apply_cancel_state(db, existing, f)
                if outcome == "cancelled":
                    cancelled += 1
                elif outcome == "refunded":
                    refunded += 1
                elif outcome == "flagged":
                    flagged += 1
                # eBay-Versandstatus in die lokale DB zuruecksynchronisieren: was auf
                # eBay als versandt (FULFILLED) gilt, darf lokal nicht mehr "offen"
                # stehen (Nutzer meldet Tracking oft direkt auf eBay -> sonst driftet
                # der Status). Storno/Refund werden NIE ueberschrieben, nie Downgrade.
                if (f.get("fulfillment_status") == "FULFILLED"
                        and existing.status not in ("tracking", "delivered",
                                                    "cancelled", "refunded")):
                    existing.status = "tracking"
                    marked_shipped += 1
                # Self-Heilung: die eBay-Publish-SKU IMMER am Sale sichern (harmlose Metadaten) UND die
                # verkaufte Variante deterministisch aus unserer Publish-SKU ({base}-V{i}) setzen:
                #  - keine Variante -> nachtragen,
                #  - vorhandene, aber NICHT wertverifizierte Variante, die von der Position abweicht
                #    -> AUTO-KORREKTUR auf die Position (fixt z.B. eine versehentliche Fehlzuordnung;
                #       der Nutzer verifiziert vor dem Kauf, kein Auto-Kauf).
                # Eine durch die Kaeuferwerte VERIFIZIERTE Zuordnung wird NIE ueberschrieben.
                if (f.get("sku") and isinstance(existing.variant_selected, dict)
                        and existing.listing_id):
                    _vs2 = dict(existing.variant_selected)
                    _dirty = False
                    if not _vs2.get("ebay_sku"):
                        _vs2["ebay_sku"] = f["sku"]
                        _dirty = True
                    # Auto-Korrektur NUR bei noch offenen Sales (nicht bei versandten/stornierten –
                    # Audit-Wahrheit) und NIE bei einer menschlich bestaetigten (locked) Zuordnung.
                    if (existing.status in ("pending", "needs_manual_review")
                            and not _vs2.get("variant_locked")):
                        _lst = db.get(Listing, existing.listing_id)
                        _pr = db.get(Product, _lst.product_id) if (_lst and _lst.product_id) else None
                        _d = _attr_from_ebay_variation_sku(_lst, _pr, f["sku"], existing.variant_selected)
                        _pa = (_d or {}).get("attr")
                        if _pa:
                            _skus = ((_pr.variants or {}).get("skus") or []) if _pr else []
                            _cur = _vs2.get("attr")
                            if not _cur or (_pa != _cur
                                            and not _variant_consistent_with_buyer(_vs2, _skus, _cur)):
                                _vs2.update({"attr": _pa, "options": _d["options"], "id": _d["id"]})
                                _dirty = True
                    if _dirty:
                        existing.variant_selected = _vs2
                continue
            listing = _match_listing(db, f["sku"], f["item_id"])
            # Schon storniert/erstattet eingehende Bestellungen direkt so verbuchen
            # (Datensatz fuer die Historie, aber KEINE Bestell-Aktionen im UI –
            # sonst kauft Auto-Fulfill im selben Scheduler-Lauf ein).
            if f.get("cancel_state") == "CANCELED":
                initial_status = "cancelled"
            elif f.get("payment_status") == "FULLY_REFUNDED":
                initial_status = "refunded"
            elif f.get("fulfillment_status") == "FULFILLED":
                # Auf eBay bereits versandt (haeufig: aeltere/manuell versandte Order)
                # -> nicht als "offen" anlegen, sonst faelschlich in der To-do-Liste.
                initial_status = "tracking"
            else:
                initial_status = "pending"
            # DETERMINISTISCHE Variante aus unserer Publish-SKU baken: die verkaufte eBay-SKU
            # '{base}-V{i}' zeigt EXAKT auf die AliExpress-Variante (kein Raten fuer eigene
            # Listings). Zusaetzlich die eBay-SKU merken (Ausweichquellen/Backfill).
            _vs = dict(f["variant_selected"] or {})
            if f.get("sku"):
                _vs.setdefault("ebay_sku", f["sku"])
                _prod = db.get(Product, listing.product_id) if (listing and listing.product_id) else None
                _det = _attr_from_ebay_variation_sku(listing, _prod, f["sku"], _vs)
                if _det and _det.get("attr"):
                    _vs.update({"attr": _det["attr"], "options": _det["options"], "id": _det["id"]})
            sale = Sale(
                ebay_transaction_id=f["tx_id"],
                ebay_order_id=f["ebay_order_id"],
                ebay_line_item_id=f["ebay_line_item_id"],
                listing_id=listing.id if listing else None,
                buyer_name=f["buyer_name"],
                buyer_email=f["buyer_email"],
                delivery_address=f["delivery_address"],
                quantity=f["quantity"],
                variant_selected=(_vs or None),
                price_eur=f["price_eur"] or (listing.price_eur if listing else None),
                sale_date=f["sale_date"] or datetime.now(timezone.utc),
                status=initial_status,
                ebay_cancel_state=f.get("cancel_state") if f.get("cancel_state")
                in ("CANCELED", "IN_PROGRESS") else None,
            )
            db.add(sale)
            db.flush()
            # Varianten-Verkauf + eBay-Bild-Cache fehlt -> nach dem Commit einmalig holen
            # (Orders-Vorschau zeigt sonst kein/das falsche Varianten-Bild).
            if (f["variant_selected"] and listing is not None
                    and not getattr(listing, "ebay_variant_images", None)):
                _vimg_listing_ids.add(listing.id)
            if initial_status == "cancelled":
                cancelled += 1
            elif initial_status == "refunded":
                refunded += 1
            else:
                try:
                    from app.services import invoice_service
                    invoice_service.generate_sale_invoice(db, sale_id=sale.id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("auto sale-invoice (sync) failed",
                                   extra={"sale_id": sale.id, "error": str(exc)})
                if initial_status == "tracking":
                    marked_shipped += 1
                # Auslands-Lieferung? -> nach dem Commit pruefen, ob die Quellen
                # dorthin liefern (Liefer-Check, Nutzerauftrag 16.08.).
                _land = str(((f["delivery_address"] or {}).get("country")) or "").upper()
                if _land and _land != "DE":
                    _liefercheck_sales.append(sale.id)
            created += 1
    db.commit()
    # eBay-Varianten-Bilder fuer neue Varianten-Sales nachladen (best effort, read-only,
    # NACH dem Commit -> keine Schreibsperre ueber eBay-Calls). Nur Betriebs-Instanz.
    if _vimg_listing_ids and get_settings().monitor_push_real:
        from app.services import golive_service as _gl
        for lid in list(_vimg_listing_ids)[:10]:
            try:
                await _gl.pull_ebay_variant_images(db, listing_id=lid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("variant-image pull failed",
                               extra={"listing_id": lid, "error": str(exc)[:120]})
    # Liefer-Check fuer neue Auslands-Sales (best effort, NACH dem Commit — keine
    # Schreibsperre ueber Netz-Calls). Ein Fehler bricht den Sync nie ab.
    for _sid in _liefercheck_sales:
        try:
            from app.services import delivery_check_service
            _s2 = db.get(Sale, _sid)
            if _s2 is not None:
                await delivery_check_service.pruefe_sale(db, _s2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("liefer-check fehlgeschlagen",
                           extra={"sale_id": _sid, "error": str(exc)[:120]})
    return {"orders_seen": len(orders), "sales_created": created,
            "sales_cancelled": cancelled, "sales_refunded": refunded,
            "cancel_flags": flagged, "marked_shipped": marked_shipped,
            "liefer_checks": len(_liefercheck_sales)}


def mark_sale_cancelled(db: Session, *, sale_id: int, undo: bool = False) -> dict:
    """Manueller Storno-Fallback im UI (z.B. telefonisch vereinbart, Sync spaeter).

    Storniert NUR offene Sales ohne AliExpress-Einkauf; bereits eingekaufte werden
    lediglich geflaggt (Warnhinweis) – nie wird bei AliExpress etwas angefasst.
    """
    sale = db.get(Sale, sale_id)
    if sale is None:
        raise PersistentError("Sale nicht gefunden")
    if undo:
        if sale.status not in ("cancelled", "refunded"):
            raise PersistentError("Sale ist nicht storniert")
        sale.status = "pending"
        sale.ebay_cancel_state = None
        db.commit()
        return {"sale_id": sale_id, "status": sale.status, "undone": True}
    has_order = db.scalar(select(OrderAliexpress.id)
                          .where(OrderAliexpress.sale_id == sale_id)) is not None
    if has_order or sale.status in ("ordered_aliexpress", "tracking", "delivered"):
        sale.ebay_cancel_state = sale.ebay_cancel_state or "CANCELED"
        db.commit()
        return {"sale_id": sale_id, "status": sale.status, "flagged": True,
                "warning": "Bereits bei AliExpress eingekauft – Bestellung dort "
                           "manuell pruefen/stornieren (macht das Tool nie automatisch)."}
    if sale.status not in _OPEN_SALE_STATES:
        raise PersistentError(f"Status '{sale.status}' kann nicht storniert werden")
    sale.status = "cancelled"
    sale.ebay_cancel_state = sale.ebay_cancel_state or "CANCELED"
    db.commit()
    return {"sale_id": sale_id, "status": "cancelled"}


def discard_order_for_reorder(db: Session, *, sale_id: int) -> dict:
    """Eine NICHT versendete (i.d.R. verfallene/unbezahlte) AliExpress-Bestellung VERWERFEN,
    damit der Sale NEU bestellt werden kann. Der Nutzer bestaetigt im UI ausdruecklich, dass
    die alte Bestellung nicht bezahlt wurde/verfallen ist (sonst Doppelkauf). Loescht nur den
    lokalen Order-Datensatz + setzt den Sale auf 'pending'; bei AliExpress wird NICHTS
    angefasst. Versendete/getrackte Bestellungen werden NIE verworfen."""
    sale = db.get(Sale, sale_id)
    if sale is None:
        raise PersistentError("Sale nicht gefunden")
    order = db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == sale_id))
    if order is not None and (order.tracking_number or sale.status in ("tracking", "delivered")):
        raise PersistentError("Bestellung ist bereits versandt/getrackt – wird NICHT verworfen "
                              "(sonst Doppelkauf). Bei AliExpress manuell pruefen.")
    old = order.aliexpress_order_id if order else None
    if order is not None:
        db.delete(order)
    sale.status = "pending"
    sale.ebay_cancel_state = None
    db.commit()
    return {"sale_id": sale_id, "status": "pending", "discarded": bool(order),
            "old_aliexpress_order_id": old}


# Manuell setzbare Status (UI-Dropdown). Aendert NIE etwas bei AliExpress/eBay.
_MANUAL_STATUSES = {"pending", "ordered_aliexpress", "tracking", "delivered",
                    "cancelled", "refunded", "needs_manual_review"}


def set_sale_status(db: Session, *, sale_id: int, status: str) -> dict:
    """Manuelle Status-Aenderung eines Verkaufs (UI). Nur der LOKALE Status."""
    sale = db.get(Sale, sale_id)
    if sale is None:
        raise PersistentError("Sale nicht gefunden")
    st = (status or "").strip()
    if st not in _MANUAL_STATUSES:
        raise PersistentError(f"Ungueltiger Status '{st}'")
    sale.status = st
    if st in ("cancelled", "refunded"):
        sale.ebay_cancel_state = sale.ebay_cancel_state or "CANCELED"
    else:
        sale.ebay_cancel_state = None
    db.commit()
    return {"sale_id": sale_id, "status": st}


def mark_cancel_reviewed(db: Session, *, sale_id: int, reviewed: bool = True) -> dict:
    """Storno abhaken -> faellt aus der 'bitte pruefen'-Liste (temporaeres Tracking)."""
    sale = db.get(Sale, sale_id)
    if sale is None:
        raise PersistentError("Sale nicht gefunden")
    sale.cancel_reviewed = bool(reviewed)
    db.commit()
    return {"sale_id": sale_id, "cancel_reviewed": sale.cancel_reviewed}


def mark_ae_paid(db: Session, *, sale_id: int, paid: bool = True) -> dict:
    """'Bei AliExpress bezahlt' markieren/zuruecknehmen — nur eine lokale Notiz.

    Loest KEINE Zahlung aus und aendert nichts bei AliExpress. Dient allein dazu,
    im Dashboard zu sehen, welche Bestellung bereits bezahlt wurde.
    """
    sale = db.get(Sale, sale_id)
    if sale is None:
        raise PersistentError("Sale nicht gefunden")
    sale.ae_paid = bool(paid)
    db.commit()
    return {"sale_id": sale_id, "ae_paid": sale.ae_paid}


def bulk_set_status(db: Session, *, from_statuses: list[str], to_status: str) -> dict:
    """Alle Sales aus from_statuses auf to_status setzen (einmalige Bereinigung).
    Nur der LOKALE Status – nie etwas bei AliExpress/eBay."""
    if to_status not in _MANUAL_STATUSES:
        raise PersistentError(f"Ungueltiger Zielstatus '{to_status}'")
    rows = db.scalars(select(Sale).where(Sale.status.in_(from_statuses))).all()
    for s in rows:
        s.status = to_status
    db.commit()
    return {"updated": len(rows), "to": to_status}


def _auto_fulfill_margin_ok(listing: Listing, settings) -> tuple[bool, float | None]:
    """Aktuelle Marge des Listings aus den zuletzt gesyncten Werten (EK/VK).

    Gleiche Formel wie die Preis-Check-Anzeige (listing_match_service):
        gewinn = VK - VK*fee_pct - fixe_gebuehr - EK ; marge = gewinn / VK.
    EK stammt aus dem Preis-Monitoring (alle 6 h aktualisiert). Fehlt EK oder VK
    (oder EK<=0), ist die Marge NICHT verifizierbar -> Fail-safe: nicht ok, damit
    NIE blind mit unbekannter/negativer Marge automatisch gekauft wird.
    """
    price = float(listing.price_eur) if listing.price_eur is not None else None
    cost = float(listing.cost_eur) if listing.cost_eur is not None else None
    if not price or not cost or cost <= 0:
        return False, None
    from app.services import pricing as _pricing
    fee_pct = _pricing.effective_fee_pct_for_listing(listing, settings=settings)
    profit = price - price * fee_pct - _pricing.ebay_fixed_fee(settings) - cost
    margin = profit / price
    return (margin >= settings.auto_fulfill_min_margin_pct), round(margin, 4)


async def auto_fulfill_due(db: Session, *, min_age_minutes: int | None = None,
                           max_per_run: int = 5) -> dict:
    """AUTO-FULFILLMENT: offene Sales automatisch bei AliExpress bestellen.

    Läuft NUR, wenn AUTO_FULFILL=true gesetzt ist (Geld-Schalter, Default aus).
    Sicherungen:
      * MARGE-SPERRE: nur bestellen, wenn die aktuelle Marge >= Schwelle
        (auto_fulfill_min_margin_pct) ist – sonst -> needs_manual_review, KEIN Kauf.
      * VARIANTEN-BLINDKAUF-SPERRE greift in fulfill_sale (mehrdeutig -> manuell).
      * Karenzzeit konfigurierbar (auto_fulfill_grace_minutes, Default 0 = sofort).
      * Max N je Lauf (Sicherheitsbremse). Idempotenz in fulfill_sale.
    Fehlerhafte/gesperrte Sales landen in needs_manual_review (kein Auto-Retry).
    """
    from datetime import timedelta
    settings = get_settings()
    if not settings.auto_fulfill:
        return {"enabled": False, "ordered": 0}
    if min_age_minutes is None:
        min_age_minutes = max(0, settings.auto_fulfill_grace_minutes)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=min_age_minutes)
    sales = db.scalars(select(Sale).where(Sale.status == "pending",
                                          Sale.listing_id.isnot(None))).all()
    ordered = errors = skipped_margin = 0
    for sale in sales:
        if ordered >= max_per_run:
            break
        when = sale.sale_date or sale.created_at
        if when is not None:
            w = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
            if w > cutoff:
                continue  # Karenzzeit noch nicht um (bei grace=0 quasi immer erfuellt)
        listing = db.get(Listing, sale.listing_id)
        if listing is None or not listing.product_id:
            continue  # ohne AliExpress-Produkt nicht automatisch bestellbar
        # EIGENBESTAND: selbst gelagerte Varianten NICHT automatisch bestellen -> Nutzer
        # versendet selbst (der manuelle Fulfill-Klick markiert sie als "self_shipped").
        if _resolve_self_stock_attr(sale, listing, db.get(Product, listing.product_id)):
            continue
        # MARGE-SPERRE: nie automatisch mit Verlust/zu geringer Marge einkaufen.
        ok, margin = _auto_fulfill_margin_ok(listing, settings)
        if not ok:
            sale.status = "needs_manual_review"
            db.commit()
            skipped_margin += 1
            logger.info("auto-fulfill SKIP (Marge zu niedrig/unbekannt)",
                        extra={"sale_id": sale.id, "margin": margin,
                               "min": settings.auto_fulfill_min_margin_pct})
            continue
        try:
            await fulfill_sale(db, sale_id=sale.id)
            ordered += 1
            logger.info("auto-fulfill OK", extra={"sale_id": sale.id, "margin": margin})
        except Exception as exc:  # noqa: BLE001 – Status setzt fulfill_sale selbst
            errors += 1
            logger.warning("auto-fulfill failed", extra={"sale_id": sale.id,
                                                         "error": str(exc)[:200]})
    return {"enabled": True, "ordered": ordered, "errors": errors,
            "skipped_margin": skipped_margin}


def promote_stale_tracking(db: Session, *, days: int | None = None) -> dict:
    """"Unterwegs" (tracking) -> "Zugestellt" (delivered), wenn der Verkauf aelter als
    ``days`` Tage ist.

    eBay/AliExpress melden kein echtes Zustell-Ereignis; nach dieser Frist ist ein Paket
    nach DE praktisch sicher angekommen (Zeit-Heuristik, kein Finanzwert). Beruehrt NUR
    Sales im Status 'tracking' – Storno/Erstattung/offen bleiben unangetastet. ``days=0``
    (bzw. Config 0) schaltet die Hochstufung aus.
    """
    from datetime import timedelta
    if days is None:
        days = get_settings().tracking_delivered_after_days
    if not days or days <= 0:
        return {"promoted": 0, "days": days}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    promoted = 0
    for sale in db.scalars(select(Sale).where(Sale.status == "tracking")).all():
        when = sale.sale_date or sale.created_at
        if when is None:
            continue
        w = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        if w < cutoff:
            sale.status = "delivered"
            promoted += 1
    if promoted:
        db.commit()
    return {"promoted": promoted, "days": days}


# Zusteller-Status (AliExpress/Cainiao get_tracking, roher Freitext!) -> „zugestellt".
# ASCII-Marker mit WORTGRENZE, damit „signed" NICHT „assigned" und „delivered" NICHT
# „undelivered" trifft. CJK-Marker als reine Teilstrings (keine Wortgrenzen).
_DELIVERED_RE = re.compile(
    r"(?<![a-z])(delivered|signed|zugestellt|livr[ée]|entregado|consegnato|"
    r"delivery successful|successfully delivered)(?![a-z])")
_DELIVERED_CJK = ("签收", "投递成功", "已妥投", "妥投")
# NEGATIONEN: eine Zustell-Meldung MIT Negation ist eine FEHLzustellung/Retoure/Ablehnung –
# NICHT zugestellt (z.B. „konnte nicht zugestellt werden", „undelivered, returned to sender",
# „no entregado", „拒绝签收", „failed delivery attempt"). Lieber ein False-Negativ (die
# Zeit-Heuristik holt es nach) als faelschlich „zugestellt".
_DELIVERED_NEG = ("not delivered", "nicht zugestellt", "konnte nicht", "no entregado",
                  "non livr", "non consegnat", "unable", "could not", "fail", "refus",
                  "returned", "return to", "undeliver", "attempt", "拒", "无法", "未妥投",
                  "退回", "退件", "退货",
                  # „签收" (quittiert) hat eine haeufige NOCH-NICHT-Form: „待签收" (wartet auf
                  # Quittierung, am Abholpunkt), „即将签收", „未签收" -> NICHT zugestellt.
                  "待签收", "即将签收", "未签收", "待取")
# ZUKUNFT/VERLAUF: dieselbe Partizipform heisst im Deutschen/Romanischen auch „wird (heute)
# zugestellt" bzw. „sera livré" (= UNTERWEGS, noch nicht da). Das Pendant zu „out for delivery"
# – ebenfalls NICHT als zugestellt werten (DE ist die Default-Sprache der Tracking-API).
_DELIVERED_FUTURE_RE = re.compile(
    r"\bwird\b.*\bzugestellt|voraussicht|\bsera\b.*\blivr|\bser[áa]\b.*\bentregad|"
    r"\bsar[àa]\b.*\bconsegnat")


def _is_delivered_tracking(status) -> bool:
    """True, wenn der Zusteller-Status eine ABGESCHLOSSENE ZUSTELLUNG meldet (mehrsprachig,
    defensiv gegen Negationen, Teilwort-Treffer UND Zukunfts-/Verlaufsform)."""
    if not status:
        return False
    s = str(status).strip().lower()
    if any(n in s for n in _DELIVERED_NEG) or _DELIVERED_FUTURE_RE.search(s):
        return False
    return bool(_DELIVERED_RE.search(s)) or any(c in s for c in _DELIVERED_CJK)


async def promote_delivered_from_tracking(db: Session, *, ae=None, limit: int = 150,
                                          min_age_days: int = 2) -> dict:
    """„Unterwegs" (tracking) -> „Zugestellt" (delivered) auf Basis des ECHTEN Zusteller-Status
    (AliExpress/Cainiao get_tracking), nicht nur der Zeit-Heuristik.

    Prueft NUR Sales im Status 'tracking' mit Sendungsnummer, aelter als ``min_age_days`` (zu frisch
    = sicher noch nicht da) und juenger als die Zeit-Frist ``tracking_delivered_after_days`` (aeltere
    stuft ``promote_stale_tracking`` ohnehin hoch). Gedeckelt (``limit``), aelteste zuerst, je
    AE-Order-ID nur EIN Abruf/Lauf, best-effort. Storno/Erstattung/offen bleiben unangetastet.
    Reiner Fulfillment-Status (kein Finanzwert)."""
    from datetime import timedelta
    s = get_settings()
    if not s.auto_delivered_from_tracking:
        return {"checked": 0, "promoted": 0, "errors": 0, "disabled": True}
    now = datetime.now(timezone.utc)
    young_cutoff = now - timedelta(days=max(0, min_age_days))          # nicht zu frisch
    old_cutoff = now - timedelta(days=max(1, s.tracking_delivered_after_days or 40))
    q = (select(Sale, OrderAliexpress)
         .join(OrderAliexpress, OrderAliexpress.sale_id == Sale.id)
         .where(Sale.status == "tracking",
                OrderAliexpress.tracking_number.isnot(None),
                OrderAliexpress.aliexpress_order_id.isnot(None),
                # Grob im Fenster schon in SQL begrenzen (nicht ALLE 'tracking'-Sales laden);
                # die exakte Ober-/Untergrenze prueft danach der Python-Filter.
                func.coalesce(Sale.sale_date, Sale.created_at) >= old_cutoff)
         .order_by(Sale.sale_date.asc()))
    ae = ae or get_aliexpress_client()
    checked = promoted = errors = 0
    seen: dict[str, bool] = {}                 # AE-Order-ID -> zugestellt? (einmal je Lauf abfragen)
    for sale, order in db.execute(q).all():
        if checked >= max(1, limit):
            break
        when = sale.sale_date or sale.created_at
        if when is not None:
            w = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
            if w > young_cutoff or w < old_cutoff:
                continue                        # zu frisch ODER schon im Zeit-Heuristik-Bereich
        aoid = str(order.aliexpress_order_id or "")
        if not aoid or aoid.startswith("EBAY-"):
            continue                            # CSV-Import ohne echte AliExpress-Nummer
        checked += 1
        try:
            if aoid not in seen:
                t = await ae.get_tracking(aoid)
                seen[aoid] = _is_delivered_tracking(getattr(t, "status", None))
            if seen[aoid]:
                sale.status = "delivered"
                if order.status in ("ordered", "shipped"):
                    order.status = "delivered"
                promoted += 1
        except Exception as exc:  # noqa: BLE001 – Einzelfehler stoppen den Lauf nicht
            errors += 1
            logger.warning("delivered-from-tracking failed",
                           extra={"sale_id": sale.id, "error": str(exc)[:160]})
    if promoted:
        db.commit()
    return {"checked": checked, "promoted": promoted, "errors": errors}


# ANNAHME: EIN Prozess (uvicorn ohne --workers). Budget/Takt sind prozess-lokal – bei einem
# Multi-Worker-Deploy muesste die Drossel geteilt werden (Redis o.ae.), sonst je Worker 250/Tag.
_dhl_budget = {"date": None, "count": 0}   # Prozess-lokales Tages-Abrufbudget (Best-effort)
_dhl_pace = {"lock": None, "next_ts": 0.0}  # prozessweit: 1 Abruf / 5 s – AUCH bei ueberlappenden
                                            # Laeufen (stuendl. Scheduler + manuelles Synchronisieren)


async def _dhl_pace_wait() -> None:
    """Serialisiert ALLE DHL-Abrufe im Prozess auf >= 5 s Abstand (verhindert 429 bei Ueberlappung)."""
    import asyncio as _a
    import time as _t
    lock = _dhl_pace["lock"]
    if lock is None:
        lock = _dhl_pace["lock"] = _a.Lock()
    async with lock:
        wait = _dhl_pace["next_ts"] - _t.monotonic()
        if wait > 0:
            await _a.sleep(wait)
        _dhl_pace["next_ts"] = _t.monotonic() + 5.2


def _is_dhl_number(tracking: str | None) -> bool:
    return "dhl" in (carrier_from_tracking(tracking) or "").lower()


async def promote_delivered_from_dhl(db: Session, *, dhl=None, limit: int = 30,
                                     min_age_days: int = 1, recheck_hours: int = 20) -> dict:
    """„Unterwegs" -> „Zugestellt" per ECHTEM DHL-Zustellstatus (strukturierter statusCode der
    DHL-Shipment-Tracking-API), wo AliExpress die DHL-Zustellung nicht meldet.

    Gedrosselt gegen das Free-Limit (250/Tag, 1/5s): je Sendung max. 1 Abruf/``recheck_hours``
    (``delivery_checked_at``), max. ``limit``/Lauf, 5s Pause zwischen Abrufen, Prozess-Tagesbudget
    ``dhl_daily_cap``. NUR DHL-Nummern; Storno/Erstattung/offen bleiben unangetastet. Fulfillment-
    Status (kein Finanzwert)."""
    from datetime import timedelta
    from app.services.app_settings import effective_dhl_api_key
    s = get_settings()
    key = effective_dhl_api_key(db)          # Umgebung (.env) ODER im Dashboard gesetzt
    if not key:
        return {"checked": 0, "promoted": 0, "errors": 0, "disabled": True}
    from app.integrations.dhl import DhlRateLimited, DhlTrackingClient
    dhl = dhl or DhlTrackingClient(key)
    now = datetime.now(timezone.utc)
    young_cutoff = now - timedelta(days=max(0, min_age_days))
    old_cutoff = now - timedelta(days=max(1, s.tracking_delivered_after_days or 40))
    recheck_cutoff = now - timedelta(hours=max(1, recheck_hours))
    today = now.date().isoformat()
    if _dhl_budget["date"] != today:                      # Tagesbudget bei Datumswechsel neu
        _dhl_budget["date"], _dhl_budget["count"] = today, 0
    q = (select(Sale, OrderAliexpress)
         .join(OrderAliexpress, OrderAliexpress.sale_id == Sale.id)
         .where(Sale.status == "tracking",
                # Nur DHL-Paketnummern (003…) schon in SQL laden – Hermes/Cainiao gar nicht erst
                # scannen. TRIM: robust gegen fuehrende Leerzeichen (sonst faelschlich ausgeschlossen).
                func.trim(OrderAliexpress.tracking_number).like("003%"),
                func.coalesce(Sale.sale_date, Sale.created_at) >= old_cutoff,
                or_(OrderAliexpress.delivery_checked_at.is_(None),
                    OrderAliexpress.delivery_checked_at < recheck_cutoff))
         # laengst-/nie-geprueft zuerst -> gleichmaessige Rotation im Tagesbudget
         .order_by(OrderAliexpress.delivery_checked_at.is_(None).desc(),
                   OrderAliexpress.delivery_checked_at.asc(),
                   Sale.sale_date.asc()))
    checked = promoted = errors = 0
    for sale, order in db.execute(q).all():
        if checked >= max(1, limit) or _dhl_budget["count"] >= max(1, s.dhl_daily_cap):
            break
        when = sale.sale_date or sale.created_at
        if when is not None:
            w = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
            if w > young_cutoff:
                continue                                  # zu frisch (sicher noch nicht da)
        if not _is_dhl_number(order.tracking_number):
            continue                                      # doppelter Gurt: kein DHL -> nicht fragen
        await _dhl_pace_wait()                            # prozessweit 1 Abruf / 5 s einhalten
        checked += 1
        _dhl_budget["count"] += 1
        try:
            r = await dhl.get_status(order.tracking_number)
        except DhlRateLimited:
            _dhl_budget["count"] = max(1, s.dhl_daily_cap)   # heute Schluss
            break
        except Exception as exc:  # noqa: BLE001 – ein Fehler stoppt den Lauf nicht
            errors += 1
            order.delivery_checked_at = now
            db.commit()
            logger.warning("dhl status failed", extra={"sale_id": sale.id, "error": str(exc)[:160]})
            continue
        order.delivery_checked_at = now
        if r.delivered:
            sale.status = "delivered"
            if order.status in ("ordered", "shipped"):
                order.status = "delivered"
            promoted += 1
        db.commit()   # pro Order committen: Fortschritt durabel + ueberlappende Laeufe sehen ihn
    return {"checked": checked, "promoted": promoted, "errors": errors}


async def sync_tracking_all(db: Session, *, max_age_days: int = 30) -> dict:
    """Auto-Tracking-Sync: offene AliExpress-Bestellungen abfragen und Sendungsnummern
    automatisch an eBay melden (v.a. DHL kommt schnell).

    Ueberspringt CSV-importierte Alt-Kaeufe (synthetische Referenz / zu alt) und
    Sales, deren Tracking bereits gemeldet ist. Fehler je Order werden gezaehlt,
    stoppen den Lauf aber nicht.
    """
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, max_age_days))
    orders = db.scalars(select(OrderAliexpress).where(
        OrderAliexpress.status.in_(["ordered", "shipped"]),
        OrderAliexpress.aliexpress_order_id.isnot(None),
    )).all()
    checked = reported = errors = 0
    _tracking_cache: dict[str, tuple] = {}   # AE-Order-ID -> (nummer, carrier, eta) je Lauf
    for order in orders:
        aoid = str(order.aliexpress_order_id or "")
        if aoid.startswith("EBAY-"):
            continue  # CSV-Import ohne echte AliExpress-Nummer
        when = order.order_date or order.created_at
        if when is not None:
            w = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
            if w < cutoff:
                continue
        sale = db.get(Sale, order.sale_id) if order.sale_id else None
        # AUSLANDS-UPGRADE (Nutzer-Hinweis 19.08.): eine bereits gemeldete Standard-
        # Nummer weiter beobachten, bis der Endzusteller (Oesterr. Post/Express One)
        # uebernimmt — dann die bessere Nummer nachmelden. Endet mit der Uebergabe
        # (Nummer wird final) oder dem 30-Tage-Fenster oben.
        upgrade_offen = (
            sale is not None and sale.status == "tracking" and _sale_ausland(sale)
            and bool(order.tracking_number)
            and not _is_final_tracking(order.tracking_number))
        # "cancelled" MUSS hier stehen (Vorfall Sale 1179, 16.08.): der Nutzer hatte
        # storniert, aber der Sync meldete das vorhandene Tracking erneut an eBay
        # und setzte den Status alle 45 min zurueck auf "unterwegs".
        if (sale is not None and sale.status in ("tracking", "delivered",
                                                 "refunded", "cancelled")
                and not upgrade_offen):
            continue  # bereits gemeldet/abgeschlossen/storniert
        checked += 1
        vorher = order.tracking_number
        try:
            if not order.tracking_number:
                # Mehrpositions-Bestellungen teilen sich eine AE-Order-ID -> Tracking
                # nur EINMAL je AE-ID abrufen, weitere Zeilen uebernehmen es.
                cached = _tracking_cache.get(aoid)
                if cached is not None:
                    order.tracking_number, order.tracking_carrier, order.estimated_delivery = cached
                    if order.tracking_number:
                        order.tracking_carrier = (carrier_from_tracking(order.tracking_number)
                                                  or order.tracking_carrier)
                    db.commit()
                else:
                    await refresh_tracking(db, order_id=order.id)
                    _tracking_cache[aoid] = (order.tracking_number, order.tracking_carrier,
                                             order.estimated_delivery)
            elif upgrade_offen and aoid not in _tracking_cache:
                # refresh_tracking uebernimmt eine Endzusteller-Nummer, laesst die
                # Standard-Nummer sonst unangetastet; je AE-ID nur einmal pro Lauf.
                await refresh_tracking(db, order_id=order.id)
                _tracking_cache[aoid] = (order.tracking_number, order.tracking_carrier,
                                         order.estimated_delivery)
            if (_tracking_meldbar(order.tracking_number, sale)
                    and sale is not None
                    and (sale.status != "tracking" or order.tracking_number != vorher)
                    and sale.ebay_order_id and sale.ebay_line_item_id):
                # DE: NUR finale DHL-/Hermes-Nummern (Nutzerregel 2026-07-05).
                # AUSLAND: beste verfuegbare Nummer (Nutzer-Anweisung 19.08.,
                # Details am Helfer _tracking_meldbar).
                if not get_settings().monitor_push_real:
                    continue   # Dev-Kopie: niemals an das echte eBay melden
                await report_tracking_to_ebay(db, order_id=order.id)
                reported += 1
        except Exception as exc:  # noqa: BLE001 – Einzelfehler stoppen den Lauf nicht
            errors += 1
            logger.warning("tracking sync failed", extra={"order_id": order.id,
                                                          "error": str(exc)[:200]})
    return {"orders_open": len(orders), "checked": checked,
            "reported_to_ebay": reported, "errors": errors}


_UPU_POST = {  # UPU-S10-Suffix (Laenderkennung des Postbetreibers) -> Zusteller
    "AT": "Österreichische Post", "GR": "Hellenic Post (ELTA)", "NL": "PostNL",
    "BE": "bpost", "FR": "La Poste", "IT": "Poste Italiane", "ES": "Correos",
    "PL": "Poczta Polska", "CZ": "Česká pošta", "CH": "Swiss Post",
    "DE": "Deutsche Post", "GB": "Royal Mail", "PT": "CTT", "SE": "PostNord",
    "DK": "PostNord", "FI": "Posti", "HU": "Magyar Posta", "RO": "Poșta Română",
    "BG": "Bulgarian Posts", "HR": "Hrvatska pošta", "SI": "Pošta Slovenije",
    "SK": "Slovenská pošta", "IE": "An Post", "LU": "POST Luxembourg",
}


def carrier_from_tracking(tracking: str | None) -> str | None:
    """Versanddienstleister aus der Sendungsnummer ableiten.

    Regeln (siehe docs/versandpartner-laender.md, aus echten Sendungen gelernt):
    * '003' + Ziffern              -> DHL Paket (Deutschland)
    * 'H' + 19 Alphanum            -> Hermes (Deutschland)
    * UPU-S10 (XX#########YY)      -> Landespost per Suffix (AT=Österr. Post,
                                      GR=Hellenic Post, NL=PostNL, ...)
    * 20+ Ziffern (z.B. 158278...) -> Cainiao/AliExpress-Netzwerk
    Sonst None (eBay bekommt dann den konfigurierten Default).
    """
    import re
    t = (tracking or "").strip().upper()
    if t.startswith("003"):
        return "DHL"
    m = re.fullmatch(r"[A-Z]{2}\d{9}([A-Z]{2})", t)
    if m:
        return _UPU_POST.get(m.group(1), f"Landespost {m.group(1)}")
    if t.startswith("H") and len(t) >= 14:
        return "Hermes"
    if re.fullmatch(r"\d{20,26}", t):
        return "Cainiao (AliExpress Standard)"
    return None


def _is_final_tracking(tracking: str | None) -> bool:
    """True NUR für eine finale Zusteller-Nummer (DHL/Hermes/Landespost).

    Nutzerregel (2026-07-05): an eBay ausschliesslich die FINALE DHL-/Hermes-Nummer
    melden – niemals die provisorische AliExpress/Cainiao-Nummer (AP…/LP… oder eine
    reine 20-stellige Cainiao-Nummer), und vor der finalen Nummer lieber gar nichts.
    ``carrier_from_tracking`` liefert für AP/LP ``None`` und für die reine
    Cainiao-Nummer einen ``"Cainiao …"``-Namen – beides gilt hier als NICHT final.
    """
    c = carrier_from_tracking(tracking)
    return bool(c) and not c.startswith("Cainiao")


def _wirkt_cainiao(carrier: str | None) -> bool:
    """Traegt der API-Carrier-Name die AliExpress-/Cainiao-Handschrift?

    Dient dem Auslands-Upgrade: nur ein Carrier-Name OHNE diese Handschrift
    (z. B. 'Express One', 'Oesterreichische Post') gilt als Endzusteller-Uebergabe."""
    c = str(carrier or "").lower()
    return (not c) or any(m in c for m in ("aliexpress", "cainiao", "selection"))


def _sale_ausland(sale: Sale | None) -> bool:
    """Lieferland des Verkaufs ausserhalb Deutschlands?"""
    if sale is None:
        return False
    land = str(((sale.delivery_address or {}).get("country")) or "DE").strip().upper()
    return bool(land) and land != "DE"


def _tracking_meldbar(number: str | None, sale: Sale | None) -> bool:
    """Darf diese Nummer an eBay gemeldet werden?

    DE-Lieferungen: unveraendert NUR finale DHL-/Hermes-/Landespost-Nummern
    (Nutzerregel 2026-07-05 — die finale DHL-Nummer folgt dort sicher, vorher
    lieber gar nichts). AUSLANDS-Lieferungen (Vorfall Sales 1266/1274/1277,
    19.08., Nutzer-Anweisung "muss immer direkt aktualisiert werden"): dort
    folgt oft NIE eine deutsche Endzusteller-Nummer — die Cainiao-/AliExpress-
    Standard-Nummer ist die einzige trackbare und wird gemeldet.
    """
    if not number:
        return False
    return _is_final_tracking(number) or _sale_ausland(sale)


async def report_tracking_to_ebay(db: Session, *, order_id: int) -> dict:
    """Sendungsnummer einer AliExpress-Order an eBay melden (createShippingFulfillment)."""
    order = db.get(OrderAliexpress, order_id)
    if order is None:
        raise PersistentError("Order nicht gefunden")
    if not order.tracking_number:
        raise PersistentError("Noch keine Sendungsnummer vorhanden")
    sale = db.get(Sale, order.sale_id) if order.sale_id else None
    if sale is None or not sale.ebay_order_id or not sale.ebay_line_item_id:
        raise PersistentError("Keine eBay-Order/LineItem zur Meldung vorhanden")
    # STORNO-GUARD (Vorfall Sale 1179): fuer stornierte/erstattete Verkaeufe wird
    # NIE Tracking gemeldet – die Meldung wuerde den manuell gesetzten Status
    # ueberschreiben ("unterwegs") und dem Kaeufer einen Versand signalisieren.
    if sale.status in ("cancelled", "refunded"):
        raise PersistentError(
            "Verkauf ist storniert/erstattet – Tracking wird nicht an eBay gemeldet. "
            "Falls doch versendet werden soll: Status zuerst im ✏️-Menü zurücksetzen.")

    # SICHERUNG (Vorfall 2026-07-03): Dieser Pfad schreibt DIREKT auf das echte
    # eBay, unabhaengig von MOCK_EBAY. Eine Dev-Kopie mit gemocktem AliExpress
    # hat so eine Test-Sendungsnummer (1Z999AA...) an einen echten Kaeufer
    # gemeldet. Echte Meldungen nur auf der Betriebs-Instanz (MONITOR_PUSH_REAL).
    if not get_settings().monitor_push_real:
        raise PersistentError(
            "Tracking-Meldung an eBay auf dieser Instanz deaktiviert "
            "(MONITOR_PUSH_REAL=false – Dev-Kopie meldet nie an das echte eBay)")

    # Carrier automatisch erkennen (003->DHL, H->Hermes), falls nicht gesetzt
    if not order.tracking_carrier:
        order.tracking_carrier = carrier_from_tracking(order.tracking_number)

    ebay = _real_ebay()
    with task_log(db, task_type="report_tracking", reference_id=order_id) as tl:
        fid = await ebay.create_shipping_fulfillment(
            sale.ebay_order_id, tracking_number=order.tracking_number,
            line_items=[{"lineItemId": sale.ebay_line_item_id, "quantity": sale.quantity or 1}],
            carrier_code=order.tracking_carrier or None,
        )
        sale.status = "tracking"
        tl.result_data = {"fulfillment_id": fid, "tracking": order.tracking_number}
        db.commit()
    return {"order_id": order.id, "ebay_order_id": sale.ebay_order_id,
            "fulfillment_id": fid, "tracking_number": order.tracking_number, "status": "reported"}


async def add_manual_tracking(db: Session, *, sale_id: int, tracking_number: str,
                              carrier: str | None = None,
                              aliexpress_order_id: str | None = None) -> dict:
    """Sendungsnummer von Hand nachtragen und an eBay melden.

    Deckt die zwei Faelle ab, in denen die automatische Tracking-Sync NICHT
    greifen kann:
      1) Die Bestellung wurde MANUELL auf AliExpress aufgegeben -> es existiert
         gar keine ``OrderAliexpress``-Zeile, die Sync sieht sie also nie.
      2) Die App hat zwar bestellt, aber die AliExpress ``ds.order.tracking``-API
         liefert die Nummer nicht zurueck (haeufig – sie steht nur auf der
         Webseite; ``get_tracking`` gibt trotz Status 'bezahlt' ``None``).
    In beiden Faellen traegt der Nutzer die auf AliExpress sichtbare Nummer ein;
    die App legt/vervollstaendigt die Order-Zeile, meldet an eBay
    (createShippingFulfillment) und markiert versendet. KEIN Geld-Call.
    """
    tn = (tracking_number or "").strip()
    if not tn:
        raise PersistentError("Sendungsnummer fehlt")
    sale = db.get(Sale, sale_id)
    if sale is None:
        raise PersistentError("Sale nicht gefunden")
    if not sale.ebay_order_id or not sale.ebay_line_item_id:
        raise PersistentError("Keine eBay-Order/LineItem – kann nicht an eBay melden")

    order = db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == sale_id))
    if order is None:
        # Manuell auf AliExpress bestellt -> Zeile nachtragen, damit Tracking
        # gemeldet und die Bestellung fuer Belege/Sync verknuepft ist.
        listing = db.get(Listing, sale.listing_id) if sale.listing_id else None
        order = OrderAliexpress(
            sale_id=sale_id,
            product_id=(listing.product_id if listing else None),
            aliexpress_order_id=((aliexpress_order_id or "").strip() or None),
            variant_selected=sale.variant_selected,
            quantity=sale.quantity or 1,
            delivery_name=sale.buyer_name,
            delivery_address=sale.delivery_address,
            order_date=datetime.now(timezone.utc),
            status="shipped",
        )
        db.add(order)
        db.flush()
    elif (aliexpress_order_id or "").strip():
        order.aliexpress_order_id = aliexpress_order_id.strip()

    order.tracking_number = tn
    order.tracking_carrier = ((carrier or "").strip()
                              or carrier_from_tracking(tn)
                              or order.tracking_carrier)
    order.status = "shipped"
    db.commit()

    result = await report_tracking_to_ebay(db, order_id=order.id)
    result["manual"] = True
    return result


async def record_ebay_tracking(db: Session, *, sale_id: int) -> dict:
    """Selbst verschickt + Sendungsnummer DIREKT auf eBay eingetragen? Diese Nummer aus eBay
    LESEN und lokal hinterlegen – OHNE erneut an eBay zu melden (steht ja schon dort). Legt/
    vervollstaendigt die OrderAliexpress-Zeile (aliexpress_order_id bleibt None = Eigenversand)
    und markiert den Sale als versendet (tracking). KEIN Geld-Call, KEIN eBay-Schreibcall."""
    sale = db.get(Sale, sale_id)
    if sale is None:
        raise PersistentError("Sale nicht gefunden")
    if not sale.ebay_order_id:
        raise PersistentError("Keine eBay-Order – Tracking kann nicht bei eBay abgefragt werden")
    fulfillments = await _real_ebay().get_shipping_fulfillments(sale.ebay_order_id)
    f0 = next((f for f in fulfillments if f.get("tracking_number")), None)
    if f0 is None:
        return {"sale_id": sale_id, "found": False,
                "message": "eBay hat für diese Order keine Sendungsnummer hinterlegt."}
    tn = f0["tracking_number"]
    order = db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == sale_id))
    if order is None:
        listing = db.get(Listing, sale.listing_id) if sale.listing_id else None
        order = OrderAliexpress(
            sale_id=sale_id, product_id=(listing.product_id if listing else None),
            aliexpress_order_id=None,   # Eigenversand / manuell – keine AliExpress-Bestellung
            variant_selected=sale.variant_selected, quantity=sale.quantity or 1,
            delivery_name=sale.buyer_name, delivery_address=sale.delivery_address,
            order_date=datetime.now(timezone.utc), status="shipped")
        db.add(order)
        db.flush()
    order.tracking_number = tn
    order.tracking_carrier = (f0.get("carrier") or carrier_from_tracking(tn) or order.tracking_carrier)
    order.status = "shipped"
    if sale.status in ("pending", "needs_manual_review", "ordered_aliexpress"):
        sale.status = "tracking"
    db.commit()
    return {"sale_id": sale_id, "found": True, "tracking_number": tn,
            "carrier": order.tracking_carrier}


async def backfill_tracking_from_ebay(db: Session, *, limit: int = 300) -> dict:
    """Fuer ALLE Sales, die auf eBay als versendet gelten (Status tracking/delivered), aber
    lokal KEINE Sendungsnummer haben (z.B. selbst verschickt): die auf eBay eingetragene
    Nummer nachziehen. Ein Klick statt Order fuer Order."""
    rows = db.scalars(select(Sale).where(
        Sale.status.in_(("tracking", "delivered")), Sale.ebay_order_id.isnot(None)
    ).limit(limit)).all()
    checked = filled = 0
    for s in rows:
        o = db.scalar(select(OrderAliexpress).where(OrderAliexpress.sale_id == s.id))
        if o is not None and o.tracking_number:
            continue   # hat schon eine lokale Nummer
        checked += 1
        try:
            r = await record_ebay_tracking(db, sale_id=s.id)
            if r.get("found"):
                filled += 1
        except Exception as exc:  # noqa: BLE001 – eine Order darf den Lauf nicht stoppen
            logger.warning("tracking backfill failed", extra={"sale_id": s.id, "error": str(exc)[:120]})
    return {"checked": checked, "filled": filled}
