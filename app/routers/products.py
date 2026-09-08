"""Bereich 1: Produktupload-Endpoints (Spec Kap. 3.1)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.retry import PersistentError
from app.schemas import (
    ProductFinalizeRequest,
    ProductFinalizeResponse,
    ProductUploadRequest,
    ProductUploadResponse,
)
from app.services import product_service, sprachfilter

logger = logging.getLogger("app.routers.products")

router = APIRouter(prefix="/api/v1/products", tags=["Bereich 1 – Produktupload"])


@router.post("/upload", response_model=ProductUploadResponse, status_code=201)
async def upload(body: ProductUploadRequest, db: Session = Depends(get_db)):
    try:
        result = await product_service.upload_product(
            db, aliexpress_url=body.aliexpress_url, skip_autods=body.skip_autods,
            ignoriere_duplikat=body.ignoriere_duplikat,
        )
    except product_service.DuplikatListing as exc:
        # Eigener Status + strukturierte Antwort, damit das Dashboard hier einen
        # "Trotzdem hochladen"-Knopf zeigen kann statt einer Sackgasse.
        raise HTTPException(status_code=409, detail={
            "code": "duplikat", "message": str(exc),
            "listing_id": exc.listing_id, "listing_status": exc.listing_status})
    except sprachfilter.FremdspracheAbgelehnt as exc:
        # Keine Stoerung, sondern eine Entscheidung - und der Text sagt bereits,
        # was zu tun ist. Als 500 "Extraction failed" waere er unbrauchbar.
        raise HTTPException(status_code=422, detail=str(exc))
    except PersistentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Extraction failed: {exc}")
    return result


@router.post("/store-import", status_code=202)
async def store_import(body: dict = Body(...)):
    """Bestseller eines AliExpress-SHOPS als Entwuerfe importieren (laeuft im Hintergrund).

    Antwortet SOFORT: Der Lauf oeffnet je Produkt die Seite und dauert Minuten – ein
    synchroner Request wuerde in den Timeout laufen (Lehre aus dem Silber-Lauf).
    Fortschritt kommt ueber ``GET /store-import/status``.
    """
    import asyncio

    from app.integrations import aliexpress_store
    from app.services import store_import_service

    url = str(body.get("store_url") or body.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Bitte einen AliExpress-Shop-Link angeben.")
    # Ausweich-Weg (Store 1105638009): eingefuegte PRODUKT-Links/IDs statt Shop-Link —
    # AliExpress liefert manche Shop-Seiten an Server-IPs leer aus; die Produkt-IDs
    # laufen dann direkt durch denselben Import (ohne Seiten-Ernte).
    product_ids = store_import_service.extract_product_ids(url)
    if not product_ids and not aliexpress_store.extract_store_id(url):
        raise HTTPException(
            status_code=400,
            detail="Keine Shop-Nummer im Link erkannt. Bitte den Link der SHOP-Seite einfügen "
                   "(z. B. https://de.aliexpress.com/store/1103573332) — ODER, falls der Shop "
                   "blockiert ist, mehrere Produkt-Links aus dem Shop hineinkopieren.")
    try:
        limit = int(body.get("limit") or 25)
    except (TypeError, ValueError):
        limit = 25
    # Platz SOFORT belegen (nicht erst im Hintergrund-Lauf) – sonst starten zwei schnelle
    # Klicks zwei Browser und importieren dieselben Produkte doppelt.
    if not store_import_service.try_reserve():
        raise HTTPException(status_code=409,
                            detail="Es läuft bereits ein Shop-Import. Bitte abwarten.")
    store_import_service._task = asyncio.create_task(
        store_import_service.import_store_products_bg(
            store_url_or_id=url, limit=limit, product_ids=product_ids or None))
    return {"gestartet": True, "limit": max(1, min(limit, store_import_service.MAX_PRODUCTS)),
            "modus": "produkt-ids" if product_ids else "shop",
            "hinweis": "Import läuft im Hintergrund. Fortschritt wird angezeigt."}


@router.post("/store-import/abbrechen")
async def store_import_abbrechen():
    """Laufenden Shop-Import anhalten. Bereits erzeugte Entwuerfe bleiben."""
    from app.services import store_import_service
    return store_import_service.abbrechen()


@router.get("/store-import/status")
async def store_import_status():
    """Fortschritt des laufenden (oder letzten) Shop-Imports."""
    from app.services import store_import_service
    return store_import_service.status()


@router.post("/finalize/{listing_id}", response_model=ProductFinalizeResponse)
async def finalize(listing_id: int, body: ProductFinalizeRequest, db: Session = Depends(get_db)):
    try:
        return await product_service.finalize_listing(
            db, listing_id=listing_id, approve=body.approve,
            overrides=body.overrides.model_dump(exclude_none=True),
        )
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# --------------------------------------------------------------------------
# Kategorie
#
# Die Bausteine lagen fertig im eBay-Client, es fehlten die Adressen. Alle drei
# gehen ueber category_service, damit Fehlerbehandlung und Attrappen-Umgang an
# EINER Stelle stehen und nicht in jedem Endpunkt nachgebaut werden.
# --------------------------------------------------------------------------
@router.get("/{listing_id}/category-suggestions")
async def kategorie_vorschlaege(listing_id: int, q: str | None = None,
                                db: Session = Depends(get_db)):
    """Kategorie-Vorschlaege in Klartext zum Titel des Listings.

    Rein lesend - hier geht nichts zu eBay hinaus, es wird nur gefragt. Deshalb
    auch im Attrappen-Betrieb bedienbar; die Antwort sagt ueber ``quelle``, woher
    sie kommt.

    ``q`` ueberschreibt den Suchtext, falls der Titel in die Irre fuehrt.
    """
    from app.models import Listing
    from app.services import category_service

    listing = db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="Listing nicht gefunden")

    suchtext = (q or "").strip() or (listing.title_seo or "")
    try:
        ergebnis = await category_service.vorschlaege(suchtext)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Vorschlaege nicht abrufbar: {exc}")

    ergebnis.update({
        "listing_id": listing.id,
        "suchtext": suchtext,
        "aktuell": {"id": listing.category_id, "name": listing.category_name},
    })
    return ergebnis


@router.get("/categories/{category_id}/aspects")
async def kategorie_merkmale(category_id: str):
    """Merkmale einer Kategorie - Pflicht und Kuer, mit erlaubten Werten.

    WICHTIG fuer die Oberflaeche: Eine leere Liste heisst NICHT gesichert "diese
    Kategorie hat keine Pflichtfelder". Die Taxonomie-Aufrufe des Clients
    verschlucken jede Ausnahme und geben ``[]`` zurueck - ein abgelaufener Token
    sieht genauso aus. Deshalb wird hier nichts behauptet; die Antwort nennt nur
    ihre Herkunft.
    """
    from app.services import category_service

    try:
        return await category_service.merkmale(category_id)
    except category_service.KategorieFehler as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Merkmale nicht abrufbar: {exc}")


@router.post("/{listing_id}/category")
async def kategorie_setzen(listing_id: int, body: dict = Body(...),
                           db: Session = Depends(get_db)):
    """Kategorie eines ENTWURFS setzen.

    Bewusst nur Entwuerfe. Bei einem aktiven Listing ist ein Kategoriewechsel ein
    Eingriff bei eBay: die Kategorie muss auf jedes Varianten-Offer, die Gruppe
    muss neu veroeffentlicht werden, und dabei prueft eBay die Achsennamen und
    Pflichtmerkmale gegen die NEUE Kategorie. Das ist ein eigener Weg mit eigener
    Rueckfrage - und er laesst sich hier gerade nicht erproben (Probebetrieb,
    keine aktiven Listings). Halb gebaut waere er gefaehrlicher als gar nicht.

    Ehrlich gehalten wird ausserdem der Entwurfsfall: Ein Entwurf hat oft schon
    ein echtes, unveroeffentlichtes eBay-Offer (aus dem Upload oder aus
    /ebay-draft). Dessen Kategorie aendert sich hier NICHT mit. Die Antwort sagt
    das, statt die Datenbank etwas behaupten zu lassen, was drueben nicht gilt.
    """
    from app.models import Listing
    from app.services import category_service

    listing = db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="Listing nicht gefunden")

    if listing.ebay_item_id:
        raise HTTPException(
            status_code=409,
            detail=("Dieses Listing ist bei eBay veroeffentlicht. Die Kategorie "
                    "eines aktiven Angebots laesst sich hier noch nicht aendern - "
                    "das ist ein Eingriff bei eBay und braucht einen eigenen, "
                    "abgesicherten Weg."),
        )

    try:
        kid = category_service.pruefe_kategorie_id(body.get("category_id"))
    except category_service.KategorieFehler as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    name = (body.get("category_name") or "").strip() or None

    listing.category_id = kid
    if name:
        listing.category_name = name
    db.commit()

    hinweise: list[str] = []
    if listing.ebay_draft_id:
        hinweise.append(
            "Zu diesem Entwurf gibt es bereits ein Angebot bei eBay. Dessen "
            "Kategorie bleibt unveraendert, bis der Entwurf neu hochgeladen wird."
        )
    if not name:
        hinweise.append(
            "Kein Kategoriename gespeichert - die Provisionsrechnung ordnet den "
            "Artikel dann der Pauschale zu."
        )

    return {"listing_id": listing.id, "category_id": kid,
            "category_name": listing.category_name, "hinweise": hinweise}


@router.post("/{listing_id}/edit")
async def edit_listing(listing_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    """Listing bearbeiten (Titel/Beschreibung/Merkmale/Preis) + falls live auf eBay updaten."""
    from app.services import golive_service
    try:
        return await golive_service.update_listing_live(
            db, listing_id=listing_id,
            title=body.get("title"), description=body.get("description"),
            item_specifics=body.get("item_specifics"), price_eur=body.get("price_eur"),
        )
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"eBay-Update fehlgeschlagen: {exc}")


@router.get("/variant-repair-report")
def variant_repair_report(scope: str = "all", db: Session = Depends(get_db)):
    """READ-ONLY Bericht: welche Live-Multivarianten-Listings brauchen eine Reparatur
    (Bild-Aufraeumen / Bestand / Achsen-Drift)? scope='week' oder 'all'. Aendert nichts."""
    from app.services import golive_service
    return golive_service.variant_repair_report(db, scope=scope)


@router.post("/bulk-repair-variants")
async def bulk_repair_variants(scope: str = "week", dry_run: bool = True,
                               limit: int | None = None, db: Session = Depends(get_db)):
    """Bulk-Reparatur der Live-Multivarianten-Listings (Bilder/Bestand/Achsen).

    SICHERHEIT: ``dry_run`` ist STANDARD True -> plant nur, aendert nichts. Fuer den echten
    Lauf explizit ``?dry_run=false``. ``scope=week|all``, ``limit=N`` fuer einen Test-Batch.
    Beendet NIE automatisch (25013 -> nur als 'needs_recreate' gemeldet).
    """
    from app.services import golive_service
    try:
        return await golive_service.bulk_repair_variants(
            db, scope=scope, dry_run=dry_run, limit=limit)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Bulk-Reparatur fehlgeschlagen: {exc}")


@router.post("/{listing_id}/repair-variant-axes")
async def repair_variant_axes(listing_id: int, db: Session = Depends(get_db)):
    """Gedriftete Varianten-Achsen eines Live-Multivarianten-Listings reparieren (Bild-Bug).

    Setzt die Inventory-Item-Group auf die von den Items live getragenen Achsennamen zurueck.
    Geht das nicht in-place (eBay 25013), wird NICHTS beendet -> Antwort mit action_required.
    """
    from app.services import golive_service
    try:
        return await golive_service.repair_variant_axes(db, listing_id=listing_id)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Reparatur fehlgeschlagen: {exc}")


@router.post("/{listing_id}/repair-variant-images")
async def repair_variant_images(listing_id: int, db: Session = Depends(get_db)):
    """CHIRURGISCH IN-PLACE: jede Variante auf genau 1 Bild kuerzen (gleiche Item-ID,
    KEIN neues Listing/Duplikat). Behebt die '8 Standardbilder je Variante'-Altlast."""
    from app.services import golive_service
    try:
        return await golive_service.repair_variant_images(db, listing_id=listing_id)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Bild-Reparatur fehlgeschlagen: {exc}")


@router.post("/silver-regenerate")
async def silver_regenerate(limit: int = 3, db: Session = Depends(get_db)):
    """Die nächsten ``limit`` von 925/Echtsilber/Sterlingsilber betroffenen Listings per KI neu
    formulieren (SEO-Titel + saubere Beschreibung) und live setzen. Läuft im HINTERGRUND (KI/eBay
    dauern lange) – kehrt SOFORT zurück mit der Anzahl betroffener Listings."""
    import asyncio

    from app.services import precious_metal_service
    n = precious_metal_service.count_affected(db)
    batch = min(n, max(1, min(limit, 20)))
    if n:
        asyncio.create_task(precious_metal_service.regenerate_affected_bg(limit=batch))
    return {"started": bool(n), "affected": n, "batch": batch}


@router.get("/{listing_id}/variant-prices")
async def get_variant_prices(listing_id: int, mode: str | None = None, value: float | None = None,
                             db: Session = Depends(get_db)):
    """Preis-Editor: Varianten + Preisvorschau (mode 'margin' %/'profit' €). Rechnet nur.

    Fuer LIVE-Multivarianten-Listings kommen die Zeilen DIREKT aus den echten eBay-Variationen
    (GetItem), damit der spaetere Preis-Write die Variation ohne Heuristik-Matching adressiert.
    """
    from app.services import golive_service
    try:
        return await golive_service.preview_variant_prices(db, listing_id=listing_id,
                                                           mode=mode, value=value)
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{listing_id}/variant-prices")
async def set_variant_prices(listing_id: int, body: dict = Body(...),
                             db: Session = Depends(get_db)):
    """Preise je Variante setzen: entweder mode+value (Marge %/Gewinn €) ODER
    ein explizites {sku: preis}-Mapping. Rundet auf x,95, pusht verifiziert auf eBay."""
    from app.services import golive_service
    prices = body.get("prices")
    mode, value = body.get("mode"), body.get("value")
    if not prices and mode in ("margin", "profit") and value is not None:
        # Aus Modus berechnen (gleiche Rundung + gleiche eBay-getriebene Zeilen wie die Vorschau,
        # damit die Ziel-SKUs exakt denen des Dialogs entsprechen).
        preview = await golive_service.preview_variant_prices(db, listing_id=listing_id,
                                                             mode=mode, value=float(value))
        prices = {r["sku"]: r["new_price_eur"] for r in preview["rows"]
                  if r.get("new_price_eur") is not None}
    if not prices:
        raise HTTPException(status_code=400, detail="Weder Preise noch gueltiger Modus angegeben")
    try:
        return await golive_service.apply_variant_prices(
            db, listing_id=listing_id, price_by_sku={str(k): float(v) for k, v in prices.items()})
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Preis-Push fehlgeschlagen: {exc}")


@router.post("/bulk-reprice")
async def bulk_reprice(body: dict = Body(...), db: Session = Depends(get_db)):
    """Bulk-Marge fuer mehrere Listings auf einmal: Ziel-Marge (mode 'margin' %) ODER
    fixer Gewinn (mode 'profit' €), je Variante aus deren EK gerechnet.

    Body: {listing_ids: [int], mode: 'margin'|'profit', value: float, apply: bool}
    apply=false (Default) -> nur Vorschau (alt->neu). apply=true -> live auf eBay setzen.
    """
    from app.services import golive_service
    ids = body.get("listing_ids") or []
    mode = body.get("mode")
    value = body.get("value")
    apply = bool(body.get("apply"))
    # Eingaben zuerst SAUBER parsen -> Client-Fehler = 400 (nicht 502).
    try:
        ids = [int(x) for x in ids]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="listing_ids müssen ganze Zahlen sein")
    try:
        return await golive_service.bulk_reprice(
            db, listing_ids=ids, mode=mode, value=value, apply=apply)
    except PersistentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Bulk-Marge fehlgeschlagen: {exc}")


@router.post("/{listing_id}/main-image")
async def set_main_image(listing_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    """Ein Bild (Standard- oder Varianten-Bild) zum Hauptbild machen (an Position 1)."""
    from app.services import golive_service
    url = (body.get("image_url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="image_url fehlt")
    try:
        return await golive_service.set_main_image(db, listing_id=listing_id, image_url=url)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Hauptbild setzen fehlgeschlagen: {exc}")


@router.delete("/{listing_id}/variants/{attr}")
def delete_variant(listing_id: int, attr: str, db: Session = Depends(get_db)):
    """Eine einzelne Variante aus dem Produkt entfernen (Nutzer-Aktion)."""
    from app.services import golive_service
    try:
        return golive_service.delete_variant(db, listing_id=listing_id, attr=attr)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{listing_id}/sync-variants")
async def sync_variants(listing_id: int, db: Session = Depends(get_db)):
    """Lokal geloeschte Varianten auf eBay unverkaeuflich machen (ausdrueckliche Nutzer-Aktion).

    Setzt verwaiste Varianten auf Menge 0 und nimmt sie – wenn eBay das noch zulaesst – aus
    der Variantengruppe. Beendet NICHTS am Listing (Regel 7).
    """
    from app.services import golive_service
    try:
        return await golive_service.sync_variants_live(db, listing_id=listing_id)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Varianten-Abgleich fehlgeschlagen: {exc}")


@router.get("/reprice-report")
def get_reprice_report(db: Session = Depends(get_db)):
    """Match-/Neukalkulations-Report (Zoll 07/2026): Quelle, Soll-Preis, Differenz, Temu-Liste."""
    from app.services import listing_match_service
    return listing_match_service.reprice_report(db)


@router.post("/reprice-report/rebuild")
def rebuild_reprice(db: Session = Depends(get_db)):
    """Report frisch aus der DB rechnen (nach Quellen-Aenderungen / EK-Updates)."""
    from app.services import listing_match_service
    return listing_match_service.rebuild_reprice_report(db)


@router.post("/sync-ebay-prices")
async def sync_ebay_prices(db: Session = Depends(get_db)):
    """Echte Live-eBay-Preise je Variante abgleichen (Quelle der Wahrheit fuer die Cockpit-Marge +
    Drift-Warnung). Read-only von eBay; danach den Report neu rechnen."""
    from app.services import ebay_import_service, listing_match_service
    try:
        r = await ebay_import_service.sync_ebay_live_prices(db)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"eBay-Preis-Abgleich fehlgeschlagen: {exc}")
    try:
        listing_match_service.rebuild_reprice_report(db)   # frische Marge/Drift im Report
    except Exception:  # noqa: BLE001 – Rebuild ist best effort
        pass
    return r


@router.get("/{listing_id}/ebay-images")
async def ebay_images(listing_id: int, db: Session = Depends(get_db)):
    """Echte Bilder des LIVE-eBay-Listings (24h-Cache) — fuers Detail-Modal."""
    try:
        return await golive_service.get_ebay_gallery(db, listing_id=listing_id)
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{listing_id}/sync-ebay-price")
async def sync_one_ebay_price(listing_id: int, db: Session = Depends(get_db)):
    """EINEN Artikel SOFORT mit dem echten eBay-Listing-Preis abgleichen (1 GetItem) + Report neu
    rechnen. Sofort-Verifikation der Drift-Anzeige, ohne den langsamen Voll-Abgleich abzuwarten."""
    from app.models import Listing
    from app.services import ebay_import_service, listing_match_service
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="Listing nicht gefunden")
    if not listing.ebay_item_id:
        raise HTTPException(status_code=400, detail="Listing hat keine eBay-Item-ID (nicht live)")
    try:
        r = await ebay_import_service.sync_ebay_live_prices(db, only_ids=[listing_id])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"eBay-Abgleich fehlgeschlagen: {exc}")
    try:
        listing_match_service.rebuild_reprice_report(db)
    except Exception:  # noqa: BLE001 – Rebuild ist best effort
        pass
    db.refresh(listing)
    vals = [v for v in (listing.ebay_live_prices or {}).values() if v is not None]
    return {
        "listing_id": listing_id,
        "checked": r.get("checked", 0), "errors": r.get("errors", []),
        "ebay_price_min_eur": min(vals) if vals else None,
        "ebay_price_max_eur": max(vals) if vals else None,
        "internal_price_eur": float(listing.price_eur) if listing.price_eur is not None else None,
    }


@router.get("/sold-out-center")
def get_sold_out_center(db: Session = Depends(get_db)):
    """Ausverkauf-Center: flache Liste aller ausverkauften Varianten aller aktiven Listings
    mit Zustand (keine Alternative / Backup aus / gerettet / Quelle weg). Read-only."""
    from app.services import listing_match_service
    return listing_match_service.sold_out_center(db)


@router.post("/sync-ad-rates")
async def post_sync_ad_rates(db: Session = Depends(get_db)):
    """Echte Promoted-Listings-Anzeigenraten JETZT von eBay nachziehen (sonst taeglich 02:30).
    Die Gebuehrenkalkulation nutzt danach die tatsaechliche Rate je Listing statt der Pauschale.
    Braucht den sell.marketing-Scope; ist er nicht autorisiert, kommt updated=0 zurueck."""
    from app.services import ad_rate_service
    try:
        return await ad_rate_service.sync_ad_rates(db)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Anzeigenraten-Sync fehlgeschlagen: {exc}")


@router.get("/{listing_id}/find-sources")
async def find_sources(listing_id: int, db: Session = Depends(get_db)):
    """KI-Quellenfinder: Bildsuche + Claude-Bewertung -> sortierte Vorschläge
    (verknüpft nichts; der Nutzer bestätigt im Bild-Vergleichs-Modal)."""
    from app.services import supplier_service
    try:
        return await supplier_service.find_source_candidates(db, listing_id=listing_id)
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Quellensuche fehlgeschlagen: {exc}")


@router.post("/{listing_id}/sources")
async def add_supplier_source(listing_id: int, body: dict = Body(...),
                              db: Session = Depends(get_db)):
    """AliExpress-Link als Lieferanten-Quelle hinterlegen (max 3 Slots je Artikel).

    body: {url, primary?} – primary ersetzt die Hauptquelle (Korrektur falscher Matches).
    """
    from app.services import supplier_service
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url fehlt")
    try:
        result = await supplier_service.add_source(db, listing_id=listing_id, url=url,
                                                   primary=bool(body.get("primary")))
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Quelle konnte nicht geladen werden: {exc}")

    # Nach dem Hinterlegen einer Quelle den Ausverkauft-Stempel DIESES Listings neu bewerten.
    # Ohne das bleibt der Artikel im Cockpit unter „Ausverkauft o. Alt." stehen, obwohl die
    # neue Quelle lieferbar ist: `monitor_status` (Basis von monitor_oos/fully_out) setzt sonst
    # erst das 6h-Monitoring zurueck — ein Report-Rebuild allein hilft nicht, weil er den
    # alten Stempel aus der DB uebernimmt. sync_listing macht KEINE Preis-Pushes (nur EK/
    # Bestand + der erlaubte Ausverkauft-Schutz). Danach den Cockpit-Report auffrischen.
    # Best effort: die Quelle IST gespeichert und committet — ein Fehler hier darf den
    # Erfolg nicht kippen (der Nutzer kann jederzeit „🔄 Synchronisieren" druecken).
    try:
        from app.services import listing_match_service, monitoring_service
        resync = await monitoring_service.sync_listing(db, listing_id=listing_id)
        result["resync_action"] = resync.get("action")
        listing_match_service.rebuild_reprice_report(db)
        result["report_refreshed"] = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("add_source: Resync/Report-Refresh fehlgeschlagen",
                       extra={"listing_id": listing_id, "error": str(exc)[:200]})
        result["report_refreshed"] = False
    return result


@router.delete("/{listing_id}/sources/{aliexpress_id}")
async def delete_supplier_source(listing_id: int, aliexpress_id: str, index: int | None = None,
                                 db: Session = Depends(get_db)):
    """Eine Quelle entfernen. ``index`` (Position) bevorzugt -> entfernt GENAU den
    Slot, eindeutig auch bei doppelten/leeren IDs."""
    from app.services import supplier_service
    try:
        return await supplier_service.remove_source(
            db, listing_id=listing_id, aliexpress_id=aliexpress_id, slot_index=index)
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Entfernen fehlgeschlagen: {exc}")


@router.get("/{listing_id}/sources/{aliexpress_id}/variants")
async def source_variant_options(listing_id: int, aliexpress_id: str,
                                 for_attr: str | None = None, sel: str | None = None,
                                 db: Session = Depends(get_db)):
    """Varianten EINES Quellen-Slots zum Auswaehlen (Bild/Preis/Bestand; scrapt live,
    bestellt NICHTS). Fuer die Ausweich-/Zuordnungs-Picker.

    ``for_attr`` (lokale Varianten-attr) ODER ``sel`` (JSON-Auswahl-Dict) liefert
    zusaetzlich ``suggested_attr`` — die vermutlich passende Quell-Variante."""
    import json as _json
    from app.services import supplier_service
    sel_dict = None
    if sel:
        try:
            parsed = _json.loads(sel)
            sel_dict = parsed if isinstance(parsed, dict) else None
        except ValueError:
            sel_dict = None
    try:
        return await supplier_service.source_variant_options(
            db, listing_id=listing_id, aliexpress_id=aliexpress_id,
            for_attr=for_attr, sel=sel_dict)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Quelle nicht ladbar: {exc}")


@router.get("/variant-name-audit")
async def variant_name_audit(limit: int = 500, db: Session = Depends(get_db)):
    """READ-ONLY Bestands-Scan: Listings, deren eBay-Variationsnamen nicht zur Quelle
    passen (erfundene Namen, Fall Zirkonia-Kette). Aendert nichts."""
    from app.services import golive_service
    try:
        return await golive_service.variant_name_audit(db, limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Scan fehlgeschlagen: {exc}")


@router.post("/{listing_id}/append-variants")
async def append_variants(listing_id: int, db: Session = Depends(get_db)):
    """OPTION A (Namens-Reparatur): korrekt benannte Quell-Varianten ZUSAETZLICH
    anhaengen, falsch benannte auf Menge 0. Nur per Nutzer-Klick."""
    from app.services import golive_service
    try:
        return await golive_service.append_corrected_variants(db, listing_id=listing_id)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Reparatur fehlgeschlagen: {exc}")


@router.post("/{listing_id}/variant-map/auto")
async def auto_variant_map(listing_id: int, db: Session = Depends(get_db)):
    """ALLE offenen eBay-Variationen im Stapel zuordnen (Regeln -> Mass-Matching -> KI
    mit Mass-Guard, fail-closed). Nur Zuordnung/Lernen — bestellt nichts, aendert keine
    Preise. Rueckgabe: {total, already, mapped, open: [Namen]}."""
    from app.services import golive_service
    try:
        return await golive_service.auto_map_ebay_variations(db, listing_id=listing_id)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Zuordnung fehlgeschlagen: {exc}")


@router.post("/{listing_id}/variant-map")
def learn_variant_map(listing_id: int, body: dict = Body(...),
                      db: Session = Depends(get_db)):
    """eBay-Variante MANUELL einer Hauptquellen-SKU zuordnen (lernt listing.variant_map).

    Body: {selection: {merkmal: wert}, attr}. Nur Zuordnung — kein eBay-Call, keine
    Bestellung. Danach kennt der Preis-Dialog den EK der Variante (Stufe C liest die
    gelernte Map) und der Bestell-Flow ordert deterministisch die richtige Ware."""
    from app.models import Listing, Product
    from app.services.order_service import _learn_variant
    selection = body.get("selection")
    attr = (body.get("attr") or "").strip()
    if not (isinstance(selection, dict) and selection and attr):
        raise HTTPException(status_code=400, detail="selection (Dict) und attr erforderlich")
    listing = db.get(Listing, listing_id)
    product = db.get(Product, listing.product_id) if listing and listing.product_id else None
    if product is None:
        raise HTTPException(status_code=404, detail="Kein Produkt zu diesem Listing")
    skus = (product.variants or {}).get("skus") or []
    if not any(s.get("attr") == attr for s in skus):
        raise HTTPException(status_code=409,
                            detail="Diese Quell-Variante existiert (nicht mehr) in der Hauptquelle")
    if not _learn_variant(listing, str(product.aliexpress_id), selection, attr,
                          write_legacy=True):
        raise HTTPException(status_code=400, detail="Auswahl ergibt keinen lernbaren Schluessel")
    db.commit()
    return {"listing_id": listing_id, "attr": attr, "learned": True}


@router.post("/{listing_id}/variant-alt-source")
async def link_variant_alt_source(listing_id: int, body: dict = Body(...),
                                  db: Session = Depends(get_db)):
    """PER-VARIANTE eine Ausweich-Quelle verknuepfen (propose-only, bestellt nichts).

    body: {sku_attr, aliexpress_id, alt_sku_attr?, alt_name?, alt_image?}. Mit
    alt_sku_attr wird die KONKRETE Ziel-Variante der Ausweich-Quelle gemerkt – das
    Bestellen routet dann automatisch dorthin (Klick bleibt die Freigabe). Die
    ausverkaufte Variante bleibt sellbar, solange die Ziel-Variante lieferbar ist;
    der Bestand wird sofort mit eBay abgeglichen."""
    from app.services import monitoring_service, supplier_service
    sku_attr = (body.get("sku_attr") or "").strip()
    aliexpress_id = str(body.get("aliexpress_id") or "").strip()
    if not sku_attr or not aliexpress_id:
        raise HTTPException(status_code=400, detail="sku_attr und aliexpress_id noetig")
    try:
        res = supplier_service.set_variant_alt_source(
            db, listing_id=listing_id, sku_attr=sku_attr, aliexpress_id=aliexpress_id,
            alt_sku_attr=(body.get("alt_sku_attr") or "").strip() or None,
            alt_name=(body.get("alt_name") or "").strip() or None,
            alt_image=(body.get("alt_image") or "").strip() or None)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    try:
        res["resync"] = await monitoring_service.resync_listing_variant_stock(
            db, listing_id=listing_id)
    except Exception as exc:  # noqa: BLE001 – Verknuepfung ist gespeichert; Push best effort
        res["resync_error"] = str(exc)[:160]
    return res


@router.delete("/{listing_id}/variant-alt-source")
async def unlink_variant_alt_source(listing_id: int, sku_attr: str,
                                    db: Session = Depends(get_db)):
    """Per-Variante-Ausweich-Quelle loesen. Ausverkaufte Variante wird sofort auf 0 gesetzt."""
    from app.services import monitoring_service, supplier_service
    try:
        res = supplier_service.clear_variant_alt_source(
            db, listing_id=listing_id, sku_attr=sku_attr)
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        res["resync"] = await monitoring_service.resync_listing_variant_stock(
            db, listing_id=listing_id)
    except Exception as exc:  # noqa: BLE001
        res["resync_error"] = str(exc)[:160]
    return res


@router.post("/{listing_id}/self-stock")
async def set_self_stock(listing_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    """Variante als Eigenbestand markieren. Body: {sku_attr, qty, cost_eur?, price_eur?}.
    Braucht keine Quelle; rechnet mit eigenem EK; lieferbar solange qty > 0.
    Nutzer-Go 08.08.: setzt danach die Versandbedingung DIESES Listings auf die
    schnelle Policy um (Einzelfall, nie pauschal; Ergebnis im Feld fast_policy)."""
    from app.services import supplier_service
    attr = body.get("sku_attr")
    if not attr or body.get("qty") is None:
        raise HTTPException(status_code=400, detail="sku_attr und qty erforderlich")
    try:
        res = supplier_service.set_self_stock(
            db, listing_id=listing_id, sku_attr=attr, qty=body["qty"],
            cost_eur=body.get("cost_eur"), price_eur=body.get("price_eur"))
        try:
            from app.integrations import get_ebay_client
            from app.models import Listing
            from app.services import fast_shipping_service
            l = db.get(Listing, listing_id)
            if (l is not None and l.ebay_item_id
                    and fast_shipping_service._has_self_stock(l)):  # noqa: SLF001
                res["fast_policy"] = await fast_shipping_service.apply_fast_policy_to_listing(
                    db, get_ebay_client(), l)
        except Exception as exc:  # noqa: BLE001 – Umstellung ist best effort
            res["fast_policy"] = {"switched": False, "note": str(exc)[:120]}
        return res
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/{listing_id}/self-stock")
def clear_self_stock(listing_id: int, sku_attr: str, db: Session = Depends(get_db)):
    """Eigenbestand einer Variante entfernen -> faellt zurueck auf die AliExpress-Quelle."""
    from app.services import supplier_service
    try:
        return supplier_service.clear_self_stock(db, listing_id=listing_id, sku_attr=sku_attr)
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/{listing_id}/alternatives/{aliexpress_id}")
def delete_alternative(listing_id: int, aliexpress_id: str, index: int | None = None,
                       db: Session = Depends(get_db)):
    """Falschen Bildsuche-Vorschlag entfernen. ``index`` bevorzugt (eindeutig)."""
    from app.services import supplier_service
    try:
        return supplier_service.remove_alternative_item(
            db, listing_id=listing_id, aliexpress_id=aliexpress_id, item_index=index)
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{listing_id}/end")
async def end_listing(listing_id: int, db: Session = Depends(get_db)):
    """Artikel LÖSCHEN/BEENDEN: auf eBay delisten + lokal auf 'ended' setzen."""
    from app.services import golive_service
    try:
        return await golive_service.end_listing_live(db, listing_id=listing_id)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Beenden fehlgeschlagen: {exc}")


@router.post("/{listing_id}/sources/{aliexpress_id}/primary")
async def make_source_primary(listing_id: int, aliexpress_id: str,
                              db: Session = Depends(get_db)):
    from app.services import supplier_service
    try:
        return await supplier_service.set_primary(db, listing_id=listing_id,
                                                  aliexpress_id=aliexpress_id)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Quelle nicht ladbar: {exc}")


@router.post("/{listing_id}/sources/suggestion-images")
async def enrich_suggestion_images(listing_id: int, db: Session = Depends(get_db)):
    """Fehlende Bilder der Bildsuche-Vorschläge on-demand nachladen (gecacht)."""
    from app.services import supplier_service
    try:
        return await supplier_service.enrich_suggestion_images(db, listing_id=listing_id)
    except PersistentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Bilder nicht ladbar: {exc}")


@router.post("/{listing_id}/sources/refresh")
async def refresh_supplier_sources(listing_id: int, db: Session = Depends(get_db)):
    """Alle Quellen-Slots dieses Listings sofort neu prüfen (Preis + Bestand)."""
    from app.services import supplier_service
    try:
        return await supplier_service.refresh_sources(db, listing_id=listing_id)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{listing_id}/sources/confirm")
def confirm_supplier_match(listing_id: int, db: Session = Depends(get_db)):
    """Nutzer bestätigt: AliExpress-Quelle passt zum eBay-Artikel (⚠-Verdacht weg)."""
    from app.services import supplier_service
    try:
        return supplier_service.confirm_match(db, listing_id=listing_id)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/categories/backfill")
async def categories_backfill(limit: int = 800, force: bool = False,
                              db: Session = Depends(get_db)):
    """Fehlende eBay-Kategorie je aktivem Listing nachtragen (Basis für die
    kategoriegenaue Provision). Liest nur bei eBay, ändert kein Listing."""
    from app.services import ebay_import_service
    try:
        return await ebay_import_service.backfill_categories(db, limit=limit, force=force)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Kategorie-Nachtrag fehlgeschlagen: {exc}")


@router.get("/categories/overview")
def categories_overview(db: Session = Depends(get_db)):
    """Welche Kategorien haben unsere aktiven Listings und wie weit weicht die
    kategoriegenaue Provision vom Default ab? (reine Auswertung)"""
    from app.services import ebay_import_service
    return ebay_import_service.category_fee_overview(db)


@router.post("/shipping/scan")
async def shipping_scan(force: bool = False, db: Session = Depends(get_db)):
    """Versand-Policy aller aktiven Listings lesen (Basis für den 3€-Filter)."""
    from app.services import ebay_import_service
    try:
        return await ebay_import_service.scan_shipping_policies(db, force=force)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Versand-Scan fehlgeschlagen: {exc}")


@router.post("/{listing_id}/shipping-policy")
async def switch_shipping(listing_id: int, body: dict = Body(default={}),
                          db: Session = Depends(get_db)):
    """Versand-Policy dieses Listings wechseln (Default: Kostenloser Versand)."""
    from app.services import ebay_import_service
    try:
        return await ebay_import_service.switch_listing_shipping(
            db, listing_id=listing_id, policy_id=body.get("policy_id"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Versand-Wechsel fehlgeschlagen: {exc}")


@router.post("/{listing_id}/name-search")
async def name_search(listing_id: int, body: dict = Body(default={}),
                      db: Session = Depends(get_db)):
    """Zweiter Match-Weg: AliExpress-Textsuche nach Artikelname (z.B. 'HTC NE70')."""
    from app.services import supplier_service
    try:
        return await supplier_service.name_search_candidates(
            db, listing_id=listing_id, query=body.get("query"))
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Suche fehlgeschlagen: {exc}")


@router.get("/shipping/policies")
async def shipping_policies():
    """Versand-Policies inkl. Kosten – zeigt, wo der 3€-Zuschlag noch aktiv ist."""
    from app.config import get_settings
    from app.integrations.ebay import RealEbayClient
    try:
        return {"policies": await RealEbayClient(get_settings()).list_fulfillment_policies()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Policies nicht lesbar: {exc}")


@router.post("/shipping/free")
async def shipping_make_free(body: dict = Body(default={})):
    """3€-Versandzuschlag entfernen: genannte (oder alle belasteten) Policies auf
    kostenlos stellen. Wirkt SOFORT auf alle Listings mit diesen Policies."""
    from app.config import get_settings
    from app.integrations.ebay import RealEbayClient
    ebay = RealEbayClient(get_settings())
    try:
        ids = body.get("policy_ids")
        if not ids:
            ids = [p["policy_id"] for p in await ebay.list_fulfillment_policies()
                   if p["has_surcharge"]]
        results = [await ebay.make_fulfillment_policy_free(pid) for pid in ids]
        return {"updated": results}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Policy-Update fehlgeschlagen: {exc}")


@router.post("/{listing_id}/alternatives")
async def find_listing_alternatives(listing_id: int, db: Session = Depends(get_db)):
    """Alternative Händler fürs Produkt per Bildsuche finden (Ausfall-Absicherung + Preisvergleich)."""
    from app.config import get_settings
    from app.integrations.aliexpress import RealAliExpressClient
    from app.models import Listing, Product
    from app.services import pricing
    listing = db.get(Listing, listing_id)
    product = db.get(Product, listing.product_id) if listing and listing.product_id else None
    if product is None or not (product.images or []):
        raise HTTPException(status_code=404, detail="Kein Produkt/Bild zu diesem Listing")
    s = get_settings()
    ae = RealAliExpressClient(s)
    try:
        img = (await ae._http().get(product.images[0], timeout=20)).content
        hits = await ae.image_search(img, page_size=10)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Bildsuche fehlgeschlagen: {exc}")
    alts = []
    for h in hits:
        if h["aliexpress_id"] == (product.aliexpress_id or ""):
            continue
        alts.append({"aliexpress_id": h["aliexpress_id"], "url": h["url"],
                     "title": h.get("title"), "store_url": h.get("store_url"),
                     "price_eur": h.get("price_eur"),
                     "ek_eur": round(pricing.effective_cost(h["price_eur"], settings=s), 2)
                               if h.get("price_eur") else None})
        if len(alts) >= 6:
            break
    prices = [a["price_eur"] for a in alts if a["price_eur"]]
    if product.price_cny:
        prices.append(float(product.price_cny))
    avg = round(sum(prices) / len(prices), 2) if prices else None
    # MERGE statt Ueberschreiben: die Quellen-Slots (sources/avg_ek_eur/confirmed)
    # aus supplier_service liegen im selben Dict und muessen erhalten bleiben.
    alt = dict(product.alternatives) if isinstance(product.alternatives, dict) else {}
    alt["items"] = alts
    alt["avg_price_eur"] = avg
    product.alternatives = alt
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(product, "alternatives")
    db.commit()
    return {"listing_id": listing_id, "count": len(alts), "avg_price_eur": avg,
            "alternatives": alts}


@router.post("/{listing_id}/promote")
async def promote(listing_id: int, body: dict = Body(default={}), db: Session = Depends(get_db)):
    """Anzeigentarif (Promoted Listings) fuer ein Live-Listing setzen ODER ANHEBEN.

    body {"rate_pct": 15} = Ziel-Tarif in Prozent (Default: config 10%). Legt eine Ad
    an oder hebt eine bestehende an (create-or-update).
    """
    from app.config import get_settings
    from app.integrations.ebay import RealEbayClient
    from app.models import Listing
    listing = db.get(Listing, listing_id)
    if listing is None or not listing.ebay_item_id:
        raise HTTPException(status_code=404, detail="Listing nicht gefunden oder nicht live")
    rate = body.get("rate_pct")
    bid = (float(rate) / 100.0) if rate is not None else None
    try:
        return await RealEbayClient(get_settings()).set_ad_rate(
            [listing.ebay_item_id], bid_pct=bid if bid is not None else get_settings().ebay_ad_rate_pct)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Anzeigen-Setup fehlgeschlagen: {exc}")


@router.post("/{listing_id}/ai-edit")
async def ai_edit(listing_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    """KI-Bearbeitung nach Freitext-Anweisung – liefert einen VORSCHLAG (propose-only)."""
    instruction = (body.get("instruction") or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="instruction fehlt")
    try:
        return await product_service.ai_edit_listing(
            db, listing_id=listing_id, instruction=instruction)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"KI-Bearbeitung fehlgeschlagen: {exc}")


@router.post("/{listing_id}/regenerate")
async def regenerate(listing_id: int, db: Session = Depends(get_db)):
    """Entwurf frisch von AliExpress holen + mit aktuellem (verbessertem) LLM neu aufbauen."""
    try:
        return await product_service.regenerate_listing(db, listing_id=listing_id)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Neu-Generierung fehlgeschlagen: {exc}")


@router.delete("/{listing_id}/draft")
async def delete_draft(listing_id: int, db: Session = Depends(get_db)):
    """Entwurf endgültig löschen (Nutzer-Aktion; aktive Listings sind geschützt)."""
    from app.services import golive_service
    try:
        return await golive_service.delete_draft(db, listing_id=listing_id)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Löschen fehlgeschlagen: {exc}")


def _klick_freigeben(listing_id: int) -> bool:
    """Ein Mensch hat bewusst geklickt: dieses eine Listing darf einmal raus.

    Frueher stand hier ``_attrappe_sperrt_ebay()`` und wies im Probebetrieb JEDEN
    Klick mit 409 ab. Das war zu grob. Nutzerwunsch vom 01.09.2026, woertlich:
    "mock ebay kann ja gerne auf true bleiben, wennich ein entwurf live schalten
    will dann soll ich das doch duerfen."

    Der Probebetrieb behaelt seinen Zweck - er soll verhindern, dass etwas OHNE
    Zutun rausgeht. Genau das bleibt: die Freigabe gilt fuer ein einzelnes
    Listing, wird beim ersten Zugriff verbraucht und ist nirgends gespeichert.
    Der Selbstheilungs-Job, der alle 20 Minuten dieselbe Warteschlange fuettert,
    bekommt keine und laeuft weiter gegen die Sperre.

    Rueckgabe: ob der Probebetrieb ueberhaupt lief (nur fuer die Rueckmeldung an
    die Oberflaeche - bei MOCK_EBAY=false ist die Freigabe wirkungslos, weil dann
    ohnehin nichts sperrt).
    """
    from app.config import get_settings
    from app.services import freigabe

    freigabe.erteile(listing_id)
    return bool(get_settings().use_mock("ebay"))


@router.post("/{listing_id}/ebay-draft", status_code=200)
async def ebay_entwurf(listing_id: int, db: Session = Depends(get_db)):
    """Angebot bei eBay VORBEREITEN - ohne zu veroeffentlichen.

    Legt Inventory-Item und Offer bei eBay an, haelt aber vor dem
    Veroeffentlichen an. Das Angebot ist fuer Kaeufer nicht sichtbar.

    WAS DAS NICHT IST (Befund 03.09.2026): Es ist KEIN Entwurf im Seller Hub.
    eBay bietet keine Schnittstelle, um dort Entwuerfe anzulegen; ueber die
    Inventory-API erzeugte Angebote erscheinen in der Entwurfsliste NIE - erst
    das Veroeffentlichen macht sie sichtbar. Frueher hiess der Knopf
    "eBay-Entwurf" und die Meldung sagte "in deinem eBay-Konto unter Entwuerfe".
    Der Betreiber hat dort gesucht und nichts gefunden, obwohl drei Angebote
    nachweislich als UNPUBLISHED bei eBay lagen.

    Nachpruefbar bleibt es trotzdem: Die Offer-Nummer steht in ``ebay_draft``
    und laesst sich ueber ``getOffers`` abfragen.

    Die Logik dafuer gab es laengst (``draft_only=True``), sie war nur ueber
    keine Adresse erreichbar - der einzige Knopf zu eBay veroeffentlichte
    sofort. Das war fuer einen ersten Versuch zu scharf.
    """
    from app.services import freigabe, golive_service

    # Anders als der Live-Weg laeuft der Entwurf DIREKT in dieser Anfrage, nicht
    # ueber die Warteschlange. Das Tor muss deshalb hier auf - der Worker, der es
    # sonst oeffnet, ist gar nicht beteiligt.
    _klick_freigeben(listing_id)
    try:
        with freigabe.beim_veroeffentlichen(listing_id):
            ergebnis = await golive_service.publish_listing_live(
                db, listing_id=listing_id, draft_only=True
            )
    except PersistentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Entwurf fehlgeschlagen: {exc}") from exc
    return ergebnis


@router.get("/startklar")
def startklar_bericht(nur_entwuerfe: bool = True, limit: int = 500,
                      db: Session = Depends(get_db)):
    """Was passiert, wenn diese Entwuerfe live gehen? Der Blick VOR dem Klick.

    Aendert NICHTS und ruft eBay NICHT an - reine Lesepruefung, deshalb GET.

    Bewusst getrennt vom Trockenlauf in ``/publish-all-drafts?dry_run=true``:
    der zaehlt nur, ob ein Preis gesetzt ist, und meldet darum auch dann
    "bereit", wenn Bilder fehlen oder die Marge nicht traegt. Diese Route sagt
    je Listing, was wirklich im Weg steht - rot heisst "wuerde scheitern",
    gelb heisst "geht durch, sieh es dir trotzdem an".
    """
    from app.services import startklar

    return startklar.bericht(db, nur_entwuerfe=nur_entwuerfe, limit=limit)


@router.post("/publish-all-drafts", status_code=202)
async def publish_all_drafts(dry_run: bool = True, bestaetigt: bool = False,
                             limit: int = 500, db: Session = Depends(get_db)):
    """ALLE Entwuerfe live stellen — einer nach dem anderen ueber die Warteschlange.

    Uebernommen aus dem Original-Projekt (eBay-Automation), das diesen Weg schon hat;
    unsere Kopie ist aelter. Bei dreissig Entwuerfen sind dreissig Einzelklicks keine
    zumutbare Bedienung.

    Diese Route steht bewusst VOR /{listing_id}/publish - sonst versuchte FastAPI,
    "publish-all-drafts" als listing_id zu lesen.

    ZWEISTUFIG, aus gutem Grund: ``dry_run=true`` (Vorgabe) reiht NICHTS ein, sondern
    meldet nur, wie viele startklar sind. Live-Stellen kostet eBay-Einstellgebuehren
    und laesst sich nicht per Klick zurueckdrehen — das Beenden von Listings ist
    bewusst gesperrt (Eiserne Regel 2). Der echte Lauf verlangt deshalb ZUSAETZLICH
    ``bestaetigt=true``, genau wie der Einzel-Knopf.
    """
    from app.services import publish_queue
    if not dry_run and not bestaetigt:
        raise HTTPException(
            status_code=428,
            detail=("Sammel-Live-Gang braucht bestaetigt=true. Erst den Trockenlauf "
                    "ansehen (dry_run=true), dann bestaetigen — Einstellgebuehren "
                    "fallen sofort an und sind nicht zurueckdrehbar."))
    try:
        # ``freigeben`` nur beim bestaetigten Echtlauf: dann - und nur dann -
        # bekommt jedes eingereihte Listing die einmalige Schreibfreigabe, die
        # es im Probebetrieb durch die Sperre laesst.
        return await publish_queue.enqueue_all_drafts(
            db, dry_run=dry_run, limit=limit, freigeben=bool(bestaetigt))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Massen-Einreihen fehlgeschlagen: {exc}")


@router.post("/{listing_id}/publish", status_code=202)
async def publish(listing_id: int, bestaetigt: bool = False, db: Session = Depends(get_db)):
    """Draft-Listing live stellen — laeuft in der Publish-WARTESCHLANGE.

    Antwortet sofort (202). Der Entwurf bleibt bei Fehlern erhalten; transiente
    Fehler werden automatisch wiederholt, harte Fehler stehen als publish_error
    am Listing (Dashboard zeigt sie mit Retry-Knopf).

    **Braucht ``bestaetigt=true``.** Veroeffentlichen ist nach aussen gerichtet und
    kaum zurueckzuholen: Das Angebot ist sofort oeffentlich sichtbar, und die
    Warteschlange wiederholt den Versuch bis zu fuenf Mal. Ein einzelner Fehlklick
    - oder ein versehentlicher Aufruf - soll das nicht ausloesen koennen. Wer nur
    schauen will, wie das Angebot bei eBay aussieht, nimmt
    ``POST /{listing_id}/ebay-draft``: gleicher Aufbau, aber unveroeffentlicht.
    """
    if not bestaetigt:
        raise HTTPException(
            status_code=428,
            detail=("Veroeffentlichen muss ausdruecklich bestaetigt werden "
                    "(bestaetigt=true). Zum gefahrlosen Ansehen: /ebay-draft."),
        )

    _klick_freigeben(listing_id)
    from app.services import publish_queue
    try:
        return await publish_queue.enqueue(listing_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Einreihen fehlgeschlagen: {exc}")
