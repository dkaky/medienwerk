"""Schnelle Versandbedingung („Kostenlos Bearbeitung 2 Tage") fuer lokale Quellen.

Nutzerfokus 08/2026: Neue Produkte, deren AliExpress-Quelle aus einem ECHTEN EU-Lager
versendet („Versand aus"-SKU-Achse 200007763, s. aliexpress_api) oder die Eigenbestand
haben, bekommen beim Veroeffentlichen automatisch die schnelle eBay-Versandbedingung.

Frueher lief die Erkennung ueber die versprochene Lieferzeit (query_freight) — das war
falsch: AliExpress verspricht auch aus China kurze Lieferzeiten (Nutzer-Befund, Lese-Test
am Live-Produkt). Jetzt zaehlt ausschliesslich der im Produkt gespeicherte Lagerort
(Product.variants -> skus[].ship_from), den parse_product beim Import ablegt. Das braucht
beim Publish KEINEN API-Call mehr.

WICHTIG (Vorfall, nicht wiederholen): Versandbedingungen BESTEHENDER Listings werden NIE
pauschal geaendert — dieser Service wirkt nur beim Publish NEUER Offers.

Alle Funktionen sind defensiv: jede Stoerung fuehrt zu None — ein Publish darf daran nie
scheitern, er laeuft dann mit der Standard-Bedingung weiter.
"""
from __future__ import annotations

import logging
import time

from app.config import get_settings
from app.integrations.aliexpress_api import has_eu_warehouse

logger = logging.getLogger("app.services.fast_shipping")

# Policy-Aufloesung aendert sich quasi nie -> Prozess-Cache.
_policy_cache: dict[str, tuple[float, str | None]] = {}
_POLICY_TTL_S = 6 * 3600


def clear_caches() -> None:
    """Nur fuer Tests."""
    _policy_cache.clear()


async def resolve_fast_policy_id(ebay) -> str | None:
    """ID der schnellen Versandbedingung — per NAME aufgeloest (Account API).

    Der Name steht in settings.ebay_fulfillment_policy_fast_name; so braucht es
    keinen .env-Eingriff auf dem VPS fuer die ID.
    """
    name = (get_settings().ebay_fulfillment_policy_fast_name or "").strip()
    if not name:
        return None
    hit = _policy_cache.get(name)
    if hit and time.time() - hit[0] < _POLICY_TTL_S:
        return hit[1]
    try:
        policies = await ebay.list_fulfillment_policies()
    except Exception as exc:  # noqa: BLE001 – Account-API-Stoerung darf nichts reissen
        logger.warning("fast-policy Aufloesung fehlgeschlagen: %s", str(exc)[:120])
        return hit[1] if hit else None
    pid = next((p.get("policy_id") for p in policies
                if (p.get("name") or "").strip().lower() == name.lower()), None)
    _policy_cache[name] = (time.time(), pid)
    if pid is None:
        logger.warning("schnelle Versandbedingung '%s' nicht im eBay-Konto gefunden", name)
    return pid


def _has_self_stock(listing) -> bool:
    """Eigenbestand vorhanden (mind. eine Variante mit qty > 0)?"""
    stock = listing.self_stock or {}
    try:
        return any(int((v or {}).get("qty") or 0) > 0 for v in stock.values())
    except AttributeError:
        return False


def variants_have_eu_warehouse(product) -> bool:
    """Hat die gespeicherte Quelle mindestens eine SKU mit EU-Lagerort (ship_from)?

    Gilt nur fuer Produkte, die NACH dem Ships-From-Umbau importiert/synchronisiert
    wurden — aeltere haben kein ship_from-Feld und zaehlen konservativ als nicht lokal.
    """
    variants = getattr(product, "variants", None) or {}
    skus = variants.get("skus") or []
    return has_eu_warehouse([s.get("ship_from") for s in skus if isinstance(s, dict)])


async def apply_fast_policy_to_listing(db, ebay, listing) -> dict:  # noqa: ARG001 – db fuer Symmetrie/Zukunft
    """Versandbedingung EINES bestehenden Listings auf die schnelle Policy umstellen.

    Nutzer-Go 08.08. (Kulturtaschen-Fall): ausgeloest beim Setzen von Eigenbestand —
    IMMER Einzelfall, nie pauschal (Vorfall Pauschal-Aktion). Aktualisiert die
    Inventory-Offers per updateOffer; Klassik-Listings ohne Offers melden eine Notiz
    (manuell in eBay umstellen). Fehler brechen nie den Aufrufer."""
    try:
        fast_id = await resolve_fast_policy_id(ebay)
        if not fast_id:
            return {"switched": False, "note": "schnelle Versandbedingung nicht im eBay-Konto gefunden"}
        from app.services.golive_service import _listing_variant_skus
        skus = await _listing_variant_skus(ebay, listing)
        if not skus and getattr(listing, "ebay_sku", None):
            skus = [listing.ebay_sku]
        updated = 0
        for sku in skus:
            offer = await ebay._first_offer_for_sku(sku)  # noqa: SLF001 – bewusst derselbe Pfad wie create_offer
            if not offer or not offer.get("offerId"):
                continue
            if ((offer.get("listingPolicies") or {}).get("fulfillmentPolicyId")) == fast_id:
                updated += 1
                continue
            body = ebay._sanitize_offer(offer)  # noqa: SLF001
            pol = dict(body.get("listingPolicies") or {})
            pol["fulfillmentPolicyId"] = fast_id
            body["listingPolicies"] = pol
            await ebay.update_offer(offer["offerId"], body)
            updated += 1
        if updated:
            logger.info("Versandbedingung umgestellt: Listing %s, %s Offer(s)",
                        getattr(listing, "id", "?"), updated)
            return {"switched": True, "offers": updated}
        return {"switched": False,
                "note": "keine Inventory-Offers gefunden — bitte einmal manuell in eBay umstellen"}
    except Exception as exc:  # noqa: BLE001 – Aufrufer (Eigenbestand speichern) nie brechen
        logger.warning("fast-policy Umstellung fehlgeschlagen: %s", str(exc)[:150])
        return {"switched": False, "note": f"Fehler: {str(exc)[:120]}"}


async def publish_policies(ebay, listing) -> dict | None:
    """Policies-Override fuer createOffer: schnelle Versandbedingung, wenn die Quelle
    aus einem EU-Lager versendet ODER Eigenbestand existiert. Sonst None -> Standard.

    Gibt IMMER den vollstaendigen Policy-Satz zurueck (payment/return aus dem
    Standard-Kontext), damit dem Offer keine Policy fehlt.
    """
    try:
        if not get_settings().fast_shipping_auto_policy:
            return None
        product = getattr(listing, "product", None)
        self_stock = _has_self_stock(listing)
        qualifies = self_stock or variants_have_eu_warehouse(product)
        if not qualifies:
            return None
        fast_id = await resolve_fast_policy_id(ebay)
        if not fast_id:
            return None
        ctx = await ebay._listing_context()  # noqa: SLF001 – bewusst: derselbe Cache wie createOffer
        policies = dict(ctx.get("policies") or {})
        policies["fulfillment"] = fast_id
        logger.info("publish: schnelle Versandbedingung fuer Listing %s (%s)",
                    getattr(listing, "id", "?"),
                    "Eigenbestand" if self_stock else "EU-Lager-Quelle")
        return policies
    except Exception as exc:  # noqa: BLE001 – der Publish darf hieran NIE scheitern
        logger.warning("publish_policies uebersprungen: %s", str(exc)[:150])
        return None
