"""Laender-Ausschluss je Listing (Nutzerauftrag 16.08.: "passt so bau es").

Abgestimmter Umfang — nur die STABILEN Faelle: EU-Lager-Quellen liefern dauerhaft
nicht in bestimmte Nicht-EU-Laender (CH/GB), waehrend die Versand-Policy des
Listings diese Laender anbietet (Worldwide-/Europe-Include). Dynamische Faelle
(z. B. Estland-Folie, Sale 1204) deckt der Bestell-Liefer-Check ab.

- ``scan``: READ-ONLY. Findet aktive Listings mit EU-Lager-Quelle, deren Policy
  ein Pruef-Land anbietet, fuer das die Quelle KEINEN positiven Liefer-Beweis hat.
  Ergebnis = Vorschlagsliste (Zustandsdatei) — es wird NICHTS an eBay geaendert.
- ``apply``: NUR auf ausdrueckliche Freigabe je Listing-Liste. Bestehende Policies
  werden NIE geaendert (Vorfall-Regel: sie wirken auf alle Listings) — je
  Ausschluss-Muster wird eine Policy-KOPIE mit erweitertem regionExcluded angelegt
  (einmalig, danach wiederverwendet) und dem Offer des Listings zugewiesen —
  gleicher Pfad wie fast_shipping_service.apply_fast_policy_to_listing.

Sicherheits-Doktrin (mit Nutzer abgestimmt): fuers LISTING zaehlt der POSITIVE
Liefer-Beweis — kein Beweis (False ODER unbekannt) => Land ausschliessen. Zu viel
Ausschluss kostet nur einen potenziellen Auslandskaeufer; zu wenig einen
geplatzten Kauf. (Der Bestell-Preflight arbeitet bewusst umgekehrt: fail-open.)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Listing
from app.retry import PersistentError

logger = logging.getLogger("app.services.listing_country_service")

# Nicht-EU-Laender, die Worldwide-/Europe-Policies anbieten, EU-Lager-Quellen aber
# oft dauerhaft nicht beliefern (Proben 16.08.: Autowaschmop CH/GB ohne Beweis).
PRUEF_LAENDER = ("CH", "GB")

_STATE_FILE = "./data/country_exclusion_state.json"

# eBay-Regionen, die die Pruef-Laender ENTHALTEN (EuropeanUnion enthaelt CH/GB NICHT).
_BREITE_REGIONEN = {"Worldwide", "Europe"}


def _state_load() -> dict:
    import json
    from pathlib import Path
    try:
        d = json.loads(Path(_STATE_FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 – fehlt/kaputt = leerer Zustand
        return {}


def _state_save(state: dict) -> None:
    import json
    from pathlib import Path
    p = Path(_STATE_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def laender_im_angebot(policy_raw: dict) -> set[str]:
    """Welche PRUEF_LAENDER bietet diese Versand-Policy tatsaechlich an?

    Ein Land gilt als angeboten, wenn es EXPLIZIT included ist ODER eine breite
    Region (Worldwide/Europe) included ist und das Land nicht explizit excluded."""
    stl = (policy_raw or {}).get("shipToLocations") or {}
    inc = {str(r.get("regionName") or "") for r in (stl.get("regionIncluded") or [])}
    exc = {str(r.get("regionName") or "") for r in (stl.get("regionExcluded") or [])}
    breit = bool(_BREITE_REGIONEN & inc)
    return {land for land in PRUEF_LAENDER
            if (land in inc) or (breit and land not in exc)}


async def scan(db: Session, *, ebay, ae, limit: int | None = None) -> dict:
    """Read-only Bestands-Scan -> Vorschlagsliste in der Zustandsdatei.

    Kandidaten: aktive Listings, deren Quelle nachweislich aus einem EU-Lager
    versendet (variants_have_eu_warehouse) — genau dort ist "liefert nicht nach
    CH/GB" ein DAUERHAFTER Zustand. Je Kandidat: Policy des Offers lesen, nur
    tatsaechlich angebotene Pruef-Laender gegen die Quelle proben.
    """
    from app.services.fast_shipping_service import variants_have_eu_warehouse

    # Policy-Landkarte einmalig laden (id -> {name, angebotene Prueflaender}).
    policy_map: dict[str, dict] = {}
    for p in await ebay.list_fulfillment_policies():
        pid = str(p.get("policy_id") or "")
        if not pid:
            continue
        raw = await ebay._get_json(f"{ebay._account}/fulfillment_policy/{pid}")  # noqa: SLF001
        policy_map[pid] = {"name": (raw or {}).get("name") or p.get("name") or "",
                           "raw": raw or {},
                           "angeboten": laender_im_angebot(raw or {})}

    listings = db.scalars(select(Listing).where(
        Listing.listing_status == "active",
        Listing.product_id.isnot(None))).all()
    vorschlaege: dict[str, dict] = {}
    geprueft = 0
    uebersprungen_policy_sicher = 0
    klassik_ohne_offer = 0
    for lst in listings:
        if limit and geprueft >= limit:
            break
        product = getattr(lst, "product", None)
        if product is None or not product.aliexpress_id:
            continue
        if not variants_have_eu_warehouse(product):
            continue                     # China-Quelle: dynamisch -> Bestell-Check
        offer = None
        try:
            offer = await ebay._first_offer_for_sku(lst.ebay_sku)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001 – einzelner Ausfall stoppt den Scan nicht
            logger.info("scan: offer-lookup fehlgeschlagen (listing %s): %s",
                        lst.id, str(exc)[:100])
        if not offer:
            klassik_ohne_offer += 1
            continue                     # Klassik-Listing: manueller Weg (siehe Doku)
        pid = str(((offer.get("listingPolicies") or {}).get("fulfillmentPolicyId")) or "")
        angeboten = (policy_map.get(pid) or {}).get("angeboten") or set()
        if not angeboten:
            uebersprungen_policy_sicher += 1
            continue                     # Policy bietet CH/GB gar nicht an -> sicher
        geprueft += 1
        ohne_beweis = []
        for land in sorted(angeboten):
            try:
                beweis = await ae.delivery_availability(
                    product_id=str(product.aliexpress_id), country=land)
            except Exception:  # noqa: BLE001
                beweis = None
            if beweis is not True:        # kein POSITIVER Beweis -> ausschliessen
                ohne_beweis.append(land)
        if ohne_beweis:
            vorschlaege[str(lst.id)] = {
                "titel": (lst.title_seo or "")[:80],
                "policy_id": pid,
                "policy_name": (policy_map.get(pid) or {}).get("name") or "",
                "laender": ohne_beweis,
                "status": "offen",
            }

    state = _state_load()
    # Bereits umgestellte Eintraege behalten (Historie); offene durch den frischen
    # Scan ersetzen.
    alte = {k: v for k, v in (state.get("vorschlaege") or {}).items()
            if (v or {}).get("status") == "umgestellt"}
    alte.update(vorschlaege)
    state["vorschlaege"] = alte
    state["scanned_at"] = datetime.now(timezone.utc).isoformat()
    _state_save(state)
    return {"kandidaten_geprueft": geprueft, "vorschlaege": len(vorschlaege),
            "policy_sicher": uebersprungen_policy_sicher,
            "klassik_ohne_offer": klassik_ohne_offer,
            "liste": [{"listing_id": int(k), **v} for k, v in vorschlaege.items()]}


async def _variante_finden_oder_anlegen(ebay, state: dict, *, policy_id: str,
                                        laender: list[str]) -> str:
    """Policy-Variante "<Original> ohne CH+GB" wiederverwenden oder NEU anlegen.

    Das Original bleibt unangetastet (Vorfall-Regel). Der Cache in der Zustands-
    datei verhindert doppelte Varianten je (Original, Laender-Muster)."""
    key = f"{policy_id}|{'+'.join(sorted(laender))}"
    varianten = state.setdefault("varianten", {})
    if varianten.get(key):
        return varianten[key]
    raw = await ebay._get_json(f"{ebay._account}/fulfillment_policy/{policy_id}")  # noqa: SLF001
    if not raw:
        raise PersistentError(f"Versand-Policy {policy_id} nicht lesbar")
    body = {k: v for k, v in raw.items() if k != "fulfillmentPolicyId"}
    suffix = f" ohne {'+'.join(sorted(laender))}"
    body["name"] = ((raw.get("name") or "Versand")[: 64 - len(suffix)] + suffix)
    stl = dict(body.get("shipToLocations") or {})
    exc = list(stl.get("regionExcluded") or [])
    vorhanden = {str(r.get("regionName") or "") for r in exc}
    for land in sorted(laender):
        if land not in vorhanden:
            exc.append({"regionName": land})
    stl["regionExcluded"] = exc
    body["shipToLocations"] = stl
    neue_id = await ebay.create_fulfillment_policy(body)
    varianten[key] = neue_id
    _state_save(state)
    logger.info("Policy-Variante angelegt: %s -> %s (%s)", policy_id, neue_id, key)
    return neue_id


async def apply(db: Session, *, ebay, listing_ids: list[int]) -> dict:
    """Umstellung NUR fuer die ausdruecklich freigegebenen Listings.

    Je Listing: Variante der aktuellen Policy (mit Laender-Ausschluss) zuweisen —
    alle Offers des Listings (Varianten-SKUs), gleicher Pfad wie fast_shipping."""
    from app.services.golive_service import _listing_variant_skus

    state = _state_load()
    vorschlaege = state.get("vorschlaege") or {}
    ergebnisse = []
    for lid in listing_ids:
        v = vorschlaege.get(str(lid))
        if not v or v.get("status") != "offen":
            ergebnisse.append({"listing_id": lid, "ok": False,
                               "note": "kein offener Vorschlag (erst landscan)"})
            continue
        lst = db.get(Listing, lid)
        if lst is None:
            ergebnisse.append({"listing_id": lid, "ok": False, "note": "Listing fehlt"})
            continue
        try:
            variante = await _variante_finden_oder_anlegen(
                ebay, state, policy_id=v["policy_id"], laender=v["laender"])
            skus = await _listing_variant_skus(ebay, lst)
            if not skus and lst.ebay_sku:
                skus = [lst.ebay_sku]
            updated = 0
            for sku in skus:
                offer = await ebay._first_offer_for_sku(sku)  # noqa: SLF001
                if not offer or not offer.get("offerId"):
                    continue
                if ((offer.get("listingPolicies") or {}).get("fulfillmentPolicyId")) == variante:
                    updated += 1
                    continue
                body = ebay._sanitize_offer(offer)  # noqa: SLF001
                pol = dict(body.get("listingPolicies") or {})
                pol["fulfillmentPolicyId"] = variante
                body["listingPolicies"] = pol
                await ebay.update_offer(offer["offerId"], body)
                updated += 1
            if not updated:
                ergebnisse.append({"listing_id": lid, "ok": False,
                                   "note": "keine Inventory-Offers gefunden"})
                continue
            v["status"] = "umgestellt"
            v["variante"] = variante
            v["applied_at"] = datetime.now(timezone.utc).isoformat()
            _state_save(state)
            ergebnisse.append({"listing_id": lid, "ok": True,
                               "laender": v["laender"], "offers": updated,
                               "policy_variante": variante})
        except Exception as exc:  # noqa: BLE001 – Einzel-Fehler zeigen, weiterlaufen
            ergebnisse.append({"listing_id": lid, "ok": False,
                               "note": str(exc)[:160]})
    return {"ergebnisse": ergebnisse,
            "umgestellt": sum(1 for e in ergebnisse if e.get("ok"))}


# ------------------------------------------------------------------ Nur-DE
# Nutzerauftrag 19.08. (Temu-Powerbank, Listing 248): einzelne Listings, deren
# Quelle NUR nach Deutschland liefert, komplett auf Inlandsversand begrenzen.

_NUR_DE_SUFFIX = " nur DE"
_POLICY_NAME_MAX = 64
# Ausschluesse, die auch bei DE-only sinnvoll bleiben (Live-Policy 248 nutzt sie).
_INLAND_AUSSCHLUESSE = {"Packstation", "PO Box", "APO/FPO"}


def nur_de_body(raw: dict) -> dict:
    """Baut aus einer bestehenden Versand-Policy die Kopie "<Name> nur DE".

    Nur-DE heisst: alle INTERNATIONAL-Versandoptionen entfallen, shipToLocations
    wird auf DE begrenzt, Global Shipping aus. Das Original-Dict bleibt
    unveraendert (Vorfall-Regel: bestehende Policies wirken auf ALLE Listings)."""
    inland = [o for o in (raw.get("shippingOptions") or [])
              if str(o.get("optionType") or "").upper() != "INTERNATIONAL"]
    if not inland:
        raise PersistentError(
            "Policy hat keine Inlands-Versandoption — 'nur DE' nicht moeglich. "
            "Du kannst das im eBay-Verkaeuferkonto unter Versandeinstellungen "
            "selbst beheben (eine Inlandsversandart anlegen).")
    body = {k: v for k, v in raw.items() if k != "fulfillmentPolicyId"}
    name = raw.get("name") or "Versand"
    body["name"] = name[: _POLICY_NAME_MAX - len(_NUR_DE_SUFFIX)] + _NUR_DE_SUFFIX
    body["shippingOptions"] = inland
    stl = {"regionIncluded": [{"regionName": "DE"}]}
    # Sonder-Ausschluesse der Original-Policy (Packstation/Postfach) gelten auch
    # innerhalb DE weiter — Laender-/Kontinent-Ausschluesse sind durch DE-only obsolet.
    sonder = [r for r in ((raw.get("shipToLocations") or {}).get("regionExcluded") or [])
              if str(r.get("regionName") or "") in _INLAND_AUSSCHLUESSE]
    if sonder:
        stl["regionExcluded"] = sonder
    body["shipToLocations"] = stl
    if "globalShipping" in body:
        body["globalShipping"] = False
    return body


async def nur_deutschland(db: Session, *, ebay, listing_id: int,
                          go: bool = False) -> dict:
    """Versand EINES ausdruecklich benannten Listings auf "nur Deutschland" begrenzen.

    go=False: Read-only-Vorschau (aktuelle Policy, geplanter Varianten-Name) —
    KEINE Schreib-Calls. go=True: Policy-Variante anlegen/wiederverwenden und
    alle Offers des Listings umstellen — gleicher Pfad wie ``apply``."""
    from app.services.golive_service import _listing_variant_skus

    lst = db.get(Listing, listing_id)
    if lst is None:
        raise PersistentError(f"Listing {listing_id} nicht gefunden")
    skus = await _listing_variant_skus(ebay, lst)
    if not skus and lst.ebay_sku:
        skus = [lst.ebay_sku]
    offers = []
    for sku in skus:
        offer = await ebay._first_offer_for_sku(sku)  # noqa: SLF001
        if offer and offer.get("offerId"):
            offers.append((sku, offer))
    weg = "inventory" if offers else "klassik"
    if offers:
        pid = str(((offers[0][1].get("listingPolicies") or {})
                   .get("fulfillmentPolicyId")) or "")
    else:
        # Klassik-Listing (Trading-API, z. B. Powerbank 248): kein Offer, aber
        # die Versand-Policy haengt als SellerShippingProfile am Item.
        if not lst.ebay_item_id:
            return {"listing_id": listing_id, "ok": False,
                    "note": "Kein Inventory-Offer und keine eBay-Item-ID "
                            "(Klassik-Listing unvollstaendig erfasst) — Versand "
                            "nur manuell im eBay-Verkaeuferkonto umstellbar"}
        profil = await ebay.get_item_shipping_profile(str(lst.ebay_item_id))
        pid = str(profil.get("profile_id") or "")
        if not pid:
            return {"listing_id": listing_id, "ok": False,
                    "note": "Klassik-Listing ohne Versand-Policy (Business "
                            "Policy) — Versand nur manuell im "
                            "eBay-Verkaeuferkonto umstellbar"}
    raw = await ebay._get_json(f"{ebay._account}/fulfillment_policy/{pid}")  # noqa: SLF001
    if not raw:
        raise PersistentError(f"Versand-Policy {pid} nicht lesbar")
    if str(raw.get("name") or "").endswith(_NUR_DE_SUFFIX):
        # Idempotenz: erneuter Lauf darf keine "nur DE nur DE"-Variante anlegen.
        return {"listing_id": listing_id, "ok": True, "bereits_nur_de": True,
                "policy_id": pid, "policy_name": raw.get("name"),
                "note": "Listing steht bereits auf 'nur DE' — nichts zu tun"}
    plan = nur_de_body(raw)
    if not go:
        intl = sum(1 for o in (raw.get("shippingOptions") or [])
                   if str(o.get("optionType") or "").upper() == "INTERNATIONAL")
        return {"listing_id": listing_id, "ok": True, "vorschau": True,
                "weg": weg, "titel": (lst.title_seo or "")[:70],
                "offers": [s for s, _ in offers],
                "item_id": (str(lst.ebay_item_id) if weg == "klassik" else None),
                "policy_id": pid, "policy_name": raw.get("name"),
                "ship_to": raw.get("shipToLocations"),
                "intl_optionen": intl,
                "variante_name": plan["name"]}
    state = _state_load()
    varianten = state.setdefault("varianten", {})
    key = f"{pid}|nurDE"
    variante = varianten.get(key)
    if not variante:
        variante = await ebay.create_fulfillment_policy(plan)
        varianten[key] = variante
        _state_save(state)
        logger.info("Nur-DE-Policy-Variante angelegt: %s -> %s", pid, variante)
    updated = 0
    if offers:
        for _sku, offer in offers:
            if ((offer.get("listingPolicies") or {})
                    .get("fulfillmentPolicyId")) == variante:
                updated += 1
                continue
            body = ebay._sanitize_offer(offer)  # noqa: SLF001
            pol = dict(body.get("listingPolicies") or {})
            pol["fulfillmentPolicyId"] = variante
            body["listingPolicies"] = pol
            await ebay.update_offer(offer["offerId"], body)
            updated += 1
    else:
        await ebay.revise_item_shipping_profile(str(lst.ebay_item_id), variante)
        updated = 1
    nurde = state.setdefault("nur_de", {})
    nurde[str(listing_id)] = {"titel": (lst.title_seo or "")[:80], "weg": weg,
                              "policy_original": pid, "variante": variante,
                              "applied_at": datetime.now(timezone.utc).isoformat()}
    _state_save(state)
    return {"listing_id": listing_id, "ok": True, "vorschau": False,
            "offers": updated, "policy_variante": variante}
