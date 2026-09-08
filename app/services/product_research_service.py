"""Produkt-Research: AliExpress nach neuen, margenstarken Produkten durchsuchen.

Zweistufig (durch API-Limits bedingt):
1. `ds.text.search` je Nische -> Kandidaten (Rating-Sterne, Verkäufe, Preis).
2. `ds.product.get` je Kandidat -> harte Filter: Rating > 4.0, Lieferzeit ≤ 10 Tage,
   onSelling, Store. Dann Pricing (VK/Gewinn/Marge) und Speicherung als ProductIdea.

Choice hat in der DS-API kein echtes Flag -> Näherung über `sl_product` + Rating +
Verkaufsvolumen. Rate-Limit: ≥1s zwischen Calls.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations import aliexpress_api as api
from app.models import ProductIdea
from app.services import pricing

logger = logging.getLogger("app.services.research")

# Standard-Nischen – bewusst SEHR divers, viele Kategorien, auch Neues abseits des Shops.
DEFAULT_NICHES = [
    # Auto & Zweirad
    "auto detailing zubehör", "auto reinigung mikrofaser", "auto organizer kofferraum",
    "motorrad zubehör", "fahrrad zubehör", "fahrrad licht usb",
    # Schmuck & Identität
    "edelstahl halskette herren", "edelstahl armband", "gravur anhänger edelstahl",
    # Haushalt & Küche
    "küchenhelfer edelstahl", "küchen organizer", "gadget haushalt praktisch",
    "grill zubehör edelstahl", "kaffee zubehör", "vakuum vorratsdosen",
    # Ordnung, Reise & Alltag (bewusst NICHT-elektronisch statt Tech-/Handy-Fokus)
    "schreibtisch organizer", "reise packwürfel set", "schmuck organizer box",
    "kosmetik organizer", "kleiderbügel platzsparend", "aufbewahrung vakuumbeutel",
    # Haustier
    "haustier zubehör", "hunde spielzeug robust", "katzen zubehör",
    # Garten & Outdoor
    "garten bewässerung", "camping gadget", "angeln zubehör", "solarleuchte garten",
    "wandern ausrüstung",
    # Fitness & Beauty
    "fitness widerstandsband", "massage tool", "beauty gadget", "haar styling tool",
    # Werkzeug & DIY
    "werkzeug set", "präzisions schraubendreher set", "messwerkzeug digital",
    # Baby/Kids & Sonstiges
    "baby pflege gadget", "kinder lernspielzeug", "reise organizer", "büro gadget",
    "led deko lampe", "sicherheit haushalt",
    # Spielzeug, Spielsachen & Figuren
    "actionfigur sammelfigur", "anime figur", "modellauto sammler", "bausteine set",
    "rc auto spielzeug", "brettspiel zubehör", "3d puzzle", "fidget spielzeug",
    "plüschtier", "spielzeug jungen", "spielzeug mädchen", "sammelkarten zubehör",
]

_RATE_S = 1.2

# Ergebnis des LETZTEN Suchlaufs (fuers Dashboard: erklaert, warum ggf. 0 Ideen
# uebernommen wurden — kept/scanned/drops/Begriffe/Zeitstempel).
_last_result: dict = {}


def last_result() -> dict:
    """Zusammenfassung des letzten Suchlaufs (leer, wenn noch keiner lief)."""
    return dict(_last_result)

# Erkennbare Elektronik / batteriebetriebene / Strom-Geraete. Seit 19.08. STRIKT
# (Nutzer: "Bitte strikt keine elektroartikel mehr importieren" — Kaffeemaschinen,
# Fritteusen, Sensoren, OBD2-Tester, RC-Autos rutschten durch): lieber eine gute
# Idee zu viel aussortieren als weiter Elektro importieren. Grund: hoehere Retour-
# quote + deutsche Compliance-Pflichten (ElektroG/WEEE, Batteriegesetz BattG).
# Ausnahme bleibt reines unelektrisches Zubehoer ohne Elektro-Begriff im Titel.
_ELECTRONICS_TERMS = (
    "powerbank", "power bank", "akku", "batterie", "battery", "ladegerät", "ladekabel",
    "ladestation", "netzteil", "charger", "wireless charger",
    "kopfhörer", "ohrhörer", "earbuds", "earphone", "headphone", "headset", "in-ear",
    "bluetooth", "lautsprecher", "speaker", "soundbar", "mikrofon",
    "beamer", "projektor", "projector", "webcam", "dashcam", "action cam", "gopro",
    "überwachungskamera", "ip kamera", "smartwatch", "fitnessuhr", "fitness tracker",
    "tablet", "laptop", "notebook", "monitor", "tastatur", "keyboard",
    "drohne", "drone", "smart home", "wlan", "wifi", "wi-fi", "router", "repeater",
    "festplatte", " ssd", "usb-stick", "usb stick", "usb hub", "sd karte", "speicherkarte",
    "voltmeter", "multimeter", "arduino", "raspberry", "solarpanel", "solarmodul",
    "wechselrichter", "led strip", "led streifen", "led controller", "rgb controller",
    "e-bike", "e-scooter", "elektroroller", "autoradio",
    "gps tracker", "funkgerät", "walkie talkie",
    "föhn", "haartrockner", "lockenstab", "glätteisen", "elektrisch",
    "rasierer", "epilierer", "nagelfräser", "taschenlampe", "usb ventilator",
    # Nachtrag 16.08. (Store-Funde Super PDR/Jianyana rutschten durch): Heisskleber-
    # und Arbeits-Elektrik in Werkzeug-Sets. "led lampe"/"led-lampe" trifft NICHT
    # die erlaubte Nische "led deko lampe" (dort steht "deko" dazwischen).
    "klebepistole", "glue gun", "led-lampe", "led lampe", "led lamp",
    "drehmaschine",
    # Nachtrag 19.08. — die realen Durchrutscher der letzten Tage:
    # Elektro-Haushaltsgeraete (HiBREW-Kaffeemaschinen, Fritteuse, Dampfreiniger)
    "kaffeemaschine", "espressomaschine", "cafetera", "kaffeevollautomat",
    "kapselmaschine", "siebträgermaschine", "kaffee maschine",
    # (KEIN "mikrowelle"/"kühlschrank"/"induktion"/"klimaanlage": die Geraete
    # selbst werden nicht dropshippt — die Woerter treffen nur Zubehoer wie
    # Abdeckhauben, Gewuerzregale "fuer Kuehlschrank", Pfannen "fuer Induktion".)
    "fritteuse", "airfryer", "air fryer", "wasserkocher", "toaster",
    "reiskocher", "eismaschine", "waffeleisen", "sandwichmaker", "bügeleisen",
    "dampfreiniger", "dampfglätter", "dampfbügel", "staubsauger", "saugroboter",
    "stabmixer", "standmixer", "küchenmaschine",
    "heizlüfter", "heizdecke", "heizkissen", "luftbefeuchter", "luftentfeuchter",
    "luftreiniger", "klimagerät", "ventilator",
    # Sensorik / Smart-Geraete / Messtechnik (SONOFF Zigbee, Anker Smart Meter)
    "sensor", "zigbee", "alexa", "smart meter", "thermostat", "hygrometer",
    "lcd", "oled", "touchscreen", "solarbank", "balkonkraftwerk", "solarleuchte",
    # Kfz-Elektronik (VCDS/OBD2, Podofo Diagnosetester)
    "obd", "diagnosegerät", "diagnosetester", "diagnose gerät",
    # Funk / ferngesteuert (WLtoys RC-Auto, Neon-Lichterkette mit App)
    "rc-auto", "rc auto", "ferngesteuert", "fernbedienung", "fernsteuerung",
    "lichterkette", "neonlicht", "nachtlampe",
    # Elektro-Werkzeug (Punktschweissgeraet, drahtloser Polierer)
    "schweißgerät", "punktschweiß", "lötkolben", "lötstation", "heißluftpistole",
    "poliermaschine", "akkuschrauber", "bohrmaschine", "schleifmaschine",
    "drucker", "kabellos", "drahtlos", "cordless", "wiederaufladbar", "rechargeable",
    # Sonstiges eindeutig Elektrisches
    "massagegerät", "massagepistole", "massage gun", "e-zigarette", "vape",
    "elektronik", "elektronisch", "electronic", "elektro-", " elektro ",
    "li-ion", "lithium", "lipo ", "diffusor", "diffuser", "steckdose",
    "barttrimmer", "haartrimmer", "haarschneider",
    # Kaputte Titel-Formatierung ("Espresso maschine") + reine Elektro-Marken.
    # KEIN "anker" (Anker-Armband ist eine Schmuck-Nische!).
    "espresso maschine", "hibrew", "sonoff", "wltoys", "anycubic", "seesii",
    # Batteriebetrieben, faellt unter BattG (Uhren-ARMBAENDER bleiben erlaubt).
    "quarzuhr", "armbanduhr", "mit beleuchtung",
)

# Generische Elektro-Signale im Titel: Leistungs-/Spannungs-/Kapazitaets-/Funk-
# Angaben stehen praktisch nur an Strom-/Akku-Geraeten ("1800W", "12V", "5000mAh",
# "2,4GHz"). Faengt Elektro, dessen Geraetename in keiner Begriffsliste steht.
_ELECTRO_PATTERNS = (
    re.compile(r"\b\d{2,4}\s?w(att)?\b"),        # Leistung: "1800W", "20 Watt"
    re.compile(r"\b\d{1,3}\s?v\b(?![-\w])"),     # Spannung: "12V", "5 V" (nicht "V-Ausschnitt")
    re.compile(r"\d\s?mah\b"),                   # Akku-Kapazitaet: "5000mAh"
    re.compile(r"\b\d[.,]?\d?\s?ghz\b"),         # Funk: "2,4GHz"
)

# Zubehoer-Ausnahme (Nintendo-Fall 19.08.: "Huelle fuer Switch OLED" ist KEIN
# Elektroartikel): nennt ein Zubehoer-Titel das Geraet nur als BEZUG, bleibt er
# erlaubt. Gilt AUSSCHLIESSLICH, wenn saemtliche Elektro-Treffer aus der
# Geraete-REFERENZ-Liste stammen — harte Woerter (akku, ladegeraet, bluetooth,
# Watt/mAh-Angaben ...) machen den Artikel weiterhin IMMER zum Elektroartikel.
_ZUBEHOER_WOERTER = (
    "hülle", "huelle", "case", "cover", "etui", "tasche", "skin", "aufkleber",
    "folie", "panzerglas", "schutzglas", "halterung", "halter", "ständer",
    "stand ", "organizer", "beutel", "sleeve", "armband",
)
_GERAETE_REFERENZEN = frozenset((
    "oled", "lcd", "tablet", "laptop", "notebook", "monitor", "smartwatch",
    "fitnessuhr", "kopfhörer", "earbuds", "earphone", "headphone", "headset",
    "drohne", "drone", "gopro", "action cam", "dashcam", "e-bike", "e-scooter",
    "ladestation", "drucker", "staubsauger", "kaffeemaschine", "airfryer",
    "fritteuse", "konsole", "fernbedienung",
))


def _is_electronic(title: str | None) -> bool:
    """Elektronik-Erkennung am Produkttitel — seit 19.08. STRIKT (Begriffe + Muster),
    mit Zubehoer-Ausnahme fuer reine Geraete-NENNUNGEN ("Huelle fuer ... OLED").
    True = elektronisch/batteriebetrieben -> aus Discover/Trend/Store aussortieren,
    wenn ``avoid_electronics``."""
    t = f" {(title or '').lower()} "
    treffer = [term for term in _ELECTRONICS_TERMS if term in t]
    muster = any(p.search(t) for p in _ELECTRO_PATTERNS)
    if not treffer and not muster:
        return False
    if muster:
        return True
    if (all(term.strip() in _GERAETE_REFERENZEN for term in treffer)
            and any(z in t for z in _ZUBEHOER_WOERTER)):
        return False                 # Zubehoer, das sein Geraet nur benennt
    return True


def _real_ae():
    from app.integrations.aliexpress import RealAliExpressClient
    return RealAliExpressClient(get_settings())


def _f(v):
    try:
        return float(str(v).replace(",", ".")) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _i(v):
    try:
        return int(float(str(v).replace(",", "").replace("+", ""))) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _orders_to_int(v):
    """'50,000+' / '1394' -> int."""
    return _i(v)


def _resp_root(data: dict) -> dict:
    """Äußeren *_response-Wrapper einer TOP/IOP-Antwort abschälen."""
    if isinstance(data, dict) and len(data) == 1:
        inner = next(iter(data.values()))
        if isinstance(inner, dict):
            return inner
    return data or {}


def _norm_url(u: str | None, pid: str = "") -> str:
    # Kanonische, saubere URL aus der Produkt-ID (ohne Query-Müll, korrekte Domain).
    if pid:
        return f"https://de.aliexpress.com/item/{pid}.html"
    if u:
        u = u.strip()
        if u.startswith("//"):
            u = "https:" + u
        return u
    return ""


def _local_search_url(keyword: str) -> str:
    """AliExpress-WEB-Suche mit "Versand aus Deutschland"-Filter (Lokal-Fokus 10.08.).

    Die DS-API-Textsuche kann NICHT nach Lager filtern — generische Treffer sind fast
    immer China-Versand, der EU-Filter nach dem teuren Enrich liess die Trend-Ausbeute
    auf 0 fallen. Die Web-Suche kann filtern (shipFromCountry=DE) und wird wie der
    Seiten-Import headless geerntet."""
    from urllib.parse import quote
    slug = quote(str(keyword or "").strip().replace(" ", "-"))
    return f"https://de.aliexpress.com/w/wholesale-{slug}.html?shipFromCountry=DE"


async def _text_search(ae, keyword: str, *, page_size: int = 20, page: int = 1,
                       sort: str | None = None) -> list[dict]:
    # RELEVANZ statt Bestseller-Sortierung (Default): 'sortBy=orders,desc' spuelte bei
    # spezifischen Suchen (z.B. "kinesio tapes") hochvolumige, thematisch FALSCHE Treffer
    # nach oben (Elektro-/Industrie-Klebebaender) und draengte die echten raus. VPS-Probe
    # 12.07.: orders,desc = 8/20 relevant, ohne sortBy = 20/20 relevant. Ohne sortBy liefert
    # die API die "Beste Ergebnisse"-Reihenfolge wie die AliExpress-Website. Bestseller-
    # Sortierung ist per ``sort`` weiterhin moeglich (das orders-Signal bleibt je Treffer).
    params = {
        "keyWord": keyword, "local": "de_DE", "countryCode": "DE",
        "currency": "EUR", "pageSize": str(page_size), "pageIndex": str(page),
    }
    if sort:
        params["sortBy"] = sort
    data = await ae._call("aliexpress.ds.text.search", params)
    root = _resp_root(data)
    prods = (((root.get("data") or {}).get("products") or {}).get("selection_search_product")) or []
    if isinstance(prods, dict):
        prods = [prods]
    out = []
    for p in prods:
        pid = str(p.get("itemId") or p.get("item_id") or "")
        if not pid:
            continue
        out.append({
            "id": pid,
            "title": p.get("title"),
            "image": p.get("itemMainPic"),
            "score": _f(p.get("score")),
            "orders": _orders_to_int(p.get("orders")),
            "price": _f(p.get("targetSalePrice")),
            "url": _norm_url(p.get("itemUrl"), pid),
        })
    return out


async def _enrich(ae, product_id: str) -> dict:
    data = await ae._call("aliexpress.ds.product.get", {
        "product_id": product_id, "ship_to_country": "DE",
        "target_currency": "EUR", "target_language": "de",
    })
    result = api._unwrap_result(data)
    base = api._deep_get(result, "ae_item_base_info_dto") or {}
    logi = (api._deep_get(result, "logistics_info_dto")
            or api._deep_get(result, "ae_item_logistics_info_dto") or {})
    store = api._deep_get(result, "ae_store_info") or {}
    parsed = api.parse_product(data)
    return {
        "rating": _f(base.get("avg_evaluation_rating")),
        "reviews": _i(base.get("evaluation_count")),
        "status": base.get("product_status_type"),
        "sl_product": bool(base.get("sl_product")),
        "delivery_days": _i(logi.get("delivery_time")),
        # "Versand aus"-Werte (SKU-Achse 200007763) — Grundlage des Lokal-Lager-Filters.
        # Kommt aus derselben Antwort, kostet also KEINEN zusaetzlichen API-Call.
        "ships_from": api.extract_ships_from(result),
        "store_name": store.get("store_name"),
        # Store-ID (gleiche Antwort, kein Extra-Call) — Grundlage der Store-Pipeline
        # (Nutzerauftrag 14.08.: Stores lokaler Produkte raussuchen und scrapen).
        "store_id": str(store.get("store_id") or "") or None,
        "price_cny": parsed.get("price_cny"),
        "images": parsed.get("images") or [],
        "title": parsed.get("title_raw"),
        "category_id": str(base.get("category_id") or ""),
    }



def _is_aliexpress_page_url(value: str) -> bool:
    """Ist der "Suchbegriff" in Wahrheit eine AliExpress-Seiten-URL (Seiten-Import)?

    Nur aliexpress-Domains sind erlaubt — der Server rendert die Seite headless,
    beliebige fremde URLs waeren ein Sicherheitsrisiko (SSRF).
    """
    v = (value or "").strip().lower()
    if not v.startswith(("http://", "https://")):
        return False
    from urllib.parse import urlparse
    host = urlparse(v).netloc
    return "aliexpress." in host


async def discover(db: Session, *, niches: list[str] | None = None, target: int = 100,
                   per_query: int = 45, min_price: float = 0.0, min_profit: float = 0.0,
                   min_margin: float = 0.20, max_delivery: int = 10, min_rating: float = 4.0,
                   choice_only: bool = False, per_niche_cap: int | None = None,
                   min_cost: float = 0.0, max_cost: float = 0.0,
                   from_trend: bool = False, avoid_electronics: bool | None = None,
                   local_only: bool = False, use_hub: bool = True,
                   trust_pages: bool = True,
                   product_ids: list[str] | None = None,
                   id_import_label: str | None = None) -> dict:
    """AliExpress nach passenden, margenstarken Produkten durchsuchen -> ProductIdea-Rows.

    Diversitaet erzwungen: hoechstens `per_niche_cap` Treffer je Nische, damit die
    ersten Nischen der Liste nicht das ganze Kontingent fuellen.

    ``avoid_electronics`` (Default aus Config): klar elektronische/batteriebetriebene
    Artikel werden aussortiert (Retouren + ElektroG/BattG). Der Filter greift FRUEH am
    Suchtitel (spart teure Detail-Calls) und nochmal am angereicherten Titel.
    """
    ae = _real_ae()
    s = get_settings()
    if avoid_electronics is None:
        avoid_electronics = s.avoid_electronics
    # HARTER Zoll-Deckel: keine Artikel ueber der EK-Obergrenze einstellen (ab ~150 EUR
    # Warenwert drohen Zollanmeldung + Zusatzkosten; s. max_source_cost_eur). Wirkt IMMER,
    # zusaetzlich zur optionalen EK-Spanne des Nutzers -> die STRENGSTE Grenze gewinnt.
    customs_cap = s.max_source_cost_eur or 0.0
    eff_max_cost = max_cost
    if customs_cap > 0:
        eff_max_cost = customs_cap if not eff_max_cost else min(eff_max_cost, customs_cap)
    niches = niches or DEFAULT_NICHES
    # ID-Import (14.08., Store 1105638009): AliExpress liefert manche Store-Seiten
    # an Server-IPs leer aus — die IDs kommen dann extern (Browser-Ernte) und
    # laufen hier durch die NORMALE Pipeline (Enrich, Filter, Kalkulation).
    if product_ids:
        # id_import_label: sichtbare Nische der angelegten Ideen (z. B. "🏬 StoreName"
        # bei der taeglichen Store-Entdeckung) statt des generischen "ID-Import".
        niches = [(id_import_label or "ID-Import")[:120]]
    # use_hub=False: Aufrufer bringt seine EIGENE Seite mit (Store-Scan) — die
    # Hub-Seite wird dann nicht dazugemischt.
    if local_only and use_hub and not product_ids and (s.local_source_page_url or "").strip():
        # Lokal-Fokus (10.08.): Suchseiten sind fuer den Harvester blockiert, die API
        # kennt keinen Lager-Filter — die konfigurierte Hub-Seite (Local+/Versand aus
        # DE) liefert die Kandidaten. Keyword-Nischen dienen dann nur der Trend-Doku.
        page_niches = [n for n in niches if _is_aliexpress_page_url(n)]
        hub = s.local_source_page_url.strip()
        if hub not in page_niches:
            page_niches = [hub] + page_niches
        logger.info("lokal-fokus: %s Keyword-Nischen -> %s Hub-Seite(n) als Quelle",
                    len(niches), len(page_niches))
        niches = page_niches
    if per_niche_cap is None:
        per_niche_cap = max(3, -(-target // len(niches)) + 1)  # ceil + Puffer
    seen = {row for (row,) in db.execute(select(ProductIdea.aliexpress_id)).all()}
    kept = 0
    scanned = 0
    electronics_skipped = 0
    local_skipped = 0
    # Abwurf-Zaehler je Filter ("no silent caps"): macht sichtbar, WARUM
    # Kandidaten nicht uebernommen wurden (Store 1105638009: 40 geprueft, 0 uebernommen).
    drops = {"rating": 0, "lieferzeit": 0, "status": 0, "choice": 0,
             "ek_spanne": 0, "vk_min": 0, "gewinn": 0, "marge": 0,
             "duplikat": 0}
    for niche in niches:
        if kept >= target:
            break
        niche_kept = 0
        # Seiten-Import (08/2026): eine AliExpress-URL im Nischen-Feld wird headless
        # gerendert und die Produkt-IDs geerntet (z. B. "Local+"-Kampagne oder eine
        # "Versand aus Deutschland"-Suchseite) — danach laeuft die NORMALE Pipeline
        # (Enrich, Marge-Kriterien, Elektronik- und Lokal-Filter).
        niche_label = niche
        if product_ids:
            # Extern geerntete IDs: konservativ OHNE Lokal-Blankovertrauen —
            # EU zaehlt nur per "Versand aus"-Achse am Produkt; die Kalkulation
            # waehlt lokal/China dann automatisch je Produkt.
            candidates = [{"id": pid, "title": None, "image": None, "score": None,
                           "orders": None, "price": None, "url": _norm_url(None, pid),
                           "local_src": False}
                          for pid in product_ids]
        elif _is_aliexpress_page_url(niche):
            from app.integrations import aliexpress_store as _store
            try:
                page_ids = await _store.fetch_page_product_ids(
                    niche, limit=max(per_query, target * 3))
            except Exception as exc:  # noqa: BLE001
                logger.warning("seiten-import '%s' fehlgeschlagen: %s", niche[:80], exc)
                continue
            # local_src bei Lokal-Fokus: die Seite ist die gewaehlte Lokal-Quelle
            # (Hub "Versand aus DE") — Ein-Lager-Produkte ohne "Versand aus"-Achse
            # gelten damit als belegt lokal (sonst 0-Ausbeute, Testlauf 10.08.).
            # trust_pages=False (Store-Scan): Store-Seiten sind KEIN Lokal-Beweis —
            # ein Store mit einem EU-Lager-Produkt versendet nicht alles aus der EU.
            # Dann zaehlt nur die explizite "Versand aus"-EU-Achse am Produkt.
            candidates = [{"id": pid, "title": None, "image": None, "score": None,
                           "orders": None, "price": None, "url": _norm_url(None, pid),
                           "local_src": local_only and trust_pages}
                          for pid in page_ids]
            niche_label = "Seiten-Import"
        else:
            # Suchseiten-Ernte ist von AliExpress blockiert (Test 10.08., 3 URL-Formate)
            # — Keywords laufen ueber die API-Suche; bei local_only kommen die
            # Kandidaten regulaer von der Hub-Seite (siehe oben).
            try:
                candidates = await _text_search(ae, niche, page_size=per_query)
            except Exception as exc:  # noqa: BLE001
                logger.warning("text.search '%s' fehlgeschlagen: %s", niche, exc)
                continue
        await asyncio.sleep(_RATE_S)
        # Vorfilter: grobes Rating aus der Suche
        candidates = [c for c in candidates if c["score"] is None or c["score"] >= min_rating]
        for c in candidates:
            if kept >= target or niche_kept >= per_niche_cap:
                break
            # Duplikat-Pruefung ueber BEIDE ID-Formen derselben Ware (3256/1005,
            # Offset 2^51): Browser-Ernten liefern die 3256-Form, die DB fuehrt
            # meist die 1005-Form — exakter Vergleich uebersah das (Review 16.08.).
            from app.integrations.aliexpress_store import id_variants as _idv
            _formen = _idv(c["id"]) if c["id"] else []
            if not _formen or any(v in seen for v in _formen):
                # Sichtbar zaehlen ("no silent caps", Nintendo-Befund 19.08.:
                # 12 von 20 Treffern waren stille Duplikate zu Bestands-Ideen).
                drops["duplikat"] += 1
                continue
            seen.update(_formen)
            # FRUEH aussortieren (spart den teuren Detail-Call): Elektronik am Suchtitel.
            if avoid_electronics and _is_electronic(c.get("title")):
                electronics_skipped += 1
                continue
            # EK-Spanne gesetzt? Schon am SUCH-Preis grob vorfiltern (spart den teuren Detail-
            # Call bei klar zu billigen/teuren Treffern). Toleranz, weil der Detailpreis leicht
            # abweichen kann – die EXAKTE Grenze zieht der Filter nach dem Enrich.
            if (min_cost or eff_max_cost) and c.get("price"):
                pre_ek = pricing.effective_cost(c["price"], settings=s)
                if (min_cost and pre_ek < min_cost * 0.8) or (eff_max_cost and pre_ek > eff_max_cost * 1.25):
                    continue
            scanned += 1
            try:
                e = await _enrich(ae, c["id"])
            except Exception:  # noqa: BLE001
                continue
            await asyncio.sleep(_RATE_S)
            # Nochmal am vollstaendigen (angereicherten) Titel pruefen.
            if avoid_electronics and _is_electronic(e.get("title")):
                electronics_skipped += 1
                continue
            # Lokal-Lager-Filter (Nutzerfokus 08/2026): nur Produkte mit "Versand aus"-
            # Option in einem EU-Lager. Konservativ: keine Achse -> gilt als nicht lokal.
            # Lokal-Beweis: EU-Achse ODER (Hub-/Seiten-Quelle UND keine Achse — Ein-
            # Lager-Produkte tragen oft keine "Versand aus"-Achse). Eine EXPLIZITE
            # Nur-China-Achse schlaegt das Seiten-Vertrauen (gemischte Seiten).
            _ships = e.get("ships_from")
            if local_only and not (api.has_eu_warehouse(_ships)
                                   or (c.get("local_src") and not _ships)):
                local_skipped += 1
                continue

            rating = e["rating"] if e["rating"] is not None else c["score"]
            # 0.0 OHNE eine einzige Rezension heisst "noch unbewertet", nicht
            # "schlecht" (Store 1105638009: brandneuer Shop, 40/40 rating-Drops).
            # Unbewertet wird wie None behandelt (bestehende Regel: None passiert).
            if rating is not None and float(rating) == 0.0 and not e.get("reviews"):
                rating = None
            if rating is not None and rating < min_rating:
                drops["rating"] += 1
                continue
            if e["delivery_days"] is not None and e["delivery_days"] > max_delivery:
                drops["lieferzeit"] += 1
                continue
            if e["status"] and str(e["status"]).lower() not in ("onselling", "on_selling", "active"):
                drops["status"] += 1
                continue
            if choice_only and not e["sl_product"]:
                drops["choice"] += 1
                continue

            raw_price = e["price_cny"] or c["price"]
            # Lokal (EU-Lager): angezeigter Preis = Endpreis -> kein Zoll, keine
            # Versand-Schaetzung (Nutzerregel 08.08.2026).
            _ships2 = e.get("ships_from")
            is_local_src = (api.has_eu_warehouse(_ships2)
                            or bool(c.get("local_src") and not _ships2))
            ek = pricing.effective_cost(raw_price, settings=s, local=is_local_src)
            if ek <= 0 or ek < min_cost:
                drops["ek_spanne"] += 1
                continue
            # EK-Obergrenze = strengste aus Nutzer-EK-Spanne und Zoll-Deckel (0 = keine Grenze).
            if eff_max_cost and ek > eff_max_cost:
                drops["ek_spanne"] += 1
                continue
            # VK ueber das ECHTE Listing-Preismodell (25 % Marge ODER 4 € Mindestgewinn,
            # was hoeher ist) – so stimmen VK/Gewinn/Marge mit dem spaeteren Listing ueberein
            # (Nutzerregel 10.07.: kein 8-€-Zwang, alle Preise/Kategorien, Bedingung = die
            # Zielmarge ist erreichbar). Gewinn/Marge EXAKT an diesem Preis.
            br = pricing.upload_breakdown_from_cny(raw_price, settings=s, local=is_local_src)
            vk = br.rounded_price_eur
            profit = br.profit_eur
            margin = br.margin_pct if br.margin_pct is not None else (profit / vk if vk else 0.0)
            if min_price and vk < min_price:            # Default 0 = keine VK-Untergrenze
                drops["vk_min"] += 1
                continue
            if min_profit and profit < min_profit:      # Default 0 = kein Gewinn-Filter
                drops["gewinn"] += 1
                continue
            # Bedingung: die geforderte Mindest-Marge muss erreichbar sein (Default 25 %).
            if min_margin and margin < min_margin - 1e-9:
                drops["marge"] += 1
                continue

            db.add(ProductIdea(
                aliexpress_id=c["id"],
                aliexpress_url=_norm_url(c["url"], c["id"]),
                title=(e["title"] or c["title"] or "")[:500],
                image_url=(e["images"][0] if e["images"] else c["image"]),
                category=e["category_id"] or None,
                niche=niche_label,
                cost_eur=round(ek, 2),
                price_eur=vk,
                profit_eur=profit,
                margin_pct=round(margin, 4) if vk else None,
                rating=rating,
                reviews=e["reviews"],
                orders_volume=c["orders"],
                delivery_days=e["delivery_days"],
                store_name=e["store_name"],
                store_id=e.get("store_id"),
                is_choice=e["sl_product"],
                from_trend=from_trend,
                # Seiten-Vertrauen (Hub "Versand aus DE", Achse fehlt): "DE" als belegten
                # Versand-aus-Wert speichern, damit Anzeige/Weiterverarbeitung konsistent
                # lokal rechnen (list_ideas.local_warehouse, Upload-Kalkulation).
                ships_from=(e.get("ships_from")
                            or (["Germany"] if is_local_src else None)),
                status="new",
            ))
            kept += 1
            niche_kept += 1
        db.commit()
    logger.info("research fertig: %s Ideen aus %s geprüften Kandidaten (%s Elektronik übersprungen)",
                kept, scanned, electronics_skipped)
    ergebnis = {"kept": kept, "scanned": scanned, "target": target,
                "local_skipped": local_skipped, "niches": len(niches),
                "electronics_skipped": electronics_skipped, "drops": drops}
    # Ergebnis fuers Dashboard merken (Nutzerfrage 19.08.: "25 eingegeben, kein
    # einziges — wie kann das sein?" — /status sagte bis dahin nur running=false,
    # die Abwurf-Gruende landeten ausschliesslich im Server-Log).
    from datetime import datetime, timezone
    global _last_result
    _last_result = {**ergebnis,
                    "begriffe": [str(n)[:60] for n in niches[:5]],
                    "beendet": datetime.now(timezone.utc).isoformat()}
    return ergebnis


def prune_old_ideas(db: Session, *, days: int = 5,
                    statuses: tuple = ("new", "rejected")) -> dict:
    """Alte, UNBEARBEITETE Produkt-Ideen loeschen (gegen den Stau). Standard: Status
    'new'+'rejected' aelter als ``days`` Tage. 'gemerkt'/importiert bleiben IMMER erhalten.
    🏬-Store-Ideen (taegliche Store-Entdeckung) bleiben EBENFALLS immer stehen —
    Nutzerauftrag 15.08.: "einmal taeglich reicht ohne zu loeschen"."""
    from datetime import datetime, timedelta
    days = max(1, int(days))   # nie < 1 -> ein negativer/0-Wert darf NIE alles (auch frische) loeschen
    cutoff = datetime.utcnow() - timedelta(days=days)   # naive UTC (passt zu created_at)
    old = db.scalars(select(ProductIdea).where(
        ProductIdea.status.in_(list(statuses)),
        ProductIdea.created_at < cutoff,
        ProductIdea.niche.is_(None) | ProductIdea.niche.notlike("🏬%"))).all()
    n = len(old)
    for idea in old:
        db.delete(idea)
    db.commit()
    logger.info("prune_old_ideas: %s Ideen (>%s Tage, %s) geloescht", n, days, statuses)
    return {"deleted": n, "days": days}


_STORE_SEEN_FILE = "./data/store_ideas_seen.json"


def _store_seen_load() -> dict:
    """Merkliste der Store-Entdeckung ({store_id: {name, date, fails}})."""
    import json
    from pathlib import Path
    try:
        d = json.loads(Path(_STORE_SEEN_FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 – fehlt/kaputt = leere Merkliste
        return {}


def _store_seen_save(seen: dict) -> None:
    import json
    from pathlib import Path
    p = Path(_STORE_SEEN_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")


async def daily_store_discovery(db: Session, *, stores_per_day: int | None = None,
                                per_store: int | None = None) -> dict:
    """Taeglich N NEUE AliExpress-Stores entdecken und ihre Bestseller als
    Produkt-Ideen ablegen (Nutzerauftrag 15.08.: "such uns jeden Tag 3 Stores raus
    und fueg sie bei Produktideen ein, ohne zu loeschen").

    Suchseiten sind fuer Server-IPs blockiert (Bot-Schutz, live verifiziert 16.08.) —
    STORE-Seiten funktionieren mit der Browser-Tarnung. Kandidaten kommen daher aus
    den Store-IDs der vorhandenen Ideen (liefert die API-Suche mit); Ideen mit
    belegtem EU-Lager (ships_from) haben Vorrang. Je Store laufen die Bestseller
    durch die NORMALE discover-Pipeline (Enrich, Filter, Kalkulation, Dedup) und
    landen unter der Nische "🏬 <Store>" — prune-fest (siehe prune_old_ideas).

    Nach jedem Store wird die Merkliste SOFORT persistiert (crash-sicher); ein
    unlesbarer Store zaehlt Fehlversuche und wird nach 3 Fehlschlaegen aufgegeben.
    """
    from datetime import datetime, timezone
    s = get_settings()
    n_stores = stores_per_day or s.store_discovery_count
    n_ideen = per_store or s.store_discovery_per_store
    seen = _store_seen_load()

    # Kandidaten: EU-Lager-Ideen zuerst (Nutzerwunsch: Stores, die lokal versenden),
    # danach der Rest; innerhalb der Paesse neueste Ideen zuerst.
    rows = db.execute(
        select(ProductIdea.store_id, ProductIdea.store_name, ProductIdea.ships_from)
        .where(ProductIdea.store_id.isnot(None))
        .order_by(ProductIdea.created_at.desc())).all()
    kandidaten: list[tuple[str, str]] = []
    dedup: set[str] = set()
    for eu_pass in (True, False):
        for sid, name, ships in rows:
            sid = str(sid)
            # ECHTES EU-Lager zaehlt (has_eu_warehouse), nicht nur "Achse vorhanden" —
            # ships_from=["China"] wuerde sonst als EU-Vorrang gelten (Review-Fund 16.08.).
            if api.has_eu_warehouse(ships) != eu_pass or sid in dedup:
                continue
            eintrag = seen.get(sid) or {}
            if eintrag.get("date"):
                continue                       # schon verarbeitet -> nie doppelt
            if int(eintrag.get("fails") or 0) >= 3:
                continue                       # dauerhaft unlesbar -> aufgeben
            dedup.add(sid)
            kandidaten.append((sid, str(name or "")))

    from app.integrations import aliexpress_store as _store
    ergebnisse: list[dict] = []
    erfolge = 0
    versuche = 0
    for sid, name in kandidaten:
        # Deckel: nach n_stores Erfolgen fertig; Fehlversuche kosten je einen
        # Browser-Lauf (~1-2 min) -> hart bei 3x n_stores Versuchen abbrechen.
        if erfolge >= n_stores or versuche >= n_stores * 3:
            break
        versuche += 1
        try:
            ids = await _store.fetch_store_product_ids(sid, limit=max(n_ideen * 4, 20))
        except Exception as exc:  # noqa: BLE001 – Bot-Block/Timeout: zaehlen, weiter
            eintrag = seen.get(sid) or {"name": name}
            eintrag["fails"] = int(eintrag.get("fails") or 0) + 1
            seen[sid] = eintrag
            _store_seen_save(seen)
            logger.warning("store-entdeckung: Store %s nicht lesbar: %s", sid, str(exc)[:120])
            ergebnisse.append({"store_id": sid, "name": name, "kept": None,
                               "fehler": str(exc)[:160]})
            continue
        label = f"🏬 {name.strip() or sid}"
        r = await discover(db, product_ids=ids, target=n_ideen, use_hub=False,
                           trust_pages=False, id_import_label=label)
        seen[sid] = {"name": name, "date": datetime.now(timezone.utc).isoformat(),
                     "fails": 0}
        _store_seen_save(seen)
        erfolge += 1
        ergebnisse.append({"store_id": sid, "name": name,
                           "kept": r.get("kept", 0), "gescannt": r.get("scanned", 0)})
    return {"stores": ergebnisse, "neu_verarbeitet": erfolge,
            "ideen": sum(e.get("kept") or 0 for e in ergebnisse if e.get("kept")),
            "kandidaten": len(kandidaten)}


_TRENDS_FILE = "./data/last_trends.json"

# EIN gemeinsamer Lauf-Lock fuer ALLE Such-Pfade (manuelle Suche, Trend-Suche,
# woechentlicher Scheduler-Job). Verhindert parallele Laeufe -> keine doppelten
# BEZAHLTEN web_search-Aufrufe und keine gleichzeitigen ProductIdea-Schreiber.
# threading.Lock, weil Router-Handler im Threadpool, der Scheduler im Loop laeuft.
_run_lock = threading.Lock()
_run_active = {"active": False}


def try_acquire_run() -> bool:
    """Atomar den Lauf-Lock holen. True = geholt (Aufrufer MUSS release_run()
    im finally aufrufen), False = es laeuft bereits ein Suchlauf."""
    with _run_lock:
        if _run_active["active"]:
            return False
        _run_active["active"] = True
        return True


def release_run() -> None:
    with _run_lock:
        _run_active["active"] = False


def is_running() -> bool:
    return _run_active["active"]


async def discover_trends(db: Session, *, target: int = 25, min_price: float = 0.0,
                          min_profit: float = 0.0, min_rating: float = 4.0,
                          max_delivery: int = 10, max_cost: float = 0.0,
                          min_margin: float = 0.20, local_only: bool = False,
                          fallback_china: bool = False) -> dict:
    """KI-Trend-Recherche: Claude recherchiert aktuelle Verkaufstrends (Web) und
    leitet AliExpress-Suchbegriffe ab -> normale discover()-Suche mit Marge-Kriterien.
    Die gefundenen Trend-Begriffe landen in ``last_trends.json`` fuers Dashboard.
    """
    from datetime import datetime, timezone
    import json as _json
    from pathlib import Path

    from app.integrations import get_llm_client
    ctx = f"Heutiges Datum: {datetime.now(timezone.utc).date().isoformat()}. Zielmarkt: eBay.de (Deutschland)."
    terms = await get_llm_client().research_trends(context=ctx, max_terms=get_settings().trend_research_terms)
    keywords = [t["keyword"] for t in terms if t.get("keyword")]
    Path(_TRENDS_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(_TRENDS_FILE).write_text(_json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(), "terms": terms},
        ensure_ascii=False, indent=1), encoding="utf-8")
    if not keywords:
        return {"trends": [], "kept": 0, "scanned": 0, "note": "keine Trend-Keywords ermittelt"}
    res = await discover(db, niches=keywords, target=target, min_price=min_price,
                         min_profit=min_profit, min_rating=min_rating, min_margin=min_margin,
                         max_delivery=max_delivery, max_cost=max_cost, from_trend=True,
                         local_only=local_only)
    # Nutzerwunsch 14.08. ("wir brauchen einfach input täglich"): liefert die
    # Lokal-Quelle nichts Neues mehr (Hub leergeerntet), fuellt eine zweite Runde
    # mit NORMALEN China-Produkten auf — gleiche Trend-Keywords, Kalkulation
    # rechnet automatisch mit Zoll-/Versandpuffer (local=False), Elektronik-
    # Filter greift weiter. Lieferzeit-Deckel dann der normale (10 Tage).
    if fallback_china and local_only and (res.get("kept") or 0) < target:
        rest = target - (res.get("kept") or 0)
        res2 = await discover(db, niches=keywords, target=rest, min_price=min_price,
                              min_profit=min_profit, min_rating=min_rating,
                              min_margin=min_margin, max_delivery=10,
                              max_cost=max_cost, from_trend=True, local_only=False)
        res = {**res,
               "kept": (res.get("kept") or 0) + (res2.get("kept") or 0),
               "scanned": (res.get("scanned") or 0) + (res2.get("scanned") or 0),
               "china_kept": res2.get("kept") or 0,
               "china_scanned": res2.get("scanned") or 0}
    logger.info("trend-research fertig: %s Begriffe, %s Ideen", len(keywords), res.get("kept"))
    return {"trends": terms, **res}


def winner_dna_context(db: Session, *, max_items: int = 25, min_sales: int = 2) -> str:
    """Gewinner-Steckbrief fuer den KI-Scout: was verkauft sich NACHWEISLICH?

    Datenlage (Portfolio-Analyse 08/2026): 79 % der aktiven Listings werden zwar
    angeklickt, aber nie gekauft — breites Zufalls-Sourcing funktioniert nicht.
    Der Steckbrief speist deshalb die ECHTEN Verkaeufer (eBay-Lebenszeit-Zaehler)
    als Vorgabe in die Recherche, statt einer statischen Nischenliste zu vertrauen.
    """
    from app.models import Listing
    rows = db.scalars(select(Listing)
                      .where(Listing.listing_status == "active",
                             Listing.sales_total >= min_sales)
                      .order_by(Listing.sales_total.desc())
                      .limit(max_items)).all()
    if not rows:
        return ""
    lines = []
    for l in rows:
        preis = f"{float(l.price_eur):.2f} EUR" if l.price_eur is not None else "Preis ?"
        cat = f" | {l.category_name}" if l.category_name else ""
        lines.append(f"- {l.title_seo[:90]} | {preis} | {l.sales_total} Verkäufe{cat}")
    return "\n".join(lines)


async def winner_clone(db: Session, *, target: int = 25, min_price: float = 0.0,
                       min_profit: float = 0.0, min_rating: float = 4.0,
                       max_delivery: int = 10, max_cost: float = 0.0,
                       min_margin: float = 0.20, local_only: bool = False) -> dict:
    """Gewinner klonen: Suchbegriffe aus den eigenen Bestsellern ableiten -> discover().

    Gleicher Ablauf wie discover_trends(), aber der KI-Kontext prioritisiert die
    NACHGEWIESENEN Shop-Gewinner (Varianten, Schwester-Produkte, Zubehoer derselben
    Zielgruppe) statt allgemeiner Web-Trends. Elektronik-Filter etc. gelten unveraendert
    ueber discover() — Compliance-Filter werden hier bewusst NICHT gelockert.
    """
    from datetime import datetime, timezone

    from app.integrations import get_llm_client

    dna = winner_dna_context(db)
    if not dna:
        return {"terms": [], "kept": 0, "scanned": 0,
                "note": 'keine Gewinner mit genug Verkäufen — erst „Statistik laden" ausführen'}
    ctx = (
        f"Heutiges Datum: {datetime.now(timezone.utc).date().isoformat()}. "
        "Zielmarkt: eBay.de (Deutschland).\n"
        "WICHTIGSTE VORGABE — GEWINNER KLONEN: Die folgenden Artikel verkaufen sich "
        "NACHWEISLICH in unserem Shop. Leite die Suchbegriffe VORRANGIG aus diesen "
        "Gewinnern ab: naheliegende Varianten (Farben/Groessen/Motive), Schwester-Produkte, "
        "Zubehoer und Nachbar-Motive derselben Zielgruppen. Allgemeine Web-Trends nur "
        "ergaenzend, wenn sie zu den Gewinner-Zielgruppen passen.\n"
        f"Unsere Gewinner:\n{dna}")
    terms = await get_llm_client().research_trends(
        context=ctx, max_terms=get_settings().trend_research_terms)
    keywords = [t["keyword"] for t in terms if t.get("keyword")]
    if not keywords:
        return {"terms": terms, "kept": 0, "scanned": 0, "note": "keine Keywords ermittelt"}
    res = await discover(db, niches=keywords, target=target, min_price=min_price,
                         min_profit=min_profit, min_rating=min_rating, min_margin=min_margin,
                         max_delivery=max_delivery, max_cost=max_cost, from_trend=True,
                         local_only=local_only)
    logger.info("winner-clone fertig: %s Begriffe, %s Ideen", len(keywords), res.get("kept"))
    return {"terms": terms, **res}


def _as_list(v):
    if isinstance(v, dict):
        return [v]
    return v or []


async def source_info(aliexpress_id: str) -> dict:
    """Diagnose (NUR LESEN): Roh-Logistik + SKU-Eigenschaften eines AliExpress-Produkts.

    Zweck 08/2026: herausfinden, wie „Versand aus" (Ships From) in der ECHTEN
    ds.product.get-Antwort heisst, bevor der Lokal-Filter gebaut wird. Die lokalen
    Dev-Keys sind Platzhalter — der Test laeuft deshalb ueber diesen Endpunkt auf
    dem Live-System. Fehler werden als Felder zurueckgegeben, nie geworfen.
    """
    ae = _real_ae()
    out: dict = {"aliexpress_id": str(aliexpress_id)}
    try:
        data = await ae._call("aliexpress.ds.product.get", {
            "product_id": str(aliexpress_id), "ship_to_country": "DE",
            "target_currency": "EUR", "target_language": "de"})
        result = api._unwrap_result(data)
        out["logistics_info_dto"] = (api._deep_get(result, "logistics_info_dto")
                                     or api._deep_get(result, "ae_item_logistics_info_dto") or {})
        sku_root = api._deep_get(result, "ae_item_sku_info_dtos") or {}
        skus = _as_list(sku_root.get("ae_item_sku_info_d_t_o")
                        if isinstance(sku_root, dict) else sku_root)
        axes: dict[str, list] = {}
        sku_summaries = []
        for sku in skus[:40]:
            props_root = (sku or {}).get("ae_sku_property_dtos") or {}
            props = _as_list(props_root.get("ae_sku_property_d_t_o")
                             if isinstance(props_root, dict) else props_root)
            plist = []
            for p in props:
                name = p.get("sku_property_name")
                val = (p.get("sku_property_value")
                       or p.get("property_value_definition_name"))
                plist.append({"id": p.get("sku_property_id"), "name": name, "value": val})
                if name is not None:
                    key = f"{name} [id={p.get('sku_property_id')}]"
                    if str(val) not in axes.setdefault(key, []):
                        axes[key].append(str(val))
            sku_summaries.append({"sku_attr": (sku or {}).get("sku_attr")
                                  or (sku or {}).get("id"), "properties": plist})
        out["axes"] = axes
        out["skus"] = sku_summaries[:10]
    except Exception as exc:  # noqa: BLE001 – Diagnose darf nie werfen
        out["error"] = f"product.get fehlgeschlagen: {str(exc)[:220]}"
    try:
        out["freight"] = await ae.query_freight(product_id=str(aliexpress_id))
    except Exception as exc:  # noqa: BLE001
        out["freight_error"] = str(exc)[:220]
    return out


def last_trends() -> dict:
    """Zuletzt recherchierte Trend-Begriffe (fuers Dashboard)."""
    import json as _json
    from pathlib import Path
    p = Path(_TRENDS_FILE)
    if not p.exists():
        return {"terms": [], "generated_at": None}
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"terms": [], "generated_at": None}


def list_ideas(db: Session, *, status: str | None = None, limit: int = 200,
               sort: str = "newest", niche: str | None = None) -> dict:
    # "newest" (Default): zuletzt gefundene Ideen immer oben (id = Einfüge-Reihenfolge);
    # "profit": höchster Gewinn zuerst (alte Ansicht).
    order = (ProductIdea.profit_eur.desc().nullslast()
             if sort == "profit" else ProductIdea.id.desc())
    stmt = select(ProductIdea).order_by(order).limit(min(max(limit, 1), 500))
    if status and status != "all":
        # „offen" (new) zeigt auch die gerade laufenden UND fehlgeschlagenen Importe – sonst
        # verschwaenden angeklickte Ideen spurlos bzw. ein Fehler bliebe unsichtbar.
        if status == "new":
            stmt = stmt.where(ProductIdea.status.in_(("new", "importing", "import_failed")))
        else:
            stmt = stmt.where(ProductIdea.status == status)
    if niche:   # Filter auf eine Nische/Trend-Begriff (Klick auf einen Trend-Chip)
        stmt = stmt.where(ProductIdea.niche == niche)
    rows = db.scalars(stmt).all()
    def d(x):
        return {
            "id": x.id, "aliexpress_id": x.aliexpress_id, "url": x.aliexpress_url,
            "title": x.title, "image": x.image_url, "niche": x.niche, "category": x.category,
            "cost_eur": float(x.cost_eur) if x.cost_eur is not None else None,
            "price_eur": float(x.price_eur) if x.price_eur is not None else None,
            "profit_eur": float(x.profit_eur) if x.profit_eur is not None else None,
            "margin_pct": x.margin_pct, "rating": x.rating, "reviews": x.reviews,
            "orders_volume": x.orders_volume, "delivery_days": x.delivery_days,
            "store_name": x.store_name, "store_id": x.store_id,
            "is_choice": bool(x.is_choice),
            "from_trend": bool(x.from_trend),
            "ships_from": getattr(x, "ships_from", None) or [],
            "local_warehouse": api.has_eu_warehouse(getattr(x, "ships_from", None)),
            "source": getattr(x, "source", None) or "aliexpress",
            "source_note": getattr(x, "source_note", None),
            "source_url": getattr(x, "source_url", None),
            "alternatives": x.alternatives or [], "status": x.status,
            "import_error": x.import_error,
        }
    items = [d(x) for x in rows]
    total = db.scalar(select(__import__("sqlalchemy").func.count()).select_from(ProductIdea)) or 0
    return {"ideas": items, "count": len(items), "total": int(total)}


async def find_alternatives(db: Session, *, idea_id: int, limit: int = 6) -> dict:
    """Per Bildsuche gleiche/ähnliche Produkte anderer Händler zur Idee finden + speichern."""
    idea = db.get(ProductIdea, idea_id)
    if idea is None or not idea.image_url:
        return {"idea_id": idea_id, "count": 0, "alternatives": [], "error": "kein Bild"}
    ae = _real_ae()
    s = get_settings()
    try:
        img = (await ae._http().get(idea.image_url, timeout=20)).content
        hits = await ae.image_search(img, page_size=limit + 4)
    except Exception as exc:  # noqa: BLE001
        logger.warning("image_search idea %s failed: %s", idea_id, exc)
        return {"idea_id": idea_id, "count": 0, "alternatives": [], "error": str(exc)[:120]}

    alts = []
    for h in hits:
        if h["aliexpress_id"] == idea.aliexpress_id:
            continue
        price = h.get("price_eur")
        # KEIN local= moeglich: h ist ein Bildsuche-Treffer, kein gespeichertes Produkt -
        # Variantendaten mit ``ship_from`` gibt es hier nicht. Der Wert ist deshalb eine
        # OBERGRENZE (China-Annahme): kommt die Ware aus einem EU-Lager, faellt der echte
        # Preis um Versand und Zoll niedriger aus. Fuer eine Vorschau vertretbar - sobald
        # das Produkt wirklich angelegt wird, rechnet der Import mit dem echten Lager.
        # Genauer ginge es nur mit einem Netz-Call je Treffer.
        ek = pricing.effective_cost(price, settings=s) if price else None
        vk = pricing.price_from_cny(price, settings=s).rounded_price_eur if price else None
        alts.append({
            "aliexpress_id": h["aliexpress_id"], "url": h["url"], "title": h.get("title"),
            "store_url": h.get("store_url"), "price_eur": price,
            "ek_eur": round(ek, 2) if ek else None, "vk_eur": vk,
        })
        if len(alts) >= limit:
            break
    # Durchschnitts-EK (Quelle + Alternativen) als Orientierung
    costs = [a["price_eur"] for a in alts if a["price_eur"]]
    if idea.cost_eur:
        costs.append(float(idea.cost_eur))
    avg = round(sum(costs) / len(costs), 2) if costs else None
    idea.alternatives = {"items": alts, "avg_price_eur": avg}
    db.commit()
    return {"idea_id": idea.id, "count": len(alts), "avg_price_eur": avg, "alternatives": alts}


async def create_from_idea(db: Session, *, idea_id: int, publish: bool = False,
                           min_profit_eur: float | None = None) -> dict:
    """Aus einer Idee ein Listing bauen (Scrape + KI) und als eBay-ENTWURF anlegen
    oder direkt LIVE stellen. Fehler werden mit Klartext zurückgegeben.

    ``min_profit_eur``: individueller Ziel-Mindestgewinn € (statt Config-8 €);
    wirkt auf den Basis- UND alle Varianten-Preise (Konkurrenzfaehigkeit)."""
    from app.models import Listing, Product
    from app.services import golive_service, product_service
    idea = db.get(ProductIdea, idea_id)
    if idea is None or not idea.aliexpress_url:
        raise ValueError("Idee/AliExpress-URL fehlt")

    # Bereits importiert? -> vorhandenes Listing wiederverwenden.
    product = db.scalar(select(Product).where(Product.aliexpress_url == idea.aliexpress_url))
    if product is not None:
        listing = db.scalar(select(Listing).where(Listing.product_id == product.id))
    else:
        res = await product_service.upload_product(
            db, aliexpress_url=idea.aliexpress_url, skip_autods=True)
        listing = db.get(Listing, res["listing_id"])
    if listing is None:
        raise ValueError("Listing konnte nicht erstellt werden")
    if product is None:   # frisch via upload_product angelegt -> aus Listing holen
        product = db.get(Product, listing.product_id) if listing.product_id else None

    # Individueller Gewinn -> am Listing hinterlegen + Basispreis neu rechnen
    # (die Varianten-Preise nutzen ihn automatisch via compute_variant_prices).
    if min_profit_eur is not None and min_profit_eur >= 0 and product is not None \
            and product.price_cny is not None:
        listing.min_profit_eur = round(float(min_profit_eur), 2)
        s = get_settings()
        # EU-Lager mitgeben - diese Stelle SCHREIBT den Verkaufspreis. Ohne den Hinweis
        # bekaeme Ware aus einem deutschen Lager China-Versand und Zollpauschale
        # aufgeschlagen und damit einen dauerhaft um rund 6-7 EUR zu hohen Preis.
        # Der Shop-Import macht es richtig (product_service:349), dieser Weg tat es
        # nicht - er faellt nur seltener auf, weil er ueber die Recherche laeuft.
        from app.services.fast_shipping_service import variants_have_eu_warehouse
        br = pricing.price_from_cny(product.price_cny, settings=s,
                                    min_profit_eur=float(min_profit_eur),
                                    local=variants_have_eu_warehouse(product))
        listing.price_eur = br.rounded_price_eur

    db.commit()   # Listing/Preis gesichert; Status erst NACH erfolgreichem Entwurf/Queue setzen

    if publish:
        # LIVE: eBay-Publish in die Warteschlange (Hintergrund, Retries).
        from app.services import publish_queue
        r = await publish_queue.enqueue(listing.id)
        idea.status = "imported"
        idea.import_error = None
        db.commit()
        return {"idea_id": idea.id, "listing_id": listing.id, **r}
    # ENTWURF = NUR LOKAL (Nutzerwunsch 10.07.): KEIN eBay-Call. Der lokale Entwurf
    # (listing_status="draft") steht sofort im Tab „eBay-Produkte"; online geht er erst
    # bei „Live" (publish=True bzw. „🚀 Live stellen"). Vorteil: schnell + robust, keine
    # minutenlangen eBay-Transaktionen, die (SQLite) parallele Anlagen aussperren.
    idea.status = "imported"
    idea.import_error = None
    db.commit()
    return {"idea_id": idea.id, "listing_id": listing.id,
            "status": "draft_local", "ebay_item_id": None, "draft_only": True}


# Referenzen auf laufende Hintergrund-Importe halten (sonst kann der GC die Task killen).
_import_tasks: set = set()
# Idee-IDs, die IN DIESEM PROZESS gerade importiert werden (eingereiht oder laufend).
# Das ist das ZUVERLAESSIGE Lebenszeichen: nur was hier drin steht, laeuft wirklich –
# ein "importing"-Status ohne Eintrag = verwaister Zombie eines frueheren Prozesses.
_importing_ids: set[int] = set()
# Ein Import haengt "zu lange": laenger als das reale Worst-Case (Scrape+LLM+Draft, dazu
# Warteschlange unter _UPLOAD_SEMAPHORE(2)) -> Re-Trigger erlaubt. Bewusst konservativ (30 min),
# damit ein noch LAUFENDER, nur langsamer Import nicht faelschlich doppelt gestartet wird
# (updated_at der Idee wird waehrend des Laufs NICHT angefasst).
STALE_IMPORT_MINUTES = 30
# Absolute Obergrenze: haengt eine Idee ueber mehrere Neustarts hinweg so lange, ist sie
# vermutlich "vergiftet" -> als import_failed markieren (manueller Retry) statt endlos requeuen.
HARD_STUCK_HOURS = 6


def begin_import(db: Session, *, idea_id: int, publish: bool = False,
                 min_profit_eur: float | None = None, force: bool = False) -> dict:
    """Startet den Import EINER Idee im HINTERGRUND (eigene DB-Session) und kehrt SOFORT
    zurueck. So blockiert der HTTP-Request nicht mehr, wenn mehrere Ideen gleichzeitig
    angeklickt werden – jede wird zuverlaessig abgearbeitet (Upload-Semaphore serialisiert
    die schwere Arbeit), der Fortschritt steht am Idee-Status:
    importing -> imported (Erfolg) / import_failed (mit Klartext-Fehler zum erneut Versuchen).

    Idempotent, aber SELBSTHEILEND bei Zombies: Stand der Status auf "importing", der Import
    aber lief in KEINEM lebenden Task dieses Prozesses (verwaist durch Neustart/Crash) und ist
    alt genug (bzw. ``force``), wird er neu eingereiht statt blockiert. Ohne diese Logik blieb
    eine Idee nach einem Neustart ewig auf "wird angelegt" haengen (Vorfall 11.07.)."""
    from datetime import datetime, timezone, timedelta
    idea = db.get(ProductIdea, idea_id)
    if idea is None or not idea.aliexpress_url:
        raise ValueError("Idee/AliExpress-URL fehlt")
    if idea.status == "imported":
        return {"idea_id": idea_id, "status": "imported", "already": True}
    if idea.status == "importing":
        # Laeuft NACHWEISLICH in diesem Prozess -> nie doppelt starten.
        if idea_id in _importing_ids:
            return {"idea_id": idea_id, "status": "importing", "already": True}
        # Kein lebender Task hier. Frisch (< Schwelle) und kein force -> vermutlich gerade erst
        # gestartet/anderer Weg: nicht anfassen. Sonst = Zombie -> unten neu einreihen.
        upd = idea.updated_at
        if upd is not None and upd.tzinfo is None:
            upd = upd.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_IMPORT_MINUTES)
        if not force and upd is not None and upd >= cutoff:
            return {"idea_id": idea_id, "status": "importing", "already": True}
    idea.status = "importing"
    idea.import_error = None
    idea.publish_requested = bool(publish)   # Absicht merken -> Recovery kann Live wiederherstellen
    db.commit()
    _schedule_import(idea_id, publish, min_profit_eur)
    return {"idea_id": idea_id, "status": "importing", "already": False}


def _schedule_import(idea_id: int, publish: bool, min_profit_eur: float | None) -> None:
    """Import als Loop-Task einreihen (Referenz halten). Ohne laufenden Loop (z.B. Test):
    synchron ausfuehren. Die Idee-ID wird in ``_importing_ids`` gefuehrt (Lebenszeichen) und
    im done-Callback wieder entfernt – so kann sie nach Abschluss/Fehler erneut versucht werden,
    aber solange sie laeuft nie doppelt gestartet werden."""
    coro = _run_import(idea_id, publish, min_profit_eur)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        _importing_ids.add(idea_id)
        task = loop.create_task(coro)
        _import_tasks.add(task)

        def _done(t, _id=idea_id):
            _import_tasks.discard(t)
            _importing_ids.discard(_id)

        task.add_done_callback(_done)
    else:
        _importing_ids.add(idea_id)
        try:
            asyncio.run(coro)
        finally:
            _importing_ids.discard(idea_id)


def _is_transient_db(exc: BaseException) -> bool:
    """Voruebergehende DB-/Netz-Fehler, die sich per Retry loesen (v.a. SQLite
    'database is locked', wenn ein anderer langer Schreiber gerade die Sperre haelt)."""
    s = str(exc).lower()
    return "database is locked" in s or "operationalerror" in type(exc).__name__.lower() \
        or "timeout" in s


async def _run_import(idea_id: int, publish: bool, min_profit_eur: float | None) -> None:
    """Ein Import im Hintergrund. Bei VORUEBERGEHENDEN Fehlern (DB-Sperre) wird kurz
    wiederholt; erst danach als 'import_failed' vermerkt (nie stillschweigend verloren) –
    der Nutzer sieht 'fehlgeschlagen' + kann erneut anklicken. create_from_idea ist
    wiederhol-sicher (vorhandenes Produkt/Listing wird wiederverwendet)."""
    from app.database import SessionLocal
    last_exc: BaseException | None = None
    for attempt in range(3):
        db = SessionLocal()
        try:
            await create_from_idea(db, idea_id=idea_id, publish=publish,
                                   min_profit_eur=min_profit_eur)
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            if _is_transient_db(exc) and attempt < 2:
                logger.info("idea import retry", extra={"idea_id": idea_id, "attempt": attempt + 1})
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            break
        finally:
            db.close()
    logger.warning("idea import failed", extra={"idea_id": idea_id, "error": str(last_exc)[:200]})
    db = SessionLocal()
    try:
        idea = db.get(ProductIdea, idea_id)
        if idea is not None:
            idea.status = "import_failed"
            idea.import_error = f"{type(last_exc).__name__}: {last_exc}"[:400]
            db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    finally:
        db.close()


def recover_stuck_imports() -> dict:
    """Beim App-Start: Ideen, die ein FRUEHERER Prozess in 'importing' liegen liess, wieder
    einreihen. Grund: der Import lief als In-Memory-asyncio-Task – stirbt der Prozess (Deploy/
    Crash), bleibt die Idee ewig 'wird angelegt' und ``begin_import`` liess sie nicht neu
    starten (Vorfall 11.07.). Analog zu ``publish_queue`` (haengende Go-Lives) und
    ``sweep_stale_claims`` (verwaiste Bestellungen).

    Sicher & konvergent: Beim Start laeuft KEIN Import-Task -> jedes 'importing' ist verwaist.
    Die urspruengliche Absicht (Entwurf vs. 🚀 Live) steht in ``publish_requested`` und wird
    wiederhergestellt – so wird ein Live-Wunsch nach einem Neustart nicht stillschweigend zum
    Entwurf (Review-Fund 11.07.). Wiederholt sich das ueber viele Neustarts (Crash vor
    Statuswechsel), fangen wir es nach ``HARD_STUCK_HOURS`` als import_failed ab -> kein
    Endlos-Requeue. IDs werden in EINER kurzen Read-Session gelesen und die Session VOR dem
    Einreihen geschlossen (keine Schreibsperre ueber die Task-Planung)."""
    from datetime import datetime, timezone, timedelta
    from app.database import SessionLocal
    now = datetime.now(timezone.utc)
    hard = now - timedelta(hours=HARD_STUCK_HOURS)
    requeue: list[tuple[int, bool]] = []   # (idea_id, publish) – Absicht mitnehmen
    failed = 0
    db = SessionLocal()
    try:
        rows = db.scalars(select(ProductIdea).where(ProductIdea.status == "importing")).all()
        for it in rows:
            upd = it.updated_at
            if upd is not None and upd.tzinfo is None:
                upd = upd.replace(tzinfo=timezone.utc)
            if upd is not None and upd < hard:
                it.status = "import_failed"
                it.import_error = "Import hing ueber Neustart(s) fest - bitte erneut versuchen (↻)."
                failed += 1
            elif it.id not in _importing_ids:
                requeue.append((it.id, bool(it.publish_requested)))
        if failed:
            db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        raise
    finally:
        db.close()
    for iid, publish in requeue:
        if iid not in _importing_ids:
            _schedule_import(iid, publish, None)
    if requeue or failed:
        logger.warning("idea imports recovered",
                       extra={"requeued": len(requeue), "failed": failed})
    return {"requeued": len(requeue), "failed": failed}


def set_status(db: Session, *, idea_id: int, status: str) -> dict:
    idea = db.get(ProductIdea, idea_id)
    if idea is None:
        return {"ok": False}
    idea.status = status
    db.commit()
    return {"ok": True, "id": idea_id, "status": status}
