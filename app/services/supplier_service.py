"""Lieferanten-Quellen je Listing: bis zu 3 AliExpress-Anbieter pro Artikel.

Der Nutzer (oder die Auto-Bildsuche) hinterlegt je Listing 1–3 AliExpress-Links
("Slots"). Slot 0 ist die Hauptquelle: ihre Varianten/Preise/Bestaende bestimmen
Kalkulation und Bestellweg. Alle Slots zusammen ergeben den Durchschnitts-EK und
dienen als Ausfall-Absicherung (Anbieter A ausverkauft -> B/C vorhanden).

Manuell gesetzte Quellen gelten als BESTAETIGT (confirmed) – sie verlieren den
"⚠ Match pruefen"-Verdacht und nehmen wieder am Monitoring teil.

Ablage in ``product.alternatives``::

    {"sources": [{aliexpress_id, url, title, price_eur, price_max_eur, in_stock,
                  stock, variant_count, added: 'auto'|'manual', checked_at}],
     "avg_price_eur": ..., "avg_ek_eur": ..., "confirmed": bool,
     "items": [...legacy Bildsuche-Alternativen...]}
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.config import get_settings
from app.integrations import aliexpress_api as api
from app.integrations.aliexpress import OutOfStockError, ProductNotFoundError
from app.models import Listing, Product
from app.retry import PersistentError
from app.services import pricing

logger = logging.getLogger("app.services.supplier")

MAX_SOURCES = 3
_RATE_S = 1.1


def _real_ae():
    from app.integrations.aliexpress import RealAliExpressClient
    return RealAliExpressClient(get_settings())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def find_source_candidates(db: Session, *, listing_id: int,
                                 page_size: int = 30) -> dict:
    """KI-Quellenfinder: AliExpress-Bildsuche zum eBay-Bild + Claude-Bewertung.

    Fuer Listings OHNE Quelle oder mit verdaechtiger Quelle. Sucht per Bild
    aehnliche AliExpress-Produkte und laesst Claude jeden Treffer bewerten
    ("dasselbe Produkt?" -> Konfidenz). VERKNUEPFT NICHTS automatisch (geldwirksam):
    liefert nur nach Konfidenz sortierte Vorschlaege fuers Bild-Vergleichs-Modal,
    der Nutzer bestaetigt per Klick.
    """
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    if not listing.image_url:
        return {"listing_id": listing_id, "ebay_title": listing.title_seo,
                "ebay_image": None, "candidates": [], "note": "kein eBay-Bild"}

    ae = _real_ae()
    try:
        img = (await ae._http().get(listing.image_url, timeout=20)).content
        hits = await ae.image_search(img, page_size=page_size)
    except Exception as exc:  # noqa: BLE001 – Bildsuche kann leer/fehlerhaft sein
        logger.warning("find_source_candidates image_search failed",
                       extra={"listing_id": listing_id, "error": str(exc)[:150]})
        return {"listing_id": listing_id, "ebay_title": listing.title_seo,
                "ebay_image": listing.image_url, "candidates": [],
                "note": "Bildsuche fehlgeschlagen"}

    # Bereits verknuepfte Quelle(n) NICHT erneut vorschlagen
    product = db.get(Product, listing.product_id) if listing.product_id else None
    own = {str(product.aliexpress_id)} if product else set()
    for s in ((product.alternatives or {}).get("sources") or []) if product else []:
        own.add(str(s.get("aliexpress_id")))
    # Bildsuche liefert oft 30-40 Treffer (bild-aehnlichkeits-sortiert). Nur die
    # besten ~12 an die KI + ins Modal: mehr waere teuer, sprengt das LLM-Token-
    # Limit (Output-Overflow -> Parse-Fehler) und ist zum Vergleichen unuebersichtlich.
    priced = [h for h in hits
              if h.get("price_eur") and str(h.get("aliexpress_id")) not in own][:12]

    from app.integrations import get_llm_client
    try:
        ranks = await get_llm_client().rank_source_candidates(
            ebay_title=listing.title_seo or "", candidates=priced)
    except Exception as exc:  # noqa: BLE001
        logger.warning("rank_source_candidates failed", extra={"error": str(exc)[:150]})
        ranks = []
    rmap = {str(r.get("aliexpress_id")): r for r in ranks}

    s = get_settings()
    cands = []
    for h in priced:
        r = rmap.get(str(h.get("aliexpress_id")), {})
        # KEIN local= moeglich: h ist ein Suchtreffer, kein gespeichertes Produkt -
        # Variantendaten mit ``ship_from`` liegen hier nicht vor. Der Wert ist damit
        # eine OBERGRENZE (China-Annahme). Beim Vergleich mehrerer Kandidaten ist das
        # fair, weil ALLE gleich gerechnet werden; sobald einer uebernommen wird,
        # rechnet der Import mit dem echten Lager.
        ek = pricing.effective_cost(h["price_eur"], settings=s)
        vk = pricing.price_from_cny(h["price_eur"], settings=s).rounded_price_eur
        cands.append({
            "aliexpress_id": str(h.get("aliexpress_id") or ""),
            "url": h.get("url"), "title": (h.get("title") or "")[:120],
            "image": h.get("image"), "price_eur": h.get("price_eur"),
            "ek_eur": round(ek, 2), "vk_eur": vk,
            "ki_confidence": float(r.get("confidence") or 0.0),
            "ki_reason": r.get("reason") or "",
        })
    cands.sort(key=lambda c: -c["ki_confidence"])
    return {"listing_id": listing_id, "ebay_title": listing.title_seo,
            "ebay_image": listing.image_url, "candidates": cands}


def _alt_dict(product: Product) -> dict:
    alt = product.alternatives if isinstance(product.alternatives, dict) else {}
    return dict(alt)


def _variant_prices(scraped_variants: dict | None) -> list[float]:
    out = []
    for v in ((scraped_variants or {}).get("skus") or []):
        try:
            if v.get("price") is not None:
                out.append(float(v["price"]))
        except (TypeError, ValueError):
            pass
    return out


def _slot_skus_snapshot(scraped) -> tuple[list[dict], bool]:
    """Schlanker Per-SKU-Snapshot einer Quelle (attr/id/stock/price/image).

    Versorgt Monitoring + Varianten-Picker mit Alt-SKU-Daten ueber die BESTEHENDEN
    Scrape-Pfade (add_source/refresh_sources/refresh_secondary_sources) – keine
    neuen Netz-Calls. Cap 100 Eintraege (truncated-Flag), damit die JSON-Spalte
    schlank bleibt."""
    skus = ((getattr(scraped, "variants", None) or {}).get("skus") or [])
    out = [{"attr": v.get("attr"), "id": v.get("id"), "stock": v.get("stock"),
            "price": v.get("price"), "image": v.get("image"),
            "options": v.get("options") or {}}
           for v in skus[:100]]
    return out, len(skus) > 100


def _slot_from_scrape(scraped, url: str, added: str) -> dict:
    vp = _variant_prices(getattr(scraped, "variants", None))
    price = float(scraped.price_cny or 0) or (min(vp) if vp else 0.0)
    imgs = getattr(scraped, "images", None) or []
    sk, truncated = _slot_skus_snapshot(scraped)
    return {
        "aliexpress_id": str(scraped.aliexpress_id),
        "url": url,
        "title": (scraped.title_raw or "")[:100],
        "image": imgs[0] if imgs else None,   # fuer den Bild-Vergleich im Panel
        "price_eur": round(price, 2),
        "price_max_eur": round(max(vp), 2) if vp else round(price, 2),
        "in_stock": bool(getattr(scraped, "in_stock", True)),
        "stock": sum(int(v.get("stock") or 0) for v in ((getattr(scraped, "variants", None) or {}).get("skus") or [])) or None,
        "variant_count": len((getattr(scraped, "variants", None) or {}).get("skus") or []),
        "skus": sk,                            # Per-SKU-Snapshot (Alt-Varianten-Auswahl)
        "skus_truncated": truncated,
        "added": added,
        "checked_at": _now_iso(),
    }


def _recalc_averages(alt: dict) -> None:
    prices = [s["price_eur"] for s in (alt.get("sources") or []) if s.get("price_eur")]
    if prices:
        s = get_settings()
        alt["avg_price_eur"] = round(sum(prices) / len(prices), 2)
        alt["avg_ek_eur"] = round(sum(pricing.effective_cost(p, settings=s) for p in prices)
                                  / len(prices), 2)
    else:
        alt.pop("avg_price_eur", None)
        alt.pop("avg_ek_eur", None)


def ensure_primary_slot(product: Product) -> dict:
    """Slot 0 aus den Produktfeldern initialisieren, falls noch keine Quellen existieren.

    Migriert Alt-Daten (Bildsuche-Match ohne sources-Struktur) verlustfrei.
    """
    alt = _alt_dict(product)
    if not alt.get("sources") and product.aliexpress_url:
        vp = _variant_prices(product.variants)
        price = float(product.price_cny or 0) or (min(vp) if vp else 0.0)
        alt["sources"] = [{
            "aliexpress_id": product.aliexpress_id or "",
            "url": product.aliexpress_url,
            "title": (product.title_raw or "")[:100],
            "price_eur": round(price, 2) if price else None,
            "price_max_eur": round(max(vp), 2) if vp else (round(price, 2) if price else None),
            "in_stock": True,
            "stock": None,
            "variant_count": len((product.variants or {}).get("skus") or []),
            "added": "auto" if (alt.get("matched") or "").startswith("image") else "import",
            "checked_at": None,
        }]
        _recalc_averages(alt)
        product.alternatives = alt
        flag_modified(product, "alternatives")
    return alt


def _apply_primary(product: Product, scraped, url: str) -> None:
    """Hauptquellen-Daten in die Produktfelder spiegeln (Kalkulation + Bestellweg).

    Varianten werden IMMER ersetzt (auch durch leer): alte Varianten eines anderen
    Anbieters wuerden sonst falsche sku_attrs/Preise liefern.
    """
    product.aliexpress_id = str(scraped.aliexpress_id)
    product.aliexpress_url = url
    product.price_cny = scraped.price_cny
    product.variants = getattr(scraped, "variants", None) or None
    flag_modified(product, "variants")
    if getattr(scraped, "images", None) and not product.images:
        product.images = scraped.images
    if getattr(scraped, "title_raw", None) and not product.title_raw:
        product.title_raw = scraped.title_raw


def _norm_url(url: str) -> tuple[str, str]:
    pid = api.extract_product_id(url or "")
    if not pid:
        raise PersistentError(f"Keine AliExpress-Produkt-ID in URL: {url!r}")
    return pid, f"https://de.aliexpress.com/item/{pid}.html"


async def add_source(db: Session, *, listing_id: int, url: str,
                     primary: bool = False, added: str = "manual",
                     carry_slots: list | None = None,
                     carry_confirmed: bool | None = None) -> dict:
    """AliExpress-Link als Quelle hinterlegen (Slot); optional als Hauptquelle setzen.

    Hat das Listing noch KEIN Produkt (not_found/Temu oder falscher Match geloescht),
    wird die Quelle automatisch Hauptquelle und das Produkt neu angelegt/verknuepft.
    ``carry_slots``/``carry_confirmed``: beim Abkoppeln von einem GETEILTEN Produkt
    werden die Ausweich-Slots + Bestaetigung dieses Listings mitgenommen (sonst
    gingen manuell gepflegte Backup-Anbieter beim Hauptquellen-Wechsel verloren).
    """
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    pid, url = _norm_url(url)

    ae = _real_ae()
    scraped = await ae.scrape_product(url)   # Preis + Varianten + Bestand in einem Call

    product = db.get(Product, listing.product_id) if listing.product_id else None
    if product is not None and primary and str(product.aliexpress_id) != pid:
        # Produkt kann von MEHREREN Listings geteilt sein (gleicher Bildsuche-Treffer).
        # Hauptquellen-Korrektur darf nur DIESES Listing umhaengen, nicht alle.
        shared = db.scalar(select(func.count(Listing.id))
                           .where(Listing.product_id == product.id, Listing.id != listing.id))
        # aliexpress_id ist UNIQUE: gehoert die Ziel-ID bereits einem ANDEREN Produkt, darf die ID
        # DIESES Produkts NICHT ueberschrieben werden (sonst IntegrityError beim Commit, Fund 20.07.).
        # Dann dieses Listing an das VORHANDENE Produkt umhaengen (wie beim geteilten Produkt).
        pid_taken = db.scalar(select(Product.id).where(
            Product.aliexpress_id == pid, Product.id != product.id)) is not None
        if shared or pid_taken:
            old = _alt_dict(product)
            if carry_slots is None:   # Ausweich-Slots des alten Produkts mitnehmen
                carry_slots = [s for s in (old.get("sources") or [])
                               if str(s.get("aliexpress_id")) != pid]
            if carry_confirmed is None:
                carry_confirmed = old.get("confirmed")
            product = None
    if product is None:
        # Falsch-Match-Korrektur / Erst-Verknuepfung: Produkt zur Quelle suchen/anlegen
        product = db.scalar(select(Product).where(Product.aliexpress_id == pid))
        if product is None:
            product = Product(aliexpress_url=url, aliexpress_id=pid,
                              title_raw=(scraped.title_raw or "")[:500],
                              price_cny=scraped.price_cny,
                              images=scraped.images or [],
                              variants=scraped.variants or None)
            db.add(product)
            db.flush()
        listing.product_id = product.id
        listing.auto_reprice = False   # Preis-Push weiterhin erst nach Review
        primary = True
        # Listing haengt jetzt an einem ANDEREN Produkt -> alte Varianten-Verknuepfungen
        # (Keys = Primaer-attrs des alten Produkts) sind ungueltig (Attr-Kollisions-Gefahr).
        if listing.variant_source_map:
            listing.variant_source_map = None
            flag_modified(listing, "variant_source_map")

    alt = ensure_primary_slot(product)
    sources = [s for s in (alt.get("sources") or []) if s.get("aliexpress_id") != pid]
    slot = _slot_from_scrape(scraped, url, added)
    auto_match = None
    if primary:
        old_pid = str(product.aliexpress_id or "")
        # ALTE Varianten (Basis der eBay-Varianten) VOR dem Ersetzen sichern -> danach
        # automatisch auf die SKUs der NEUEN Quelle zuordnen (Nutzerwunsch 08.08.).
        _old_skus = list(((product.variants or {}).get("skus")) or [])
        sources.insert(0, slot)
        _apply_primary(product, scraped, url)
        # Hauptquelle GEAENDERT -> Varianten-Verknuepfungen aller Listings loeschen
        # (Keys sind Primaer-attrs; globale AE-Attr-IDs koennten zufaellig matchen).
        if old_pid and old_pid != pid:
            _invalidate_variant_links(db, product, reason=f"primary {old_pid} -> {pid}")
        # Auto-Zuordnung NACH der Invalidierung: bestell-erprobtes Matching, Mehrdeutigkeit
        # -> kein Eintrag (Mensch ordnet zu). variant_map ueberlebt die Invalidierung.
        new_skus = list(((getattr(scraped, "variants", None) or {}).get("skus")) or [])
        auto_match = _auto_match_variants(listing, pid, _old_skus, new_skus)
    else:
        if len(sources) >= MAX_SOURCES:
            raise PersistentError(f"Maximal {MAX_SOURCES} Quellen je Artikel – erst eine entfernen.")
        sources.append(slot)
    # Mitgebrachte Ausweich-Slots (Detach vom geteilten Produkt) ohne Duplikate anhaengen
    for c in (carry_slots or []):
        cid = str(c.get("aliexpress_id"))
        if cid != pid and all(cid != str(s.get("aliexpress_id")) for s in sources):
            sources.append(c)
    alt["sources"] = sources[:MAX_SOURCES]
    if added == "manual" or carry_confirmed:
        alt["confirmed"] = True        # Nutzer hat den Link geprueft -> kein Verdacht mehr
    _recalc_averages(alt)
    product.alternatives = alt
    flag_modified(product, "alternatives")

    # EK des Listings aktualisieren (Basis: teuerste Variante der Hauptquelle)
    if primary:
        base = alt["sources"][0].get("price_max_eur") or alt["sources"][0].get("price_eur")
        if base:
            from app.services.fast_shipping_service import variants_have_eu_warehouse as _vheu
            listing.cost_eur = Decimal(str(pricing.effective_cost(
                base, local=_vheu(product))))
        listing.supplier_in_stock = bool(slot.get("in_stock", True))
    db.commit()
    return {"listing_id": listing_id, "sources": alt["sources"],
            "avg_price_eur": alt.get("avg_price_eur"), "avg_ek_eur": alt.get("avg_ek_eur"),
            "primary": bool(primary), "variant_match": auto_match}


def set_variant_alt_source(db: Session, *, listing_id: int, sku_attr: str,
                           aliexpress_id: str, alt_sku_attr: str | None = None,
                           alt_name: str | None = None, alt_image: str | None = None) -> dict:
    """PER-VARIANTE eine Ausweich-Quelle verknuepfen (propose-only).

    Ist diese Variante bei der Hauptquelle ausverkauft, bleibt sie auf eBay SELLBAR,
    solange die verknuepfte Quelle (ein bereits hinterlegter Slot) lieferbar ist –
    bestellt wird dann gezielt dort (Freigabe pro Bestellung). Verknuepft NUR, bestellt
    nichts. ``aliexpress_id`` muss ein vorhandener Quellen-Slot sein.

    Mit ``alt_sku_attr`` wird zusaetzlich die KONKRETE Ziel-Variante der Ausweich-Quelle
    gemerkt (Nutzer-Projekt 11.07.): Wert wird zum Dict {source, sku_attr, sku_id, name,
    image, linked_at}. Ohne alt_sku_attr bleibt das Legacy-String-Format (ganze Quelle).
    Der alt_sku_attr wird gegen den Slot-SKU-Snapshot validiert (Router scrapt vorher
    frisch); die sku_id des Snapshots dient als Drift-Anker fuer den Bestellweg."""
    listing = db.get(Listing, listing_id)
    product = db.get(Product, listing.product_id) if listing and listing.product_id else None
    if product is None:
        raise PersistentError("Kein Produkt zu diesem Listing")
    from app.services.golive_service import variant_stock_state
    attrs = {st["attr"] for st in variant_stock_state(listing, product) if st.get("attr")}
    if sku_attr not in attrs:
        raise PersistentError("Unbekannte Variante (sku_attr) – bitte Preis-Check neu laden")
    alt = ensure_primary_slot(product)
    slot = next((s for s in (alt.get("sources") or [])
                 if str(s.get("aliexpress_id")) == str(aliexpress_id)), None)
    if slot is None:
        raise PersistentError("Diese Quelle ist nicht hinterlegt – erst als Slot hinzufuegen")
    entry: str | dict = str(aliexpress_id)
    if alt_sku_attr:
        snap = next((sk for sk in (slot.get("skus") or []) if sk.get("attr") == alt_sku_attr), None)
        if snap is None and not slot.get("skus_truncated"):
            raise PersistentError("Ziel-Variante nicht (mehr) in der Quelle gefunden – "
                                  "Varianten neu laden und erneut waehlen")
        entry = {"source": str(aliexpress_id), "sku_attr": alt_sku_attr,
                 "sku_id": (snap or {}).get("id"),
                 "name": (alt_name or "")[:120] or None,
                 "image": alt_image or (snap or {}).get("image"),
                 "linked_at": _now_iso()}
    vmap = dict(listing.variant_source_map or {})
    vmap[sku_attr] = entry
    listing.variant_source_map = vmap
    flag_modified(listing, "variant_source_map")
    db.commit()
    return {"listing_id": listing_id, "sku_attr": sku_attr,
            "aliexpress_id": str(aliexpress_id),
            "alt_sku_attr": alt_sku_attr, "variant_source_map": vmap}


def clear_variant_alt_source(db: Session, *, listing_id: int, sku_attr: str) -> dict:
    """Per-Variante-Ausweich-Quelle wieder loesen. Ist die Variante ausverkauft, wird
    sie beim naechsten Bestands-Sync (oder sofort per resync) auf eBay auf 0 gesetzt."""
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    vmap = dict(listing.variant_source_map or {})
    vmap.pop(sku_attr, None)
    listing.variant_source_map = vmap or None
    flag_modified(listing, "variant_source_map")
    db.commit()
    return {"listing_id": listing_id, "sku_attr": sku_attr, "cleared": True,
            "variant_source_map": vmap}


def set_self_stock(db: Session, *, listing_id: int, sku_attr: str, qty: int,
                   cost_eur: float | None = None, price_eur: float | None = None) -> dict:
    """Eine Variante als EIGENBESTAND markieren: eigene Menge/EK/Preis, keine AliExpress-Quelle
    noetig. Solange qty > 0 gilt sie als lieferbar; qty = 0 -> wieder ausverkauft. Der EK geht
    mit dem eigenen ``cost_eur`` (oft guenstigerer Versand) in Gewinn/Marge ein."""
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    try:
        q = max(0, int(qty))
    except (TypeError, ValueError):
        raise PersistentError("Ungueltige Menge")
    ss = dict(listing.self_stock or {})
    # MERGE mit bestehendem Eintrag: ein reines Mengen-Update (ohne cost/price) darf den
    # zuvor gesetzten EK/Preis NICHT verwerfen.
    prev = ss.get(str(sku_attr)) if isinstance(ss.get(str(sku_attr)), dict) else {}
    entry: dict = dict(prev)
    entry["qty"] = q
    if cost_eur is not None:
        entry["cost_eur"] = round(float(cost_eur), 2)
    if price_eur is not None:
        entry["price_eur"] = round(float(price_eur), 2)
    ss[str(sku_attr)] = entry
    listing.self_stock = ss
    flag_modified(listing, "self_stock")
    db.commit()
    return {"listing_id": listing_id, "sku_attr": sku_attr, "self_stock": ss}


def clear_self_stock(db: Session, *, listing_id: int, sku_attr: str) -> dict:
    """Eigenbestand einer Variante entfernen -> sie faellt wieder auf die AliExpress-Quelle
    (bzw. gilt als ausverkauft, wenn dort kein Bestand)."""
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    ss = dict(listing.self_stock or {})
    ss.pop(str(sku_attr), None)
    listing.self_stock = ss or None
    flag_modified(listing, "self_stock")
    db.commit()
    return {"listing_id": listing_id, "sku_attr": sku_attr, "cleared": True, "self_stock": ss}


def parse_variant_alt(entry) -> dict | None:
    """ZENTRALER Leser fuer variant_source_map-Werte (einziger Ort mit Format-Wissen).

    Legacy-String (nur Quellen-ID) ODER Dict (mit Ziel-Variante) -> normalisiert:
    {source, sku_attr, sku_id, name, image, linked_at}. Kaputter Eintrag -> None
    (konservativ = wie unverknuepft)."""
    if not entry:
        return None
    if isinstance(entry, str):
        return {"source": entry, "sku_attr": None, "sku_id": None,
                "name": None, "image": None, "linked_at": None}
    if isinstance(entry, dict) and (entry.get("source") or entry.get("id")):
        return {"source": str(entry.get("source") or entry.get("id")),
                "sku_attr": entry.get("sku_attr") or None,
                "sku_id": entry.get("sku_id"),
                "name": entry.get("name") or None,
                "image": entry.get("image") or None,
                "linked_at": entry.get("linked_at")}
    return None


def variant_alt_availability(product: Product | None, entry, *, need: int = 1) -> dict:
    """Ist die verknuepfte Ausweich-Variante LIEFERBAR (laut letztem Slot-Snapshot)?

    Fail-safe-Matrix (Design 11.07.):
    - Slot weg/ausverkauft -> False; Legacy-Eintrag oder Slot ohne SKU-Snapshot ->
      Ganze-Quelle-in_stock (bisheriges Verhalten, konvergiert nach dem naechsten Scrape);
    - Ziel-attr nicht (mehr) im Snapshot (und nicht truncated) -> False (Drift);
    - gespeicherte sku_id weicht vom Snapshot ab -> False (Variante umbenannt/ersetzt);
    - Bestand bekannt < need -> False; Bestand unbekannt + Slot lieferbar -> True.
    Die BESTELL-Wahrheit bleibt der Live-Scrape in fulfill_sale – dieser Check steuert
    nur Anzeige + eBay-Menge."""
    ref = parse_variant_alt(entry)
    if ref is None or product is None:
        return {"available": False, "reason": "nicht verknuepft", "ref": None, "sku": None}
    alt = (product.alternatives or {}) if isinstance(product.alternatives, dict) else {}
    slot = next((s for s in (alt.get("sources") or [])
                 if str(s.get("aliexpress_id")) == ref["source"]), None)
    if slot is None:
        return {"available": False, "reason": "Quelle nicht mehr hinterlegt", "ref": ref, "sku": None}
    if slot.get("in_stock") is False:
        return {"available": False, "reason": "Ausweich-Quelle ausverkauft", "ref": ref, "sku": None}
    if not ref["sku_attr"] or not slot.get("skus"):
        # Legacy-Verknuepfung bzw. noch kein Snapshot -> bisheriges Ganze-Quelle-Verhalten
        return {"available": bool(slot.get("in_stock", True)),
                "reason": "ganze Quelle (keine Ziel-Variante verknuepft)"
                          if not ref["sku_attr"] else "Snapshot fehlt noch (naechster Abgleich)",
                "ref": ref, "sku": None}
    sku = next((sk for sk in slot["skus"] if sk.get("attr") == ref["sku_attr"]), None)
    if sku is None:
        if slot.get("skus_truncated"):
            return {"available": bool(slot.get("in_stock", True)),
                    "reason": "Snapshot gekappt – Live-Check bei Bestellung", "ref": ref, "sku": None}
        return {"available": False, "reason": "Ziel-Variante nicht mehr in der Quelle – neu verknuepfen",
                "ref": ref, "sku": None}
    if ref["sku_id"] and sku.get("id") and str(sku["id"]) != str(ref["sku_id"]):
        return {"available": False, "reason": "Ziel-Variante geaendert (SKU-ID weicht ab) – neu verknuepfen",
                "ref": ref, "sku": sku}
    try:
        stock = int(float(str(sku.get("stock")).replace(",", ""))) if sku.get("stock") not in (None, "") else None
    except (TypeError, ValueError):
        stock = None
    if stock is not None and stock < max(1, need):
        return {"available": False, "reason": f"Ziel-Variante ausverkauft (Bestand {stock})",
                "ref": ref, "sku": sku}
    return {"available": True, "reason": "Ziel-Variante lieferbar", "ref": ref, "sku": sku}


def _auto_match_variants(listing: Listing, source_pid: str,
                         old_skus: list, new_skus: list) -> dict:
    """eBay-Varianten (alte Hauptquellen-SKUs) automatisch den SKUs der NEUEN Quelle
    zuordnen — mit der bestell-erprobten Aufloesung inkl. Mehrdeutigkeits-Sperre.
    Nicht-Treffer bleiben leer (manuelle Zuordnung). Rueckgabe: {matched, total}."""
    from app.services.order_service import _learn_variant, resolve_selection_against_skus
    matched = total = 0
    for v in (old_skus or []):
        sel = (v or {}).get("options") or {}
        if not sel:
            continue
        total += 1
        hit = resolve_selection_against_skus(sel, new_skus, listing=listing,
                                             source_id=source_pid)
        if hit and hit.get("attr"):
            # Neue HAUPTquelle -> Legacy-Key erlaubt (den liest die Hauptquellen-Aufloesung).
            if _learn_variant(listing, source_pid, sel, hit["attr"], write_legacy=True):
                matched += 1
    if matched:
        flag_modified(listing, "variant_map")
    return {"matched": matched, "total": total}


def _invalidate_variant_links(db: Session, product: Product, *, reason: str) -> int:
    """Varianten-Verknuepfungen ALLER Listings dieses Produkts loeschen (Kollisions-Killer).

    variant_source_map-Keys sind PRIMAER-sku_attrs; AliExpress-Attr-IDs sind global
    (\"14:691#Black\" existiert auf vielen Produkten). Nach einem Hauptquellen-Wechsel
    koennten alte Keys zufaellig auf Attrs der NEUEN Quelle matchen -> falsche Variante
    bliebe sellable/wuerde falsch geroutet. Kein eBay-Objekt wird angefasst; betroffene
    OOS-Varianten fallen auf das sichere Menge-0-Verhalten zurueck."""
    cleared = 0
    for l in db.scalars(select(Listing).where(Listing.product_id == product.id)).all():
        if l.variant_source_map:
            l.variant_source_map = None
            flag_modified(l, "variant_source_map")
            cleared += 1
    if cleared:
        logger.warning("variant alt links cleared", extra={"product_id": product.id,
                                                           "reason": reason, "listings": cleared})
    return cleared


async def source_variant_options(db: Session, *, listing_id: int, aliexpress_id: str,
                                 for_attr: str | None = None,
                                 sel: dict | None = None) -> dict:
    """Varianten EINES hinterlegten Quellen-Slots zum AUSWAEHLEN (read-only, scrapt live).

    Fuer den Picker im Preis-Check: Karten mit Bild/Preis/Bestand je Alt-Variante.
    Aktualisiert nebenbei den Slot-Snapshot (Netz-Call VOR dem kurzen Commit,
    SQLite-Lock-Regel). Bestellt NICHTS.

    ``for_attr``/``sel``: Ziel-Variante (lokale attr ODER fertiges Auswahl-Dict) —
    dann bestimmt die Bestell-Maschinerie die VERMUTLICH passende Quell-Variante,
    geliefert als ``suggested_attr`` (gruen im Picker, wie beim Bestellen)."""
    listing = db.get(Listing, listing_id)
    product = db.get(Product, listing.product_id) if listing and listing.product_id else None
    if product is None:
        raise PersistentError("Kein Produkt zu diesem Listing")
    alt = ensure_primary_slot(product)
    slot = next((s for s in (alt.get("sources") or [])
                 if str(s.get("aliexpress_id")) == str(aliexpress_id)), None)
    if slot is None:
        raise PersistentError("Diese Quelle ist nicht hinterlegt – erst als Slot hinzufuegen")
    slot_url = slot["url"]
    try:
        scraped = await _real_ae().scrape_product(slot_url)
    except Exception as exc:  # noqa: BLE001 – Picker soll Fehler sauber melden
        raise PersistentError(f"Quelle nicht ladbar: {str(exc)[:150]}")
    # LOST-UPDATE-SCHUTZ: Waehrend des (sekundenlangen) Netz-Calls kann ein anderer
    # Schreiber committen (Monitoring-Slot-Refresh, remove_source, Failover). Darum den
    # Zustand FRISCH laden und den Slot neu suchen, statt den Vor-Scrape-Stand
    # zurueckzuschreiben (sonst wuerden entfernte Slots wiederbelebt o.ae.).
    db.rollback()
    db.refresh(product)
    alt = _alt_dict(product)
    slot = next((s for s in (alt.get("sources") or [])
                 if str(s.get("aliexpress_id")) == str(aliexpress_id)), None)
    sk, truncated = _slot_skus_snapshot(scraped)
    if slot is not None:   # Slot wurde inzwischen entfernt -> nichts persistieren
        slot["skus"] = sk
        slot["skus_truncated"] = truncated
        slot["in_stock"] = bool(getattr(scraped, "in_stock", True))
        slot["checked_at"] = _now_iso()
        product.alternatives = alt
        flag_modified(product, "alternatives")
        db.commit()
    s = get_settings()
    options = []
    for v in sk:
        try:
            ek = round(pricing.effective_cost(float(v["price"]), settings=s), 2) if v.get("price") is not None else None
        except (TypeError, ValueError):
            ek = None
        options.append({"attr": v.get("attr"), "id": v.get("id"),
                        "name": " / ".join(str(x) for x in (v.get("options") or {}).values()) or (v.get("attr") or ""),
                        "price": v.get("price"), "ek_eur": ek,
                        "stock": v.get("stock"), "image": v.get("image")})
    # Vermutete Ziel-Variante (wie beim Bestellen): fail-closed, None bei Mehrdeutigkeit.
    suggested = None
    sel_dict = sel if isinstance(sel, dict) and sel else None
    if sel_dict is None and for_attr:
        local = next((x for x in ((product.variants or {}).get("skus") or [])
                      if x.get("attr") == for_attr), None)
        sel_dict = (local or {}).get("options") or None
    if sel_dict:
        try:
            from app.services.order_service import resolve_selection_against_skus
            hit = resolve_selection_against_skus(sel_dict, sk, listing=listing,
                                                 source_id=str(aliexpress_id))
            suggested = (hit or {}).get("attr")
        except Exception:  # noqa: BLE001 – nur ein Anzeige-Hinweis
            suggested = None
    # slot kann nach rollback/refresh None sein (Primaer-Slot nie persistiert / parallel
    # entfernt) -> Anzeige-Felder defensiv, die Optionen kommen ohnehin aus dem Scrape.
    return {"listing_id": listing_id, "aliexpress_id": str(aliexpress_id),
            "title": (slot or {}).get("title"),
            "url": (slot or {}).get("url") or slot_url,
            "in_stock": bool(getattr(scraped, "in_stock", True)),
            "options": options, "truncated": truncated,
            "suggested_attr": suggested}


async def set_primary(db: Session, *, listing_id: int, aliexpress_id: str) -> dict:
    """Vorhandenen Slot zur Hauptquelle machen (frisch nachgescraped)."""
    listing = db.get(Listing, listing_id)
    product = db.get(Product, listing.product_id) if listing and listing.product_id else None
    if product is None:
        raise PersistentError("Kein Produkt zu diesem Listing")
    alt = ensure_primary_slot(product)
    match = next((s for s in (alt.get("sources") or [])
                  if str(s.get("aliexpress_id")) == str(aliexpress_id)), None)
    if match is None:
        raise PersistentError("Quelle nicht in den Slots gefunden")
    return await add_source(db, listing_id=listing_id, url=match["url"],
                            primary=True, added=match.get("added") or "manual")


async def remove_source(db: Session, *, listing_id: int, aliexpress_id: str | None = None,
                        slot_index: int | None = None) -> dict:
    """EINE Quelle entfernen – auch die Hauptquelle (falscher Match).

    Bevorzugt ``slot_index`` (Position im Panel) -> entfernt GENAU diesen Slot,
    eindeutig auch bei doppelten/leeren aliexpress_id (fruehere Ursache dafuer,
    dass versehentlich alle gleich-ID-Slots verschwanden). Fallback: erstes
    Vorkommen der ``aliexpress_id``.

    * Hauptquelle weg + weitere Slots -> naechster Slot wird Hauptquelle
      (frisch nachgescraped, damit Varianten/Preise stimmen).
    * Letzte Quelle weg -> Listing wird vom Produkt GETRENNT ("ohne Quelle").
    """
    listing = db.get(Listing, listing_id)
    product = db.get(Product, listing.product_id) if listing and listing.product_id else None
    if product is None:
        raise PersistentError("Kein Produkt zu diesem Listing")
    alt = ensure_primary_slot(product)
    before = alt.get("sources") or []

    if slot_index is not None and 0 <= slot_index < len(before):
        idx = slot_index
    else:
        idx = next((i for i, s in enumerate(before)
                    if str(s.get("aliexpress_id")) == str(aliexpress_id)), None)
    if idx is None:
        raise PersistentError("Quelle nicht gefunden")
    aliexpress_id = str(before[idx].get("aliexpress_id") or "")
    remaining = [s for i, s in enumerate(before) if i != idx]   # GENAU einen Slot raus
    was_primary = idx == 0
    shared = db.scalar(select(func.count(Listing.id))
                       .where(Listing.product_id == product.id, Listing.id != listing.id))
    # Wird die HAUPTQUELLE entfernt und der naechste Slot gehoert bereits einem ANDEREN Produkt
    # (aliexpress_id UNIQUE), darf die ID dieses Produkts nicht ueberschrieben werden -> wie
    # "geteilt" behandeln: dieses Listing sauber auf das vorhandene Produkt umhaengen (Fund 20.07.).
    next_owner = False
    if was_primary and remaining:
        _npid = str(remaining[0].get("aliexpress_id") or "")
        if _npid:
            next_owner = db.scalar(select(Product.id).where(
                Product.aliexpress_id == _npid, Product.id != product.id)) is not None
    # Varianten-Verknuepfungen, die auf den ENTFERNTEN Slot zeigen, loesen (betroffene
    # OOS-Varianten fallen auf Menge-0 zurueck). Bei GETEILTEM Produkt NUR das handelnde
    # Listing: die Slots des geteilten Produkts werden fuer die anderen Listings nie
    # mutiert – deren Verknuepfungen bleiben gueltig und muessen ueberleben.
    links_cleared = 0
    if aliexpress_id:
        targets = ([listing] if shared else
                   db.scalars(select(Listing).where(Listing.product_id == product.id)).all())
        for l in targets:
            vmap = dict(l.variant_source_map or {})
            drop = [k for k, v in vmap.items()
                    if (parse_variant_alt(v) or {}).get("source") == aliexpress_id]
            for k in drop:
                vmap.pop(k, None)
            if drop:
                l.variant_source_map = vmap or None
                flag_modified(l, "variant_source_map")
                links_cleared += len(drop)
        if links_cleared:
            logger.warning("variant alt links cleared", extra={
                "product_id": product.id, "reason": f"source removed {aliexpress_id}",
                "links": links_cleared})

    if not remaining:
        # Kompletter Fehl-Match: Listing zurueck auf "ohne Quelle". Produkt-Slots nur
        # leeren, wenn KEIN anderes Listing dasselbe Produkt nutzt (geteilte Matches).
        if not shared:
            alt["sources"] = []
            alt.pop("confirmed", None)
            _recalc_averages(alt)
            product.alternatives = alt
            flag_modified(product, "alternatives")
        listing.product_id = None
        listing.auto_reprice = False
        listing.supplier_in_stock = True
        db.commit()
        return {"listing_id": listing_id, "sources": [], "unlinked": True}

    if shared or next_owner:
        # Geteiltes Produkt NIE mutieren (andere Listings rechnen damit weiter) ODER die Ziel-ID
        # gehoert schon einem anderen Produkt: dieses Listing mit den verbleibenden Slots sauber
        # auf ein eigenes/vorhandenes Produkt abkoppeln. Schlaegt der Scrape fehl, bleibt alles
        # unveraendert (kein halber Zustand).
        return await add_source(db, listing_id=listing_id, url=remaining[0]["url"],
                                primary=True, added=remaining[0].get("added") or "manual",
                                carry_slots=remaining[1:],
                                carry_confirmed=alt.get("confirmed"))

    if was_primary:
        # ERST den naechsten Slot erfolgreich zur Hauptquelle machen (Scrape kann
        # fehlschlagen -> dann bleibt der alte Zustand komplett intakt), DANN den
        # entfernten Slot austragen. Kein Commit vor gelungenem Scrape.
        result = await add_source(db, listing_id=listing_id, url=remaining[0]["url"],
                                  primary=True, added=remaining[0].get("added") or "manual",
                                  carry_confirmed=alt.get("confirmed"))
        alt2 = _alt_dict(product)
        alt2["sources"] = [s for s in (alt2.get("sources") or [])
                           if str(s.get("aliexpress_id")) != str(aliexpress_id)]
        _recalc_averages(alt2)
        product.alternatives = alt2
        flag_modified(product, "alternatives")
        db.commit()
        result["sources"] = alt2["sources"]
        result["avg_ek_eur"] = alt2.get("avg_ek_eur")
        result["avg_price_eur"] = alt2.get("avg_price_eur")
        return result

    alt["sources"] = remaining
    _recalc_averages(alt)
    product.alternatives = alt
    flag_modified(product, "alternatives")
    db.commit()
    return {"listing_id": listing_id, "sources": remaining,
            "avg_ek_eur": alt.get("avg_ek_eur"), "avg_price_eur": alt.get("avg_price_eur")}


def remove_alternative_item(db: Session, *, listing_id: int, aliexpress_id: str | None = None,
                            item_index: int | None = None) -> dict:
    """EINEN Eintrag aus den Bildsuche-ALTERNATIVEN entfernen. ``item_index``
    (Position) bevorzugt -> genau dieser Eintrag, eindeutig auch bei doppelten IDs."""
    listing = db.get(Listing, listing_id)
    product = db.get(Product, listing.product_id) if listing and listing.product_id else None
    if product is None:
        raise PersistentError("Kein Produkt zu diesem Listing")
    alt = _alt_dict(product)
    before = alt.get("items") or []
    if item_index is not None and 0 <= item_index < len(before):
        idx = item_index
    else:
        idx = next((i for i, it in enumerate(before)
                    if str(it.get("aliexpress_id")) == str(aliexpress_id)), None)
    if idx is None:
        raise PersistentError("Alternative nicht gefunden")
    alt["items"] = [it for i, it in enumerate(before) if i != idx]   # GENAU einen raus
    product.alternatives = alt
    flag_modified(product, "alternatives")
    db.commit()
    return {"listing_id": listing_id, "items": alt["items"]}


async def enrich_suggestion_images(db: Session, *, listing_id: int, limit: int = 6) -> dict:
    """Fehlende Bilder der Bildsuche-Vorschlaege (alt['items']) on-demand nachladen.

    Aeltere Vorschlaege wurden ohne Bild-URL gespeichert; damit man sie im Panel
    vergleichen kann OHNE auf AliExpress zu gehen, wird das Hauptbild je Vorschlag
    OHNE Bild einmal nachgescraped (parallel, max ``limit``) und persistiert (gecacht).
    Vorschlaege MIT Bild bleiben unberuehrt (kein unnoetiger API-Call).
    """
    listing = db.get(Listing, listing_id)
    product = db.get(Product, listing.product_id) if listing and listing.product_id else None
    if product is None:
        return {"listing_id": listing_id, "items": []}
    alt = product.alternatives if isinstance(product.alternatives, dict) else {}
    items = alt.get("items") or []
    missing = [(i, it) for i, it in enumerate(items)
               if not it.get("image") and it.get("url")][:limit]
    if not missing:
        return {"listing_id": listing_id, "items": items}
    ae = _real_ae()

    async def _one(idx: int, it: dict):
        try:
            sc = await ae.scrape_product(it["url"])
            imgs = getattr(sc, "images", None) or []
            return idx, (imgs[0] if imgs else None)
        except Exception:  # noqa: BLE001 – ein fehlender Vorschlag darf die anderen nicht kippen
            return idx, None

    results = await asyncio.gather(*[_one(i, it) for i, it in missing])
    changed = False
    for idx, img in results:
        if img:
            items[idx]["image"] = img
            changed = True
    if changed:
        alt["items"] = items
        product.alternatives = alt
        flag_modified(product, "alternatives")
        db.commit()
    return {"listing_id": listing_id, "items": items}


async def name_search_candidates(db: Session, *, listing_id: int,
                                 query: str | None = None, limit: int = 6) -> dict:
    """Zweiter Match-Weg: AliExpress-TEXTSUCHE nach dem Artikelnamen (z.B. 'HTC NE70').

    Liefert Kandidaten mit Preis/EK zum Uebernehmen – ergaenzt die Bildsuche dort,
    wo sie falsche oder teure Treffer geliefert hat.
    """
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    q = (query or "").strip() or _search_query_from_title(listing.title_seo or "")
    if not q:
        raise PersistentError("Kein Suchbegriff ableitbar – bitte Begriff mitgeben.")
    hits = await _relevance_text_search(_real_ae(), q, page_size=max(12, limit * 3))
    # Modellnummern-Token (z.B. 'NE70') zuerst: Kandidaten, deren Titel das Token
    # enthaelt, sind mit hoher Sicherheit DER Artikel – Rest ist Umfeld-Rauschen.
    sig = [t.lower() for t in q.split() if any(c.isdigit() for c in t) and len(t) >= 3]
    if sig:
        hits.sort(key=lambda h: 0 if any(t in (h.get("title") or "").lower() for t in sig) else 1)
    s = get_settings()
    out = []
    for h in hits:
        if not h.get("price"):
            continue
        out.append({"aliexpress_id": h["id"], "url": h["url"],
                    "title": (h.get("title") or "")[:90], "image": h.get("image"),
                    "price_eur": h["price"], "orders": h.get("orders"),
                    "ek_eur": round(pricing.effective_cost(h["price"], settings=s), 2),
                    "model_match": bool(sig and any(t in (h.get("title") or "").lower()
                                                    for t in sig))})
        if len(out) >= limit:
            break
    return {"listing_id": listing_id, "query": q, "candidates": out}


async def _relevance_text_search(ae, keyword: str, *, page_size: int = 12) -> list[dict]:
    """ds.text.search OHNE Bestseller-Sortierung (Relevanz statt orders,desc).

    Die Research-Variante sortiert nach Verkaeufen – gut fuer Produktideen, schlecht
    fuer 'finde GENAU diesen Artikel'.
    """
    from app.services.product_research_service import _resp_root, _f, _orders_to_int, _norm_url
    data = await ae._call("aliexpress.ds.text.search", {
        "keyWord": keyword, "local": "de_DE", "countryCode": "DE",
        "currency": "EUR", "pageSize": str(page_size), "pageIndex": "1",
    })
    root = _resp_root(data)
    prods = (((root.get("data") or {}).get("products") or {}).get("selection_search_product")) or []
    if isinstance(prods, dict):
        prods = [prods]
    out = []
    for p in prods:
        pid = str(p.get("itemId") or p.get("item_id") or "")
        if not pid:
            continue
        out.append({"id": pid, "title": p.get("title"), "image": p.get("itemMainPic"),
                    "orders": _orders_to_int(p.get("orders")),
                    "price": _f(p.get("targetSalePrice")),
                    "url": _norm_url(p.get("itemUrl"), pid)})
    return out


_STOP_WORDS = {"mit", "und", "für", "fuer", "der", "die", "das", "in", "aus", "von",
               "neu", "set", "stück", "stueck", "premium", "original", "hochwertig"}


def _search_query_from_title(title: str) -> str:
    """SEO-Titel -> KURZE Suchanfrage: Modell-Token (HTC, NE70, X9…) + erstes Substantiv.

    Kurz ist besser: 'HTC NE70 Kopfhörer' findet den Artikel, der volle SEO-Titel
    findet Bestseller-Rauschen.
    """
    words = [w.strip(",.–-()|/") for w in (title or "").split()]
    words = [w for w in words if len(w) > 1 and w.lower() not in _STOP_WORDS]
    # Modell-Kandidaten: GROSSBUCHSTABEN-Tokens oder Tokens mit Ziffern (NE70, X9, 30W)
    model = [w for w in words[:5] if w.isupper() or any(c.isdigit() for c in w)]
    rest = [w for w in words if w not in model]
    return " ".join((model[:3] + rest[:2])[:4] if model else words[:3])


async def refresh_sources(db: Session, *, listing_id: int) -> dict:
    """Alle Slots eines Listings frisch prüfen (Preis + Bestand); Hauptquelle spiegeln."""
    listing = db.get(Listing, listing_id)
    product = db.get(Product, listing.product_id) if listing and listing.product_id else None
    if product is None:
        raise PersistentError("Kein Produkt zu diesem Listing")
    alt = ensure_primary_slot(product)
    ae = _real_ae()
    refreshed = []
    for i, slot in enumerate(alt.get("sources") or []):
        try:
            scraped = await ae.scrape_product(slot["url"])
            fresh = _slot_from_scrape(scraped, slot["url"], slot.get("added") or "auto")
            refreshed.append(fresh)
            if i == 0:
                old_pid = str(product.aliexpress_id or "")
                _apply_primary(product, scraped, slot["url"])
                # Identitaet der Hauptquelle geaendert (AliExpress-Merge/Redirect) ->
                # Varianten-Verknuepfungen loeschen (Keys = Primaer-attrs der ALTEN Quelle).
                if old_pid and old_pid != str(scraped.aliexpress_id):
                    _invalidate_variant_links(
                        db, product,
                        reason=f"refresh primary {old_pid} -> {scraped.aliexpress_id}")
                listing.supplier_in_stock = fresh["in_stock"]
        except (OutOfStockError, ProductNotFoundError):
            # Produkt wirklich weg/leer -> ausverkauft markieren
            slot = {**slot, "in_stock": False, "checked_at": _now_iso()}
            refreshed.append(slot)
            if i == 0:
                listing.supplier_in_stock = False
        except Exception as exc:  # noqa: BLE001 – API-/Netzfehler: Zustand NICHT aendern
            logger.warning("source refresh failed (transient?)",
                           extra={"url": slot.get("url"), "error": str(exc)[:120]})
            refreshed.append(slot)
        await asyncio.sleep(_RATE_S)
    alt["sources"] = refreshed
    _recalc_averages(alt)
    product.alternatives = alt
    flag_modified(product, "alternatives")
    db.commit()
    return {"listing_id": listing_id, "sources": refreshed,
            "avg_ek_eur": alt.get("avg_ek_eur")}


_COLOR_SYN = {"schwarz": "black", "weiss": "white", "weiß": "white", "rot": "red",
              "blau": "blue", "gruen": "green", "grün": "green", "gelb": "yellow",
              "silber": "silver", "gold": "gold", "rosa": "pink", "pink": "pink",
              "lila": "purple", "violett": "purple", "grau": "gray", "braun": "brown",
              "orange": "orange", "beige": "beige", "tuerkis": "turquoise"}


def _value_tokens(v) -> set:
    """Wert -> Tokens. Zahlen von Buchstaben trennen ('65cm' == '65 cm') und deutsche
    Farben auf ihr englisches Pendant mappen (Alt-Quellen sind meist englisch)."""
    import re as _re
    # ß gehoert zur Buchstabenklasse, sonst zerreisst 'weiß' -> {'wei'} und der
    # _COLOR_SYN-Eintrag 'weiß':'white' (haeufigste dt. Schreibweise) greift nie.
    toks = set(_re.findall(r"\d+|[a-zäöüß]+", str(v).lower()))
    return toks | {_COLOR_SYN[t] for t in toks if t in _COLOR_SYN}


def best_guess_variant(selection: dict | None, skus_with_attr: list) -> dict:
    """Beste Variante + KONFIDENZ fuer die UI-VORWAHL bei mehrdeutigen Quellen (kein Auto-Kauf).

    Nutzerwunsch: bei JEDEM Sale eine Variante VORWAEHLEN -> idx ist immer gesetzt, solange es
    Kandidaten gibt (bester Score; Gleichstand -> niedrigster Index; null Ueberlappung / keine
    Kaeuferauswahl -> erste Variante). ABER confident=True nur bei ECHTER, EINDEUTIGER Wert-
    Ueberlappung. Bei confident=False ist die Vorwahl nur eine Schaetzung -> die UI verlangt einen
    bestaetigenden Klick, statt sie als „passend" order-bereit zu markieren (Geld-Schutz)."""
    if not skus_with_attr:
        return {"idx": None, "confident": False}
    sel = set()
    for k, v in (selection or {}).items():
        if k not in ("attr", "options", "id"):
            sel |= _value_tokens(v)
    if not sel:
        return {"idx": 0, "confident": False}   # keine Auswahl-Tokens -> Default, unsicher
    scored = []
    for i, x in enumerate(skus_with_attr):
        toks = set()
        for v in (x.get("options") or {}).values():
            toks |= _value_tokens(v)
        scored.append((len(sel & toks), i))
    scored.sort(key=lambda t: (-t[0], t[1]))   # hoechster Score, bei Gleichstand niedrigster Index
    best = scored[0]
    if best[0] == 0:
        return {"idx": 0, "confident": False}   # keine Ueberlappung -> Default, unsicher
    tie = len(scored) > 1 and scored[1][0] == best[0]
    return {"idx": best[1], "confident": not tie}   # Ueberlappung + eindeutig -> sicher


def best_guess_variant_idx(selection: dict | None, skus_with_attr: list) -> int | None:
    """Nur der Vorwahl-Index (Rueckwaerts-Kompat) – siehe best_guess_variant fuer die Konfidenz."""
    return best_guess_variant(selection, skus_with_attr)["idx"]


async def source_options_for_sale(db: Session, *, sale_id: int) -> dict:
    """QUELLEN-VERGLEICH fuer eine offene Bestellung: alle Slots frisch pruefen und
    als Bestell-Vorschlaege aufbereiten (guenstigste lieferbare Quelle zuerst).

    VORSCHLAG-ONLY (Geld-Regel): verknuepft/bestellt NICHTS – der Nutzer waehlt im
    UI explizit "Mit dieser Quelle bestellen". Je Quelle wird die Kaeufer-Variante
    gegen die FRISCHEN SKUs DIESER Quelle aufgeloest (sku_attrs sind quellen-
    spezifisch!); mehrdeutige Faelle bleiben zur manuellen Zuordnung offen.
    """
    from app.models import Sale
    from app.services import order_service as osvc

    sale = db.get(Sale, sale_id)
    if sale is None:
        raise PersistentError("Sale nicht gefunden")
    listing = db.get(Listing, sale.listing_id) if sale.listing_id else None
    product = db.get(Product, listing.product_id) if listing and listing.product_id else None
    if product is None:
        raise PersistentError("Keine AliExpress-Quelle am Listing")
    alt = ensure_primary_slot(product)
    sources = alt.get("sources") or []
    if not sources:
        raise PersistentError("Keine Quellen-Slots am Produkt")

    s = get_settings()
    ae = _real_ae()
    qty = sale.quantity or 1
    price_eur = float(sale.price_eur) if sale.price_eur else None
    options: list[dict] = []
    for i, slot in enumerate(sources):
        sid = str(slot.get("aliexpress_id") or "")
        entry = {
            "aliexpress_id": sid,
            "url": slot.get("url"),
            "title": slot.get("title"),
            "image": slot.get("image"),
            "is_primary": i == 0,
            "added": slot.get("added"),
            "in_stock": None, "price_eur": None, "ek_eur": None,
            "variant_attr": None, "variant_name": None, "variant_ambiguous": False,
            "variant_options": [],
            "eligible": False, "reason": None,
        }
        try:
            scraped = await ae.scrape_product(slot["url"])
        except (OutOfStockError, ProductNotFoundError):
            entry.update(in_stock=False, reason="ausverkauft/nicht mehr verfuegbar")
            options.append(entry)
            await asyncio.sleep(_RATE_S)
            continue
        except Exception as exc:  # noqa: BLE001
            entry.update(reason=f"Abruf fehlgeschlagen: {str(exc)[:80]}")
            options.append(entry)
            await asyncio.sleep(_RATE_S)
            continue

        skus = ((getattr(scraped, "variants", None) or {}).get("skus") or [])
        in_stock = bool(getattr(scraped, "in_stock", True))
        base_price = float(scraped.price_cny or 0) or None
        # Kaeufer-Variante gegen DIESE Quelle aufloesen (Regeln + je Quelle gelernte Map)
        resolved = None
        if sale.variant_selected and skus:
            resolved = osvc.resolve_selection_against_skus(
                sale.variant_selected, skus, listing=listing, source_id=sid)
        res_attr = (resolved or {}).get("attr")
        sku_price = None
        sku_stock = None
        if res_attr:
            hit = next((x for x in skus if x.get("attr") == res_attr), None)
            if hit is not None:
                try:
                    sku_price = float(hit["price"]) if hit.get("price") is not None else None
                except (TypeError, ValueError):
                    sku_price = None
                sku_stock = hit.get("stock")
                entry["variant_attr"] = res_attr
                entry["variant_name"] = " · ".join(
                    str(v) for v in (hit.get("options") or {}).values()) or res_attr
        ambiguous = bool(sale.variant_selected and skus and not res_attr)
        # Vorwahl NUR bei Mehrdeutigkeit: die HAUPTQUELLE kennt die verkaufte Variante determi-
        # nistisch aus UNSERER Publish-SKU ({base}-V{i}) – gerade wenn die AliExpress-Optionsnamen
        # nichtssagend/gleich sind (Wert-Matching unmoeglich, Fall #1161). Sie FUELLT nur die Luecke
        # und ueberschreibt NIE eine bereits verifizierte Aufloesung (res_attr laeuft ueber
        # _baked_attr_intact/Score und ist robuster gegen Positions-Drift durch delete_variant).
        # Sonst Wert-Best-Guess (Ausweichquellen). Braucht die am Sale gespeicherte ebay_sku.
        sug_idx, sug_conf, sug_pos = None, False, False
        if ambiguous:
            _cands = [x for x in skus if x.get("attr")]
            if entry["is_primary"] and product is not None:
                _eb = (sale.variant_selected or {}).get("ebay_sku")
                _det = osvc._attr_from_ebay_variation_sku(listing, product, _eb) if _eb else None
                _pa = (_det or {}).get("attr")
                if _pa:
                    sug_idx = next((k for k, x in enumerate(_cands) if x.get("attr") == _pa), None)
                    sug_pos = sug_idx is not None
            if sug_idx is None:
                _g = best_guess_variant(sale.variant_selected, _cands)
                sug_idx, sug_conf = _g["idx"], _g["confident"]
        eff_price = sku_price if sku_price is not None else base_price
        ek = round(pricing.effective_cost(eff_price, settings=s), 2) if eff_price else None
        profit = margin = None
        if ek is not None and price_eur:
            # Kategorie-genaue Gebuehr inkl. Anzeigenrate + 19% MwSt (nie rohes ebay_fee_pct,
            # sonst Gewinn der Quellen-Vergleichs-Tabelle ~0,9-2€ zu hoch je Verkauf).
            profit = round(price_eur - price_eur * pricing.effective_fee_pct_for_listing(listing, settings=s)
                           - pricing.ebay_fixed_fee(s) - ek * qty, 2)
            margin = round(profit / price_eur, 4)
        variant_missing = bool(sale.variant_selected and not skus)
        eligible = bool(in_stock and eff_price and not ambiguous and not variant_missing
                        and (sku_stock is None or sku_stock > 0))
        reason = None
        if not in_stock:
            reason = "ausverkauft"
        elif variant_missing:
            reason = "Quelle liefert keine Varianten (falsches Produkt?)"
        elif ambiguous:
            reason = "Variante mehrdeutig – manuell zuordnen"
        elif sku_stock is not None and sku_stock <= 0:
            reason = "gewaehlte Variante ausverkauft"
        entry.update({
            "in_stock": in_stock,
            "price_eur": round(eff_price, 2) if eff_price else None,
            "ek_eur": ek,
            "profit_eur": profit,
            "margin": margin,
            "variant_ambiguous": ambiguous,
            "eligible": eligible,
            "reason": reason,
            # Bei Mehrdeutigkeit: Optionen MIT BILD zum manuellen Waehlen im UI + eine VORWAHL:
            # Hauptquelle -> deterministisch aus unserer Publish-SKU (variant_suggested_position),
            # sonst Wert-Best-Guess. Der Nutzer checkt nur gegen, statt blind zu suchen.
            "variant_options": ([{
                "attr": x.get("attr"),
                "name": " · ".join(str(v) for v in (x.get("options") or {}).values()) or x.get("attr"),
                "price": x.get("price"), "stock": x.get("stock"), "image": x.get("image"),
            } for x in skus if x.get("attr")] if ambiguous else []),
            "variant_suggested_idx": sug_idx,
            "variant_suggested_confident": sug_conf,
            "variant_suggested_position": sug_pos,
        })
        options.append(entry)
        await asyncio.sleep(_RATE_S)

    primary_ek = next((o.get("ek_eur") for o in options if o["is_primary"]), None)
    for o in options:
        o["savings_vs_primary_eur"] = (round((primary_ek - o["ek_eur"]) * qty, 2)
                                       if (primary_ek is not None and o.get("ek_eur") is not None
                                           and not o["is_primary"]) else None)
    ranked = sorted(options, key=lambda o: (not o["eligible"],
                                            o["ek_eur"] if o["ek_eur"] is not None else 9e9))
    best = next((o for o in ranked if o["eligible"]), None)
    return {
        "sale_id": sale_id,
        "listing_id": listing.id if listing else None,
        "selected": sale.variant_selected,
        "ebay_image": listing.image_url if listing else None,
        "confirmed": bool(alt.get("confirmed")),
        "options": ranked,
        "recommended_aliexpress_id": best["aliexpress_id"] if best else None,
        "recommendation_note": ("Guenstigste lieferbare Quelle – Bestellung erst nach "
                                "deinem Klick." if best else
                                "Keine Quelle automatisch bestellbar – Angaben pruefen."),
    }


def confirm_match(db: Session, *, listing_id: int) -> dict:
    """Nutzer bestaetigt: AliExpress-Quelle == eBay-Artikel. Hebt den ⚠-Verdacht
    dauerhaft auf und nimmt das Listing wieder voll ins Monitoring."""
    listing = db.get(Listing, listing_id)
    product = db.get(Product, listing.product_id) if listing and listing.product_id else None
    if product is None:
        raise PersistentError("Kein Produkt zu diesem Listing")
    alt = ensure_primary_slot(product)
    alt["confirmed"] = True
    alt["verified_at"] = _now_iso()
    product.alternatives = alt
    flag_modified(product, "alternatives")
    db.commit()
    return {"listing_id": listing_id, "confirmed": True, "verified_at": alt["verified_at"]}


async def failover_primary(db: Session, *, listing, product) -> tuple[bool, object]:
    """Hauptquelle tot/ausverkauft -> lieferbare AUSWEICH-Quelle automatisch zur
    Hauptquelle machen (Listing bleibt verkaufsfaehig statt Menge 0).

    Der tote Primary bleibt als (ausverkauft markierter) Slot erhalten – Anbieter
    fuellen oft wieder auf. Rueckgabe: (umgeschaltet?, frischer Scrape | None).
    """
    alt = _alt_dict(product)
    sources = alt.get("sources") or []
    if len(sources) < 2:
        return False, None
    # GETEILTE Produkte nie automatisch umschreiben (andere Listings rechnen damit) –
    # dort bleibt der sichere OOS-Pfad (Menge 0) + manuelle Korrektur im Preis-Check.
    shared = db.scalar(select(func.count(Listing.id))
                       .where(Listing.product_id == product.id, Listing.id != listing.id))
    if shared:
        return False, None
    ae = _real_ae()
    for slot in sources[1:]:
        if slot.get("in_stock") is False:
            continue
        try:
            scraped = await ae.scrape_product(slot["url"])
        except Exception:  # noqa: BLE001 – naechsten Slot probieren
            continue
        if not getattr(scraped, "in_stock", False):
            continue
        # Fail-open-in_stock ist KEIN positiver Beweis (Review-Fund 16.08.): fuer den
        # destruktiven Quellen-Wechsel (invalidiert Varianten-Verknuepfungen!) braucht
        # es gemeldeten Bestand — ausser das Produkt hat gar keine SKU-Liste (dort
        # gibt es nie Bestandsfelder; der erfolgreiche Abruf zaehlt wie bisher).
        _skus = ((getattr(scraped, "variants", None) or {}).get("skus") or [])
        if _skus and not getattr(scraped, "stock_reported", True):
            continue
        pid = str(scraped.aliexpress_id)
        fresh = _slot_from_scrape(scraped, slot["url"], slot.get("added") or "auto")
        others = []
        for j, s in enumerate(sources):
            if str(s.get("aliexpress_id")) == pid:
                continue
            if j == 0:   # alter, toter Primary: als ausverkauft markiert behalten
                s = {**s, "in_stock": False, "checked_at": _now_iso()}
            others.append(s)
        alt["sources"] = ([fresh] + others)[:MAX_SOURCES]
        _apply_primary(product, scraped, slot["url"])
        # Hauptquelle gewechselt -> Varianten-Verknuepfungen sind ungueltig (Keys =
        # Primaer-attrs der ALTEN Quelle; globale AE-Attr-IDs koennten zufaellig matchen).
        _invalidate_variant_links(db, product, reason=f"failover -> {pid}")
        _recalc_averages(alt)
        product.alternatives = alt
        flag_modified(product, "alternatives")
        base = fresh.get("price_max_eur") or fresh.get("price_eur")
        if base:
            from app.services.fast_shipping_service import variants_have_eu_warehouse as _vheu
            listing.cost_eur = Decimal(str(pricing.effective_cost(
                base, local=_vheu(product))))
        # KEIN supplier_in_stock=True hier: das macht der Restock-Zweig im Monitoring
        # ATOMAR mit dem Mengen-Push (sonst bliebe die eBay-Menge dauerhaft auf 0).
        # Preis-Pushes stoppen bis zum Review: die Varianten des neuen Anbieters
        # passen NICHT sicher auf die beim Publish vergebenen eBay-Varianten-SKUs.
        listing.auto_reprice = False
        db.commit()
        logger.info("source failover", extra={"listing_id": listing.id, "new_primary": pid})
        return True, scraped
    return False, None


async def refresh_secondary_sources(db: Session, *, max_listings: int | None = None) -> dict:
    """Bestands-Check der ZUSATZ-Slots (2+3) aller Listings – fuer den Monitoring-Job.

    Slot 0 wird bereits vom regulaeren Monitoring (sync_listing) geprueft; hier laufen
    nur die Ausweich-Anbieter, damit 'Anbieter B/C noch lieferbar?' aktuell bleibt.
    """
    listings = db.scalars(select(Listing).where(Listing.product_id.isnot(None),
                                                Listing.listing_status == "active")).all()
    ae = _real_ae()
    checked = failed = 0
    for l in listings if max_listings is None else listings[:max_listings]:
        product = db.get(Product, l.product_id)
        if product is None:
            continue
        alt = _alt_dict(product)
        sources = alt.get("sources") or []
        if len(sources) < 2:
            continue
        for i, slot in enumerate(sources[1:], start=1):
            try:
                scraped = await ae.scrape_product(slot["url"])
                sources[i] = _slot_from_scrape(scraped, slot["url"], slot.get("added") or "auto")
            except (OutOfStockError, ProductNotFoundError):
                sources[i] = {**slot, "in_stock": False, "checked_at": _now_iso()}
                failed += 1
            except Exception:  # noqa: BLE001 – API-/Netzfehler: Zustand nicht aendern
                pass
            checked += 1
            await asyncio.sleep(_RATE_S)
        alt["sources"] = sources
        _recalc_averages(alt)
        product.alternatives = alt
        flag_modified(product, "alternatives")
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    return {"checked": checked, "unavailable": failed}
