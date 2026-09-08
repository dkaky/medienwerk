"""Monitoring-/Repricing-Service (nativer AutoDS-Ersatz, Teil 2: Sync).

Periodischer Abgleich Lieferant <-> eBay – die Kernleistung, fuer die man sonst
AutoDS bucht:

* **Preis-Drift-Erkennung**: Lieferantenpreis neu lesen -> Soll-Preis via
  Pricing-Engine berechnen -> Abweichung nur MARKIEREN (price_drift); die
  Preisanpassung auf eBay macht ausschliesslich der Nutzer im Preis-Check.
* **Bestands-Sync**: Ausverkauf beim Lieferanten -> eBay-Menge auf 0 (kein Verkauf
  ohne Nachschub); wieder verfuegbar -> Menge zurueck auf 1.

Alle Aenderungen sind idempotent und werden in ``task_logs`` + ``price_history``
auditiert. eBay-Calls erfolgen nur fuer aktive Listings (Drafts existieren dort noch nicht).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations import get_aliexpress_client, get_ebay_client
from app.integrations.aliexpress import OutOfStockError, ProductNotFoundError
from app.models import Listing, Product
from app.services import pricing
from app.services.common import task_log

logger = logging.getLogger("app.services.monitoring")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _push_client(settings):
    """Client fuer eBay-Pushes: REAL nur mit explizitem Schalter (MONITOR_PUSH_REAL).

    Tests/Dev bleiben so garantiert ohne echte Schreib-Calls.
    """
    if settings.monitor_push_real and settings.ebay_refresh_token:
        from app.integrations.ebay import RealEbayClient
        return RealEbayClient(settings), True
    return get_ebay_client(), False


async def _safe_ebay_update(ebay, is_real: bool, listing, *, price_eur=None,
                            quantity=None, quantity_by_sku: dict | None = None) -> bool:
    """Preis/Menge auf eBay best-effort aktualisieren – Varianten-faehig.

    ``quantity``: EINE Menge fuer alle Varianten-SKUs (Standardfall OOS/Restock).
    ``quantity_by_sku``: {ebay_sku: menge} – PER-VARIANTE Menge (nur ausgewaehlte
    Varianten auf 0/zurueck). Hat Vorrang je SKU; SKUs ohne Eintrag bekommen ``quantity``.

    Real-Pfad: Varianten-SKUs aufloesen und die dedizierte Bulk-Preis/Mengen-API nutzen
    (keine Voll-Revalidierung). Mock-/Fallback-Pfad: update_inventory je SKU.
    Fehler werden geloggt, nicht geworfen (ein kaputtes Offer stoppt den Lauf nicht).
    """
    def _q_for(sku: str):
        if quantity_by_sku is not None and sku in quantity_by_sku:
            return quantity_by_sku[sku]
        return quantity
    try:
        if is_real:
            from app.services.golive_service import _listing_variant_skus
            skus = await _listing_variant_skus(ebay, listing)
            updates = []
            found_offer = False
            for sku in skus:
                offer = await ebay._first_offer_for_sku(sku)
                if not (offer and offer.get("offerId")):
                    continue
                found_offer = True
                q = _q_for(sku)
                if price_eur is None and q is None:
                    continue    # leerer Eintrag wird je SKU abgelehnt -> ganzer Batch kaputt
                updates.append({"sku": sku, "offer_id": offer["offerId"],
                                "price_eur": price_eur, "quantity": q})
            # FAIL-CLOSED bei Inventory-Listings: JEDER Ziel-Schluessel aus quantity_by_sku
            # muss wirklich im Push landen. Still verworfene Ziele (Phantom-/Altbestands-
            # Schluessel, Offer fehlt) wuerden sonst als Erfolg gemeldet und der Aufrufer
            # vergiftet den variant_stock-Spiegel -> die Nullung wird NIE wieder versucht.
            # Klassik-Listings ohne jedes Inventory-Offer behalten den Trading-Fallback.
            if found_offer and quantity_by_sku:
                missing = set(quantity_by_sku) - {u["sku"] for u in updates}
                if missing:
                    logger.warning("variant qty push unvollstaendig", extra={
                        "sku": listing.ebay_sku, "missing": sorted(missing)[:5],
                        "live": skus[:5]})
                    return False
            if updates:
                await ebay.bulk_update_price(updates)
            elif not found_offer and listing.ebay_item_id:
                # Importiertes Klassik-Listing (kein Inventory-Offer) -> Trading API.
                # Kein Per-Varianten-Update moeglich -> aggregierte Menge (Fallback).
                await ebay.revise_item_status(listing.ebay_item_id,
                                              price_eur=price_eur, quantity=quantity)
        elif quantity_by_sku is not None:
            # Mock/Fallback: Per-Variante je SKU melden.
            for sku, q in quantity_by_sku.items():
                await ebay.update_inventory(sku, quantity=q)
        else:
            kwargs = {}
            if price_eur is not None:
                kwargs["price_eur"] = price_eur
            if quantity is not None:
                kwargs["quantity"] = quantity
            await ebay.update_inventory(listing.ebay_sku, **kwargs)
        return True
    except Exception as exc:  # noqa: BLE001 – Sync darf an eBay-Fehlern nicht scheitern
        logger.warning("ebay update failed", extra={"sku": listing.ebay_sku, "error": str(exc)})
        return False


# (ehem. _alt_source_in_stock_map: ganze-Quelle-Proxy. Ersetzt durch
#  supplier_service.variant_alt_availability – prueft die KONKRETE Alt-SKU laut
#  Slot-Snapshot; Legacy-Eintraege/fehlender Snapshot fallen automatisch auf das
#  alte Ganze-Quelle-Verhalten zurueck.)


async def _push_classic_variation_qty(ebay, is_real: bool, listing, product,
                                      changed: dict, state: list[dict]) -> tuple[bool, str | None]:
    """KLASSIK-Listing (importiert, keine Inventory-Offers): Mengen je VARIATION
    ueber die Trading-API setzen (Vorfall Listing 200: Ausweich-Reaktivierung
    erreichte eBay nie — der Aggregat-Fallback kann keine einzelne Variante).

    Bruecke lokale Variante -> Live-Variation ueber die geld-erprobte Bestell-
    Aufloesung (variant_map/Regeln/Uebersetzung). FAIL-CLOSED: ist auch nur EINE
    geaenderte Variante nicht eindeutig zuordenbar, wird NICHTS gepusht (False),
    damit der variant_stock-Spiegel nicht vergiftet wird. Dauerhaft False ->
    einmal ⚡-Zuordnung im Preis-Dialog lernt die Bruecke."""
    if not is_real:
        try:
            for sku, q in changed.items():
                await ebay.update_inventory(sku, quantity=q)
            return True, None
        except Exception as exc:  # noqa: BLE001 – wie _safe_ebay_update: nie werfen
            logger.warning("classic qty push (mock) fehlgeschlagen: %s", str(exc)[:120])
            return False, str(exc)[:200]
    from app.services.order_service import resolve_selection_against_skus
    try:
        info = await ebay.get_item_price_info(listing.ebay_item_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("classic qty push: GetItem fehlgeschlagen: %s", str(exc)[:120])
        return False, f"GetItem: {str(exc)[:180]}"
    variations = info.get("variations") or []
    skus_local = ((product.variants or {}).get("skus") or []) if product else []
    if not variations or not skus_local:
        return False, "keine Live-Variationen oder Quell-SKUs"
    live_by_attr: dict[str, list] = {}
    for v in variations:
        specs = [(str(n), str(val)) for n, val in (v.get("specifics") or [])
                 if str(val).strip()]
        sel = dict(specs)
        if not sel:
            continue
        hit = resolve_selection_against_skus(sel, skus_local, listing=listing,
                                             source_id=str(product.aliexpress_id))
        attr = (hit or {}).get("attr")
        if attr and attr not in live_by_attr:
            live_by_attr[attr] = specs
    attr_by_our_sku = {st["ebay_sku"]: st.get("attr") for st in state}
    updates: list[tuple[list, int]] = []
    for our_sku, q in changed.items():
        attr = attr_by_our_sku.get(our_sku)
        specs = live_by_attr.get(attr) if attr else None
        if not specs:
            logger.info("classic qty push: Variante nicht zuordenbar -> kein Push "
                        "(⚡-Zuordnung im Preis-Dialog lernt die Bruecke)",
                        extra={"listing_id": listing.id, "our_sku": our_sku,
                               "attr": attr})
            return False, f"Variante nicht zuordenbar (attr={attr})"
        updates.append((specs, int(q)))
    try:
        # VariationSpecifics statt SKU: importierte Variationen tragen oft KEINE SKUs
        # (ReviseInventoryStatus scheiterte live an Listing 200).
        await ebay.revise_variation_quantities_by_specifics(
            listing.ebay_item_id, updates)
    except Exception as exc:  # noqa: BLE001
        logger.warning("classic qty push fehlgeschlagen: %s", str(exc)[:150])
        return False, f"Revise: {str(exc)[:200]}"
    logger.info("classic qty push ok",
                extra={"listing_id": listing.id, "updates": len(updates)})
    return True, None


async def _sync_variant_stock(db: Session, ebay, is_real: bool, listing, product,
                              settings, restock_ok: bool = True) -> dict:
    """PER-VARIANTE Bestand mit eBay abgleichen (nur Multivarianten-Listings).

    Ausverkaufte Varianten -> Menge 0; wieder lieferbare -> Standardmenge. Ist eine
    Variante bei der Hauptquelle ausverkauft, aber eine im Preis-Check verknuepfte
    Ausweich-Quelle (variant_source_map) ist lieferbar, bleibt sie SELLBAR (bestellt
    wird dann gezielt dort, Freigabe pro Bestellung). Seit 11.07. zaehlt dabei die
    KONKRETE Ziel-Variante der Ausweich-Quelle (Slot-SKU-Snapshot), nicht mehr nur
    die ganze Quelle; Legacy-Verknuepfungen behalten das alte Verhalten. Es werden
    NUR geaenderte SKUs gepusht (idempotent gegen listing.variant_stock)."""
    from sqlalchemy.orm.attributes import flag_modified
    from app.services.golive_service import variant_stock_state
    from app.services.supplier_service import variant_alt_availability
    state = variant_stock_state(listing, product)
    if not state:
        return {"variants": 0, "oos": 0, "changed": 0}
    default_q = settings.default_listing_quantity
    vmap = listing.variant_source_map or {}
    desired: dict = {}
    oos_unresolved = 0
    for st in state:
        if st["oos"]:
            entry = vmap.get(st["attr"]) if st.get("attr") else None
            has_alt = bool(entry) and variant_alt_availability(product, entry)["available"]
            desired[st["ebay_sku"]] = default_q if has_alt else 0
            if not has_alt:
                oos_unresolved += 1
        else:
            desired[st["ebay_sku"]] = default_q
    prev = listing.variant_stock or {}
    if not restock_ok:
        # Ohne positiven Bestands-Beweis (degradierte Antwort) KEINE Erhoehungen:
        # nur Senkungen/Nullungen und das Erstbefuellen unbekannter Spiegel-Staende
        # durchlassen – sonst reaktivieren stale gespeicherte Bestaende ein
        # womoeglich wirklich ausverkauftes Listing (Review-Fund 16.08.).
        desired = {sku: q for sku, q in desired.items()
                   if sku not in prev or int(q) <= int(prev.get(sku) or 0)}
    changed = {sku: q for sku, q in desired.items() if int(prev.get(sku, -1)) != int(q)}
    pushed_ok = True
    push_error: str | None = None
    if changed:
        is_active = (listing.listing_status == "active"
                     and bool(listing.ebay_sku or listing.ebay_item_id))
        if not is_active:
            pushed_ok = True
        elif (listing.ebay_item_id
              and not (listing.ebay_draft_id or "").endswith("-GRP")):
            # Importiertes KLASSIK-Listing: per-Varianten-Menge geht nur ueber die
            # Trading-API (Vorfall Listing 200 — Inventory-Offers existieren nicht).
            pushed_ok, push_error = await _push_classic_variation_qty(
                ebay, is_real, listing, product, changed, state)
        else:
            pushed_ok = await _safe_ebay_update(
                ebay, is_real, listing, quantity_by_sku=changed)
    if pushed_ok:
        # Bei aktivem Restock-Beweis-Filter ist desired nur eine TEILMENGE —
        # den Spiegel dann mergen statt ueberschreiben, sonst verliert er die
        # Null-Markierungen der gefilterten SKUs (und der naechste Lauf wuerde
        # sie als "unbekannt" wieder hochsetzen).
        listing.variant_stock = desired if restock_ok else {**prev, **desired}
        flag_modified(listing, "variant_stock")
    # Whole-Listing-Flags NUR bei erfolgreichem Push aendern (Kohaerenz mit dem echten
    # eBay-Zustand). Schlaegt der Push fehl, bleiben supplier_in_stock/quantity_available/
    # variant_stock unveraendert -> der naechste Lauf versucht es erneut, ohne dass die DB
    # einen falschen Zwischenzustand ("ausverkauft" ohne genullte Menge) zeigt.
    # monitor_status setzt der Aufrufer (die Repricing-Stufe wuerde ihn sonst ueberschreiben).
    all_zero = bool(desired) and all(q == 0 for q in desired.values())
    if pushed_ok:
        if all_zero:
            listing.supplier_in_stock = False
            listing.quantity_available = 0
        elif desired:
            # desired kann durch den Restock-Beweis-Filter leer sein — dann die
            # Whole-Listing-Flags NICHT anfassen (kein Beweis = kein "lieferbar").
            listing.supplier_in_stock = True
            if (listing.quantity_available or 0) == 0:
                listing.quantity_available = default_q
    return {"variants": len(state), "oos": oos_unresolved, "changed": len(changed),
            "pushed": pushed_ok, "push_error": push_error,
            "all_oos": all_zero and pushed_ok}


def _supplier_price(scraped):
    """Repricing-Basis: hoechster Varianten-EK (konsistent zum Einheitspreis beim Publish)."""
    return _supplier_price_and_origin(scraped)[0]


def _supplier_price_and_origin(scraped):
    """Repricing-Basis + ob GENAU DIESE Variante aus einem EU-Lager kommt.

    Die Lokal-Entscheidung muss an der SKU haengen, deren Preis auch verwendet
    wird. Bisher stand hier ``variants_have_eu_warehouse``, und das fragt "hat
    IRGENDEINE SKU ein EU-Lager?". Bei gemischten Produkten - eine Variante aus
    Deutschland, der Rest aus China - streicht das den Pauschalzoll auch dann,
    wenn die teuerste und damit kalkulationsrelevante Variante importiert wird.
    Folge: EK zu niedrig, Marge zu hoch, Gefahr zu billig zu verkaufen.

    Uebernommen aus dem Ursprungssystem (Fund dort am 30.08.2026); unsere Kopie
    hatte die alte Fassung behalten.

    Quelle ist bewusst das FRISCHE Scrape-Ergebnis: in die gespeicherten
    Varianten schreibt der Abgleich weiter unten nur ``stock``/``price``, nie
    ``ship_from``. Ueber ``product`` waere der Schalter fuer den Altbestand
    dauerhaft aus.
    """
    from app.integrations.aliexpress_api import has_eu_warehouse

    variants = getattr(scraped, "variants", None) or {}
    skus = [v for v in (variants.get("skus") or []) if isinstance(v, dict)]
    bester, lokal = None, False
    for v in skus:
        try:
            if v.get("price") is None:
                continue
            preis = float(v["price"])
        except (TypeError, ValueError):
            continue
        if bester is None or preis > bester:
            bester, lokal = preis, has_eu_warehouse([v.get("ship_from")])
    if bester is None:
        # Kein Variantenpreis -> Produktpreis. Der gehoert zu keiner einzelnen
        # Variante, deshalb nur lokal, wenn ALLE SKUs aus der EU kommen.
        return scraped.price_cny, bool(skus) and all(
            has_eu_warehouse([v.get("ship_from")]) for v in skus)
    return bester, lokal


async def sync_listing(db: Session, *, listing_id: int) -> dict:
    """Ein Listing mit dem Lieferanten abgleichen (EK + Bestand, KEINE Preis-Pushes).

    Abweichungen zwischen Soll- und Ist-Preis werden als monitor_status
    "price_changed" markiert und im Preis-Check manuell freigegeben.
    """
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ValueError("Listing nicht gefunden")
    product = db.get(Product, listing.product_id) if listing.product_id else None
    if product is None:
        raise ValueError("Kein Product zur Listing")

    settings = get_settings()
    ae = get_aliexpress_client()
    ebay, is_real = _push_client(settings)
    from app.services.golive_service import _usable_variants, variant_stock_state
    # Importierte Klassik-Listings haben oft KEINE SKU, aber eine Item-ID -> auch pushen
    is_active = (listing.listing_status == "active"
                 and bool(listing.ebay_sku or listing.ebay_item_id))

    result = {
        "listing_id": listing_id,
        "action": "none",
        "in_stock": True,
        "old_price_eur": float(listing.price_eur) if listing.price_eur is not None else None,
        "new_price_eur": float(listing.price_eur) if listing.price_eur is not None else None,
    }

    with task_log(db, task_type="monitor", reference_id=listing_id) as tl:
        # 1) Lieferanten-Daten neu lesen (Preis/Bestand). Fehler => Bestands-Annahme OOS.
        out_of_stock = False
        scraped = None
        try:
            scraped = await ae.scrape_product(product.aliexpress_url)
            out_of_stock = not getattr(scraped, "in_stock", True)
        except (OutOfStockError, ProductNotFoundError):
            out_of_stock = True

        # 1b) AUSFALL-FAILOVER: Hauptquelle tot, aber Ausweich-Slot lieferbar ->
        #     automatisch umschalten statt eBay-Menge auf 0 zu setzen.
        audit_extra: dict = {}
        if out_of_stock:
            try:
                from app.services import supplier_service
                switched, scraped2 = await supplier_service.failover_primary(
                    db, listing=listing, product=product)
                if switched:
                    scraped = scraped2
                    out_of_stock = not getattr(scraped, "in_stock", True)
                    audit_extra["failover"] = product.aliexpress_id
                    result["failover"] = product.aliexpress_id
            except Exception as exc:  # noqa: BLE001 – Failover ist best effort
                logger.warning("failover failed", extra={"listing_id": listing_id,
                                                         "error": str(exc)[:120]})

        # 2) Bestands-Sync. Alle DB-Aenderungen werden gesammelt und am Ende EINMAL
        #    committet; eBay-Fehler markieren nur monitor_status='error' (kein Crash).
        if out_of_stock:
            result.update(action="out_of_stock", in_stock=False)
            listing.supplier_in_stock = False
            listing.monitor_status = "out_of_stock"
            if (listing.quantity_available or 0) != 0:
                # Lokale Menge erst NACH erfolgreichem eBay-Push nullen – sonst haelt
                # die Guard oben den fehlgeschlagenen Push fuer erledigt (kein Retry).
                pushed = (not is_active) or await _safe_ebay_update(ebay, is_real,
                                                                    listing, quantity=0)
                if pushed:
                    listing.quantity_available = 0
                    # variant_stock spiegeln: der Whole-OOS-Push nullt ALLE Varianten. Ohne
                    # das Spiegeln wuerde der Recovery-Lauf Varianten, die vorher mit Menge
                    # gespeichert waren, nicht wieder hochsetzen (blieben auf eBay auf 0).
                    try:
                        from sqlalchemy.orm.attributes import flag_modified
                        vs = variant_stock_state(listing, product)
                        if vs:
                            listing.variant_stock = {st["ebay_sku"]: 0 for st in vs}
                            flag_modified(listing, "variant_stock")
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    listing.monitor_status = "error"   # naechster Lauf versucht es erneut
            listing.last_monitored_at = _now()
            tl.result_data = {"action": "out_of_stock", **audit_extra}
            db.commit()
            return result

        # RESTOCK-BEWEIS (Review-Fund 16.08.): Mengen werden nur mit POSITIVEM
        # Bestands-Signal erhoeht. Eine degradierte Antwort (SKUs ohne Bestands-
        # felder) darf zwar nicht mehr faelschlich nullen (fail-open in parse_product),
        # aber auch kein womoeglich wirklich ausverkauftes Listing reaktivieren.
        # Produkte ganz OHNE SKU-Liste haben nie Bestandsfelder – dort bleibt der
        # erfolgreiche Abruf (kein OOS/NotFound) wie bisher der Beweis.
        _skus_scraped = ((getattr(scraped, "variants", None) or {}).get("skus") or [])
        restock_ok = (not _skus_scraped) or bool(getattr(scraped, "stock_reported", True))

        # Wieder verfuegbar: Sichtbestand zuruecksetzen (Standardmenge, nicht 1).
        # Bei MULTIVARIANTEN-Listings NICHT pauschal alle Varianten auf Default pushen
        # (das machte ausverkaufte Varianten kurz sichtbar) – der Per-Varianten-Sync unten
        # setzt Menge + Flags gezielt je Variante.
        is_multivariant = bool(product.variants) and bool(_usable_variants(product)[0])
        if not listing.supplier_in_stock and not is_multivariant and restock_ok:
            if (listing.quantity_available or 0) == 0:
                pushed = (not is_active) or await _safe_ebay_update(
                    ebay, is_real, listing, quantity=settings.default_listing_quantity)
                if pushed:
                    listing.supplier_in_stock = True
                    listing.quantity_available = settings.default_listing_quantity
                else:
                    listing.monitor_status = "error"   # Flag bleibt False -> Retry
            else:
                listing.supplier_in_stock = True

        # 2b) Varianten-EKs auffrischen: frische Preise per AliExpress-SKU-Id in die
        #     GESPEICHERTEN Varianten mergen (Reihenfolge/Namen bleiben -> SKU-Mapping stabil).
        variant_prices: list[dict] = []
        vres: dict = {}
        if product.variants and isinstance(product.variants, dict):
            from app.services.golive_service import _stock_num
            fresh = {str(v.get("id")): v for v in ((getattr(scraped, "variants", None) or {}).get("skus") or [])}
            touched = False
            for v in (product.variants.get("skus") or []):
                f = fresh.get(str(v.get("id")))
                if not f:
                    continue
                # Bestand IMMER auffrischen (auch wenn der Preis gleich blieb) – sonst
                # bleibt eine ausverkaufte Variante mit altem Bestand stehen und der
                # Per-Varianten-OOS-Sync greift nie. NUMERISCH vergleichen ("100" == 100),
                # sonst loest jeder Lauf einen Scheinschreib-/Commit aus.
                fk, fv = _stock_num(f.get("stock"))
                if fk:
                    vk, vv = _stock_num(v.get("stock"))
                    if (not vk) or fv != vv:
                        v["stock"] = fv                 # normiert als int ablegen
                        touched = True
                if f.get("price") is not None and f.get("price") != v.get("price"):
                    v["price"] = f["price"]
                    touched = True
            if touched:
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(product, "variants")
            from app.services.golive_service import compute_variant_prices
            variant_prices = compute_variant_prices(listing, product, settings)
            # PER-VARIANTE Bestands-Sync: einzelne ausverkaufte Varianten auf eBay auf 0
            # setzen (statt das ganze Listing), lieferbare zurueck – beruecksichtigt
            # verknuepfte Ausweich-Quellen. Best effort; Fehler brechen den Lauf nicht ab.
            try:
                vres = await _sync_variant_stock(db, ebay, is_real, listing, product,
                                                 settings, restock_ok=restock_ok)
                if vres.get("changed"):
                    result["variant_stock"] = vres
                    audit_extra["variant_stock"] = vres
            except Exception as exc:  # noqa: BLE001
                logger.warning("variant stock sync failed",
                               extra={"listing_id": listing_id, "error": str(exc)[:120]})

        # 3) Repricing (Listing kann eigenen Zusatzgewinn-% tragen, sonst Config-Default)
        profit_pct_override = float(listing.markup_pct) if listing.markup_pct is not None else None
        # Echte AliExpress-Versandkosten aus dem Listing verwenden (statt Pauschale), damit der
        # Monitor den korrekt kalkulierten EK nicht bei jedem Lauf mit der Schaetzung ueberschreibt.
        sov = float(listing.supplier_ship_eur) if listing.supplier_ship_eur is not None else None
        # EU-Lager mitgeben, sonst rechnet der Monitor China-Versand UND die
        # Zollpauschale (3,57 EUR) auf Ware, die aus Deutschland kommt. Der Import
        # (product_service:349) macht das richtig, dieser Aufruf tat es nicht - und
        # ueberschreibt cost_eur bei jedem Lauf.
        #
        # Folge im Testbestand vom 28.08.2026: alle 20 Entwuerfe bekamen einen um
        # 3,57 EUR zu hohen EK, waehrend der Preis unveraendert blieb. Die Marge sah
        # dadurch nach 4 % aus statt nach 20 %, und jedes Listing wurde als
        # "price_changed" gemeldet - zwanzig Fehlalarme, die zu unnoetigen
        # Preiserhoehungen gefuehrt haetten.
        # Preis UND Lagerort aus einer Hand: beides gehoert zu DERSELBEN Variante.
        # Vorher stand hier variants_have_eu_warehouse(scraped) - "hat irgendeine
        # SKU ein EU-Lager?". Bei gemischten Produkten fiel dadurch der Zoll weg,
        # obwohl der Preis von einer China-Variante stammte.
        ek_cny, ist_lokal = _supplier_price_and_origin(scraped)
        # KATEGORIE-GENAUE Gebuehr statt Pauschale. Ohne sie rechnet der Abgleich mit
        # dem Standardsatz (12 % Provision + 10 % Anzeige), waehrend der gespeicherte
        # Preis mit dem Satz DIESER Kategorie entstand. Bei Kleidung sind beide
        # zufaellig gleich; bei einer 7-%-Kategorie (Computer, Haushaltsgeraete)
        # laufen sie auseinander, und jeder Lauf meldet "price_changed", obwohl sich
        # beim Lieferanten nichts geaendert hat. Im Ursprungssystem loest das der
        # Zweig ueber upload_breakdown_from_cny; bei uns rechnen beide Modelle seit
        # "ein Preismodell statt zwei" ohnehin gleich - es fehlte nur die Kategorie.
        breakdown = pricing.price_from_cny(
            ek_cny, settings=settings, profit_pct=profit_pct_override,
            fee_pct=pricing.effective_fee_pct_for_listing(listing, settings=settings),
            ship_override=sov, local=ist_lokal,
            min_price_eur=float(listing.min_price_eur) if listing.min_price_eur is not None else None,
            max_price_eur=float(listing.max_price_eur) if listing.max_price_eur is not None else None,
        )
        new_repr = (max(p["price_eur"] for p in variant_prices)
                    if variant_prices else breakdown.rounded_price_eur)
        old_price = float(listing.price_eur) if listing.price_eur is not None else None
        changed = (old_price is None) or (abs(new_repr - old_price) >= 0.01)

        # KEIN Auto-Repricing (Nutzer-Entscheidung 2026-07-03): Die Ueberwachung
        # pusht NIEMALS selbststaendig Preise auf eBay. Sie aktualisiert nur den
        # EK und markiert Abweichungen ("price_changed") -> die erscheinen im
        # Preis-Check und werden dort MANUELL freigegeben (Alle sicheren
        # Anhebungen). Der Ausverkauft-Schutz (OOS -> Menge 0) bleibt aktiv.
        listing.cost_eur = breakdown.cost_eur
        if changed:
            listing.monitor_status = "price_changed"
            result.update(action="price_drift", target_price_eur=new_repr)
        else:
            listing.monitor_status = "ok"
        # Sind ALLE Varianten ausverkauft (ohne lieferbare Ausweich-Quelle), zaehlt das
        # Listing als ausverkauft – nach der Repricing-Statuszuweisung setzen, damit sie
        # den OOS-Status nicht ueberschreibt.
        if vres.get("changed") and not vres.get("pushed", True):
            listing.monitor_status = "error"        # Per-Varianten-Push fehlgeschlagen -> Retry
        elif vres.get("all_oos") and listing.monitor_status != "error":
            listing.monitor_status = "out_of_stock"
            result.update(action="out_of_stock", in_stock=False)

        listing.last_monitored_at = _now()
        tl.result_data = {"action": result["action"],
                          "price_eur": breakdown.rounded_price_eur, **audit_extra}
        db.commit()

    return result


async def resync_listing_variant_stock(db: Session, *, listing_id: int) -> dict:
    """Nur den Per-Varianten-Bestand eines Listings sofort mit eBay abgleichen.

    Wird nach dem Verknuepfen/Loesen einer Ausweich-Quelle je Variante aufgerufen, damit
    die Reaktivierung (Menge zurueck) bzw. das Nullen unmittelbar auf eBay wirkt statt
    erst beim naechsten Monitoring-Lauf. Kein Preis-Push."""
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ValueError("Listing nicht gefunden")
    if getattr(listing, "sales_hold", False):
        # Bewusst pausierte Artikel (Verkaufsstopp, Menge 0) nicht per Abgleich reaktivieren
        # — derselbe Skip wie im 6h-Monitor-Lauf.
        return {"variants": 0, "changed": 0, "skipped": "sales_hold"}
    product = db.get(Product, listing.product_id) if listing.product_id else None
    if product is None:
        return {"variants": 0, "changed": 0}
    settings = get_settings()
    ebay, is_real = _push_client(settings)
    res = await _sync_variant_stock(db, ebay, is_real, listing, product, settings)
    if res.get("all_oos") and listing.monitor_status != "error":
        listing.monitor_status = "out_of_stock"
    elif not res.get("all_oos") and listing.monitor_status == "out_of_stock":
        listing.monitor_status = "ok"
    listing.last_monitored_at = _now()
    db.commit()
    return res


async def run_monitoring(db: Session, *, only_auto: bool = True) -> dict:
    """Aktive + Draft-Listings abgleichen. Liefert Aggregat-Zaehler fuers Dashboard.

    Drafts werden bewusst mitgenommen: ihr Repricing aktualisiert nur den gespeicherten
    Preis (eBay-Calls sind per ``is_active`` deaktiviert), sodass der Preis beim spaeteren
    Live-Stellen aktuell ist. Beendete Listings bleiben aussen vor.

    Auto-Repricing existiert nicht mehr: Die Ueberwachung erkennt Abweichungen
    ("price_drift") und den Bestand (OOS -> Menge 0), Preis-Aenderungen macht
    ausschliesslich der Nutzer im Preis-Check. Unbestaetigte Verdachts-Matches
    werden uebersprungen (keine Daten auf Basis eines womoeglich falschen Produkts).
    """
    from app.services.listing_match_service import match_is_suspect
    settings = get_settings()
    # Nur Listings mit AliExpress-Produkt (importierte AutoDS-Listings ohne
    # product_id koennen nicht gegen den Lieferanten geprueft werden).
    stmt = select(Listing).where(Listing.listing_status.in_(["active", "draft"]),
                                 Listing.product_id.isnot(None))
    # Studio-Angebote haben keinen AliExpress-Lieferanten. Ohne Riegel koennte eine
    # versehentlich hinterlegte Quelle ihre Menge auf null ziehen - das Angebot waere
    # still ausverkauft. Laeuft alle sechs Stunden und ist nicht abschaltbar.
    from app.studio import exclude_studio
    stmt = exclude_studio(stmt, db)
    if only_auto:
        stmt = stmt.where(Listing.auto_reprice.is_(True))
    listings = db.scalars(stmt).all()

    counters = {"checked": 0, "price_drift": 0, "out_of_stock": 0, "errors": 0,
                "skipped_suspect": 0, "held": 0}
    for lst in listings:
        # Manuelle Verkaufs-Sperre: NIE anfassen (kein Scrape, kein Restock-Push). So bleibt
        # ein bewusst pausiertes Listing (eBay-Menge 0) dauerhaft pausiert, statt beim naechsten
        # Lauf wieder auf Standardmenge gesetzt zu werden.
        if getattr(lst, "sales_hold", False):
            counters["held"] += 1
            continue
        product = db.get(Product, lst.product_id)
        if product is not None and match_is_suspect(lst, product, settings):
            counters["skipped_suspect"] += 1
            continue
        counters["checked"] += 1
        try:
            res = await sync_listing(db, listing_id=lst.id)
            if res["action"] == "price_drift":
                counters["price_drift"] += 1
            elif res["action"] == "out_of_stock":
                counters["out_of_stock"] += 1
        except Exception as exc:  # noqa: BLE001 – Einzel-Fehler darf den Lauf nicht stoppen
            counters["errors"] += 1
            logger.error("monitoring failed", extra={"listing_id": lst.id, "error": str(exc)})
        # Rate-Limit AliExpress: bei ~300 verknuepften Listings sonst 429-Kaskade
        if len(listings) > 10:
            await asyncio.sleep(1.0)

    # Report frisch halten: der Preis-Check/Cockpit liest die gecachten Zeilen (inkl. der
    # monitor_oos/fully_out-Flags). Ohne Neubau blieben verschwundene/ausverkaufte Listings
    # bis zum naechsten manuellen „Synchronisieren" unsichtbar. Billig (kein Scrape); ein
    # Fehler hier darf den Monitoring-Lauf NICHT kippen.
    try:
        from app.services.listing_match_service import rebuild_reprice_report
        rebuild_reprice_report(db)
    except Exception:  # noqa: BLE001
        logger.warning("monitoring: reprice-report-Neubau fehlgeschlagen", exc_info=True)

    return {
        "task_id": f"monitor_{_now().strftime('%Y%m%d_%H%M')}",
        **counters,
        "status": "completed",
    }


def monitor_status(db: Session) -> dict:
    """GET-Status: Pro Listing Preis/Kosten/Marge/Bestand + Aggregat fuers Dashboard."""
    listings = db.scalars(
        select(Listing).where(Listing.listing_status.in_(["active", "draft"]))
    ).all()
    items = []
    out_of_stock = price_alerts = 0
    for l in listings:
        price = float(l.price_eur) if l.price_eur is not None else None
        cost = float(l.cost_eur) if l.cost_eur is not None else None
        # Echter Gewinn: VK - eBay-Gebuehren (22% + fix) - EK, NICHT nur VK - EK
        profit = pricing.profit_at_price(price, cost)
        if l.monitor_status == "out_of_stock":
            out_of_stock += 1
        if l.monitor_status == "price_changed":
            price_alerts += 1
        items.append({
            "listing_id": l.id,
            "title": l.title_seo,
            "sku": l.ebay_sku,
            "price_eur": price,
            "cost_eur": cost,
            "profit_eur": profit,
            "auto_reprice": bool(l.auto_reprice),
            "supplier_in_stock": bool(l.supplier_in_stock),
            "monitor_status": l.monitor_status,
            "sales_hold": bool(getattr(l, "sales_hold", False)),
            "hold_reason": getattr(l, "hold_reason", None),
            "last_monitored_at": l.last_monitored_at,
        })
    return {
        "items": items,
        "stats": {
            "monitored": len(items),
            "out_of_stock": out_of_stock,
            "price_alerts": price_alerts,
            "held": sum(1 for i in items if i["sales_hold"]),
            "auto_reprice_on": sum(1 for i in items if i["auto_reprice"]),
        },
    }
