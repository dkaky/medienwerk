"""Schnelle Versandbedingung fuer EU-Lager-Quellen/Eigenbestand (Publish-Hook).

Sicherheitskern: publish_policies darf einen Publish NIE scheitern lassen und
NIE einen unvollstaendigen Policy-Satz liefern (payment/return muessen mitkommen).
Kriterium seit dem Ships-From-Umbau: gespeicherter Lagerort (ship_from je SKU),
NICHT die versprochene Lieferzeit (China-Express-Falle).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import fast_shipping_service as fs

FAST = {"policy_id": "FAST-1", "name": "Kostenlos Bearbeitung 2 Tage"}
SLOW = {"policy_id": "STD-1", "name": "Versand 7 Tage Bearbeitung"}
CTX = {"policies": {"payment": "PAY-1", "fulfillment": "STD-1", "return": "RET-1"}}


@pytest.fixture(autouse=True)
def _fresh_caches_and_settings(monkeypatch):
    fs.clear_caches()
    monkeypatch.setattr(fs, "get_settings", lambda: SimpleNamespace(
        ebay_fulfillment_policy_fast_name="Kostenlos Bearbeitung 2 Tage",
        fast_shipping_auto_policy=True))
    yield
    fs.clear_caches()


def _ebay(policies=(FAST, SLOW), fail=False):
    class E:
        async def list_fulfillment_policies(self):
            if fail:
                raise RuntimeError("Account API down")
            return list(policies)

        async def _listing_context(self):
            return dict(CTX)
    return E()


def _listing(ship_froms=None, self_stock=None, variants=...):
    if variants is ...:
        variants = ({"skus": [{"attr": f"200007763:x#{v}", "ship_from": v}
                              for v in ship_froms]} if ship_froms is not None else None)
    product = SimpleNamespace(aliexpress_id="123", variants=variants) if variants is not None else None
    return SimpleNamespace(id=42, product=product, self_stock=self_stock or {})


async def test_resolve_policy_by_name_case_insensitive():
    ebay = _ebay(policies=({"policy_id": "FAST-1", "name": "  kostenlos bearbeitung 2 tage "}, SLOW))
    assert await fs.resolve_fast_policy_id(ebay) == "FAST-1"


async def test_resolve_policy_missing_and_api_error_return_none():
    assert await fs.resolve_fast_policy_id(_ebay(policies=(SLOW,))) is None
    fs.clear_caches()
    assert await fs.resolve_fast_policy_id(_ebay(fail=True)) is None


def test_variants_have_eu_warehouse():
    assert fs.variants_have_eu_warehouse(_listing(["CHINA", "Polen"]).product) is True
    assert fs.variants_have_eu_warehouse(_listing(["Frankreich"]).product) is True
    assert fs.variants_have_eu_warehouse(_listing(["CHINA"]).product) is False
    # Alt-Produkte ohne ship_from-Feld -> konservativ nicht lokal
    assert fs.variants_have_eu_warehouse(
        SimpleNamespace(variants={"skus": [{"attr": "14:1#Rot"}]})) is False
    assert fs.variants_have_eu_warehouse(None) is False


async def test_publish_policies_eu_warehouse_gets_fast_policy():
    pol = await fs.publish_policies(_ebay(), _listing(["CHINA", "Polen"]))
    assert pol == {"payment": "PAY-1", "fulfillment": "FAST-1", "return": "RET-1"}, \
        "voller Policy-Satz mit getauschter Versandbedingung"


async def test_publish_policies_china_only_returns_none():
    assert await fs.publish_policies(_ebay(), _listing(["CHINA"])) is None
    # kurze Lieferzeit-Versprechen zaehlen NICHT mehr — ohne EU-ship_from kein Tausch
    assert await fs.publish_policies(_ebay(), _listing(ship_froms=None, variants=None)) is None


async def test_publish_policies_self_stock_without_source():
    listing = _listing(ship_froms=None, variants=None,
                       self_stock={"977:1#One": {"qty": 3, "cost_eur": 2.0}})
    pol = await fs.publish_policies(_ebay(), listing)
    assert pol and pol["fulfillment"] == "FAST-1"


async def test_publish_policies_kill_switch_and_errors_never_break(monkeypatch):
    monkeypatch.setattr(fs, "get_settings", lambda: SimpleNamespace(
        ebay_fulfillment_policy_fast_name="Kostenlos Bearbeitung 2 Tage",
        fast_shipping_auto_policy=False))
    assert await fs.publish_policies(_ebay(), _listing(["Polen"])) is None

    monkeypatch.setattr(fs, "get_settings", lambda: SimpleNamespace(
        ebay_fulfillment_policy_fast_name="Kostenlos Bearbeitung 2 Tage",
        fast_shipping_auto_policy=True))
    # ALLES kaputt -> trotzdem None statt Exception (Publish geht weiter)
    assert await fs.publish_policies(_ebay(fail=True), _listing(["Polen"])) is None


# --- apply_fast_policy_to_listing (Einzelfall-Umstellung bei Eigenbestand) ----

def _offer_ebay(offers_by_sku, fail=False):
    """Fake mit Offer-Lookup + PUT-Aufzeichnung (Muster wie _ebay, plus Inventory)."""
    class E:
        puts: list = []

        async def list_fulfillment_policies(self):
            if fail:
                raise RuntimeError("Account API down")
            return [FAST, SLOW]

        async def _first_offer_for_sku(self, sku):
            return offers_by_sku.get(sku)

        def _sanitize_offer(self, offer):
            drop = {"offerId", "listing", "status", "sku"}
            return {k: v for k, v in offer.items() if k not in drop}

        async def update_offer(self, offer_id, body):
            self.puts.append((offer_id, body))
    return E()


@pytest.fixture()
def _skus_from_variants(monkeypatch):
    """_listing_variant_skus auf die Fixture-Varianten mappen (ohne echte eBay-Calls)."""
    from app.services import golive_service

    async def fake(ebay, listing):
        skus = ((getattr(listing.product, "variants", None) or {}).get("skus")
                if getattr(listing, "product", None) else None) or []
        return [s["attr"] for s in skus]
    monkeypatch.setattr(golive_service, "_listing_variant_skus", fake)


async def test_apply_fast_policy_switches_offers(_skus_from_variants):
    listing = _listing(["Polen", "Frankreich"])  # verschiedene Lager -> verschiedene SKU-attrs
    attrs = [s["attr"] for s in listing.product.variants["skus"]]
    ebay = _offer_ebay({
        attrs[0]: {"offerId": "O-1", "sku": attrs[0], "status": "PUBLISHED",
                   "listingPolicies": {"fulfillmentPolicyId": "STD-1", "paymentPolicyId": "PAY-1"}},
        attrs[1]: {"offerId": "O-2", "sku": attrs[1],
                   "listingPolicies": {"fulfillmentPolicyId": "FAST-1"}},  # schon schnell
    })
    res = await fs.apply_fast_policy_to_listing(None, ebay, listing)
    assert res == {"switched": True, "offers": 2}
    # nur der langsame Offer wird per PUT angefasst; uebrige Policies bleiben erhalten
    assert [oid for oid, _ in ebay.puts] == ["O-1"]
    body = ebay.puts[0][1]
    assert body["listingPolicies"]["fulfillmentPolicyId"] == "FAST-1"
    assert body["listingPolicies"]["paymentPolicyId"] == "PAY-1"
    assert "offerId" not in body and "status" not in body, "Body muss sanitisiert sein"


async def test_apply_fast_policy_no_offers_returns_note(_skus_from_variants):
    listing = _listing(["Polen"])
    res = await fs.apply_fast_policy_to_listing(None, _offer_ebay({}), listing)
    assert res["switched"] is False and "manuell" in res["note"]


async def test_apply_fast_policy_resolve_or_api_errors_return_note(_skus_from_variants):
    listing = _listing(["Polen"])
    res = await fs.apply_fast_policy_to_listing(None, _offer_ebay({}, fail=True), listing)
    assert res["switched"] is False, "Fehler duerfen den Aufrufer nie brechen"
