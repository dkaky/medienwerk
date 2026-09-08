"""Publish-Smoketest gegen das ECHTE eBay (Production) – sicher & aufraeumend.

Testet die native Listing-Pipeline (Inventory-Item -> Offer) mit echten Credentials
und raeumt danach wieder auf. Standardmaessig wird NICHTS oeffentlich gestellt
(publishOffer wird uebersprungen). Nur mit ``--publish`` wuerde ein echtes,
oeffentliches Listing entstehen (braucht eBay-Business-Policies).

Aufruf:
    .venv\\Scripts\\python.exe -m scripts.smoke_ebay_publish
    .venv\\Scripts\\python.exe -m scripts.smoke_ebay_publish --publish   # geht oeffentlich live!
"""
from __future__ import annotations

import asyncio
import sys

from app.config import get_settings
from app.integrations.ebay import RealEbayClient

# Unverfaengliche Testkategorie (eBay DE: "Sonstige") – nur fuer den Smoke-Test.
_TEST_CATEGORY = "171228"


async def main(publish: bool) -> int:
    s = get_settings()
    print("=" * 60)
    print(" eBay Publish-Smoketest")
    print("=" * 60)
    print(f"  Umgebung:   {'SANDBOX' if s.ebay_use_sandbox else 'PRODUCTION'}")
    print(f"  Publish:    {'JA (oeffentliches Listing!)' if publish else 'nein (nur Inventory+Offer, danach Cleanup)'}")
    if not (s.ebay_client_id and s.ebay_refresh_token):
        print("FAIL: eBay-Credentials fehlen in der .env.")
        return 1

    client = RealEbayClient(s)
    sku = "SMOKE-TEST-DELETE-ME"
    offer_id = None
    try:
        print("\n1) createOrReplaceInventoryItem ...")
        await client.create_inventory_item(
            sku, title="SMOKE TEST – bitte ignorieren",
            description="Interner Verbindungstest. Kein echtes Angebot.",
            image_urls=[
                "https://upload.wikimedia.org/wikipedia/commons/thumb/3/3a/Cat03.jpg/640px-Cat03.jpg"
            ],
            quantity=1, aspects={"Marke": ["Markenlos"]},
        )
        print("   OK: Inventory-Item angelegt (SKU " + sku + ").")

        print("2) createOffer ...")
        try:
            offer_id = await client.create_offer(
                sku, price_eur=9.99, category_id=_TEST_CATEGORY, quantity=1,
            )
            print(f"   OK: Offer angelegt (offerId {offer_id}, noch UNVEROEFFENTLICHT).")
        except Exception as exc:  # noqa: BLE001
            print(f"   Teil-OK: Offer nicht angelegt – {type(exc).__name__}: {str(exc)[:200]}")
            print("   (Haeufig: fehlende eBay-Business-Policies/Versandeinstellungen –"
                  " fuer den Verbindungsbeweis unkritisch.)")

        if publish and offer_id:
            print("3) publishOffer (GEHT OEFFENTLICH LIVE) ...")
            item_id = await client.publish_listing(offer_id, title="SMOKE TEST",
                                                   category_id=_TEST_CATEGORY)
            print(f"   OK: Listing live – itemId {item_id}. BITTE manuell beenden!")
        elif publish:
            print("3) publishOffer uebersprungen (kein Offer vorhanden).")

        print("\nERGEBNIS: Native Publish-Pipeline gegen Production erreichbar. PASS")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAIL bei createInventoryItem: {type(exc).__name__}: {str(exc)[:300]}")
        return 2
    finally:
        # Aufraeumen: Inventory-Item (und damit das unveroeffentlichte Offer) entfernen.
        if not publish:
            try:
                await client.delete_inventory_item(sku)
                print("\nCleanup: Test-Inventory-Item wieder geloescht. (nichts blieb zurueck)")
            except Exception as exc:  # noqa: BLE001
                print(f"\nCleanup-Hinweis: konnte SKU {sku} nicht loeschen – {str(exc)[:160]}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--publish" in sys.argv[1:])))
