"""eBay-Import/-Sync: echte aktive Listings (Trading API) + Verkaeufe (getOrders) -> DB.

Liest bewusst ueber einen ECHTEN eBay-Client (unabhaengig von MOCK_EBAY), damit man
den realen Shop-Bestand ins Dashboard holen kann, waehrend Schreib-/Publish-Flows im
sicheren Mock bleiben. Upsert ist idempotent (ebay_item_id bzw. orderId).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Listing, Sale
from app.retry import PersistentError
from app.services.common import task_log

logger = logging.getLogger("app.services.ebay_import")


def _real_ebay():
    from app.integrations.ebay import RealEbayClient
    return RealEbayClient(get_settings())


async def sync_ebay_live_prices(db: Session, *, ebay=None, limit: int = 2000,
                                only_ids=None) -> dict:
    """ECHTE Live-eBay-Preise je Variante abgleichen (GetItem) und am Listing speichern.

    QUELLE DER WAHRHEIT fuer die Cockpit-Marge/Drift-Warnung. Read-only von eBay (kein Push).
    Fuellt ``ebay_live_prices`` = {ebay_sku: preis} + ``ebay_price_synced_at``. Einzel-Listings
    (keine Variationen) -> Basis-SKU = item current_price. Best-effort: ein Listing-Fehler stoppt
    den Rest nicht (Fund 17.07.: interner Preis wich vom echten eBay-Preis ab)."""
    ebay = ebay or _real_ebay()
    q = select(Listing).where(Listing.listing_status == "active",
                              Listing.ebay_item_id.isnot(None))
    if only_ids:
        q = q.where(Listing.id.in_([int(x) for x in only_ids]))
    # ROTATION statt Hungern: nie-/laengst-abgeglichene zuerst. limit=2000 deckt den ganzen
    # aktiven Bestand (~615) pro Lauf ab; die Reihenfolge bleibt als Sicherheitsnetz, falls der
    # Shop das limit je uebersteigt, damit kein Schwanz dauerhaft auf der internen Scheinmarge
    # laeuft. NULLs (nie geprueft) zuerst.
    q = q.order_by(Listing.ebay_price_synced_at.is_(None).desc(),
                   Listing.ebay_price_synced_at.asc())
    listings = list(db.scalars(q).all())[:max(1, int(limit))]
    checked = updated = drift = 0
    errors = []
    empty = []                                   # GetItem lieferte keinen Preis -> alter Wert bleibt
    now = datetime.now(timezone.utc)
    for l in listings:
        try:
            info = await ebay.get_item_price_info(l.ebay_item_id)
        except Exception as exc:  # noqa: BLE001 – ein Listing darf den Lauf nicht stoppen
            errors.append({"listing_id": l.id, "error": str(exc)[:140]})
            continue
        checked += 1
        # Preis je Variante speichern – UNTER ZWEI Keys, damit die Anzeige je Variante immer den
        # richtigen Preis findet: (1) eBay-SKU (falls == {base}-V{i}), (2) Merkmals-WERT-Signatur
        # ("sig:…"), die auch bei abweichenden SKUs zur Variante passt (Fund 17.07.: sonst zeigte
        # der Dialog fuer ALLE Varianten den niedrigsten Preis).
        from app.services.listing_match_service import _value_sig
        prices = {}
        for v in (info.get("variations") or []):
            pr = v.get("price")
            if not pr:
                continue
            try:
                p = round(float(pr), 2)
            except (TypeError, ValueError):
                continue
            if v.get("sku"):
                prices[str(v["sku"])] = p
            sig = _value_sig([val for _n, val in (v.get("specifics") or [])])
            if sig:
                key = "sig:" + sig
                # KONSERVATIV bei kollidierender Signatur (mehrere Variationen mit denselben
                # Merkmals-WERTEN, z.B. permutierte 2-Achsen): den NIEDRIGSTEN Preis behalten – eine
                # mehrdeutige Signatur darf NIE einen zu hohen, Verlust versteckenden Preis anzeigen.
                prices[key] = p if key not in prices else min(prices[key], p)
        if not prices and info.get("current_price"):
            try:
                prices[l.ebay_sku or f"AE-{l.id}"] = round(float(info["current_price"]), 2)
            except (TypeError, ValueError):
                pass
        if not prices:
            # KEIN Ueberschreiben mit None: ein leeres/partielles GetItem-Ergebnis (blanke SKU,
            # transiente Success-Antwort ohne Preis) darf einen ZUVOR bestaetigten echten Preis
            # – und damit die Drift-Warnung – NICHT wieder loeschen (sonst versteckt genau EIN
            # flaky Sync wieder einen realen Verlust). Nur echte Preise werden geschrieben.
            empty.append(l.id)
            continue
        l.ebay_live_prices = prices
        l.ebay_price_synced_at = now
        updated += 1
        try:                                     # Drift (nur Info): interner Preis vs. echter Min-eBay-Preis
            if l.price_eur is not None:
                lo = min(prices.values())
                if abs(float(l.price_eur) - lo) >= max(0.5, 0.02 * lo):
                    drift += 1
        except (TypeError, ValueError):
            pass
    db.commit()
    return {"checked": checked, "updated": updated, "drift": drift,
            "empty": len(empty), "errors": errors[:20]}


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def import_listings(db: Session, *, ebay=None, max_items: int = 10000) -> dict:
    """Aktive eBay-Listings holen und als Listing-Datensaetze upserten."""
    ebay = ebay or _real_ebay()
    rows = await ebay.get_active_listings(max_items=max_items)
    created = updated = 0
    for r in rows:
        item_id = r.get("item_id")
        if not item_id:
            continue
        listing = db.scalar(select(Listing).where(Listing.ebay_item_id == str(item_id)))
        price = _dec(r.get("price"))
        qty = None
        try:
            qty = int(r["quantity"]) if r.get("quantity") not in (None, "") else None
        except (ValueError, TypeError):
            qty = None
        title = r.get("title") or f"eBay-Artikel {item_id}"
        sold = None
        try:
            sold = int(r["quantity_sold"]) if r.get("quantity_sold") not in (None, "") else None
        except (ValueError, TypeError):
            sold = None
        start_dt = _parse_dt(r.get("start_time"))   # echtes eBay-Einstelldatum
        if listing is None:
            listing = Listing(
                ebay_item_id=str(item_id), ebay_sku=r.get("sku"),
                title_seo=title[:255], description="(von eBay importiert)",
                listing_status="active", price_eur=price, quantity_available=qty,
                category_id=r.get("category_id"), category_name=r.get("category_name"),
                monitor_status="ok",
                supplier_in_stock=True, image_url=r.get("gallery_url"),
                sales_total=sold, listing_start_date=start_dt,
            )
            db.add(listing)
            created += 1
        else:
            listing.title_seo = title[:255]
            listing.listing_status = "active"
            if price is not None:
                listing.price_eur = price
            if qty is not None:
                listing.quantity_available = qty
            if r.get("sku"):
                listing.ebay_sku = r.get("sku")
            if r.get("gallery_url"):
                listing.image_url = r.get("gallery_url")
            if sold is not None:
                listing.sales_total = sold
            if start_dt is not None:
                listing.listing_start_date = start_dt
            # Kategorie nachtragen (fuer kategorie-genaue eBay-Gebuehren) – nur fuellen,
            # nie ueberschreiben.
            if r.get("category_id") and not listing.category_id:
                listing.category_id = r.get("category_id")
            if r.get("category_name") and not listing.category_name:
                listing.category_name = r.get("category_name")
            updated += 1

    # RECONCILE: DB-Listings, die als "active" gelten, aber NICHT mehr in eBays
    # aktiver Liste stehen, sind auf eBay bereits beendet/ausverkauft -> Status
    # angleichen, damit die Zaehler (Overview/Optimierung) stimmen. Das ist KEIN
    # App-seitiges Auto-Beenden LIVE-Listings, sondern das Spiegeln des echten
    # eBay-Zustands. Guard: nur wenn eBay ueberhaupt Daten geliefert hat.
    reconciled: list[int] = []
    live_ids = {str(r.get("item_id")) for r in rows if r.get("item_id")}
    # VOLLSTAENDIGKEITS-WACHE (Fund 17.07.): der destruktive 'ended'-Abgleich laeuft NUR, wenn der
    # eBay-Fetch nachweislich bis zur letzten Seite KOMPLETT war (explizites Signal vom Client,
    # nicht aus einer Mengen-Heuristik geraten). Ein gekappter ODER auf einer leeren Zwischenseite
    # abgebrochener Fetch ist unvollstaendig -> Reconcile pausiert, sonst wuerden die nicht geholten
    # aber LIVE Listings faelschlich auf 'ended' gesetzt (genau die Ursache von 615->500).
    # FAIL-CLOSED: fehlt das Signal (unbekannter/kuenftiger Client), NICHT reconcilen – ein
    # destruktiver 'ended'-Abgleich darf nie auf einer Annahme laufen. Der echte Client setzt es stets.
    fetch_complete = bool(getattr(ebay, "_last_active_fetch_complete", False))
    skipped_reason = None if fetch_complete else "incomplete_fetch"
    if live_ids and fetch_complete:
        stale = db.scalars(select(Listing).where(
            Listing.listing_status == "active",
            Listing.ebay_item_id.isnot(None),
            Listing.ebay_item_id.notin_(live_ids))).all()
        for l in stale:
            l.listing_status = "ended"
            reconciled.append(l.id)
    elif live_ids and not fetch_complete:
        logger.warning("import_listings: Reconcile UEBERSPRUNGEN – eBay-Fetch unvollstaendig "
                       "(nur %s Listings geholt, letzte Seite nicht erreicht); nichts auf 'ended' gesetzt.",
                       len(rows))
    db.commit()
    return {"fetched": len(rows), "created": created, "updated": updated,
            "reconciled_ended": len(reconciled), "reconciled_ids": reconciled,
            "reconcile_skipped": skipped_reason}


async def backfill_start_dates(db: Session, *, ebay=None, max_items: int = 2000) -> dict:
    """Echtes eBay-Einstelldatum (ListingDetails/StartTime) fuer bestehende aktive
    Listings nachtragen – OHNE andere Felder anzufassen.

    Fuer Bestandsdaten, die vor der StartTime-Erfassung importiert wurden und deshalb
    nur das Import-Datum als Naeherung tragen. Idempotent; danach zeigt „online seit"
    das echte Datum und die Optimieren-Liste sortiert korrekt nach Alter.
    """
    ebay = ebay or _real_ebay()
    rows = await ebay.get_active_listings(max_items=max_items)
    updated = 0
    for r in rows:
        item_id = r.get("item_id")
        st = _parse_dt(r.get("start_time"))
        if not item_id or st is None:
            continue
        l = db.scalar(select(Listing).where(Listing.ebay_item_id == str(item_id)))
        if l is not None and l.listing_start_date != st:
            l.listing_start_date = st
            updated += 1
    db.commit()
    return {"fetched": len(rows), "updated": updated}


async def backfill_categories(db: Session, *, ebay=None, limit: int = 800,
                              rate_s: float = 0.35, force: bool = False) -> dict:
    """Fehlende eBay-Kategorie (ID + Name) je aktivem Listing per GetItem nachtragen.

    WARUM: Der Bulk-Abruf GetMyeBaySelling liefert ``PrimaryCategory`` NICHT mit (siehe
    ``RealEbayClient.get_item_category``). Importierte Listings haben deshalb keinen
    ``category_name`` – und ohne den faellt ``pricing.commission_pct()`` auf die
    Default-Provision zurueck. Die VORWAERTS-Kalkulation (Upload-Preis, Cockpit-Marge)
    rechnet dann z. B. Schmuck mit 12 % statt der echten 16 %.

    Rein LESEND auf eBay-Seite – aendert nur unsere DB, nie ein Listing.
    """
    import asyncio
    ebay = ebay or _real_ebay()
    stmt = select(Listing).where(Listing.listing_status == "active",
                                 Listing.ebay_item_id.isnot(None))
    if not force:
        stmt = stmt.where(Listing.category_name.is_(None))
    todo = db.scalars(stmt.limit(max(1, int(limit)))).all()
    scanned = updated = errors = 0
    for l in todo:
        try:
            cat_id, cat_name = await ebay.get_item_category(l.ebay_item_id)
            if cat_name:
                l.category_name = cat_name
                if cat_id:
                    l.category_id = str(cat_id)
                updated += 1
            scanned += 1
            if scanned % 25 == 0:
                db.commit()
        except Exception as exc:  # noqa: BLE001 – ein Fehler stoppt den Lauf nicht
            errors += 1
            logger.warning("category backfill failed", extra={"item": l.ebay_item_id,
                                                              "error": str(exc)[:120]})
        await asyncio.sleep(rate_s)
    db.commit()
    remaining = db.scalar(select(func.count()).select_from(Listing).where(
        Listing.listing_status == "active", Listing.ebay_item_id.isnot(None),
        Listing.category_name.is_(None))) or 0
    return {"scanned": scanned, "updated": updated, "errors": errors,
            "remaining": int(remaining), **category_fee_overview(db)}


def category_fee_overview(db: Session) -> dict:
    """Welche Kategorien haben unsere aktiven Listings – und weicht die kategoriegenaue
    eBay-Provision vom Default ab?

    Beantwortet die Frage „betrifft die falsche Gebuehr nur Schmuck oder auch andere?".
    Reine Auswertung des IST-Zustands (keine Hochrechnung, kein Euro-Betrag): die
    Prozentpunkte sind die Differenz, mit der die Vorwaerts-Kalkulation danebenlag,
    solange die Kategorie fehlte.
    """
    from app.services import pricing
    rows = db.execute(
        select(Listing.category_name, func.count())
        .where(Listing.listing_status == "active")
        .group_by(Listing.category_name)).all()
    default = pricing.commission_pct(None)
    by_category: dict[str, int] = {}
    without = 0
    for name, n in rows:
        if not name:
            without += int(n)
            continue
        top = str(name).split(":")[0].strip()
        by_category[top] = by_category.get(top, 0) + int(n)
    out = []
    for top, n in by_category.items():
        rate = pricing.commission_pct(top)
        out.append({"category": top, "listings": n,
                    "commission_pct": round(rate * 100, 1),
                    "diff_to_default_pp": round((rate - default) * 100, 1)})
    out.sort(key=lambda r: (-abs(r["diff_to_default_pp"]), -r["listings"]))
    return {"default_commission_pct": round(default * 100, 1),
            "listings_without_category": without,
            "affected_listings": sum(r["listings"] for r in out if r["diff_to_default_pp"]),
            "by_category": out}


async def scan_shipping_policies(db: Session, *, ebay=None, force: bool = False,
                                 rate_s: float = 0.35) -> dict:
    """Versand-Policy jedes aktiven Listings lesen (Trading GetItem) und speichern.

    Grundlage fuer den 3€-Zuschlag-Filter im Preis-Check: welche Artikel haengen
    noch auf 'Versand 7 Tage bearbeitung' (3€) statt 'Kostenloser Versand'?
    """
    import asyncio
    ebay = ebay or _real_ebay()
    policies = {p["policy_id"]: p for p in await ebay.list_fulfillment_policies()}
    stmt = select(Listing).where(Listing.listing_status == "active",
                                 Listing.ebay_item_id.isnot(None))
    if not force:
        stmt = stmt.where(Listing.shipping_policy_id.is_(None))
    listings = db.scalars(stmt).all()
    scanned = errors = 0
    for l in listings:
        try:
            prof = await ebay.get_item_shipping_profile(l.ebay_item_id)
            if prof.get("profile_id"):
                l.shipping_policy_id = prof["profile_id"]
                l.shipping_policy_name = (prof.get("profile_name")
                                          or (policies.get(prof["profile_id"]) or {}).get("name"))
            scanned += 1
            if scanned % 25 == 0:
                db.commit()
        except Exception as exc:  # noqa: BLE001 – einzelner Fehler stoppt den Scan nicht
            errors += 1
            logger.warning("shipping scan failed", extra={"item": l.ebay_item_id,
                                                          "error": str(exc)[:120]})
        await asyncio.sleep(rate_s)
    db.commit()
    surcharge_ids = {pid for pid, p in policies.items() if p.get("has_surcharge")}
    all_active = db.scalars(select(Listing).where(Listing.listing_status == "active")).all()
    with_surcharge = sum(1 for l in all_active if l.shipping_policy_id in surcharge_ids)
    return {"scanned": scanned, "errors": errors, "surcharge_listings": with_surcharge,
            "surcharge_policies": [policies[p]["name"] for p in surcharge_ids]}


_POLICY_NAME_CACHE: dict[str, str] = {}


async def _policy_name(ebay, policy_id: str) -> str | None:
    """Policy-Name mit Prozess-Cache (im Bulk sonst 1 Account-API-Call je Listing)."""
    if policy_id not in _POLICY_NAME_CACHE:
        try:
            for p in await ebay.list_fulfillment_policies():
                _POLICY_NAME_CACHE[str(p["policy_id"])] = p.get("name") or ""
        except Exception:  # noqa: BLE001 – Name ist Kosmetik, nie den Wechsel gefaehrden
            return None
    return _POLICY_NAME_CACHE.get(policy_id)


async def switch_listing_shipping(db: Session, *, listing_id: int,
                                  policy_id: str | None = None, ebay=None) -> dict:
    """Versand-Policy EINES Listings wechseln (Default: die gepinnte Kostenlos-Policy)."""
    from app.config import get_settings
    ebay = ebay or _real_ebay()
    listing = db.get(Listing, listing_id)
    if listing is None or not listing.ebay_item_id:
        raise ValueError("Listing nicht gefunden oder nicht live")
    target = str(policy_id or get_settings().ebay_fulfillment_policy_id)
    if not target:
        raise ValueError("Keine Ziel-Policy (EBAY_FULFILLMENT_POLICY_ID fehlt)")
    await ebay.revise_item_shipping_profile(listing.ebay_item_id, target)
    # Erfolg SOFORT persistieren – die Namensaufloesung danach ist nur Kosmetik.
    listing.shipping_policy_id = target
    listing.shipping_policy_name = None
    db.commit()
    name = await _policy_name(ebay, target)
    if name:
        listing.shipping_policy_name = name
        db.commit()
    return {"listing_id": listing_id, "shipping_policy_id": target,
            "shipping_policy_name": name}


async def migrate_listing_plus3_free_shipping(db: Session, *, listing_id: int,
                                              delta: float = 3.0, ebay=None) -> dict:
    """3€-Versand-Migration fuer EIN Listing: jede Variante +delta, Versand kostenlos.

    Gesamtsumme fuer den Kaeufer bleibt identisch (Artikel +3€, Versand -3€).
    Ablauf mit Verifikation: Preise +3 je Variante -> READ-BACK-Check -> Policy auf
    Kostenlos. Schlaegt der Policy-Wechsel fehl, werden die Preise ZURUECKGEROLLT
    (kein Zustand 'teurer UND noch 3€ Versand').
    """
    ebay = ebay or _real_ebay()
    listing = db.get(Listing, listing_id)
    if listing is None or not listing.ebay_item_id or listing.listing_status != "active":
        raise ValueError("Listing nicht aktiv/live")
    item_id = str(listing.ebay_item_id)
    free_policy = str(get_settings().ebay_fulfillment_policy_id)

    # 1) Preise erhoehen – Inventory-Listings ueber ihre Offers, Klassik via Trading.
    from app.services.golive_service import _listing_variant_skus
    skus = await _listing_variant_skus(ebay, listing)
    offers = []
    for sku in skus:
        o = await ebay._first_offer_for_sku(sku)
        if o and o.get("offerId"):
            offers.append((sku, o))
    if offers:
        info = await ebay.get_item_price_info(item_id)
        expected = {}
        if info["variations"]:
            expected = {(v.get("sku") or f"_idx{i}"): round(v["price"] + delta, 2)
                        for i, v in enumerate(info["variations"])}
        else:
            expected = {"_single": round((info["current_price"] or 0) + delta, 2)}
        updates = []
        for sku, o in offers:
            try:
                cur = float(((o.get("pricingSummary") or {}).get("price") or {}).get("value"))
            except (TypeError, ValueError):
                cur = None
            target = expected.get(sku) or (cur + delta if cur is not None else None)
            if target is None:
                raise PersistentError(f"Kein Ist-Preis fuer SKU {sku} ermittelbar")
            updates.append({"sku": sku, "offer_id": o["offerId"],
                            "price_eur": round(target, 2)})
        await ebay.bulk_update_price(updates)
        new_max = max(u["price_eur"] for u in updates)
    else:
        r = await ebay.raise_item_price_delta(item_id, delta)
        expected = r["expected"]
        new_max = r["new_max"]

    # 2) READ-BACK: jede Variante muss den Zielpreis zeigen, sonst Abbruch.
    if not await ebay.verify_item_prices_map(item_id, expected):
        raise PersistentError("Preis +3€ nicht verifizierbar – Artikel unveraendert lassen")

    # 3) Versand-Policy auf Kostenlos. Fehler -> Preise zurueckrollen (best effort).
    try:
        await ebay.revise_item_shipping_profile(item_id, free_policy)
    except Exception as exc:
        try:
            await ebay.raise_item_price_delta(item_id, -delta)
        except Exception:  # noqa: BLE001
            logger.error("rollback failed", extra={"item": item_id})
            raise PersistentError(
                f"Policy-Wechsel fehlgeschlagen UND Preis-Rollback fehlgeschlagen – "
                f"Artikel {item_id} manuell pruefen! ({exc})") from exc
        raise PersistentError(f"Policy-Wechsel fehlgeschlagen, Preise zurueckgerollt: {exc}") from exc

    # 4) Lokal persistieren (Preis-Konvention: teuerste Variante).
    listing.price_eur = Decimal(str(round(new_max, 2)))
    listing.shipping_policy_id = free_policy
    listing.shipping_policy_name = await _policy_name(ebay, free_policy)
    db.commit()
    return {"listing_id": listing_id, "item_id": item_id, "variants": len(expected),
            "new_max_eur": new_max, "verified": True}


async def sync_listing_stats(db: Session, *, ebay=None, days: int = 30) -> dict:
    """Performance-Statistik aller aktiven Listings aktualisieren.

    * Verkäufe gesamt: Trading GetMyeBaySelling (QuantitySold, Lebenszeit)
    * Aufrufe 30 Tage: Analytics Traffic-Report (LISTING_VIEWS_TOTAL, 200er-Batches)
    """
    ebay = ebay or _real_ebay()
    listings = db.scalars(select(Listing).where(Listing.listing_status == "active",
                                                Listing.ebay_item_id.isnot(None))).all()
    by_item = {str(l.ebay_item_id): l for l in listings}
    now = datetime.now(timezone.utc)

    sold_updated = 0
    # Obergrenze dynamisch: alle bekannten aktiven Listings + Puffer fuer neue
    rows = await ebay.get_active_listings(max_items=max(1000, len(by_item) + 200))
    for r in rows:
        l = by_item.get(str(r.get("item_id") or ""))
        if l is None:
            continue
        # eBay laesst QuantitySold bei 0 Verkaeufen weg -> Item in der Aktiv-Liste
        # ohne Feld bedeutet 0 (wichtig fuers Aufraeumen von Ladenhuetern).
        try:
            sold = int(r.get("quantity_sold") or 0)
        except (ValueError, TypeError):
            sold = 0
        l.sales_total = sold
        sold_updated += 1

    views_updated = 0
    traffic_error = None
    try:
        traffic = await ebay.get_traffic_batch(list(by_item.keys()), days=days)
        for item_id, l in by_item.items():
            l.views_30d = (traffic.get(item_id) or {}).get("views", 0)
            l.stats_synced_at = now
            views_updated += 1
    except Exception as exc:  # noqa: BLE001 – Analytics darf QuantitySold nicht blockieren
        traffic_error = str(exc)[:200]
        for l in by_item.values():
            l.stats_synced_at = now
    db.commit()
    out = {"listings": len(listings), "sales_updated": sold_updated,
           "views_updated": views_updated}
    if traffic_error:
        out["traffic_error"] = traffic_error
    return out


async def import_all(db: Session, *, ebay=None, days: int = 90, max_items: int = 10000,
                     max_orders: int = 500) -> dict:
    """Beides importieren (erst Listings, dann Verkaeufe) + Audit-Log.

    Verkaeufe laufen ueber ``order_service.sync_ebay_orders`` – den EINEN, line-item-
    genauen, Storno-/Refund-sicheren, deduplizierten Order-Pfad (gleicher Code wie der
    5-Min-Scheduler). Frueher gab es hier einen eigenen Order-LEVEL-Import, der bei
    weitem Zeitfenster Dubletten erzeugte (abgeschnittene tx = orderId, kein lineItem)
    und bestehende ``refunded``/``cancelled`` bedingungslos ueberschrieb – Datenkorruption
    (Vorfall 2026-07-06, siehe Orders-Import-Duplikat-Bug). Nie wieder zwei Importer.
    """
    from app.services import order_service

    ebay = ebay or _real_ebay()
    with task_log(db, task_type="ebay_import", reference_id="sync") as tl:
        listings = await import_listings(db, ebay=ebay, max_items=max_items)
        o = await order_service.sync_ebay_orders(db, days=days, max_orders=max_orders, ebay=ebay)
        # Nicht-Storno-Umsatz frisch aus der DB (fuer die Toast-Anzeige), rueckwaerts-
        # kompatible Feldnamen (fetched/created/revenue_eur) fuer das Frontend.
        rev = db.scalar(select(func.coalesce(func.sum(Sale.price_eur), 0))
                        .where(Sale.status.notin_(("cancelled", "refunded")))) or 0
        orders = {
            "fetched": o.get("orders_seen", 0),
            "created": o.get("sales_created", 0),
            "cancelled": o.get("sales_cancelled", 0),
            "refunded": o.get("sales_refunded", 0),
            "marked_shipped": o.get("marked_shipped", 0),
            "revenue_eur": round(float(rev), 2),
        }
        tl.result_data = {"listings": listings, "orders": orders}
    return {"listings": listings, "orders": orders, "status": "completed"}
