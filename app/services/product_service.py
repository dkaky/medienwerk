"""Bereich 1: Produktupload (Spec Kap. 5.1).

URL-Validierung -> Datenextraktion -> LLM-Titel/Beschreibung -> AutoDS-Draft
-> Speichern -> Warteschlange fuer manuelle Sichtkontrolle.
"""
from __future__ import annotations

import asyncio
import logging
import re
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations import (
    get_aliexpress_client,
    get_ebay_client,
    get_listing_client,
    get_llm_client,
)
from app.integrations import aliexpress_api as _ae_api
from app.integrations.native_listing import make_sku
from app.models import Listing, PriceHistory, Product
from app.retry import PersistentError, TransientError, retry_async
from app.services import pricing, sprachfilter, variantenkennung
from app.services.common import task_log

logger = logging.getLogger("app.services.product")

_ALIEXPRESS_RE = re.compile(r"^https?://([a-z0-9-]+\.)*aliexpress\.(com|us|ru)/", re.IGNORECASE)

# So viele Titelanfaenge gehen hoechstens in den Prompt. Mehr hilft der KI nicht bei
# der Wahl und kostet nur Tokens; die haeufigsten stehen durch die Sortierung vorn.
_MAX_ANFAENGE = 15


def vergebene_titelanfaenge(db: Session, *, ausser_listing: int | None = None,
                            ausser_produkt: int | None = None) -> list[str]:
    """Erste Woerter der schon vorhandenen Titel – haeufigste zuerst.

    Der System-Prompt verlangt seit jeher, das Einstiegs-Keyword zwischen aehnlichen
    Artikeln zu variieren. Befolgen konnte die KI das nie: jedes Produkt entsteht in
    einem EIGENEN Aufruf, der die anderen nicht kennt, also haelt sich jeder Aufruf
    fuer den ersten. Beim Shop-Import am 27.08.2026 fingen darum alle zehn Titel mit
    "T-Shirt" an - zehn eigene Angebote, die in der eBay-Suche gegeneinander laufen
    statt gegen fremde.

    Frisch je Produkt lesen, nicht einmal vorab: der Shop-Import laeuft seriell, also
    sieht das zweite Produkt die eben getroffene Wahl des ersten und weicht ihr aus.
    Vorab eingesammelt waeren alle zehn wieder gleich.
    """
    q = select(Listing.title_seo).where(Listing.title_seo.isnot(None))
    if ausser_listing is not None:
        q = q.where(Listing.id != ausser_listing)
    if ausser_produkt is not None:
        # Beim Auffrischen eines vorhandenen Produkts darf sein EIGENER Titel nicht
        # als vergeben gelten - sonst muss er bei jedem Import ein neues Anfangswort
        # suchen und wandert immer weiter weg vom passendsten.
        q = q.where(Listing.product_id != ausser_produkt)
    zaehler: dict[str, int] = {}
    for (titel,) in db.execute(q):
        erstes = (titel or "").strip().split(" ")[0].strip(",;:")
        # Mengenangaben wie "3er", "2x", "10" sind keine Einstiegswoerter, sondern
        # stehen VOR dem Produktwort. Sie zu meiden brächte nichts - der naechste
        # Titel duerfte dann keine Menge mehr nennen.
        if len(erstes) > 1 and not erstes[0].isdigit():
            zaehler[erstes] = zaehler.get(erstes, 0) + 1
    return [w for w, _ in sorted(zaehler.items(), key=lambda kv: -kv[1])][:_MAX_ANFAENGE]


async def resolve_supplier_ship_eur(ae, aliexpress_url: str | None, *,
                                    sku_id: str | None = None) -> Optional[float]:
    """Echte AliExpress-Versandkosten (€) eines Produkts via freight query, oder ``None``.

    ``None`` bei fehlender URL/Produkt-ID oder wenn der Lieferant keine Option liefert –
    die Preiskalkulation faellt dann sauber auf die Pauschal-Schaetzung (1,99 €) zurueck.
    Nie werfend: ein Versand-Query darf einen Upload/Reprice nie scheitern lassen.
    """
    pid = _ae_api.extract_product_id(aliexpress_url or "")
    if not pid:
        return None
    try:
        fr = await ae.query_freight(product_id=pid, sku_id=sku_id)
    except Exception:  # noqa: BLE001 – Versand unbekannt -> Pauschale
        return None
    return fr.get("fee_eur") if fr else None


async def backfill_supplier_ship(db: Session, *, only_missing: bool = True,
                                 recompute_cost: bool = True, limit: int | None = None) -> dict:
    """Echte AliExpress-Versandkosten fuer bestehende Listings nachtragen (einmalige Pflege).

    Fuer jedes (aktive/Entwurf) Listing mit AliExpress-Produkt wird die freight query
    ausgefuehrt und ``supplier_ship_eur`` gesetzt; optional ``cost_eur`` mit dem echten
    Versand neu berechnet, damit Preis-Check/Optimieren sofort korrekt rechnen. Der
    Netz-Call laeuft je Listing OHNE offene Schreibsperre – erst danach ein kurzer Commit
    (Lock-Regel [[sqlite-lock-lesson]]).
    """
    ae = get_aliexpress_client()
    settings = get_settings()
    q = select(Listing).where(Listing.listing_status.in_(("active", "draft")))
    if only_missing:
        q = q.where(Listing.supplier_ship_eur.is_(None))
    listings = list(db.scalars(q).all())
    if limit:
        listings = listings[:limit]
    updated, freed, none_cnt, errors = 0, 0, 0, 0
    for listing in listings:
        product = db.get(Product, listing.product_id) if listing.product_id else None
        url = getattr(product, "aliexpress_url", None)
        if not url:
            none_cnt += 1
            continue
        try:
            fee = await resolve_supplier_ship_eur(ae, url)   # Netz-Call, keine offene Txn
        except Exception:  # noqa: BLE001
            errors += 1
            continue
        if fee is None:
            none_cnt += 1
            continue
        listing.supplier_ship_eur = Decimal(str(fee))
        if recompute_cost and product is not None and product.price_cny is not None:
            # EU-Lager mitgeben - sonst schlaegt diese Pflege-Funktion Zoll und
            # China-Versand auf Ware aus einem deutschen Lager auf und ueberschreibt
            # damit einen vorher korrekten EK. Sie war die LETZTE Stelle im Projekt,
            # die cost_eur ohne diese Pruefung schrieb (Durchsicht 30.08.2026).
            from app.services.fast_shipping_service import variants_have_eu_warehouse
            listing.cost_eur = Decimal(str(pricing.effective_cost(
                product.price_cny, settings=settings, ship_override=fee,
                local=variants_have_eu_warehouse(product))))
        db.commit()
        updated += 1
        if fee == 0.0:
            freed += 1
    return {"listings": len(listings), "updated": updated, "free_shipping": freed,
            "unknown": none_cnt, "errors": errors}


def validate_url(url: str) -> None:
    """Step 1: RegEx-Check auf AliExpress-Domain."""
    if not _ALIEXPRESS_RE.match(url or ""):
        raise PersistentError("URL ist keine gueltige AliExpress-URL")


def _apply_variant_names(variants: dict | None, mapping: dict | None) -> dict | None:
    """Deutsche Varianten-Namen (LLM-Mapping) auf axes + sku-Optionen anwenden."""
    if not variants or not isinstance(variants, dict) or not mapping:
        return variants
    ax_map = mapping.get("axes") or {}
    val_map = mapping.get("values") or {}
    new_axes: dict = {}
    for axis, vals in (variants.get("axes") or {}).items():
        vm = val_map.get(axis) or {}
        new_vals = [vm.get(str(v), v) for v in vals]
        if len(set(map(str, new_vals))) != len(new_vals):  # Kollision -> Original behalten
            new_vals = vals
            vm = {}
        new_axes[ax_map.get(axis, axis)] = new_vals
        val_map[axis] = vm
    new_skus = []
    for sku in variants.get("skus") or []:
        opts = {}
        for axis, val in (sku.get("options") or {}).items():
            vm = val_map.get(axis) or {}
            opts[ax_map.get(axis, axis)] = vm.get(str(val), val)
        new_skus.append({**sku, "options": opts})
    return {**variants, "axes": new_axes, "skus": new_skus}


async def _germanized_variants(llm, scraped) -> dict | None:
    """Varianten-Namen best effort eindeutschen (Fehler -> Original)."""
    variants = scraped.variants
    axes = (variants or {}).get("axes") if isinstance(variants, dict) else None
    if not axes:
        return variants
    try:
        mapping = await llm.normalize_variants(axes, scraped.title_raw)
        return _apply_variant_names(variants, mapping)
    except Exception:  # noqa: BLE001 – Politur darf den Upload nie brechen
        return variants


class DuplikatListing(PersistentError):
    """Das Produkt steht nach unserem Stand noch als LIVE-Listing auf eBay.

    Traegt die blockierende Listing-ID mit, damit das Dashboard einen bewussten
    „Trotzdem hochladen"-Knopf anbieten kann statt nur eine Sackgasse zu melden.
    """

    def __init__(self, message: str, *, listing_id: int, listing_status: str) -> None:
        super().__init__(message)
        self.listing_id = listing_id
        self.listing_status = listing_status


def _lebendes_listing(listings) -> Listing | None:
    """Das Listing, das einen erneuten Upload wirklich sperren darf – oder None.

    Frueher genuegte eine eBay-Artikelnummer an IRGENDEINER Zeile. Die bleibt aber
    fuer immer stehen, auch nachdem Wajjahat das Listing beendet oder geloescht hat
    – der Artikel liess sich danach nie wieder anlegen, obwohl auf eBay laengst
    nichts mehr stand (Vorfall 21.08.). Es zaehlt deshalb nur, was nach unserem
    Stand noch aktiv ist. Ist unser Stand veraltet, entscheidet der Nutzer per Knopf.
    """
    return next((l for l in listings
                 if l.ebay_item_id and (l.listing_status or "") == "active"), None)


# Max. 2 Uploads gleichzeitig: schuetzt das AliExpress-API-Kontingent (Test-Tier
# drosselt Bursts -> Timeout-Stuerme) und haelt DB-Sessions kurz. Weitere Uploads
# warten hier geordnet, statt parallel zu scheitern.
_UPLOAD_SEMAPHORE = asyncio.Semaphore(2)


async def upload_product(db: Session, *, aliexpress_url: str, skip_autods: bool = False,
                         ignoriere_duplikat: bool = False) -> dict:
    """Vollstaendiger Upload-Workflow. Gibt die Felder fuer ProductUploadResponse zurueck.

    ``ignoriere_duplikat``: bewusste Nutzer-Entscheidung, denselben Artikel trotz
    eines noch als live gefuehrten Listings erneut anzulegen. Nur ueber den Knopf
    im Dashboard – nie automatisch, denn zwei gleiche Artikel gleichzeitig live
    mag eBay nicht.
    """
    validate_url(aliexpress_url)
    async with _UPLOAD_SEMAPHORE:
        return await _upload_product_now(db, aliexpress_url=aliexpress_url,
                                         skip_autods=skip_autods,
                                         ignoriere_duplikat=ignoriere_duplikat)


async def _upload_product_now(db: Session, *, aliexpress_url: str, skip_autods: bool = False,
                              ignoriere_duplikat: bool = False) -> dict:

    # Step 1b: bereits in DB? Duplikat-Erkennung per URL **und** per AliExpress-ID – dieselbe Ware
    # kommt oft über leicht andere URLs (Query-Params/Mirror), die reine URL-Prüfung verfehlte sie
    # und der spätere INSERT crashte am aliexpress_id-UNIQUE (Vorfall 21.07.). Existiert das Produkt
    # NUR als Entwurf (nie auf eBay veröffentlicht), wird es beim erneuten Import IN PLACE mit den
    # frischen Daten (inkl. korrigierter Größen) aktualisiert statt zu duplizieren oder zu crashen.
    pid = _ae_api.extract_product_id(aliexpress_url) or ""
    existing = db.scalar(select(Product).where(Product.aliexpress_url == aliexpress_url))
    if existing is None and pid:
        existing = db.scalar(select(Product).where(Product.aliexpress_id == pid))
    if existing is not None and not ignoriere_duplikat:
        live = _lebendes_listing(existing.listings)
        if live is not None:
            raise DuplikatListing(
                f"Produkt ist als Listing #{live.id} auf eBay live – normalerweise dort über "
                f"„Preise anpassen“ / Bearbeiten korrigieren statt neu zu importieren.",
                listing_id=live.id, listing_status=live.listing_status or "")
    refresh = existing is not None
    # Einen eBay-Entwurf brauchen wir immer dann, wenn wir KEINEN vorhandenen
    # wiederverwenden. Das hing frueher an ``refresh`` – dann bekam ein zweiter
    # Entwurf neben einem beendeten Listing gar keine Draft-ID und liess sich
    # spaeter nicht live stellen (der Knopf haette einen toten Entwurf erzeugt).
    hat_entwurf = existing is not None and any(
        (l.listing_status or "") == "draft" for l in existing.listings)

    ae = get_aliexpress_client()
    llm = get_llm_client()
    backend = get_listing_client()
    settings = get_settings()

    with task_log(db, task_type="upload", reference_id=aliexpress_url) as tl:
        # Step 2: Datenextraktion (mit Retry bei transienten Fehlern)
        scraped = await retry_async(
            lambda: ae.scrape_product(aliexpress_url), label="scrape", max_retries=3
        )
        # SPRACHE: nur deutsche und englische Ware. Die Pruefung steht bewusst
        # HIER - vor dem LLM-Aufruf, vor jedem Schreibzugriff. Ein abgelehntes
        # Produkt soll nichts kosten und nichts hinterlassen.
        # Sie lehnt nur bei Beweis ab; im Zweifel geht die Ware durch. Warum, steht
        # im Modulkopf von app/services/sprachfilter.py.
        sprachfilter.pruefe(getattr(scraped, "title_raw", None),
                            getattr(scraped, "description_raw", None))
        variants_de = await _germanized_variants(llm, scraped)
        # WICHTIG (Lock-Vermeidung, Vorfall 10.07.): ALLE langsamen Netz-/LLM-Calls laufen
        # ZUERST – OHNE offene DB-Schreibtransaktion. Frueher wurde direkt nach db.flush()
        # der LLM-Call gemacht und die SQLite-Schreibsperre 20-40 s ueber ihn gehalten ->
        # jede parallele Anlage lief in "database is locked". Erst danach wird kurz & am
        # Stueck in die DB geschrieben.

        # Step 3: LLM-Titel + Beschreibung (Fallback auf Default bei Fehler)
        # Eigennamen VORAB erkennen (eigener enger Call) und als Pflicht mitgeben – die
        # Regel im grossen Prompt allein reichte nicht (Vorfall 03.08.: "Boehse Onkelz"
        # wurde zu "Punk Rock" verallgemeinert, das Listing war nicht mehr auffindbar).
        try:
            pflicht_namen = await llm.extract_names(title_raw=scraped.title_raw)
        except Exception:  # noqa: BLE001 – Namenserkennung darf den Upload nie stoppen
            pflicht_namen = []
        # AUFDRUCK VOM BILD. Bei Motiv-Bekleidung steht der Spruch fast immer NUR
        # dort - im Rohtitel und in der Beschreibung taucht er nicht auf. Ohne ihn
        # kann die Titelregel ihn nicht in den Titel schreiben und darf ihn nicht
        # erfinden; die Titel hiessen dann "Baumwolle O-Ausschnitt" statt
        # "Team Lecker Bierchen" (Befund 03.09.2026 an 27 von 28 Entwuerfen).
        #
        # Er wandert als Pflichtangabe zu den Namen: dieselbe Zusage gilt schon
        # fuer Eigennamen, und das Modell behandelt beide gleich.
        # Faellt die Analyse aus, geht der Upload ohne sie weiter - ein
        # schlechterer Titel ist besser als ein abgebrochener Import.
        try:
            gelesen = await llm.lies_aufdruck(product_title=scraped.title_raw,
                                              image_urls=list(scraped.images or []))
            if gelesen.get("text") and gelesen.get("sicher") and not gelesen.get("fehler"):
                pflicht_namen = list(pflicht_namen) + [gelesen["text"][:120]]
                logger.info("Aufdruck vom Bild gelesen: %r", gelesen["text"][:60])
        except Exception as exc:  # noqa: BLE001 – darf den Upload nie stoppen
            logger.warning("Aufdruck-Analyse uebersprungen: %s", str(exc)[:120])
        # EINMAL vor der Wiederholungsschleife lesen, nicht im lambda: dort liefe die
        # Abfrage bei jedem der drei Versuche erneut, und die Liste koennte sich
        # zwischen zwei Versuchen aendern. Ausserdem gilt hier die Regel von oben -
        # vor dem LLM-Aufruf keine DB-Arbeit mehr als noetig.
        anfaenge = vergebene_titelanfaenge(
            db, ausser_produkt=existing.id if existing is not None else None)
        try:
            gen = await retry_async(
                lambda: llm.generate_listing(
                    title_raw=scraped.title_raw,
                    description_raw=scraped.description_raw,
                    category_guess=None,
                    specs=scraped.specs,
                    variant_axes=(variants_de or {}).get("axes"),
                    pflicht_namen=pflicht_namen,
                    vergebene_anfaenge=anfaenge,
                ),
                label="llm",
                max_retries=3,
            )
            title_seo, description, warnings = gen.title_seo, gen.description_clean, list(gen.warnings)
            from app.brand_filter import (ensure_names_in_title,
                                          ensure_variant_values_in_description,
                                          source_haystack, verify_brand)
            from app.spec_filter import strip_forbidden_specs
            # Sicherheitsnetz: fehlt ein Pflicht-Name trotzdem, setzt der Code ihn hinter
            # den Produkttyp.
            title_seo, nicht_reingepasst = ensure_names_in_title(title_seo, pflicht_namen)
            if nicht_reingepasst:
                warnings.append("Kein Platz im Titel für: " + ", ".join(nicht_reingepasst))
            # Erfundene Mengen-Behauptungen entfernen (Vorfall Zoro 19.08.: die KI
            # machte aus einem Ein-Paar-Artikel ein "3er-Set" — der Masse-Guard
            # schuetzt nur vor VERLUST, nicht vor ERFINDUNG von Zahlen).
            from app.integrations.llm import strip_invented_counts
            title_seo, erfundene_mengen = strip_invented_counts(
                source_haystack(scraped.title_raw, scraped.description_raw), title_seo)
            if erfundene_mengen:
                warnings.append("Erfundene Mengen-Angabe aus dem Titel entfernt: "
                                + ", ".join(erfundene_mengen))
            description = ensure_variant_values_in_description(
                description, (variants_de or {}).get("axes"))
            item_specifics = strip_forbidden_specs(gen.item_specifics or {})
            # Marke muss WOERTLICH in den Quelldaten stehen – sonst raus (kein erfundenes
            # "Bandai" wie beim Import am 03.08.).
            item_specifics, marken_hinweis = verify_brand(
                item_specifics, quelle=source_haystack(
                    scraped.title_raw, scraped.description_raw),
                roh_titel=scraped.title_raw)
            if marken_hinweis:
                warnings.append(marken_hinweis)
            # Strategischer Hinweis (Markenrisiko/SEO) sichtbar machen
            if getattr(gen, "strategic_note", ""):
                warnings.insert(0, f"Strategie: {gen.strategic_note}")
        except Exception:  # noqa: BLE001 – Fallback laut Spec Kap. 5.1
            title_seo = scraped.title_raw[:80]
            description = "Hochwertiges Produkt."
            warnings = ["LLM nicht verfuegbar – Default-Titel verwendet"]
            item_specifics = {}

        # Step 3a: ECHTE Lieferanten-Versandkosten (freight query) – Netz-Call, laeuft noch
        # VOR den DB-Schreibvorgängen. Ersetzt die 1,99-€-Pauschale, wo bekannt (manche
        # Artikel kosten 3,29 €, manche liefern gratis) -> Marge wird nicht unterschaetzt.
        ship_eur = await resolve_supplier_ship_eur(ae, aliexpress_url)

        # Step 3b: Verkaufspreis aus Einkaufspreis berechnen – EMPFEHLUNG auf ZIELMARGE
        # (25 %) statt auf den 8-€-Mindestgewinn (Nutzerregel 08.07.).
        from app.services.fast_shipping_service import variants_have_eu_warehouse
        breakdown = pricing.upload_breakdown_from_cny(scraped.price_cny, settings=settings,
                                                      ship_override=ship_eur,
                                                      local=variants_have_eu_warehouse(scraped))
        sku = make_sku(scraped.aliexpress_id, aliexpress_url)
        # Standard-Sichtbestand (Dropshipping): 20 je Artikel/Variante (echter Bestand beim Lieferanten)
        quantity = settings.default_listing_quantity

        # Step 4: Listing-Draft erzeugen (native eBay-Engine oder AutoDS) –
        # im Test-Modus (skip_autods) ueberspringbar. Netz-Call -> VOR den DB-Schreibvorgängen.
        ebay_draft_id = None
        backend_used = backend.name
        if not skip_autods and not hat_entwurf:   # vorhandenen Entwurf wiederverwenden statt duplizieren
            try:
                draft = await retry_async(
                    lambda: backend.create_draft(
                        sku=sku,
                        aliexpress_url=aliexpress_url,
                        title_seo=title_seo,
                        description=description,
                        image_urls=scraped.images or [],
                        category_id=None,
                        price_eur=breakdown.rounded_price_eur,
                        quantity=quantity,
                        cost_eur=breakdown.cost_eur,
                    ),
                    label=f"draft_{backend.name}",
                    max_retries=3,
                )
                ebay_draft_id = draft.ebay_draft_id
            except Exception as exc:  # noqa: BLE001 – Teilweise Erfolg (Spec Kap. 5.1)
                # Der Grund gehoert in die Warnung. Vorher stand hier nur, DASS es
                # schiefging - der eigentliche eBay-Fehler blieb im Log stehen und
                # im Dashboard war nicht zu erkennen, was zu tun ist.
                logger.warning("Entwurf fehlgeschlagen (%s): %s", backend.name, exc)
                warnings.append(
                    f"{backend.name}-Fehler – Draft-ID = null, Produkt aber gespeichert: {exc}"
                )

        # --- Erst JETZT die DB-Schreibvorgänge: kurz & am Stück, KEINE Netz-Calls dazwischen,
        #     damit die SQLite-Schreibsperre nur Millisekunden gehalten wird. ---
        # TOCTOU-Schutz (Refresh): zwischen der frühen Veröffentlicht-Prüfung und JETZT liefen langsame
        # Netz-Calls (Scrape/LLM). In dem Fenster könnte ein PARALLELER Publish (eigene Session) das
        # Listing live geschaltet haben. Deshalb DIREKT vor dem Schreiben frisch nachladen und den Guard
        # erneut anwenden – so wird nie eine zwischenzeitlich veröffentlichte Zeile überschrieben.
        draft_listing = None
        if refresh:
            db.expire_all()
            existing = db.get(Product, existing.id)
            if existing is None:
                refresh = False                     # zwischenzeitlich gelöscht -> als Neuanlage behandeln
            else:
                cur = db.scalars(select(Listing).where(Listing.product_id == existing.id)).all()
                live = _lebendes_listing(cur)
                if live is not None and not ignoriere_duplikat:
                    raise DuplikatListing(
                        f"Produkt ging zwischenzeitlich live (Listing #{live.id}) – bitte über "
                        f"„Preise anpassen“ / Bearbeiten korrigieren, nicht neu importieren.",
                        listing_id=live.id, listing_status=live.listing_status or "")
                draft_listing = next((l for l in cur if l.listing_status == "draft"), None)

        if refresh:
            # Vorhandenes (nur-Entwurf-)Produkt IN PLACE aktualisieren – kein Duplikat, keine Löschung.
            product = existing
            product.title_raw = scraped.title_raw
            product.description_raw = scraped.description_raw
            product.price_cny = scraped.price_cny
            product.images = scraped.images
            # Kennungen schon HIER festschreiben, nicht erst beim Live-Gang: bis
            # dahin waere die Zuordnung positionsbasiert und verrutscht, sobald
            # AliExpress umsortiert. Bereits vergebene bleiben unangetastet.
            product.variants, _neu = variantenkennung.vergib(
                variants_de, f"AE-{scraped.aliexpress_id}")
            product.supplier_id = scraped.supplier_id
            product.supplier_rating = scraped.supplier_rating
            listing = draft_listing
            if listing is None:                     # Produkt ohne Entwurf (Teil-Anlage) -> Entwurf anlegen
                listing = Listing(
                    product_id=product.id, ebay_sku=sku, listing_status="draft",
                    ebay_draft_id=ebay_draft_id, optimization_status="healthy",
                    markup_pct=settings.profit_pct, quantity_available=quantity,
                    auto_reprice=settings.auto_reprice, min_price_eur=settings.min_price_eur,
                    max_price_eur=settings.max_price_eur, supplier_in_stock=True, monitor_status="ok",
                )
                db.add(listing)
            listing.title_seo = title_seo
            listing.description = description
            listing.price_eur = breakdown.rounded_price_eur
            listing.cost_eur = breakdown.cost_eur
            listing.supplier_ship_eur = (Decimal(str(ship_eur)) if ship_eur is not None else None)
            listing.item_specifics = item_specifics or None
            ebay_draft_id = listing.ebay_draft_id   # bestehende Draft-ID in der Antwort korrekt melden
            db.flush()
            # Zwei verschiedene Faelle, die frueher dieselbe (dann falsche) Meldung bekamen:
            # entweder wurde ein vorhandener Entwurf aufgefrischt, oder es entsteht bewusst
            # ein ZWEITER Entwurf neben einem beendeten/live Listing.
            warnings.insert(0, (
                "Produkt existierte bereits als Entwurf – Daten inkl. korrigierter "
                "Größen aktualisiert (kein Duplikat angelegt)."
                if draft_listing is not None else
                "Produkt war schon einmal angelegt – neuer Entwurf erstellt, die alten "
                "Listings bleiben unberührt."))
        else:
            product = Product(
                aliexpress_url=aliexpress_url,
                aliexpress_id=scraped.aliexpress_id,
                title_raw=scraped.title_raw,
                description_raw=scraped.description_raw,
                price_cny=scraped.price_cny,
                images=scraped.images,
                # Siehe oben: die Zuordnung Variante <-> AliExpress-SKU entsteht
                # beim Import und wandert danach nicht mehr.
                variants=variantenkennung.vergib(
                    variants_de, f"AE-{scraped.aliexpress_id}")[0],
                supplier_id=scraped.supplier_id,
                supplier_rating=scraped.supplier_rating,
            )
            db.add(product)
            try:
                db.flush()  # product.id – kann hier am aliexpress_url/-id-UNIQUE kollidieren
            except IntegrityError:
                # Wettlauf: ein PARALLELER Import derselben (neuen) Ware hat zwischen unserer
                # Duplikat-Prüfung und JETZT dasselbe Produkt angelegt. Sauber zurückrollen und als
                # „bitte erneut versuchen“ melden (dann greift beim Retry der Refresh-Pfad) statt
                # eines 500-Crashs / PendingRollbackError im Task-Log.
                db.rollback()
                raise PersistentError(
                    "Produkt wurde gerade zeitgleich importiert bzw. existiert bereits – bitte kurz "
                    "warten und erneut importieren.")

            listing = Listing(
                product_id=product.id,
                ebay_sku=sku,
                title_seo=title_seo,
                description=description,
                listing_status="draft",
                ebay_draft_id=ebay_draft_id,
                optimization_status="healthy",
                price_eur=breakdown.rounded_price_eur,
                cost_eur=breakdown.cost_eur,
                supplier_ship_eur=(Decimal(str(ship_eur)) if ship_eur is not None else None),
                markup_pct=settings.profit_pct,
                quantity_available=quantity,
                auto_reprice=settings.auto_reprice,
                min_price_eur=settings.min_price_eur,
                max_price_eur=settings.max_price_eur,
                supplier_in_stock=True,
                monitor_status="ok",
                item_specifics=item_specifics or None,
            )
            db.add(listing)
            db.flush()

            # Initialen Preis in die Repricing-Historie schreiben (Audit/Charts).
            db.add(PriceHistory(
                listing_id=listing.id,
                old_price_eur=None,
                new_price_eur=breakdown.rounded_price_eur,
                cost_eur=breakdown.cost_eur,
                profit_eur=breakdown.profit_eur,
                margin_pct=breakdown.margin_pct,
                reason="initial",
            ))

        tl.result_data = {"product_id": product.id, "listing_id": listing.id,
                          "backend": backend_used, "price_eur": breakdown.rounded_price_eur}
        try:
            db.commit()
        except IntegrityError:
            # Wettlauf: ein PARALLELER Import derselben (neuen) Ware hat zwischen unserer Duplikat-
            # Prüfung und dem Commit dasselbe Produkt angelegt -> aliexpress_url/-id-UNIQUE. Statt eines
            # 500-Crashs sauber zurückrollen und als „bitte erneut versuchen“ melden (dann greift der
            # Refresh-Pfad, weil das Produkt jetzt existiert).
            db.rollback()
            raise PersistentError(
                "Produkt wurde gerade zeitgleich importiert bzw. existiert bereits – bitte kurz "
                "warten und erneut importieren.")

        # Step 5/6: Response (Benachrichtigung -> hier Log; real: Email/Telegram/WebUI)
        return {
            "product_id": product.id,
            "listing_id": listing.id,
            "ebay_draft_id": ebay_draft_id,
            "status": "draft_created",
            "title_seo": title_seo,
            "description": description,
            "warnings": warnings,
            "backend": backend_used,
            "price_eur": breakdown.rounded_price_eur,
            "cost_eur": breakdown.cost_eur,
            "profit_eur": breakdown.profit_eur,
            "margin_pct": breakdown.margin_pct,
        }


async def ai_edit_listing(db: Session, *, listing_id: int, instruction: str) -> dict:
    """KI-BEARBEITUNG (propose-only): Der Nutzer beschreibt in Freitext, was am Listing
    geaendert werden soll (Titel/Beschreibung umformulieren, Merkmale ergaenzen, andere
    Kategorie, Markenrechts-Ausnahme im Titel …). Die KI liefert einen VORSCHLAG –
    es wird NICHTS automatisch gespeichert oder auf eBay gepusht. Der Nutzer prueft im
    Bearbeiten-Formular und speichert selbst (Regel: Mensch bestaetigt jede Aenderung).
    """
    from app.spec_filter import strip_forbidden_specs
    instruction = (instruction or "").strip()
    if not instruction:
        raise PersistentError("Bitte eine Anweisung eingeben, was geaendert werden soll.")
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    llm = get_llm_client()
    try:
        res = await llm.revise_listing(
            instruction=instruction,
            current_title=listing.title_seo or "",
            current_description=listing.description or "",
            current_specifics=strip_forbidden_specs(listing.item_specifics or {}),
            current_category=listing.category_id)
    except Exception as exc:  # noqa: BLE001
        raise PersistentError(f"KI-Bearbeitung fehlgeschlagen: {str(exc)[:200]}")
    # Herkunftsland China nie uebernehmen, auch wenn die KI es vorschlaegt.
    specifics = strip_forbidden_specs(res.get("item_specifics") or {})
    return {
        "listing_id": listing_id,
        "instruction": instruction,
        "proposal": {
            "title": (res.get("title_seo") or listing.title_seo or "")[:80],
            "description": res.get("description") or listing.description or "",
            "item_specifics": specifics,
            "category_hint": res.get("category_hint"),
        },
        "current": {
            "title": listing.title_seo, "description": listing.description,
            "item_specifics": strip_forbidden_specs(listing.item_specifics or {}),
            "category_id": listing.category_id,
        },
        "note": res.get("note") or "",
        "warnings": res.get("warnings") or [],
        "applied": False,   # nichts gespeichert – Nutzer bestaetigt im Formular
    }


async def regenerate_listing(db: Session, *, listing_id: int) -> dict:
    """Entwurf frisch von AliExpress holen + mit aktuellem LLM neu aufbauen (Titel/Beschreibung/Specs/Preis)."""
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    product = db.get(Product, listing.product_id) if listing.product_id else None
    if product is None or not product.aliexpress_url:
        raise PersistentError("Kein AliExpress-Produkt zur Neu-Generierung")

    settings = get_settings()
    ae = get_aliexpress_client()
    llm = get_llm_client()

    scraped = await retry_async(lambda: ae.scrape_product(product.aliexpress_url),
                                label="scrape", max_retries=3)
    # Produkt-Rohdaten auffrischen (Specs/Varianten/Bilder/Preis, Varianten eingedeutscht)
    product.title_raw = scraped.title_raw
    product.description_raw = scraped.description_raw
    product.price_cny = scraped.price_cny
    product.images = scraped.images
    product.variants = await _germanized_variants(llm, scraped)

    try:
        pflicht_namen = await llm.extract_names(title_raw=scraped.title_raw)
    except Exception:  # noqa: BLE001
        pflicht_namen = []
    # Eigenen Anfang ausschliessen: sonst gilt der Titel, den wir gerade ersetzen
    # wollen, als "schon vergeben" und blockiert sich selbst. Einmal vor der
    # Wiederholungsschleife, nicht im lambda (liefe sonst je Versuch erneut).
    anfaenge = vergebene_titelanfaenge(db, ausser_listing=listing.id)
    try:
        gen = await retry_async(
            lambda: llm.generate_listing(
                title_raw=scraped.title_raw, description_raw=scraped.description_raw,
                category_guess=None, specs=scraped.specs,
                variant_axes=(product.variants or {}).get("axes"),
                pflicht_namen=pflicht_namen,
                vergebene_anfaenge=anfaenge,
            ),
            label="llm", max_retries=3,
        )
        from app.brand_filter import (ensure_names_in_title,
                                      ensure_variant_values_in_description,
                                      source_haystack, verify_brand)
        from app.spec_filter import strip_forbidden_specs
        gen.title_seo, _ = ensure_names_in_title(gen.title_seo, pflicht_namen)
        listing.title_seo = gen.title_seo
        listing.description = ensure_variant_values_in_description(
            gen.description_clean, (product.variants or {}).get("axes"))
        specs_neu, _ = verify_brand(
            strip_forbidden_specs(gen.item_specifics or {}),
            quelle=source_haystack(scraped.title_raw, scraped.description_raw),
            roh_titel=scraped.title_raw)
        listing.item_specifics = specs_neu or None
    except Exception as exc:  # noqa: BLE001
        raise PersistentError(f"LLM-Fehler bei Neu-Generierung: {exc}")

    ship_eur = await resolve_supplier_ship_eur(ae, product.aliexpress_url)
    from app.services.fast_shipping_service import variants_have_eu_warehouse
    breakdown = pricing.upload_breakdown_from_cny(
        scraped.price_cny, settings=settings, category_name=listing.category_name,
        ship_override=ship_eur, local=variants_have_eu_warehouse(scraped))
    listing.price_eur = breakdown.rounded_price_eur
    listing.cost_eur = breakdown.cost_eur
    if ship_eur is not None:
        listing.supplier_ship_eur = Decimal(str(ship_eur))
    if not listing.quantity_available or listing.quantity_available < 1:
        listing.quantity_available = settings.default_listing_quantity
    db.commit()
    return {
        "listing_id": listing.id, "title_seo": listing.title_seo,
        "price_eur": float(listing.price_eur), "item_specifics": listing.item_specifics or {},
        "title_len": len(listing.title_seo or ""),
    }


async def finalize_listing(db: Session, *, listing_id: int, approve: bool,
                           overrides: dict | None = None) -> dict:
    """Draft nach manueller Kontrolle live stellen (POST .../finalize/{listing_id})."""
    overrides = overrides or {}
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise PersistentError("Listing nicht gefunden")
    if not approve:
        return {"listing_id": listing_id, "ebay_item_id": None, "status": "rejected"}

    if overrides.get("title"):
        listing.title_seo = overrides["title"]
    if overrides.get("category_id"):
        listing.category_id = overrides["category_id"]

    # Ohne echte Draft/Offer-ID kann nicht publiziert werden (sonst garantiert 404).
    if not listing.ebay_draft_id:
        raise PersistentError(
            "Kein eBay-Draft/Offer vorhanden (Draft-Erstellung fehlgeschlagen) – "
            "Listing kann nicht live gestellt werden."
        )

    ebay = get_ebay_client()
    item_id = await retry_async(
        lambda: ebay.publish_listing(
            listing.ebay_draft_id,
            title=listing.title_seo,
            category_id=listing.category_id or "0",
        ),
        label="ebay_publish",
    )
    listing.ebay_item_id = item_id
    listing.listing_status = "active"
    db.commit()
    return {"listing_id": listing_id, "ebay_item_id": item_id, "status": "active"}
