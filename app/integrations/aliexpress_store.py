"""AliExpress-SHOP-Scraper: die meistverkauften Produkte eines Stores holen.

WARUM ein echter Browser: Die Dropshipping-API kann NICHT nach Shop filtern (geprueft 02.08.:
``sellerId`` in ``ds.text.search`` wird ignoriert und liefert Produkte FREMDER Shops; die
Methoden ``ds.seller.item.list.get`` / ``ds.store.item.query`` existieren nicht; die Affiliate-API
hat keine Berechtigung). Die Shop-Seite selbst ist per HTTP bot-geschuetzt (liefert nur ein
JavaScript-Geruest und leitet um). Ein Headless-Chromium rendert die Seite dagegen sauber.

Ablauf: Shop-Seite "alle Artikel" nach BESTSELLERN sortiert oeffnen -> scrollen (Nachladen) ->
Produkt-IDs in Reihenfolge einsammeln. Die IDs sind anschliessend ganz normal ueber
``ds.product.get`` abrufbar (verifiziert: die Treffer tragen die angefragte ``supplier_id``).
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("app.integrations.aliexpress_store")

# AliExpress sortiert "Orders" (meistverkauft) mit diesem Parameter.
_SORT_BESTSELLER = "totalTranpro_desc"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
_ITEM_RE = re.compile(r"/item/(\d{10,16})")


class StoreScrapeError(Exception):
    """Shop-Seite nicht lesbar (Browser fehlt, Timeout, Bot-Schutz, leere Seite)."""


# Dieselbe Ware traegt bei AliExpress zwei Nummern, die sich um exakt 2**51 unterscheiden
# (3256... und 1005...). Beide funktionieren mit ds.product.get. Die Shop-Seite liefert die
# 3256-Form, in unserer Datenbank steht oft die 1005-Form -> ohne Umrechnung wuerde die
# Duplikat-Pruefung dieselbe Ware nicht wiedererkennen.
_ID_OFFSET = 2251799813685248


def id_variants(product_id: str | int) -> list[str]:
    """Beide Schreibweisen derselben Ware (Original zuerst)."""
    s = str(product_id or "").strip()
    if not s.isdigit():
        return [s] if s else []
    n = int(s)
    andere = n - _ID_OFFSET if n > _ID_OFFSET else n + _ID_OFFSET
    return [s] if andere <= 0 else [s, str(andere)]


def extract_store_id(url_or_id: str) -> str | None:
    """Shop-Nummer aus einer AliExpress-Shop-URL ziehen (oder die reine Nummer durchreichen).

    Erkennt u.a.:
      https://www.aliexpress.com/store/1103573332
      https://de.aliexpress.com/store/1103573332/pages/all-items.html
      https://www.aliexpress.com/store/home.html?shopId=1103573332
      https://de.aliexpress.com/w/wholesale.html?...&sellerAdminSeq=1103573332
    """
    s = str(url_or_id or "").strip()
    if not s:
        return None
    if s.isdigit():
        return s
    for pat in (r"/store/(?:home\.html.*?[?&](?:shopId|storeId)=)?(\d{5,})",
                r"[?&](?:shopId|storeId|sellerAdminSeq|sellerId)=(\d{5,})"):
        m = re.search(pat, s, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def store_url(store_id: str, *, bestseller: bool = True) -> str:
    """Shop-Seite mit ALLEN Artikeln; standardmaessig nach Bestsellern sortiert."""
    base = f"https://www.aliexpress.com/store/{store_id}/pages/all-items.html"
    return f"{base}?sortType={_SORT_BESTSELLER}" if bestseller else base


# Ansichten derselben Store-Produktliste: Sortierungen zuerst (Bestseller bleibt
# vorn -> Import-Prioritaet), danach Preisbaender (EUR) als Rest-Beschaffung.
_SORTIERUNGEN = (_SORT_BESTSELLER, "created_desc", "price_asc", "price_desc")
_PREIS_BAENDER = ((0, 2), (2, 4), (4, 6), (6, 9), (9, 13), (13, 20), (20, 35),
                  (35, None))


def _store_ansichten(store_id: str, *, bestseller: bool = True) -> list[str]:
    """URL je ANSICHT der Store-Produktliste.

    Sonde 19.08. (527-Artikel-Store 1105317951): die Seite zeigt je Ansicht nur
    ~40 Artikel, hat KEIN Pagination-Element und ``&page=N`` liefert exakt
    dieselbe Ware. Jede Sortierung / jedes Preisband zeigt aber ANDERE ~40 —
    die Vereinigung der Ansichten ergibt den Katalog."""
    base = f"https://www.aliexpress.com/store/{store_id}/pages/all-items.html"
    urls = [store_url(store_id, bestseller=bestseller)]
    for sort in _SORTIERUNGEN:
        u = f"{base}?sortType={sort}"
        if u not in urls:
            urls.append(u)
    for lo, hi in _PREIS_BAENDER:
        preis = f"minPrice={lo}" + (f"&maxPrice={hi}" if hi is not None else "")
        urls.append(f"{base}?sortType=price_asc&{preis}")
    return urls


# Fuers Ableiten der In-Store-Suchbegriffe aus den Bestseller-Titeln: Fuellwoerter,
# die keine brauchbaren Suchbegriffe sind.
_BEGRIFF_STOPP = frozenset((
    "aliexpress", "item", "https", "http", "html", "sale", "shop", "store", "free",
    "und", "mit", "das", "der", "die", "von", "zum", "zur", "aus", "auf",
    "eine", "einen", "sich", "sind", "wird", "nach", "beim", "sowie", "oder",
    "with", "from", "this", "your", "gift", "hot", "new", "neue", "neu",
    "stück", "set", "paar", "shipping", "versand", "gratis",
    # Karten-Marketing-Geruell (Messung 20.08.: "verkauft"/"spare" wurden als
    # Begriffe abgeleitet — "1000+ verkauft", "Spare 20%", Muenzen-Aktionen).
    "verkauft", "spare", "sparen", "rabatt", "coins", "münzen", "bonus",
    "deal", "deals", "heute", "woche", "kostenlose", "kostenloser", "extra",
))


def _suchbegriffe_aus_texten(texte, *, anzahl: int = 12) -> list[str]:
    """Haeufigste Produkt-Woerter aus Karten-Texten -> In-Store-Suchbegriffe.

    Selbstkalibrierend je Store (Schmuck-Store liefert "halskette"/"ohrringe",
    ein Auto-Store "poliertuch"/"reiniger" ...). Nur Buchstaben-Woerter ab 4
    Zeichen, Fuellwoerter raus, nach Haeufigkeit sortiert."""
    from collections import Counter
    zaehler: Counter = Counter()
    for t in texte or []:
        for wort in re.findall(r"[a-zA-ZäöüÄÖÜß]{4,}", str(t or "").lower()):
            if wort not in _BEGRIFF_STOPP:
                zaehler[wort] += 1
    return [w for w, n in zaehler.most_common(anzahl) if n >= 2]


def _store_such_url(store_id: str, begriff: str) -> str:
    from urllib.parse import quote
    return (f"https://www.aliexpress.com/store/{store_id}/search"
            f"?SearchText={quote(begriff)}")


async def fetch_store_product_ids(store_id: str, *, limit: int = 25,
                                  bestseller: bool = True, max_scrolls: int = 30,
                                  timeout_ms: int = 45000, versuche: int = 3,
                                  protokoll: list | None = None) -> list[str]:
    """Produkt-IDs eines Shops in Reihenfolge (Bestseller zuerst), maximal ``limit`` Stueck.

    Die Blaetterung ueber ``&page=N`` (Ansatz 15.08.) ist TOT — die Sonde vom
    19.08. zeigte: dieselben 40 IDs auf jeder "Unterseite". Stattdessen werden
    mehrere ANSICHTEN derselben Liste geerntet (:func:`_store_ansichten`) und
    vereinigt — EINE Browser-Sitzung fuer alle Ansichten (statt je Seite ein
    eigener Browser-Start). Drei Ansichten ohne neue Ware = Katalog erschoepft.
    ``protokoll``: optionale Liste, je Ansicht wird ``{ansicht, geerntet, neu}``
    angehaengt (fuer Diagnose-Ausgaben).
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # noqa: BLE001 – Browser nicht installiert
        raise StoreScrapeError(
            "Headless-Browser fehlt auf dem Server (playwright + chromium installieren).") from exc
    out: list[str] = []
    seen: set[str] = set()
    try:
        async with async_playwright() as p:
            ctx, browser, page = await _kontext_oeffnen(p)
            try:
                ohne_neues = 0
                warteschlange = list(_store_ansichten(store_id,
                                                      bestseller=bestseller))
                i = -1
                while warteschlange:
                    i += 1
                    url = warteschlange.pop(0)
                    if len(out) >= limit:
                        break
                    try:
                        ids = await _seite_ernten(
                            page, url, limit=max(limit, 60),
                            max_scrolls=max_scrolls, timeout_ms=timeout_ms,
                            versuche=versuche if i == 0 else 1)
                    except Exception:  # noqa: BLE001 – spaetere Ansicht darf ausfallen
                        if i == 0:
                            raise
                        ids = []
                    neu = [x for x in ids if x not in seen]
                    for x in neu:
                        seen.add(x)
                        out.append(x)
                    if protokoll is not None:
                        protokoll.append(
                            {"ansicht": (url.split("?", 1)[1] if "?" in url
                                         else "basis")[:60],
                             "geerntet": len(ids), "neu": len(neu)})
                    if i == 0:
                        if not ids:
                            break       # Ansicht 1 leer -> unten sauber melden
                        if len(out) < limit:
                            # IN-STORE-SUCHE als Haupt-Nachschub (Sonde 19.08.:
                            # Sortierungen/Preisbaender liefern dieselben 40,
                            # die Store-Suche je Begriff aber ANDERE ~40).
                            # Begriffe kommen aus den Karten-Texten der Seite.
                            try:
                                texte = await page.eval_on_selector_all(
                                    'a[href*="/item/"]',
                                    "els => els.map(e => (e.textContent || '')"
                                    ".slice(0, 200))")
                            except Exception:  # noqa: BLE001
                                texte = []
                            begriffe = _suchbegriffe_aus_texten(texte)
                            warteschlange = ([_store_such_url(store_id, b)
                                              for b in begriffe]
                                             + warteschlange)
                    ohne_neues = 0 if neu else ohne_neues + 1
                    if ohne_neues >= 3:
                        break           # drei Ansichten ohne neue Ware: erschoepft
            finally:
                await ctx.close()
                if browser is not None:
                    await browser.close()
    except Exception as exc:  # noqa: BLE001 – Browser-/Netzfehler sauber melden
        if isinstance(exc, StoreScrapeError):
            raise
        raise StoreScrapeError(f"Seite nicht lesbar: {str(exc)[:180]}") from exc
    if not out:
        raise StoreScrapeError(
            "Keine Produkte auf der Seite gefunden (leer, URL falsch oder von "
            "AliExpress blockiert). Bitte Link pruefen und spaeter erneut versuchen.")
    logger.info("store ernte ok", extra={"store": store_id, "gefunden": len(out)})
    return out[:limit]


#: Reihenfolge der Browser, die probiert werden. Playwrights MITGELIEFERTER
#: Chromium (kein Kanal, daher None am Ende) startet auf diesem Windows zwar
#: unsichtbar, aber NICHT als Fenster - er scheitert mit "spawn UNKNOWN"
#: (gemessen 05.09.2026). Der installierte Chrome und Edge koennen beides.
#: Wichtig ist vor allem, dass Anmeldung und Scraper DENSELBEN Kanal nehmen -
#: sonst liest der Scraper ein Profil, das ein anderer Browser geschrieben hat.
BROWSER_KANAELE = ("chrome", "msedge", None)


async def _starte_profil(p, profil, *, sichtbar: bool = False, **kw):
    """Dauerhaftes Profil oeffnen - mit dem ersten Kanal, der laeuft.

    Gibt ``(ctx, kanal)`` zurueck. Wirft nur, wenn KEIN Kanal startet.
    """
    letzter = None
    for kanal in BROWSER_KANAELE:
        try:
            ctx = await p.chromium.launch_persistent_context(
                str(profil), headless=not sichtbar,
                **({"channel": kanal} if kanal else {}), **kw)
            return ctx, kanal or "playwright-chromium"
        except Exception as exc:  # noqa: BLE001 - naechsten Kanal probieren
            letzter = exc
    raise letzter if letzter else RuntimeError("Kein Browser startbar")


async def _kontext_oeffnen(p):
    """Browser-Kontext mit Tarnprofil, DE-Region-Cookie und Aufwaermbesuch.

    Rueckgabe ``(ctx, browser, page)`` — ``browser`` ist None, wenn das dauerhafte
    Profil benutzt wird (launch_persistent_context traegt den Browser selbst)."""
    _args = ["--no-sandbox", "--disable-dev-shm-usage",
             "--disable-blink-features=AutomationControlled"]
    from pathlib import Path as _P
    _profil = _P("data/browser_profile")
    browser = None
    try:
        _profil.mkdir(parents=True, exist_ok=True)
        ctx, _kanal = await _starte_profil(
            p, _profil, args=_args, user_agent=_UA, locale="de-DE",
            viewport={"width": 1366, "height": 900})
    except Exception:  # noqa: BLE001 – Profil gesperrt/kaputt -> frisch
        browser = await p.chromium.launch(args=_args)
        ctx = await browser.new_context(user_agent=_UA, locale="de-DE",
                                        viewport={"width": 1366, "height": 900})
    # Region FEST auf Deutschland pinnen (Fund 15.08.: ohne Region-Cookie zeigt
    # z.B. Store 1105638009 "0 items").
    try:
        await ctx.add_cookies([{
            "name": "aep_usuc_f",
            "value": "site=deu&c_tp=EUR&region=DE&b_locale=de_DE",
            "domain": ".aliexpress.com", "path": "/"}])
    except Exception:  # noqa: BLE001 – Kuer, nicht Pflicht
        pass
    page = await ctx.new_page()
    # Aufwaermbesuch: erst die Startseite (Basis-Cookies + echter Referer).
    try:
        await page.goto("https://de.aliexpress.com/",
                        wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(2500)
    except Exception:  # noqa: BLE001 – Aufwaermen ist Kuer, nicht Pflicht
        pass
    return ctx, browser, page


async def _seite_ernten(page, url: str, *, limit: int, max_scrolls: int,
                        timeout_ms: int, versuche: int) -> list[str]:
    """Ernte-Kern: EINE Seite auf ``url`` fahren und Item-IDs einsammeln
    (Scroll-Nachladen; Wiederholung, wenn AliExpress die Seite leer ausliefert).
    Gibt bei dauerhaft leerer Seite [] zurueck — die Fehler-Entscheidung trifft
    der Aufrufer (Seite 1 leer = Fehler, spaetere Ansicht leer = fertig)."""
    found: list[str] = []
    seen: set[str] = set()
    for versuch in range(1, max(1, versuche) + 1):
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(6000 if versuch == 1 else 9000)
        leer_am_stueck = 0
        for _ in range(max(1, max_scrolls)):
            vorher = len(found)
            links = await page.eval_on_selector_all(
                'a[href*="/item/"]', "els => els.map(e => e.href)")
            for pid in _ITEM_RE.findall(" ".join(links)):
                if pid not in seen:
                    seen.add(pid)
                    found.append(pid)
            if len(found) >= limit:
                break
            # Durchgescrollt: mehrere Runden ohne Zuwachs -> nichts kommt mehr nach.
            leer_am_stueck = 0 if len(found) > vorher else leer_am_stueck + 1
            # Geduld: grosse Shops laden traege nach; 2 leere Runden waren zu
            # streng (Abbruch bei ~40, Vorfall 15.08.).
            if leer_am_stueck >= 3 and found:
                break
            await page.mouse.wheel(0, 3500)
            await page.wait_for_timeout(2200)
        if found:
            break
        logger.warning("seiten scrape leer, neuer Versuch",
                       extra={"url": url[:120], "versuch": versuch})
    return found[:limit]


async def probe_page(url: str, *, timeout_ms: int = 45000) -> dict:
    """DIAGNOSE (nur lesen): Wie kommt man auf dieser Seite an MEHR Produkte?

    Fuer den 100er-Store-Import (19.08.: Ernte blieb bei ~40 haengen). Laedt die
    Seite, scrollt bis nichts mehr nachkommt und meldet dann alles Blaettern-
    Relevante: Gesamtzahl-Text, Pagination-Elemente, was ein Probeklick auf
    Weiter-Kandidaten bringt, und welche JSON-XHRs die Produktliste nachladen."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # noqa: BLE001
        raise StoreScrapeError("Headless-Browser fehlt auf dem Server.") from exc

    async def _ids(page) -> list[str]:
        links = await page.eval_on_selector_all(
            'a[href*="/item/"]', "els => els.map(e => e.href)")
        out, seen = [], set()
        for pid in _ITEM_RE.findall(" ".join(links)):
            if pid not in seen:
                seen.add(pid)
                out.append(pid)
        return out

    report: dict = {"url": url[:140], "xhrs": [], "klicks": []}
    async with async_playwright() as p:
        ctx, browser, page = await _kontext_oeffnen(p)
        try:
            def _xhr(resp):
                u = resp.url
                if (len(report["xhrs"]) < 20 and resp.request.resource_type in
                        ("xhr", "fetch") and
                        any(t in u.lower() for t in ("search", "mtop", "list",
                                                     "product", "item", "wtf"))):
                    report["xhrs"].append(f"{resp.status} {u[:200]}")
            page.on("response", _xhr)
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(6000)
            leer = 0
            for _ in range(20):
                vorher = len(await _ids(page))
                await page.mouse.wheel(0, 3500)
                await page.wait_for_timeout(2200)
                nachher = len(await _ids(page))
                leer = 0 if nachher > vorher else leer + 1
                if leer >= 3:
                    break
            ids = await _ids(page)
            report["ids_nach_scroll"] = len(ids)
            report["ids_erste5"] = ids[:5]
            report["total_text"] = await page.evaluate(
                "() => (document.body.innerText.match(/[\\d.,]+\\s*(Artikel|items|"
                "Produkte|products|Ergebnisse|results)/i) || [null])[0]")
            report["pagination"] = await page.evaluate(
                """() => [...document.querySelectorAll(
                     '[class*="pagination" i], [class*="pageNum" i], .comet-pagination')]
                   .slice(0, 3).map(e => e.outerHTML.slice(0, 300))""")
            # Kategorie-/Gruppen-Links des Stores: jede Gruppe hat ihre EIGENE
            # ~40er-Liste — Vereinigung ueber Gruppen = Weg zum vollen Katalog.
            report["group_links"] = await page.evaluate(
                """() => [...new Set([...document.querySelectorAll('a[href]')]
                     .map(a => a.href)
                     .filter(h => /productGroupId=|\\/category\\//i.test(h)))]
                   .slice(0, 30)""")
            # Weiter-/Mehr-Kandidaten probeweise klicken: bringt das neue IDs?
            kandidaten = [
                ".comet-pagination-next",
                'li[class*="next" i]:not([class*="disabled" i])',
                'button[class*="next" i]',
                'text="Mehr anzeigen"', 'text="View more"', 'text="Weiter"',
                'text="Next"', 'text="Mehr"',
            ]
            for sel in kandidaten:
                try:
                    el = page.locator(sel).first
                    if not await el.is_visible(timeout=800):
                        continue
                except Exception:  # noqa: BLE001
                    continue
                vorher = len(await _ids(page))
                try:
                    await el.click(timeout=3000)
                    await page.wait_for_timeout(3500)
                except Exception as exc:  # noqa: BLE001
                    report["klicks"].append({"sel": sel, "fehler": str(exc)[:80]})
                    continue
                nachher = len(await _ids(page))
                report["klicks"].append({"sel": sel, "vorher": vorher,
                                         "nachher": nachher})
                if nachher > vorher:
                    break               # Mechanik gefunden — reicht fuer die Diagnose
            # Blaettern per URL-Parameter: liefert &page=2 NEUE Ware?
            trenner = "&" if "?" in url else "?"
            await page.goto(f"{url}{trenner}page=2",
                            wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(6000)
            p2 = await _ids(page)
            report["page2_ids"] = len(p2)
            report["page2_neu"] = len([x for x in p2 if x not in ids])
        finally:
            await ctx.close()
            if browser is not None:
                await browser.close()
    return report


async def probe_mtop(store_id: str, *, timeout_ms: int = 45000) -> dict:
    """DIAGNOSE (nur lesen): kann man die interne Produktlisten-API der Store-
    Seite (mtop 'recommend') aus dem SEITENKONTEXT weiterblaettern?

    Die anonyme Store-Seite zeigt fix ~40 Artikel (Sonde 19.08.); die Liste
    kommt aus mtop-XHRs. Deren Signatur berechnet die Seiten-Bibliothek
    ``lib.mtop`` selbst — wenn wir sie mit erhoehter Seitennummer aufrufen
    koennen, ist der volle Katalog erreichbar."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # noqa: BLE001
        raise StoreScrapeError("Headless-Browser fehlt auf dem Server.") from exc
    import json as _json
    from urllib.parse import parse_qs, unquote, urlparse

    report: dict = {"recommend_urls": []}
    async with async_playwright() as p:
        ctx, browser, page = await _kontext_oeffnen(p)
        try:
            def _xhr(resp):
                u = resp.url
                if ("relationrecommend" in u
                        and len(report["recommend_urls"]) < 6):
                    report["recommend_urls"].append(u)
            page.on("response", _xhr)
            await page.goto(store_url(store_id), wait_until="domcontentloaded",
                            timeout=timeout_ms)
            await page.wait_for_timeout(7000)
            for _ in range(4):
                await page.mouse.wheel(0, 3500)
                await page.wait_for_timeout(2000)
            report["hat_lib_mtop"] = await page.evaluate(
                "() => typeof lib !== 'undefined' && !!(lib.mtop && lib.mtop.request)")
            if not report["recommend_urls"]:
                return report
            # data-Payload des letzten recommend-Calls verstehen und mit
            # erhoehter Seitenzahl erneut aufrufen (lib.mtop signiert selbst).
            qs = parse_qs(urlparse(report["recommend_urls"][-1]).query)
            data_raw = unquote((qs.get("data") or [""])[0])
            report["data_keys"] = sorted(list((_json.loads(data_raw) or {}).keys()))[:12] \
                if data_raw.startswith("{") else None
            report["data_probe"] = data_raw[:400]
            api_name = (qs.get("api") or ["mtop.relationrecommend.AliexpressRecommend.recommend"])[0]
            if report["hat_lib_mtop"] and data_raw.startswith("{"):
                report["nachruf"] = await page.evaluate(
                    """async ([apiName, dataRaw]) => {
                         const d = JSON.parse(dataRaw);
                         // Seitenzahl-artige Felder erhoehen (auch in params-Strings)
                         const bump = (o) => {
                           for (const k of Object.keys(o)) {
                             if (typeof o[k] === 'object' && o[k]) bump(o[k]);
                             else if (/^(page|pageNo|beginPage|pageIndex|pageNum)$/i.test(k))
                               o[k] = String(Number(o[k] || 1) + 1);
                             else if (typeof o[k] === 'string' && o[k].startsWith('{')) {
                               try { const inner = JSON.parse(o[k]); bump(inner);
                                     o[k] = JSON.stringify(inner); } catch (e) {}
                             }
                           }
                         };
                         bump(d);
                         return await new Promise((res) => {
                           lib.mtop.request({
                             api: apiName, v: '1.0', type: 'originaljson',
                             dataType: 'originaljson', data: d,
                           }, (r) => {
                             const s = JSON.stringify(r).slice(0, 60000);
                             const ids = [...new Set((s.match(/\"productId\\\":\\\"?(\\d{10,16})/g) || [])
                               .map(x => x.replace(/\\D/g, '')))];
                             res({ok: true, ids: ids.slice(0, 10), n: ids.length,
                                  ret: (r && r.ret ? String(r.ret).slice(0, 80) : null)});
                           }, (e) => res({ok: false, fehler: String(e && e.ret ? e.ret : e).slice(0, 120)}));
                         });
                       }""", [api_name, data_raw])
        finally:
            await ctx.close()
            if browser is not None:
                await browser.close()
    return report


async def fetch_page_product_ids(url: str, *, limit: int = 25, max_scrolls: int = 14,
                                 timeout_ms: int = 45000, versuche: int = 3) -> list[str]:
    """Produkt-IDs einer BELIEBIGEN AliExpress-Seite (Shop, Kampagne wie "Local+",
    gefilterte Suche) in Seiten-Reihenfolge, maximal ``limit`` Stueck.

    Faellt der Browser aus oder liefert die Seite nichts, wird ``StoreScrapeError`` geworfen –
    NIE eine stille leere Liste (sonst wirkt ein Bot-Block wie "Seite hat keine Produkte").

    AliExpress liefert Seiten gelegentlich leer aus (live beobachtet: dieselbe Seite war
    Sekunden spaeter wieder vollstaendig da). Ein einzelner Aussetzer darf den Lauf nicht
    beenden, deshalb wird bis zu ``versuche``-mal neu geladen.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # noqa: BLE001 – Browser nicht installiert
        raise StoreScrapeError(
            "Headless-Browser fehlt auf dem Server (playwright + chromium installieren).") from exc
    try:
        async with async_playwright() as p:
            # Tarnung gegen Bot-Blocks (Store 1105638009, 15.08.): dauerhaftes
            # Profil + DE-Region-Cookie + Aufwaermbesuch — siehe _kontext_oeffnen.
            ctx, browser, page = await _kontext_oeffnen(p)
            try:
                found = await _seite_ernten(page, url, limit=limit,
                                            max_scrolls=max_scrolls,
                                            timeout_ms=timeout_ms, versuche=versuche)
            finally:
                await ctx.close()
                if browser is not None:
                    await browser.close()
    except Exception as exc:  # noqa: BLE001 – Browser-/Netzfehler sauber melden
        if isinstance(exc, StoreScrapeError):
            raise
        raise StoreScrapeError(f"Seite nicht lesbar: {str(exc)[:180]}") from exc

    if not found:
        raise StoreScrapeError(
            "Keine Produkte auf der Seite gefunden (leer, URL falsch oder von "
            "AliExpress blockiert). Bitte Link pruefen und spaeter erneut versuchen.")
    logger.info("seiten scrape ok", extra={"url": url[:120], "gefunden": len(found)})
    return found[:limit]


def product_url(product_id: str) -> str:
    return f"https://de.aliexpress.com/item/{product_id}.html"
