"""Ein Draft-Listing kontrolliert ECHT auf eBay veröffentlichen ("Live stellen").

Nutzt bewusst einen echten RealEbayClient (unabhängig von MOCK_EBAY), damit die App
im sicheren Mock bleibt, das Live-Stellen aber real gegen eBay geht.

Zwei Wege:
- OHNE Varianten -> Einzel-Listing: createInventoryItem -> createOffer -> publishOffer.
- MIT Varianten -> echtes Multivarianten-Listing: je Varianten-SKU ein Inventory-Item
  (mit Achsenwerten in product.aspects) + Offer, dann Inventory-Item-Group +
  publishOfferByInventoryItemGroup (auswählbare Farbe/Größe in EINEM Angebot).
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.config import get_settings
from app.integrations import aliexpress_api as _ae_api
from app.models import Listing, PriceHistory, Product, Sale
from app.retry import PersistentError, TransientError
from app.services import pricing
from app.services.common import task_log

logger = logging.getLogger("app.services.golive")


_EBAY_CLIENT = None


def _real_ebay():
    """GETEILTER Real-Client fuer alle Go-Live-Pfade (API, Queue-Worker, Retry-Job —
    alle auf dem Haupt-Event-Loop): Token-Cache + HTTP-Verbindungen werden
    wiederverwendet statt pro Publish neu aufgebaut (frueher: 1 OAuth-Roundtrip
    pro Operation, spuerbar langsamere Publishes)."""
    global _EBAY_CLIENT
    if _EBAY_CLIENT is None:
        from app.integrations.ebay import RealEbayClient
        _EBAY_CLIENT = RealEbayClient(get_settings())
    return _EBAY_CLIENT


def _absorb_item_twin(db: Session, listing: Listing, item_id: str) -> None:
    """Duplikat-Zeile mit derselben eBay-Item-Nr. in dieses Listing mergen.

    Entsteht, wenn ein Publish auf eBay durchging, der DB-Commit aber scheiterte
    und der eBay-Import das live gegangene Item als NEUE Zeile anlegte. Ohne Merge
    blockiert die UNIQUE-Constraint auf ebay_item_id jedes weitere Speichern.
    """
    twin = db.scalar(select(Listing).where(Listing.ebay_item_id == str(item_id),
                                           Listing.id != listing.id))
    if twin is None:
        return
    for s in db.scalars(select(Sale).where(Sale.listing_id == twin.id)).all():
        s.listing_id = listing.id
    for ph in db.scalars(select(PriceHistory).where(PriceHistory.listing_id == twin.id)).all():
        ph.listing_id = listing.id
    if twin.image_url and not listing.image_url:
        listing.image_url = twin.image_url
    logger.info("Duplikat-Listing %s (Item %s) in %s absorbiert", twin.id, item_id, listing.id)
    db.delete(twin)
    db.flush()


_BULLET_START = re.compile(r"^\s*[•\-\*–—▪✔✓]\s*")
# Überschriftszeile beginnt mit Emoji/Symbol (kein Buchstabe/Ziffer/Satzzeichen).
_HEADING_START = re.compile(r"^\s*[^\w\s.,;:!?(){}\[\]\"'/&%+-]")


def _is_heading(line: str) -> bool:
    return bool(_HEADING_START.match(line or ""))


def _html_description(text: str) -> str:
    """Klartext-Beschreibung in sauberes HTML für eBay: Emoji-Überschriften FETT, Absätze, Listen."""
    text = (text or "").strip()
    if not text:
        return ""
    low = text.lower()
    if "<p>" in low or "<ul>" in low or "<strong>" in low or "<br" in low:
        return text  # bereits HTML
    blocks = re.split(r"\n\s*\n", text)  # Absätze an Leerzeilen
    out: list[str] = []
    for block in blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        bullets = [l for l in lines if _BULLET_START.match(l)]
        if len(bullets) >= 2 and len(bullets) >= len(lines) - 1:
            # optionale Überschrift (erste Zeile ohne Bullet) fett voranstellen
            head = ""
            if not _BULLET_START.match(lines[0]):
                head = "<p><strong>" + _html.escape(lines[0]) + "</strong></p>"
                lines = lines[1:]
            items = "".join("<li>" + _html.escape(_BULLET_START.sub("", l)) + "</li>" for l in lines)
            out.append(head + "<ul>" + items + "</ul>")
        elif len(lines) == 1:
            l = lines[0]
            out.append("<p>" + (("<strong>" + _html.escape(l) + "</strong>") if _is_heading(l) else _html.escape(l)) + "</p>")
        else:
            # Mehrzeiliger Block: erste Zeile = Überschrift (fett), Rest normal
            head = "<strong>" + _html.escape(lines[0]) + "</strong>"
            rest = "<br>".join(_html.escape(l) for l in lines[1:])
            out.append("<p>" + head + "<br>" + rest + "</p>")
    return "".join(out) or ("<p>" + _html.escape(text) + "</p>")


def _axis_is_color(name: str) -> bool:
    n = (name or "").lower()
    return any(k in n for k in ("farbe", "color", "colour", "farbton", "shade"))


def _detect_image_axis(axis_names: list[str], variants: list[dict]) -> str | None:
    """DATENGETRIEBEN: nach WELCHER Achse variieren die Variantenbilder wirklich?

    Die Bild-Achse ist die, bei der Varianten mit demselben Wert DASSELBE Bild
    teilen und verschiedene Werte VERSCHIEDENE Bilder haben (>=2 distinct). Beispiel
    'Adler-Design': Style 1..24 -> je 1 eigenes Bild, egal ob 'Mit Kette'/'Nur
    Anhaenger'. 'Lieferumfang' waere KEINE Bild-Achse (beide Werte teilen alle Bilder).
    Braucht ein Bild an JEDER Variante. Liefert den Achsennamen oder None.
    """
    for a in axis_names:
        by_val: dict[str, str] = {}
        ok = True
        for v in variants:
            img = v.get("image")
            if not img:
                ok = False
                break
            val = str((v.get("options") or {}).get(a, ""))
            if val in by_val and by_val[val] != img:
                ok = False      # gleicher Wert, anderes Bild -> nicht diese Achse
                break
            by_val.setdefault(val, img)
        if ok and len(set(by_val.values())) >= 2:
            return a
    return None


def _image_axis(orig_axis_names: list[str], renamed_axis_names: list[str],
                axis_map: dict, variants: list[dict] | None = None) -> str | None:
    """Achse, nach der die Variantenbilder variieren – ROBUST gegen eBay-Umbenennung.

    Reihenfolge:
      1. Genau EINE Achse -> die Bilder variieren zwangslaeufig nach ihr.
      2. DATENGETRIEBEN aus den echten SKU-Bildern (funktioniert auch bei Achsen wie
         'Adler-Design'/'Stil', die NICHT 'Farbe' heissen – Vorfall 07/2026, wo alle
         50 Varianten dasselbe Bild bekamen, weil nur nach dem Namen 'Farbe' gesucht wurde).
      3. Fallback: Farb-Achse am ORIGINAL-Namen (auf den evtl. umbenannten Namen gemappt).
    """
    # GROESSE IST NIE EINE BILD-ACHSE (Nutzerbefund 03.09.2026).
    #
    # Ein T-Shirt in S und in 4XL zeigt dasselbe Motiv - die Groesse aendert am
    # Aussehen nichts. Trotzdem bekam jede der acht Groessen ein eigenes
    # Hauptbild, also achtmal dieselbe Datei im Angebot.
    #
    # Ausgeloest hat es Regel 1 unten ("nur eine Achse -> danach variieren die
    # Bilder"): Seit die einwertige Farbe korrekt herausfliegt, bleibt bei
    # Bekleidung NUR NOCH die Groesse uebrig - und wurde damit zwangslaeufig zur
    # Bild-Achse. Die Regel stimmt fuer Achsen wie "Stil 1..24"; fuer Groessen
    # ist sie sinnlos.
    #
    # Woertlich: "brauchen tshirts nicht alle einzeln ein bild je variante, weil
    # es ja nur groessen sind die unterschiedlich sind ... es ist nicht relevant
    # bei tshirts mit dem selben motiv wo sich nur groesse unterscheidet."
    from app.services import groessen as _groessen

    bildfaehig = [a for a in renamed_axis_names if not _groessen.ist_groessen_achse(a)]
    if not bildfaehig:
        return None                      # nur Groessen -> ein Bild fuer alle

    # Regel 1 zaehlt die URSPRUENGLICHEN Achsen, nicht die uebrig gebliebenen.
    # Sonst wuerde aus ["Groesse", "Material"] ploetzlich "Material" die
    # Bild-Achse, nur weil die Groesse herausfiel - geraten statt belegt. Bei
    # zwei Achsen ohne Farbe und ohne Bilddaten bleibt es bei "unbekannt".
    if len(renamed_axis_names) == 1:
        return bildfaehig[0]
    if variants:
        detected = _detect_image_axis(bildfaehig, variants)
        if detected:
            return detected
    orig = next((a for a in orig_axis_names if _axis_is_color(a)), None)
    return axis_map.get(orig, orig) if orig else None


def _truncate_aspects(aspects: dict, maxlen: int = 65) -> dict:
    """eBay begrenzt Merkmalswerte auf 65 Zeichen – zu lange Werte sauber kuerzen."""
    out = {}
    for name, vals in (aspects or {}).items():
        vlist = vals if isinstance(vals, list) else [vals]
        clean = []
        for v in vlist:
            sv = str(v)
            if len(sv) > maxlen:
                sv = sv[:maxlen].rstrip(" ,;.-")
            clean.append(sv)
        out[str(name)[:maxlen]] = clean
    return out


def _brand_from(aspects: dict) -> str:
    m = aspects.get("Marke") or aspects.get("Brand") or aspects.get("Markenname")
    if isinstance(m, list):
        m = m[0] if m else None
    return (m or "Markenlos").strip() or "Markenlos"


def _supplier_ship(listing) -> float | None:
    """Echte AliExpress-Versandkosten des Listings (€) fuer die Kalkulation, oder None
    (unbekannt -> Pauschal-Schaetzung)."""
    v = getattr(listing, "supplier_ship_eur", None)
    return float(v) if v is not None else None


def _variant_price(raw_price, fallback_eur, settings, min_profit_eur=None,
                   ship_override: float | None = None, category_name: str | None = None,
                   local: bool = False) -> float:
    """Verkaufspreis einer Variante aus deren Einkaufspreis (Pricing-Engine).

    OHNE Listing-Override gilt die UPLOAD-Regel wie fuer den Listing-Preis: der HOEHERE aus
    Zielmarge (``upload_margin_pct``, 20 %) und Mindestgewinn (``upload_min_profit_eur``, 4 €).
    Frueher lief hier der Config-Boden ``min_profit_eur`` (8 €) mit – dadurch waren die
    VARIANTEN-Preise neuer Produkte deutlich teurer als der kalkulierte Listing-Preis
    (Nutzerfund 02.08.: "Gewinnmarge zu hoch, oftmals bei 8 Euro").

    ``min_profit_eur`` (per Listing gesetzt) ueberschreibt weiterhin alles.
    ``ship_override`` = echte AliExpress-Versandkosten (statt Pauschale).
    ``local`` = Ware liegt in einem EU-Lager -> kein China-Versand, kein Zoll. Ohne
    dieses Kennzeichen bekam JEDE Variante Versand und Zollpauschale (3,57 EUR)
    aufgeschlagen, auch bei Ware aus Deutschland - und das sind die Preise, die der
    Kaeufer je Groesse tatsaechlich zahlt (Fund 30.08.2026).
    """
    if raw_price is not None:
        try:
            if min_profit_eur is None:
                return float(pricing.upload_breakdown_from_cny(
                    Decimal(str(raw_price)), settings=settings, local=local,
                    category_name=category_name, ship_override=ship_override).rounded_price_eur)
            return float(pricing.price_from_cny(
                Decimal(str(raw_price)), settings=settings, local=local,
                min_profit_eur=min_profit_eur, ship_override=ship_override).rounded_price_eur)
        except (InvalidOperation, ValueError, TypeError):
            pass
    return float(fallback_eur or 0)


_PREMIUM_SUFFIX = re.compile(r"[\s\-–_]*[AK]I$", re.IGNORECASE)


def _apply_premium_rule(prices: list[dict], axis_names: list[str]) -> None:
    """KI-/Premium-Varianten duerfen nie billiger sein als ihre Basisvariante.

    Erkennung: Optionswert mit AI/KI-Suffix, dessen Basiswert (ohne Suffix) als
    eigene Variante existiert (z.B. 'Orange-AI' vs. 'Orange'). Dann wird der
    Premium-Preis mindestens auf den Basispreis angehoben.
    """
    if len(axis_names) != 1:
        return
    a = axis_names[0]
    by_val = {str(p["options"].get(a, "")): p for p in prices}
    for val, p in by_val.items():
        if p.get("overridden"):
            continue   # explizit vom Nutzer gesetzter Preis bleibt unangetastet
        stripped = _PREMIUM_SUFFIX.sub("", val).strip(" -–_")
        if stripped and stripped != val and stripped in by_val:
            base = by_val[stripped]
            if p["price_eur"] < base["price_eur"]:
                p["price_eur"] = base["price_eur"]


def _variant_image_urls(product: Product | None) -> list[str]:
    """Alle eindeutigen Variantenbilder eines Produkts (Reihenfolge = Variantenreihenfolge)."""
    out: list[str] = []
    for v in ((product.variants or {}).get("skus") or []) if product else []:
        img = v.get("image")
        if img and img not in out:
            out.append(img)
    return out


def gallery_images(product: Product | None) -> list[str]:
    """Galerie-Bilder fuers Listing: Standardbilder ZUERST, dann alle noch nicht
    enthaltenen Variantenbilder – so sieht der Kaeufer beim Durchblaettern auch
    jede Variante. eBay erlaubt bis zu 24 Bilder je Listing."""
    base = [u for u in ((product.images if product else None) or []) if u]
    for vimg in _variant_image_urls(product):
        if vimg not in base:
            base.append(vimg)
    return base[:24]


def image_candidates(product: Product | None) -> list[dict]:
    """Auswahl fuers Hauptbild: Standard- + Variantenbilder mit Herkunft/Label."""
    base = [u for u in ((product.images if product else None) or []) if u]
    out = [{"url": u, "kind": "standard", "is_main": i == 0}
           for i, u in enumerate(base)]
    seen = set(base)
    var_by_img = {}
    for v in ((product.variants or {}).get("skus") or []) if product else []:
        img = v.get("image")
        if img:
            var_by_img.setdefault(img, " · ".join(
                str(x) for x in (v.get("options") or {}).values()))
    for img, label in var_by_img.items():
        if img not in seen:
            out.append({"url": img, "kind": "variant", "label": label, "is_main": False})
            seen.add(img)
    return out


async def ebay_listing_images(ebay, listing) -> list[dict]:
    """Die ECHTEN Bilder des LIVE eBay-Listings (Inventory API), Titelbild zuerst.

    Wichtig: NICHT die (evtl. falschen) AliExpress-Quell-Bilder – nur was auf eBay steht.
    Union ueber alle Varianten-SKUs (jedes Item traegt die geteilte Galerie); erstes Bild
    des ersten SKU = aktuelles Titelbild. Leere Liste bei Fehler (Aufrufer faellt zurueck).
    """
    out: list[dict] = []
    seen: set = set()
    # 1) Trading-GetItem ZUERST: liefert die ECHTE Live-Galerie (i.ebayimg.com, Titelbild zuerst)
    #    fuer Inventory- UND Klassik-Listings. Der Inventory-Pfad gaebe nur die beim Publish
    #    EINGEREICHTEN AliExpress-URLs zurueck — genau die falschen Bilder im Detail-Modal.
    if getattr(listing, "ebay_item_id", None):
        try:
            for u in await ebay.get_item_pictures(listing.ebay_item_id):
                if u and u not in seen:
                    seen.add(u)
                    out.append({"url": u, "kind": "ebay", "is_main": not out})
        except Exception:  # noqa: BLE001
            pass
    # 2) Fallback Inventory-API (z.B. GetItem-Stoerung/Entwurf): product.imageUrls je SKU.
    if not out:
        try:
            skus = await _listing_variant_skus(ebay, listing)
        except Exception:  # noqa: BLE001
            skus = [listing.ebay_sku] if listing.ebay_sku else []
        for sku in skus:
            try:
                item = await ebay.get_inventory_item(sku)
            except Exception:  # noqa: BLE001 – ein SKU-Fehler stoppt die Sammlung nicht
                continue
            for u in ((item or {}).get("product") or {}).get("imageUrls") or []:
                if u and u not in seen:
                    seen.add(u)
                    out.append({"url": u, "kind": "ebay", "is_main": not out})
    return out


async def get_ebay_gallery(db: Session, *, listing_id: int, max_age_hours: int = 24) -> dict:
    """Echte Bilder des LIVE-eBay-Listings fuers Detail-Modal (24h-Cache am Listing).

    Netz-Call passiert VOR dem kurzen Commit (Regel 12). Bei API-Stoerung wird der
    (ggf. alte) Cache geliefert statt eines Fehlers — das Modal hat den Quell-Fallback.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    from sqlalchemy.orm.attributes import flag_modified as _fm

    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    if not listing.ebay_item_id:
        return {"images": [], "live": False}
    cache = listing.ebay_gallery or {}
    # Vergiftete Alt-Caches (Inventory-Pfad lieferte AliExpress-URLs) sofort verwerfen:
    # eine echte eBay-Galerie besteht aus EPS-URLs (i.ebayimg.com).
    cached_urls = [u for u in (cache.get("urls") or []) if u]
    looks_live = any("ebayimg" in str(u) for u in cached_urls)
    try:
        fresh = bool(cached_urls) and looks_live and cache.get("fetched_at") and (
            _dt.now(_tz.utc) - _dt.fromisoformat(cache["fetched_at"])) < _td(hours=max_age_hours)
    except (ValueError, TypeError):
        fresh = False
    if fresh:
        return {"images": list(cache["urls"]), "live": True, "cached": True}
    try:
        ebay = _real_ebay()
        urls = [c.get("url") for c in await ebay_listing_images(ebay, listing) if c.get("url")]
    except Exception as exc:  # noqa: BLE001 – Anzeige-Feature: nie den Modal-Aufruf sprengen
        logger.warning("ebay gallery fetch failed", extra={"listing_id": listing_id,
                                                           "error": str(exc)[:120]})
        return {"images": list(cache.get("urls") or []), "live": True,
                "cached": bool(cache.get("urls")), "stale": True}
    if not urls and cached_urls:
        # Leere Antwort (eBay-Stoerung faengt ebay_listing_images intern ab) darf einen
        # guten Cache nicht ueberschreiben — alten Stand zeigen, naechster Aufruf probiert neu.
        return {"images": cached_urls, "live": True, "cached": True, "stale": True}
    listing.ebay_gallery = {"urls": urls, "fetched_at": _dt.now(_tz.utc).isoformat()}
    _fm(listing, "ebay_gallery")
    db.commit()
    return {"images": urls, "live": True, "cached": False}


async def set_main_image(db: Session, *, listing_id: int, image_url: str) -> dict:
    """Gewaehltes Bild zum HAUPTBILD (Titelbild) machen.

    LIVE-Listing: die ECHTE eBay-Galerie umsortieren (gewaehltes Bild zuerst) und pushen –
    NICHT die evtl. falschen Quell-Bilder. Nur wenn das Bild dort nicht vorkommt (oder als
    Entwurf), wird auf product.images zurueckgefallen (wirkt dann beim Veroeffentlichen).
    """
    listing = db.get(Listing, listing_id)
    product = db.get(Product, listing.product_id) if listing and listing.product_id else None

    # 1) LIVE-Pfad: die tatsaechliche eBay-Galerie umsortieren (gewaehltes Bild = Titelbild).
    #    Inventory-Listing -> update_inventory_item_fields je SKU; Klassik-Listing -> Trading
    #    ReviseFixedPriceItem (PictureDetails). Ist das Bild NICHT in der eBay-Galerie, faellt
    #    es auf den Quell-Pfad zurueck (z.B. Bild-Auswahl im Produktdetail).
    if listing is not None and listing.ebay_item_id and get_settings().monitor_push_real:
        try:
            ebay = _real_ebay()
            gallery = [c["url"] for c in await ebay_listing_images(ebay, listing)]
            if image_url in gallery:
                reordered = [image_url] + [u for u in gallery if u != image_url]
                pushed = inv_ok = False
                for sku in await _listing_variant_skus(ebay, listing):
                    if await ebay.get_inventory_item(sku):     # Inventory-Item vorhanden
                        inv_ok = True
                        try:
                            await ebay.update_inventory_item_fields(sku, image_urls=reordered)
                            pushed = True
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("main-image inv push (%s) failed: %s", sku, str(exc)[:120])
                if not inv_ok:                                 # Klassik (Trading-API)
                    try:
                        await ebay.revise_item_pictures(listing.ebay_item_id, reordered)
                        pushed = True
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("main-image trading push failed: %s", str(exc)[:150])
                listing.image_url = image_url
                if pushed:
                    # 24h-Galerie-Cache invalidieren, sonst zeigt das Modal die alte Reihenfolge
                    listing.ebay_gallery = None
                    flag_modified(listing, "ebay_gallery")
                db.commit()
                return {"listing_id": listing_id, "main_image": image_url,
                        "pushed_to_ebay": pushed, "source": "ebay"}
        except Exception as exc:  # noqa: BLE001 – auf den Quell-Pfad zurueckfallen
            logger.warning("ebay main-image path failed: %s", str(exc)[:150])

    # 2) Fallback / Entwurf: product.images umsortieren (wirkt beim naechsten Publish).
    if product is None:
        raise PersistentError("Kein Produkt zu diesem Listing")
    imgs = [u for u in (product.images or []) if u]
    # Variantenbild? -> in die Galerie aufnehmen, falls noch nicht vorhanden.
    if image_url not in imgs and image_url in _variant_image_urls(product):
        imgs.append(image_url)
    if image_url not in imgs:
        raise PersistentError("Bild gehoert nicht zu diesem Produkt")
    reordered = [image_url] + [u for u in imgs if u != image_url]
    product.images = reordered
    flag_modified(product, "images")
    if listing.image_url:
        listing.image_url = image_url   # Thumbnail im Dashboard nachziehen
    pushed = False
    if listing.ebay_item_id and get_settings().monitor_push_real:
        try:
            ebay = _real_ebay()
            for sku in await _listing_variant_skus(ebay, listing):
                try:
                    await ebay.update_inventory_item_fields(sku, image_urls=reordered)
                    pushed = True    # ehrlich: nur wenn mindestens EIN Push ankam
                except Exception as exc:  # noqa: BLE001
                    logger.warning("main-image push (%s) failed: %s", sku, str(exc)[:120])
        except Exception as exc:  # noqa: BLE001 – DB-Reihenfolge bleibt trotzdem gesetzt
            logger.warning("main-image push failed: %s", str(exc)[:150])
    if pushed:
        listing.ebay_gallery = None
        flag_modified(listing, "ebay_gallery")
    db.commit()
    return {"listing_id": listing_id, "main_image": image_url,
            "images": reordered, "pushed_to_ebay": pushed}


def delete_variant(db: Session, *, listing_id: int, attr: str) -> dict:
    """EINE Variante aus dem Produkt entfernen (Nutzer-Aktion, nie automatisch).

    Entfernt die SKU aus product.variants.skus und bereinigt die Achsenwerte.
    Bei Live-Listings ist danach ein erneutes Veroeffentlichen noetig, damit die
    Variante auch auf eBay verschwindet – der Hinweis wird zurueckgegeben (es wird
    hier NICHTS automatisch bei eBay beendet, Nutzerregel [[keine-auto-loeschungen]]).
    """
    listing = db.get(Listing, listing_id)
    product = db.get(Product, listing.product_id) if listing and listing.product_id else None
    if product is None:
        raise PersistentError("Kein Produkt zu diesem Listing")
    variants = product.variants if isinstance(product.variants, dict) else {}
    skus = list(variants.get("skus") or [])
    remaining = [s for s in skus if s.get("attr") != attr]
    if len(remaining) == len(skus):
        raise PersistentError("Variante (attr) nicht gefunden")
    if len(remaining) < 1:
        raise PersistentError("Die letzte Variante kann nicht geloescht werden "
                              "(dann besser den ganzen Artikel beenden).")
    # Achsen neu aus den verbleibenden SKUs ableiten (Werte, die es nicht mehr gibt, raus).
    axes = {}
    for s in remaining:
        for a, val in (s.get("options") or {}).items():
            axes.setdefault(a, [])
            if val not in axes[a]:
                axes[a].append(val)
    new_variants = dict(variants)
    new_variants["skus"] = remaining
    new_variants["axes"] = axes
    product.variants = new_variants
    flag_modified(product, "variants")
    db.commit()
    return {"listing_id": listing_id, "removed_attr": attr,
            "remaining": len(remaining),
            "needs_republish": bool(listing.ebay_item_id),
            "note": ("Variante entfernt. Sie ist bei eBay noch KAUFBAR, bis der Abgleich "
                     "„Varianten auf eBay uebertragen“ gelaufen ist."
                     if listing.ebay_item_id else "Variante entfernt.")}


async def _resync_oos(db: Session, listing_id: int) -> dict | None:
    """Ausverkauft-Abgleich (Menge 0 / Menge zurueck) NACH dem SKU-Binding — best effort.

    Sicher, weil bind_live_variant_skus vorher die echten Live-SKUs gepinnt und committet
    hat; der Resync arbeitet damit gegen korrekte Schluessel (der 6h-Monitor allein hat
    dieses Binding nicht). Fehler brechen den Varianten-Abgleich nie."""
    try:
        from app.services.monitoring_service import resync_listing_variant_stock
        return await resync_listing_variant_stock(db, listing_id=listing_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("OOS-Abgleich nach Varianten-Sync fehlgeschlagen: %s", str(exc)[:150])
        return None


async def sync_variants_live(db: Session, *, listing_id: int) -> dict:
    """Lokal geloeschte Varianten auf eBay unverkaeuflich machen UND den Ausverkauft-
    Bestand je Variante abgleichen (Nutzer-Aktion, nie automatisch).

    Ablauf – in dieser Reihenfolge, weil jeder Schritt den naechsten absichert:

    1. Echte Live-SKUs von eBay holen und am Produkt festschreiben
       (``bind_live_variant_skus``). Ist die Zuordnung nicht eindeutig, bricht der ganze
       Abgleich ab und es wird NICHTS angefasst.
    2. Verwaiste SKUs (auf eBay, lokal geloescht) auf **Menge 0** setzen. Das ist der
       verlaessliche Teil: die Variante ist sofort nicht mehr kaufbar. eBay steuert die
       Sichtbarkeit einer Variation ueber die Offer-``availableQuantity``.
    3. ERST DANN versuchen, sie ganz aus der Gruppe zu nehmen (createOrReplace + republish).
       Verkaufte Variationen laesst eBay nicht mehr entfernen – schlaegt das fehl, bleibt es
       bei Menge 0 und der Grund wird zurueckgemeldet (kein Abbruch, kein Rollback).

    Beendet oder loescht NICHTS am Listing (Regel 7); der Artikel bleibt mit denselben
    Item-IDs live.
    """
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    gk = listing.ebay_draft_id or ""
    if not (listing.ebay_item_id and listing.listing_status == "active" and gk.endswith("-GRP")):
        raise PersistentError(
            "Das geht nur bei einem aktiven Multivarianten-Artikel, der ueber dieses Tool "
            "veroeffentlicht wurde.")
    product = db.get(Product, listing.product_id) if listing.product_id else None
    if product is None:
        raise PersistentError("Kein Produkt zu diesem Listing")

    ebay = _real_ebay()
    with task_log(db, task_type="sync_variants", reference_id=listing_id) as tl:
        # 1) Zuordnung herstellen (wirft bei Mehrdeutigkeit – dann ist nichts passiert).
        binding = await bind_live_variant_skus(ebay, db, listing, product)
        # SOFORT committen: die Zuordnung ist fuer sich genommen richtig und darf nicht als
        # offene Schreib-Transaktion ueber die folgenden eBay-Calls haengen (Regel 12).
        db.commit()
        orphans = binding["orphans"]
        if not orphans:
            # Keine geloeschten Varianten — aber AUSVERKAUFTE trotzdem abgleichen (Menge 0
            # bzw. Menge zurueck). Genau dieser Schritt fehlte hier frueher: der Button
            # meldete "stimmen ueberein", ohne die Mengen anzufassen.
            oos = await _resync_oos(db, listing_id)
            tl.result_data = {"orphans": 0, "bound": binding["persisted"],
                              "oos_sync": oos}
            changed = bool(oos and oos.get("changed"))
            if oos is None:
                msg = ("Varianten stimmen ueberein; Mengen-Abgleich fehlgeschlagen — "
                       "der naechste Ueberwachungslauf versucht es erneut.")
            elif oos.get("changed") and not oos.get("pushed", True):
                msg = ("Varianten stimmen ueberein; Mengen-Push zu eBay fehlgeschlagen — "
                       "der naechste Ueberwachungslauf versucht es erneut.")
            elif changed:
                msg = (f"Varianten stimmen ueberein; {oos['changed']} Mengen-Aenderung(en) "
                       "auf eBay uebertragen (ausverkauft → 0 / wieder lieferbar → Standard).")
            else:
                msg = "Alle Varianten und Mengen stimmen bereits mit eBay ueberein."
            return {"listing_id": listing_id, "changed": changed, "removed": [],
                    "quantity_zeroed": [], "removed_from_group": False,
                    "oos_sync": oos, "message": msg}

        # 2) Verwaiste Varianten unverkaeuflich machen (Menge 0). Bewusst VOR dem
        #    Gruppen-Umbau: schlaegt Schritt 3 fehl, ist die Variante trotzdem schon weg
        #    vom Markt – die Reihenfolge umgedreht bliebe sie im Fehlerfall kaufbar.
        updates = []
        for sku in orphans:
            offer = await ebay._first_offer_for_sku(sku)
            if offer and offer.get("offerId"):
                updates.append({"sku": sku, "offer_id": offer["offerId"], "quantity": 0})
        if updates:
            await ebay.bulk_update_price(updates)
        zeroed = [u["sku"] for u in updates]
        no_offer = [s for s in orphans if s not in set(zeroed)]

        # 3) Ganz aus der Gruppe nehmen. _push_group_update baut sie aus den VERBLIEBENEN
        #    lokalen Varianten – mit deren festgeschriebenen SKUs (Schritt 1), also ohne
        #    Umnummerierung. Danach republish, damit eBay das Angebot neu rendert.
        removed_from_group = False
        group_error = None
        try:
            if not await _push_group_update(ebay, listing, db):
                raise PersistentError("Gruppe konnte nicht neu geschrieben werden")
            await ebay.publish_offer_by_inventory_item_group(gk)
            removed_from_group = True
        except Exception as exc:  # noqa: BLE001 – Menge 0 steht bereits, das genuegt
            group_error = str(exc)[:200]
            logger.warning("Varianten-Abgleich: Gruppe nicht aktualisierbar",
                           extra={"listing_id": listing_id, "error": group_error})

        # 4) Bestands-Spiegel zuruecksetzen: die alten Schluessel waren positionsbasiert und
        #    zeigen nach dem Loeschen auf fremde Varianten. Leeren heisst, der naechste
        #    Monitor-Lauf rechnet alle Mengen frisch gegen die jetzt korrekten SKUs.
        listing.variant_stock = None
        flag_modified(listing, "variant_stock")
        db.commit()
        # 5) Ausverkauft-Abgleich sofort nachziehen: rechnet alle Mengen frisch gegen die
        #    korrekten (gerade gebundenen) SKUs, statt auf den 6h-Monitor zu warten.
        oos = await _resync_oos(db, listing_id)
        tl.result_data = {"orphans": len(orphans), "zeroed": len(zeroed),
                          "removed_from_group": removed_from_group,
                          "group_error": group_error, "oos_sync": oos}

    if removed_from_group:
        msg = f"{len(orphans)} Variante(n) von eBay entfernt."
    elif zeroed:
        msg = (f"{len(zeroed)} Variante(n) auf Menge 0 gesetzt – sie sind nicht mehr kaufbar. "
               "Ganz entfernen laesst eBay sie nicht mehr (das geht nur, solange eine Variante "
               "noch nie verkauft wurde).")
    else:
        msg = ("Zu den geloeschten Varianten wurde bei eBay kein Angebot gefunden – bitte im "
               "Seller Hub pruefen.")
    return {"listing_id": listing_id, "changed": True, "removed": orphans,
            "quantity_zeroed": zeroed, "no_offer": no_offer,
            "removed_from_group": removed_from_group, "group_error": group_error,
            "oos_sync": oos, "message": msg}


def _price_for_mode(ek_eur: float, *, mode: str, value: float, settings,
                    category_name: str | None = None) -> float:
    """Verkaufspreis einer Variante aus ihrem EK nach dem gewaehlten Modus,
    gerundet auf den NAECHSTEN x,95 (Nutzerregel).

    * mode 'margin': value = Ziel-Bruttomarge in Prozent (z.B. 25 -> 0.25):
        preis = (EK + fix) / (1 - fee_pct - marge).
    * mode 'profit': value = absoluter Ziel-Gewinn in EUR je Variante:
        preis = (EK + fix + gewinn) / (1 - fee_pct).

    ``fee_pct`` ist kategorie-genau (Provision + Anzeigenrate), sofern ``category_name``
    bekannt ist; sonst der pauschale ebay_fee_pct.
    """
    # effective_fee_pct ist auch ohne Kategorie korrekt (Default-Provision + Anzeigenrate,
    # inkl. 19% MwSt). NIE auf rohes ebay_fee_pct zurueckfallen (das liesse Anzeigenrate
    # UND MwSt weg -> Preis zu billig). Fixbetrag ebenfalls MwSt-inkl. via Helfer.
    fee = pricing.effective_fee_pct(category_name, settings=settings)
    fix = pricing.ebay_fixed_fee(settings)
    ek = max(0.0, float(ek_eur or 0))
    if mode == "margin":
        m = float(value) / 100.0 if value and value > 1 else float(value or 0)
        denom = max(0.01, 1.0 - fee - m)          # Marge zu hoch -> Deckel bei 0.01
        raw = (ek + fix) / denom
    else:  # profit (EUR)
        raw = (ek + fix + float(value or 0)) / max(0.01, 1.0 - fee)
    cents = settings.price_cents or 0.95
    # AUFrunden auf x,95 (Nutzerregel): so faellt die Zielmarge nie durch Abrunden weg.
    return pricing.round_up_to_cents(raw, cents)


def _persist_variant_price_overrides(db: Session, listing, targets: dict[str, float]) -> None:
    """Manuelle Variantenpreise als Override je AE-sku_attr speichern (ueberlebt Re-Publish).

    ``targets`` ist {sku (AE-{id}-Vn): preis}. Die Vn-Position wird ueber
    _usable_variants auf den stabilen sku_attr aufgeloest.
    """
    product = db.get(Product, listing.product_id) if listing.product_id else None
    _, uvars = _usable_variants(product)
    base_sku = listing.ebay_sku or f"AE-{listing.id}"
    sku_to_attr = {f"{base_sku}-V{i}"[:50]: uvars[i - 1].get("attr")
                   for i in range(1, len(uvars) + 1)}
    vp = dict(listing.variant_prices or {})
    changed = False
    for sku, price in targets.items():
        attr = sku_to_attr.get(sku)
        if attr:
            vp[attr] = round(float(price), 2)
            changed = True
    if changed:
        listing.variant_prices = vp
        flag_modified(listing, "variant_prices")


def variant_price_rows(listing, product, settings) -> list[dict]:
    """Je Variante: Name, AE-Preis, EK, aktuell kalkulierter VK (fuer den Preis-Editor).

    Einzel-Listing (keine echten Varianten) -> eine Pseudo-Zeile mit der Basis-SKU.
    """
    # EINHEITLICH: der „aktuelle VK" im Preis-Editor ist der ECHTE eBay-Preis (nicht der interne
    # Kalkulationspreis) – so sieht der Nutzer beim Öffnen sofort, welche Variante real Verlust macht.
    # Vor dem ersten Abgleich Fallback auf den internen Preis. (Lokaler Import: sonst Zirkelbezug.)
    from app.services.listing_match_service import effective_ebay_price
    axis_names, variants = _usable_variants(product)
    base_sku = listing.ebay_sku or f"AE-{listing.id}"
    sov = _supplier_ship(listing)   # echte AliExpress-Versandkosten (statt Pauschale)
    if not axis_names:
        cost = float(listing.cost_eur) if listing.cost_eur is not None else None
        _live = effective_ebay_price(listing, base_sku)
        _internal = float(listing.price_eur) if listing.price_eur is not None else None
        return [{
            "sku": base_sku, "name": "(ohne Varianten)",
            "ae_price_eur": float(product.price_cny) if product and product.price_cny else None,
            "ek_eur": cost,
            "current_price_eur": _live if _live is not None else _internal,
            "price_is_live": _live is not None,
            "stock": None,
        }]
    calc = {v["sku"]: v for v in compute_variant_prices(listing, product, settings)}
    rows = []
    for i, v in enumerate(variants, 1):
        sku = f"{base_sku}-V{i}"[:50]
        ae = None
        try:
            ae = float(v["price"]) if v.get("price") is not None else None
        except (TypeError, ValueError):
            ae = None
        # Signatur NUR aus den publizierten Achsen (axis_options), nicht dem rohen options-Dict.
        _live = effective_ebay_price(listing, sku, (calc.get(sku) or {}).get("axis_options"))
        _internal = (calc.get(sku) or {}).get("price_eur")
        rows.append({
            "sku": sku,
            "name": " / ".join(str(v.get("options", {}).get(a, "")) for a in axis_names),
            "ae_price_eur": ae,
            "ek_eur": round(pricing.effective_cost(
                ae, settings=settings, ship_override=sov,
                local=_ae_api.has_eu_warehouse([v.get("ship_from")])), 2) if ae else None,
            "current_price_eur": _live if _live is not None else _internal,
            "price_is_live": _live is not None,
            "stock": v.get("stock"),
        })
    return rows


def _validate_reprice_target(mode, value) -> float:
    """EINE Wahrheit fuer Ziel-Marge/-Gewinn (Preis-Editor + Bulk-Marge). Verhindert, dass
    ein negativer/absurder Wert stumm auf den 0,95-€-Boden kollabiert und live gepusht wird.

    Gibt den geprueften float-Wert zurueck; wirft PersistentError bei Unfug.
    """
    if mode not in ("margin", "profit"):
        raise PersistentError("mode muss 'margin' oder 'profit' sein")
    if value is None:
        raise PersistentError("value fehlt")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise PersistentError("value muss eine Zahl sein")
    if v != v or v in (float("inf"), float("-inf")):   # NaN/Inf
        raise PersistentError("value ist keine gueltige Zahl")
    if v < 0:
        raise PersistentError("Wert darf nicht negativ sein (Marge/Gewinn ≥ 0)")
    if mode == "margin" and v >= 95:
        raise PersistentError("Ziel-Marge muss unter 95 % liegen")
    return v


async def preview_variant_prices(db: Session, *, listing_id: int, mode: str | None = None,
                                 value: float | None = None, ebay_driven: bool = True) -> dict:
    """Preis-Editor: aktuelle Varianten + (optional) Vorschau der neuen Preise nach
    Modus 'margin' (%)/'profit' (€). Rechnet NUR – aendert NICHTS (propose-only).

    GRUNDSANIERUNG (20.07.): fuer LIVE-Multivarianten-Listings werden die Zeilen DIREKT aus den
    ECHTEN eBay-Variationen gebaut (``_ebay_variation_rows`` via GetItem) – jede Zeile traegt die
    ECHTE eBay-SKU/Specifics + den ECHTEN eBay-Preis, sodass der spaetere Preis-Write die Variation
    ohne Heuristik-Matching adressiert. Faellt GetItem aus (oder Einzel-/Entwurfs-Listing), wird auf
    das interne Modell (``variant_price_rows``) zurueckgefallen. ``ebay_driven=False`` erzwingt das
    interne Modell (Bulk-Marge: dort ist das interne Modell gewollt, kein GetItem je Listing)."""
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    # Ziel-Wert pruefen, sobald ein Modus gesetzt ist (deckt Einzel-Listing UND Bulk ab).
    if mode in ("margin", "profit") and value is not None:
        value = _validate_reprice_target(mode, value)
    product = db.get(Product, listing.product_id) if listing.product_id else None
    settings = get_settings()
    fee_pct = pricing.effective_fee_pct_for_listing(listing, settings=settings)
    fix = pricing.ebay_fixed_fee(settings)
    rows = None
    ebay_source = False
    if ebay_driven and listing.ebay_item_id:
        try:
            rows = await _ebay_variation_rows(_real_ebay(), listing, product, settings)
        except Exception:  # noqa: BLE001 – GetItem-Ausfall → interner Fallback, Dialog oeffnet trotzdem
            rows = None
        ebay_source = rows is not None
    if rows is None:
        rows = variant_price_rows(listing, product, settings)
    for r in rows:
        # AKTUELLE Marge/Gewinn je Variante immer mitliefern – damit der Dialog beim Öffnen
        # zeigt, welche Varianten teuer/dünn sind (gezieltes Anheben der teureren).
        cp, ek = r.get("current_price_eur"), r.get("ek_eur")
        if cp is not None and ek is not None and cp:
            r["current_profit_eur"] = round(cp - cp * fee_pct - fix - ek, 2)
            r["current_margin"] = round(r["current_profit_eur"] / cp, 4)
        else:
            r["current_profit_eur"] = r["current_margin"] = None
        if mode in ("margin", "profit") and value is not None and ek:
            r["new_price_eur"] = _price_for_mode(ek, mode=mode, value=value,
                                                 settings=settings,
                                                 category_name=listing.category_name)
            p = r["new_price_eur"]
            r["new_profit_eur"] = round(p - p * fee_pct - fix - ek, 2)
            r["new_margin"] = round(r["new_profit_eur"] / p, 4) if p else None
    # variant_count: bei eBay-getriebenen Zeilen ist jede Zeile eine echte Variation; sonst die
    # bisherige Heuristik (Zeilen != Basis-SKU des Einzel-Listings).
    variant_count = (len(rows) if ebay_source
                     else len([r for r in rows if r["sku"] != listing.ebay_sku])) or 0
    return {"listing_id": listing_id, "is_live": bool(listing.ebay_item_id),
            "variant_count": variant_count, "ebay_source": ebay_source,
            "rows": rows, "mode": mode, "value": value}


def _int_or_none(x):
    """x -> int oder None (robust; '' / 'N/A' / None -> None)."""
    if x is None:
        return None
    try:
        return int(str(x).strip())
    except (TypeError, ValueError):
        return None


def _norm_val(x) -> str:
    """Einzelwert normalisieren wie _value_sig: klein, ohne Leerzeichen (für Exakt-Vergleiche)."""
    return re.sub(r"\s+", "", str(x or "").strip().lower())


def _norm_loose(x) -> str:
    """Kleinschreibung + Rand trimmen, INNERE Trenner (Leerzeichen/Bindestrich/Klammer) BLEIBEN –
    nötig für die Wortgrenzen-Erkennung beim Präfix-Match."""
    return str(x or "").strip().lower()


def _match_prefix(o: str, e: str) -> bool:
    """Unser Wert ``o`` passt zu eBay-Wert ``e``, wenn e == o ODER e mit o + WORTGRENZE beginnt
    (das nächste Zeichen ist KEIN Buchstabe/Ziffer). So trifft 'guinea'->'guinea-bissau' und
    '60cm'->'60cm oder 23,6 Zoll', aber NICHT 'niger'->'nigeria' (Grenze mitten im Wort → anderes
    Land). Verhindert Fehl-Preise durch zufällige Präfixe unterschiedlicher Werte."""
    if not o or not e:
        return False
    if e == o:
        return True
    return e.startswith(o) and len(e) > len(o) and not e[len(o)].isalnum()


def _prefix_of_distinct(our_vals: list[str], ebay_vals: list[str]) -> bool:
    """True, wenn JEDER unserer Werte ein WORTGRENZEN-Präfix eines – jeweils eigenen – eBay-Werts
    ist (bijektiv). Deckt VERBOSE eBay-Werte ab (unser 'guinea'/'60cm' ist Anfang von 'guinea-…'
    bzw. '60cm oder …'), vermeidet aber Fehltreffer wie 'niger' in 'nigeria', '60cm' in '160cm' oder
    'guinea' in 'äquatorialguinea'. Leere Eingabe/Wert -> False (fail-closed)."""
    if not our_vals:
        return False
    pool = list(ebay_vals)
    for o in our_vals:
        hit = next((k for k, e in enumerate(pool) if _match_prefix(o, e)), None)
        if hit is None:
            return False
        pool.pop(hit)
    return True


def _bijective_prefix_assign(our_by_sku: dict, ebay_variations: list) -> dict:
    """GLOBALE bijektive Präfix-Zuordnung unserer Varianten -> eBay-Variations-Index. Löst die
    Mehrdeutigkeit, die eine Einzel-Zuordnung nicht kann: eine EINDEUTIGE Variante („Guinea-Bissau"
    passt nur zu „guinea-bissau") wird zugeordnet und belegt ihre eBay-Variation; dadurch wird die
    zuvor mehrdeutige („Guinea" passte zu „guinea" UND „guinea-bissau") auf die verbleibende gezwungen.

    GELD-SICHER: es werden AUSSCHLIESSLICH ZWANGS-Zuordnungen gesetzt (eine Variante mit – nach Abzug
    schon belegter – GENAU EINEM Kandidaten). Nie geraten, keine Position. Bleibt etwas mehrdeutig,
    wird es NICHT zugeordnet -> Aufrufer fail-closed. Rückgabe: {unser_sku: ev_index}.
    """
    ev_vals = [[_norm_loose(val) for _n, val in (v.get("specifics") or [])] for v in ebay_variations]
    cand: dict[str, set] = {}
    for sku, opts in (our_by_sku or {}).items():
        ovals = [_norm_loose(x) for x in (opts or {}).values() if _norm_loose(x)]
        if ovals:
            cand[sku] = {j for j, evs in enumerate(ev_vals) if _prefix_of_distinct(ovals, evs)}
    # DEFENSE (Review 20.07.): der Präfix-Zuordnung NUR trauen, wenn eBay GENAUSO VIELE Variationen
    # hat wie wir Varianten (mit Werten). Weichen die Anzahlen ab, fehlt/weicht mindestens eine
    # Variation ab -> eine unserer Varianten könnte auf eine FREMDE eBay-Variation gezwungen werden
    # (z.B. „Niger" -> „Nigeria"). Dann lieber GAR NICHT zuordnen (fail-closed).
    if len(cand) != len(ebay_variations):
        return {}
    assigned: dict[str, int] = {}
    used: set[int] = set()
    changed = True
    while changed:
        changed = False
        # Alle „naked singles" dieser Runde sammeln (Variante mit genau EINEM freien Kandidaten).
        singles: dict[str, int] = {}
        for sku in cand:
            if sku in assigned:
                continue
            avail = cand[sku] - used
            if len(avail) == 1:
                singles[sku] = next(iter(avail))
        # KONFLIKT-SCHUTZ: wollen ZWEI Varianten dieselbe eBay-Variation als einzigen Kandidaten
        # (z.B. eBay fehlt eine Variation), wird KEINE davon zugeordnet -> fail-closed, nie geraten.
        jcount: dict[int, int] = {}
        for j in singles.values():
            jcount[j] = jcount.get(j, 0) + 1
        for sku, j in singles.items():
            if jcount[j] == 1:
                assigned[sku] = j
                used.add(j)
                changed = True
    return assigned


def _map_variations_to_internal(variations: list, internal_rows: list[dict],
                                listing=None, product=None) -> dict[int, dict]:
    """BEST-EFFORT (DISPLAY-ONLY): jede echte eBay-Variation ihrer internen Variante zuordnen, um
    EK/AE-Preis/attr fuer die Anzeige anzuhaengen. Rückgabe: {ev_index: internal_row}.

    ZWEISTUFIG, echte Merkmals-Evidenz zuerst:
      A) WERTE-BASIERT (drift-sicher, weil über die TATSÄCHLICHEN Merkmalswerte, nicht die Position):
         exakte Merkmals-Signatur (eindeutig) → globale bijektive Präfix-Zuordnung. Deckt auch
         UMSORTIERUNGEN von product.variants ab. (Bewusst KEIN Abgleich der internen SKU gegen die echte
         eBay-SKU: beide sind {base}-V{i} = positionsabgeleitet – nach einem Umsortieren zeigt die interne
         V-Position auf die falsche Variante, ein SKU-Gleichstand wäre also nur getarnte Positions-Logik.)
      B) POSITION als FALLBACK nur für eBay-Variationen, die A) NICHT zuordnen konnte (typisch: eBay-Wert
         in anderer Sprache als der intern gekürzte, z. B. „紅色" vs „Rot" – dann ist die Publish-Position
         {base}-V{i} das einzige verlässliche Signal). DREI Sicherungen gegen product.variants-Drift:
           (i)   POSITIONS-VERTRAUEN: widerspricht bei IRGENDEINER werte-zugeordneten Variation die echte
                 eBay-V-Position der (aus LIVE-product.variants neu berechneten) internen V-Position, wurde
                 seit Publish umsortiert → Position ist KORRUPT → B komplett aus (lieber „?" als falsch).
           (ii)  gleiche Variantenzahl (kein Hinzufügen/Entfernen).
           (iii) die V-Position im internen Modell ist EINDEUTIG (keine [:50]-Trunkierungs-Kollision).
         (i) fängt jedes Umsortieren, das eine werte-VERGLEICHBARE Variante betrifft. RESTRISIKO (sehr eng,
         display-only, Preis vom Nutzer bestätigt): ein Umsortieren AUSSCHLIESSLICH unter mehreren zugleich
         werte-UNZUORDENBAREN Varianten bleibt unentdeckt (keine vergleichbare Variante widerlegt Position).
      C) BESTELL-AUFLOESUNG als Schlussstufe (nur mit listing/product): fuer importierte Listings und
         gewechselte Hauptquellen (deutsche eBay-Werte vs. englische Quell-Optionen) ordnet
         resolve_selection_against_skus fail-closed zu — dieselbe Maschinerie, die echte Bestellungen
         adressiert. Mehrdeutiges bleibt weiterhin unzugeordnet.

    Adressiert NIE die Variation für den Write (das macht die ECHTE eBay-SKU/Specifics direkt) – ein
    Fehltreffer setzt also NIE einen Preis auf die FALSCHE Variation. Einschränkung: im Marge-/Gewinn-
    Modus fliesst der hier gemappte EK in den BERECHNETEN Zielpreis der (korrekt adressierten) Variation
    ein; der Nutzer sieht Preis+Marge in der Vorschau vor dem Übernehmen. Mehrdeutiges bleibt UNzugeordnet
    (EK = None → „?" statt Schätzung).
    """
    from app.services.listing_match_service import _value_sig
    out: dict[int, dict] = {}
    claimed_ir: set[int] = set()                               # index in internal_rows bereits vergeben
    claimed_ev: set[int] = set()                               # eBay-Variation bereits zugeordnet

    # A) WERTE-BASIERT ZUERST: exakte Merkmals-Signatur (eindeutig) → globale bijektive Präfix-Zuordnung.
    by_real_sig: dict[str, list[int]] = {}
    for i, v in enumerate(variations):
        sig = _value_sig([val for _n, val in (v.get("specifics") or [])])
        if sig:
            by_real_sig.setdefault(sig, []).append(i)
    our_by_sku = {r["sku"]: (r.get("axis_options") or {}) for r in internal_rows}
    assign = _bijective_prefix_assign(our_by_sku, variations)   # {internal_sku: ev_index}
    for j, r in enumerate(internal_rows):
        sig = _value_sig((r.get("axis_options") or {}).values())   # 1) exakte Merkmals-Signatur (eindeutig)
        cands = by_real_sig.get(sig) if sig else None
        ev = cands[0] if (cands and len(cands) == 1) else None
        if ev is None:                                          # 2) globale bijektive Präfix-Zuordnung
            ev = assign.get(r["sku"])
        if ev is not None and ev not in claimed_ev:
            out[ev] = r
            claimed_ir.add(j)
            claimed_ev.add(ev)

    # (i) POSITIONS-VERTRAUEN: widerspricht bei einer werte-zugeordneten Variation die echte eBay-V-Position
    #     der internen V-Position, wurde product.variants seit Publish umsortiert → Position ist als Signal
    #     korrupt → Positions-Fallback (B) komplett abschalten.
    position_trustworthy = True
    for ev, r in out.items():
        ev_m = re.search(r"-V(\d+)$", str(variations[ev].get("sku") or ""))
        ir_m = re.search(r"-V(\d+)$", str(r.get("sku") or ""))
        if ev_m and ir_m and int(ev_m.group(1)) != int(ir_m.group(1)):
            position_trustworthy = False
            break

    # B) POSITIONS-FALLBACK {base}-V{i} – NUR für werte-basiert offene eBay-Variationen, drift-gesichert.
    if position_trustworthy and len(internal_rows) == len(variations):  # (i) kein Umsortieren, (ii) kein +/-
        vpos_count: dict[int, int] = {}
        ir_by_vpos: dict[int, int] = {}                        # V-Position -> index in internal_rows
        for j, r in enumerate(internal_rows):
            m = re.search(r"-V(\d+)$", str(r.get("sku") or ""))
            if m:
                pos = int(m.group(1))
                vpos_count[pos] = vpos_count.get(pos, 0) + 1
                ir_by_vpos.setdefault(pos, j)
        for i, v in enumerate(variations):
            if i in claimed_ev:
                continue
            m = re.search(r"-V(\d+)$", str(v.get("sku") or ""))
            if not m:
                continue
            pos = int(m.group(1))
            if vpos_count.get(pos) != 1:                       # mehrdeutige Position (Trunkierung) → nicht raten
                continue
            j = ir_by_vpos.get(pos)
            if j is not None and j not in claimed_ir:
                out[i] = internal_rows[j]
                claimed_ir.add(j)
                claimed_ev.add(i)

    # C) BESTELL-MASCHINERIE als letzte Stufe: importierte Listings / gewechselte Hauptquellen
    #    tragen deutsche eBay-Werte + fremde SKUs ohne {base}-V{i} — A und B greifen dort nie.
    #    resolve_selection_against_skus ist die geld-erprobte Aufloesung des Bestell-Flows
    #    (DE→EN-Uebersetzung, je Quelle gelernte variant_map, Mehrdeutigkeits-Sperren) —
    #    deterministisch und fail-closed, keine Schaetzung.
    if listing is not None and product is not None:
        open_evs = [i for i in range(len(variations)) if i not in claimed_ev]
        if open_evs:
            from app.services.order_service import resolve_selection_against_skus
            skus = (product.variants or {}).get("skus") or []
            ir_by_attr: dict[str, int] = {}
            for j, r in enumerate(internal_rows):
                if r.get("attr") and r["attr"] not in ir_by_attr:
                    ir_by_attr[r["attr"]] = j
            for i in open_evs:
                sel = {str(n): str(val) for n, val in (variations[i].get("specifics") or [])
                       if str(val).strip()}
                if not sel:
                    continue
                try:
                    hit = resolve_selection_against_skus(
                        sel, skus, listing=listing,
                        source_id=getattr(product, "aliexpress_id", None))
                except Exception:  # noqa: BLE001 – Anzeige-Mapping darf den Dialog nie sprengen
                    continue
                attr = (hit or {}).get("attr")
                j = ir_by_attr.get(attr) if attr else None
                if j is not None and j not in claimed_ir:
                    out[i] = internal_rows[j]
                    claimed_ir.add(j)
                    claimed_ev.add(i)
    return out


def _uniform_source_ek(listing, product, settings) -> float | None:
    """EK, wenn er quellenweit DETERMINISTISCH ist: ALLE Quell-SKUs tragen denselben Preis
    (oder es gibt gar keine SKUs, nur product.price_cny). Dann kostet jede eBay-Variation
    im Einkauf dasselbe — das ist Aufloesung, keine Schaetzung. Sonst None."""
    try:
        skus = (product.variants or {}).get("skus") or []
        prices: set[float] = set()
        for s in skus:
            p = s.get("price")
            if p is None:
                return None                        # SKU ohne Preis -> nicht deterministisch
            prices.add(round(float(p), 4))
        if len(prices) > 1:
            return None
        cny = prices.pop() if prices else product.price_cny
        if cny is None:
            return None
        # Lokal-Kalkulation nur, wenn das Lager EINHEITLICH ist: bei gemischtem "Versand
        # aus" (China + EU) waere der EK je Erfuellung verschieden -> fail-closed None.
        ships = [s.get("ship_from") for s in skus if s.get("ship_from")]
        eu_flags = [_ae_api.has_eu_warehouse([sf]) for sf in ships]
        if eu_flags and any(eu_flags) and not all(eu_flags):
            return None
        local = bool(eu_flags) and all(eu_flags)
        return round(pricing.effective_cost(
            float(cny), settings=settings, ship_override=_supplier_ship(listing),
            local=local), 2)
    except Exception:  # noqa: BLE001 – Anzeige-Hilfe, nie den Dialog sprengen
        return None


async def append_corrected_variants(db: Session, *, listing_id: int) -> dict:
    """OPTION A (Vorfall Zirkonia-Kette 09.08., Nutzer-Freigabe): korrekt benannte
    Quell-Varianten ZUSAETZLICH an die Live-Gruppe anhaengen, falsch benannte
    Alt-Variationen auf Menge 0 setzen. Listing/Item-ID/Verkaufshistorie bleiben.

    eBay laesst Variationsnamen an veroeffentlichten Listings nicht aendern (25013) —
    dieser Weg ist die dokumentierte Reparatur fuer Listings MIT Verkauf. Fail-closed:
    nur Ein-Achsen-Listings; Werte, die live schon existieren, werden nicht doppelt
    angelegt. NUR per Nutzer-Klick (Regel 7/8: nichts wird beendet, kein Preis geaendert;
    neue Varianten bekommen den intern kalkulierten Preis wie beim Publish)."""
    from app.services import fast_shipping_service as _fss
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    gk = listing.ebay_draft_id or ""
    if not (listing.ebay_item_id and listing.listing_status == "active" and gk.endswith("-GRP")):
        raise PersistentError("Das geht nur bei einem aktiven Multivarianten-Artikel, "
                              "der ueber dieses Tool veroeffentlicht wurde.")
    product = db.get(Product, listing.product_id) if listing.product_id else None
    axis_names, variants = _usable_variants(product)
    if len(axis_names) != 1:
        raise PersistentError("Automatisch nur bei EIN-Achsen-Listings — dieses Listing "
                              "hat mehrere Varianten-Achsen (bitte manuell klaeren).")
    settings = get_settings()
    ebay = _real_ebay()
    with task_log(db, task_type="append_variants", reference_id=listing_id) as tl:
        grp = await ebay.get_inventory_item_group(gk)
        live_skus = list((grp or {}).get("variantSKUs") or [])
        varies = (grp or {}).get("variesBy") or {}
        gspecs = [s for s in (varies.get("specifications") or []) if s.get("name")]
        if len(gspecs) != 1 or not live_skus:
            raise PersistentError("Live-Gruppe nicht lesbar oder mehrachsig — bitte manuell.")
        live_axis = gspecs[0]["name"]
        live_values = [str(x) for x in (gspecs[0].get("values") or [])]
        live_lc = {v.strip().lower() for v in live_values}

        def _raw_name(v: dict) -> str:
            # ORIGINAL-Name der Quelle: die WAHRHEIT steht im SKU-attr nach '#' (Regel 9).
            # product.variants kann bei selbst veroeffentlichten Listings bereits die
            # germanisierten — im Zirkonia-Fall ERFUNDENEN — Labels tragen; genau die
            # sollen ersetzt werden, nicht erneut angehaengt. Fallback: Options-Wert.
            attr = str(v.get("attr") or "")
            if "#" in attr:
                raw = attr.split("#", 1)[1].strip()
                if raw:
                    return raw
            return str(v["options"][axis_names[0]])

        base_sku = _publish_base_sku(listing)
        calc = {c["sku"]: c for c in compute_variant_prices(listing, product, settings)}
        desc = _html_description(listing.description or listing.title_seo)
        pol = await _fss.publish_policies(ebay, listing)
        default_qty = settings.default_listing_quantity
        new_entries: list[tuple[dict, str, str]] = []      # (variante, neuer sku, wert)
        resumed: list[tuple[dict, str, str]] = []          # frueherer Teillauf (Item existiert)
        for i, v in enumerate(variants, 1):
            val = _raw_name(v)
            nsku = f"{base_sku}-R{i}"[:50]
            if nsku in live_skus:
                # WIEDERAUFNAHME (Echtlauf 758): ein frueherer Lauf hat Item+Gruppe schon
                # geschrieben, brach aber vor Publish/Nullung/Pinning ab -> nicht neu
                # anlegen, aber die restlichen Schritte zu Ende fuehren.
                resumed.append((v, nsku, val))
                continue
            if val.strip().lower() in live_lc:
                continue                                   # existiert live schon korrekt
            vstock = v.get("stock")
            vqty = 0 if (vstock is not None and vstock <= 0) else default_qty
            vimg = v.get("image") or next(iter((product.images or [])), None)
            await ebay.create_inventory_item(
                nsku, title=listing.title_seo, description=desc,
                image_urls=([vimg] if vimg else []), quantity=vqty,
                # eBay-Inventory verlangt Aspect-Werte als LISTE (Fehler 2004:
                # "Could not serialize field [product.aspects.*]" bei blankem String).
                aspects={live_axis: [val]}, brand=None)
            price = ((calc.get(variant_ebay_sku(base_sku, v, i)) or {}).get("price_eur")
                     or float(listing.price_eur or 0))
            await ebay.create_offer(nsku, price_eur=float(price),
                                    category_id=listing.category_id or "0",
                                    quantity=vqty, listing_description=desc,
                                    listing_policies=pol)
            new_entries.append((v, nsku, val))
        if not new_entries and not resumed:
            tl.result_data = {"appended": 0}
            return {"listing_id": listing_id, "appended": 0, "zeroed": [],
                    "message": "Alle Quell-Varianten existieren bereits unter ihrem Namen."}

        # Gruppe voll-ersetzend NEU schreiben: alte SKUs/Werte BLEIBEN + neue kommen dazu.
        # Pflicht-Merkmale der Kategorie wie beim Publish auto-fuellen (sonst 25002
        # "Produktart fehlt" — Echtlauf 758); die Varianten-Achse bleibt draussen.
        # Gruppe IMMER (auch beim Resume) voll-ersetzend mit gefuellten Pflicht-Merkmalen
        # schreiben: der Teillauf hatte sie ohne "Produktart" hinterlassen -> Publish 25002.
        base_aspects = dict(listing.item_specifics or {"Marke": "Markenlos"})
        filled = _truncate_aspects(await ebay.build_aspects(listing.category_id or "0",
                                                            base_aspects))
        non_axis = {k: v for k, v in filled.items() if k != live_axis}
        new_vals = [val for _v, _n, val in new_entries if val not in live_values]
        await ebay.create_inventory_item_group(
            gk, title=listing.title_seo, description=desc,
            image_urls=gallery_images(product),
            variant_skus=live_skus + [n for _v, n, _val in new_entries],
            specifications=[{"name": live_axis, "values": live_values + new_vals}],
            image_varies_by=(varies.get("aspectsImageVariesBy") or None),
            aspects=(non_axis or None))
        await ebay.publish_offer_by_inventory_item_group(gk)

        # Falsch benannte Alt-Variationen (Wert existiert NICHT in der Quelle) -> Menge 0.
        local_lc = {_raw_name(v).strip().lower() for v in variants}
        zero_updates = []
        for sku in live_skus:
            item = await ebay.get_inventory_item(sku)
            vals = [str(x) for vv in (((item or {}).get("product") or {})
                                      .get("aspects") or {}).values()
                    for x in (vv if isinstance(vv, list) else [vv])]
            if any(str(x).strip().lower() in local_lc for x in vals):
                continue                                   # traegt einen korrekten Quell-Namen
            offer = await ebay._first_offer_for_sku(sku)   # noqa: SLF001
            if offer and offer.get("offerId"):
                zero_updates.append({"sku": sku, "offer_id": offer["offerId"], "quantity": 0})
        if zero_updates:
            await ebay.bulk_update_price(zero_updates)

        # ERST JETZT lokal schreiben: neue SKUs an die Quell-Varianten pinnen, Spiegel leeren.
        for v, nsku, _val in new_entries + resumed:
            v["ebay_sku"] = nsku
        flag_modified(product, "variants")
        listing.variant_stock = None
        flag_modified(listing, "variant_stock")
        db.commit()
        tl.result_data = {"appended": len(new_entries), "resumed": len(resumed),
                          "zeroed": [u["sku"] for u in zero_updates]}
    return {"listing_id": listing_id, "appended": len(new_entries),
            "resumed": len(resumed),
            "zeroed": [u["sku"] for u in zero_updates],
            "message": (f"{len(new_entries)} Variante(n) angehaengt, {len(resumed)} aus "
                        f"frueherem Teillauf uebernommen, {len(zero_updates)} falsch "
                        "benannte auf Menge 0 gesetzt. Der Artikel blieb online.")}


async def variant_name_audit(db: Session, *, limit: int = 500) -> dict:
    """READ-ONLY Bestands-Scan (Nutzer-Auftrag 09.08.): bei welchen Live-Multivarianten-
    Listings passen die eBay-Variationsnamen NICHT zur Quelle (erfundene Namen wie bei
    der Zirkonia-Kette)? Prueft je Variation Regeln/gelernte Map + Mass-Matching (ohne
    KI, ohne Schreiben). Verdaechtig = mindestens eine unzuordenbare Variation."""
    from app.integrations.llm import _measurement_set
    from app.services.order_service import resolve_selection_against_skus
    listings = db.scalars(select(Listing).where(
        Listing.listing_status == "active",
        Listing.ebay_item_id.isnot(None))).all()
    ebay = _real_ebay()
    out, checked = [], 0
    for l in listings[:max(1, limit)]:
        product = db.get(Product, l.product_id) if l.product_id else None
        skus = ((product.variants or {}).get("skus") or []) if product else []
        if not skus:
            continue
        try:
            info = await ebay.get_item_price_info(l.ebay_item_id)
        except Exception:  # noqa: BLE001 – ein Listing stoppt den Scan nicht
            continue
        variations = info.get("variations") or []
        if len(variations) < 2:
            continue
        checked += 1
        open_names = []
        for v in variations:
            sel = {str(n): str(val) for n, val in (v.get("specifics") or [])
                   if str(val).strip()}
            if not sel:
                continue
            if resolve_selection_against_skus(sel, skus, listing=l,
                                              source_id=str(product.aliexpress_id)):
                continue
            ms = _measurement_set(" ".join(sel.values()))
            if ms and sum(1 for s in skus if ms <= _measurement_set(
                    " ".join(str(x) for x in (s.get("options") or {}).values())
                    + " " + str(s.get("attr") or ""))) == 1:
                continue                                   # Mass-eindeutig -> zuordenbar
            open_names.append(" / ".join(sel.values())[:50])
        if open_names:
            out.append({"listing_id": l.id, "title": l.title_seo,
                        "ebay_item_id": l.ebay_item_id,
                        "total": len(variations), "unmapped": len(open_names),
                        "beispiele": open_names[:5]})
    out.sort(key=lambda x: -(x["unmapped"] / max(1, x["total"])))
    return {"checked": checked, "suspicious": len(out), "listings": out}


async def auto_map_ebay_variations(db: Session, *, listing_id: int) -> dict:
    """ALLE eBay-Variationen eines Listings im STAPEL den Hauptquellen-SKUs zuordnen
    (Nutzerwunsch 09.08., Fall Cuban Chain: einzeln zuordnen ist zu aufwaendig).

    Drei Stufen je offener Variation, fail-closed:
      1. Regeln + gelernte variant_map (resolve_selection_against_skus) — schon klar?
      2. MASS-MATCHING deterministisch: alle Mass-Tokens der eBay-Variation (8mm, 20cm, …)
         muessen in GENAU EINEM Quell-SKU stecken (Regel 9: Masse sind die Wahrheit;
         Tie -> offen lassen, nie raten).
      3. KI wie beim Bestellen (resolve_variant_smart) — zusaetzlich hart per
         preserves_measurements abgesichert: die KI darf NIE eine Zuordnung liefern,
         die ein Mass veraendert.
    Gelernt wird in listing.variant_map (bijektiv innerhalb des Laufs); Netz-Calls laufen
    VOR dem einen kurzen Commit (Regel 12). Bestellt nichts, aendert keine Preise."""
    from app.integrations.llm import _measurement_set, preserves_measurements
    from app.services.order_service import (_learn_variant, resolve_selection_against_skus,
                                            resolve_variant_smart)
    listing = db.get(Listing, listing_id)
    if listing is None or not listing.ebay_item_id:
        raise PersistentError("Listing nicht gefunden oder nicht live")
    product = db.get(Product, listing.product_id) if listing.product_id else None
    skus = ((product.variants or {}).get("skus") or []) if product else []
    if not skus:
        raise PersistentError("Keine Quell-Varianten am Produkt — erst Quelle hinterlegen")
    ebay = _real_ebay()
    info = await ebay.get_item_price_info(listing.ebay_item_id)
    variations = info.get("variations") or []
    if not variations:
        raise PersistentError("Keine eBay-Variationen an diesem Listing gefunden")

    def _src_text(s: dict) -> str:
        return " ".join(str(x) for x in (s.get("options") or {}).values()) + " " + str(s.get("attr") or "")

    sels = []
    for v in variations:
        sels.append({str(n): str(val) for n, val in (v.get("specifics") or [])
                     if str(val).strip()})
    # Pass 1: was Regeln/gelernte Map schon kennen, claimt seinen attr (Bijektivitaet).
    claimed: set[str] = set()
    state: list[tuple[dict, str | None]] = []          # (sel, bereits-aufgeloester attr)
    already = 0
    for sel in sels:
        hit = (resolve_selection_against_skus(sel, skus, listing=listing,
                                              source_id=str(product.aliexpress_id))
               if sel else None)
        attr = (hit or {}).get("attr")
        if attr:
            already += 1
            claimed.add(attr)
        state.append((sel, attr))
    # Pass 2+3 fuer die offenen Variationen.
    mapped, learned, open_names = 0, [], []
    for sel, attr in state:
        if attr or not sel:
            if not sel:
                open_names.append("(ohne Merkmale)")
            continue
        ebay_text = " ".join(sel.values())
        cand = None
        ms = _measurement_set(ebay_text)
        if ms:
            matches = [s for s in skus
                       if s.get("attr") and s["attr"] not in claimed
                       and ms <= _measurement_set(_src_text(s))]
            if len(matches) == 1:
                cand = matches[0]["attr"]
        if cand is None:
            hit2, _src = await resolve_variant_smart(db, product=product, listing=listing,
                                                     variant_selected=sel)
            a2 = (hit2 or {}).get("attr")
            sku2 = next((s for s in skus if s.get("attr") == a2), None) if a2 else None
            if (sku2 is not None and a2 not in claimed
                    and preserves_measurements(ebay_text, _src_text(sku2))):
                cand = a2
        if cand:
            claimed.add(cand)
            mapped += 1
            learned.append((sel, cand))
        else:
            open_names.append(" / ".join(sel.values())[:60])
    # ERST JETZT schreiben (alle Netz-Calls sind durch) — ein kurzer Commit.
    for sel, cand in learned:
        _learn_variant(listing, str(product.aliexpress_id), sel, cand, write_legacy=True)
    db.commit()
    return {"listing_id": listing_id, "total": len(variations), "already": already,
            "mapped": mapped, "open": open_names}


async def _ebay_variation_rows(ebay, listing, product, settings) -> list[dict] | None:
    """GRUNDSANIERUNG (20.07.): Dialog-Zeilen fuer ein LIVE-Multivarianten-Listing DIREKT aus den
    ECHTEN eBay-Variationen (GetItem) bauen – jede Zeile IST eine echte eBay-Variation:
      * ``sku``  = die ECHTE eBay-SKU (Identität für den Preis-Write; kein Heuristik-Matching mehr).
                   Hat die Variation keine SKU → Sentinel ``evsig:<Merkmals-Signatur>`` (Specifics-basiert).
      * ``name`` = die ECHTEN eBay-Merkmalswerte (VariationSpecifics), nicht die intern gekürzten.
      * ``current_price_eur`` = der ECHTE aktuelle eBay-Preis DIESER Variation (kein Signatur-Lookup).
      * ``stock`` = die aktuelle eBay-Menge.
    EK/AE-Preis werden NUR fuer die Anzeige best-effort aus dem internen Modell gemappt
    (``_map_variations_to_internal``) – nie geld-kritisch.

    Rückgabe ``None``, wenn das Listing KEINE echten Variationen hat (Einzel-Listing) oder GetItem
    scheitert → der Aufrufer fällt auf das interne Modell (``variant_price_rows``) zurück.
    """
    from app.services.listing_match_service import _value_sig
    info = await ebay.get_item_price_info(listing.ebay_item_id)
    variations = info.get("variations") or []
    if len(variations) < 2:
        return None                                            # Einzel-Listing → interner Pfad
    axis_names, uvars = _usable_variants(product)
    internal_rows: list[dict] = []
    if uvars:
        calc = compute_variant_prices(listing, product, settings)
        for cr, uv in zip(calc, uvars):
            try:
                ae = float(uv["price"]) if uv.get("price") is not None else None
            except (TypeError, ValueError):
                ae = None
            internal_rows.append({
                "sku": cr["sku"], "axis_options": cr.get("axis_options") or {},
                "ek_eur": cr.get("ek_eur"), "ae_price_eur": ae, "attr": uv.get("attr")})
    ev_map = (_map_variations_to_internal(variations, internal_rows, listing, product)
              if internal_rows else {})
    rows = []
    for i, v in enumerate(variations):
        specs = v.get("specifics") or []
        name = " / ".join(str(val) for _n, val in specs if str(val).strip())
        rsku = v.get("sku")
        realsig = _value_sig([val for _n, val in specs])
        if rsku:
            key = str(rsku)
        elif realsig:
            key = "evsig:" + realsig                            # keine SKU → Specifics-Identität
        else:
            key = f"evidx:{i}"                                  # weder SKU noch Merkmale → nicht sicher schreibbar
        internal = ev_map.get(i) or {}
        # EK NUR exakt aus der über die {base}-V{i}-Position zugeordneten internen Variante (= genau die
        # AliExpress-Variante, aus der WIR diese eBay-Variation publiziert haben). NIE schätzen: lässt sich
        # die Variation nicht sicher zuordnen, bleibt EK None (Dialog zeigt „?") und der Preis ist manuell
        # eingebbar – lieber ehrlich „unbekannt" als ein falscher Näherungswert.
        rows.append({
            "sku": key,
            "ebay_sku": (str(rsku) if rsku else None),
            "name": name or key,
            # Echte Merkmalspaare der Variation: Basis fuer die manuelle Zuordnung
            # ("EK zuordnen" im Dialog lernt damit die variant_map).
            "options": {str(n): str(val) for n, val in specs if str(val).strip()},
            "image": v.get("image"),                    # ECHTES eBay-Varianten-Bild (nicht AliExpress)
            "ae_price_eur": internal.get("ae_price_eur"),
            "ek_eur": internal.get("ek_eur"),           # exakter Varianten-EK ODER None (keine Schätzung)
            "current_price_eur": v.get("price"),
            "price_is_live": True,
            "stock": _int_or_none(v.get("quantity")),
            "ebay_driven": True,
        })
    # EINHEITSPREIS-QUELLE: konnte KEINE Variation zugeordnet werden, die Quelle hat aber einen
    # einzigen einheitlichen SKU-Preis, ist der EK trotzdem deterministisch (jede Variation wird
    # aus derselben Kondition erfuellt) — dann an alle Zeilen anhaengen statt ueberall "?".
    if all(r.get("ek_eur") is None for r in rows):
        uek = _uniform_source_ek(listing, product, settings)
        if uek is not None:
            for r in rows:
                r["ek_eur"] = uek
    # KOLLISIONS-SCHUTZ: Zeilen mit IDENTISCHER Identität (gleiche echte SKU ODER gleiche Specifics-
    # Signatur) waeren beim {sku: preis}-Aufbau still ueberschrieben worden – und lassen sich am Write
    # nicht eindeutig adressieren. Solche Zeilen bekommen eine nicht-schreibbare Identität (evidx:i) und
    # KEINEN berechenbaren EK -> im Dialog sichtbar (echter Preis), aber nicht per Marge/Gewinn setzbar
    # (fail-closed statt still einen zweiten Preis verschlucken).
    key_counts: dict[str, int] = {}
    for r in rows:
        key_counts[r["sku"]] = key_counts.get(r["sku"], 0) + 1
    for idx, r in enumerate(rows):
        if key_counts.get(r["sku"], 0) > 1:
            r["sku"] = f"evidx:{idx}"          # eindeutige, aber nicht-schreibbare Identität
            r["ek_eur"] = None                 # nicht per Modus repricebar (fail-closed)
            r["ambiguous"] = True
    return rows


async def _apply_classic_variation_prices(ebay, listing, targets: list[tuple[str, float]],
                                          options_by_sku: dict,
                                          skip_skus: set | None = None) -> tuple[list, list]:
    """Klassisches (Trading-API-)Listing: je Ziel-Variante IHREN EIGENEN Preis setzen.

    ``targets`` = [(unser_sku, preis)]. Ordnet jede Ziel-Variante der ECHTEN eBay-Variation zu
    (exakte eBay-SKU ODER eindeutige Merkmals-Signatur wie beim Live-Preis-Abgleich) und setzt in
    EINEM ReviseFixedPriceItem je Variation IHREN Preis – NIE mehr alle Variationen auf einen Preis
    flach (Fund 18.07.). Nicht eindeutig zuordenbare Ziele werden gemeldet (fail-closed, kein Raten).

    ``skip_skus`` = eBay-SKUs, die bereits ueber die Inventory-API gesetzt wurden – die werden NICHT
    mit gesendet (sonst wuerde ihr alter GetItem-Preis den frischen Bulk-Preis ueberschreiben).
    Rueckgabe: (updated, failed).
    """
    from app.services.listing_match_service import _value_sig
    skip_skus = skip_skus or set()
    updated, failed = [], []
    try:
        info = await ebay.get_item_price_info(listing.ebay_item_id)
    except Exception as exc:  # noqa: BLE001 – ohne GetItem keine sichere Zuordnung
        return [], [{"sku": s, "error": f"GetItem fehlgeschlagen: {str(exc)[:90]}"} for s, _ in targets]
    variations = info.get("variations") or []
    if not variations:
        # Einzel-Listing (keine echten Variationen) -> reiner ItemID-Preis (ReviseInventoryStatus,
        # aendert die Menge NICHT und kann NIE flach setzen). Nur fuer EIN Ziel sinnvoll.
        sku, target = targets[0]
        try:
            await ebay.revise_single_item_price(listing.ebay_item_id, target)
            updated.append({"sku": sku, "price_eur": target})
        except Exception as exc:  # noqa: BLE001
            failed.append({"sku": sku, "error": str(exc)[:120]})
        failed.extend({"sku": s, "error": "Listing hat keine Variationen"} for s, _ in targets[1:])
        return updated, failed
    # eBay-Variationen indexieren: exakte SKU + Merkmals-Signatur (mehrdeutig -> nicht zuordnen).
    by_sku, by_sig = {}, {}
    sig_by_idx: dict[int, str] = {}      # ev-Index -> ECHTE Merkmals-Signatur (fuer live_prices-Nachzug)
    for i, v in enumerate(variations):
        if v.get("sku"):
            by_sku[str(v["sku"])] = v
        sig = _value_sig([val for _n, val in (v.get("specifics") or [])])
        sig_by_idx[i] = sig
        if sig:
            by_sig.setdefault(sig, []).append(v)
    # GLOBALE bijektive Präfix-Zuordnung über ALLE Varianten (löst Mehrdeutigkeit wie Guinea/
    # Guinea-Bissau via Zwang). Einmal für das ganze Listing berechnen.
    assign = _bijective_prefix_assign(options_by_sku, variations)
    # Jedes Ziel GENAU EINER echten eBay-Variation zuordnen (fail-closed bei Mehrdeutigkeit).
    target_price = {}    # variations-Index -> (unser_sku, neuer Preis)
    ev_index = {id(v): i for i, v in enumerate(variations)}
    unique_sigs = {s for s, vs in by_sig.items() if len(vs) == 1}   # nur EINDEUTIGE Signaturen (live_prices)
    for sku, target in targets:
        _is_evsig = str(sku).startswith("evsig:")
        # evsig-Keys NIE über by_sku auflösen (reservierter Namespace): sonst könnte eine echte SKU,
        # die zufällig wie "evsig:<sig>" aussieht, die eindeutigkeits-geprüfte Signatur-Auflösung umgehen.
        ev = None if _is_evsig else by_sku.get(str(sku))         # exakte ECHTE eBay-SKU (eBay-getriebene
        #                                                          Zeile ODER self-pub {base}-V{i})
        if ev is None and _is_evsig:
            # GRUNDSANIERUNG: eBay-getriebene Zeile OHNE eigene SKU – über die ECHTE Merkmals-Signatur
            # (aus GetItem) EINDEUTIG auflösen. Kein Heuristik-/Positions-Raten, fail-closed bei Kollision.
            cands = by_sig.get(str(sku)[len("evsig:"):])
            ev = cands[0] if (cands and len(cands) == 1) else None
        if ev is None and not _is_evsig:
            sig = _value_sig((options_by_sku.get(sku) or {}).values())
            cands = by_sig.get(sig) if sig else None
            ev = cands[0] if (cands and len(cands) == 1) else None   # eindeutig? sonst fail-closed
        if ev is None and not _is_evsig:
            # Deckt den systematischen Fall ab, dass eBay die VERBOSEN AliExpress-Werte führt
            # („60cm oder 23,6 Zoll") und wir die LLM-gekürzten („60cm"): globale Präfix-Zwangs-
            # Zuordnung. Kein Treffer -> fail-closed (KEINE Position, kein Raten).
            j = assign.get(sku)
            ev = variations[j] if j is not None else None
        if ev is None:
            # DIAGNOSE: unser Merkmal-Code vs. die tatsaechlichen eBay-Codes – zeigt Rename vs.
            # Kollision vs. leere eBay-Merkmale, ohne DB-Zugriff (Nutzer kann die Meldung teilen).
            _osig = (_value_sig((options_by_sku.get(sku) or {}).values()) or "(leer)")[:40]
            _es = ", ".join(f"{(s or '(leer)')[:22]} x{len(vs)}"
                            for s, vs in list(by_sig.items())[:6])
            _more = "..." if len(by_sig) > 6 else ""
            failed.append({"sku": sku, "error":
                f"Variante nicht zuordenbar — unser Code [{_osig}]; eBay {len(variations)} Var.: "
                f"[{_es}{_more}]. Listing neu veröffentlichen oder Preis im eBay Seller Hub setzen"})
            continue
        idx = ev_index[id(ev)]
        if idx in target_price:                # zwei Ziele auf DIESELBE eBay-Variation
            failed.append({"sku": sku, "error": "mehrdeutige Zuordnung (Signatur-Kollision)"})
            continue
        target_price[idx] = (sku, target)
    if not target_price:
        return updated, failed
    # INVENTORY-API-Listings (-GRP / von uns publiziert): die ECHTE Variations-SKU (aus GetItem) hat
    # ein Sell-Offer -> Preis ueber die Inventory-API setzen (bulk_update_price). Trading-Revise lehnt
    # eBay bei warenbestandsbasierten Listings ab (Fund 18.07.: der Import hatte ebay_sku auf den
    # Gruppen-Key "...-GRP" gesetzt, die konstruierte SKU traf daher KEIN Offer -> Trading -> Ablehnung).
    # Die REALE SKU aus GetItem trifft das Offer -> korrekt per Inventory-API.
    inv_done = set()
    for idx in list(target_price):
        our_sku, price = target_price[idx]
        real_sku = (variations[idx] or {}).get("sku")
        if not real_sku or str(real_sku) in skip_skus:
            continue
        try:
            offer = await ebay._first_offer_for_sku(real_sku)
        except Exception:  # noqa: BLE001
            offer = None
        if not (offer and offer.get("offerId")):
            continue                                       # kein Offer -> echt klassisch (Trading unten)
        inv_done.add(idx)
        try:
            await ebay.bulk_update_price([{"sku": real_sku, "offer_id": offer["offerId"],
                                           "price_eur": price}])
            _rs = sig_by_idx.get(idx)
            updated.append({"sku": our_sku, "price_eur": price, "ebay_sku": str(real_sku),
                            "sig": (_rs if _rs in unique_sigs else None)})   # nur eindeutige Sig persistieren
            skip_skus = skip_skus | {str(real_sku)}        # aus dem Trading-Echo raushalten
        except Exception as exc:  # noqa: BLE001 – abgelehnt = NICHT zusaetzlich per Trading versuchen
            failed.append({"sku": our_sku, "error": str(exc)[:120]})
    for idx in inv_done:
        target_price.pop(idx, None)
    if not target_price:
        return updated, failed                             # alles ueber die Inventory-API gesetzt
    # SICHERHEIT: ALLE Variationen senden – Ziel -> neuer Preis, sonst der AKTUELLE eBay-Preis;
    # IMMER mit aktueller MENGE (sonst nullt eBay den Bestand!) und SKU. So wird keine Variation
    # weggelassen (kein Loesch-Risiko) und keine flach gesetzt; nur die ausgewaehlten aendern sich.
    revise_payload, sent = [], []
    for i, v in enumerate(variations):
        qty = _int_or_none(v.get("quantity"))
        if i in target_price:
            sku_i, price = target_price[i]
            if qty is None:
                # Ohne bekannte Menge NICHT schreiben (sonst Bestand -> 0). Variante bleibt unberuehrt.
                failed.append({"sku": sku_i, "error": "Menge unbekannt – Preis nicht sicher setzbar"})
                continue
            sent.append((sku_i, price, i))
        else:
            if v.get("sku") and str(v["sku"]) in skip_skus:
                continue                        # schon per Inventory-API gesetzt -> nicht ueberschreiben
            price = v.get("price")
            if price is None or price <= 0 or qty is None:
                continue                        # untouched ohne saubere Basis -> weglassen (bleibt unveraendert)
        revise_payload.append({"specifics": v.get("specifics"), "sku": v.get("sku"),
                               "quantity": qty, "price_eur": price})
    if not sent:
        return updated, failed                  # nichts sicher sendbar
    try:
        await ebay.revise_variation_prices(listing.ebay_item_id, revise_payload)
        updated.extend({"sku": s, "price_eur": pr,
                        "ebay_sku": (str(variations[i].get("sku")) if variations[i].get("sku") else None),
                        "sig": (sig_by_idx.get(i) if sig_by_idx.get(i) in unique_sigs else None)}
                       for s, pr, i in sent)
    except Exception as exc:  # noqa: BLE001 – der eine Revise-Call schlug ganz fehl
        failed.extend({"sku": s, "error": str(exc)[:120]} for s, pr, i in sent)
    return updated, failed


async def _verify_variation_prices(ebay, listing, updated: list, options_by_sku: dict):
    """Read-back je gesetzter Variante: echten eBay-Preis erneut lesen und gegen das Ziel pruefen.

    Rein informativ (landet im task_log); die Geld-Sicherheit haengt NICHT hieran, sondern daran,
    dass nur erfolgreich gepushte SKUs als Override persistiert werden. None wenn GetItem scheitert.
    """
    try:
        info = await ebay.get_item_price_info(listing.ebay_item_id)
    except Exception:  # noqa: BLE001
        return None
    from app.services.listing_match_service import _value_sig
    by_sku, by_sig = {}, {}
    for v in (info.get("variations") or []):
        if v.get("sku"):
            by_sku[str(v["sku"])] = v.get("price")
        sig = _value_sig([val for _n, val in (v.get("specifics") or [])])
        if sig:
            by_sig.setdefault(sig, []).append(v.get("price"))   # kollidierende Signatur = nicht eindeutig
    rows = []
    for u in updated:
        # Bevorzugt die ECHTE eBay-SKU/-Signatur, die der Write an der Variation festgehalten hat
        # (eBay-getriebene Zeilen + evsig); sonst der bisherige Weg über die interne Options-Signatur.
        actual = by_sku.get(str(u.get("ebay_sku"))) if u.get("ebay_sku") else None
        if actual is None:
            actual = by_sku.get(u["sku"])
        if actual is None:
            sig = u.get("sig") or _value_sig((options_by_sku.get(u["sku"]) or {}).values())
            cands = by_sig.get(sig) if sig else None
            actual = cands[0] if (cands and len(cands) == 1) else None   # mehrdeutig -> nicht verifizierbar
        if actual is None and not info.get("variations"):
            actual = info.get("current_price")
        ok = actual is not None and abs(float(actual) - float(u["price_eur"])) < 0.01
        rows.append({"sku": u["sku"], "expected": u["price_eur"], "actual": actual, "ok": ok})
    return {"variants": rows, "all_ok": (all(r["ok"] for r in rows) if rows else None)}


async def apply_variant_prices(db: Session, *, listing_id: int,
                               price_by_sku: dict[str, float]) -> dict:
    """Preise je Variante SETZEN – bei Live-Listings verifiziert auf eBay pushen,
    bei Entwuerfen lokal (Basis-Preis). Jeder Preis wird auf x,95 gerundet.

    Read-back-Verifikation je Offer wie in update_listing_live: nur ein wirklich
    auf eBay angekommener Preis gilt als Erfolg.
    """
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    settings = get_settings()
    cents = settings.price_cents or 0.95
    # Alle Zielpreise auf x,95 runden (Sicherheit, falls das UI Rohwerte schickt).
    targets = {str(sku): pricing.round_to_nearest_cents(float(p), cents)
               for sku, p in (price_by_sku or {}).items() if p is not None}
    if not targets:
        raise PersistentError("Keine Preise angegeben")

    if not listing.ebay_item_id:
        # Entwurf: kein eBay-Push; Override lokal setzen (Basis-Preis = hoechster).
        _persist_variant_price_overrides(db, listing, targets)
        listing.price_eur = Decimal(str(max(targets.values())))
        db.commit()
        return {"listing_id": listing_id, "pushed_to_ebay": False,
                "prices": targets, "price_eur": float(listing.price_eur)}

    ebay = _real_ebay()
    product = db.get(Product, listing.product_id) if listing.product_id else None
    # Options je Variante (NUR publizierte Achsen) fuer die Signatur-Zuordnung bei klassischen
    # Trading-API-Listings, deren eBay-SKUs oft NICHT den Positions-SKUs entsprechen – dieselbe
    # Signatur wie beim Live-Preis-Abgleich (eine Wahrheit).
    # ALT-SKU fuer selbst publizierte (-GRP) Listings: die ECHTE Inventory-Offer-SKU ist
    # {publish-base}-V{i}. Der publish-base steckt ZUVERLAESSIG in ebay_draft_id ({base}-GRP),
    # waehrend ebay_sku importweise auf UUID/Gruppen-Key verbogen sein kann (Fund 20.07.: dann traf
    # {ebay_sku}-V{i} kein Offer und die Signatur-Zuordnung scheiterte). Positions-Zuordnung (V-Index)
    # wie beim Fulfillment (siehe deterministische Varianten-Zuordnung).
    draft = listing.ebay_draft_id or ""
    alt_base = draft[:-len("-GRP")] if draft.endswith("-GRP") else None
    # EINDEUTIGE interne Merkmals-Signatur -> AE-attr: fuer die Override-Persistenz eBay-getriebener
    # Zeilen (deren Key die ECHTE eBay-SKU ist, nicht {base}-V{i}). Bei selbst publizierten Listings
    # sind die eBay-Merkmalswerte == die internen Werte -> die Signatur trifft. Nur EINDEUTIGE Signaturen
    # (kein Mehrfach-attr) zulassen (sonst mehrdeutig -> ausgelassen, best-effort).
    from app.services.listing_match_service import _value_sig as _vsig_ov
    _, _uvars_ov = _usable_variants(product)
    options_by_sku, alt_by_sku = {}, {}
    _sig_attr: dict[str, set] = {}
    _calc_ov = compute_variant_prices(listing, product, settings)
    for _i, r in enumerate(_calc_ov, 1):
        options_by_sku[r["sku"]] = r.get("axis_options") or {}
        if alt_base:
            alt_by_sku[r["sku"]] = f"{alt_base}-V{_i}"[:50]
        _attr_ov = _uvars_ov[_i - 1].get("attr") if _i - 1 < len(_uvars_ov) else None
        _isig = _vsig_ov((r.get("axis_options") or {}).values())
        if _isig and _attr_ov:
            _sig_attr.setdefault(_isig, set()).add(_attr_ov)
    attr_by_sig = {s: next(iter(a)) for s, a in _sig_attr.items() if len(a) == 1}
    internal_skus_ov = set(options_by_sku)              # exakte Menge der internen {base}-V{i}-Keys
    # ECHTE Offer-SKU (-GRP: {publish-base}-V{i}) -> interne SKU. Damit persistiert der Override auch,
    # wenn der eBay-getriebene Dialog die ECHTE Offer-SKU als Key schickt (Inventory-Pfad, kein sig).
    inv_alt = {v: k for k, v in alt_by_sku.items()}
    updated, failed = [], []
    with task_log(db, task_type="variant_prices", reference_id=listing_id) as tl:
        classic: list[tuple[str, float]] = []
        # 1) Inventory-API-Listings (von uns publiziert): je SKU ein eigenes Offer -> exakter Preis.
        #    Erst die konstruierte SKU, dann – falls kein Offer – die ALT-SKU (publish-base aus -GRP).
        for sku, target in targets.items():
            cands = [sku] + ([alt_by_sku[sku]] if alt_by_sku.get(sku) and alt_by_sku[sku] != sku else [])
            hit = None
            for cand in cands:
                try:
                    offer = await ebay._first_offer_for_sku(cand)
                except Exception:  # noqa: BLE001 – Lookup-Fehler -> naechster Kandidat/klassisch
                    offer = None
                if offer and offer.get("offerId"):
                    hit = (cand, offer)
                    break
            if hit:
                cand, offer = hit
                try:
                    await ebay.bulk_update_price([{"sku": cand, "offer_id": offer["offerId"],
                                                   "price_eur": target}])
                    # unser sku -> Persist/Anzeige; die getroffene Offer-SKU -> live_prices-Nachzug.
                    # internal_sku: eBay-getriebene Zeile (Key = echte Offer-SKU) -> interne SKU via
                    # inv_alt, damit der Override ueber das Positions-Mapping persistiert (-GRP-Fall).
                    _isk = sku if sku in internal_skus_ov else inv_alt.get(cand)
                    updated.append({"sku": sku, "price_eur": target, "ebay_sku": str(cand),
                                    "internal_sku": _isk})
                except Exception as exc:  # noqa: BLE001
                    failed.append({"sku": sku, "error": str(exc)[:120]})
            else:
                classic.append((sku, target))   # klassisch -> Trading-API je Variation
        # 2) Klassische/importierte Listings: EIN ReviseFixedPriceItem, jede Variation IHR EIGENER
        #    Preis (nie mehr alle auf einen Preis flach – Fund 18.07.); Zuordnung SKU/Signatur,
        #    fail-closed bei Mehrdeutigkeit.
        if classic:
            _u, _f = await _apply_classic_variation_prices(
                ebay, listing, classic, options_by_sku,
                skip_skus={u["sku"] for u in updated})   # schon per Inventory-API gesetzte nicht ueberschreiben
            updated.extend(_u)
            failed.extend(_f)
        # READ-BACK je gesetzter Variante: echten eBay-Preis erneut lesen und vergleichen.
        verified = await _verify_variation_prices(ebay, listing, updated, options_by_sku) if updated else None
        tl.result_data = {"updated": updated, "failed": failed, "verified": verified}
        # GELD-SICHERHEIT: NUR erfolgreich auf eBay gepushte Preise als Override persistieren.
        # Sonst stuende die DB (und damit Cockpit-Preis/Marge) hoch, waehrend eBay noch den alten
        # Preis fuehrt -> optimistische Scheinmarge, ggf. Verlustverkauf (Fund 17.07.). Fehlgeschlagene
        # SKUs behalten ihren bisherigen (mit eBay uebereinstimmenden) Preis.
        if updated:
            # 1) Positions-Overrides: interne {base}-V{i}-Keys ODER (Inventory-/-GRP-Pfad) die via
            #    inv_alt aufgeloeste interne SKU. _persist_variant_price_overrides mappt {base}-V{i}->attr.
            _persist_variant_price_overrides(
                db, listing, {(u.get("internal_sku") or u["sku"]): u["price_eur"] for u in updated})
            # 2) eBay-getriebene KLASSISCHE Zeilen ohne internen SKU-Bezug: Key ist die ECHTE eBay-SKU
            #    (nicht {base}-V{i}) -> Override best-effort ueber die EINDEUTIGE Merkmals-Signatur
            #    nachziehen (deckt selbst publizierte Trading-Listings ab; ueberlebt Re-Publish).
            extra_ov = {}
            for u in updated:
                if u.get("internal_sku") or str(u["sku"]) in internal_skus_ov:
                    continue                                   # schon per Positions-Mapping persistiert
                attr = attr_by_sig.get(u.get("sig")) if u.get("sig") else None
                if attr:
                    extra_ov[attr] = u["price_eur"]
            if extra_ov:
                vp_ov = dict(listing.variant_prices or {})
                vp_ov.update(extra_ov)
                listing.variant_prices = vp_ov
                flag_modified(listing, "variant_prices")
            # BEOBACHTBARKEIT (Review 20.07.): Der ECHTE eBay-Preis ist gesetzt+bestaetigt; nur die
            # Override-Buchhaltung (ueberlebt Re-Publish) konnte hier nicht per Position/Signatur an eine
            # AE-Variante gebunden werden (eng: Inventory-verwaltete, NICHT von uns publizierte Listings
            # ohne -GRP-Draft). Nicht still lassen -> im task_log vermerken, statt auf Stammwissen zu bauen.
            override_unpersisted = [
                u["sku"] for u in updated
                if (u.get("internal_sku") or str(u["sku"])) not in internal_skus_ov
                and not (u.get("sig") and attr_by_sig.get(u["sig"]))]
            tl.result_data["override_unpersisted"] = override_unpersisted
            listing.price_eur = Decimal(str(max(u["price_eur"] for u in updated)))
            # LIVE-PREIS lokal NACHZIEHEN: das Cockpit/der Report rechnen Marge/Spanne/Bucket gegen
            # ``ebay_live_prices`` (die Quelle der Wahrheit). Ohne dieses Nachziehen zeigte das Cockpit
            # nach dem Anheben WEITER die alte Marge/Spanne + „Verlust", bis zum naechsten 🔄 Sync
            # (Fund 20.07.). Wir kennen den echten neuen Preis (gerade gesetzt + je Variante bestaetigt)
            # -> unter Positions-SKU UND Merkmals-Signatur eintragen (dieselben Keys wie der Sync).
            from app.services.listing_match_service import _value_sig as _vsig
            from datetime import datetime as _dt, timezone as _tz
            lp = dict(listing.ebay_live_prices or {})
            for u in updated:
                lp[u["sku"]] = u["price_eur"]
                # ECHTE eBay-SKU (eBay-getriebene Zeile / getroffene Offer-SKU) – identisch zum
                # Key-Schema von ``sync_ebay_live_prices``.
                if u.get("ebay_sku") and str(u["ebay_sku"]) != u["sku"]:
                    lp[str(u["ebay_sku"])] = u["price_eur"]
                # interne Merkmals-Signatur (internes Modell) – deckt die interne Zeilen-Auflösung ab.
                opts = options_by_sku.get(u["sku"]) or {}
                sig = _vsig(opts.values() if isinstance(opts, dict) else opts)
                if sig:
                    lp["sig:" + sig] = u["price_eur"]
                # ECHTE eBay-Merkmals-Signatur (eBay-getriebene Zeilen): wichtig, wenn die eBay-SKU von
                # der internen SKU abweicht, die Merkmalswerte aber uebereinstimmen (importierte Listings).
                if u.get("sig"):
                    lp["sig:" + u["sig"]] = u["price_eur"]
            listing.ebay_live_prices = lp
            flag_modified(listing, "ebay_live_prices")
            listing.ebay_price_synced_at = _dt.now(_tz.utc)
        db.commit()
    if updated:
        # Cockpit-Report neu rechnen, damit Spanne/Marge/Bucket SOFORT stimmen (nicht erst nach Sync).
        try:
            from app.services.listing_match_service import rebuild_reprice_report
            rebuild_reprice_report(db)
        except Exception:  # noqa: BLE001 – Anzeige-Refresh darf den erfolgreichen Push nie kippen
            pass
    if not updated:
        raise PersistentError("Kein Variantenpreis konnte auf eBay gesetzt werden: "
                              + "; ".join(f"{f['sku']}: {f['error']}" for f in failed)[:300])
    return {"listing_id": listing_id, "pushed_to_ebay": True,
            "updated": updated, "failed": failed, "price_eur": float(listing.price_eur)}


MAX_BULK_REPRICE_LISTINGS = 300


async def bulk_reprice(db: Session, *, listing_ids: list[int], mode: str,
                       value: float, apply: bool = False) -> dict:
    """Bulk-Marge: fuer mehrere Listings die Variantenpreise auf eine Zielmarge (%) ODER
    einen festen Gewinn (€) rechnen. Modus 'margin' = Ziel-Marge in %, 'profit' = fixer
    Gewinn in € – je Variante einzeln aus deren EK gerechnet.

    apply=False -> nur Vorschau (alt->neu je Variante, propose-only).
    apply=True  -> live auf eBay setzen (nutzt apply_variant_prices mit Read-back).

    Baut ausschliesslich auf preview_variant_prices/apply_variant_prices auf (eine
    Rechen-Wahrheit; EK/Gebuehr/Anzeigenrate kommen aus denselben pricing-Helfern).
    """
    value = _validate_reprice_target(mode, value)   # fail-fast, bevor irgendetwas laeuft
    ids = [int(x) for x in dict.fromkeys(listing_ids or [])]  # dedupe, Reihenfolge stabil
    if not ids:
        raise PersistentError("Keine Listings angegeben")
    if len(ids) > MAX_BULK_REPRICE_LISTINGS:
        raise PersistentError(
            f"Zu viele Listings ({len(ids)}); max. {MAX_BULK_REPRICE_LISTINGS} pro Aufruf")

    results: list[dict] = []
    total_variants = 0
    for lid in ids:
        listing = db.get(Listing, lid)
        try:
            # Bulk-Marge nutzt bewusst das INTERNE Modell (kein GetItem je Listing): die Ziel-
            # Marge wird aus dem internen EK gerechnet; der Write ordnet je Variante ueber die
            # bestehende SKU/Signatur-Auflösung zu (fail-closed). ebay_driven=False.
            prev = await preview_variant_prices(db, listing_id=lid, mode=mode, value=value,
                                                ebay_driven=False)
        except Exception as exc:  # noqa: BLE001 – je Listing, Rest weiter
            results.append({"listing_id": lid, "ok": False, "reason": str(exc)[:160]})
            continue
        rows = []
        for r in prev.get("rows") or []:
            np = r.get("new_price_eur")
            if np is None:
                continue
            rows.append({
                "sku": r["sku"], "name": r.get("name"),
                "ek_eur": r.get("ek_eur"),
                "old_price_eur": r.get("current_price_eur"),
                "new_price_eur": np,
                "new_profit_eur": r.get("new_profit_eur"),
                "new_margin": r.get("new_margin"),
            })
        total_variants += len(rows)
        entry = {"listing_id": lid, "ok": True,
                 "title": (getattr(listing, "title_seo", None) if listing else None),
                 "is_live": bool(listing and listing.ebay_item_id),
                 "rows": rows}
        if apply and rows:
            try:
                pb = {r["sku"]: r["new_price_eur"] for r in rows}
                res = await apply_variant_prices(db, listing_id=lid, price_by_sku=pb)
                entry["applied"] = True
                entry["pushed_to_ebay"] = res.get("pushed_to_ebay")
                entry["failed"] = res.get("failed") or []
            except Exception as exc:  # noqa: BLE001
                entry["applied"] = False
                entry["reason"] = str(exc)[:160]
        elif apply:
            entry["applied"] = False
            entry["reason"] = "keine berechenbaren Varianten (EK fehlt?)"
        results.append(entry)

    ok = [r for r in results if r.get("ok")]
    applied_ok = [r for r in results if r.get("applied")]
    return {"mode": mode, "value": value, "applied": bool(apply),
            "listing_count": len(results), "ok_count": len(ok),
            "applied_count": len(applied_ok), "variant_count": total_variants,
            "results": results}


def variant_ebay_sku(base_sku: str, variant: dict, index: int) -> str:
    """Die eBay-SKU EINER Variante: die beim Publish festgeschriebene, sonst positionsbasiert.

    Die positionsbasierte Ableitung (V1..Vn) stimmt NUR solange, wie sich die Reihenfolge der
    lokalen Varianten nie aendert. Wird eine Variante geloescht, ruecken alle nachfolgenden
    eine Nummer hoch – Preise, Bestand und Gruppen-Updates landen dann auf der FALSCHEN
    eBay-Variante (z.B. Preis von '40x200cm' auf '35x500cm'). Darum schreibt der Publish die
    SKU an der Variante fest (``ebay_sku``) und alle Ableitungen lesen sie hier.

    Der positionsbasierte Fallback bleibt fuer Altbestand ohne festgeschriebene SKU; fuer
    LIVE-Listings holt ``bind_live_variant_skus`` die echte Zuordnung von eBay nach.
    """
    sku = (variant or {}).get("ebay_sku")
    return str(sku) if sku else f"{base_sku}-V{index}"[:50]


def _persist_variant_skus(product: Product | None, sku_by_attr: dict[str, str]) -> int:
    """``ebay_sku`` an den Varianten des Produkts festschreiben (Zuordnung ueber ``attr``).

    Gibt die Zahl der geaenderten Varianten zurueck. Committet NICHT (Regel 12: der
    Aufrufer schreibt kurz, nachdem alle Netz-Calls durch sind).
    """
    if product is None or not sku_by_attr:
        return 0
    variants = product.variants if isinstance(product.variants, dict) else {}
    skus = variants.get("skus") or []
    changed = 0
    for v in skus:
        want = sku_by_attr.get(v.get("attr"))
        if want and v.get("ebay_sku") != want:
            v["ebay_sku"] = want
            changed += 1
    if changed:
        new_variants = dict(variants)
        new_variants["skus"] = skus
        product.variants = new_variants
        flag_modified(product, "variants")
    return changed


async def _live_sku_options(ebay, listing) -> dict[str, dict] | None:
    """{live_sku: {aspektname: wert}} fuer ALLE SKUs der Live-Gruppe – oder None.

    None, sobald auch nur ein Inventory-Item nicht lesbar ist: eine unvollstaendige
    Momentaufnahme darf NIE Grundlage einer Zuordnung werden (lieber abbrechen als raten).
    """
    gk = listing.ebay_draft_id or ""
    if not gk.endswith("-GRP"):
        return None
    try:
        grp = await ebay.get_inventory_item_group(gk) or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Gruppe nicht lesbar: %s", str(exc)[:120])
        return None
    live_skus = [s for s in (grp.get("variantSKUs") or []) if s]
    if not live_skus:
        return None
    out: dict[str, dict] = {}
    for sku in live_skus:
        try:
            item = await ebay.get_inventory_item(sku)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Item %s nicht lesbar: %s", sku, str(exc)[:120])
            return None
        aspects = ((item or {}).get("product") or {}).get("aspects") or {}
        if not aspects:
            return None
        out[sku] = {k: _first_aspect_val(v) for k, v in aspects.items()}
    return out


def _match_variants_to_live_skus(axis_names: list[str], variants: list[dict],
                                 live_options: dict[str, dict]) -> tuple[dict, list] | None:
    """Lokale Varianten den ECHTEN Live-SKUs zuordnen – ueber die Achsen-WERTE.

    Rueckgabe ``({attr: live_sku}, [verwaiste_live_skus])`` oder None, wenn die Zuordnung
    nicht EINDEUTIG ist. Verwaist = auf eBay vorhanden, lokal nicht mehr (= geloescht).

    Bewusst streng: bei jeder Mehrdeutigkeit None. Eine geratene Zuordnung wuerde Preise
    und Bestand auf fremde Varianten schreiben – teurer als ein abgebrochener Abgleich.
    """
    if not live_options or not axis_names or not variants:
        return None
    all_names: set[str] = set().union(*[set(o.keys()) for o in live_options.values()])
    # Achsen-Kandidaten: Aspekte, die ueber die Live-SKUs variieren (konstante wie 'Marke' raus).
    varying = {n for n in all_names
               if len({_norm_val(o.get(n)) for o in live_options.values()}) > 1}
    axis_map: dict[str, str] = {}
    used: set[str] = set()
    for a in axis_names:
        # Unsere (nach dem Loeschen ggf. kleinere) Wertemenge muss TEILMENGE der live
        # gefuehrten Werte dieses Aspekts sein – und das darf nur auf einen Aspekt passen.
        want = {_norm_val((v.get("options") or {}).get(a)) for v in variants}
        hits = [n for n in varying if n not in used
                and want <= {_norm_val(o.get(n)) for o in live_options.values()}]
        if len(hits) != 1:
            return None
        axis_map[a] = hits[0]
        used.add(hits[0])
    # Live-SKU je Wertekombination. Doppelte Kombination -> nicht unterscheidbar -> Abbruch.
    by_combo: dict[tuple, str] = {}
    for sku, opts in live_options.items():
        combo = tuple(_norm_val(opts.get(axis_map[a])) for a in axis_names)
        if combo in by_combo:
            return None
        by_combo[combo] = sku
    bound: dict[str, str] = {}
    for v in variants:
        combo = tuple(_norm_val((v.get("options") or {}).get(a)) for a in axis_names)
        sku = by_combo.get(combo)
        if not sku or not v.get("attr"):
            return None          # lokale Variante live nicht wiederzufinden -> Abbruch
        bound[v["attr"]] = sku
    orphans = sorted(set(live_options) - set(bound.values()))
    return bound, orphans


async def bind_live_variant_skus(ebay, db: Session, listing, product) -> dict:
    """Echte eBay-SKUs von der Live-Gruppe holen und am Produkt festschreiben.

    Heilt Altbestand, der noch keine festgeschriebene ``ebay_sku`` hat, und deckt auf,
    welche Varianten nur noch auf eBay existieren (``orphans`` = lokal geloescht).
    Wirft ``PersistentError``, wenn die Zuordnung nicht eindeutig ist – dann wird NICHTS
    geaendert (weder lokal noch auf eBay).
    """
    axis_names, variants = _usable_variants(product)
    if not axis_names:
        raise PersistentError("Listing hat keine echten Varianten")
    live_options = await _live_sku_options(ebay, listing)
    if live_options is None:
        raise PersistentError(
            "Die Varianten dieses Artikels sind bei eBay gerade nicht vollstaendig lesbar – "
            "es wurde nichts geaendert. Bitte spaeter erneut versuchen.")
    matched = _match_variants_to_live_skus(axis_names, variants, live_options)
    if matched is None:
        raise PersistentError(
            "Die Varianten lassen sich nicht eindeutig zwischen Tool und eBay zuordnen – zur "
            "Sicherheit wurde nichts geaendert. Bitte die Variante direkt bei eBay entfernen.")
    bound, orphans = matched
    changed = _persist_variant_skus(product, bound)
    return {"bound": bound, "orphans": orphans, "persisted": changed,
            "live_count": len(live_options), "local_count": len(variants)}


def compute_variant_prices(listing, product, settings) -> list[dict]:
    """Je Variante eigener Verkaufspreis aus deren EK: [{sku, price_eur, options}].

    SKU-Zuordnung ueber ``variant_ebay_sku`` (festgeschriebene SKU, sonst V1..Vn).
    Leere Liste, wenn das Produkt keine echten Varianten hat.
    """
    axis_names, variants = _usable_variants(product)
    if not axis_names:
        return []
    base_sku = listing.ebay_sku or f"AE-{listing.id}"
    lmp = float(listing.min_profit_eur) if listing.min_profit_eur is not None else None
    fallback_ek = float(listing.cost_eur) if listing.cost_eur else None
    sov = _supplier_ship(listing)   # echte AliExpress-Versandkosten (statt Pauschale)
    # EU-Lager EINMAL bestimmen und an jede Variante weiterreichen. Ohne das bekam
    # jede Variante Zoll und China-Versand aufgeschlagen - und das sind die Preise,
    # die der Kaeufer je Groesse tatsaechlich zahlt (Fund 30.08.2026).
    from app.services.fast_shipping_service import variants_have_eu_warehouse
    _eu_lager = variants_have_eu_warehouse(product)
    # MANUELLE Preis-Overrides (per AE-sku_attr) haben VORRANG – so ueberleben die im
    # Preis-Editor gesetzten Preise ein Re-Publish (sonst rechnet der Publish sie neu).
    overrides = listing.variant_prices or {}
    cents = settings.price_cents or 0.95
    prices = []
    for i, v in enumerate(variants, 1):
        try:
            ek = (round(pricing.effective_cost(
                      v.get("price"), settings=settings, ship_override=sov,
                      local=_ae_api.has_eu_warehouse([v.get("ship_from")])), 2)
                  if v.get("price") is not None else fallback_ek)
        except (InvalidOperation, ValueError, TypeError):
            ek = fallback_ek
        attr = v.get("attr")
        ov = overrides.get(attr) if attr else None
        overridden = ov is not None
        price = (pricing.round_to_nearest_cents(float(ov), cents) if overridden
                 else _variant_price(v.get("price"), listing.price_eur, settings,
                                     min_profit_eur=lmp, ship_override=sov,
                                     category_name=listing.category_name,
                                     local=_eu_lager))
        opts = v.get("options") or {}
        prices.append({
            "sku": variant_ebay_sku(base_sku, v, i),
            "price_eur": price,
            "ek_eur": ek,
            "options": opts,
            # NUR die tatsaechlich auf eBay veroeffentlichten Achsen (axis_names) – exakt die
            # Werte-Menge, aus der `sync_ebay_live_prices` die Merkmals-Signatur ("sig:…") baut.
            # Der rohe options-Dict kann zusaetzliche, nicht publizierte Konstant-Achsen (z.B.
            # "Verpackung") enthalten; die wuerden die Signatur verfehlen (Fund Review 17.07.).
            "axis_options": {a: opts.get(a) for a in axis_names},
            "overridden": overridden,   # Premium-Regel nie ueber einen Nutzerpreis
        })
    _apply_premium_rule(prices, axis_names)
    return prices


def _stock_num(raw) -> tuple[bool, int]:
    """AliExpress-Bestandswert defensiv deuten: (bekannt?, wert).

    NUR echte Zahlenangaben gelten als BEKANNT. '', None, 'N/A', bool o.ae. = UNBEKANNT
    -> (False, 0). Damit wird ein fehlender/kaputter Bestandswert NIE als „ausverkauft"
    (Bestand 0) missdeutet – sonst wuerden Varianten faelschlich auf eBay auf 0 gesetzt."""
    if raw is None or isinstance(raw, bool):
        return (False, 0)
    if isinstance(raw, (int, float)):
        return (True, int(raw))
    s = str(raw).strip().replace(",", "").replace(" ", "")   # '1,000' / '1 000' -> 1000
    if not s:
        return (False, 0)
    try:
        return (True, int(float(s)))
    except (TypeError, ValueError):
        return (False, 0)


def _stock_int(raw) -> int:
    """Bestand als int (Fallback 0) – fuer Mengen-Pushes, wo unbekannt=0 unkritisch ist."""
    return _stock_num(raw)[1]


def variant_stock_state(listing, product) -> list[dict]:
    """Pro ECHTER Variante: {ebay_sku, attr, name, stock, in_stock, oos}. Basis fuer
    den Per-Varianten-Bestands-Sync (monitoring) und die Preis-Check-Anzeige.

    ebay_sku identisch zu compute_variant_prices/Publish (ueber ``variant_ebay_sku``). Leer,
    wenn kein echtes Multivarianten-Listing.

    KONSERVATIV: ``oos`` (ausverkauft) nur bei einem DEFINITIVEN Bestand <= 0. Fehlt der
    Bestandswert (None/unbekannt), gilt die Variante als lieferbar (in_stock=True) – so
    werden Varianten nie faelschlich wegen fehlender Daten auf 0 gesetzt (Umsatzschutz)."""
    axis_names, variants = _usable_variants(product)
    if not axis_names:
        return []
    base = listing.ebay_sku or f"AE-{listing.id}"
    out = []
    for i, v in enumerate(variants, 1):
        known, val = _stock_num(v.get("stock"))
        oos = known and val <= 0          # ausverkauft NUR bei definitivem Bestand <= 0
        out.append({
            "ebay_sku": variant_ebay_sku(base, v, i),
            "attr": v.get("attr"),
            "name": " / ".join(str(v.get("options", {}).get(a, "")) for a in axis_names),
            "stock": (val if known else None),
            "in_stock": not oos,
            "oos": oos,
        })
    return out


# Platzhalter-/Junk-Optionswerte, die KEINE echte Variante darstellen.
_JUNK_OPTION_VALUES = {
    "as picture shows", "as picture", "as pictures", "as shown", "as the picture",
    "picture", "as photo", "random", "default", "standard", "as description",
    "one size", "einheitsgröße", "einheitsgroesse", "n/a", "none", "-", "",
}


def _is_junk_option(v) -> bool:
    return str(v or "").strip().lower() in _JUNK_OPTION_VALUES


def _anderes_aktives_listing_mit_sku(db: Session, listing: Listing,
                                     base_sku: str) -> int | None:
    """ID eines ANDEREN aktiven Listings, das denselben SKU-Namensraum haelt.

    ``ebay_sku`` ist ``AE-<aliexpress_id>`` und damit je PRODUKT stabil - beim
    Relist tragen Alt-Listing und neuer Entwurf denselben Wert. Publiziert man
    den Entwurf, solange das Alte live ist, ueberschreiben die
    createOrReplace-Aufrufe dessen Varianten-Items und Offers: Bestand und
    Mengen des LIVE-Angebots werden ueberschrieben, und die publizierte Gruppe
    scheitert mit Fehler 25013 - halb umgebaut.

    Uebernommen aus dem Ursprungssystem (Vorfall dort am 22.08.2026, Listing
    1656); unsere Kopie hatte diese Sperre nicht.
    """
    if not base_sku:
        return None
    andere = db.scalars(select(Listing).where(
        Listing.ebay_sku == base_sku,
        Listing.id != listing.id,
        Listing.listing_status == "active")).all()
    for a in andere:
        if a.ebay_item_id:
            return a.id
    return None


def _aspekte_fuer_varianten(base: dict, product: Product | None,
                            axis_names: list[str]) -> dict:
    """Merkmale am ANGEBOT sauber von den Merkmalen der VARIANTEN trennen.

    eBay kennt zwei Sorten: was fuer das ganze Angebot gilt, steht einmal am
    Artikel; was sich je Variante unterscheidet, steht in den Varianten. Wird das
    vermischt, geht beides schief - und genau das war hier der Fall:

    1. ``Größe`` stand als EIN Artikelmerkmal drin, mit dem Wert
       ``"S, M, L, XL, XXL, XXXL, 4XL, 5XL"``. Das ist fuer eBay ein Eigenwert
       fuer die Groesse, und Eigenwerte sind in dieser Kategorie verboten ->
       Fehler 25129, das Angebot wurde abgelehnt. Eine variierende Achse gehoert
       NICHT an den Artikel.

    2. ``Farbe`` fehlte ganz, obwohl eBay sie als Pflichtmerkmal fuehrt. Alle
       dreissig Artikel haben genau eine Farbe (Schwarz), weshalb sie zu Recht
       keine Auswahl-Achse ist - aber gesagt werden muss sie trotzdem. Fehlt sie,
       ergaenzt eBay sie selbst, und der Kaeufer bekommt eine Auswahlliste mit
       einem einzigen Eintrag vorgesetzt.

       Nutzerbericht vom 03.09.2026: "bei den meisten artikeln ist die farbe
       allerdings schwarz und es gibt gar keine moeglichkeit eine andere farbe
       auszuwaehlen, deshalb soll farbe garnicht anklickbar sein".

    Bei einem Einzelartikel ohne Varianten (``axis_names`` leer) bleibt alles wie
    es ist - dort gibt es nichts zu trennen.
    """
    if not axis_names:
        return base

    daten = getattr(product, "variants", None) if product is not None else None
    sauber = dict(base)

    # 1. Variierende Achsen raus - sie stehen gleich in den Varianten.
    for achse in axis_names:
        sauber.pop(achse, None)

    if not isinstance(daten, dict):
        return sauber

    # 2. Einwertige Achsen rein, mit ihrem einen Wert.
    werte: dict[str, set[str]] = {}
    for sku in daten.get("skus") or []:
        for name, wert in (sku.get("options") or {}).items():
            if name in axis_names:
                continue
            text = str(wert or "").strip()
            if text and not _is_junk_option(text):
                werte.setdefault(name, set()).add(text)

    for name, menge in werte.items():
        if len(menge) == 1 and name not in sauber:
            sauber[name] = next(iter(menge))
    return sauber


def _usable_variants(product: Product | None) -> tuple[list[str], list[dict]]:
    """(Achsennamen, distinkte Varianten) für ein ECHTES Multivarianten-Listing; sonst ([],[]).

    Filtert Platzhalter-Werte ("as picture shows"), nutzt nur Achsen mit ≥2 echten
    Werten und dedupliziert auf eindeutige Kombinationen. So werden Pseudo-Varianten
    (alle gleich) korrekt als Einzel-Listing behandelt (verhindert eBay-Fehler 25013).
    """
    variants_data = (product.variants or {}) if product else {}
    if not isinstance(variants_data, dict):
        return [], []
    skus = [v for v in (variants_data.get("skus") or []) if v.get("options")]
    axes = variants_data.get("axes") or {}
    if len(skus) < 2 or not axes:
        return [], []
    # Achsen: von jeder Variante geführt UND ≥2 distinkte, nicht-junk Werte
    axis_names = []
    for a in axes:
        if not all(a in v["options"] for v in skus):
            continue
        real_vals = {str(v["options"][a]).strip() for v in skus if not _is_junk_option(v["options"].get(a))}
        if len(real_vals) >= 2:
            axis_names.append(a)
    if not axis_names:
        return [], []
    # Auf eindeutige Achsen-Kombinationen deduplizieren; Junk-Kombis überspringen.
    #
    # Zurueck kommen die ORIGINAL-Datensaetze, nicht Kopien. Das ist wichtig:
    # ``append_corrected_variants`` schreibt die vergebene eBay-SKU in genau
    # diese Objekte zurueck. Ein Umbau auf Kopien liess diese Rueckschreibung
    # am 03.09.2026 ins Leere laufen - die SKUs waren danach still None.
    # Die Groessen-Umwandlung sitzt deshalb weiter unten, kurz vor dem Bau der
    # eBay-Daten, wo ohnehin mit Kopien gearbeitet wird.
    seen: set = set()
    uniq: list[dict] = []
    for v in skus:
        combo = tuple(str(v["options"].get(a, "")).strip() for a in axis_names)
        if any(_is_junk_option(x) for x in combo) or combo in seen:
            continue
        seen.add(combo)
        uniq.append(v)
    if len(uniq) < 2:
        return [], []
    return axis_names, uniq


def _normalisiere_groessen(axis_names: list[str], variants: list[dict]) -> list[dict]:
    """Groessenwerte auf eBays Schreibweise bringen - auf KOPIEN.

    Ohne das scheitert die Veroeffentlichung an eBay-Fehler 25129: die Kategorie
    erlaubt keine Eigenwerte fuer "Groesse", und ``XXL``/``XXXL`` stehen nicht in
    ihrer Liste - dort heissen sie ``2XL`` und ``3XL``. Am 03.09.2026 hing genau
    daran der Live-Gang zweier Entwuerfe.

    Kopien, weil ``v["options"]`` zum Produktdatensatz in der Datenbank gehoert.
    Ein Ueberschreiben wuerde die Lieferantendaten veraendern und die Zuordnung
    zur Quelle zerstoeren - die Bestellabwicklung findet die SKU dann nicht mehr.
    """
    from app.services import groessen

    if not any(groessen.ist_groessen_achse(a) for a in axis_names):
        return variants
    fertig = []
    for v in variants:
        opts = v.get("options") or {}
        fertig.append({**v, "options": {
            k: groessen.normalisiere_achse(k, val) for k, val in opts.items()
        }})
    return fertig


async def publish_listing_live(db: Session, *, listing_id: int, draft_only: bool = False,
                               force: bool = False) -> dict:
    """Listing auf eBay anlegen. draft_only=True -> als unveröffentlichter eBay-ENTWURF
    (Inventory-Item + Offer, aber KEIN publishOffer). Sonst live veröffentlichen.
    Idempotent, wenn bereits live – ausser force=True: dann wird ein bereits
    veroeffentlichtes Listing NEU aufgebaut (gleiches Item), z.B. um Variantenbilder/
    -preise zu korrigieren (createOrReplace + publishOfferByInventoryItemGroup = In-Place-Update)."""
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    if listing.ebay_item_id and not force:
        return {"listing_id": listing.id, "ebay_item_id": listing.ebay_item_id,
                "url": f"https://www.ebay.de/itm/{listing.ebay_item_id}",
                "status": "active", "already_live": True}

    product = db.get(Product, listing.product_id) if listing.product_id else None
    images = product.images if product and product.images else []
    if not images:
        raise PersistentError("Listing hat keine Bilder – eBay verlangt mindestens eines.")

    settings = get_settings()
    ebay = _real_ebay()
    base_sku = listing.ebay_sku or f"AE-{listing.id}"

    # SKU-NAMENSRAUM-SPERRE: Teilen sich zwei Listings dieselbe ebay_sku
    # (AE-<aliexpress_id>, je Produkt stabil), wuerde das Veroeffentlichen des
    # neuen die Inventory-Items und Offers des LIVE-Listings ueberschreiben -
    # dessen Bestand und Mengen waeren weg, und die publizierte Gruppe scheitert
    # mit 25013, halb umgebaut. Deshalb VOR jedem eBay-Aufruf abbrechen.
    # Uebernommen aus dem Ursprungssystem (Vorfall dort 22.08.2026).
    anderes = _anderes_aktives_listing_mit_sku(db, listing, base_sku)
    if anderes is not None:
        raise PersistentError(
            f"SKU-Namensraum {base_sku} gehoert noch dem AKTIVEN Listing "
            f"{anderes} - erst dieses beenden (Relist-Reihenfolge: beenden, "
            f"dann neu veroeffentlichen), sonst wird dessen Live-Bestand "
            f"ueberschrieben.")

    with task_log(db, task_type=("ebay_draft" if draft_only else "golive"), reference_id=listing_id) as tl:
        # Kategorie-Kandidaten: gespeicherte zuerst, dann eBay-Vorschläge (Fallback bei 25005,
        # wenn eBay eine Kategorie vorschlägt, die keine Angebote erlaubt).
        candidates = [listing.category_id] if listing.category_id else []
        for c in await ebay.suggest_categories(listing.title_seo):
            if c not in candidates:
                candidates.append(c)
        if not candidates:
            # Taxonomy zickt gelegentlich (Transient/Sonderzeichen) -> Retry mit
            # vereinfachtem Titel. NIE Kategorie "0" senden (garantiert 25002).
            simple = re.sub(r"[^\w\säöüÄÖÜß]", " ", listing.title_seo or "")[:60].strip()
            for c in await ebay.suggest_categories(simple):
                if c not in candidates:
                    candidates.append(c)
        if not candidates:
            raise PersistentError(
                "Keine eBay-Kategorie ermittelbar (Vorschlagsdienst nicht erreichbar) – "
                "automatischer Retry folgt")
        from app.spec_filter import strip_forbidden_specs
        base = strip_forbidden_specs(dict(listing.item_specifics or {})) or {"Marke": "Markenlos"}
        axis_names, variants = _usable_variants(product)
        base = _aspekte_fuer_varianten(base, product, axis_names)

        from app.retry import retry_async
        item_id = mode = None
        last_exc: Exception | None = None
        extra_aspects: dict = {}   # selbst geheilte Pflicht-Merkmale (z.B. Hersteller)
        done = False
        for ci, category in enumerate(candidates):
            aspect_tries = 0
            while True:
                aspects = strip_forbidden_specs(_truncate_aspects(
                    await ebay.build_aspects(category, {**base, **extra_aspects})))
                brand = _brand_from(aspects)
                try:
                    if axis_names:
                        item_id, mode = await retry_async(
                            lambda cat=category, asp=aspects, br=brand: _publish_multi(
                                ebay, listing, product, category=cat, aspects=asp,
                                brand=br, axis_names=axis_names, variants=variants,
                                base_sku=base_sku, settings=settings, draft_only=draft_only),
                            label="publish", max_retries=2)
                    else:
                        item_id, mode = await retry_async(
                            lambda cat=category, asp=aspects, br=brand: _publish_single(
                                ebay, listing, category=cat, aspects=asp, brand=br,
                                images=images, base_sku=base_sku, settings=settings,
                                draft_only=draft_only),
                            label="publish", max_retries=2)
                    done = True
                    break
                except PersistentError as exc:
                    msg = str(exc)
                    # Selbstheilung: "Artikelmerkmal X fehlt" -> Merkmal setzen + sofort erneut
                    m = re.search(r"Artikelmerkmal\s+(.+?)\s+fehlt", msg)
                    if m and aspect_tries < 3:
                        name = m.group(1).strip()
                        extra_aspects[name] = brand if name.lower() in ("hersteller", "marke") else "Sonstige"
                        aspect_tries += 1
                        logger.warning("Pflicht-Merkmal '%s' fehlte – gesetzt auf '%s', Retry",
                                       name, extra_aspects[name])
                        continue
                    # 25005 = Kategorie erlaubt keine Angebote -> nächster Vorschlag
                    if "25005" in msg and ci + 1 < len(candidates):
                        logger.warning("Kategorie %s ungültig (25005) – nächster Vorschlag", category)
                        last_exc = exc
                        break
                    raise
            if done:
                break
        if not done:
            raise last_exc or PersistentError("Keine gültige eBay-Kategorie gefunden")

        listing.category_id = category
        if draft_only:
            listing.listing_status = "draft"
            tl.result_data = {"draft": listing.ebay_draft_id, "category": category, "mode": mode}
            db.commit()
            return {"listing_id": listing.id, "ebay_item_id": None, "status": "draft",
                    "ebay_draft": listing.ebay_draft_id, "mode": mode}

        _absorb_item_twin(db, listing, item_id)
        listing.ebay_item_id = item_id
        listing.listing_status = "active"
        # ECHTES Online-Datum stempeln (jetzt geht es live) – NUR wenn noch keins da ist.
        # So kennt der Optimier-Filter das wahre Alter, statt aufs alte Entwurfsdatum
        # (created_at) zurueckzufallen (sonst erscheinen frisch gelistete Artikel zu frueh).
        if listing.listing_start_date is None:
            from datetime import datetime, timezone
            listing.listing_start_date = datetime.now(timezone.utc)
        # Queue-Status an der Quelle abraeumen — auch wenn der Erfolg NICHT ueber
        # die Queue kam (Retry-Job), verschwindet ein alter Upload-Fehler im UI.
        listing.publish_queued = False
        listing.publish_error = None
        tl.result_data = {"ebay_item_id": item_id, "category": category, "mode": mode}
        db.commit()

    # Auto-Mengenrabatt (Nutzer-Entscheid 08.08.): beim Livegang aktivieren, wenn die
    # Marge eine Staffel traegt. Best effort — "lohnt sich nicht" (ValueError) ist der
    # Normalfall und still; echte Fehler nur loggen, nie den Publish brechen.
    if get_settings().multibuy_auto_activate_on_publish:
        try:
            from app.services import optimization_service as _opt
            vp = await _opt.activate_volume_pricing(db, listing_id=listing.id)
            logger.info("auto volume pricing aktiviert", extra={
                "listing_id": listing.id, "promotion": vp.get("promotion_id")})
        except ValueError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto volume pricing fehlgeschlagen: %s", str(exc)[:150])

    # Standard-Anzeigentarif (Promoted Listings, 10%) automatisch setzen –
    # best effort: darf das Publish nie brechen (z.B. solange sell.marketing fehlt).
    promoted = False
    try:
        ad = await ebay.promote_listings([item_id])
        promoted = ad.get("created", 0) + ad.get("already", 0) > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("Promoted-Listing (10%%) fuer %s nicht gesetzt: %s", item_id, str(exc)[:200])

    # eBay-Varianten-Bilder fuer die Orders-Vorschau cachen (best effort, read-only).
    try:
        await pull_ebay_variant_images(db, listing_id=listing.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("variant-image pull (%s) failed: %s", listing.id, str(exc)[:120])

    return {"listing_id": listing.id, "ebay_item_id": item_id,
            "url": f"https://www.ebay.de/itm/{item_id}", "status": "active",
            "already_live": False, "mode": mode, "promoted": promoted}


def _first_aspect_val(val) -> str:
    """Erster Wert eines eBay-Aspektfelds (Liste ODER Skalar) als String."""
    if isinstance(val, list):
        return str(val[0]) if val else ""
    return str(val if val is not None else "")


def _match_axis_map(local_axis_names: list[str], variants: list[dict],
                    live_seqs: dict[str, list[str]]) -> dict[str, str] | None:
    """Lokale AE-Achsen den LIVE-Aspektnamen zuordnen ueber die Werte-Reihenfolge.

    ``live_seqs`` = {live_aspektname: [wert_v1, wert_v2, ...]} in V-SKU-Reihenfolge.
    Eine lokale Achse wird dem Live-Aspekt zugeordnet, dessen Wertefolge EXAKT der
    Achsen-Wertfolge entspricht (Achsenwerte sind >=2 distinkt -> kollidieren nie mit
    konstanten Shared-Aspekten wie 'Marke'). Nur wenn ALLE Achsen eindeutig getroffen
    werden, kommt eine Map zurueck – sonst None (Aufrufer nutzt den alten Pfad).
    """
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for a in local_axis_names:
        target = [str((v.get("options") or {}).get(a, "")) for v in variants]
        hit = next((name for name, seq in live_seqs.items()
                    if name not in used and seq == target), None)
        if hit is None:
            return None
        mapping[a] = hit
        used.add(hit)
    return mapping


async def _live_axis_map(ebay, listing, local_axis_names: list[str],
                         variants: list[dict]) -> dict[str, str] | None:
    """AE-Achsen -> die Namen, die das Listing SCHON LIVE auf eBay traegt.

    Quelle 1 (massgeblich): die Inventory-ITEMS (product.aspects). Das ist, wogegen eBay
    die Variationen matcht, und die Items werden bei Updates NIE umbenannt – deshalb sind
    sie die stabile Wahrheit. Quelle 2 (Fallback): die variesBy.specifications der Live-Gruppe.

    So bleiben Item- und Gruppen-Achsen konsistent (kein erneutes, nicht-deterministisches
    _variation_safe_axis_map, das bei Taxonomy-Ausfall die Identity-Map liefert und damit
    Achsen auseinanderdriften laesst – Bug Listing 426) UND eine bereits gedriftete Gruppe
    wird auf die Item-Wahrheit zurueckgezogen. Liefert eine local->live Map oder None.
    """
    base_sku = listing.ebay_sku or f"AE-{listing.id}"
    # --- Quelle 1: Inventory-Items ---
    try:
        item_aspects: list[dict] = []
        # Je Variante ihre EIGENE SKU lesen (festgeschrieben, sonst positionsbasiert). Stur
        # V1..Vn zu lesen liefert nach einer geloeschten Variante die Aspekte FREMDER
        # Varianten – die Wertefolge passt dann zu keiner Achse und die Map faellt aus.
        for i, v in enumerate(variants, 1):
            item = await ebay.get_inventory_item(variant_ebay_sku(base_sku, v, i))
            item_aspects.append(((item or {}).get("product") or {}).get("aspects") or {})
        if item_aspects and all(item_aspects):
            names: set[str] = set().union(*[set(a.keys()) for a in item_aspects])
            seqs = {name: [_first_aspect_val(a.get(name)) for a in item_aspects] for name in names}
            m = _match_axis_map(local_axis_names, variants, seqs)
            if m:
                return m
    except Exception as exc:  # noqa: BLE001 – nicht lesbar -> naechste Quelle
        logger.debug("live item aspects nicht lesbar: %s", str(exc)[:120])
    # --- Quelle 2: variesBy.specifications der Live-Gruppe (Werte-Menge) ---
    try:
        gk = listing.ebay_draft_id or ""
        grp = await ebay.get_inventory_item_group(gk) if gk.endswith("-GRP") else None
        specs = ((grp or {}).get("variesBy") or {}).get("specifications") or []
        by_valueset = {s["name"]: {str(x) for x in (s.get("values") or [])}
                       for s in specs if s.get("name")}
        if by_valueset:
            mapping: dict[str, str] = {}
            used: set[str] = set()
            for a in local_axis_names:
                want = {str((v.get("options") or {}).get(a, "")) for v in variants}
                hit = next((nm for nm, vs in by_valueset.items()
                            if nm not in used and vs == want), None)
                if hit is None:
                    return None
                mapping[a] = hit
                used.add(hit)
            return mapping or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("live group specs nicht lesbar: %s", str(exc)[:120])
    return None


def _publish_base_sku(listing) -> str:
    """Publish-Basis-SKU eines Multivarianten-Listings: die echten Inventory-Items heißen
    {base}-V{i}. Der zuverlässige base steckt in ebay_draft_id ({base}-GRP) – ``ebay_sku`` kann
    vom eBay-Import auf den GRUPPEN-Key ('…-GRP') verbogen sein (SKU-Falle, Vorfall 29.07.:
    Gruppen-Update baute '…-GRP-V1' -> eBay 25701 „Bestandseinheit nicht gefunden")."""
    gk = listing.ebay_draft_id or ""
    if gk.endswith("-GRP"):
        return gk[:-4]
    base = listing.ebay_sku or f"AE-{listing.id}"
    return base[:-4] if base.endswith("-GRP") else base


async def _push_group_update(ebay, listing, db, *, title: str | None = None,
                             desc_html: str | None = None, aspects: dict | None = None) -> bool:
    """Inventory-Item-Group aus lokalen Daten neu schreiben (Titel/Beschreibung-Update).

    Voll-ersetzend, identisch zum Publish-Aufbau (variesBy aus product.variants),
    damit Umlaut-Achsen ('Länge') nicht am GET-Encoding scheitern.
    """
    product = db.get(Product, listing.product_id) if listing.product_id else None
    axis_names, variants = _usable_variants(product)
    if not axis_names:
        return False
    orig_axis_names = list(axis_names)
    # Bei einem LIVE-Listing die bereits live GETRAGENEN Achsennamen wiederverwenden (aus den
    # Inventory-Items, Fallback Gruppe) statt sie ueber _variation_safe_axis_map NEU – und
    # nicht-deterministisch (Taxonomy-Ausfall -> Identity) – zu raten. Sonst driften Item- und
    # Gruppen-Achsen auseinander und eBay zeigt fuer alle Varianten dasselbe Bild (Listing 426).
    axis_map = None
    if listing.ebay_item_id:
        axis_map = await _live_axis_map(ebay, listing, axis_names, variants)
    if axis_map is None:
        # Entwurf/keine Live-Achsen lesbar: wie beim Publish je Kategorie umbenennen.
        axis_map = await _variation_safe_axis_map(ebay, listing.category_id or "0", axis_names)
    if any(axis_map.get(a, a) != a for a in axis_names):
        axis_names, variants = _rename_axes(axis_names, variants, axis_map)
    # Unbedingt, nicht nur beim Umbenennen: die Groessen muessen in JEDEM Fall
    # in eBays Schreibweise raus, sonst Fehler 25129.
    variants = _normalisiere_groessen(axis_names, variants)
    base_sku = _publish_base_sku(listing)
    color_axis = _image_axis(orig_axis_names, axis_names, axis_map, variants)
    used: dict[str, list[str]] = {a: [] for a in axis_names}
    variant_skus: list[str] = []
    all_have_image = bool(color_axis)
    for i, v in enumerate(variants, 1):
        # Festgeschriebene SKU bevorzugen (_rename_axes kopiert sie mit). Nur so bleibt die
        # Gruppe nach einer geloeschten Variante auf DIESELBEN eBay-Items gerichtet, statt
        # die uebrigen um eine Nummer nach vorn zu schieben.
        variant_skus.append(variant_ebay_sku(base_sku, v, i))
        for a in axis_names:
            val = v["options"][a]
            if val not in used[a]:
                used[a].append(val)
        if color_axis and not v.get("image"):
            all_have_image = False
    # Groessen klein nach gross. Gesammelt werden sie in der Reihenfolge, in der
    # AliExpress die Varianten liefert - und die ist beliebig ("4XL, 5XL, S, M,
    # XXL, ..."). Genau so stand es dann im Ausklapper beim Kaeufer.
    # Nutzerwunsch vom 03.09.2026: die Reihenfolge zaehlt beim Bestellen.
    from app.services import groessen as _groessen
    specs = [{"name": a,
              "values": (_groessen.sortiere_sicher(used[a])
                         if _groessen.ist_groessen_achse(a) else used[a])}
             for a in axis_names if used[a]]
    # Wie beim Publish: Pflicht-Merkmale der Kategorie auto-fuellen (sonst 25002). WICHTIG: die
    # ANGEFRAGTEN (korrigierten) Merkmale nutzen, wenn übergeben – ``listing.item_specifics`` trägt an
    # dieser Stelle noch den ALTEN Wert (``_apply_local`` läuft erst NACH dem Push), sonst würde die
    # Gruppe die alte (falsche) Material-Angabe erneut veröffentlichen.
    base_aspects = dict((aspects if aspects is not None else (listing.item_specifics or {}))
                        or {"Marke": "Markenlos"})
    filled = _truncate_aspects(await ebay.build_aspects(listing.category_id or "0", base_aspects))
    non_axis = {k: v for k, v in filled.items() if k not in axis_names}
    await ebay.create_inventory_item_group(
        listing.ebay_draft_id,
        title=(title or listing.title_seo),
        description=(desc_html or _html_description(listing.description or listing.title_seo)),
        image_urls=gallery_images(product),   # Standard + Variantenbilder
        variant_skus=variant_skus, specifications=specs,
        image_varies_by=([color_axis] if (color_axis and all_have_image) else None),
        aspects=(non_axis or None))
    return True


async def _listing_variant_skus(ebay, listing) -> list[str]:
    """SKUs, die zu einem Listing gehoeren (Einzel = Basis-SKU; Multi = alle Varianten-SKUs)."""
    base = listing.ebay_sku or f"AE-{listing.id}"
    gk = listing.ebay_draft_id or ""
    if gk.endswith("-GRP"):
        grp = await ebay.get_inventory_item_group(gk)
        skus = (grp or {}).get("variantSKUs") or []
        return skus or [base]
    return [base]


async def pull_ebay_variant_images(db: Session, *, listing_id: int) -> dict:
    """Varianten-Bilder des EIGENEN eBay-Listings holen und am Listing speichern.

    Fuer die Orders-Vorschau: der Nutzer will SEIN eBay-Varianten-Bild sehen – die eBay-
    Auswahlwerte ("Only Goggles (365458)") sind ohne Bild nicht zuordenbar (Vorfall
    Motocross 11.07.). Je Varianten-SKU wird das Inventory-Item gelesen (variierende
    Aspekte + imageUrls) und unter dem stabilen ``variant_map_key`` der eBay-Auswahl
    abgelegt. Read-only bei eBay, kein Geld-Call; Netz-Calls laufen VOR dem kurzen Commit.
    """
    from app.services.order_service import variant_map_key
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    gk = listing.ebay_draft_id or ""
    if not gk.endswith("-GRP"):
        # IMPORTIERTES (Trading-)Listing ohne Inventory-Gruppe (z.B. Motocross-Brille):
        # Varianten-Bilder kommen aus GetItem -> VariationSpecificPictureSet.
        if listing.ebay_item_id:
            ebay = _real_ebay()
            vp = await ebay.get_item_variation_pictures(listing.ebay_item_id)
            t_map = {variant_map_key({vp.get("axis_name") or "Variante": val}): url
                     for val, url in (vp.get("values") or {}).items()}
            t_map = {k: v for k, v in t_map.items() if k}
            if t_map:
                listing.ebay_variant_images = t_map
                flag_modified(listing, "ebay_variant_images")
                db.commit()
            return {"listing_id": listing_id, "variants": len(vp.get("values") or {}),
                    "images": len(t_map), "note": "trading"}
        return {"listing_id": listing_id, "variants": 0, "images": 0,
                "note": "kein Multivarianten-Listing"}
    ebay = _real_ebay()
    grp = await ebay.get_inventory_item_group(gk) or {}
    skus = grp.get("variantSKUs") or []
    # Nur die VARIIERENDEN Aspekte keyen (nicht alle Artikelmerkmale) – das entspricht
    # exakt den variationAspects, die eBay an Verkaeufen mitliefert.
    vary_names = {s.get("name") for s in ((grp.get("variesBy") or {}).get("specifications") or [])
                  if s.get("name")}
    mapping: dict[str, str] = {}
    for sku in skus[:80]:
        try:
            item = await ebay.get_inventory_item(sku)
        except Exception:  # noqa: BLE001 – einzelne kaputte SKU bricht den Rest nicht
            continue
        prod = (item or {}).get("product") or {}
        aspects = prod.get("aspects") or {}
        imgs = prod.get("imageUrls") or []
        if not aspects or not imgs:
            continue
        sel = {a: (v[0] if isinstance(v, list) and v else v)
               for a, v in aspects.items() if (not vary_names or a in vary_names)}
        key = variant_map_key(sel)
        if key:
            mapping[key] = imgs[0]
    if mapping:
        listing.ebay_variant_images = mapping
        flag_modified(listing, "ebay_variant_images")
        db.commit()
    return {"listing_id": listing_id, "variants": len(skus), "images": len(mapping)}


async def _listing_has_multiple_variants(ebay, listing, product: "Product | None") -> bool | None:
    """Hat dieses Live-Listing MEHRERE echte Varianten (mit je eigener Preis-Staffel)?

    Ein Listing-EINZELPREIS (POST /{id}/edit, z.B. „🔻 Senken“ im Optimieren-Tab) darf ein
    solches Listing NIE flach auf EINEN Preis setzen – dafuer ist der per-Varianten-Dialog
    („Preise anpassen“, apply_variant_prices) zustaendig. Maßgeblich ist die ECHTE eBay-
    Struktur:
      1) native Inventar-Gruppe (``ebay_draft_id`` endet auf ``-GRP``) -> immer multi;
      2) sonst live -> GetItem: >= 2 Variationen = multi (deckt auch klassisch/importierte
         Listings ab, genau den Fall, den ``revise_item_price_smart`` flach setzen wuerde);
      3) der PRODUKT-Hinweis (>= 2 echte Achsen-Varianten) sperrt IMMER zusaetzlich – auch
         wenn GetItem faelschlich < 2 Variationen meldet (Under-Report / eBay-Eventual-
         Consistency). Sonst koennte ``revise_item_price_smart`` mit seinem EIGENEN GetItem
         doch noch die volle Staffel flach setzen. Faellt GetItem aus UND fehlt der Hinweis,
         -> None (unklar).
    Rueckgabe: True = multi, False = Einzel-Listing, None = eBay nicht pruefbar
    (Aufrufer entscheidet fail-closed: lieber verweigern als blind flach setzen).
    """
    if (listing.ebay_draft_id or "").endswith("-GRP"):
        return True
    axis_names, uvars = _usable_variants(product)
    product_multi = bool(axis_names) and len(uvars) >= 2   # kennt das Produkt echte Varianten?
    if not listing.ebay_item_id:
        return product_multi   # Entwurf/nicht live -> nur der Produkt-Hinweis (kein Live-Push)
    try:
        info = await ebay.get_item_price_info(listing.ebay_item_id)
    except Exception:  # noqa: BLE001 – GetItem aus -> Produkt-Hinweis, sonst unklar (fail-closed)
        return True if product_multi else None
    # GetItem ist maßgeblich; der Produkt-Hinweis darf ZUSAETZLICH sperren, nie flach setzen.
    return (len(info.get("variations") or []) >= 2) or product_multi


async def update_listing_live(db: Session, *, listing_id: int, title: str | None = None,
                              description: str | None = None,
                              item_specifics: dict | None = None,
                              price_eur: float | None = None) -> dict:
    """Listing aendern und – falls live – ZUERST auf eBay pushen, DANN lokal speichern.

    Preis -> Bulk-Offer-Update (native) oder Varianten-faehiges Trading-Revise
    (importierte Listings). Nach jedem Preis-Push wird der Live-Preis von eBay
    ZURUECKGELESEN und verglichen: nur ein verifizierter Push gilt als Erfolg.
    Schlaegt der Push/die Verifikation fehl, bleibt die DB UNVERAENDERT (kein
    'System sagt ok, eBay stimmt nicht').
    """
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    # SCHUTZ (Vorfall 30.07.): NIE einen Platzhalter als Beschreibung auf eBay pushen. Importierte
    # Listings tragen intern nur "(von eBay importiert)" – ein Push damit ZERSTOERT die echte
    # Live-Beschreibung (5 Listings betroffen, per LLM neu erstellt). Stub -> Beschreibung ignorieren.
    if description is not None:
        _d = str(description).strip().lower()
        if not _d or "(von ebay importiert" in _d or _d == "(importiert)":
            description = None

    def _apply_local() -> list[str]:
        """Felder erst hier setzen – bei Live-Listings NACH erfolgreichem Push."""
        out: list[str] = []
        if title is not None:
            listing.title_seo = title[:80]; out.append("Titel")
        if description is not None:
            listing.description = description; out.append("Beschreibung")
        if item_specifics is not None:
            listing.item_specifics = item_specifics or None; out.append("Merkmale")
        if price_eur is not None:
            listing.price_eur = round(float(price_eur), 2); out.append("Preis")
        return out

    changed = [lbl for val, lbl in ((title, "Titel"), (description, "Beschreibung"),
                                    (item_specifics, "Merkmale"), (price_eur, "Preis"))
               if val is not None]
    if not (listing.ebay_item_id and changed):
        changed = _apply_local()
        db.commit()   # Entwurf/lokal: direkt speichern
        return {"listing_id": listing.id, "changed": changed, "pushed_to_ebay": False,
                "warnings": []}

    # WICHTIG: Bei Live-Listings werden die DB-Felder ERST NACH erfolgreichem,
    # VERIFIZIERTEM eBay-Push gesetzt (task_log committet zwischendurch – vorher
    # gesetzte Felder wuerden auch bei Fehlern persistiert -> DB luegt).
    pushed = False
    price_verified = None
    warnings: list[str] = []
    if changed:   # live + Aenderungen -> eBay-Push mit Read-back-Verifikation
        ebay = _real_ebay()
        with task_log(db, task_type="update_listing", reference_id=listing_id) as tl:
            skus = await _listing_variant_skus(ebay, listing)
            is_multi = (listing.ebay_draft_id or "").endswith("-GRP")
            product = db.get(Product, listing.product_id) if listing.product_id else None
            from app.spec_filter import strip_forbidden_specs
            desc_html = _html_description(description) if description is not None else None
            aspects = _truncate_aspects(strip_forbidden_specs(item_specifics)) if item_specifics else None
            # SCHUTZ: Achsen-Merkmale (z.B. Metallfarbe/Länge) NIE auf Varianten mergen –
            # sonst ueberschreibt 'Gold, Silber' den Einzelwert 'Gold' -> eBay 25013.
            if aspects and is_multi:
                axis_names, _ = _usable_variants(product)
                aspects = {k: v for k, v in aspects.items() if k not in axis_names}

            # PREIS: dedizierte Bulk-Preis-API (keine Voll-Revalidierung -> robust).
            # Importierte (AutoDS-)Listings haben KEIN Inventory-Offer -> Trading-API,
            # Varianten-faehig (eBay 21916736 verlangt SKUs je Variation).
            if price_eur is not None:
                target = round(float(price_eur), 2)
                # SCHUTZ (Fund 18.07., analog Commit 376a167): Ein Listing-EINZELPREIS darf ein
                # ECHTES Multivarianten-Listing NIE flach setzen – weder via bulk_update_price
                # (alle Offers auf denselben Preis) noch via revise_item_price_smart (alle
                # Variationen auf EINEN StartPrice). Solche Listings haben je Variante eine eigene
                # Preis-Staffel -> hier verweigern und auf den per-Varianten-Dialog „Preise
                # anpassen“ verweisen. Nur ein ECHTES Einzel-Listing darf hier durch.
                multi = await _listing_has_multiple_variants(ebay, listing, product)
                if multi:
                    raise PersistentError(
                        "Dieses Listing hat mehrere Varianten mit je eigenem Preis. Ein einzelner "
                        "Preis würde ALLE Varianten auf denselben Wert setzen und die Preis-"
                        "Staffelung zerstören. Bitte den Preis je Variante über „Preise anpassen“ "
                        "im Listing-Cockpit ändern.")
                if multi is None:
                    raise PersistentError(
                        "Die Varianten dieses Listings lassen sich gerade nicht bei eBay prüfen "
                        "– bitte später erneut versuchen oder den Preis je Variante über „Preise "
                        "anpassen“ im Listing-Cockpit setzen.")
                updates = []
                for sku in skus:
                    offer = await ebay._first_offer_for_sku(sku)
                    if offer and offer.get("offerId"):
                        updates.append({"sku": sku, "offer_id": offer["offerId"],
                                        "price_eur": target})
                if updates:
                    await ebay.bulk_update_price(updates)
                else:
                    # Kein Inventory-Offer in UNSEREM Namespace -> Trading-API versuchen.
                    # AutoDS-/fremd-erstellte (warenbestandsbasierte) Listings lehnt eBay ab
                    # -> klare, handlungsleitende Meldung statt kryptischem Roh-Fehler.
                    try:
                        await ebay.revise_item_price_smart(listing.ebay_item_id, target)
                    except Exception as exc:  # noqa: BLE001
                        _m = str(exc).lower()
                        if ("warenbestandsbasiert" in _m or "nicht unterstützt" in _m
                                or "nicht unterstuetzt" in _m or "erstellt haben" in _m):
                            raise PersistentError(
                                "Dieses Listing wurde außerhalb dieses Tools erstellt (z.B. "
                                "AutoDS) und ist bei eBay warenbestandsbasiert – der Preis "
                                "lässt sich darüber nicht ändern. Optionen: Preis direkt bei "
                                "eBay im Seller Hub anpassen, oder das Listing hier nativ neu "
                                "veröffentlichen (dann voll editierbar).")
                        raise
                # READ-BACK: eBay nach dem Ist-Preis fragen – nur verifiziert = Erfolg.
                price_verified = await ebay.verify_item_price(listing.ebay_item_id, target)
                if not price_verified:
                    raise PersistentError(
                        f"Preis-Push NICHT wirksam: eBay zeigt weiterhin einen anderen "
                        f"Preis als {target:.2f}€ (Read-back-Verifikation fehlgeschlagen)")

            # TITEL/BESCHREIBUNG/MERKMALE:
            content_changed = title is not None or desc_html is not None or bool(aspects)
            if is_multi:
                # eBay rendert Titel/Beschreibung der GRUPPE -> Gruppe ist massgeblich.
                if content_changed:
                    await _push_group_update(ebay, listing, db, title=title, desc_html=desc_html,
                                             aspects=aspects)
                # Einzel-Items best effort (eBay revalidiert hier teils uebertrieben streng).
                for sku in skus:
                    try:
                        if content_changed:
                            await ebay.update_inventory_item_fields(
                                sku, title=title, description=desc_html, aspects=aspects)
                    except Exception as exc:  # noqa: BLE001
                        warnings.append(f"{sku}: {str(exc)[:120]}")
                # REPUBLISH (PFLICHT): erst publishOfferByInventoryItemGroup uebernimmt die
                # Inventory-Aenderung ins LIVE-Listing – ohne diesen Schritt zeigte eBay weiter den
                # ALTEN Titel, waehrend die DB den neuen trug (Fund 29.07., Tennisarmband #409).
                # Schlaegt das Republish fehl, fliegt die Exception -> DB bleibt beim alten Stand.
                if content_changed and hasattr(ebay, "publish_offer_by_inventory_item_group"):
                    await ebay.publish_offer_by_inventory_item_group(listing.ebay_draft_id)
            else:
                sku = skus[0]
                offer = None
                if desc_html is not None:
                    offer = await ebay._first_offer_for_sku(sku)
                    if offer and offer.get("offerId"):
                        await ebay.update_offer_fields(offer["offerId"], offer,
                                                       listing_description=desc_html)
                if content_changed:
                    await ebay.update_inventory_item_fields(
                        sku, title=title, description=desc_html, aspects=aspects)
                    # REPUBLISH auch beim Einzel-Listing (Inventory-Write allein wird sonst
                    # nicht (zuverlaessig) live).
                    if hasattr(ebay, "republish_offer"):
                        if offer is None:
                            offer = await ebay._first_offer_for_sku(sku)
                        if offer and offer.get("offerId"):
                            await ebay.republish_offer(offer["offerId"])
            # READ-BACK Titel (best effort): stimmt der LIVE-Titel jetzt wirklich?
            if title is not None and hasattr(ebay, "get_item_price_info"):
                try:
                    info = await ebay.get_item_price_info(listing.ebay_item_id)
                    live_title = (info.get("title") or "").strip()
                    if live_title and live_title != title.strip():
                        warnings.append(f"Titel-Read-back weicht ab – eBay zeigt: {live_title[:80]}")
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"Titel-Read-back nicht moeglich: {str(exc)[:100]}")
            tl.result_data = {"skus": len(skus), "changed": changed,
                              "price_verified": price_verified,
                              "warnings": warnings or None}
            pushed = True

    # Push (inkl. Read-back) erfolgreich -> JETZT erst lokal uebernehmen. Bei jedem
    # Fehler oben fliegt die Exception durch (Router -> 502) und die DB bleibt beim
    # alten Stand: System und eBay laufen nie auseinander.
    changed = _apply_local()
    db.commit()
    if pushed and price_eur is not None:
        # Cockpit SOFORT ehrlich machen (Nutzer-Fund 08.08.: "macht Verlust" hing bis zum
        # Sync): echten Live-Preis zurücklesen (korrekte Key-Schemata) + Report-Rebuild —
        # derselbe erprobte Pfad wie der ⟳-Knopf am Einzel-Listing.
        try:
            from app.services import ebay_import_service
            from app.services.listing_match_service import rebuild_reprice_report
            await ebay_import_service.sync_ebay_live_prices(db, only_ids=[listing.id])
            rebuild_reprice_report(db)
        except Exception as exc:  # noqa: BLE001 – Refresh darf den Preis-Erfolg nicht kippen
            warnings.append(f"Cockpit-Refresh unvollständig: {str(exc)[:100]}")
    return {"listing_id": listing.id, "changed": changed, "pushed_to_ebay": pushed,
            "price_verified": price_verified, "warnings": warnings}


def _already_ended_error(text: str) -> bool:
    """Erkennt PRAEZISE 'Listing ist schon beendet' in eBay-Fehlertexten.

    Bewusst nur explizite Formulierungen/Fehlercodes – lose Woerter wie 'ended'
    wuerden auch 'cannot be ended' matchen und echte Fehler verschlucken.
    """
    low = (text or "").lower()
    phrases = (
        "already been closed", "already closed", "already ended", "already been ended",
        "auction has been closed", "bereits beendet", "wurde bereits beendet",
        "item is not active", "listing is not active", "invalid item id",
        "item id is invalid",
    )
    if any(p in low for p in phrases):
        return True
    # Trading-Fehlercode 1047 = "auction already closed" (als eigenes Wort, nicht Teil
    # einer Item-ID – die sind 12-stellig und wuerden '1047' zufaellig enthalten)
    import re as _re
    return bool(_re.search(r"\b1047\b(?!\d)", low))


async def delete_draft(db: Session, *, listing_id: int) -> dict:
    """Entwurf ENDGÜLTIG löschen (nur Nutzer-Aktion über den 🗑-Knopf, nie automatisch).

    Nur für Drafts ohne eBay-Item; Listings mit Verkäufen sind geschützt.
    Best effort werden eventuell vorhandene eBay-Entwurfs-Objekte (Inventory-Items
    aus fehlgeschlagenen Publishes) mit entfernt — nur auf der Betriebs-Instanz.
    """
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    if listing.listing_status != "draft" or listing.ebay_item_id:
        raise PersistentError("Nur Entwürfe können gelöscht werden — "
                              "aktive Listings über 'Beenden' (🗑) delisten")
    has_sales = db.scalar(select(func.count(Sale.id)).where(Sale.listing_id == listing_id))
    if has_sales:
        raise PersistentError(f"Listing hat {has_sales} Verkäufe – löschen nicht möglich "
                              "(Belege/Historie referenzieren es)")
    if listing_id in _publish_pending_ids():
        raise PersistentError("Listing lädt gerade zu eBay hoch – erst abwarten")

    title = listing.title_seo
    ebay_cleanup = "übersprungen"
    settings = get_settings()
    if settings.monitor_push_real:
        # eBay-Reste aus frueheren Publish-Versuchen best effort entfernen
        try:
            ebay = _real_ebay()
            skus = await _listing_variant_skus(ebay, listing)
            for sku in skus:
                try:
                    await ebay.delete_inventory_item(sku)
                except Exception:  # noqa: BLE001 – nie da gewesen ist auch ok
                    pass
            ebay_cleanup = f"{len(skus)} SKU(s) bereinigt"
        except Exception as exc:  # noqa: BLE001 – Cleanup darf das Loeschen nicht blocken
            ebay_cleanup = f"nicht möglich ({str(exc)[:60]})"

    with task_log(db, task_type="delete_draft", reference_id=listing_id) as tl:
        db.query(PriceHistory).filter(PriceHistory.listing_id == listing_id).delete()
        db.delete(listing)
        tl.result_data = {"title": (title or "")[:120], "ebay_cleanup": ebay_cleanup}
        db.commit()
    logger.info("draft geloescht (Nutzer-Aktion)", extra={"listing_id": listing_id})
    return {"deleted": True, "listing_id": listing_id, "ebay_cleanup": ebay_cleanup}


def _publish_pending_ids() -> set:
    try:
        from app.services import publish_queue
        return publish_queue.pending_ids()
    except Exception:  # noqa: BLE001
        return set()


async def end_listing_live(db: Session, *, listing_id: int) -> dict:
    """Listing endgueltig beenden: auf eBay delisten + lokal auf 'ended' setzen.

    Pfad je Listing-Typ: Multivarianten-Gruppe -> withdrawOfferByInventoryItemGroup;
    Inventory-Einzeloffer -> withdrawOffer; importiertes Klassik-Listing -> Trading
    EndItem. Bereits beendete Listings ('nicht mehr aktiv') gelten als Erfolg.
    Der DB-Datensatz bleibt erhalten (Verkaeufe/Belege referenzieren ihn).
    """
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")

    ended_on_ebay = False
    detail = "war nicht live"
    if listing.ebay_item_id and listing.listing_status == "active":
        ebay = _real_ebay()
        with task_log(db, task_type="end_listing", reference_id=listing_id) as tl:
            try:
                gk = listing.ebay_draft_id or ""
                if gk.endswith("-GRP"):
                    await ebay.withdraw_offer_by_group(gk)
                    detail = "Gruppe zurueckgezogen"
                else:
                    offer = await ebay._first_offer_for_sku(listing.ebay_sku or f"AE-{listing.id}")
                    if offer and offer.get("offerId"):
                        await ebay.withdraw_offer(offer["offerId"])
                        detail = "Offer zurueckgezogen"
                    else:
                        await ebay.end_item(listing.ebay_item_id)
                        detail = "EndItem (Trading)"
                ended_on_ebay = True
            except Exception as exc:  # noqa: BLE001 – zweiter Weg, dann harter Fehler
                msg = str(exc)
                try:
                    await ebay.end_item(listing.ebay_item_id)
                    ended_on_ebay = True
                    detail = "EndItem (Fallback)"
                except Exception as exc2:  # noqa: BLE001
                    if _already_ended_error(f"{msg} {exc2}"):
                        ended_on_ebay = True
                        detail = "war bereits beendet"
                    else:
                        raise PersistentError(f"Beenden auf eBay fehlgeschlagen: {exc2}") from exc2
            tl.result_data = {"ended_on_ebay": ended_on_ebay, "detail": detail}

    listing.listing_status = "ended"
    listing.quantity_available = 0
    db.commit()
    return {"listing_id": listing_id, "status": "ended",
            "ended_on_ebay": ended_on_ebay, "detail": detail}


async def repair_variant_axes(db: Session, *, listing_id: int) -> dict:
    """Gedriftete Varianten-Achsen eines LIVE-Multivarianten-Listings wieder konsistent machen.

    Drift-Bug (Listing 426): die Inventory-ITEMS tragen andere Achsennamen als die
    Inventory-Item-GROUP (weil _variation_safe_axis_map bei getrennten Operationen
    nicht-deterministisch renamte). Folge: ``aspectsImageVariesBy`` zeigt auf einen
    Achsennamen, den kein Item traegt -> eBay zeigt fuer alle Varianten dasselbe Bild.

    Reparatur: die von den ITEMS live getragenen Achsennamen sind massgeblich; die Gruppe
    wird darauf zurueckgeschrieben (in-place: createOrReplace via _push_group_update +
    publishOfferByInventoryItemGroup, gleiche Item-ID). Geht das nicht (eBay 25013), wird
    NICHTS automatisch beendet – es kommt ``action_required``/``needs_recreate`` zurueck und
    der Nutzer entscheidet ueber beenden+neu (Regel [[keine-auto-loeschungen]]).
    """
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    gk = listing.ebay_draft_id or ""
    if not (listing.ebay_item_id and listing.listing_status == "active" and gk.endswith("-GRP")):
        raise PersistentError("Nur aktive Multivarianten-Listings koennen repariert werden")
    product = db.get(Product, listing.product_id) if listing.product_id else None
    axis_names, variants = _usable_variants(product)
    if not axis_names:
        raise PersistentError("Listing hat keine echten Varianten")

    ebay = _real_ebay()
    # 1) Ist-Zustand der Gruppe: welche Achsen-/Bild-Achsennamen traegt sie aktuell?
    grp = await ebay.get_inventory_item_group(gk) or {}
    varies = grp.get("variesBy") or {}
    group_axis_names = [s.get("name") for s in (varies.get("specifications") or []) if s.get("name")]
    group_image_axis = varies.get("aspectsImageVariesBy") or []
    # 2) Wahrheit aus den Items: welche Namen tragen die Varianten wirklich?
    truth = await _live_axis_map(ebay, listing, axis_names, variants)
    if truth is None:
        raise PersistentError(
            "Live-Achsen der Varianten nicht eindeutig lesbar – In-Place-Reparatur nicht "
            "moeglich. Bitte das Listing beenden und neu veroeffentlichen.")
    item_axis_names = set(truth.values())
    # 3) Drift? Gruppe ist konsistent, wenn ihre Achsen UND ihre Bild-Achse von den Items getragen werden.
    drift = (set(group_axis_names) != item_axis_names
             or any(a not in item_axis_names for a in group_image_axis))
    if not drift:
        return {"listing_id": listing_id, "repaired": False, "drift": False,
                "axes": sorted(item_axis_names),
                "message": "Item- und Gruppen-Achsen sind bereits konsistent."}

    # 4) In-Place reparieren: Gruppe aus der Item-Wahrheit neu schreiben + republish.
    with task_log(db, task_type="repair_variant_axes", reference_id=listing_id) as tl:
        try:
            if not await _push_group_update(ebay, listing, db):
                raise PersistentError("Gruppe konnte nicht neu geschrieben werden")
            await ebay.publish_offer_by_inventory_item_group(gk)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "25013" in msg:
                # NICHT reparierbar in-place -> nichts beenden, dem Nutzer melden.
                tl.result_data = {"repaired": False, "needs_recreate": True, "error": msg[:200]}
                return {"listing_id": listing_id, "repaired": False, "drift": True,
                        "needs_recreate": True, "action_required": True,
                        "group_axes": group_axis_names, "item_axes": sorted(item_axis_names),
                        "message": ("In-Place nicht moeglich (eBay 25013). Zum Beheben das "
                                    "Listing beenden und neu veroeffentlichen – es wurde nichts "
                                    "automatisch beendet.")}
            if isinstance(exc, PersistentError):
                raise
            raise PersistentError(f"Reparatur fehlgeschlagen: {msg}") from exc
        # 5) Verifikation: Gruppe neu lesen – Achsen + Bild-Achse muessen jetzt zu den Items passen.
        grp2 = await ebay.get_inventory_item_group(gk) or {}
        v2 = grp2.get("variesBy") or {}
        new_axes = {s.get("name") for s in (v2.get("specifications") or []) if s.get("name")}
        new_img = v2.get("aspectsImageVariesBy") or []
        verified = new_axes == item_axis_names and all(a in item_axis_names for a in new_img)
        tl.result_data = {"repaired": True, "axes": sorted(item_axis_names),
                          "image_varies_by": new_img, "verified": verified}
    return {"listing_id": listing_id, "repaired": True, "drift": True,
            "axes": sorted(item_axis_names), "image_varies_by": new_img, "verified": verified,
            "message": ("Gruppen-Achsen auf die Item-Wahrheit zurueckgesetzt."
                        if verified else
                        "Gruppe neu geschrieben, Verifikation aber uneindeutig – bitte pruefen.")}


async def _retry_transient(coro_factory, *, attempts: int = 5, base_delay: float = 1.5):
    """Ruft eine async-Operation und wiederholt bei TransientError mit Backoff.

    eBays createOrReplaceInventoryItem liefert sporadisch 500/25001 ('Interner Core
    Inventory Service-Fehler') – ein FLAKY interner Fehler, der beim Wiederholen fast
    immer durchgeht. PersistentError (echte 4xx-Ablehnungen) wird NICHT wiederholt.
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await coro_factory()
        except TransientError as exc:  # 5xx / Netzwerk -> erneut versuchen
            last = exc
            if i < attempts - 1:
                await asyncio.sleep(base_delay * (2 ** i))
    raise last  # type: ignore[misc]


async def repair_variant_images(db: Session, *, listing_id: int) -> dict:
    """CHIRURGISCH IN-PLACE: jede Variante auf GENAU EIN Bild (ihr eigenes) zuruecksetzen.

    Alt-Bug: jede Variante trug 1 Variantenbild + 8 Standardbilder. Fix: je Varianten-
    Inventory-Item die imageUrls auf das erste (= Variantenbild) kuerzen, dann die
    BESTEHENDE Gruppe re-publishen -> GLEICHE Item-ID, KEIN neues Listing, kein Duplikat.
    Unterscheidet sich bewusst von publish_listing_live(force=True), das ein neues
    Listing erzeugt. Die Standardbilder bleiben in der Gruppen-Galerie erhalten.

    Robust gegen eBays flaky 25001: jede SKU wird mit Backoff wiederholt; SKUs, die
    trotzdem scheitern, werden GESAMMELT (nicht abgebrochen) und zurueckgemeldet, damit
    ein Folgelauf nur sie erneut anfasst. Re-Publish nur, wenn mindestens eine SKU
    gekuerzt wurde.
    """
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    gk = listing.ebay_draft_id or ""
    if not (listing.ebay_item_id and listing.listing_status == "active" and gk.endswith("-GRP")):
        raise PersistentError("Nur aktive Multivarianten-Listings koennen repariert werden")
    ebay = _real_ebay()
    grp = await ebay.get_inventory_item_group(gk) or {}
    skus = grp.get("variantSKUs") or []
    if not skus:
        raise PersistentError("Keine Varianten-SKUs in der Gruppe gefunden")
    trimmed = 0
    already_ok = 0
    failed: list[str] = []
    with task_log(db, task_type="repair_variant_images", reference_id=listing_id) as tl:
        for sku in skus:
            try:
                item = await _retry_transient(lambda s=sku: ebay.get_inventory_item(s))
            except Exception as exc:  # noqa: BLE001 – GET selbst dauerhaft kaputt
                failed.append(sku)
                logger.warning("repair_variant_images: GET %s fehlgeschlagen: %s", sku, exc)
                continue
            imgs = ((item or {}).get("product") or {}).get("imageUrls") or []
            if len(imgs) <= 1:
                already_ok += 1
                continue
            # Erstes Bild = das Variantenbild (Publish legte es zuerst ab).
            try:
                await _retry_transient(
                    lambda s=sku, im=imgs: ebay.update_inventory_item_fields(s, image_urls=im[:1]))
                trimmed += 1
            except Exception as exc:  # noqa: BLE001 – nach Retries immer noch 25001 o.ae.
                failed.append(sku)
                logger.warning("repair_variant_images: SKU %s nach Retries fehlgeschlagen: %s",
                               sku, exc)
        if trimmed:
            # BESTEHENDE Gruppe re-publishen -> gleiche Item-ID, Aenderung wird live.
            await _retry_transient(lambda: ebay.publish_offer_by_inventory_item_group(gk))
        tl.result_data = {"variants": len(skus), "trimmed": trimmed,
                          "already_ok": already_ok, "failed": failed}
    if failed:
        msg = (f"{trimmed} Varianten gekuerzt, {len(failed)} nach Retries fehlgeschlagen "
               f"(erneut versuchbar). Item-ID unveraendert.")
    elif trimmed:
        msg = f"{trimmed} Varianten auf 1 Bild gekuerzt (in-place, gleiche Item-ID)."
    else:
        msg = "Alle Varianten hatten bereits nur 1 Bild."
    return {"listing_id": listing_id, "item_id": listing.ebay_item_id,
            "variants": len(skus), "trimmed": trimmed, "already_ok": already_ok,
            "failed": failed, "message": msg}


def variant_repair_report(db: Session, *, scope: str = "all") -> dict:
    """READ-ONLY: welche LIVE Multivarianten-Listings brauchen eine Reparatur? Aendert NICHTS.

    Erkennt aus der DB (ohne eBay-Calls, daher schnell und ungefaehrlich):
      * **Bild-Aufraeumen**: bis zum Fix 2026-07-05 bekam jede Variante 1+8 Bilder
        (eigenes + Standardbilder) -> ALLE bestehenden Multi-Live-Listings sollten auf
        genau EIN Bild je Variante aufgeraeumt werden.
      * **Bestand**: Varianten, die laut DB ausverkauft sind (`stock<=0`) oder deren Produkt
        `supplier_in_stock=False` ist, das Listing aber noch aktiv -> Menge muss auf 0.
      * **Achsen-Drift** (alle Varianten gleiches Bild): steht NICHT in der DB, wird pro
        Listing beim eigentlichen Reparatur-Lauf LIVE geprueft -> hier nur so markiert.

    ``scope``: 'week' = nur in den letzten 7 Tagen hochgeladene, 'all' = alle.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    listings = db.scalars(select(Listing).where(
        Listing.ebay_item_id.is_not(None),
        Listing.listing_status == "active")).all()
    rows: list[dict] = []
    stock_cnt = week_cnt = 0
    for l in listings:
        if not (l.ebay_draft_id or "").endswith("-GRP"):
            continue   # nur echte Multivarianten-Listings (Gruppe)
        product = db.get(Product, l.product_id) if l.product_id else None
        axis_names, variants = _usable_variants(product)
        if not axis_names:
            continue
        created = l.created_at
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        this_week = bool(created and created >= week_ago)
        if scope == "week" and not this_week:
            continue
        sold_out = sum(1 for v in variants
                       if (v.get("stock") is not None and v.get("stock") <= 0))
        needs_stock = sold_out > 0 or (l.supplier_in_stock is False)
        rows.append({
            "listing_id": l.id, "title": l.title_seo, "ebay_item_id": l.ebay_item_id,
            "url": f"https://www.ebay.de/itm/{l.ebay_item_id}",
            "created_at": created.isoformat() if created else None,
            "this_week": this_week, "variant_count": len(variants),
            "sold_out_variants": sold_out,
            "needs_image_cleanup": True,          # alle Alt-Listings (vor dem Fix publiziert)
            "needs_stock_fix": needs_stock,
            "axis_check": "live beim Lauf",
        })
        stock_cnt += 1 if needs_stock else 0
        week_cnt += 1 if this_week else 0
    # Diese Woche zuerst, dann die groessten (meisten Varianten) oben.
    rows.sort(key=lambda r: (not r["this_week"], -r["variant_count"]))
    return {
        "scope": scope, "generated_at": now.isoformat(),
        "total_multi_live": len(rows), "this_week": week_cnt, "older": len(rows) - week_cnt,
        "needs_image_cleanup": len(rows), "needs_stock_fix": stock_cnt,
        "listings": rows,
        "note": ("Nur Analyse – es wurde nichts geaendert. Achsen-Drift (alle Varianten "
                 "gleiches Bild) wird pro Listing erst beim eigentlichen Reparatur-Lauf live "
                 "geprueft und behoben."),
    }


async def bulk_repair_variants(db: Session, *, scope: str = "week", dry_run: bool = True,
                               limit: int | None = None) -> dict:
    """Bulk-BILD-Reparatur aller LIVE Multivarianten-Listings. Standard: ``dry_run=True``
    (nur planen, NICHTS aendern).

    Pro Listing der CHIRURGISCHE In-Place-Fix ``repair_variant_images``: je Varianten-Item
    die imageUrls auf 1 (ihr eigenes) kuerzen, dann die BESTEHENDE Gruppe re-publishen ->
    GLEICHE Item-ID, KEIN neues Listing, kein Duplikat.

    WARUM NICHT publish_listing_live(force=True): das erzeugte fuer Multivarianten ein
    NEUES Listing (neue Item-ID, -GRP-GRP) und beendete das alte nicht -> Duplikat
    (belegt an Listing 414). Deshalb hier ausschliesslich der In-Place-Weg.

    Bestand (Menge 0 bei ausverkauft) und Achsen-Drift werden hier BEWUSST nicht
    angefasst – die Bild-Reparatur soll keine anderen Felder mitbewegen. ``needs_stock_fix``
    kommt aus dem Report weiterhin als Hinweis mit. Jedes Listing ist isoliert: ein Fehler
    stoppt den Lauf nicht; flaky eBay-25001 werden je SKU automatisch wiederholt, dauerhaft
    fehlschlagende SKUs kommen pro Listing als ``failed_skus`` zurueck (Folgelauf fasst nur
    sie an).

    ``scope``: 'week' (nur diese Woche hochgeladene) | 'all'. ``limit``: nur die ersten N
    (fuer einen kleinen Test-Batch zuerst).
    """
    report = variant_repair_report(db, scope=scope)
    targets = report["listings"]
    if limit is not None:
        targets = targets[:limit]

    if dry_run:
        return {"dry_run": True, "scope": scope, "count": len(targets),
                "needs_stock_fix": report.get("needs_stock_fix"),
                "plan": ("Pro Listing chirurgischer In-Place-Bildfix (repair_variant_images): "
                         "je Variante auf 1 Bild, dann bestehende Gruppe re-publishen. Gleiche "
                         "Item-ID, kein Duplikat. Bestand/Achsen werden NICHT angefasst."),
                "listings": targets, "note": report["note"]}

    # --- ECHTER LAUF (nur Bilder, in-place) ---
    results: list[dict] = []
    repaired = with_failures = errored = 0
    total_trimmed = total_failed_skus = 0
    for t in targets:
        lid = t["listing_id"]
        try:
            res = await repair_variant_images(db, listing_id=lid)
            fskus = res.get("failed") or []
            total_trimmed += res.get("trimmed", 0)
            total_failed_skus += len(fskus)
            status = "partial" if fskus else "repaired"
            with_failures += 1 if fskus else 0
            repaired += 0 if fskus else 1
            results.append({"listing_id": lid, "status": status,
                            "ebay_item_id": res.get("item_id"), "trimmed": res.get("trimmed"),
                            "failed_skus": fskus})
        except Exception as exc:  # noqa: BLE001 – je Listing isoliert, Rest laeuft weiter
            msg = str(exc)
            results.append({"listing_id": lid, "status": "error", "error": msg[:200]})
            errored += 1
            logger.warning("bulk-repair-images: %s fehlgeschlagen: %s", lid, msg[:200])
    return {"dry_run": False, "scope": scope, "count": len(targets),
            "repaired": repaired, "partial": with_failures, "errored": errored,
            "total_trimmed": total_trimmed, "total_failed_skus": total_failed_skus,
            "results": results,
            "note": ("Fertig – nur Bilder, in-place (gleiche Item-ID, keine Duplikate). "
                     "'partial' = einzelne SKUs nach Retries noch offen (Lauf wiederholen). "
                     "'error' = Listing komplett fehlgeschlagen. Bestand/Achsen wurden nicht "
                     "angefasst; needs_stock_fix aus dem Report separat behandeln.")}


async def _publish_single(ebay, listing, *, category, aspects, brand, images,
                          base_sku, settings, draft_only: bool = False) -> tuple[str | None, str]:
    # AUSVERKAUFT -> Menge 0 (nie mit Standard-20 anbieten, was der Lieferant nicht hat).
    if listing.supplier_in_stock is False:
        qty = 0
    else:
        qty = int(listing.quantity_available or settings.default_listing_quantity)
        # eBay-Limit: max. 250 Bestandseinheiten je Anfrage (Fehler 25723) — AliExpress-
        # Riesenbestaende (z. B. 4000+) hart deckeln, sonst haengt der Entwurf dauerhaft.
        qty = min(qty, 250)
    desc = _html_description(listing.description or listing.title_seo)
    await ebay.create_inventory_item(
        base_sku, title=listing.title_seo, description=desc,
        image_urls=images, quantity=qty, aspects=aspects, brand=brand)
    # Lokale Quelle/Eigenbestand -> schnelle Versandbedingung (None = Standard).
    from app.services import fast_shipping_service
    pol = await fast_shipping_service.publish_policies(ebay, listing)
    offer_id = await ebay.create_offer(
        base_sku, price_eur=float(listing.price_eur or 0), category_id=category,
        quantity=qty, listing_description=desc, listing_policies=pol)
    listing.ebay_draft_id = offer_id
    if draft_only:
        return None, "draft"
    item_id = await ebay.publish_listing(offer_id, title=listing.title_seo, category_id=category)
    return item_id, "single"


async def retry_failed_publishes(db: Session, *, max_age_hours: int = 24,
                                 max_attempts: int = 5) -> dict:
    """Selbstheilung: fehlgeschlagene Publish-/Entwurf-Versuche automatisch nachholen.

    Findet Listings, deren letzter golive-/ebay_draft-Versuch scheiterte (noch draft,
    < max_attempts Versuche, juenger als max_age_hours) und versucht sie erneut –
    inkl. aller Fallbacks (Kategorie, Transient-Retry). Der Nutzer muss nichts tun.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import desc

    from app.models import TaskLog
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    logs = db.scalars(select(TaskLog).where(
        TaskLog.task_type.in_(["golive", "ebay_draft"]),
        TaskLog.created_at >= cutoff).order_by(desc(TaskLog.created_at))).all()
    last_by_ref: dict[str, TaskLog] = {}
    attempts: dict[str, int] = {}
    for t in logs:
        ref = str(t.reference_id)
        last_by_ref.setdefault(ref, t)
        if t.status == "failed":
            attempts[ref] = attempts.get(ref, 0) + 1

    retried = succeeded = 0
    for ref, last in last_by_ref.items():
        if last.status != "failed" or not ref.isdigit():
            continue
        if attempts.get(ref, 0) >= max_attempts:
            continue
        listing = db.get(Listing, int(ref))
        if listing is None or listing.ebay_item_id or listing.listing_status != "draft":
            continue
        if not listing.product_id:
            continue  # ohne Produkt/Bilder nicht automatisch loesbar
        from app.studio import is_studio
        if is_studio(listing, db):
            continue  # Studio-Angebot: geht nie ueber den Dropshipping-Publish
        # DOPPEL-PUBLISH-SCHUTZ: Listings, die der Publish-Queue gehoeren (eingereiht
        # oder gerade im Backoff zwischen zwei Versuchen), NIE direkt publishen —
        # sonst laufen Job + Worker interleaved auf denselben SKUs/Offers.
        from app.services import publish_queue
        if listing.publish_queued or listing.id in publish_queue.pending_ids():
            continue
        retried += 1
        try:
            if last.task_type == "ebay_draft":
                await publish_listing_live(db, listing_id=listing.id, draft_only=True)
            else:
                # Live-Publishes laufen IMMER ueber die Queue (ein serieller Worker
                # = der einzige Live-Publish-Pfad; enqueue ist idempotent).
                await publish_queue.enqueue(listing.id)
            succeeded += 1
            logger.info("auto-retry publish eingereiht/OK fuer Listing %s", ref)
        except Exception as exc:  # noqa: BLE001 – naechster Zyklus versucht erneut
            logger.warning("auto-retry publish fehlgeschlagen fuer %s: %s", ref, str(exc)[:150])
    return {"candidates": retried, "succeeded": succeeded}


async def _end_conflicting_single(ebay, base_sku: str) -> str | None:
    """Beendet ein evtl. vorhandenes Einzel-Angebot auf der Basis-SKU (sonst Duplikat).

    Nötig, wenn ein Produkt bereits als Einzel-Listing live ist und jetzt als
    Varianten-Listing neu veröffentlicht wird (eBay kann nicht in-place umwandeln).
    Gibt die beendete listingId zurück (oder None).
    """
    try:
        existing = await ebay._first_offer_for_sku(base_sku)
    except Exception:  # noqa: BLE001
        return None
    if not existing:
        return None
    ended = ((existing.get("listing") or {}).get("listingId"))
    try:
        if ended:
            await ebay.withdraw_offer(existing["offerId"])
        await ebay.delete_inventory_item(base_sku)  # Einzel-Item + dessen Offer entfernen
    except Exception as exc:  # noqa: BLE001
        logger.warning("Alt-Einzelangebot konnte nicht vollständig beendet werden: %s", exc)
    return ended


# Bevorzugte Ersatz-Achsennamen, wenn eBay den AliExpress-Achsennamen nicht als
# Variantenmerkmal erlaubt (Reihenfolge = Priorität; es zählt nur, was die
# Kategorie laut Taxonomy wirklich als Variation erlaubt).
_VARIATION_FALLBACK_AXES = ["Modell", "Stil", "Typ", "Farbe", "Größe", "Material",
                            "Motiv", "Design", "Ausführung", "Charakter"]


async def _variation_safe_axis_map(ebay, category_id: str, axis_names: list[str]) -> dict[str, str]:
    """Achsennamen auf eBay-variationsfähige Merkmale mappen (Fix für eBay 25002
    'X ist kein zulässiges Variantenmerkmal', z.B. AliExpress-Achse 'Charakter').

    Erlaubte Namen bleiben (case-korrekt); nicht erlaubte bekommen das erste noch
    freie erlaubte Merkmal aus _VARIATION_FALLBACK_AXES (sonst alphabetisch erstes
    freies). Liefert die Taxonomy keine Info, bleibt alles unverändert.
    """
    try:
        aspects = await ebay.get_required_aspects(category_id or "0")
    except Exception:  # noqa: BLE001 – kein Taxonomy-Zugriff -> nichts umbenennen
        aspects = []
    allowed = {a["name"] for a in aspects if a.get("variation") and a.get("name")}
    if not allowed:
        return {a: a for a in axis_names}
    by_lower = {a.lower(): a for a in allowed}
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for ax in axis_names:   # exakte Treffer zuerst reservieren
        hit = by_lower.get(ax.strip().lower())
        if hit and hit not in used:
            mapping[ax] = hit
            used.add(hit)
    for ax in axis_names:
        if ax in mapping:
            continue
        fb = next((by_lower[f.lower()] for f in _VARIATION_FALLBACK_AXES
                   if f.lower() in by_lower and by_lower[f.lower()] not in used), None)
        if fb is None:
            fb = next((a for a in sorted(allowed) if a not in used), None)
        mapping[ax] = fb or ax
        if fb:
            used.add(fb)
    return mapping


def _rename_axes(axis_names: list[str], variants: list[dict],
                 mapping: dict[str, str]) -> tuple[list[str], list[dict]]:
    """Achsen in Namensliste + Varianten-Optionen konsistent umbenennen."""
    new_names = [mapping.get(a, a) for a in axis_names]
    renamed = []
    for v in variants:
        nv = dict(v)
        nv["options"] = {mapping.get(k, k): val for k, val in (v.get("options") or {}).items()}
        renamed.append(nv)
    return new_names, renamed


async def _publish_multi(ebay, listing, product, *, category, aspects, brand,
                         axis_names, variants, base_sku, settings,
                         draft_only: bool = False) -> tuple[str | None, str]:
    """Echtes Multivarianten-Listing (Inventory Item Group)."""
    # eBay erlaubt je Kategorie nur bestimmte Varianten-Achsen -> ggf. umbenennen.
    axis_map = await _variation_safe_axis_map(ebay, category, axis_names)
    orig_axis_names = list(axis_names)
    if any(axis_map.get(a, a) != a for a in axis_names):
        logger.info("Varianten-Achsen fuer Kategorie %s umbenannt: %s", category,
                    {k: v for k, v in axis_map.items() if k != v})
        axis_names, variants = _rename_axes(axis_names, variants, axis_map)
    # Unbedingt, nicht nur beim Umbenennen - siehe _normalisiere_groessen.
    variants = _normalisiere_groessen(axis_names, variants)
    # War das Produkt bereits als Einzel-Listing live? -> zuerst beenden (kein Duplikat).
    ended = await _end_conflicting_single(ebay, base_sku)
    desc = _html_description(listing.description or listing.title_seo)
    common_images = [u for u in (product.images or []) if u]
    # Galerie: Standardbilder + alle Variantenbilder -> Kaeufer sieht beim Blaettern
    # jede Variante (eBay bis 24 Bilder). Wird der Gruppe als image_urls gegeben.
    group_gallery = gallery_images(product)
    non_axis = {k: v for k, v in aspects.items() if k not in axis_names}
    color_axis = _image_axis(orig_axis_names, axis_names, axis_map, variants)
    default_qty = settings.default_listing_quantity
    # Lokale Quelle/Eigenbestand -> schnelle Versandbedingung; EINMAL vor der Schleife
    # rechnen (ein Freight-Check je Produkt, nicht je Variante). None = Standard.
    from app.services import fast_shipping_service
    fast_pol = await fast_shipping_service.publish_policies(ebay, listing)

    variant_skus: list[str] = []
    used: dict[str, list[str]] = {a: [] for a in axis_names}
    all_have_image = bool(color_axis)

    # Jede Variante bekommt ihren EIGENEN kalkulierten Preis (aus ihrem EK).
    # Premium-Regel: KI-Variante nie billiger als die Basisvariante.
    price_map = {p["sku"]: p["price_eur"]
                 for p in compute_variant_prices(listing, product, settings)}

    sku_by_attr: dict[str, str] = {}   # -> nach dem Aufbau am Produkt festschreiben
    for i, v in enumerate(variants, 1):
        vsku = variant_ebay_sku(base_sku, v, i)
        if v.get("attr"):
            sku_by_attr[v["attr"]] = vsku
        variant_skus.append(vsku)
        opts = v["options"]
        vaspects = dict(non_axis)
        for a in axis_names:
            val = opts[a]
            vaspects[a] = [val]
            if val not in used[a]:
                used[a].append(val)
        vimg = v.get("image")
        if color_axis and not vimg:
            all_have_image = False
        # GENAU EIN Bild je Variante: nur das eigene Variantenbild. Die Standardbilder NICHT
        # mitschleppen (frueher 1+8 pro Variante -> bei 50 Varianten hunderte Bilder, voellig
        # unuebersichtlich). Standardbilder erscheinen weiter in der GRUPPEN-Galerie
        # (gallery_images) fuers Gesamtlisting. Ohne eigenes Bild: das Hauptbild als Fallback.
        vimages = [vimg] if vimg else common_images[:1]
        # AUSVERKAUFTE Variante -> Menge 0 (nicht mit Standard-20 anbieten). Bestand
        # <=0 oder explizit not-in-stock zaehlt als ausverkauft.
        vstock = v.get("stock")
        vqty = 0 if (vstock is not None and vstock <= 0) else default_qty
        await ebay.create_inventory_item(
            vsku, title=listing.title_seo, description=desc,
            image_urls=vimages, quantity=vqty, aspects=vaspects, brand=brand)
        await ebay.create_offer(
            vsku, price_eur=price_map.get(vsku, float(listing.price_eur or 0)),
            category_id=category, quantity=vqty, listing_description=desc,
            listing_policies=fast_pol)

    # Groessen klein nach gross. Gesammelt werden sie in der Reihenfolge, in der
    # AliExpress die Varianten liefert - und die ist beliebig ("4XL, 5XL, S, M,
    # XXL, ..."). Genau so stand es dann im Ausklapper beim Kaeufer.
    # Nutzerwunsch vom 03.09.2026: die Reihenfolge zaehlt beim Bestellen.
    from app.services import groessen as _groessen
    specs = [{"name": a,
              "values": (_groessen.sortiere_sicher(used[a])
                         if _groessen.ist_groessen_achse(a) else used[a])}
             for a in axis_names if used[a]]
    image_varies = [color_axis] if (color_axis and all_have_image) else None
    group_key = f"{base_sku}-GRP"[:50]   # eigener Namespace, kollidiert nicht mit SKUs
    await ebay.create_inventory_item_group(
        group_key, title=listing.title_seo, description=desc,
        image_urls=group_gallery, variant_skus=variant_skus,
        specifications=specs, image_varies_by=image_varies,
        aspects=(non_axis or None))
    # SKUs am PRODUKT festschreiben (ueber attr, denn _rename_axes arbeitet auf Kopien).
    # Ab hier haengt die Zuordnung Variante<->eBay-SKU nicht mehr an der Listenposition:
    # wird spaeter eine Variante geloescht, ruecken die uebrigen NICHT mehr nach.
    _persist_variant_skus(product, sku_by_attr)
    listing.ebay_draft_id = group_key
    if price_map:
        listing.price_eur = max(price_map.values())  # repraesentativer (hoechster) Preis
    mode = f"multi:{len(variant_skus)} Varianten" + (f" (Einzel-Listing {ended} beendet)" if ended else "")
    if draft_only:
        return None, "draft-" + mode
    item_id = await ebay.publish_offer_by_inventory_item_group(group_key)
    return item_id, mode
