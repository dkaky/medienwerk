"""Stufe 2: aus einer Shop-Seite Artikel samt Verkaufssignal holen.

Zwei Haelften, mit Absicht getrennt:

* ``deute()`` liest aus dem Text einer Trefferkarte, was darin an Zahlen steht.
  Reine Rechnerei, ohne Netz - und damit pruefbar.
* ``ernte()`` faehrt den Browser. Das laesst sich nur im Betrieb pruefen.

Was am 05.09.2026 an den echten Seiten nachgesehen wurde (damit es niemand
zweimal herausfinden muss):

* **eBay** liefert die Treffer als ``li.s-card`` (neues Layout) bzw. ``li.s-item``
  (altes). Eine Verkaufszahl steht NICHT auf jeder Karte - mal "12+ verkauft",
  meistens gar nichts.
* **eBay kennt einen Filter fuer verkaufte Artikel** (``LH_Sold=1&LH_Complete=1``).
  Das waere das beste Signal, das es gibt - er verlangt aber eine Anmeldung
  (ohne sie landet der Browser auf ``signin.ebay.de``). Deshalb ist
  ``nur_verkauft`` standardmaessig aus, und wenn er an ist, gibt es bei
  fehlender Anmeldung einen klaren Fehler statt einer leeren Liste.
* **Etsy** liefert diesem Browser eine komplett leere Seite (Bot-Schutz). Kommt
  von dort nichts, ist das kein "der Shop hat keine Artikel", sondern eine
  Abweisung - und muss auch so gemeldet werden.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.studio.radar.quellen import Quelle

logger = logging.getLogger("app.studio.radar.ernte")

MAX_JE_LAUF = 100          # dieselbe Deckelung wie beim Shop-Import
_SCROLLS = 14

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# Eine Zahl, wie eine Shop-Karte sie schreibt: "12", "1.204", "1 204".
#
# Zwei Fallen stecken darin, beide am 05.09.2026 im Selbstversuch getreten:
#   * Der Ausdruck darf NICHT ueber einen Zeilenumbruch laufen. "EUR 18,68"
#     und eine Zeile spaeter "12+ verkauft" wurden sonst zu "6812 verkauft".
#     Deshalb ``[^\S\n]`` (Leerraum ohne Umbruch) statt ``\s``.
#   * Vor der Zahl darf keine Ziffer und kein Komma stehen, sonst wird aus dem
#     Nachkommateil eines Preises eine Verkaufszahl.
_ZAHL = r"(?<![\d,])(\d{1,3}(?:[.  ]\d{3})+|\d+)"
_H = r"[^\S\n]*"                                   # Leerraum, aber kein Umbruch

# "12+ verkauft", "1.204 verkauft", "312 sold"
_VERKAUFT = re.compile(rf"{_ZAHL}{_H}\+?{_H}(?:verkauft|sold)\b", re.I)
# "5 Beobachter", "23 watchers"
_BEOBACHTER = re.compile(rf"{_ZAHL}{_H}(?:Beobachter|watcher)", re.I)
# "1.234 Bewertungen" (eBay) - Etsy schreibt die Zahl nur in Klammern
_BEWERTUNGEN = re.compile(rf"{_ZAHL}{_H}(?:Bewertung|review)", re.I)
_KLAMMERZAHL = re.compile(rf"\({_ZAHL}\)")

_ID_AUS_URL = (
    re.compile(r"/itm/(\d{6,})"),            # eBay
    re.compile(r"/listing/(\d{6,})"),        # Etsy
    re.compile(r"/(\d{6,})[.\-]"),           # Redbubble / Spreadshirt
)


#: Werbekarten, die eBay zwischen die Treffer schiebt. Sie sehen aus wie ein
#: Artikel, tragen aber die Platzhalternummer 123456 und den Titel "Shop on
#: eBay". Im ersten Lauf gegen einen echten Shop stand so eine Karte auf Platz
#: 1 - und haette als bestverkauftes "Motiv" gegolten.
_WERBETITEL = {"shop on ebay", "auf ebay einkaufen", "sponsored", "anzeige"}
_WERBE_ID = "123456"


def ist_werbung(titel: str, url: str | None) -> bool:
    """Werbekarte statt Artikel?"""
    if (titel or "").strip().lower() in _WERBETITEL:
        return True
    return fremd_id_aus_url(url) == _WERBE_ID


class ErnteFehler(Exception):
    """Die Shop-Seite gab nichts her - der Grund steht im Text."""


@dataclass(frozen=True)
class Fund:
    """Ein fremder Artikel, so wie die Shop-Seite ihn zeigt.

    Alle Zahlen duerfen ``None`` sein. Eine fehlende Verkaufszahl ist NICHT
    null Verkaeufe - sie ist eine Luecke, und die bleibt sichtbar
    (Projektregel 3: keine Schaetzungen).
    """

    titel: str
    url: str | None = None
    fremd_id: str | None = None
    bild_url: str | None = None
    verkauft: int | None = None
    bewertungen: int | None = None
    beobachter: int | None = None
    platz: int | None = None


def _zahl(roh: str | None) -> int | None:
    """"1.204" -> 1204. Punkt und Leerzeichen sind Tausendertrenner."""
    if not roh:
        return None
    ziffern = re.sub(r"[.\s]", "", roh)
    return int(ziffern) if ziffern.isdigit() else None


def fremd_id_aus_url(url: str | None) -> str | None:
    """Artikelnummer aus dem Link - haelt denselben Fund ueber Laeufe zusammen."""
    if not url:
        return None
    for pat in _ID_AUS_URL:
        treffer = pat.search(url)
        if treffer:
            return treffer.group(1)
    return None


#: eBay legt jedes Bild in mehreren Groessen ab; der Dateiname sagt welche.
#: Die Karte zeigt "s-l500.webp" - fuer eine Motivbeschreibung ist das zu wenig.
#: "s-l1600.jpg" liefert dieselbe Aufnahme in 1600x1600 (geprueft 05.09.2026).
_BILDGROESSE = re.compile(r"/s-l\d+\.(?:webp|jpg|jpeg|png)$", re.I)


def grossbild(url: str | None) -> str | None:
    """Die grosse Fassung eines eBay-Bildes - fuer alles andere unveraendert."""
    if not url:
        return None
    if "i.ebayimg.com" not in url:
        return url
    ohne_anhang = url.split("?", 1)[0]
    return _BILDGROESSE.sub("/s-l1600.jpg", ohne_anhang)


def _erste_zahl(regex: re.Pattern, text: str) -> int | None:
    treffer = regex.search(text)
    return _zahl(treffer.group(1)) if treffer else None


def deute(text: str, *, titel: str, url: str | None = None,
          platz: int | None = None, plattform: str | None = None,
          bild_url: str | None = None) -> Fund:
    """Aus dem sichtbaren Text einer Karte die Zahlen lesen.

    ``titel`` kommt getrennt herein: im Kartentext steht er zusammen mit Preis,
    Versandhinweis und "Wird in neuem Fenster geoeffnet" - wer ihn aus dem
    Fliesstext schneiden wollte, schnitte frueher oder spaeter falsch.

    ``plattform`` entscheidet ueber die Klammerzahl. Bei Etsy steht in Klammern
    die Bewertungszahl DES ARTIKELS. Auf eBay steht dort die des VERKAEUFERS
    ("muchwerk 99,5% positiv (744)") - dieselbe Zahl auf jeder einzelnen Karte.
    Wer sie uebernimmt, gibt jedem Artikel eines grossen Verkaeufers dasselbe
    Signal und haelt Rauschen fuer eine Auskunft.
    """
    t = text or ""
    bewertungen = _erste_zahl(_BEWERTUNGEN, t)
    if bewertungen is None and (plattform or "").lower() != "ebay":
        treffer = _KLAMMERZAHL.search(t)
        bewertungen = _zahl(treffer.group(1)) if treffer else None
    return Fund(
        titel=(titel or "").strip(),
        url=url,
        fremd_id=fremd_id_aus_url(url),
        bild_url=grossbild(bild_url),
        verkauft=_erste_zahl(_VERKAUFT, t),
        bewertungen=bewertungen,
        beobachter=_erste_zahl(_BEOBACHTER, t),
        platz=platz,
    )


def nur_verkauft_url(quelle: Quelle) -> str:
    """eBay-Listenseite, gefiltert auf TATSAECHLICH VERKAUFTE Artikel.

    Das beste Marktsignal, das eBay hergibt - und das einzige, das eine
    Anmeldung verlangt.
    """
    if quelle.plattform != "ebay":
        raise ErnteFehler(
            "Einen Filter fuer verkaufte Artikel gibt es nur bei eBay, nicht "
            f"bei {quelle.plattform}.")
    # ``_sop`` steht in der Listen-URL schon drin (12 = beste Ergebnisse). Ein
    # zweites anzuhaengen ist Glueckssache - eBay nimmt dann irgendeins. Hier
    # zaehlt 13 (zuletzt beendet), also wird das vorhandene ERSETZT.
    # Herausschneiden allein genuegt nicht: stuende ``_sop`` als ERSTER
    # Parameter, risse "?_sop=12" das Fragezeichen mit weg und der Rest der
    # Adresse waere Teil des Pfades. Deshalb wird der Anhang neu gebaut.
    pfad, _, anhang = quelle.listen_url.partition("?")
    teile = [t for t in anhang.split("&") if t and not t.startswith("_sop=")]
    teile += ["LH_Sold=1", "LH_Complete=1", "_sop=13"]
    return f"{pfad}?{'&'.join(teile)}"


# Die Sammelanweisung laeuft IM BROWSER. Sie holt je Karte Titel, Link und den
# sichtbaren Text - gedeutet wird drueben in Python, damit die Regeln an einer
# Stelle stehen und pruefbar bleiben.
_SAMMLER = r"""
() => {
  const raus = [], gesehen = new Set();
  const karten = [...document.querySelectorAll(
      'li.s-card, li.s-item, [data-listing-id], .shared-product-card')];
  // Rueckfall fuer unbekannte Layouts: vom Artikel-Link aus nach oben suchen.
  // Die eBay-Shopseite (/str/) baut ihre Karussells ohne s-card - ohne diesen
  // Weg findet der Sammler dort keinen einzigen Artikel.
  if (!karten.length) {
    for (const a of document.querySelectorAll('a[href*="/itm/"], a[href*="/listing/"]')) {
      const k = a.closest('li, article, [class*="card"]') || a.parentElement;
      if (k && !karten.includes(k)) karten.push(k);
    }
  }
  for (const k of karten) {
    const a = k.querySelector('a[href*="/itm/"], a[href*="/listing/"], a[href]')
           || (k.matches && k.matches('a') ? k : null);
    const url = a ? a.href : null;
    if (!url || gesehen.has(url)) continue;
    gesehen.add(url);
    const h = k.querySelector('[role=heading], .s-item__title, h3, h2');
    const roh = h ? h.innerText : (a.getAttribute('title') || a.innerText || '');
    const titel = String(roh).split('\n')[0].trim();
    if (!titel) continue;
    const bild = k.querySelector('img');
    raus.push({titel: titel, url: url, text: k.innerText || '',
               bild: bild ? (bild.currentSrc || bild.src || '') : ''});
  }
  return raus;
}
"""

#: Den Verkaeufernamen findet man im Quelltext jeder Shopseite.
_SSN_IM_HTML = re.compile(r"_ssn=([A-Za-z0-9._\-]{3,64})")


async def _artikelliste(page, quelle: Quelle, url: str, *, timeout_ms: int) -> str:
    """Die Seite mit der VOLLSTAENDIGEN Artikelliste ansteuern.

    Fund vom 05.09.2026: ``ebay.de/str/<shopname>`` ist eine Schaufensterseite
    mit Karussells - und der Shopname ist NICHT der Verkaeufername (der Shop
    "brunobu" gehoert dem Verkaeufer "muchwerk"). Die vollstaendige, blaetterbare
    Liste haengt am Verkaeufernamen: ``sch/i.html?_ssn=<verkaeufer>``. Der Name
    steht im Quelltext der Shopseite, also wird er dort abgeholt.
    """
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    await page.wait_for_timeout(4000)
    if quelle.plattform != "ebay" or "/str/" not in url:
        return url

    treffer = _SSN_IM_HTML.search(await page.content())
    if not treffer:
        logger.warning("kein Verkaeufername auf der Shopseite",
                       extra={"quelle": quelle.schluessel})
        return url
    ziel = suchseite(treffer.group(1), url)
    await page.goto(ziel, wait_until="domcontentloaded", timeout=timeout_ms)
    await page.wait_for_timeout(4000)
    return ziel


def suchseite(verkaeufer: str, shop_url: str) -> str:
    """Aus Verkaeufername + Shop-URL die Suchseite bauen.

    Die Angaben hinter dem Fragezeichen (Seitengroesse, Sortierung, der
    Verkauft-Filter) sollen erhalten bleiben - nur der Weg davor aendert sich.
    """
    _, _, anhang = shop_url.partition("?")
    basis = f"https://www.ebay.de/sch/i.html?_ssn={verkaeufer}"
    return f"{basis}&{anhang}" if anhang else basis


async def _kontext(p):
    """Browser mit dem dauerhaften Profil - dem, an dem die Anmeldungen haengen."""
    from app.browser_profil import START_ARGUMENTE, starte_profil

    args = list(START_ARGUMENTE)
    try:
        ctx, _kanal = await starte_profil(
            p, args=args, user_agent=_UA, locale="de-DE",
            viewport={"width": 1366, "height": 900})
        return ctx, None
    except Exception:  # noqa: BLE001 - Profil gesperrt/kaputt -> frischer Browser
        browser = await p.chromium.launch(args=args)
        ctx = await browser.new_context(user_agent=_UA, locale="de-DE",
                                        viewport={"width": 1366, "height": 900})
        return ctx, browser


async def ernte(quelle: Quelle, *, limit: int = 40, nur_verkauft: bool = False,
                timeout_ms: int = 45000) -> list[Fund]:
    """Die Artikel eines fremden Shops holen - nur lesen, nichts anfassen."""
    limit = max(1, min(int(limit), MAX_JE_LAUF))
    url = nur_verkauft_url(quelle) if nur_verkauft else quelle.listen_url
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # noqa: BLE001
        raise ErnteFehler("Kein Browser installiert (playwright fehlt).") from exc

    async with async_playwright() as p:
        ctx, browser = await _kontext(p)
        try:
            page = await ctx.new_page()
            await _artikelliste(page, quelle, url, timeout_ms=timeout_ms)
            if "signin." in (page.url or ""):
                raise ErnteFehler(
                    "Die Seite verlangt eine Anmeldung. Fuer verkaufte Artikel "
                    "muss das Browser-Profil bei eBay angemeldet sein "
                    "(scripts/browser_anmelden.py).")

            roh: list[dict] = []
            leer_am_stueck = 0
            for _ in range(_SCROLLS):
                vorher = len(roh)
                gefunden = await page.evaluate(_SAMMLER)
                bekannt = {r["url"] for r in roh}
                roh.extend(g for g in gefunden
                           if g["url"] not in bekannt
                           and not ist_werbung(g["titel"], g["url"]))
                if len(roh) >= limit:
                    break
                leer_am_stueck = 0 if len(roh) > vorher else leer_am_stueck + 1
                if leer_am_stueck >= 3 and roh:
                    break
                await page.mouse.wheel(0, 3500)
                await page.wait_for_timeout(1800)

            if not roh:
                # Eine leere Seite heisst bei Etsy & Co. "abgewiesen", nicht "leer".
                laenge = len((await page.inner_text("body")) or "")
                raise ErnteFehler(
                    "Kein einziger Artikel lesbar - die Seite hat den Browser "
                    "vermutlich abgewiesen (Bot-Schutz)."
                    if laenge < 200 else
                    "Kein einziger Artikel lesbar - Seitenaufbau unbekannt.")
        finally:
            await ctx.close()
            if browser is not None:
                await browser.close()

    logger.info("radar geerntet",
                extra={"quelle": quelle.schluessel, "anzahl": len(roh)})
    return [deute(r["text"], titel=r["titel"], url=r["url"], platz=i,
                  plattform=quelle.plattform, bild_url=r.get("bild"))
            for i, r in enumerate(roh[:limit], start=1)]
