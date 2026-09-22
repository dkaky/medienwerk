"""Einmal bei eBay anmelden - danach merkt sich der interne Browser die Sitzung.

Warum es das braucht
---------------------
Der Marktcheck (``app/studio/radar/ernte.py``) und der geplante Rechnungs-Import
(eBay-Gebuehrenrechnungen und Bestell-Rechnungen "Rechnung drucken") brauchen ein
Browser-Profil, das bei eBay angemeldet ist - beides liest Seiten, die eBay ohne
Anmeldung mit einer Anmeldeseite beantwortet statt mit Daten.

Dasselbe dauerhafte Profil wie beim AliExpress-Login (``scripts/browser_anmelden.py``,
``app/browser_profil.py``): EIN Browser dieses Rechners, EINE Sitzung je Anbieter, in
demselben Profil gespeichert. Ein Login hier stoert den AliExpress-Login nicht.

**Das Passwort tippt der Betreiber selbst.** Dieses Skript fragt nie danach,
speichert keines und liest keines. Es oeffnet ein Fenster und wartet.

Bedienung
---------
Anmelden::

    .venv\\Scripts\\python.exe scripts\\ebay_anmelden.py

Nur nachsehen, ob die Anmeldung noch steht::

    .venv\\Scripts\\python.exe scripts\\ebay_anmelden.py --pruefen
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: Seite, die es NUR angemeldet gibt (Verkaeufer-Cockpit). Abgemeldet leitet eBay
#: auf die Anmeldeseite um ("signin.ebay.de/...") - daran laesst sich der Zustand
#: zweifelsfrei ablesen, genau wie beim Marktcheck (app/studio/radar/ernte.py).
_NUR_ANGEMELDET = "https://www.ebay.de/sh/lst/active"


async def _oeffne(p, sichtbar: bool):
    """Profil oeffnen - ueber denselben Weg wie der Marktcheck und der AliExpress-Login."""
    from app.browser_profil import starte_profil

    ctx, kanal = await starte_profil(
        p, sichtbar=sichtbar,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"],
        locale="de-DE",
        viewport={"width": 1366, "height": 900},
    )
    print(f"(Browser: {kanal})")
    return ctx


async def _zustand(page) -> str:
    """"angemeldet", "abgemeldet" oder "unklar" - ueber die Umleitung, nicht ueber Text."""
    try:
        await page.goto(_NUR_ANGEMELDET, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(3000)
        url = (page.url or "").lower()
    except Exception:  # noqa: BLE001
        return "unklar"
    if not url:
        return "unklar"
    if "signin." in url:
        return "abgemeldet"
    if "/sh/lst" in url:
        return "angemeldet"
    return "unklar"


async def pruefen() -> int:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        ctx = await _oeffne(p, sichtbar=False)
        page = await ctx.new_page()
        try:
            zustand = await _zustand(page)
        finally:
            await ctx.close()

    print({"angemeldet": "Angemeldet - Marktcheck und Rechnungs-Import laufen mit deinem Konto.",
           "abgemeldet": "NICHT angemeldet. Zum Anmelden dieses Skript ohne --pruefen starten.",
           "unklar": "Zustand nicht erkennbar (Seite anders aufgebaut oder Botpruefung). "
                     "Im Zweifel neu anmelden."}[zustand])
    return 0 if zustand == "angemeldet" else 1


async def anmelden() -> int:
    from playwright.async_api import async_playwright

    print("Es oeffnet sich gleich ein Browserfenster.\n")
    print("  1. Melde dich dort ganz normal bei eBay an (dein Verkaeuferkonto).")
    print("  2. Warte, bis oben rechts dein Name statt 'Anmelden' steht.")
    print("  3. Komm hierher zurueck und druecke ENTER.\n")
    print("Das Fenster gehoert diesem Programm - schliess es nicht selbst.\n")

    async with async_playwright() as p:
        ctx = await _oeffne(p, sichtbar=True)
        page = await ctx.new_page()
        await page.goto("https://www.ebay.de/", wait_until="domcontentloaded", timeout=45000)
        try:
            # Blockiert bewusst: der Mensch meldet sich an, das Skript wartet.
            await asyncio.get_running_loop().run_in_executor(None, input)
            zustand = await _zustand(page)
        finally:
            await ctx.close()

    if zustand == "angemeldet":
        print("\nAngemeldet. Die Sitzung gilt jetzt fuer Marktcheck und Rechnungs-Import.")
        return 0
    print(f"\nNoch nicht angemeldet (erkannt: {zustand}). Starte das Skript noch einmal.")
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description="Internen Browser einmal bei eBay anmelden.")
    p.add_argument("--pruefen", action="store_true",
                   help="nur nachsehen, ob die Anmeldung noch steht")
    args = p.parse_args()
    if args.pruefen:
        return asyncio.run(pruefen())
    return asyncio.run(anmelden())


if __name__ == "__main__":
    raise SystemExit(main())
