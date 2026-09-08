"""Bestands-Listings per Bildsuche mit ihrer AliExpress-Quelle verknuepfen.

Zweck (Zoll-Neukalkulation 07/2026): Fuer jedes importierte eBay-Listing das
AliExpress-Quellprodukt finden -> aktueller EK bekannt -> Preis-Empfehlung
(inkl. 3€ Pauschalzoll) vs. aktueller eBay-Preis. Listings ohne Treffer sind
die "gibt es nicht auf AliExpress"-Liste (z.B. Temu-Importe).

Sicherheit: Auto-gematchte Listings bekommen auto_reprice=False – KEINE
automatischen Preis-Pushes, bis der Nutzer die Matches/Preise freigibt.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Listing, Product
from app.services import pricing

logger = logging.getLogger("app.services.match")

_RATE_S = 1.3
REPORT_FILE = "./data/reprice_report.json"


def _real_ae():
    from app.integrations.aliexpress import RealAliExpressClient
    return RealAliExpressClient(get_settings())


def _get_or_create_product(db: Session, hit: dict) -> Product:
    pid = str(hit["aliexpress_id"])
    existing = db.scalar(select(Product).where(Product.aliexpress_id == pid))
    if existing is not None:
        return existing
    url = f"https://de.aliexpress.com/item/{pid}.html"
    existing = db.scalar(select(Product).where(Product.aliexpress_url == url))
    if existing is not None:
        return existing
    p = Product(
        aliexpress_url=url, aliexpress_id=pid,
        title_raw=(hit.get("title") or "")[:500],
        price_cny=Decimal(str(hit["price_eur"])) if hit.get("price_eur") else None,
        images=[hit["image"]] if hit.get("image") else [],
    )
    db.add(p)
    db.flush()
    return p


async def match_all(db: Session, *, limit: int | None = None, min_hits: int = 1) -> dict:
    """Alle importierten Listings (ohne Produkt, mit Galerie-Bild) matchen."""
    ae = _real_ae()
    s = get_settings()
    stmt = select(Listing).where(Listing.product_id.is_(None),
                                 Listing.image_url.isnot(None),
                                 Listing.listing_status == "active")
    listings = db.scalars(stmt).all()
    if limit:
        listings = listings[:limit]

    matched = missed = errors = 0
    report: list[dict] = []
    for i, l in enumerate(listings, 1):
        entry = {"listing_id": l.id, "ebay_item_id": l.ebay_item_id,
                 "title": (l.title_seo or "")[:80],
                 "current_price_eur": float(l.price_eur) if l.price_eur else None}
        try:
            img = (await ae._http().get(l.image_url, timeout=20)).content
            hits = await ae.image_search(img, page_size=6)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            entry.update(status="error", error=str(exc)[:120])
            report.append(entry)
            await asyncio.sleep(_RATE_S)
            continue
        priced = [h for h in hits if h.get("price_eur")]
        if len(priced) < min_hits:
            missed += 1
            entry.update(status="not_found")  # -> Temu-/Nicht-AliExpress-Kandidat
            report.append(entry)
            await asyncio.sleep(_RATE_S)
            continue

        best = priced[0]
        product = _get_or_create_product(db, best)
        alts = [{"aliexpress_id": h["aliexpress_id"], "url": h["url"],
                 "title": (h.get("title") or "")[:80], "price_eur": h.get("price_eur"),
                 "image": h.get("image"), "store_url": h.get("store_url")} for h in priced[1:5]]
        prices = [h["price_eur"] for h in priced[:5]]
        product.alternatives = {"items": alts,
                                "avg_price_eur": round(sum(prices) / len(prices), 2),
                                "matched": "image-auto",
                                "matched_at": datetime.now(timezone.utc).isoformat()}
        l.product_id = product.id
        l.auto_reprice = False  # kein Auto-Push vor Review!
        # Neues Produkt -> alte Varianten-Verknuepfungen sind ungueltig (Keys = Primaer-
        # attrs des alten Produkts; globale AE-Attr-IDs koennten kollidieren) – gleiche
        # Regel wie der add_source-Re-Attach-Pfad.
        if l.variant_source_map:
            from sqlalchemy.orm.attributes import flag_modified as _fm
            l.variant_source_map = None
            _fm(l, "variant_source_map")
        from app.services.fast_shipping_service import variants_have_eu_warehouse as _vheu
        _loc = _vheu(product)
        l.cost_eur = Decimal(str(pricing.effective_cost(best["price_eur"], settings=s,
                                                        local=_loc)))

        # Preis-Empfehlung inkl. Zoll (LOKAL: ohne Zoll/Versand-Schaetzung) vs. aktueller Preis
        br = pricing.price_from_cny(best["price_eur"], settings=s, local=_loc)
        cur = float(l.price_eur) if l.price_eur else None
        entry.update(status="matched", ae_url=product.aliexpress_url,
                     ae_price_eur=best["price_eur"],
                     cost_eff_eur=float(l.cost_eur),
                     required_price_eur=br.rounded_price_eur,
                     diff_eur=round(br.rounded_price_eur - cur, 2) if cur is not None else None)
        report.append(entry)
        matched += 1
        try:
            db.commit()
        except Exception as exc:  # noqa: BLE001 – Lock o.ä.: einmal warten + erneut
            db.rollback()
            logger.warning("commit retry (%s)", str(exc)[:80])
            await asyncio.sleep(3)
            try:
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
                errors += 1
                entry["status"] = "error"
                entry["error"] = "db locked"
        if i % 20 == 0:
            logger.info("match progress %s/%s", i, len(listings))
        await asyncio.sleep(_RATE_S)

    db.commit()
    out = {"total": len(listings), "matched": matched, "not_found": missed,
           "errors": errors, "generated_at": datetime.now(timezone.utc).isoformat()}
    Path(REPORT_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(REPORT_FILE).write_text(json.dumps({"summary": out, "rows": report},
                                            ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def _is_suspect(cur: float | None, diff: float | None) -> bool:
    """Verdacht auf FALSCHEN Bild-Match: Soll-Preis voellig neben dem Ist-Preis
    (z.B. +200€) -> vor Preisaenderung manuell den AliExpress-Link pruefen."""
    return bool(cur and diff is not None and (diff > max(10.0, cur * 0.6)
                                              or diff < -cur * 0.5))


def match_is_suspect(listing, product, settings) -> bool:
    """Fuers Monitoring: unbestaetigte Auto-Matches mit absurder Preis-Differenz
    aussparen (kein Bestands-/Preis-Push auf Basis eines womoeglich falschen Produkts)."""
    alt = product.alternatives if isinstance(product.alternatives, dict) else {}
    if alt.get("confirmed"):
        return False
    if not str(alt.get("matched") or "").startswith("image"):
        return False   # kein Bildsuche-Match (eigener Upload/manueller Link) -> vertrauenswuerdig
    if product.price_cny is None or not listing.price_eur:
        return False
    # EU-Lager mitgeben, sonst liegt der Vergleichspreis um Versand und Zoll (3,57 EUR)
    # zu hoch - und ein voellig korrekter Treffer sieht nach "absurder Preis-Differenz"
    # aus. Diese Funktion entscheidet, ob Bestand und Preis gepusht werden duerfen;
    # ein Fehlverdacht haelt also echte Aktualisierungen zurueck.
    from app.services.fast_shipping_service import variants_have_eu_warehouse
    br = pricing.price_from_cny(float(product.price_cny), settings=settings,
                                local=variants_have_eu_warehouse(product))
    cur = float(listing.price_eur)
    return _is_suspect(cur, round(br.rounded_price_eur - cur, 2))


def source_is_confirmed(product) -> bool:
    """STRENG (fuer geldrelevante Aktionen wie Mengenrabatt): Die AliExpress-Quelle gilt
    nur als SICHER, wenn sie manuell bestaetigt wurde (`alt.confirmed`) ODER gar kein
    Bildsuche-Match ist (eigener Upload / manueller Link = per Konstruktion die echte
    Quelle). Ein unbestaetigter Auto-Bild-Match ist UNSICHER – das Produkt koennte falsch
    verlinkt oder auf AliExpress gar nicht mehr vorhanden sein (Vorfall Kette 2026-07-07).
    Ohne Produkt (nur importiertes eBay-Listing) = unsicher.
    """
    if product is None:
        return False
    alt = product.alternatives if isinstance(product.alternatives, dict) else {}
    if alt.get("confirmed"):
        return True
    if not str(alt.get("matched") or "").startswith("image"):
        return True
    return False


def _shipping_fields(l, s) -> dict:
    """Versand-Policy-Infos je Zeile: traegt das Listing noch den 3€-Zuschlag?

    'Zuschlag' = Policy bekannt, NICHT die gepinnte Kostenlos-Policy und ohne
    'kostenlos' im Namen (z.B. 'Versand 7 Tage bearbeitung', 'Bearbeitung 2 tage').
    """
    pid = l.shipping_policy_id
    name = l.shipping_policy_name or ""
    surcharge = bool(pid) and str(pid) != str(s.ebay_fulfillment_policy_id) \
        and "kostenlos" not in name.lower()
    return {"shipping_policy_id": pid, "shipping_policy_name": name or None,
            "shipping_surcharge": surcharge}


def _stock_flags(l, variants=None) -> dict:
    """Listing-Ebene Ausverkauf-Signale – ROBUST gegen veralteten Varianten-Bestand.

    Nach einem 404 der Quelle bleibt product.variants[].stock „unbekannt" stehen, sodass
    keine Variante als oos gilt – ein komplett totes Listing wäre unsichtbar (Pikachu-Bug).
    Darum ist `monitor_status` (vom 6h-Monitoring gesetzt, auch bei 404) die maßgebliche
    Wahrheit, NICHT der Varianten-Bestand.

    monitor_oos = Monitoring hat das Listing als ausverkauft / Quelle weg markiert.
    fully_out   = das GANZE Listing ist tot (Monitoring-OOS, Hauptquelle weg, ODER alle
                  Varianten aus und keine über eine Ausweichquelle lieferbar).
    """
    mon = getattr(l, "monitor_status", None) == "out_of_stock"
    vs = variants or []
    all_out_no_rescue = bool(vs) and all(v.get("oos") for v in vs) and not any(v.get("sellable") for v in vs)
    fully = bool(mon or all_out_no_rescue or (not vs and l.supplier_in_stock is False))
    return {"monitor_oos": mon, "fully_out": fully}


# Zustaende der Lieferfaehigkeit. EINE Liste, damit niemand einen vierten Wert
# erfindet - genau so ist der Zaehlfehler entstanden.
LIEFERBAR = "lieferbar"
TEILWEISE_AUS = "teilweise_aus"
AUS = "aus"
PAUSIERT = "pausiert"

# Was als Handlungsbedarf gilt. Wer die Kachel "Quelle ausverkauft" zaehlt,
# zaehlt genau diese beiden - und die Liste dahinter zeigt genau dieselben.
HANDLUNGSBEDARF = (AUS, TEILWEISE_AUS)


def lieferstatus(listing, variants=None) -> str:
    """Kann ich diesen Artikel gerade verkaufen? EINE Antwort fuer alle Aufrufer.

    Vorgeschichte: "Ausverkauft" wurde an DREI Stellen verschieden gezaehlt, und
    keine Menge war Teilmenge einer anderen. Die Startseite meldete 3, die Liste
    zeigte 7 Zeilen. Beide hatten recht - sie beantworteten verschiedene Fragen,
    ohne dass das irgendwo stand.

    Die Reihenfolge der Pruefungen ist die Aussage:

    1. **pausiert** schlaegt alles. Wer ein Listing bewusst angehalten hat,
       braucht keine Ausverkauft-Meldung dazu.
    2. **Eigener Bestand schlaegt jedes Lieferantensignal.** Liegt Ware im
       eigenen Regal, ist der Artikel lieferbar - auch wenn die Quelle bei
       AliExpress tot ist. Genau das fehlte bisher: _stock_flags kennt
       self_stock nicht, und ein Eigenbestands-Listing landete als "komplett
       ausverkauft" in der Liste, obwohl die Ware dalag.
    3. **aus** heisst: das ganze Listing ist tot - Monitoring meldet es,
       die Quelle ist verwaist, oder alle Varianten sind aus ohne Rettung.
    4. **teilweise_aus** heisst: mindestens eine Variante hat keine lieferbare
       Quelle. Ueber eine Ausweichquelle GERETTETE zaehlen NICHT - der Kunde
       kann kaufen, und das ist die einzige Frage, die dieser Wert beantwortet.
    """
    if getattr(listing, "sales_hold", False):
        return PAUSIERT

    # Eigenbestand auf dem GANZEN Listing: dann ist es lieferbar, Punkt.
    eigen = getattr(listing, "self_stock", None) or {}
    try:
        ganzes_listing = (eigen.get("__listing__") or {})
        if int(ganzes_listing.get("qty") or 0) > 0:
            return LIEFERBAR
    except (AttributeError, TypeError, ValueError):
        # self_stock ist ein formfreies JSON-Feld - es kann alles enthalten.
        # Ein kaputter Wert darf die Startseite nicht zerlegen; er zaehlt dann
        # eben nicht als Bestand. Lieber eine Meldung zu viel als eine weisse
        # Seite an der ersten Stelle, die der Betreiber morgens sieht.
        pass

    vs = variants or []
    flags = _stock_flags(listing, vs)
    if flags["fully_out"]:
        return AUS

    # Eine Variante zaehlt als aus, wenn sie kein Ausweichlager hat, das liefert.
    for v in vs:
        if v.get("oos") and not v.get("sellable"):
            return TEILWEISE_AUS
    return LIEFERBAR


def zaehle_handlungsbedarf(db: Session) -> int:
    """Wie viele AKTIVE Listings brauchen wegen Lieferfaehigkeit eine Entscheidung?

    Diese Zahl speist die Kachel auf der Startseite. Sie MUSS mit der Zeilenzahl
    der Liste uebereinstimmen, in die man beim Klick landet - sonst ist der
    Zaehlfehler nur verschoben.
    """
    from app.config import get_settings

    s = get_settings()
    treffer = 0
    for l in db.scalars(select(Listing).where(Listing.listing_status == "active")):
        p = db.get(Product, l.product_id) if l.product_id else None
        vrows = _variant_rows(l, p, s) if p else []
        if lieferstatus(l, vrows) in HANDLUNGSBEDARF:
            treffer += 1
    return treffer


def sold_out_center(db: Session) -> dict:
    """Flache Liste ALLER ausverkauften Varianten aller AKTIVEN Listings – damit nichts mehr
    untergeht (SpongeBob-/Pikachu-Problem). Je Eintrag der Zustand:
      none      – ausverkauft, KEINE Ausweichquelle (Handlungsbedarf)
      backup_oos – Ausweichquelle verknuepft, aber selbst ausverkauft (Handlungsbedarf)
      rescued   – ueber Ausweichquelle lieferbar (nur Info)
      gone      – Quelle ganz weg / Monitoring meldet komplett ausverkauft
    Sortiert: Handlungsbedarf zuerst. Read-only, kein Live-Scrape."""
    s = get_settings()
    listings = db.scalars(select(Listing).where(Listing.listing_status == "active")).all()
    items = []
    summ = {"oos_total": 0, "without_alt": 0, "backup_oos": 0, "rescued": 0, "gone_listings": 0}
    for l in listings:
        # Bewusst pausierte Listings (sales_hold) sind KEIN Handlungsfall – der Nutzer hat sie
        # absichtlich gestoppt (z.B. wegen Korrektur). Konsistent zur Handlungsleiste (!sales_hold).
        if getattr(l, "sales_hold", False):
            continue
        p = db.get(Product, l.product_id) if l.product_id else None
        title = (l.title_seo or "")[:80]
        img = l.image_url or ((getattr(p, "images", None) or [None])[0] if p else None)
        # „verwaist" = Listing HATTE eine Dropship-Quelle, das Produkt wurde aber gelöscht -> tot.
        # NICHT dasselbe wie „nie gematcht" (product_id None) – letzteres gehört in „Quelle prüfen".
        orphaned = bool(l.product_id) and p is None
        gone = getattr(l, "monitor_status", None) == "out_of_stock" or orphaned
        vrows = _variant_rows(l, p, s) if p else []
        oos_vs = [v for v in vrows if v.get("oos")]
        flags = _stock_flags(l, vrows)
        # (1) GANZES Listing tot: Monitoring meldet ausverkauft/Quelle weg, Quelle verwaist,
        #     Hauptquelle weg ODER alle Varianten aus – EIN Eintrag, auch wenn (nach 404) keine
        #     einzelne Variante als oos gilt. Pikachu/SpongeBob-Fix: bisher komplett unsichtbar.
        if flags["fully_out"] or orphaned:
            summ["gone_listings"] += 1 if gone else 0
            items.append({
                "listing_id": l.id, "ebay_item_id": l.ebay_item_id, "title": title, "image": img,
                "sku": l.ebay_sku, "attr": None,
                "variant_name": "(ganzes Listing – Quelle weg)" if gone else "(ganzes Listing – komplett ausverkauft)",
                "stock": 0, "state": "gone" if gone else "sold_out",
                "has_alt": False, "alt_in_stock": False, "alt_reason": None, "alt_sku_name": None,
                "kind": "listing", "variant_count": len(vrows)})
            summ["oos_total"] += 1
            summ["without_alt"] += 1
            continue
        # (2) einzelne ausverkaufte Varianten (Listing lebt teilweise) – je Variante ein Eintrag
        if oos_vs:
            for v in oos_vs:
                has_alt = bool(v.get("alt_source_id"))
                alt_ok = bool(v.get("alt_in_stock"))
                state = "rescued" if (has_alt and alt_ok) else ("backup_oos" if has_alt else "none")
                items.append({
                    "listing_id": l.id, "ebay_item_id": l.ebay_item_id, "title": title, "image": img,
                    "sku": v.get("sku"), "attr": v.get("attr"), "variant_name": v.get("name"),
                    "stock": v.get("stock"), "state": state,
                    "has_alt": has_alt, "alt_in_stock": alt_ok, "alt_reason": v.get("alt_reason"),
                    "alt_sku_name": v.get("alt_sku_name"), "kind": "variant"})
                summ["oos_total"] += 1
                summ[{"rescued": "rescued", "backup_oos": "backup_oos"}.get(state, "without_alt")] += 1
    _rank = {"gone": 0, "sold_out": 0, "none": 0, "backup_oos": 1, "rescued": 2}
    items.sort(key=lambda it: _rank.get(it["state"], 3))
    return {"summary": summ, "count": len(items), "items": items}


def _num(x):
    """x -> gerundeter float(2) oder None (robust gegen kaputte Werte)."""
    if x is None:
        return None
    try:
        return round(float(x), 2)
    except (TypeError, ValueError):
        return None


def _price_drifts(internal, live) -> bool:
    """True, wenn der interne Preis spuerbar vom echten eBay-Preis abweicht (>= 0,50 € bzw. 2 %)."""
    return (internal is not None and live is not None
            and abs(internal - live) >= max(0.5, 0.02 * (internal or 0)))


def _live_price_values(listing) -> list[float]:
    """ALLE gespeicherten echten eBay-Preise eines Listings (robust gegen kaputte Werte)."""
    return [n for n in (_num(p) for p in (getattr(listing, "ebay_live_prices", None) or {}).values())
            if n is not None]


def _value_sig(values) -> str:
    """Normalisierte Signatur aus den Options-WERTEN einer Variante (Achsennamen egal, Leerzeichen
    raus, klein). Matcht eine eBay-Variation robust IHRER Variante zu – auch wenn die SKUs nicht
    ``{base}-V{i}`` sind (wir publishen die Options-Werte als eBay-Merkmale, s. golive)."""
    out = []
    for v in (values or []):
        if v is None:
            continue
        s = re.sub(r"\s+", "", str(v).strip().lower())
        if s:
            out.append(s)
    return "|".join(sorted(out))


def effective_ebay_price(listing, sku: str | None = None, options=None):
    """EINHEITLICHE Quelle der Wahrheit für den anzuzeigenden Preis (Cockpit, eBay-Produkte,
    Optimierung, Preise-anpassen): der ECHTE eBay-Listing-Preis DIESER Variante.

    Reihenfolge: 1) exakter SKU-Treffer ({base}-V{i} == eBay-SKU), 2) Treffer über die
    Merkmals-WERTE (``options`` -> Signatur ``sig:…``; robust bei abweichenden SKUs), 3) letzter
    Ausweg KONSERVATIV der niedrigste echte Preis (nur wenn gar nichts matcht – versteckt keinen
    Verlust). None = noch nicht abgeglichen -> Aufrufer nimmt den internen Preis.
    """
    lp = getattr(listing, "ebay_live_prices", None) or {}
    if not lp:
        return None
    if sku is not None:
        exact = _num(lp.get(sku))
        if exact is not None:
            return exact
    if options:
        sig = _value_sig(options.values() if isinstance(options, dict) else options)
        if sig:
            by_sig = _num(lp.get("sig:" + sig))
            if by_sig is not None:
                return by_sig
    vals = _live_price_values(listing)
    return min(vals) if vals else None


def _variant_rows(listing, product, s, fee_pct: float | None = None) -> list[dict]:
    """Je Variante die ECHTE Oekonomie zum AKTUELLEN Preis: EK, aktueller VK, Gewinn (€),
    Marge (%), Verlust-/zu-duenn-Flag und – NUR wenn noetig – ein gezielter Soll-Preis, der
    genau DIESE Variante auf die Zielmarge hebt. Kein pauschaler Listing-Aufschlag mehr:
    profitable Varianten bleiben, nur die verlustigen/zu duennen werden angehoben.
    """
    from app.services.golive_service import compute_variant_prices, _usable_variants, _stock_num
    axis_names, variants = _usable_variants(product)
    if not axis_names:
        return []
    if fee_pct is None:
        fee_pct = pricing.effective_fee_pct_for_listing(listing, settings=s)
    cents = s.price_cents or 0.95
    calc = {v["sku"]: v for v in compute_variant_prices(listing, product, s)}
    # ECHTE Versandkosten des Listings (freight query) statt 1,99-€-Pauschale — sonst
    # weicht der Preis-Check-EK systematisch von Upload/Golive ab (Konsistenz, 13.07.).
    from app.services.golive_service import _supplier_ship
    sov = _supplier_ship(listing)
    # Per-Variante Ausweich-Quelle + Lieferbarkeit der KONKRETEN Ziel-Variante
    # (zentraler Helper = derselbe Entscheidungsweg wie das Monitoring).
    from app.services.supplier_service import parse_variant_alt, variant_alt_availability
    vmap = listing.variant_source_map or {}
    out = []
    base_sku = listing.ebay_sku or f"AE-{listing.id}"
    for i, v in enumerate(variants, 1):
        try:
            ae_p = float(v["price"]) if v.get("price") is not None else None
        except (TypeError, ValueError):
            ae_p = None
        sku = f"{base_sku}-V{i}"[:50]
        attr = v.get("attr")
        # EIGENBESTAND: selbst gelagerte Variante rechnet mit EIGENEM EK + eigenem Preis
        # (oft guenstigerer Versand) und braucht KEINE AliExpress-Quelle.
        self_entry = (getattr(listing, "self_stock", None) or {}).get(attr) if attr else None
        is_self = isinstance(self_entry, dict)
        if is_self:
            try:
                ek = round(float(self_entry["cost_eur"]), 2) if self_entry.get("cost_eur") is not None else None
            except (TypeError, ValueError, KeyError):
                ek = None
            try:
                cur = (round(float(self_entry["price_eur"]), 2) if self_entry.get("price_eur") is not None
                       else (calc.get(sku) or {}).get("price_eur"))
            except (TypeError, ValueError, KeyError):
                cur = (calc.get(sku) or {}).get("price_eur")
        else:
            from app.integrations.aliexpress_api import has_eu_warehouse as _heu
            ek = (round(pricing.effective_cost(ae_p, settings=s, ship_override=sov,
                                               local=_heu([v.get("ship_from")])), 2)
                  if ae_p else None)
            cur = (calc.get(sku) or {}).get("price_eur")
        # ECHTER Live-eBay-Preis dieser Variante (aus dem Abgleich) — QUELLE DER WAHRHEIT fuer die
        # Marge. Fehlt er, faellt es auf den internen Preis zurueck. Weicht der interne Preis
        # deutlich ab (>= 0,50 € bzw. 2 %), ist es DRIFT (nie/fehlgeschlagen gepusht) -> Marge wird
        # auf dem echten Preis gerechnet + im Cockpit gewarnt (keine optimistische Scheinmarge).
        # Echter eBay-Preis DIESER Variante (exakt/Merkmals-Signatur/konservativ) als Quelle der
        # Wahrheit für Marge/Anzeige – dieselbe Logik überall. Fuer die Signatur NUR die publizierten
        # Achsen (axis_options), nie den rohen options-Dict (sonst matcht die "sig:…" nicht).
        live_used = effective_ebay_price(listing, sku, (calc.get(sku) or {}).get("axis_options"))
        eff_vk = live_used if live_used is not None else cur
        price_drift = bool(live_used is not None and cur is not None
                           and abs(live_used - cur) >= max(0.5, 0.02 * (cur or 0)))
        profit = margin = suggested = None
        is_loss = needs_raise = False
        if eff_vk and ek is not None:
            profit = round(eff_vk - eff_vk * fee_pct - pricing.ebay_fixed_fee(s) - ek, 2)
            margin = round(profit / eff_vk, 4) if eff_vk else None
            is_loss = profit <= 0
            # „Marge dünn" nur, wenn WEDER die Zielmarge (20 %) NOCH der Mindestgewinn (4 €)
            # erreicht wird – ein Artikel mit >= 4 € Gewinn gilt als ok, auch wenn %-Marge < 20.
            needs_raise = bool(profit > 0 and margin is not None
                               and margin < s.target_margin_pct
                               and profit < s.upload_min_profit_eur)
            if is_loss or needs_raise:
                fl = pricing.price_floor(ek, min_margin_pct=s.target_margin_pct,
                                         category_name=listing.category_name,
                                         ad_rate_pct=getattr(listing, "ad_rate_pct", None), settings=s)
                # AUFrunden: der Boden darf nicht unter die Zielmarge gerundet werden.
                suggested = pricing.round_up_to_cents(fl, cents) if fl else None
        # Bestand: EIGENBESTAND (qty) hat Vorrang; sonst Lieferanten-Bestand + Ausweich-Quelle.
        # Konservativ: unbekannter Lieferanten-Bestand = lieferbar; ausverkauft nur bei <= 0.
        if is_self:
            try:
                sqty = int(self_entry.get("qty") or 0)
            except (TypeError, ValueError):
                sqty = 0
            stock_out, oos, sellable = sqty, sqty <= 0, sqty > 0
            ref, alt_in_stock, alt_sku, alt_reason = None, False, {}, None
        else:
            sqty = None
            stock_known, stock_val = _stock_num(v.get("stock"))
            stock_out = stock_val if stock_known else None
            oos = stock_known and stock_val <= 0
            entry = vmap.get(attr) if attr else None
            ref = parse_variant_alt(entry)
            avail = variant_alt_availability(product, entry) if entry else {
                "available": False, "reason": "nicht verknuepft", "sku": None}
            alt_in_stock = bool(avail["available"])
            alt_sku = avail.get("sku") or {}
            alt_reason = avail.get("reason") if entry else None
            sellable = (not oos) or alt_in_stock
        out.append({
            "sku": sku,
            "attr": attr,
            "name": " / ".join(str(v.get("options", {}).get(a, "")) for a in axis_names),
            "ae_price_eur": (None if is_self else ae_p), "ek_eur": ek,
            "current_price_eur": cur, "required_price_eur": cur,  # required_* bleibt fuer Alt-UI
            "ebay_price_eur": live_used,     # Preis, auf dem die Marge gerechnet wurde (echter
                                             # eBay-Preis exakt ODER – bei Key-Mismatch – der konservative
                                             # Minimalpreis des Listings, also NICHT zwingend der Literal-
                                             # Preis DIESER Variante; None = noch nicht abgeglichen)
            "price_drift": price_drift,      # interner Preis weicht vom echten eBay-Preis ab
            "profit_eur": profit, "margin": margin,   # auf dem ECHTEN eBay-Preis gerechnet (wenn bekannt)
            "is_loss": is_loss, "needs_raise": needs_raise,
            "suggested_price_eur": suggested,
            "stock": stock_out,
            "oos": oos,                       # bei der Hauptquelle bzw. im Eigenbestand ausverkauft
            "sellable": sellable,             # verkaufbar (Bestand ODER Ausweich-Quelle)
            "self_stock": is_self,            # 🏠 selbst gelagert (keine Quelle noetig)
            "self_qty": sqty,
            "alt_source_id": (ref or {}).get("source"),   # verknuepfte Ausweich-Quelle
            "alt_in_stock": alt_in_stock,
            "alt_reason": alt_reason,
            # Ziel-Variante der Ausweich-Quelle (Anzeige: Name + Bild statt nackter ID)
            "alt_sku_attr": (ref or {}).get("sku_attr"),
            "alt_sku_name": (ref or {}).get("name"),
            "alt_sku_image": (ref or {}).get("image") or alt_sku.get("image"),
        })
    return out


def _model_signature(s) -> str:
    """Signatur der Kalkulations-Konstanten (Zoll/Steuer/MwSt/Gebuehr/Anzeigenrate/Versand/
    Provisionstabelle). Aendert sie sich, rechnet der Report beim naechsten Oeffnen
    automatisch neu -> nie wieder veraltete Zahlen nach einer Modell-Aenderung."""
    import hashlib
    parts = [getattr(s, "customs_fee_eur", 0), getattr(s, "aliexpress_tax_pct", 0),
             getattr(s, "ebay_fee_vat_pct", 0), getattr(s, "ebay_ad_rate_pct", 0),
             getattr(s, "ebay_fixed_fee_eur", 0), getattr(s, "aliexpress_shipping_fee_eur", 0),
             getattr(s, "aliexpress_free_shipping_threshold", 0),
             getattr(s, "target_margin_pct", 0),
             getattr(s, "upload_min_profit_eur", 0),   # 4€-Schwelle fuer „Marge duenn"
             pricing._COMMISSION_DEFAULT]
    src = "|".join(str(x) for x in parts) + "|" + json.dumps(
        pricing._COMMISSION_BY_CATEGORY, sort_keys=True)
    return hashlib.md5(src.encode()).hexdigest()[:12]


SELF_LISTING_KEY = "__listing__"   # Ganz-Listing-Eigenbestand (Listings ohne Varianten/Quelle)


def _self_stock_listing_row(l, ss: dict, s) -> dict:
    """Report-Zeile fuer ein GANZES als Eigenbestand markiertes Listing (keine AliExpress-Quelle
    noetig – eigener EK/Preis). Fuer Einzel-Artikel/Temu-Importe, die man selbst auf Lager hat."""
    ek = round(float(ss["cost_eur"]), 2) if ss.get("cost_eur") is not None else (float(l.cost_eur) if l.cost_eur else None)
    cur = round(float(ss["price_eur"]), 2) if ss.get("price_eur") is not None else (float(l.price_eur) if l.price_eur else None)
    try:
        qty = int(ss.get("qty") or 0)
    except (TypeError, ValueError):
        qty = 0
    fee_pct = pricing.effective_fee_pct_for_listing(l, settings=s)
    profit = round(cur - cur * fee_pct - pricing.ebay_fixed_fee(s) - ek, 2) if (cur is not None and ek is not None) else None
    margin = round(profit / cur, 4) if (profit is not None and cur) else None
    return {
        "listing_id": l.id, "ebay_item_id": l.ebay_item_id, "title": (l.title_seo or "")[:80],
        "image": l.image_url, "status": "matched", "ae_url": None,
        "ae_price_eur": None, "cost_eff_eur": ek, "current_price_eur": cur,
        "ek_min_eur": ek, "ek_max_eur": ek, "price_min_eur": cur, "price_max_eur": cur,
        "profit_min_eur": profit, "profit_max_eur": profit, "margin_min": margin, "margin_max": margin,
        "profit_now_eur": profit, "margin_now": margin,
        "required_price_eur": None, "diff_eur": None,
        "match_verdacht": False, "confirmed": True, "verified": True,
        "needs_raise": bool(profit is not None and profit > 0 and margin is not None
                            and margin < s.target_margin_pct and profit < s.upload_min_profit_eur),
        "target_margin_pct": s.target_margin_pct,
        "action_required": bool(profit is not None and profit <= 0),
        "loss_variant_count": 0, "thin_variant_count": 0,
        "sources": [], "avg_price_eur": None, "avg_ek_eur": None,
        "in_stock": qty > 0, "variants": [], "variant_count": 0,
        "oos_variant_count": 0, "has_oos_variant": False,
        "monitor_oos": False, "fully_out": qty <= 0,
        "self_stock_listing": True, "self_qty": qty,
        "sales_hold": bool(getattr(l, "sales_hold", False)), "hold_reason": getattr(l, "hold_reason", None),
        "sales_total": l.sales_total, "views_30d": l.views_30d, "alt_items": [],
        **_shipping_fields(l, s),
    }


def rebuild_reprice_report(db: Session) -> dict:
    """Report aus der DB neu berechnen (aktuelle Formel) – unabhaengig vom Match-Lauf.

    Je Listing: Quellen-Slots (bis 3 Anbieter) + Ø-EK, Preis je VARIANTE, Soll-Preis
    auf Basis der TEUERSTEN Variante der Hauptquelle (sicherer Deckungsbeitrag) und
    ein hartes 🔴-Flag, wo der Artikel zum aktuellen Preis VERLUST macht.
    """
    from app.services.supplier_service import ensure_primary_slot
    s = get_settings()
    rows: list[dict] = []
    matched = 0
    # Ganz-Listing-Eigenbestand (Sentinel-Key): eigener EK/Preis, KEINE Quelle noetig. Diese
    # Listings werden separat als Eigenbestand-Zeilen ausgegeben und unten uebersprungen.
    self_rows: list[dict] = []
    self_ids: set[int] = set()
    for _l in db.scalars(select(Listing).where(Listing.listing_status == "active")).all():
        _ss = (getattr(_l, "self_stock", None) or {}).get(SELF_LISTING_KEY)
        if isinstance(_ss, dict):
            self_rows.append(_self_stock_listing_row(_l, _ss, s))
            self_ids.add(_l.id)
    listings = db.scalars(select(Listing).where(Listing.product_id.isnot(None),
                                                Listing.listing_status == "active")).all()
    no_price: list[dict] = []
    for l in listings:
        if l.id in self_ids:
            continue
        p = db.get(Product, l.product_id)
        if p is None or p.price_cny is None:
            # NICHT still verschlucken: sonst ist der Artikel im Preis-Check
            # unauffindbar und der Nutzer kann keine Quelle pruefen/verwalten.
            no_price.append({
                "listing_id": l.id, "ebay_item_id": l.ebay_item_id,
                "title": (l.title_seo or "")[:80], "status": "no_price",
                "image": l.image_url,
                "current_price_eur": float(l.price_eur) if l.price_eur else None,
                "ae_url": (p.aliexpress_url if p else None),
                "sources": [], "variants": [], "alt_items": [],
                "sales_total": l.sales_total, "views_30d": l.views_30d,
                **_stock_flags(l), **_shipping_fields(l, s)})
            continue
        alt = ensure_primary_slot(p)
        fee_pct = pricing.effective_fee_pct_for_listing(l, settings=s)
        variants = _variant_rows(l, p, s, fee_pct)
        # EK-Anzeige/Match-Verdacht: teuerste Variante (falls bekannt), sonst Produkt-Preis.
        var_prices = [v["ae_price_eur"] for v in variants if v["ae_price_eur"]]
        ae_price = max(var_prices) if var_prices else float(p.price_cny)
        # ECHTE Versandkosten (freight query) auch hier — konsistent mit _variant_rows/Golive.
        from app.services.golive_service import _supplier_ship as _ship
        from app.services.fast_shipping_service import variants_have_eu_warehouse as _vheu
        eff = pricing.effective_cost(ae_price, settings=s, ship_override=_ship(l),
                                     local=_vheu(p))
        l.cost_eur = Decimal(str(eff))
        cur = float(l.price_eur) if l.price_eur else None
        confirmed = bool(alt.get("confirmed"))
        image_matched = str(alt.get("matched") or "").startswith("image")
        live_base = None   # echter Live-eBay-Basispreis (Einzel-Listing); Varianten nutzen v[ebay_price_eur]

        priced = [v for v in variants if v.get("margin") is not None]
        if priced:
            # PER-VARIANTE (Fix): profitable Varianten bleiben, nur verlustige/zu duenne
            # werden GEZIELT angehoben – kein pauschaler Listing-Aufschlag mehr.
            loss_vars = [v for v in priced if v["is_loss"]]
            thin_vars = [v for v in priced if v["needs_raise"]]
            worst = min(priced, key=lambda v: v["margin"])
            profit_now, margin_now = worst["profit_eur"], worst["margin"]
            problem = loss_vars or thin_vars
            prob_worst = min(problem, key=lambda v: v["margin"]) if problem else None
            required_price_eur = prob_worst["suggested_price_eur"] if prob_worst else None
            diff = (round(required_price_eur - prob_worst["current_price_eur"], 2)
                    if (required_price_eur and prob_worst) else None)
            suspect = bool(image_matched and not confirmed and prob_worst
                           and _is_suspect(prob_worst["current_price_eur"], diff))
            action_required = bool(loss_vars) and not suspect
            needs_raise = bool(thin_vars) and not loss_vars and not suspect
        else:
            # Einzel-Listing ohne echte Varianten: Headline-Logik (teuerste = einzige).
            # Soll-Preis = margenbasierter Boden auf ZIELMARGE (price_floor, wie im
            # Varianten-Pfad) – NICHT der 8-€-Mindestgewinn aus compute_price. Die
            # Anhebung soll nur auf ~20 % Marge heben, nicht auf Krampf 8 € Gewinn.
            fl = pricing.price_floor(eff, min_margin_pct=s.target_margin_pct,
                                     category_name=l.category_name,
                                     ad_rate_pct=getattr(l, "ad_rate_pct", None), settings=s)
            # AUFrunden: der Boden darf nicht unter die Zielmarge gerundet werden.
            floor_price = pricing.round_up_to_cents(fl, s.price_cents or 0.95) if fl else None
            # ECHTER Live-eBay-Preis (Einzel-Listing) — Marge auf der Realitaet, nicht dem internen.
            _lp = getattr(l, "ebay_live_prices", None) or {}
            # Basis-SKU bevorzugen; sonst KONSERVATIV der niedrigste Live-Preis (nicht ein
            # beliebiger per Dict-Reihenfolge) — der niedrigste deckt einen Verlust am ehesten auf.
            live_base = _lp.get(l.ebay_sku or f"AE-{l.id}")
            if live_base is None and _lp:
                try:
                    live_base = min(_lp.values())
                except (TypeError, ValueError):
                    live_base = None
            try:
                live_base = round(float(live_base), 2) if live_base is not None else None
            except (TypeError, ValueError):
                live_base = None
            _vk = live_base if live_base is not None else cur
            profit_now = (round(_vk - _vk * fee_pct - pricing.ebay_fixed_fee(s) - eff, 2)
                          if _vk is not None else None)
            margin_now = (round(profit_now / _vk, 4) if (profit_now is not None and _vk) else None)
            # Verdacht auf FALSCHEN Bild-Match weiter an der aufwaerts kalkulierten
            # Fair-Preis-Referenz messen (nicht am Boden) – sonst wuerden hochmargige
            # Artikel mit sehr niedrigem Boden faelschlich als "Match pruefen" markiert.
            # EU-Lager mitgeben (wie in match_is_suspect): sonst liegt die Referenz um
            # Versand und Zoll zu hoch und markiert korrekte Treffer als "Match pruefen".
            from app.services.fast_shipping_service import variants_have_eu_warehouse
            fair = pricing.price_from_cny(
                ae_price, settings=s, fee_pct=fee_pct,
                local=variants_have_eu_warehouse(p)).rounded_price_eur
            suspect = bool(image_matched and not confirmed
                           and _is_suspect(cur, round(fair - cur, 2) if cur is not None else None))
            action_required = bool(profit_now is not None and profit_now <= 0 and not suspect)
            # „dünn" nur, wenn WEDER Zielmarge (20 %) NOCH Mindestgewinn (4 €) erreicht sind.
            needs_raise = bool(profit_now is not None and profit_now > 0 and margin_now is not None
                               and margin_now < s.target_margin_pct
                               and profit_now < s.upload_min_profit_eur and not suspect)
            # Soll-Preis nur zeigen, wenn wirklich eine Anhebung ansteht (wie Varianten-Pfad).
            required_price_eur = floor_price if ((action_required or needs_raise) and floor_price) else None
            diff = round(required_price_eur - cur, 2) if (cur is not None and required_price_eur is not None) else None
            loss_vars, thin_vars = ([] if not action_required else [1]), ([] if not needs_raise else [1])
        # KOPIE ohne den (bis zu 100 SKUs grossen) Snapshot – die UI liest daraus nur
        # id/in_stock/title/url/image; NICHT in place loeschen (live product.alternatives!).
        sources = [{k: v for k, v in src.items() if k not in ("skus", "skus_truncated")}
                   for src in (alt.get("sources") or [])]
        # Spannen (min–max) je Listing – "immer die Spanne" statt nur Worst-Case.
        _pv = [v for v in variants if v.get("margin") is not None]
        if _pv:
            _eks = [v["ek_eur"] for v in variants if v.get("ek_eur") is not None] or [eff]
            _prs = [v["current_price_eur"] for v in variants if v.get("current_price_eur") is not None] or [cur]
            ek_min, ek_max = min(_eks), max(_eks)
            price_min, price_max = min(_prs), max(_prs)
            profit_min, profit_max = min(v["profit_eur"] for v in _pv), max(v["profit_eur"] for v in _pv)
            margin_min, margin_max = min(v["margin"] for v in _pv), max(v["margin"] for v in _pv)
        else:
            ek_min = ek_max = eff
            price_min = price_max = cur
            profit_min = profit_max = profit_now
            margin_min = margin_max = margin_now
        # Live-eBay-Preis-Spanne + DRIFT-Flag: weicht der interne Preis vom echten eBay-Preis ab?
        # Bevorzugt die per-Varianten aufgeloesten Live-Preise; matchen deren SKU-Keys NICHT (z.B.
        # importiertes/AutoDS-Listing, dessen eBay-SKUs nicht {base}-V{i} sind), ALLE gespeicherten
        # Live-Preise als Fallback nehmen – so wird die echte eBay-Spanne fuer JEDES Listing sichtbar
        # (Fund 17.07.: "zeigt fuer alle Produkte weiter den internen statt echten eBay-Preis").
        # ECHTE eBay-Preis-Spanne bevorzugt aus ALLEN gespeicherten Live-Preisen (wahre Min–Max-Spanne,
        # auch wenn die per-Varianten aufgeloesten Preise bei Key-Mismatch auf den konservativen
        # Minimalwert kollabieren); Fallback auf die per-Varianten-Werte bzw. den Einzel-Basispreis.
        _lp_all = _live_price_values(l)
        _live = [v["ebay_price_eur"] for v in variants if v.get("ebay_price_eur") is not None]
        _live_src = _lp_all or _live or ([live_base] if live_base is not None else [])
        ebay_price_min = min(_live_src) if _live_src else None
        ebay_price_max = max(_live_src) if _live_src else None
        # Drift auf Listing-Ebene: per-Variante-Drift ODER interne Preis-Spanne weicht vom echten
        # eBay-Bereich ab (deckt auch Listings ab, deren Keys nicht pro Variante matchen).
        price_drift = (any(v.get("price_drift") for v in variants)
                       or _price_drifts(price_min, ebay_price_min)
                       or _price_drifts(price_max, ebay_price_max))
        rows.append({
            "listing_id": l.id, "ebay_item_id": l.ebay_item_id,
            "title": (l.title_seo or "")[:80],
            "image": l.image_url or ((p.images or [None])[0] if p.images else None),
            **_shipping_fields(l, s),
            "status": "matched", "ae_url": p.aliexpress_url,
            "ae_price_eur": ae_price, "cost_eff_eur": eff,
            "current_price_eur": cur,
            # Spannen fuer die Anzeige (Multivarianten): EK/Preis/Gewinn/Marge min–max.
            "ek_min_eur": ek_min, "ek_max_eur": ek_max,
            "price_min_eur": price_min, "price_max_eur": price_max,
            # ECHTE Live-eBay-Preis-Spanne + Drift-Flag (interner Preis weicht ab -> Warnung im UI).
            "ebay_price_min_eur": ebay_price_min, "ebay_price_max_eur": ebay_price_max,
            "price_drift": price_drift,
            "profit_min_eur": profit_min, "profit_max_eur": profit_max,
            "margin_min": margin_min, "margin_max": margin_max,
            "required_price_eur": required_price_eur,
            "diff_eur": diff,
            "match_verdacht": suspect,
            "confirmed": confirmed,
            # 'verified' = STRENGE Quellen-Sicherheit (manuell bestaetigt ODER vom System
            # angelegt / kein Bild-Match) -> Nutzer-Filter "✓ Verifiziert". Gegenstueck zu
            # match_verdacht (unbestaetigter Bild-Match).
            "verified": source_is_confirmed(p),
            "profit_now_eur": profit_now,
            "margin_now": margin_now,
            "needs_raise": needs_raise,
            "target_margin_pct": s.target_margin_pct,
            "action_required": action_required,
            "loss_variant_count": len(loss_vars),
            "thin_variant_count": len(thin_vars),
            "sources": sources,
            "avg_price_eur": alt.get("avg_price_eur"),
            "avg_ek_eur": alt.get("avg_ek_eur"),
            "in_stock": bool(sources[0].get("in_stock", True)) if sources else bool(l.supplier_in_stock),
            "variants": variants[:16],   # Anzeige gekappt; Kalkulation oben nutzt ALLE
            "variant_count": len(variants),
            # Wie viele Varianten sind ausverkauft (ohne lieferbare Ausweich-Quelle)?
            "oos_variant_count": sum(1 for v in variants if v.get("oos") and not v.get("sellable")),
            "has_oos_variant": any(v.get("oos") for v in variants),
            # Robuste Listing-Ebene-Signale (gegen veralteten Varianten-Bestand): monitor_oos +
            # fully_out fangen den gone/„komplett tot"-Fall, den die Varianten-Flags verpassen.
            **_stock_flags(l, variants),
            # Manuelle Verkaufs-Sperre (z.B. wegen Korrektur) – im Preis-Check als Badge,
            # damit "Menge 0" nicht faelschlich als "Lieferant ausverkauft" gelesen wird.
            "sales_hold": bool(getattr(l, "sales_hold", False)),
            "hold_reason": getattr(l, "hold_reason", None),
            "sales_total": l.sales_total,
            "views_30d": l.views_30d,
            # Bildsuche-Vorschlaege (kompakt) fuer die Quellen-Verwaltung im UI
            "alt_items": [{"aliexpress_id": str(i.get("aliexpress_id") or ""),
                           "url": i.get("url"), "title": (i.get("title") or "")[:60],
                           "image": i.get("image"), "price_eur": i.get("price_eur")}
                          for i in (alt.get("items") or [])[:6]],
        })
        matched += 1
    db.commit()
    # Listings OHNE Quelle live aus der DB (Temu-/Nicht-AliExpress-Kandidaten) – hier
    # kann der Nutzer per Link-Eingabe manuell eine Quelle verbinden.
    unmatched = db.scalars(select(Listing).where(Listing.product_id.is_(None),
                                                 Listing.listing_status == "active")).all()
    keep = [{"listing_id": l.id, "ebay_item_id": l.ebay_item_id,
             "title": (l.title_seo or "")[:80], "status": "not_found",
             "image": l.image_url,
             "current_price_eur": float(l.price_eur) if l.price_eur else None,
             "sources": [], "variants": [], "alt_items": [],
             "sales_total": l.sales_total, "views_30d": l.views_30d,
             **_stock_flags(l), **_shipping_fields(l, s)}
            for l in unmatched if l.id not in self_ids]
    summary = {"total": matched + len(keep) + len(no_price) + len(self_rows), "matched": matched,
               "not_found": len(keep), "no_price": len(no_price),
               "self_stock_listings": len(self_rows), "errors": 0,
               "generated_at": datetime.now(timezone.utc).isoformat(),
               "formula": "pauschalzoll", "model_sig": _model_signature(s)}
    # Invariante: jede aktive Zeile MUSS im Report auftauchen (matched/not_found/no_price/eigenbestand)
    expected = len(listings) + len(unmatched)
    if summary["total"] != expected:
        logger.warning("reprice report row mismatch", extra={
            "rows": summary["total"], "active_listings": expected})
    Path(REPORT_FILE).write_text(json.dumps({"summary": summary,
                                             "rows": rows + no_price + keep + self_rows},
                                            ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def reprice_report(db: Session) -> dict:
    """Gespeicherten Match-/Reprice-Report lesen (fuer Dashboard/Review).

    last_sync_at wird LIVE aus der DB ergaenzt (Zeitpunkt der letzten
    Lieferanten-Ueberwachung), damit die Anzeige nicht am Report-Alter haengt.
    """
    p = Path(REPORT_FILE)
    data = (json.loads(p.read_text(encoding="utf-8")) if p.exists()
            else {"summary": None, "rows": []})
    # AUTO-FRISCHE: fehlt der Report oder wurde die Kalkulations-Formel geaendert (neue
    # Signatur), einmal frisch rechnen -> die Seite zeigt nie veraltete EK/Gewinn/Marge.
    cur_sig = _model_signature(get_settings())
    stored_sig = ((data or {}).get("summary") or {}).get("model_sig")
    if not data.get("rows") or stored_sig != cur_sig:
        try:
            rebuild_reprice_report(db)
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 – im Zweifel den (evtl. alten) Report ausliefern
            logger.warning("reprice_report: Auto-Rebuild fehlgeschlagen", exc_info=True)
    from sqlalchemy import func
    last = db.scalar(select(func.max(Listing.last_monitored_at)))
    if last is not None and last.tzinfo is None:
        # SQLite liefert naive UTC-Zeiten -> als UTC markieren, sonst zeigt der
        # Browser die Zeit um die lokale Zeitzonen-Differenz falsch an.
        last = last.replace(tzinfo=timezone.utc)
    data["last_sync_at"] = last.isoformat() if last else None
    return data
