"""Startklar-Pruefung: was passiert, wenn dieser Entwurf live geht?

Der eingebaute Trockenlauf (``publish_queue.enqueue_all_drafts(dry_run=True)``)
prueft genau EINE Sache: ob ein Preis gesetzt ist. Er meldet trotzdem
"bereit: 30" - auch wenn Bilder fehlen, die Marge nicht traegt oder die
Markenangabe gegen die Projektregel verstoesst. Diese falsche Sicherheit ist
teuer: eBay-Einstellgebuehren fallen sofort an, und das Beenden von Listings ist
bewusst gesperrt (Eiserne Regel 2). Wer "30 bereit" liest und klickt, kann den
Klick nicht zuruecknehmen.

Diese Pruefung beantwortet dieselbe Frage VOR dem Klick und **ohne einen
einzigen Netz-Aufruf**: kein eBay, keine Kosten, keine Nebenwirkung. Sie
schaetzt nichts - fehlt ein Wert, sagt sie das, statt eine Zahl zu erfinden
(Eiserne Regel 3).

Zwei Schweregrade, sauber getrennt:

``rot``
    Der Live-Gang wuerde **scheitern**. Entweder lehnt eBay ab (kein Preis,
    Menge 0) oder der Code bricht selbst ab (``publish_listing_live`` wirft bei
    fehlenden Bildern einen ``PersistentError``).

``gelb``
    Der Live-Gang **funktioniert** - aber es gibt etwas, das ein Mensch vorher
    ansehen sollte. Meist Geld (duenne Marge) oder Recht (Markenangabe).

Alles andere ist ``gruen``. Die Ampel eines Listings ist immer der schlimmste
seiner Befunde.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Listing, Product
from app.services import groessen, pricing

ROT = "rot"
GELB = "gelb"
GRUEN = "gruen"

#: eBay kuerzt laengere Titel stillschweigend ab.
MAX_TITEL_LAENGE = 80

_RANG = {GRUEN: 0, GELB: 1, ROT: 2}


@dataclass
class Befund:
    """Ein einzelner Prueffund an einem Listing."""

    schwere: str
    punkt: str
    text: str

    def als_dict(self) -> dict[str, str]:
        return {"schwere": self.schwere, "punkt": self.punkt, "text": self.text}


@dataclass
class Zeugnis:
    """Das Ergebnis fuer EIN Listing."""

    listing_id: int
    titel: str
    ampel: str = GRUEN
    befunde: list[Befund] = field(default_factory=list)
    preis_eur: float | None = None
    kosten_eur: float | None = None
    gewinn_eur: float | None = None
    marge_pct: float | None = None

    def als_dict(self) -> dict[str, Any]:
        return {
            "listing_id": self.listing_id,
            "titel": self.titel,
            "ampel": self.ampel,
            "preis_eur": self.preis_eur,
            "kosten_eur": self.kosten_eur,
            "gewinn_eur": self.gewinn_eur,
            "marge_pct": self.marge_pct,
            "befunde": [b.als_dict() for b in self.befunde],
        }


def _achsenwerte(product: Product | None) -> dict[str, list[str]]:
    """Die Variantenwerte je Achse - so, wie sie zu eBay gingen.

    Bewusst aus den SKUs gelesen und nicht aus ``axes``: die Liste dort kann
    veralten, die SKUs sind die Wahrheit.
    """
    daten = getattr(product, "variants", None) if product is not None else None
    if not isinstance(daten, dict):
        return {}
    achsen: dict[str, list[str]] = {}
    for sku in daten.get("skus") or []:
        for name, wert in (sku.get("options") or {}).items():
            achsen.setdefault(name, []).append(str(wert))
    return achsen


def _zahl(wert: Any) -> float | None:
    """Decimal/None aus der Datenbank in eine Zahl - oder None, nie eine Null.

    Bewusst KEIN ``float(wert or 0)``: eine fehlende Angabe als 0 zu lesen macht
    aus "Einkaufspreis unbekannt" ein "Einkaufspreis null" und damit aus einer
    Luecke eine Traummarge von 100 %.
    """
    if wert is None:
        return None
    try:
        return float(wert)
    except (TypeError, ValueError):
        return None


def pruefe_listing(listing: Listing, product: Product | None, *,
                   settings: Settings | None = None) -> Zeugnis:
    """Ein einzelnes Listing pruefen. Aendert NICHTS und ruft NICHTS auf.

    ``product`` wird uebergeben statt selbst geladen, damit der Sammelbericht
    alle Produkte in einem Rutsch holen kann - sonst waeren es 30 Einzelabfragen
    fuer 30 Entwuerfe.
    """
    s = settings or get_settings()
    titel = (listing.title_seo or "").strip()
    z = Zeugnis(listing_id=listing.id, titel=titel)

    preis = _zahl(listing.price_eur)
    kosten = _zahl(listing.cost_eur)
    z.preis_eur = preis
    z.kosten_eur = kosten

    # --- ROT: der Live-Gang wuerde scheitern -------------------------------
    if not titel:
        z.befunde.append(Befund(ROT, "titel", "Kein Titel - eBay verlangt einen."))

    if not preis or preis <= 0:
        z.befunde.append(Befund(
            ROT, "preis",
            "Kein Verkaufspreis gesetzt - eBay lehnt das Angebot ab."))

    bilder = list(product.images or []) if product is not None else []
    if not bilder:
        z.befunde.append(Befund(
            ROT, "bilder",
            "Keine Bilder am Produkt - der Live-Gang bricht ab "
            "(eBay verlangt mindestens eines)."))

    menge = listing.quantity_available
    if menge is not None and int(menge) <= 0:
        z.befunde.append(Befund(
            ROT, "menge",
            "Sichtbestand ist 0 - eBay stellt nichts mit Menge 0 ein."))

    if listing.ebay_item_id:
        z.befunde.append(Befund(
            ROT, "schon_live",
            f"Steht bereits als Artikel {listing.ebay_item_id} bei eBay - "
            "hier ist nichts mehr zu tun."))

    # --- GELB: geht durch, will aber angesehen werden ----------------------
    if listing.publish_queued:
        z.befunde.append(Befund(
            GELB, "in_arbeit",
            "Steht schon in der Warteschlange - nicht noch einmal einreihen."))

    if kosten is None:
        z.befunde.append(Befund(
            GELB, "kosten",
            "Einkaufspreis unbekannt - ob dieses Angebot Gewinn bringt, "
            "laesst sich nicht sagen."))
    elif preis:
        # Kategorie-genaue Gebuehr, sofern die Kategorie schon bekannt ist;
        # sonst der Pauschalsatz. NICHT einfach VK minus EK - die eBay-Gebuehren
        # sind der groesste Einzelposten und fehlen sonst in jeder Zahl.
        gewinn = pricing.profit_at_price(
            preis, kosten, category_name=listing.category_name, settings=s)
        z.gewinn_eur = gewinn
        if gewinn is not None:
            z.marge_pct = round(gewinn / preis * 100, 1) if preis else None
            if z.marge_pct is not None and z.marge_pct < s.target_margin_pct * 100:
                z.befunde.append(Befund(
                    GELB, "marge",
                    f"Marge {z.marge_pct} % liegt unter der Zielmarge "
                    f"{round(s.target_margin_pct * 100)} %."))
            if gewinn < s.upload_min_profit_eur:
                z.befunde.append(Befund(
                    GELB, "gewinn",
                    f"Nach Gebuehren bleiben {gewinn:.2f} EUR - unter dem "
                    f"Mindestgewinn von {s.upload_min_profit_eur:.2f} EUR."))

    specs = dict(listing.item_specifics or {})
    roh_marke = specs.get("Marke")
    marke = roh_marke.strip() if isinstance(roh_marke, str) else roh_marke
    if not marke:
        z.befunde.append(Befund(
            GELB, "marke",
            "Keine Markenangabe hinterlegt. Beim Anlegen setzt der Live-Gang "
            "'Markenlos' - hier steht es aber nicht, also sieht es niemand nach."))
    elif s.bekleidung_markenlos and marke != "Markenlos":
        from app import brand_filter
        if brand_filter.ist_no_name_bekleidung(titel):
            z.befunde.append(Befund(
                GELB, "marke",
                f"Marke '{marke}' an No-Name-Bekleidung. Der Aufdruck ist keine "
                "Marke - falsche Markenangaben sind auf eBay abmahnbar."))

    if len(titel) > MAX_TITEL_LAENGE:
        z.befunde.append(Befund(
            GELB, "titel",
            f"Titel ist {len(titel)} Zeichen lang - eBay kuerzt bei "
            f"{MAX_TITEL_LAENGE}."))

    if not (listing.description or "").strip():
        z.befunde.append(Befund(
            GELB, "beschreibung", "Keine Beschreibung hinterlegt."))

    # Groessen, die eBay nicht kennt, brechen den Live-Gang mit Fehler 25129 ab -
    # erst nach dem Klick, nach den Gebuehren, mit einer englischen Meldung tief
    # in der Antwort. Am 03.09.2026 hing genau daran der ganze Live-Gang.
    for name, werte in _achsenwerte(product).items():
        if not groessen.ist_groessen_achse(name):
            continue
        offen = groessen.unbekannte(werte)
        if offen:
            z.befunde.append(Befund(
                ROT, "groesse",
                f"eBay kennt diese Groesse nicht: {', '.join(sorted(set(offen)))}. "
                "Das Angebot wird abgelehnt (Fehler 25129)."))

    if not listing.category_id:
        z.befunde.append(Befund(
            GELB, "kategorie",
            "Noch keine Kategorie gespeichert. Sie wird beim Anlegen live bei "
            "eBay erfragt; antwortet der Vorschlagsdienst nicht, bricht der "
            "Live-Gang ab und wird spaeter wiederholt."))

    z.ampel = max((b.schwere for b in z.befunde), key=lambda x: _RANG[x], default=GRUEN)
    return z


def bericht(db: Session, *, nur_entwuerfe: bool = True, limit: int = 500,
            settings: Settings | None = None) -> dict[str, Any]:
    """Sammelbericht ueber alle Entwuerfe. Aendert NICHTS und ruft NICHTS auf.

    Gedacht als der Blick VOR ``publish-all-drafts``: erst hier steht, welche
    Angebote wirklich durchgehen - der Trockenlauf dort zaehlt nur Preise.
    """
    s = settings or get_settings()
    frage = select(Listing).order_by(Listing.id).limit(max(1, int(limit)))
    if nur_entwuerfe:
        frage = frage.where(Listing.listing_status == "draft")
    listings = list(db.scalars(frage).all())

    produkt_ids = {l.product_id for l in listings if l.product_id}
    produkte: dict[int, Product] = {}
    if produkt_ids:
        produkte = {
            p.id: p for p in db.scalars(
                select(Product).where(Product.id.in_(produkt_ids))).all()
        }

    zeugnisse = [
        pruefe_listing(l, produkte.get(l.product_id), settings=s) for l in listings
    ]
    ampeln = {ROT: 0, GELB: 0, GRUEN: 0}
    for z in zeugnisse:
        ampeln[z.ampel] += 1

    haeufig: dict[str, int] = {}
    for z in zeugnisse:
        for b in z.befunde:
            haeufig[b.punkt] = haeufig.get(b.punkt, 0) + 1

    return {
        "geprueft": len(zeugnisse),
        "startklar": ampeln[GRUEN],
        "mit_hinweis": ampeln[GELB],
        "blockiert": ampeln[ROT],
        "haeufigste_punkte": dict(sorted(haeufig.items(), key=lambda kv: -kv[1])),
        "listings": [z.als_dict() for z in zeugnisse],
    }
