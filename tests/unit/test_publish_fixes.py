"""Fixes fuer haengende Entwuerfe (Vorfall 08/2026, golive 1056/1065).

1. eBay 25002 "Preisangebot-Entitaet existiert bereits": die offerId steht in der
   Fehlerantwort — sie muss direkt daraus gezogen werden (SKU-Suche nur Fallback).
2. eBay 25723: Bestand > 250 wird abgelehnt -> harter Deckel beim Publish.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.integrations.ebay import RealEbayClient
from app.services import golive_service


class _Resp:
    def __init__(self, data=None, broken=False):
        self._data, self._broken = data, broken

    def json(self):
        if self._broken:
            raise ValueError("kein JSON")
        return self._data


def _exc(data=None, broken=False, no_response=False):
    e = Exception("eBay 400")
    if not no_response:
        e.response = _Resp(data, broken=broken)
    return e


def test_offer_id_from_error_reads_parameters():
    # Exakt die Fehlerstruktur aus dem echten Vorfall (golive 1056)
    data = {"errors": [{"errorId": 25002, "domain": "API_INVENTORY",
                        "message": "Preisangebot-Entität existiert bereits.",
                        "parameters": [{"name": "offerId", "value": "228960962011"}]}]}
    assert RealEbayClient._offer_id_from_error(_exc(data)) == "228960962011"


def test_offer_id_from_error_defensive():
    assert RealEbayClient._offer_id_from_error(_exc(no_response=True)) is None
    assert RealEbayClient._offer_id_from_error(_exc(broken=True)) is None
    assert RealEbayClient._offer_id_from_error(_exc({"errors": [{"parameters": []}]})) is None
    assert RealEbayClient._offer_id_from_error(_exc({"errors": None})) is None


class _FakeEbay:
    def __init__(self):
        self.inventory_qty = None
        self.offer_qty = None

    async def create_inventory_item(self, sku, *, title, description, image_urls,
                                    quantity, aspects, brand):  # noqa: ARG002
        self.inventory_qty = quantity

    async def create_offer(self, sku, *, price_eur, category_id, quantity,
                           listing_description=None, listing_policies=None):  # noqa: ARG002
        self.offer_qty = quantity
        return "OFFER-1"


async def test_publish_single_caps_quantity_at_250():
    ebay = _FakeEbay()
    listing = SimpleNamespace(
        supplier_in_stock=True, quantity_available=4000, description="Beschreibung",
        title_seo="Titel", price_eur=19.95, ebay_draft_id=None,
        product=None, self_stock={})
    settings = SimpleNamespace(default_listing_quantity=20)

    item_id, mode = await golive_service._publish_single(
        ebay, listing, category="123", aspects={}, brand=None, images=[],
        base_sku="SKU-1", settings=settings, draft_only=True)

    assert (item_id, mode) == (None, "draft")
    assert ebay.inventory_qty == 250, "eBay-Limit 25723: Bestand muss gedeckelt sein"
    assert ebay.offer_qty == 250
    assert listing.ebay_draft_id == "OFFER-1"


async def test_publish_single_normal_quantity_untouched():
    ebay = _FakeEbay()
    listing = SimpleNamespace(
        supplier_in_stock=True, quantity_available=37, description="B",
        title_seo="T", price_eur=9.95, ebay_draft_id=None, product=None, self_stock={})
    settings = SimpleNamespace(default_listing_quantity=20)
    await golive_service._publish_single(
        ebay, listing, category="1", aspects={}, brand=None, images=[],
        base_sku="SKU-2", settings=settings, draft_only=True)
    assert ebay.offer_qty == 37
