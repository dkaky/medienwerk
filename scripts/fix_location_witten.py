"""Einmal-Fix: eBay-Merchant-Location auf Witten setzen + laufendes Listing neu rendern.

Aendert die Adresse der WAREHOUSE-Location WH-DE-01 von Berlin (10115) auf Witten
(58452, NRW) und stoesst fuer das bereits live geschaltete Frankreich-Listing
(itemId 800271691842, SKU AE-1005005121819836) ein updateOffer an, damit eBay die
'Versand aus'-Angabe neu rendert. Gegen das ECHTE eBay-Konto.
"""
from __future__ import annotations

import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8")

from app.config import get_settings
from app.integrations.ebay import RealEbayClient

SKU_LIVE = "AE-1005005121819836"  # Frankreich-Kette, itemId 800271691842


def _fmt(loc: dict) -> str:
    a = (loc.get("location") or {}).get("address") or {}
    return (f"{loc.get('merchantLocationKey')}: "
            f"PLZ={a.get('postalCode')} Ort={a.get('city') or '—'} "
            f"Land={a.get('country')} Region={a.get('stateOrProvince') or '—'}")


async def main() -> int:
    s = get_settings()
    c = RealEbayClient(s)
    key = s.ebay_merchant_location_key

    print("VORHER:")
    for l in await c.get_inventory_locations():
        print("  ", _fmt(l))

    print(f"\n1) Location {key} -> Witten ({s.ebay_warehouse_postal}) ...")
    await c.update_inventory_location(
        key, postal_code=s.ebay_warehouse_postal, country=s.ebay_warehouse_country,
        city=s.ebay_warehouse_city, state=s.ebay_warehouse_state,
    )
    print("   OK (update_location_details)")

    print("NACHHER:")
    for l in await c.get_inventory_locations():
        print("  ", _fmt(l))

    print(f"\n2) Live-Listing neu rendern (SKU {SKU_LIVE}) ...")
    try:
        oid = await c.refresh_listing_location(SKU_LIVE)
        print(f"   OK offerId={oid} -> Angebot rendert 'Versand aus Witten' neu.")
    except Exception as exc:  # noqa: BLE001
        print(f"   HINWEIS: {type(exc).__name__}: {str(exc)[:400]}")
        print("   (Location ist trotzdem korrigiert; Listing ggf. manuell revidieren.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
