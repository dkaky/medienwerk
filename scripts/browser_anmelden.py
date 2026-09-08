"""Einmal bei AliExpress anmelden - danach merkt sich der interne Browser die Sitzung.

Warum es das braucht
--------------------
Der Shop-Scraper (``app/integrations/aliexpress_store.py``) benutzt laengst einen
Browser mit **dauerhaftem Profil** unter ``data/browser_profile``. Nur hat sich
dort nie jemand angemeldet. Ohne Anmeldung zeigt AliExpress:

* weniger Artikel je Shop-Seite,
* haeufiger Sperren und Botpruefungen,
* teils gar keine Ergebnisse.

Nutzerwunsch vom 03.09.2026, woertlich: „Hierfuer muessen wir einen internen
browser anlegen der dann mit meinem aliexpress konto verbunden ist, auf die shop
seite drauf geht und seit e fuer seite scraped."

Der Browser existiert also schon - dieses Skript oeffnet ihn nur SICHTBAR, damit
der Betreiber sich selbst anmelden kann. Danach liegt die Sitzung im Profil, und
jeder spaetere Scraper-Lauf ist angemeldet.

**Das Passwort tippt der Betreiber selbst.** Dieses Skript fragt nie danach,
speichert keines und liest keines. Es oeffnet ein Fenster und wartet.

Bedienung
---------
Anmelden::

    .venv\\Scripts\\python.exe scripts\\browser_anmelden.py

Nur nachsehen, ob die Anmeldung noch steht::

    .venv\\Scripts\\python.exe scripts\\browser_anmelden.py --pruefen
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PROFIL = Path("data/browser_profile")

#: Kennzeichen einer angemeldeten Sitzung. AliExpress zeigt diese Punkte nur
#: eingeloggt; ausgeloggt steht dort "Anmelden" / "Sign in".
_ANGEMELDET = ("Mein AliExpress", "My AliExpress", "Meine Bestellungen",
               "My Orders", "Abmelden", "Sign out")
_ABGEMELDET = ("Anmelden", "Sign in", "Registrieren", "Join")


async def _oeffne(p, sichtbar: bool):
    """Profil oeffnen - ueber denselben Weg wie der Scraper.

    Playwrights MITGELIEFERTER Chromium startet auf diesem Windows nur
    unsichtbar; als Fenster scheitert er mit "spawn UNKNOWN". Deshalb geht es
    ueber ``starte_profil``, das der Reihe nach Chrome, Edge und zuletzt den
    mitgelieferten Browser probiert - und zwar in BEIDEN Faellen, damit
    Anmeldung und Leser dasselbe Profil mit demselben Browser benutzen.
    """
    from app.browser_profil import starte_profil

    PROFIL.mkdir(parents=True, exist_ok=True)
    ctx, kanal = await starte_profil(
        p, PROFIL, sichtbar=sichtbar,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"],
        locale="de-DE",
        viewport={"width": 1366, "height": 900},
    )
    print(f"(Browser: {kanal})")
    return ctx


#: Seite, die es NUR angemeldet gibt. Abgemeldet leitet AliExpress auf die
#: Anmeldung um - daran laesst sich der Zustand zweifelsfrei ablesen.
_NUR_ANGEMELDET = "https://www.aliexpress.com/p/order/index.html"


async def _zustand(page) -> str:
    """"angemeldet", "abgemeldet" oder "unklar" - ohne zu raten.

    Geprueft wird ueber eine SEITE, nicht ueber Text auf der Startseite. Der
    erste Versuch suchte nach Woertern wie "Anmelden" oder "Mein AliExpress" -
    das ging schief: Am 05.09.2026 war der Betreiber nachweislich angemeldet
    (Bestellseite erreichbar, Sitzungs-Cookies im Profil), und die Erkennung
    meldete trotzdem zwoelf Minuten lang "abgemeldet" bzw. "unklar". Die
    Startseite baut den Kontobereich per Skript nach dem Laden auf und
    formuliert ihn je nach Region anders.

    Die Umleitung dagegen ist eindeutig: abgemeldet landet man auf der
    Anmeldeseite, angemeldet auf den Bestellungen.
    """
    try:
        await page.goto(_NUR_ANGEMELDET, wait_until="domcontentloaded",
                        timeout=45000)
        await page.wait_for_timeout(4000)
        url = (page.url or "").lower()
    except Exception:  # noqa: BLE001
        return "unklar"
    if not url:
        return "unklar"
    if "login" in url or "passport" in url:
        return "abgemeldet"
    if "order" in url:
        return "angemeldet"
    return "unklar"


async def pruefen() -> int:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        ctx = await _oeffne(p, sichtbar=False)
        page = await ctx.new_page()
        try:
            await page.goto("https://de.aliexpress.com/",
                            wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            zustand = await _zustand(page)
        finally:
            await ctx.close()

    print({"angemeldet": "Angemeldet - der Scraper laeuft mit deinem Konto.",
           "abgemeldet": "NICHT angemeldet. Zum Anmelden dieses Skript ohne "
                         "--pruefen starten.",
           "unklar": "Zustand nicht erkennbar (Seite anders aufgebaut oder "
                     "Botpruefung). Im Zweifel neu anmelden."}[zustand])
    return 0 if zustand == "angemeldet" else 1


async def anmelden() -> int:
    from playwright.async_api import async_playwright

    print("Es oeffnet sich gleich ein Browserfenster.\n")
    print("  1. Melde dich dort ganz normal bei AliExpress an.")
    print("  2. Warte, bis oben rechts DEIN Name statt 'Anmelden' steht.")
    print("  3. Komm hierher zurueck und druecke ENTER.\n")
    print("Das Fenster gehoert diesem Programm - schliess es nicht selbst.\n")

    async with async_playwright() as p:
        ctx = await _oeffne(p, sichtbar=True)
        page = await ctx.new_page()
        await page.goto("https://de.aliexpress.com/",
                        wait_until="domcontentloaded", timeout=45000)
        try:
            # Blockiert bewusst: der Mensch meldet sich an, das Skript wartet.
            await asyncio.get_running_loop().run_in_executor(None, input)
            zustand = await _zustand(page)
        finally:
            await ctx.close()

    if zustand == "angemeldet":
        print(f"\nAngemeldet. Die Sitzung liegt jetzt im Profil ({PROFIL}) "
              "und gilt fuer alle spaeteren Scraper-Laeufe.")
        return 0
    print(f"\nNoch nicht angemeldet (erkannt: {zustand}). "
          "Starte das Skript noch einmal.")
    return 1


async def offenhalten(minuten: int) -> int:
    """Fenster oeffnen und einfach stehen lassen - OHNE auf die Enter-Taste zu warten.

    Der Weg ueber ``input()`` hat beim Betreiber nicht funktioniert (05.09.2026).
    Warum genau, ist offen - moeglich ist ein Fenster, das den Fokus zieht, eine
    Konsole ohne Eingabe, oder ein Start aus einer Umgebung ohne Tastatur.

    Diese Betriebsart braucht keine Eingabe: sie oeffnet das Fenster, haelt es
    die angegebene Zeit offen und schreibt jede Minute den Anmeldezustand mit.
    Sobald sie "angemeldet" sieht, ist sie fertig und schliesst.
    """
    from playwright.async_api import async_playwright

    print(f"Fenster wird geoeffnet und bleibt bis zu {minuten} Minuten stehen.")
    print("Melde dich in Ruhe an - du musst hier NICHTS druecken.\n")

    async with async_playwright() as p:
        ctx = await _oeffne(p, sichtbar=True)
        page = await ctx.new_page()
        await page.goto("https://de.aliexpress.com/",
                        wait_until="domcontentloaded", timeout=45000)
        zustand = "unklar"
        try:
            for verstrichen in range(minuten):
                await page.wait_for_timeout(60_000)
                zustand = await _zustand(page)
                print(f"  nach {verstrichen + 1} min: {zustand}", flush=True)
                if zustand == "angemeldet":
                    break
        finally:
            await ctx.close()

    if zustand == "angemeldet":
        print(f"\nAngemeldet. Die Sitzung liegt im Profil ({PROFIL}).")
        return 0
    print(f"\nNoch nicht angemeldet (zuletzt: {zustand}).")
    return 1


def main() -> int:
    p = argparse.ArgumentParser(
        description="Internen Browser einmal bei AliExpress anmelden.")
    p.add_argument("--pruefen", action="store_true",
                   help="nur nachsehen, ob die Anmeldung noch steht")
    p.add_argument("--warten", type=int, metavar="MINUTEN",
                   help="Fenster oeffnen und so lange offen lassen, ohne auf "
                        "eine Eingabe zu warten")
    args = p.parse_args()
    if args.pruefen:
        return asyncio.run(pruefen())
    if args.warten:
        return asyncio.run(offenhalten(args.warten))
    return asyncio.run(anmelden())


if __name__ == "__main__":
    raise SystemExit(main())
