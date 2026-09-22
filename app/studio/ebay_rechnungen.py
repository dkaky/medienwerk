"""Die ECHTE eBay-Rechnung/Packzettel je Bestellung holen - kein Ersatzbeleg.

eBay bietet dafuer keine REST-Schnittstelle an (recherchiert 22.09.2026). Der einzige
Weg ist derselbe wie im Verkaeufer-Cockpit von Hand: die Bestellung im Cockpit oeffnen,
"Weitere Aktionen" -> "Rechnungen drucken und mehr" -> eBay erzeugt serverseitig ein PDF
und zeigt es in einer Vorschau (technisch: eine ``blob:``-URL im Browser). Dieses Modul
macht genau das per Playwright, mit dem dauerhaften, angemeldeten Browser-Profil
(``app/browser_profil.py`` - demselben, an dem der Marktcheck und der AliExpress-Login
haengen).

Braucht eine einmalige Anmeldung: ``scripts/ebay_anmelden.py``.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("app.studio.ebay_rechnungen")

_ORDERS_URL = "https://www.ebay.de/sh/ord"


class RechnungFehler(RuntimeError):
    """Die echte eBay-Rechnung liess sich nicht holen (z. B. nicht angemeldet)."""


async def _kontext(p, *, sichtbar: bool = False):
    from app.browser_profil import START_ARGUMENTE, starte_profil

    ctx, _kanal = await starte_profil(
        p, sichtbar=sichtbar, args=list(START_ARGUMENTE),
        locale="de-DE", viewport={"width": 1366, "height": 900})
    return ctx


async def _pdf_fuer_offene_seite(page, bestellnummer: str, *, timeout_ms: int) -> bytes:
    """Auf der bereits gefilterten Bestellungsseite: Checkbox, Menue, PDF holen."""
    checkbox = page.locator(f"input[name='grid-table-bulk-checkbox_order{bestellnummer}']")
    if await checkbox.count() == 0:
        raise RechnungFehler(f"Bestellung {bestellnummer} nicht in der Liste gefunden.")
    await checkbox.click(force=True, timeout=timeout_ms)
    await page.wait_for_timeout(500)

    toggle = page.locator("button[aria-label='Weitere Aktionen anzeigen']").first
    await toggle.click(timeout=timeout_ms)
    await page.wait_for_timeout(400)

    drucken = page.locator("button[data-action-name='PrintPackingSlipAndMore']").first
    await drucken.click(timeout=timeout_ms)

    # eBay erzeugt das PDF serverseitig und zeigt es in einem blob:-iframe - das
    # kann kurz dauern, deshalb mehrfach nachsehen statt fest zu warten.
    blob_url = None
    for _ in range(20):
        for frame in page.frames:
            if frame.url.startswith("blob:"):
                blob_url = frame.url
                break
        if blob_url:
            break
        await page.wait_for_timeout(500)
    if not blob_url:
        raise RechnungFehler(
            f"eBay hat fuer Bestellung {bestellnummer} keine Rechnungs-Vorschau erzeugt "
            "(evtl. ist noch keine Rechnung ausgestellt oder die Anmeldung ist abgelaufen).")

    b64 = await page.evaluate(
        """async (u) => {
            const r = await fetch(u);
            const buf = await r.arrayBuffer();
            let bin = "";
            const bytes = new Uint8Array(buf);
            for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
            return btoa(bin);
        }""", blob_url)
    import base64

    daten = base64.b64decode(b64)
    if not daten.startswith(b"%PDF"):
        raise RechnungFehler(f"Antwort fuer Bestellung {bestellnummer} sieht nicht wie ein PDF aus.")
    return daten


async def rechnung_pdf(bestellnummer: str, *, sichtbar: bool = False, timeout_ms: int = 45000) -> bytes:
    """Die echte eBay-Rechnung/Packzettel EINER Bestellung als PDF-Bytes."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        ctx = await _kontext(p, sichtbar=sichtbar)
        try:
            page = await ctx.new_page()
            await page.goto(f"{_ORDERS_URL}?filter=status:ALL&q={bestellnummer}",
                            wait_until="domcontentloaded", timeout=timeout_ms)
            if "signin." in (page.url or ""):
                raise RechnungFehler(
                    "Die Seite verlangt eine Anmeldung. Browser-Profil muss bei eBay "
                    "angemeldet sein (scripts/ebay_anmelden.py).")
            await page.wait_for_timeout(1500)
            return await _pdf_fuer_offene_seite(page, bestellnummer, timeout_ms=timeout_ms)
        finally:
            await ctx.close()


async def rechnungen_pdf(bestellnummern: list[str], *, sichtbar: bool = False,
                         timeout_ms: int = 45000) -> dict[str, bytes | Exception]:
    """Mehrere Rechnungen in EINER Browser-Sitzung holen (schneller als je Bestellung neu zu starten).

    Gibt je Bestellnummer entweder die PDF-Bytes oder die aufgetretene Ausnahme zurueck -
    eine kaputte Bestellung darf die anderen nicht verhindern.
    """
    from playwright.async_api import async_playwright

    aus: dict[str, bytes | Exception] = {}
    if not bestellnummern:
        return aus
    async with async_playwright() as p:
        ctx = await _kontext(p, sichtbar=sichtbar)
        try:
            page = await ctx.new_page()
            for nr in bestellnummern:
                try:
                    await page.goto(f"{_ORDERS_URL}?filter=status:ALL&q={nr}",
                                    wait_until="domcontentloaded", timeout=timeout_ms)
                    if "signin." in (page.url or ""):
                        raise RechnungFehler(
                            "Die Seite verlangt eine Anmeldung. Browser-Profil muss bei eBay "
                            "angemeldet sein (scripts/ebay_anmelden.py).")
                    await page.wait_for_timeout(1500)
                    aus[nr] = await _pdf_fuer_offene_seite(page, nr, timeout_ms=timeout_ms)
                except Exception as exc:  # noqa: BLE001 - je Bestellung melden, weitermachen
                    logger.warning("eBay-Rechnung fuer %s fehlgeschlagen: %s", nr, str(exc)[:200])
                    aus[nr] = exc
        finally:
            await ctx.close()
    return aus
