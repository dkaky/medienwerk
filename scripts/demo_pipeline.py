"""End-to-End-Demo (read-only): echtes AliExpress-Produkt -> Pricing -> LLM-Listing.

Zeigt den kompletten Veredelungs-Schritt OHNE DB-/eBay-Eingriff: holt echte
Produktdaten ueber die offizielle AliExpress-API, berechnet den eBay-Verkaufspreis
und erzeugt SEO-Titel + bereinigte Beschreibung ueber den LLM-Client.

Aufruf:
    .venv\\Scripts\\python.exe -m scripts.demo_pipeline "<aliexpress-produkt-url>"
"""
from __future__ import annotations

import asyncio
import sys

try:  # Windows-Konsole (cp1252) auf UTF-8 stellen -> Umlaute/Emoji statt Crash
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from app.config import get_settings
from app.integrations import get_llm_client
from app.integrations.aliexpress import RealAliExpressClient
from app.integrations.native_listing import make_sku
from app.services import pricing


async def main(url: str) -> int:
    s = get_settings()
    print("=" * 64)
    print(" Demo-Pipeline: AliExpress -> Pricing -> Listing  (read-only)")
    print("=" * 64)

    ae = RealAliExpressClient(s)
    print("1) AliExpress-Produktdaten holen (offizielle API) ...")
    scraped = await ae.scrape_product(url)
    sku = make_sku(scraped.aliexpress_id, url)
    print(f"   Roh-Titel:  {(scraped.title_raw or '')[:90]}")
    print(f"   Einkauf:    {scraped.price_cny} {s.aliexpress_target_currency}")
    print(f"   Bilder:     {len(scraped.images)} · SKUs: {len((scraped.variants or {}).get('skus', []))} · SKU={sku}")

    print("\n2) Verkaufspreis kalkulieren (Pricing-Engine, §19) ...")
    pb = pricing.price_from_cny(scraped.price_cny, settings=s)
    print(f"   Kosten EUR: {pb.cost_eur}  ->  Verkaufspreis: {pb.price_eur} EUR")
    print(f"   Gebuehr {pb.ebay_fee_eur} + Fix {pb.fixed_fee_eur}  ->  Gewinn {pb.profit_eur} EUR "
          f"(Marge {pb.margin_pct*100:.1f}% / Aufschlag {pb.markup_pct*100:.1f}%)")
    if pb.clamped:
        print(f"   (an {pb.clamped}-Grenze begrenzt)")

    print("\n3) SEO-Titel + Beschreibung erzeugen (LLM) ...")
    llm = get_llm_client()
    mode = "MOCK (regelbasiert)" if s.use_mock("llm") else f"ECHT ({s.llm_provider}/{s.llm_model})"
    print(f"   LLM-Modus:  {mode}")
    try:
        gen = await llm.generate_listing(
            title_raw=scraped.title_raw, description_raw=scraped.description_raw,
            category_guess=None,
        )
        title = gen.title_seo
        desc = gen.description_clean
        warnings = list(getattr(gen, "warnings", []) or [])
        note = getattr(gen, "strategic_note", "")
    except Exception as exc:  # noqa: BLE001
        title = (scraped.title_raw or "")[:80]
        desc = "Hochwertiges Produkt."
        warnings = [f"LLM-Fallback: {exc}"]
        note = ""

    print("\n" + "-" * 64)
    print(" FERTIGES LISTING (Vorschau)")
    print("-" * 64)
    print(f" Titel ({len(title)}/80): {title}")
    print(f" Preis:  {pb.price_eur} EUR   |   Gewinn: {pb.profit_eur} EUR")
    if note:
        print(f" Strategie-Hinweis: {note}")
    if warnings:
        print(" Warnungen: " + " | ".join(warnings))
    print("\n Beschreibung:")
    for line in (desc or "").splitlines()[:18]:
        print("   " + line)
    print("-" * 64)
    print("Echte AliExpress-Daten sind durch die komplette Veredelung gelaufen. ")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Bitte eine AliExpress-Produkt-URL als Argument angeben.")
        raise SystemExit(1)
    raise SystemExit(asyncio.run(main(sys.argv[1])))
