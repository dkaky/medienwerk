"""Anzeigenraten-Sync: echte Promoted-Listings-Rate PRO LISTING von eBay lesen.

Die Gebuehrenkalkulation rechnet standardmaessig mit der Pauschale
(settings.ebay_ad_rate_pct, z.B. 10 %). Dieser Service zieht die TATSAECHLICH bei
eBay hinterlegte Anzeigenrate (bidPercentage) je aktivem Listing und speichert sie
als Bruch in listing.ad_rate_pct -> die Kalkulation kann pro Produkt die echte Rate
nutzen (None = unbekannt -> weiter Pauschale).

Nutzt bewusst einen ECHTEN RealEbayClient (unabhaengig von MOCK_EBAY), wie
golive/optimization/fulfillment (Geld-/Live-Reads muessen echt sein).

HARTE Projektregel: keine DB-Schreibsperre ueber den Netz-Call halten. Erst ALLE
Raten holen, DANN in einer kurzen Transaktion schreiben.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Listing

logger = logging.getLogger("app.services.ad_rate")


def _real_ebay():
    """ECHTER eBay-Client, unabhaengig vom MOCK_EBAY-Flag (wie import/golive/
    optimization). Marketing-Reads (Anzeigenraten) muessen gegen das echte eBay
    laufen, auch wenn get_ebay_client() sonst gemockt ist."""
    from app.integrations.ebay import RealEbayClient
    return RealEbayClient(get_settings())


async def sync_ad_rates(db: Session) -> dict:
    """Echte Anzeigenraten aller Live-Listings von eBay nachziehen.

    Sammelt aktive Listings mit ebay_item_id, holt die Raten in EINEM Marketing-Call
    (client.get_ad_rates), und schreibt listing.ad_rate_pct fuer jedes gefundene
    Listing (Match ueber ebay_item_id). Nicht gefundene Listings bleiben unangetastet
    (kein Zuruecksetzen auf None) -> eine ausgelaufene Kampagne loescht keine
    zuletzt bekannte Rate.

    Gibt {"updated": n, "checked": m} zurueck.
    """
    # 1) Kandidaten lesen (kurz), Netz-Call OHNE offene Schreibsperre.
    listings = db.scalars(
        select(Listing).where(
            Listing.ebay_item_id.is_not(None),
            Listing.listing_status == "active",
        )
    ).all()
    checked = len(listings)
    if not checked:
        return {"updated": 0, "checked": 0}

    item_ids = [str(l.ebay_item_id) for l in listings if l.ebay_item_id]

    # 2) Netz-Call (kann dauern) – ohne offene Transaktion.
    try:
        rates = await _real_ebay().get_ad_rates(item_ids)
    except Exception as exc:  # noqa: BLE001 – Read-only, nie den Scheduler haerten
        logger.warning("sync_ad_rates: eBay-Read fehlgeschlagen (%s)", exc)
        return {"updated": 0, "checked": checked, "error": str(exc)}

    if not rates:
        return {"updated": 0, "checked": checked}

    # 3) Kurze Schreib-Transaktion: nur gefundene Raten uebernehmen.
    updated = 0
    for listing in listings:
        rate = rates.get(str(listing.ebay_item_id))
        if rate is None:
            continue
        if listing.ad_rate_pct != rate:
            listing.ad_rate_pct = rate
            updated += 1
    if updated:
        db.commit()
    return {"updated": updated, "checked": checked}


def set_ad_rate(db: Session, listing: Listing, bid_pct: float) -> None:
    """Anzeigenrate EINES Listings setzen + committen (Bruch, 0.12 = 12 %).

    Vom Optimieren-Push genutzt: wenn die echte Rate beim Setzen bekannt ist, wird
    sie sofort persistiert (statt auf den naechsten sync_ad_rates-Lauf zu warten).
    """
    listing.ad_rate_pct = bid_pct
    db.commit()
