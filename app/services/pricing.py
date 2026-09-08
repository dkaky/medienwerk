"""Pricing-/Repricing-Engine (Modell nach eigenem Kalkulator).

Berechnet aus dem Einkaufspreis (EUR) den eBay-Verkaufspreis. Seiteneffektfrei,
ohne DB/Netz -> voll unit-testbar.

Formel (identisch zum vorgegebenen Profit-Kalkulator):
* gewinn = max(kosten * profit_pct, profit_eur, min_profit_eur)
* preis  = (kosten + gewinn + fixkosten) / (1 - fee_pct)
* gebuehren = preis * fee_pct + fixkosten
* gerundeter_preis = auf Cent-Endung aufrunden (z.B. 0.95 -> 149,95)

§ 19 UStG (Kleinunternehmer) -> keine Umsatzsteuer auf den Verkaufspreis.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Optional

from app.config import Settings, get_settings


@dataclass
class PriceBreakdown:
    """Vollstaendige Preisaufschluesselung (alles in EUR) – Felder wie im Kalkulator."""

    cost_eur: float            # Einkaufskosten
    profit_eur: float          # Gewinn (Ziel = Maximum der Komponenten)
    fees_eur: float            # Gebühren (fee_pct * preis + fix)
    price_eur: float           # Verkaufspreis (ungerundet)
    rounded_price_eur: float   # gerundeter Verkaufspreis (Cent-Endung)
    ebay_fee_eur: float        # nur der prozentuale Gebührenanteil
    fixed_fee_eur: float
    margin_pct: float          # gewinn / gerundeter_preis
    markup_pct: float          # gewinn / kosten
    price_cents: float
    clamped: Optional[str] = None   # "min" | "max" | None

    def as_dict(self) -> dict:
        return asdict(self)


def cny_to_eur(price: Decimal | float | None, *, settings: Settings | None = None) -> float:
    """AliExpress-Preis -> EUR ueber den konfigurierten Faktor (Default 1.0, da EUR-Bezug)."""
    s = settings or get_settings()
    return round(float(price or 0) * s.cny_to_eur_rate, 2)


def fee_vat_factor(settings: Settings | None = None) -> float:
    """eBay besteuert die GESAMTE Gebuehr mit MwSt (DE 19 %) -> Faktor (1,19) fuer JEDE
    Gebuehren-Rechnung (variable Provision, Anzeigenrate UND Fixbetrag)."""
    s = settings or get_settings()
    return 1.0 + max(0.0, getattr(s, "ebay_fee_vat_pct", 0.0) or 0.0)


def ebay_fixed_fee(settings: Settings | None = None) -> float:
    """Fester eBay-Betrag pro Bestellung INKL. MwSt (0,45 € netto -> ~0,54 € brutto)."""
    s = settings or get_settings()
    return round(float(s.ebay_fixed_fee_eur or 0) * fee_vat_factor(s), 4)


def _round_to_cents(price: float, cents: float) -> float:
    """Kleinsten Preis mit gewuenschter Cent-Endung (>= price) bestimmen (z.B. ...,95)."""
    whole = math.floor(price)
    rounded = whole + cents
    if rounded < price - 1e-9:
        rounded = whole + 1 + cents
    return round(rounded, 2)


def round_to_nearest_cents(price: float, cents: float = 0.95) -> float:
    """Auf den NAECHSTGELEGENEN Preis mit gewuenschter Cent-Endung runden.

    Nutzerregel (Varianten-Preis-Editor): 20,87 -> 20,95 (auf), 20,11 -> 19,95 (ab).
    Kandidaten sind n+cents fuer aufeinanderfolgende ganze Zahlen; der mit dem
    kleinsten Abstand gewinnt (bei Gleichstand der hoehere = kaufmaennisch nach oben).
    Ergebnis nie negativ/kleiner als der kleinste sinnvolle Wert (cents selbst).
    """
    if price is None:
        return 0.0
    p = float(price)
    whole = math.floor(p)
    candidates = [whole - 1 + cents, whole + cents, whole + 1 + cents]
    candidates = [round(c, 2) for c in candidates if c > 0]
    if not candidates:
        return round(cents, 2)
    best = min(candidates, key=lambda c: (abs(c - p), -c))
    return round(best, 2)


def round_up_to_cents(price: float, cents: float = 0.95) -> float:
    """Kleinster Preis mit gewuenschter Cent-Endung, der >= ``price`` ist (AUFrunden).

    Fuer margenbasierte BOEDEN (``price_floor``): der Preis darf nie UNTER den Boden
    gerundet werden, sonst faellt die Marge unter das Ziel (Marge steigt monoton mit
    VK, also gilt VK >= Boden -> Marge >= Zielmarge). ``round_to_nearest_cents``
    (naechstgelegen) wuerde die Zielmarge-Garantie verletzen -> hier immer auf das
    naechste n+cents >= price. Fuer manuelle Preis-Eingaben weiter round_to_nearest_cents.
    """
    if price is None:
        return 0.0
    p = float(price)
    whole = math.floor(p)
    candidates = [round(whole - 1 + cents, 2), round(whole + cents, 2),
                  round(whole + 1 + cents, 2), round(whole + 2 + cents, 2)]
    ups = [c for c in candidates if c >= p - 1e-9 and c > 0]
    return round(min(ups), 2) if ups else round(cents, 2)


def compute_price(
    cost_eur: float,
    *,
    fee_pct: float | None = None,
    fixed_fee_eur: float | None = None,
    profit_pct: float | None = None,
    profit_eur: float | None = None,
    min_profit_eur: float | None = None,
    price_cents: float | None = None,
    min_price_eur: float | None = None,
    max_price_eur: float | None = None,
    settings: Settings | None = None,
) -> PriceBreakdown:
    """Verkaufspreis + Gewinn-Aufschluesselung nach dem Kalkulator-Modell."""
    s = settings or get_settings()
    fee_pct = effective_fee_pct(None, settings=s) if fee_pct is None else fee_pct   # inkl. MwSt
    fixed = round(ebay_fixed_fee(s) if fixed_fee_eur is None else fixed_fee_eur, 2)  # inkl. MwSt
    profit_pct = s.profit_pct if profit_pct is None else profit_pct
    profit_eur_in = s.profit_eur if profit_eur is None else profit_eur
    min_profit = s.min_profit_eur if min_profit_eur is None else min_profit_eur
    cents = s.price_cents if price_cents is None else price_cents
    min_price_eur = s.min_price_eur if min_price_eur is None else min_price_eur
    max_price_eur = s.max_price_eur if max_price_eur is None else max_price_eur

    cost = max(0.0, round(float(cost_eur), 2))
    profit = max(cost * profit_pct, profit_eur_in, min_profit)
    denom = max(1e-6, 1.0 - fee_pct)
    raw_price = (cost + profit + fixed) / denom

    # MARGE-UNTERGRENZE (29.08.2026): profit_pct ist ein AUFSCHLAG AUF DIE KOSTEN,
    # target_margin_pct dagegen eine MARGE VOM VERKAUFSPREIS. Beide stehen auf 0,20 und
    # meinten trotzdem Verschiedenes - bei teurerer Ware blieb dieses Modell unter der
    # Zielmarge, bei billiger schoss es darueber hinaus.
    #
    # Folge im Dashboard: neben einem korrekt kalkulierten Preis von 18,95 EUR stand
    # dauerhaft ein Vorschlag von 23,95 EUR (33 % statt 20 %). Der Nutzer sah eine
    # Marge, die es nie gab, und haette Preise erhoeht, die laengst stimmten.
    #
    # Jetzt gilt derselbe Boden wie im Upload-Modell. Bewusst INLINE gerechnet statt
    # ueber price_floor: das ermittelt seinen Gebuehrensatz selbst, waehrend hier ein
    # abweichender uebergeben sein kann (Printify rechnet mit eigener Gebuehr). Beide
    # Wege muessen denselben Satz benutzen, sonst widersprechen sich Boden und Preis.
    #
    # Herleitung wie in price_floor: Marge = (VK*(1-fee) - fix - EK)/VK >= m
    #                            ->  VK >= (EK + fix) / (1 - fee - m)
    # Faellt der Boden aus (Gebuehr + Marge >= 100 %), bleibt es beim alten Ergebnis.
    marge_nenner = 1.0 - fee_pct - s.target_margin_pct
    if marge_nenner > 0:
        raw_price = max(raw_price, (cost + fixed) / marge_nenner)
    fees = raw_price * fee_pct + fixed
    rounded = _round_to_cents(raw_price, cents) if cents and cents > 0 else round(raw_price, 2)

    clamped: Optional[str] = None
    if min_price_eur and rounded < min_price_eur:
        rounded, clamped = round(min_price_eur, 2), "min"
    elif max_price_eur and rounded > max_price_eur:
        rounded, clamped = round(max_price_eur, 2), "max"

    # Kennzahlen auf Basis des FINALEN (gerundeten) Preises.
    actual_fee = round(rounded * fee_pct, 2)
    actual_profit_on_rounded = round(rounded - cost - actual_fee - fixed, 2)
    margin = round(actual_profit_on_rounded / rounded, 4) if rounded else 0.0
    markup = round(actual_profit_on_rounded / cost, 4) if cost else 0.0

    return PriceBreakdown(
        cost_eur=cost,
        profit_eur=round(profit, 2),
        fees_eur=round(fees, 2),
        price_eur=round(raw_price, 2),
        rounded_price_eur=rounded,
        ebay_fee_eur=round(raw_price * fee_pct, 2),
        fixed_fee_eur=fixed,
        margin_pct=margin,
        markup_pct=markup,
        price_cents=cents,
        clamped=clamped,
    )


def profit_at_price(price_eur, cost_eur, *, category_name: str | None = None,
                    settings: Settings | None = None) -> Optional[float]:
    """ECHTER Gewinn zum gegebenen Verkaufspreis: VK - eBay-Gebuehren (fee + fix) - EK.

    Die Gebuehr ist kategorie-genau (Provision der ``category_name`` + Anzeigenrate);
    ist die Kategorie unbekannt, greift der pauschale ebay_fee_pct. NICHT einfach VK-EK:
    die eBay-Gebuehren muessen immer abgezogen werden, sonst ist der Gewinn zu hoch.
    """
    if price_eur is None or cost_eur is None:
        return None
    s = settings or get_settings()
    p = float(price_eur)
    fee = effective_fee_pct(category_name, settings=s)   # inkl. Anzeigenrate + MwSt
    return round(p - p * fee - ebay_fixed_fee(s) - float(cost_eur), 2)


# eBay.de-Verkaufsprovision je TOP-LEVEL-Kategorie (gewerblich, NEUWARE, voller Satz <990€).
# Key = category_name VOR dem ersten ':' (Top-Level). VERIFIZIERT gegen die offizielle
# eBay-Gebuehrenseite (PDF "Gebuehren fuer gewerbliche Verkaeufer", id=4809, Juli 2026).
# Nicht gelistete -> _COMMISSION_DEFAULT. Anzeigenanteil kommt separat obendrauf.
# (Gestaffelt: voller Satz bis 990€, 3% darueber; fuer Dropshipping <990€ immer voll.)
_COMMISSION_BY_CATEGORY: dict[str, float] = {
    "Uhren & Schmuck": 0.16,
    "Kleidung & Accessoires": 0.12,
    "Sport": 0.14,
    "Möbel & Wohnen": 0.14,
    "Haustierbedarf": 0.14,
    "Business & Industrie": 0.14,
    "Baby": 0.14,
    "Reisen": 0.14,
    "Garten & Terrasse": 0.13,
    "Heimwerker": 0.13,
    "TV, Video & Audio": 0.07,
    "Haushaltsgeräte": 0.07,
    "Foto & Camcorder": 0.07,
    "Computer, Tablets & Netzwerk": 0.07,
    "Handys & Kommunikation": 0.12,   # Handy-Zubehoer 12% (Dropshipping); Geraete selbst 7%
    "Musikinstrumente": 0.11,
    "Auto & Motorrad": 0.12,          # PDF: Teile #131090 = 12%
    "Beauty & Gesundheit": 0.12,      # PDF-bestaetigt
    "Sammeln & Seltenes": 0.12,       # PDF-Standardsatz
    "Spielzeug": 0.12,                # PDF-Standardsatz
}
_COMMISSION_DEFAULT = 0.12


def commission_pct(category_name: str | None) -> float:
    """eBay-Verkaufsprovision (%) fuer die Top-Level-Kategorie eines Listings."""
    if not category_name:
        return _COMMISSION_DEFAULT
    top = str(category_name).split(":")[0].strip()
    return _COMMISSION_BY_CATEGORY.get(top, _COMMISSION_DEFAULT)


def effective_fee_pct(category_name: str | None, *, settings: Settings | None = None,
                      ad_rate_pct: float | None = None) -> float:
    """EINHEITLICHE Vorwaerts-Gebuehr = Kategorie-Provision + Anzeigenrate. Ersetzt den
    pauschalen ebay_fee_pct in allen Margen-/Preis-Rechnungen, sobald die Kategorie
    bekannt ist. Ist die Kategorie unbekannt (None), greift der Default + Anzeigenrate.
    """
    s = settings or get_settings()
    ad = s.ebay_ad_rate_pct if ad_rate_pct is None else ad_rate_pct
    # + MwSt: eBay besteuert Provision UND Anzeigengebuehr (Nachweis 14.07.).
    return round((commission_pct(category_name) + max(0.0, ad)) * fee_vat_factor(s), 4)


def effective_fee_pct_for_listing(listing, *, settings: Settings | None = None) -> float:
    """Wie effective_fee_pct, aber mit der ECHTEN Anzeigenrate DIESES Listings
    (``listing.ad_rate_pct``, aus der eBay Marketing API), falls bekannt – sonst die
    Pauschale. Kategorie aus ``listing.category_name``. So rechnet die Gebuehr pro Produkt
    mit dem tatsaechlichen Promoted-Listings-Satz statt pauschal 10 %."""
    cat = getattr(listing, "category_name", None) if listing is not None else None
    ad = getattr(listing, "ad_rate_pct", None) if listing is not None else None
    return effective_fee_pct(cat, settings=settings, ad_rate_pct=ad)


def price_floor(cost_eur, *, min_margin_pct: float | None = None,
                category_name: str | None = None, ad_rate_pct: float | None = None,
                settings: Settings | None = None) -> Optional[float]:
    """Tiefster Verkaufspreis, bei dem netto noch >= ``min_margin_pct`` Marge bleibt
    (nach eBay-Gebuehren + Fixgebuehr). Untergrenze fuer Preissenkungen – NIE Verlust.

    Herleitung: margin = (VK*(1-fee) - fix - EK)/VK >= m  ->  VK >= (EK+fix)/(1-fee-m).
    Bewusst NICHT der 8-€-Mindestgewinn aus compute_price (der ist die Aufwaerts-Kalkulation);
    hier zaehlt allein die Marge-Untergrenze, damit Preise zum Markt hin sinken duerfen.

    ``ad_rate_pct``: die ECHTE Anzeigenrate DIESES Listings (listing.ad_rate_pct). MUSS
    uebergeben werden, wenn der Boden zu einem konkreten Listing gehoert – sonst rechnet
    der Boden mit der Pauschale, waehrend die angezeigte Marge die echte Rate nutzt, und
    Preissenkungen fielen unter die Zielmarge (Vorfall Revert 14.07.).
    """
    if cost_eur is None:
        return None
    s = settings or get_settings()
    m = s.lowering_min_margin_pct if min_margin_pct is None else min_margin_pct
    cost = max(0.0, float(cost_eur))
    fee = effective_fee_pct(category_name, settings=s, ad_rate_pct=ad_rate_pct)  # inkl. Anzeige + MwSt
    denom = 1.0 - fee - m
    if denom <= 0:
        return None
    return round((cost + ebay_fixed_fee(s)) / denom, 2)


def volume_pricing_plan(vk_1, ek_eff, *, fee_pct: float, ek_extra: float | None = None,
                        settings: Settings | None = None) -> dict:
    """Multi-Buy-Rabatt-Staffel fuer EINE Variante (read-only, aendert nichts).

    Dropshipping-Kern: jede Zusatz-Einheit = eigene AliExpress-Bestellung mit vollem EK;
    KEIN Skaleneffekt ausser (a) der einmaligen 0,45-€-Fixgebuehr (faellt nur bei der 1.
    Einheit der eBay-Bestellung an) und (b) dem Versand-Trick: werden mehrere Einheiten in
    EINER AliExpress-Order bestellt und ueberschreiten zusammen die 10-€-Schwelle, faellt
    der 1,99-€-Versandaufschlag fuer die Zusatz-Einheiten weg -> ``ek_extra`` (marginale EK
    der 2. Einheit) < ``ek_eff``. ECHTER Zusatzgewinn je Stueck = Gewinn(N) - Gewinn(N-1), wobei bei
    N Stueck ALLE zum Staffelpreis gerechnet werden (eBay rabattiert die ganze Bestellung, nachgewiesen
    an echter Auszahlung 16.07.) — nicht das Zusatzstueck isoliert (das war zu optimistisch).

    Empfohlen nur, wenn die Ist-Marge >= ``multibuy_min_margin_pct`` UND mindestens eine
    Staffel je Zusatz-Einheit noch >= ``multibuy_min_extra_profit_eur`` Gewinn und
    >= ``lowering_min_margin_pct`` Marge haelt. So werden „1-€-Faelle" automatisch
    ausgeschlossen (Nutzerregel: nur wo der Mehrverkauf sich echt lohnt).
    """
    s = settings or get_settings()
    if not vk_1 or not ek_eff or float(vk_1) <= 0:
        return {"recommended": False, "reason": "keine Preis-/EK-Daten", "tiers": [], "all_tiers": []}
    vk_1 = float(vk_1); ek_eff = float(ek_eff)
    ek_x = float(ek_extra) if ek_extra is not None else ek_eff   # marginale EK der Zusatz-Einheit
    fix = ebay_fixed_fee(s)   # inkl. MwSt
    margin_1 = (vk_1 * (1 - fee_pct) - fix - ek_eff) / vk_1
    profit_1 = round(vk_1 * (1 - fee_pct) - fix - ek_eff, 2)   # Gewinn beim Verkauf 1 Stueck (voll)
    cents = s.price_cents or 0.95
    cap = s.multibuy_max_discount_pct
    # Staffel: 2. Stueck -5 %, ab 3. Stueck bis zum Deckel (max 10 %). ECHTER marginaler Zusatzgewinn:
    # eBay rabattiert ALLE Stueck der Bestellung zum jeweiligen Staffelpreis. Der Mehrgewinn des
    # N-ten Stuecks = Gewinn(N, alle zum Staffelpreis) - Gewinn(N-1) — der tiefere Rabatt frisst auch
    # die frueheren Stuecke mit. Fixgebuehr faellt nur EINMAL je Bestellung an, ab dem 2. Stueck EK =
    # marginale (Bundle-)EK.
    all_tiers = []
    prev_profit = profit_1                     # Gewinn bei (N-1) Stueck; Start = 1 Stueck voll
    for qty, d in ((2, min(0.05, cap)), (3, cap)):
        vk_rab = round_to_nearest_cents(vk_1 * (1 - d), cents)
        profit_n = round(qty * vk_rab * (1 - fee_pct) - fix - (ek_eff + (qty - 1) * ek_x), 2)
        db_extra = round(profit_n - prev_profit, 2)          # echter Zusatzgewinn DIESES Stuecks
        # NUR informativ (Einzel-Stueck-Marge zum Staffelpreis); das ENTSCHEIDENDE Tor ist db_extra.
        margin_rab = (vk_rab * (1 - fee_pct) - ek_x) / vk_rab if vk_rab else 0.0
        ok = (db_extra >= s.multibuy_min_extra_profit_eur
              and margin_rab >= s.lowering_min_margin_pct)
        all_tiers.append({"qty": qty, "discount_pct": round(d * 100, 1), "price_eur": vk_rab,
                          "db_extra_eur": db_extra, "margin_pct": round(margin_rab, 4), "ok": ok})
        prev_profit = profit_n
    recommended = (margin_1 >= s.multibuy_min_margin_pct and any(t["ok"] for t in all_tiers))
    return {
        "recommended": recommended,
        "margin_1": round(margin_1, 4),
        "profit_1_eur": profit_1,
        # Angebotene Staffeln NUR bei Empfehlung (sonst leer -> nichts anbieten).
        "tiers": ([t for t in all_tiers if t["ok"]] if recommended else []),
        "all_tiers": all_tiers,
        "reason": ("Hohe Marge – Mengenrabatt lohnt sich" if recommended
                   else f"Marge zu dünn (Ist {round(margin_1*100,1)}%) – jeder Extra-Verkauf "
                        f"brächte zu wenig; kein Multirabatt"),
    }


def effective_cost(price: Decimal | float | None, *, settings: Settings | None = None,
                   ship_override: float | None = None, local: bool = False) -> float:
    """AliExpress-EK = Artikelpreis + Versand + PAUSCHALE Einfuhrgebuehr + eigener Versand.

    Modell (korrigiert 14.07.2026): AliExpress belastet je DS-Order eine PAUSCHALE geschaetzte
    Einfuhrgebuehr/Zoll (customs_fee_eur ~3,57 EUR), KEINEN prozentualen Aufschlag. Deshalb ist
    der EK preis-unabhaengig nur um diesen festen Betrag hoeher als Preis+Versand - nicht um
    einen Prozentsatz, der bei teuren Artikeln explodierte. ``aliexpress_tax_pct`` ist auf 0
    (bleibt als optionaler prozentualer Aufschlag env-tunbar, aktuell inaktiv).

    ``ship_override``: die ECHTEN AliExpress-Versandkosten dieses Produkts (aus der freight
    query). Wenn gesetzt, ersetzt der Betrag die pauschale Schwellwert-Schaetzung (1,99 €)
    komplett – manche Artikel kosten mehr (z.B. 3,29 €), manche liefern gratis (0 €).

    ``local`` (Nutzerregel 08.08.2026): EU-Lager-Produkte ("Versand aus" DE/PL/FR/...)
    zahlen KEINEN Pauschalzoll und der Versand ist in der Regel kostenlos - der
    angezeigte Preis IST der Endpreis. Es entfallen die Versand-SCHAETZUNG und die
    Einfuhrgebuehr; ECHTE Frachtkosten (ship_override, "ausser da steht extra Versand")
    und der eigene Versandaufschlag bleiben.
    """
    s = settings or get_settings()
    ae = cny_to_eur(price, settings=s)
    if ae <= 0:
        return 0.0
    if ship_override is not None:
        ae += max(0.0, float(ship_override))          # echte Versandkosten (freight query)
    elif not local:
        thr = getattr(s, "aliexpress_free_shipping_threshold", 0.0) or 0.0
        ship = getattr(s, "aliexpress_shipping_fee_eur", 0.0) or 0.0
        if thr > 0 and ae < thr:
            ae += ship
    # Optionaler prozentualer Aufschlag (aktuell 0) + PAUSCHALE Einfuhrgebuehr je Order.
    ae *= 1.0 + max(0.0, getattr(s, "aliexpress_tax_pct", 0.0) or 0.0)
    if not local:
        ae += getattr(s, "customs_fee_eur", 0.0) or 0.0   # flache geschaetzte Einfuhrgebuehr (~3,57 €)
    return round(ae + s.shipping_cost_eur, 2)


def effective_cost_bundle(price, *, quantity: int = 1, settings: Settings | None = None,
                          ship_override: float | None = None, local: bool = False) -> float:
    """Vollkosten fuer ``quantity`` Einheiten DESSELBEN Artikels in EINER AliExpress-Order
    (eine Sendung). Der Versandaufschlag gilt fuer den GESAMTBETRAG einmal (nicht je
    Einheit). ``ship_override`` (echte Versandkosten) faellt ebenfalls nur EINMAL je Sendung
    an; ohne Override kreuzt das Bundle die 10-€-Schwelle -> Pauschale entfaellt ganz. Genau
    das macht Multi-Buy im Dropshipping wirtschaftlich. (Zoll = je Sendung einmal.)
    """
    s = settings or get_settings()
    ae = cny_to_eur(price, settings=s)
    if ae <= 0:
        return 0.0
    q = max(1, int(quantity or 1))
    total = ae * q
    if ship_override is not None:
        total += max(0.0, float(ship_override))     # echte Versandkosten, einmal je Sendung
    elif not local:
        thr = getattr(s, "aliexpress_free_shipping_threshold", 0.0) or 0.0
        ship = getattr(s, "aliexpress_shipping_fee_eur", 0.0) or 0.0
        if thr > 0 and total < thr:                 # ganze Bestellung unter Schwelle -> Versand EINMAL
            total += ship
    # Optionaler prozentualer Aufschlag (aktuell 0) + PAUSCHALE Einfuhrgebuehr EINMAL je Sendung
    # (nicht je Einheit — mehrere Einheiten in einer Order = ein Zoll; LOKAL: keiner).
    total *= 1.0 + max(0.0, getattr(s, "aliexpress_tax_pct", 0.0) or 0.0)
    if not local:
        total += getattr(s, "customs_fee_eur", 0.0) or 0.0   # flache Einfuhrgebuehr je Sendung (~3,57 €)
    total += s.shipping_cost_eur * q
    return round(total, 2)


def marginal_unit_cost(price, *, at_qty: int, settings: Settings | None = None,
                       ship_override: float | None = None, local: bool = False) -> float:
    """EK der ``at_qty``-ten Einheit im Bundle = Bundle(at_qty) - Bundle(at_qty-1). Fuer die
    1. Einheit = effective_cost (inkl. evtl. Versand); ab der 2. faellt der Versandanteil weg,
    sobald das Bundle die Schwelle ueberschreitet (dann marginale EK = reiner Artikelpreis)."""
    if at_qty <= 1:
        return effective_cost(price, settings=settings, ship_override=ship_override, local=local)
    return round(effective_cost_bundle(price, quantity=at_qty, settings=settings,
                                       ship_override=ship_override, local=local)
                 - effective_cost_bundle(price, quantity=at_qty - 1, settings=settings,
                                         ship_override=ship_override, local=local), 2)


def price_from_cny(
    price: Decimal | float | None,
    *,
    settings: Settings | None = None,
    ship_override: float | None = None,
    local: bool = False,
    **overrides,
) -> PriceBreakdown:
    """Bequemer Pfad: AliExpress-Preis (EUR-Bezug) -> Vollkosten -> Verkaufspreis."""
    s = settings or get_settings()
    return compute_price(effective_cost(price, settings=s, ship_override=ship_override, local=local),
                         settings=s, **overrides)


def profit_floor(cost_eur, *, min_profit_eur: float, category_name: str | None = None,
                 ad_rate_pct: float | None = None,
                 settings: Settings | None = None) -> Optional[float]:
    """Tiefster Verkaufspreis, bei dem netto noch >= ``min_profit_eur`` EFFEKTIVER Gewinn
    bleibt (nach eBay-Gebuehren + Fixgebuehr). Gegenstueck zu ``price_floor`` (Marge-Boden),
    aber als absoluter EURO-Boden.

    Herleitung: gewinn = VK*(1-fee) - fix - EK >= P  ->  VK >= (EK + fix + P)/(1-fee).

    ``ad_rate_pct``: echte Anzeigenrate des Listings (wie bei ``price_floor`` – bei
    Listing-bezogenen Boeden zwingend, sonst rechnet der Boden zu optimistisch).
    """
    if cost_eur is None:
        return None
    s = settings or get_settings()
    cost = max(0.0, float(cost_eur))
    fee = effective_fee_pct(category_name, settings=s, ad_rate_pct=ad_rate_pct)  # inkl. Anzeige + MwSt
    denom = 1.0 - fee
    if denom <= 0:
        return None
    return round((cost + ebay_fixed_fee(s) + max(0.0, float(min_profit_eur))) / denom, 2)


def upload_breakdown_from_cny(
    price: Decimal | float | None,
    *,
    settings: Settings | None = None,
    category_name: str | None = None,
    ship_override: float | None = None,
    local: bool = False,
) -> PriceBreakdown:
    """Empfohlener UPLOAD-Preis: der HOEHERE aus (a) ZIELMARGE ``upload_margin_pct`` (z.B.
    25 %) und (b) EFFEKTIV-Mindestgewinn ``upload_min_profit_eur`` (z.B. 4 €) statt der
    starren 8-€-Grenze. So werden guenstige Artikel nicht kuenstlich teuer (Marge reicht),
    aber sehr billige, bei denen 25 % nur wenige Cent brächten, tragen trotzdem einen
    sinnvollen Mindestgewinn (Nutzerregel 09.07.: "25 %, aber mindestens 4 € effektiv").
    Faellt der Marge-Boden aus (unmoegliche Marge), Fallback auf das alte Modell.

    Metriken (Gewinn/Marge) sind EXAKT am zurueckgegebenen Preis berechnet – die Zeile
    kann direkt wie ein ``price_from_cny``-Breakdown verwendet werden.
    """
    s = settings or get_settings()
    eff = round(effective_cost(price, settings=s, ship_override=ship_override, local=local), 2)
    fl = price_floor(eff, min_margin_pct=s.upload_margin_pct, category_name=category_name,
                     settings=s)
    if fl is None:
        return compute_price(eff, settings=s)   # Fallback altes Modell
    # 4-€-Mindestgewinn-Boden: bei sehr guenstigen Artikeln haelt er den Preis oben, wenn
    # 25 % Marge nur ein paar Cent Gewinn brächten. Der hoehere der beiden Boeden gewinnt.
    pf = profit_floor(eff, min_profit_eur=s.upload_min_profit_eur, category_name=category_name,
                      settings=s)
    if pf is not None:
        fl = max(fl, pf)
    # DRITTER Boden: harter Mindestverkaufspreis (19,95 EUR). Er rechnet nicht,
    # er entscheidet - "so guenstig geben wir das Shirt nicht ab". Von den drei
    # Boeden gewinnt der hoechste. Nutzerregel vom 03.09.2026.
    #
    # Dasselbe Feld, das ``compute_price`` unten auswertet. Ein eigenes Feld nur
    # fuer diesen Weg haette die beiden Preismodelle wieder auseinanderlaufen
    # lassen (Vorfall 29.08.2026: 18,95 kalkuliert, 23,95 vorgeschlagen).
    mindest = float(s.min_price_eur or 0)
    if mindest > 0:
        fl = max(fl, mindest)
    cents = s.price_cents or 0.95
    vk = round_up_to_cents(fl, cents)           # AUF Cent-Endung AUFrunden (nie unter Marge)
    fee = effective_fee_pct(category_name, settings=s)   # inkl. Anzeigenrate + MwSt
    fix = round(ebay_fixed_fee(s), 2)
    fee_eur = round(vk * fee, 2)
    profit = round(vk - fee_eur - fix - eff, 2)
    margin = round(profit / vk, 4) if vk else 0.0
    markup = round(profit / eff, 4) if eff else 0.0
    return PriceBreakdown(
        cost_eur=eff, profit_eur=profit, fees_eur=round(vk * fee + fix, 2),
        price_eur=vk, rounded_price_eur=vk, ebay_fee_eur=fee_eur, fixed_fee_eur=fix,
        margin_pct=margin, markup_pct=markup, price_cents=cents, clamped=None)
