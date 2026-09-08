"""Laender-Ausschluss je Listing (16.08.): Angebots-Logik, Scan-Entscheidung,
Policy-Varianten-Cache und die Umstellung nur freigegebener Listings."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.models import Listing, Product
from app.services import listing_country_service as lcs


def _policy(name, include, exclude=()):
    return {"name": name,
            "shipToLocations": {
                "regionIncluded": [{"regionName": r} for r in include],
                "regionExcluded": [{"regionName": r} for r in exclude]}}


# ---------------------------------------------------------------- Angebots-Logik

def test_eu_only_policy_bietet_ch_gb_nicht_an():
    raw = _policy("EU", ["EuropeanUnion"])
    assert lcs.laender_im_angebot(raw) == set()


def test_worldwide_policy_bietet_ch_gb_an():
    raw = _policy("Welt", ["Worldwide"], ["Africa", "Asia"])
    assert lcs.laender_im_angebot(raw) == {"CH", "GB"}


def test_worldwide_mit_ch_ausschluss_bietet_nur_gb():
    raw = _policy("Welt-ohne-CH", ["Worldwide"], ["CH"])
    assert lcs.laender_im_angebot(raw) == {"GB"}


def test_explizites_include_zaehlt_auch_ohne_breite_region():
    raw = _policy("Nur-CH", ["EuropeanUnion", "CH"])
    assert lcs.laender_im_angebot(raw) == {"CH"}


# ---------------------------------------------------------------- Stubs

class _StubEbay:
    _account = "https://api.test/sell/account/v1"

    def __init__(self, policies_raw, offers_by_sku, *, profile=None):
        self.policies_raw = policies_raw   # pid -> raw policy dict
        self.offers = offers_by_sku        # sku -> offer dict
        self.profile = profile             # Klassik: GetItem-SellerShippingProfile
        self.created: list[dict] = []
        self.updated: list[tuple] = []
        self.revised: list[tuple] = []     # Klassik: (item_id, profile_id)

    async def list_fulfillment_policies(self):
        return [{"policy_id": pid, "name": raw.get("name")}
                for pid, raw in self.policies_raw.items()]

    async def _get_json(self, url, **kw):
        return self.policies_raw.get(url.rsplit("/", 1)[-1])

    async def _first_offer_for_sku(self, sku):
        return self.offers.get(sku)

    def _sanitize_offer(self, offer):
        return dict(offer)

    async def update_offer(self, offer_id, body):
        self.updated.append((offer_id, body))

    async def create_fulfillment_policy(self, body):
        self.created.append(body)
        return f"NEU-{len(self.created)}"

    async def get_item_shipping_profile(self, item_id):
        return self.profile or {"profile_id": None, "profile_name": None}

    async def revise_item_shipping_profile(self, item_id, profile_id):
        self.revised.append((item_id, profile_id))


class _StubAe:
    def __init__(self, antworten):
        self.antworten = antworten

    async def delivery_availability(self, *, product_id, country, sku_id=None):
        return self.antworten.get(country)


@pytest.fixture()
def state_file(tmp_path, monkeypatch):
    f = tmp_path / "state.json"
    monkeypatch.setattr(lcs, "_STATE_FILE", str(f))
    return f


def _listing(db, *, sku="AE-LC", ship_from="Deutschland", ali="lc900"):
    p = Product(aliexpress_url=f"https://de.aliexpress.com/item/{ali}.html",
                aliexpress_id=ali,
                variants={"axes": {"Farbe": ["Rot", "Blau"]},
                          "skus": [{"attr": "a1", "options": {"Farbe": "Rot"},
                                    "ship_from": ship_from},
                                   {"attr": "a2", "options": {"Farbe": "Blau"},
                                    "ship_from": ship_from}]})
    db.add(p)
    db.flush()
    l = Listing(product_id=p.id, ebay_sku=sku, title_seo="EU-Lager-Artikel",
                description="d", listing_status="active")
    db.add(l)
    db.commit()
    return l


# ---------------------------------------------------------------- Scan

def test_scan_schlaegt_ch_gb_ohne_beweis_vor(db, state_file):
    l = _listing(db)
    ebay = _StubEbay(
        {"P1": _policy("Bearbeitung 2 tage", ["Worldwide"], ["Africa"])},
        {l.ebay_sku: {"offerId": "OF1",
                      "listingPolicies": {"fulfillmentPolicyId": "P1"}}})
    ae = _StubAe({"CH": None, "GB": False})     # kein positiver Beweis
    r = asyncio.run(lcs.scan(db, ebay=ebay, ae=ae))
    assert r["vorschlaege"] == 1
    assert r["liste"][0]["laender"] == ["CH", "GB"]
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["vorschlaege"][str(l.id)]["status"] == "offen"


def test_scan_ueberspringt_eu_only_policy_und_china_quelle(db, state_file):
    l_eu = _listing(db, sku="AE-EU", ali="lc901")
    _listing(db, sku="AE-CN", ship_from="China", ali="lc902")
    ebay = _StubEbay(
        {"P2": _policy("Kostenlos 2 Tage", ["EuropeanUnion"])},
        {"AE-EU": {"offerId": "OF2",
                   "listingPolicies": {"fulfillmentPolicyId": "P2"}}})
    ae = _StubAe({})
    r = asyncio.run(lcs.scan(db, ebay=ebay, ae=ae))
    assert r["vorschlaege"] == 0
    assert r["policy_sicher"] == 1              # EU-only: strukturell sicher
    assert l_eu.id is not None                  # (nur Lesbarkeit)


def test_scan_mit_beweis_schlaegt_nichts_vor(db, state_file):
    l = _listing(db, ali="lc903")
    ebay = _StubEbay(
        {"P1": _policy("Welt", ["Worldwide"])},
        {l.ebay_sku: {"offerId": "OF1",
                      "listingPolicies": {"fulfillmentPolicyId": "P1"}}})
    ae = _StubAe({"CH": True, "GB": True})      # liefert nachweislich
    r = asyncio.run(lcs.scan(db, ebay=ebay, ae=ae))
    assert r["vorschlaege"] == 0


# ---------------------------------------------------------------- Variante + Apply

def test_variante_wird_einmal_angelegt_und_gecacht(state_file):
    ebay = _StubEbay({"P1": _policy("Bearbeitung 2 tage", ["Worldwide"], ["Africa"])}, {})
    state = {}
    v1 = asyncio.run(lcs._variante_finden_oder_anlegen(
        ebay, state, policy_id="P1", laender=["CH", "GB"]))
    v2 = asyncio.run(lcs._variante_finden_oder_anlegen(
        ebay, state, policy_id="P1", laender=["GB", "CH"]))
    assert v1 == v2 == "NEU-1" and len(ebay.created) == 1
    body = ebay.created[0]
    assert body["name"].endswith(" ohne CH+GB")
    exc = {r["regionName"] for r in body["shipToLocations"]["regionExcluded"]}
    assert {"CH", "GB", "Africa"} <= exc        # Original-Ausschluesse bleiben


def test_apply_stellt_nur_freigegebene_offene_vorschlaege_um(db, state_file, monkeypatch):
    l = _listing(db, ali="lc904")
    ebay = _StubEbay(
        {"P1": _policy("Welt", ["Worldwide"])},
        {l.ebay_sku: {"offerId": "OF9",
                      "listingPolicies": {"fulfillmentPolicyId": "P1"}}})
    lcs._state_save({"vorschlaege": {str(l.id): {
        "titel": "t", "policy_id": "P1", "policy_name": "Welt",
        "laender": ["CH", "GB"], "status": "offen"}}})

    async def _keine_skus(ebay_, listing_):
        return []
    from app.services import golive_service
    monkeypatch.setattr(golive_service, "_listing_variant_skus", _keine_skus)

    r = asyncio.run(lcs.apply(db, ebay=ebay, listing_ids=[l.id, 999999]))

    assert r["umgestellt"] == 1
    assert len(ebay.updated) == 1
    oid, body = ebay.updated[0]
    assert oid == "OF9"
    assert body["listingPolicies"]["fulfillmentPolicyId"] == "NEU-1"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["vorschlaege"][str(l.id)]["status"] == "umgestellt"
    # unbekannte ID sauber gemeldet
    assert any(not e["ok"] and e["listing_id"] == 999999 for e in r["ergebnisse"])

    # Zweiter Apply-Lauf: Vorschlag nicht mehr offen -> keine weitere Umstellung
    r2 = asyncio.run(lcs.apply(db, ebay=ebay, listing_ids=[l.id]))
    assert r2["umgestellt"] == 0 and len(ebay.updated) == 1


# ---------------------------------------------------------------- Nur-DE (19.08.)

def _policy_mit_versand(name="Kostenlos Bearbeitung 2 Tage", global_shipping=True):
    return {"fulfillmentPolicyId": "P1", "name": name,
            "marketplaceId": "EBAY_DE",
            "categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES"}],
            "handlingTime": {"unit": "DAY", "value": 2},
            "globalShipping": global_shipping,
            "shippingOptions": [
                {"optionType": "DOMESTIC", "costType": "FLAT_RATE",
                 "shippingServices": [{"shippingServiceCode": "DE_DHLPaket",
                                       "freeShipping": True}]},
                {"optionType": "INTERNATIONAL", "costType": "FLAT_RATE",
                 "shippingServices": [{"shippingServiceCode": "DE_DHLPaketInternational",
                                       "shipToLocations": {"regionIncluded": [
                                           {"regionName": "Worldwide"}]}}]}],
            "shipToLocations": {"regionIncluded": [{"regionName": "Worldwide"}],
                                "regionExcluded": [{"regionName": "Africa"},
                                                   {"regionName": "Packstation"},
                                                   {"regionName": "PO Box"}]}}


def test_nur_de_body_entfernt_ausland_und_begrenzt_auf_de():
    body = lcs.nur_de_body(_policy_mit_versand())
    assert "fulfillmentPolicyId" not in body
    assert body["name"] == "Kostenlos Bearbeitung 2 Tage nur DE"
    typen = [o["optionType"] for o in body["shippingOptions"]]
    assert typen == ["DOMESTIC"]
    assert body["shipToLocations"]["regionIncluded"] == [{"regionName": "DE"}]
    # Sonder-Ausschluesse (Packstation/Postfach) bleiben, Laender-Ausschluesse nicht
    assert body["shipToLocations"]["regionExcluded"] == [
        {"regionName": "Packstation"}, {"regionName": "PO Box"}]
    assert body["globalShipping"] is False
    # Original-Dict unangetastet (kein In-Place-Umbau)
    raw = _policy_mit_versand()
    lcs.nur_de_body(raw)
    assert len(raw["shippingOptions"]) == 2


def test_nur_de_body_ohne_inlandsoption_wirft_klartext():
    raw = _policy_mit_versand()
    raw["shippingOptions"] = [o for o in raw["shippingOptions"]
                              if o["optionType"] == "INTERNATIONAL"]
    from app.retry import PersistentError
    with pytest.raises(PersistentError):
        lcs.nur_de_body(raw)


def test_nur_de_body_kappt_lange_namen_auf_64():
    body = lcs.nur_de_body(_policy_mit_versand(name="X" * 70))
    assert len(body["name"]) <= 64 and body["name"].endswith(" nur DE")


def _nurde_setup(db, monkeypatch, *, offers=None, item_id=None, profile=None):
    l = _listing(db, sku="AE-PWB", ali="lc905")
    if item_id:
        l.ebay_item_id = item_id
        db.commit()
    ebay = _StubEbay({"P1": _policy_mit_versand()},
                     offers if offers is not None else
                     {"AE-PWB": {"offerId": "OF-PWB",
                                 "listingPolicies": {"fulfillmentPolicyId": "P1"}}},
                     profile=profile)

    async def _basis_sku(ebay_, listing_):
        return [listing_.ebay_sku]
    from app.services import golive_service
    monkeypatch.setattr(golive_service, "_listing_variant_skus", _basis_sku)
    return l, ebay


def test_nur_deutschland_vorschau_schreibt_nichts(db, state_file, monkeypatch):
    l, ebay = _nurde_setup(db, monkeypatch)
    r = asyncio.run(lcs.nur_deutschland(db, ebay=ebay, listing_id=l.id))
    assert r["ok"] and r["vorschau"] is True
    assert r["policy_id"] == "P1" and r["variante_name"].endswith(" nur DE")
    assert r["intl_optionen"] == 1
    assert ebay.created == [] and ebay.updated == []


def test_nur_deutschland_go_stellt_um_und_cacht_variante(db, state_file, monkeypatch):
    l, ebay = _nurde_setup(db, monkeypatch)
    r = asyncio.run(lcs.nur_deutschland(db, ebay=ebay, listing_id=l.id, go=True))
    assert r["ok"] and r["vorschau"] is False and r["offers"] == 1
    assert len(ebay.created) == 1
    assert ebay.created[0]["shipToLocations"]["regionIncluded"] == [{"regionName": "DE"}]
    oid, body = ebay.updated[0]
    assert oid == "OF-PWB"
    assert body["listingPolicies"]["fulfillmentPolicyId"] == "NEU-1"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["varianten"]["P1|nurDE"] == "NEU-1"
    assert state["nur_de"][str(l.id)]["variante"] == "NEU-1"
    # Zweiter Lauf: Variante aus dem Cache, KEINE zweite Policy
    asyncio.run(lcs.nur_deutschland(db, ebay=ebay, listing_id=l.id, go=True))
    assert len(ebay.created) == 1


def test_nur_deutschland_ohne_offer_meldet_klassik_klartext(db, state_file, monkeypatch):
    l, ebay = _nurde_setup(db, monkeypatch, offers={})
    r = asyncio.run(lcs.nur_deutschland(db, ebay=ebay, listing_id=l.id, go=True))
    assert r["ok"] is False and "Klassik" in r["note"]
    assert ebay.created == [] and ebay.updated == []


def test_nur_deutschland_klassik_vorschau_ueber_item_profil(db, state_file, monkeypatch):
    l, ebay = _nurde_setup(db, monkeypatch, offers={}, item_id="389674838953",
                           profile={"profile_id": "P1",
                                    "profile_name": "Kostenlos Bearbeitung 2 Tage"})
    r = asyncio.run(lcs.nur_deutschland(db, ebay=ebay, listing_id=l.id))
    assert r["ok"] and r["vorschau"] is True and r["weg"] == "klassik"
    assert r["policy_id"] == "P1" and r["item_id"] == "389674838953"
    assert ebay.created == [] and ebay.updated == [] and ebay.revised == []


def test_nur_deutschland_klassik_go_setzt_versandprofil(db, state_file, monkeypatch):
    l, ebay = _nurde_setup(db, monkeypatch, offers={}, item_id="389674838953",
                           profile={"profile_id": "P1",
                                    "profile_name": "Kostenlos Bearbeitung 2 Tage"})
    r = asyncio.run(lcs.nur_deutschland(db, ebay=ebay, listing_id=l.id, go=True))
    assert r["ok"] and r["offers"] == 1
    assert len(ebay.created) == 1 and ebay.updated == []
    assert ebay.revised == [("389674838953", "NEU-1")]
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["nur_de"][str(l.id)]["weg"] == "klassik"
    assert state["varianten"]["P1|nurDE"] == "NEU-1"


def test_nur_deutschland_bereits_nur_de_legt_nichts_an(db, state_file, monkeypatch):
    """Idempotenz: erneuter Lauf auf umgestelltem Listing -> keine Doppel-Variante."""
    l, ebay = _nurde_setup(db, monkeypatch, offers={}, item_id="389674838953",
                           profile={"profile_id": "P9", "profile_name": "egal"})
    ebay.policies_raw["P9"] = dict(_policy_mit_versand(
        name="Kostenloser Versand 7 Tage Bearbeitung nur DE"), fulfillmentPolicyId="P9")
    r = asyncio.run(lcs.nur_deutschland(db, ebay=ebay, listing_id=l.id, go=True))
    assert r["ok"] and r.get("bereits_nur_de") is True
    assert ebay.created == [] and ebay.revised == [] and ebay.updated == []


def test_nur_deutschland_klassik_ohne_policy_meldet_klartext(db, state_file, monkeypatch):
    l, ebay = _nurde_setup(db, monkeypatch, offers={}, item_id="389674838953",
                           profile={"profile_id": None, "profile_name": None})
    r = asyncio.run(lcs.nur_deutschland(db, ebay=ebay, listing_id=l.id, go=True))
    assert r["ok"] is False and "manuell" in r["note"]
    assert ebay.created == [] and ebay.revised == []
