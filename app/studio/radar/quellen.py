"""Stufe 1: Shop-Link rein - Plattform, Shop und Listen-URL raus.

Warum eine eigene Datei fuer so wenig Text: Der Link ist die einzige Eingabe des
ganzen Radars. Was hier falsch erkannt wird, erntet stundenlang den falschen
Shop. Und jede Plattform bringt ihre eigene Eigenart mit - eBay kennt drei
Schreibweisen fuer denselben Verkaeufer, Etsy haengt an den Shopnamen gern noch
einen Pfad.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Die Plattformen, die das Radar als Inspirations- und Trendsignal lesen kann.
# Es importiert dabei keine Produkte und loest keine Beschaffung aus.
PLATTFORMEN = ("ebay", "etsy", "redbubble", "spreadshirt")


class QuelleUnklar(Exception):
    """Aus dem Link laesst sich kein Shop lesen."""


@dataclass(frozen=True)
class Quelle:
    """Ein Shop, den das Radar beobachten kann."""

    plattform: str
    shop: str
    listen_url: str

    @property
    def schluessel(self) -> str:
        """Stabiler Name fuer Fortschritt und Anzeige."""
        return f"{self.plattform}:{self.shop}"


# eBay kennt drei Wege zum selben Verkaeufer: /str/<Shopname>, /usr/<Nutzer> und
# die Suche mit ``_ssn``. Der Shopname aus /str/ ist NICHT der Nutzername - die
# Suche braucht aber den Nutzernamen. Deshalb merkt sich ``_EBAY`` welcher Fund
# welcher Art ist.
_EBAY_STR = re.compile(r"ebay\.[a-z.]+/str/([A-Za-z0-9._\-]+)", re.I)
_EBAY_USR = re.compile(r"ebay\.[a-z.]+/usr/([A-Za-z0-9._\-]+)", re.I)
_EBAY_SSN = re.compile(r"[?&]_ssn=([A-Za-z0-9._\-]+)", re.I)
_ETSY = re.compile(r"etsy\.com/(?:[a-z\-]+/)?shop/([A-Za-z0-9._\-]+)", re.I)
_REDBUBBLE = re.compile(r"redbubble\.com/(?:[a-z\-]+/)?people/([A-Za-z0-9._\-]+)", re.I)
_SPREADSHIRT = re.compile(r"([A-Za-z0-9._\-]+)\.myspreadshop\.[a-z.]+", re.I)


def _ebay_listen_url(shop: str, *, ist_shopname: bool) -> str:
    """Alle Artikel eines eBay-Verkaeufers, 240 pro Seite.

    eBay hat KEINE Sortierung nach Verkaufszahl - ``_sop=12`` ist "Beste
    Ergebnisse". Die Verkaufszahl steht statt dessen auf jeder Karte
    ("123 verkauft"); die Rangfolge entsteht darum erst in Stufe 3 bei uns,
    nicht schon durch die Sortierung des Shops. ``_ipg=240`` spart Blaettern.
    """
    if ist_shopname:
        # Der Shopname funktioniert nur als Seite, nicht als Suchbegriff.
        return f"https://www.ebay.de/str/{shop}?_ipg=240&_sop=12"
    return f"https://www.ebay.de/sch/i.html?_ssn={shop}&_ipg=240&_sop=12"


def erkenne(link_oder_name: str) -> Quelle:
    """Aus einem Link (oder ``plattform:shop``) eine Quelle machen.

    Wirft ``QuelleUnklar``, statt zu raten. Ein falsch geratener Shop faellt
    erst Stunden spaeter auf - dann ist die Ernte Muell.
    """
    s = str(link_oder_name or "").strip()
    if not s:
        raise QuelleUnklar("Kein Link angegeben.")

    # Kurzform "ebay:druckhelden" - so steht es auch in der Datenbank.
    if ":" in s and "//" not in s:
        kopf, _, rest = s.partition(":")
        kopf = kopf.strip().lower()
        if kopf in PLATTFORMEN and rest.strip():
            return _bau(kopf, rest.strip())

    for regex, plattform in ((_EBAY_STR, "ebay"), (_EBAY_USR, "ebay"),
                             (_EBAY_SSN, "ebay"), (_ETSY, "etsy"),
                             (_REDBUBBLE, "redbubble"),
                             (_SPREADSHIRT, "spreadshirt")):
        treffer = regex.search(s)
        if treffer:
            ist_shopname = regex is _EBAY_STR
            return _bau(plattform, treffer.group(1), ebay_shopname=ist_shopname)

    raise QuelleUnklar(
        "Aus diesem Link laesst sich kein Shop lesen. Erkannt werden eBay "
        "(/str/, /usr/, _ssn=), Etsy (/shop/), Redbubble (/people/) und "
        "Spreadshirt (<name>.myspreadshop.de).")


def _bau(plattform: str, shop: str, *, ebay_shopname: bool = False) -> Quelle:
    shop = shop.strip().strip("/")
    if plattform == "ebay":
        return Quelle("ebay", shop, _ebay_listen_url(shop, ist_shopname=ebay_shopname))
    if plattform == "etsy":
        # Etsy sortiert die Shopseite nach dem Willen des Verkaeufers. Das ist
        # kein Nachteil: der Verkaeufer stellt vorn hin, was laeuft.
        return Quelle("etsy", shop, f"https://www.etsy.com/de/shop/{shop}")
    if plattform == "redbubble":
        return Quelle("redbubble", shop,
                      f"https://www.redbubble.com/de/people/{shop}/shop?sortOrder=top%20selling")
    if plattform == "spreadshirt":
        return Quelle("spreadshirt", shop, f"https://{shop}.myspreadshop.de/alle")
    raise QuelleUnklar(f"Plattform '{plattform}' kennt das Radar nicht.")
