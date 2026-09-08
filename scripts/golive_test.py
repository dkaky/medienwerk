"""Kontrollierter Go-Live-Test: EIN echtes Listing über die native Engine live stellen.

Fährt die komplette ECHTE Kette: AliExpress (Produktdaten) -> Claude (Titel/Beschreibung)
-> Pricing -> eBay createInventoryItem + createOffer (mit Policies/Location/Description)
-> publishOffer. Gibt die Live-eBay-URL aus. Erzeugt ein ECHTES, öffentliches Angebot!

Aufruf:
    .venv\\Scripts\\python.exe -m scripts.golive_test "<aliexpress-url>"
    .venv\\Scripts\\python.exe -m scripts.golive_test "<url>" --end   # danach wieder beenden
"""
from __future__ import annotations

import asyncio
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from app.config import get_settings
from app.integrations import get_llm_client
from app.integrations.aliexpress import RealAliExpressClient
from app.integrations.ebay import RealEbayClient
from app.integrations.native_listing import make_sku
from app.services import pricing


async def main(url: str, end_after: bool) -> int:
    s = get_settings()
    ae = RealAliExpressClient(s)
    ebay = RealEbayClient(s)
    llm = get_llm_client()
    print("=" * 64)
    print(" Go-Live-Test (ECHTES eBay-Listing)")
    print("=" * 64)

    print("1) AliExpress-Produktdaten …")
    p = await ae.scrape_product(url)
    sku = make_sku(p.aliexpress_id, url)
    print(f"   {p.title_raw[:70]}  |  {p.price_cny} EUR  |  {len(p.images)} Bilder  |  SKU {sku}")

    print("2) Claude: Titel + Beschreibung …")
    try:
        gen = await llm.generate_listing(title_raw=p.title_raw, description_raw=p.description_raw,
                                         category_guess=None)
        title, desc = gen.title_seo, gen.description_clean
    except Exception as exc:  # noqa: BLE001
        title, desc = p.title_raw[:80], p.description_raw
        print("   (LLM-Fallback:", exc, ")")
    print(f"   Titel: {title}")

    br = pricing.price_from_cny(p.price_cny, settings=s)
    print(f"3) Preis: Kosten {br.cost_eur} € -> Verkaufspreis {br.rounded_price_eur} € (Gewinn {br.profit_eur} €)")

    print("4) eBay-Kategorie vorschlagen …")
    category = await ebay.suggest_category(title) or "0"
    aspects = await ebay.build_aspects(category, {"Marke": ["Markenlos"]})
    print(f"   Kategorie-ID: {category}  |  Merkmale: {', '.join(aspects.keys())}")

    print("5) createInventoryItem …")
    await ebay.create_inventory_item(
        sku, title=title, description=desc, image_urls=p.images or [], quantity=1,
        aspects=aspects,
    )
    print("   OK")

    print("6) createOffer (mit Policies + Location + Beschreibung) …")
    offer_id = await ebay.create_offer(sku, price_eur=br.rounded_price_eur,
                                       category_id=category, quantity=1,
                                       listing_description=desc)
    print(f"   OK: offerId {offer_id}")

    print("7) publishOffer (GEHT JETZT ÖFFENTLICH LIVE) …")
    try:
        item_id = await ebay.publish_listing(offer_id, title=title, category_id=category)
    except Exception as exc:  # noqa: BLE001
        print(f"   FAIL publishOffer: {type(exc).__name__}: {str(exc)[:400]}")
        print("   -> Offer bleibt unpubliziert; Fehlermeldung oben zeigt fehlende Pflichtfelder.")
        return 2
    url_live = f"https://www.ebay.de/itm/{item_id}"
    print("-" * 64)
    print(f" LIVE! itemId {item_id}")
    print(f" {url_live}")
    print("-" * 64)

    if end_after:
        try:
            await ebay.delete_inventory_item(sku)
            print("Cleanup: Inventory-Item/Angebot wieder entfernt.")
        except Exception as exc:  # noqa: BLE001
            print("Cleanup-Hinweis:", str(exc)[:160])
    else:
        print("Angebot bleibt online. Zum Beenden: erneut mit --end aufrufen oder im Verkäufer-Hub.")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("Bitte eine AliExpress-URL angeben.")
        raise SystemExit(1)
    raise SystemExit(asyncio.run(main(args[0], "--end" in sys.argv[1:])))
