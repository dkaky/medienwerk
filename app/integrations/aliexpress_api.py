"""AliExpress Open-Platform / Dropshipping-API – Protokoll-Schicht (rein, testbar).

Enthaelt die Signatur (HMAC-SHA256 ODER MD5-Secret-umrahmt), den Request-Bau,
die Produkt-ID-Extraktion aus URLs und das defensive Parsen der ds.*-Antworten.
Bewusst getrennt vom Client, damit Signatur/Parser ohne Netz getestet werden koennen.

Signatur (offizielle TOP-/IOP-Spezifikation):
* Alle Request-Parameter ausser ``sign`` nach Schluessel sortieren, als
  ``key1value1key2value2`` konkatenieren.
* ``sign_method=md5``    -> ``MD5(secret + concat + secret)``        (HEX, UPPER)
* ``sign_method=sha256`` -> ``HMAC_SHA256(concat, key=secret)``      (HEX, UPPER)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

_ID_PATTERNS = [
    re.compile(r"/item/(?:[a-z]{2}/)?(\d{6,})"),   # .../item/1005006789.html, /item/de/...
    re.compile(r"/i/(\d{6,})"),                     # .../i/1005006789.html
    re.compile(r"(?:productId|product_id)=(\d{6,})", re.IGNORECASE),
    re.compile(r"(\d{10,})"),                        # Fallback: lange Ziffernfolge
]


class AliExpressApiError(Exception):
    """Fehlerhafte API-Antwort (error_response oder fehlende Nutzdaten)."""


def extract_product_id(url: str) -> Optional[str]:
    """Produkt-ID aus einer AliExpress-URL (verschiedene Formate)."""
    if not url:
        return None
    for pat in _ID_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def _concat(params: dict) -> str:
    items = {k: v for k, v in params.items() if k != "sign" and v is not None}
    return "".join(f"{k}{items[k]}" for k in sorted(items))


def sign(params: dict, secret: str, method: str = "sha256") -> str:
    """Signatur ueber die Request-Parameter (siehe Modul-Doku)."""
    base = _concat(params)
    if method == "md5":
        raw = f"{secret}{base}{secret}".encode("utf-8")
        return hashlib.md5(raw).hexdigest().upper()
    return hmac.new(secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest().upper()


def default_timestamp(sign_method: str) -> str:
    """md5 -> 'YYYY-MM-DD HH:mm:ss' (GMT+8); sha256 -> Epoch-Millisekunden."""
    if sign_method == "md5":
        return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    return str(int(time.time() * 1000))


def signed_params(method: str, business: dict | None, *, app_key: str, app_secret: str,
                  sign_method: str = "sha256", timestamp: str | None = None) -> dict:
    """Vollstaendige, signierte Parameterliste fuer einen API-Aufruf."""
    params: dict[str, Any] = {k: v for k, v in (business or {}).items() if v is not None}
    params.update({
        "app_key": app_key,
        "method": method,
        "format": "json",
        "v": "2.0",
        "sign_method": sign_method,
        "timestamp": timestamp or default_timestamp(sign_method),
    })
    params["sign"] = sign(params, app_secret, sign_method)
    return params


def oauth_authorize_url(app_key: str, redirect_uri: str,
                        host: str = "https://api-sg.aliexpress.com") -> str:
    """Consent-URL: Nutzer autorisiert die App -> Redirect mit ?code=... ."""
    from urllib.parse import urlencode
    return f"{host}/oauth/authorize?" + urlencode({
        "response_type": "code",
        "force_auth": "true",
        "client_id": app_key,
        "redirect_uri": redirect_uri,
    })


def save_token_store(path: str, *, access_token: str, refresh_token: str | None,
                     expires_in: Any) -> None:
    """access_token + Ablaufzeit persistieren (ueberlebt Neustarts)."""
    try:
        seconds = int(expires_in) if expires_in not in (None, "") else 86400
    except (ValueError, TypeError):
        seconds = 86400
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": time.time() + seconds,
    }))


def load_token_store(path: str) -> Optional[dict]:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def is_token_error(message: str) -> bool:
    """Heuristik: deutet die Fehlermeldung auf einen abgelaufenen/ungueltigen Token?"""
    m = (message or "").lower()
    if "token" not in m:
        return False
    return any(w in m for w in ("invalid", "expire", "illegal", "401", "auth"))


def parse_token(data: dict) -> dict:
    """Token-Antwort -> {access_token, refresh_token, expires_in} (defensiv)."""
    d = data if isinstance(data, dict) else {}
    # manche Antworten kommen unter '*_response' oder direkt
    for k, v in list(d.items()):
        if k.endswith("_response") and isinstance(v, dict):
            d = v
            break
    return {
        "access_token": _first_present(d, "access_token", "accessToken"),
        "refresh_token": _first_present(d, "refresh_token", "refreshToken"),
        "expires_in": _first_present(d, "expires_in", "expire_time", "expires_time"),
    }


async def call(client, *, base: str, method: str, business: dict | None,
               app_key: str, app_secret: str, sign_method: str = "sha256") -> dict:
    """Signierten POST an das Gateway senden und JSON zurueckgeben (raise bei error_response)."""
    params = signed_params(method, business, app_key=app_key, app_secret=app_secret,
                           sign_method=sign_method)
    resp = await client.post(
        base, data=params,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
    )
    resp.raise_for_status()
    data = resp.json() if resp.content else {}
    if "error_response" in data:
        raise AliExpressApiError(str(data["error_response"]))
    return data


# ----------------------------------------------------------------- Parsing
def _deep_get(obj: Any, *keys: str) -> Any:
    """Verschachtelten Wert holen; jeder Key wird in dicts gesucht (None bei Fehlschlag)."""
    cur = obj
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur


def _first_present(d: dict, *names: str) -> Any:
    for n in names:
        if isinstance(d, dict) and d.get(n) not in (None, ""):
            return d[n]
    return None


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _unwrap_result(data: dict) -> dict:
    """Die '..._response' -> 'result'-Huelle der ds.product.get-Antwort aufloesen."""
    if not isinstance(data, dict):
        return {}
    # bevorzugt den dokumentierten Schluessel, sonst erste *_response-Huelle
    resp = data.get("aliexpress_ds_product_get_response")
    if resp is None:
        for k, v in data.items():
            if k.endswith("_response") and isinstance(v, dict):
                resp = v
                break
    resp = resp or data
    return resp.get("result") or resp.get("data") or resp


def explain_empty_product(pid: str, data: dict) -> str:
    """Diagnostische Meldung, wenn ds.product.get KEINE Produkt-Basisdaten (product_id) lieferte.

    Statt pauschal „nicht (mehr) verfuegbar" (das täuscht Löschung vor, obwohl das Produkt auf
    der Website existieren kann) den ECHTEN Grund zeigen: eine AliExpress-Fehlermeldung ODER
    „nicht im Dropshipping-Katalog / nicht nach Zielland lieferbar" ODER eine gedrosselte Antwort.
    Nennt die vorhandenen Antwort-Felder als Beleg (self-diagnostizierend)."""
    resp = data.get("aliexpress_ds_product_get_response") if isinstance(data, dict) else None
    if resp is None and isinstance(data, dict):
        for k, v in data.items():
            if k.endswith("_response") and isinstance(v, dict):
                resp = v
                break
    resp = resp if isinstance(resp, dict) else {}
    top = data if isinstance(data, dict) else {}
    # 1) Explizite AliExpress-Fehler-/Status-Meldung (Feldnamen variieren stark)
    for src in (resp, top):
        for key in ("rsp_msg", "sub_msg", "msg", "message", "error_message", "error_msg"):
            m = src.get(key)
            if m:
                return f"Produkt {pid}: AliExpress meldet „{str(m)[:180]}“."
    result = resp.get("result") if isinstance(resp.get("result"), dict) else None
    keys = ", ".join(sorted(resp.keys())) if resp else ", ".join(sorted(top.keys()))
    if not result:
        return (f"Produkt {pid} liefert über die Dropshipping-API keine Produktdaten – existiert "
                f"evtl. auf der Website, ist aber nicht im DS-Katalog gelistet oder nicht ins "
                f"Zielland lieferbar (oder gedrosselte Antwort). Antwort-Felder: {keys or 'keine'}.")
    return (f"Produkt {pid}: Antwort ohne Produkt-Basisinfo (ae_item_base_info_dto fehlt). "
            f"Antwort-Felder: {keys or 'keine'}.")


def _sku_list(result: dict) -> list[dict]:
    skus = _deep_get(result, "ae_item_sku_info_dtos", "ae_item_sku_info_d_t_o")
    if isinstance(skus, dict):  # Einzel-SKU kommt manchmal als dict
        return [skus]
    return skus if isinstance(skus, list) else []


def freight_deliverable(data: dict) -> Optional[bool]:
    """Liefert der Haendler ins abgefragte Land? True/False = DEFINITIV, None = keine
    Aussage moeglich.

    Anders als parse_freight zaehlt hier die EXISTENZ von Versandoptionen (auch
    untrackbare/preislose): fuer "liefert ueberhaupt dorthin?" reicht eine Option.
    Lehre aus dem Bestands-Vorfall 15.08.: fehlt das Options-FELD komplett
    (degradierte Antwort), ist das KEIN Nein — dann None (unbekannt)."""
    res = _unwrap_result(data)
    if not isinstance(res, dict):
        return None
    if "delivery_options" not in res:
        return None                     # Feld fehlt komplett -> degradiert, kein Nein
    outer = res["delivery_options"]
    # AliExpress variiert die Huelle (Review-Fund 16.08.): mal {"delivery_option_d_t_o":
    # [...]}, mal die Options-Liste DIREKT. Nur eindeutig leere Container sind ein Nein;
    # jede unerwartete Form ist "unbekannt" (fail-open, nie faelschlich blockieren).
    if isinstance(outer, list):
        opts = outer
    elif isinstance(outer, dict):
        inner = outer.get("delivery_option_d_t_o")
        if inner is None:
            return False if not outer else None   # {} = leer -> Nein; fremde Keys -> unbekannt
        if not isinstance(inner, (list, dict)):
            return None
        opts = inner if isinstance(inner, list) else [inner]
    else:
        return None                     # null/String -> degradiert, keine Aussage
    if not opts:
        return False                    # leere Liste -> liefert NICHT dorthin
    return any(isinstance(o, dict) for o in opts)


def parse_freight(data: dict) -> Optional[dict]:
    """Antwort von ``aliexpress.ds.freight.query`` -> guenstigste TRACKBARE Versandoption
    als ``{fee_eur, free_shipping, carrier, delivery_days, tracking}`` oder ``None``, wenn
    keine Option lieferbar ist.

    Wir waehlen bewusst die guenstigste *trackbare* Option: getrackt wird zwingend gebraucht
    (eBay-Sendungsnummer), und so unterschaetzen wir die Kosten nicht durch eine billige,
    untrackbare Variante, die wir gar nicht buchen wuerden. ``shipping_fee_cent`` ist trotz
    des Namens bereits der EURO-Betrag (z.B. "3.29"); Gratisversand -> 0 €.
    """
    res = _unwrap_result(data)
    opts = _deep_get(res, "delivery_options", "delivery_option_d_t_o")
    if isinstance(opts, dict):
        opts = [opts]
    if not isinstance(opts, list):
        return None
    parsed: list[dict] = []
    for o in opts:
        if not isinstance(o, dict):
            continue
        free = bool(o.get("free_shipping"))
        fee: Optional[float]
        if free:
            fee = 0.0
        else:
            try:
                fee = float(str(o.get("shipping_fee_cent")).replace(",", "."))
            except (TypeError, ValueError):
                fee = None
        if fee is None:
            continue
        parsed.append({
            "fee_eur": round(max(0.0, fee), 2),
            "free_shipping": free,
            "carrier": o.get("company") or o.get("code"),
            "delivery_days": _deep_int(o.get("max_delivery_days")),
            "tracking": bool(o.get("tracking")),
        })
    if not parsed:
        return None
    # Getrackte Optionen bevorzugen (die buchen wir tatsaechlich), dann die guenstigste.
    tracked = [p for p in parsed if p["tracking"]]
    pool = tracked or parsed
    pool.sort(key=lambda x: x["fee_eur"])
    return pool[0]


def _deep_int(value: Any) -> Optional[int]:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


# Spec-Namen/Werte, die reiner Uebersetzungs-/Plattform-Muell sind -> nicht ins Listing.
# Das Marken-Feld des Haendlers ist UNBRAUCHBAR und bleibt draussen: Shop 1103475254 trug
# am 03.08.2026 bei praktisch jedem Artikel pauschal "Markenname: Bandai" ein – auch bei
# einer Michael-Jackson-Puppe und generischen Plueschtieren. Uebernommen wuerde daraus eine
# FALSCHE Markenangabe im eBay-Listing. Die echte Marke/Lizenz steht im Produktnamen
# (BT21, Chiikawa, One Piece ...) und wird von dort uebernommen.
_JUNK_SPEC_NAMES = {
    "hochbetriebenes chemikalienunternehmen", "material festlegen", "choice",
    "semi_choice", "ursprung", "cn", "cn (herkunft)", "modellnummer", "fein oder mode",
    "is_customized", "brand name", "markenname", "marke", "brand",
}
_JUNK_SPEC_VALUES = {"", "keine", "none", "n/a", "yes", "no", "cn", "cn(herkunft)"}


def _extract_specs(result: dict) -> list[dict]:
    """ae_item_properties -> [{name, value}] (Uebersetzungsmuell/Marke gefiltert)."""
    props = _deep_get(result, "ae_item_properties", "ae_item_property")
    if isinstance(props, dict):
        props = [props]
    out = []
    for p in props if isinstance(props, list) else []:
        name = (p.get("attr_name") or "").strip()
        value = (str(p.get("attr_value") or "")).strip()
        if not name or not value:
            continue
        if name.lower() in _JUNK_SPEC_NAMES or value.lower() in _JUNK_SPEC_VALUES:
            continue
        out.append({"name": name, "value": value})
    return out


# MEHRDIMENSIONALE Maße (LxB(xH)) MÜSSEN VOLLSTÄNDIG erhalten bleiben – NIE auf eine Kante kürzen
# (Vorfall 21.07.: '50x70cm' wurde zu '70 cm', die Länge ging verloren -> falsche Maße im Listing).
_DIM_CM_RE = re.compile(r"(\d+(?:[.,]\d+)?(?:\s*[x×*]\s*\d+(?:[.,]\d+)?)+)\s*cm", re.IGNORECASE)  # 50x70cm
_DIM_BARE_RE = re.compile(r"\d+(?:[.,]\d+)?(?:\s*[x×*]\s*\d+(?:[.,]\d+)?)+", re.IGNORECASE)        # 50x70 (ohne cm)
_CM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*cm", re.IGNORECASE)                                     # 45cm (Einzelmaß)


def _fmt_dim(tok: str) -> str:
    """Dimensions-Token vereinheitlichen: '50x70' / '50×70' / '50*70' -> '50 x 70', Komma->Punkt."""
    parts = re.split(r"\s*[x×*]\s*", tok.strip(), flags=re.IGNORECASE)
    return " x ".join(p.replace(",", ".") for p in parts)


def _clean_option_value(value: str) -> str:
    """Varianten-Werte fuer eBay saeubern (z. B. 'Gold Color'->'Gold', '45cm or 17.7 Inches'->'45 cm').

    WICHTIG: mehrdimensionale Maße (Länge×Breite) NIE auf eine Kante reduzieren – '50x70cm' bleibt
    '50 x 70 cm'. Nur ein EINZELnes cm-Maß mit optionaler Zoll-Umrechnung dahinter wird gekürzt."""
    v = (value or "").strip()
    m = _DIM_CM_RE.search(v)                          # 1) LxB mit cm -> ganzes Maß behalten
    if m:
        return f"{_fmt_dim(m.group(1))} cm"
    m = _DIM_BARE_RE.search(v)                        # 2) LxB ohne Einheit -> beide Zahlen behalten
    if m:
        return _fmt_dim(m.group(0))
    m = _CM_RE.search(v)                              # 3) Einzelmaß (45cm or 17.7 Inches -> '45 cm')
    if m:
        return f"{m.group(1).replace(',', '.')} cm"
    v = re.sub(r"\s*colou?r$", "", v, flags=re.IGNORECASE).strip()  # 'Gold Color' -> 'Gold'
    return v or (value or "").strip()


# "Versand aus"/"Ships From" — sprachunabhaengige SKU-Property-Id (Live-Test 08/2026,
# Diagnose-Endpunkt source-info). Der Lagerort ist KEINE Kauf-Option: er darf nie als
# eBay-Variante erscheinen und nie germanisiert werden. Er wird separat als "ship_from"
# am SKU-Eintrag gefuehrt; im rohen sku_attr bleibt die Komponente fuer die Bestellung.
SHIPS_FROM_PROP_ID = "200007763"

# EU-Lager (deutsch + englisch, lower-case) fuer den Lokal-Filter und die Dedup-Praeferenz.
_EU_WAREHOUSES = {
    "deutschland", "germany", "polen", "poland", "frankreich", "france",
    "spanien", "spain", "tschechien", "czechia", "czech republic", "czech",
    "belgien", "belgium", "italien", "italy", "niederlande", "netherlands",
    "oesterreich", "österreich", "austria", "ungarn", "hungary",
    "slowakei", "slovakia",
}


def has_eu_warehouse(ships_from) -> bool:
    """True, wenn mindestens ein "Versand aus"-Wert ein EU-Lager ist."""
    return any(str(v).strip().lower() in _EU_WAREHOUSES for v in (ships_from or []))


def _sku_ship_from(sku: dict) -> str | None:
    """"Versand aus"-Wert dieser SKU (Property-Id 200007763); None = keine Angabe."""
    dtos = _deep_get(sku, "ae_sku_property_dtos", "ae_sku_property_d_t_o")
    if isinstance(dtos, dict):
        dtos = [dtos]
    for d in dtos if isinstance(dtos, list) else []:
        if str(d.get("sku_property_id") or "") == SHIPS_FROM_PROP_ID:
            v = (d.get("property_value_definition_name")
                 or d.get("sku_property_value") or "").strip()
            return v or None
    return None


def extract_ships_from(result: dict) -> list[str]:
    """Alle "Versand aus"-Werte einer ds.product.get-Antwort (dedupliziert, Reihenfolge)."""
    out: list[str] = []
    for sku in _sku_list(result):
        v = _sku_ship_from(sku)
        if v and v not in out:
            out.append(v)
    return out


def _sku_options(sku: dict) -> list[tuple[str, str]]:
    """SKU -> [(Achsenname, Optionswert)] aus den ae_sku_property_dtos (Werte gesaeubert)."""
    dtos = _deep_get(sku, "ae_sku_property_dtos", "ae_sku_property_d_t_o")
    if isinstance(dtos, dict):
        dtos = [dtos]
    opts = []
    for d in dtos if isinstance(dtos, list) else []:
        if str(d.get("sku_property_id") or "") == SHIPS_FROM_PROP_ID:
            continue   # Lagerort ist keine Kauf-Option -> separat als ship_from (s. oben)
        name = (d.get("sku_property_name") or "").strip()
        value = (d.get("property_value_definition_name") or d.get("sku_property_value") or "").strip()
        if name and value:
            opts.append((name, _clean_option_value(value)))
    return opts


def _sku_image(sku: dict) -> str | None:
    """Variantenspezifisches Bild (z. B. je Farbe) aus den ae_sku_property_dtos."""
    dtos = _deep_get(sku, "ae_sku_property_dtos", "ae_sku_property_d_t_o")
    if isinstance(dtos, dict):
        dtos = [dtos]
    for d in dtos if isinstance(dtos, list) else []:
        img = (d.get("sku_image") or "").strip()
        if img:
            return img
    return None


def parse_product(data: dict) -> dict:
    """ds.product.get-Antwort -> normiertes Produkt-Dict (Felder wie ScrapedProduct).

    Defensiv: AliExpress variiert Feldnamen/Verschachtelung – fehlende Felder ergeben
    sinnvolle Defaults statt Fehler.
    """
    result = _unwrap_result(data)
    base = _deep_get(result, "ae_item_base_info_dto") or {}

    product_id = str(_first_present(base, "product_id", "productId") or
                     _first_present(result, "product_id", "productId") or "")
    title = _first_present(base, "subject", "title") or ""
    description = _first_present(base, "detail", "mobile_detail", "description") or ""
    category_id = _first_present(base, "category_id", "categoryId")

    # Bilder: ";"-getrennte Liste im Multimedia-DTO (mit Fallbacks)
    image_str = (_deep_get(result, "ae_multimedia_info_dto", "image_urls")
                 or _first_present(base, "image_u_r_ls", "image_urls") or "")
    images = [u for u in re.split(r"[;,]", image_str) if u.strip()] if image_str else []

    # SKUs -> Preis (Minimum der Verkaufspreise), Bestand, Varianten
    skus = _sku_list(result)
    variant_entries = []
    axes: dict[str, list[str]] = {}   # Achsenname -> geordnete Optionsliste
    for sku in skus:
        p = _to_decimal(_first_present(sku, "offer_sale_price", "sku_price",
                                       "offer_bulk_sale_price", "sku_bulk_order"))
        stock = _first_present(sku, "sku_available_stock", "sku_stock", "available_quantity")
        opts = _sku_options(sku)
        for name, value in opts:
            axis = axes.setdefault(name, [])
            if value not in axis:
                axis.append(value)
        variant_entries.append({
            "id": _first_present(sku, "sku_id", "id"),
            "attr": _first_present(sku, "sku_attr", "id", "sku_code"),
            "price": str(p) if p is not None else None,
            "stock": stock,
            "options": dict(opts),   # {Achse: Wert} dieser Kombination
            "image": _sku_image(sku),  # variantenspezifisches Bild (falls vorhanden)
            # Lagerort (Property 200007763) — bewusst KEINE Kauf-Option: bleibt aus
            # axes/options draussen, damit er nie als eBay-Variante publiziert oder
            # germanisiert wird. Der rohe attr behaelt die Komponente -> Bestellung
            # waehlt weiterhin das richtige Lager.
            "ship_from": _sku_ship_from(sku),
        })

    # SKUs, die sich NUR im Lagerort unterscheiden (identische Kauf-Optionen), auf EINE
    # reduzieren — mit EU-Praeferenz. Ohne das entschiede die Listenreihenfolge (oft
    # CHINA zuerst) darueber, welcher attr beim Bestellen/Publizieren verwendet wird.
    # NUR wenn das Produkt ueberhaupt eine "Versand aus"-Angabe hat: SKUs ohne jede
    # Lager-Info duerfen nicht kollabieren (sie unterscheiden sich in etwas anderem).
    if any(v.get("ship_from") for v in variant_entries):
        picked: dict[tuple, dict] = {}
        order: list[tuple] = []
        for v in variant_entries:
            key = tuple(sorted(v["options"].items()))
            prev = picked.get(key)
            if prev is None:
                picked[key] = v
                order.append(key)
            elif (has_eu_warehouse([v.get("ship_from")])
                  and not has_eu_warehouse([prev.get("ship_from")])):
                picked[key] = v
        variant_entries = [picked[k] for k in order]

    # Preis/Bestand NACH der Dedup rechnen: sonst flösse z. B. der günstigere
    # China-Preis in die Kalkulation, obwohl bestellt würde, was die EU-SKU kostet.
    prices = [_to_decimal(v["price"]) for v in variant_entries if v["price"] is not None]
    prices = [p for p in prices if p is not None]
    total_stock = 0
    stock_reported = False   # hat AliExpress ueberhaupt IRGENDEINEN Bestandswert geliefert?
    for v in variant_entries:
        stock = v["stock"]
        # Robust summieren: AliExpress liefert Bestand mal als int, mal als String
        # ('100', '1,000'). Naives int('1,000') wuerfe -> total_stock zu niedrig ->
        # das ganze Produkt faelschlich als ausverkauft (in_stock=False) markiert.
        if stock is not None:
            sv = str(stock).strip().replace(",", "").replace(" ", "")
            try:
                total_stock += int(float(sv)) if sv else 0
                if sv:
                    stock_reported = True
            except (ValueError, TypeError):
                pass

    price = min(prices) if prices else _to_decimal(
        _first_present(base, "sale_price", "min_sale_price", "original_price"))
    # Ausverkauft NUR bei einer ECHT gemeldeten 0 — laesst AliExpress die Bestands-
    # felder komplett weg (kommt vor; Vorfall 15.08.: zwei aktive "onSelling"-Produkte
    # wurden faelschlich genullt und blieben es), entscheidet der Produkt-Status.
    # Gleiche Doktrin wie variant_stock_state in golive_service: fehlende Daten
    # duerfen nie wie "Bestand 0" wirken (Umsatzschutz).
    status = str(_first_present(base, "product_status_type", "productStatusType") or "").strip()
    if skus and stock_reported:
        in_stock = total_stock > 0
    else:
        # FAIL-OPEN: nur ein DEFINITIV toter Status (offline/geloescht) zaehlt als
        # nicht lieferbar — unbekannte/leere Status duerfen nie wie "Bestand 0"
        # wirken, sonst nullt der Ausverkauft-Schutz lebendige Listings.
        in_stock = status.lower() not in {"offline", "delete", "deleted"}

    store = _deep_get(result, "ae_store_info") or {}
    supplier_id = str(_first_present(store, "store_id", "storeId") or
                      _first_present(base, "store_id") or "")
    rating = _to_decimal(_first_present(store, "store_evaluate_rate", "shop_rate")) or None

    return {
        "aliexpress_id": product_id,
        "title_raw": title,
        "description_raw": description,
        "price_cny": price if price is not None else Decimal("0"),
        "images": images,
        "variants": {"skus": variant_entries, "axes": axes} if variant_entries else {},
        "specs": _extract_specs(result),
        "supplier_id": supplier_id,
        "supplier_rating": rating if rating is not None else Decimal("0"),
        "in_stock": in_stock,
        # Positiver Bestands-Beweis vorhanden? Steuert im Monitoring, ob Mengen
        # ERHOEHT werden duerfen (fail-open schuetzt nur vor falschem Nullen).
        "stock_reported": stock_reported,
        "category_id": str(category_id) if category_id is not None else None,
    }


def parse_tracking(data: dict) -> dict:
    """Tracking-Antwort -> {tracking_number, carrier, status} (defensiv).

    Echte ds.order.tracking.get-Struktur (live verifiziert 07/2026):
    result.data.tracking_detail_line_list.tracking_detail[0].mail_no
    (+ carrier_name; Status = tracking_name des NEUESTEN detail_node).
    """
    resp = data
    for k, v in (data.items() if isinstance(data, dict) else []):
        if k.endswith("_response") and isinstance(v, dict):
            resp = v
            break
    result = resp.get("result") if isinstance(resp, dict) else None
    detail = result if isinstance(result, dict) else (resp if isinstance(resp, dict) else {})

    # Verschachtelte Detail-Zeile aufloesen (Einzel-Objekt ODER Liste)
    inner = _deep_get(detail, "data", "tracking_detail_line_list", "tracking_detail")
    if isinstance(inner, list):
        inner = inner[0] if inner else None
    line = inner if isinstance(inner, dict) else {}

    number = (_first_present(line, "mail_no", "tracking_number", "logistics_no")
              or _first_present(detail, "mail_no", "tracking_number", "logistics_no"))
    carrier = (_first_present(line, "carrier_name", "logistics_service_name")
               or _first_present(detail, "logistics_service_name", "carrier", "service_name"))
    # Neuester Statusknoten (Liste ist zeitlich absteigend sortiert)
    nodes = _deep_get(line, "detail_node_list", "detail_node")
    if isinstance(nodes, dict):
        nodes = [nodes]
    latest = (nodes or [{}])[0] if isinstance(nodes, list) else {}
    status = (_first_present(latest, "tracking_name", "tracking_detail_desc")
              or _first_present(detail, "official_status", "status", "tracking_status")
              or "unknown")
    return {"tracking_number": number, "carrier": carrier, "status": status}
