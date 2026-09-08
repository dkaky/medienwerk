"""Verbindungstest gegen die ECHTE AliExpress Open-Platform / Dropshipping-API.

Ruft aliexpress.ds.product.get fuer eine echte Produkt-URL auf und zeigt die
geparsten Felder. Veraendert nichts.

Aufruf (aus dem Projektordner):
    .venv\\Scripts\\python.exe -m scripts.verify_aliexpress "<aliexpress-produkt-url>"
"""
from __future__ import annotations

import asyncio
import sys

from app.config import get_settings
from app.integrations import aliexpress_api as api
from app.integrations.aliexpress import RealAliExpressClient
from app.retry import PersistentError, TransientError


async def main(url: str) -> int:
    s = get_settings()
    print("=" * 60)
    print(" AliExpress-API-Verbindungstest")
    print("=" * 60)
    print(f"  Gateway:      {s.aliexpress_api_base}")
    print(f"  Sign-Method:  {s.aliexpress_sign_method}")
    print(f"  App-Key:      {'gesetzt' if s.aliexpress_app_key else 'FEHLT'}")
    print(f"  Ship-To/Cur:  {s.aliexpress_ship_to} / {s.aliexpress_target_currency}")
    pid = api.extract_product_id(url)
    print(f"  Produkt-ID:   {pid}")
    print("-" * 60)
    if not pid:
        print("FAIL: Keine Produkt-ID aus der URL erkannt.")
        return 1

    client = RealAliExpressClient(s)
    try:
        p = await client.scrape_product(url)
    except PersistentError as exc:
        msg = str(exc)
        print(f"FAIL (persistent): {msg[:300]}")
        low = msg.lower()
        if "sign" in low or "signature" in low:
            print("  Hinweis: Signaturfehler -> ggf. ALIEXPRESS_SIGN_METHOD von 'sha256' auf 'md5' "
                  "(oder umgekehrt) umstellen.")
        elif "ip" in low and ("white" in low or "limit" in low):
            print("  Hinweis: IP-Whitelist -> aktuelle Server-IP im App-Console unter "
                  "'IP Whitelist' eintragen.")
        elif "app" in low and "online" in low:
            print("  Hinweis: App ist im Test-Status -> im App-Console 'Apply Online' ausfuehren.")
        return 2
    except TransientError as exc:
        print(f"FAIL (transient/Netz): {str(exc)[:200]}")
        return 3

    print("PASS – Produktdaten geladen:")
    print(f"  AliExpress-ID:   {p.aliexpress_id}")
    print(f"  Titel:           {(p.title_raw or '')[:80]}")
    print(f"  Preis:           {p.price_cny} {s.aliexpress_target_currency}")
    print(f"  Bilder:          {len(p.images)}")
    print(f"  Varianten/SKUs:  {len((p.variants or {}).get('skus', []))}")
    print(f"  Lieferant:       {p.supplier_id} (Rating {p.supplier_rating})")
    print(f"  Auf Lager:       {p.in_stock}")
    print("-" * 60)
    print("Die offizielle AliExpress-API liefert echte Produktdaten. ")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Bitte eine AliExpress-Produkt-URL als Argument angeben.")
        raise SystemExit(1)
    raise SystemExit(asyncio.run(main(sys.argv[1])))
