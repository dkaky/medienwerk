"""Tests fuer die native Listing-Engine (AutoDS-Ersatz) + Factory."""
from __future__ import annotations

import pytest

from app.integrations import get_listing_client
from app.integrations.ebay import MockEbayClient
from app.integrations.native_listing import (
    AutoDSBackend,
    NativeListingBackend,
    make_sku,
)
from app.services import golive_service


def test_image_axis_robust_against_ebay_rename():
    """Variantenbild-Achse muss auch greifen, wenn eBay 'Farbe' umbenennt (z.B.->'Duft')."""
    # Genau eine Achse (nach Umbenennung 'Duft') -> Bilder variieren nach ihr.
    assert golive_service._image_axis(["Farbe"], ["Duft"], {"Farbe": "Duft"}) == "Duft"
    # Mehrere Achsen: Farb-Achse am ORIGINAL erkannt, auf eBay-Namen gemappt.
    assert golive_service._image_axis(
        ["Farbe", "Größe"], ["Duft", "Größe"], {"Farbe": "Duft", "Größe": "Größe"}) == "Duft"
    # Mehrere Achsen ohne Farbe -> keine Bild-Achse.
    assert golive_service._image_axis(
        ["Größe", "Material"], ["Größe", "Material"], {}) is None
    # Einzelne Farb-Achse ohne Umbenennung.
    assert golive_service._image_axis(["Farbe"], ["Farbe"], {}) == "Farbe"


def test_make_sku_prefers_aliexpress_id():
    assert make_sku("123456", "https://x") == "AE-123456"


def test_make_sku_falls_back_to_url_hash():
    sku = make_sku(None, "https://de.aliexpress.com/item/abc.html")
    assert sku.startswith("AE-") and len(sku) == 15  # "AE-" + 12 hex


def test_factory_defaults_to_native():
    client = get_listing_client()
    assert isinstance(client, NativeListingBackend)
    assert client.name == "native"


@pytest.mark.asyncio
async def test_native_backend_creates_inventory_and_offer():
    backend = NativeListingBackend(MockEbayClient())
    draft = await backend.create_draft(
        sku="AE-1", title_seo="Titel", description="Beschreibung",
        image_urls=["https://img/1.jpg"], category_id="9355",
        price_eur=19.99, quantity=1, cost_eur=8.0,
    )
    assert draft.backend == "native"
    assert draft.ebay_draft_id            # offerId vom Mock
    assert draft.price_eur == 19.99
    assert draft.cost_eur == 8.0


@pytest.mark.asyncio
async def test_native_backend_sync_listing_runs():
    backend = NativeListingBackend(MockEbayClient())
    # darf nicht werfen (Mock-Update ist no-op)
    await backend.sync_listing(sku="AE-1", offer_id="offer_1", title="Neu",
                               description="d", price_eur=21.99, quantity=1)


@pytest.mark.asyncio
async def test_autods_backend_uses_real_aliexpress_url():
    """AutoDS muss die ECHTE URL bekommen (nicht die SKU) – sonst falscher Import."""
    captured = {}

    class _SpyAutoDS:
        async def import_aliexpress(self, *, aliexpress_url, title_seo, description, category_id):
            captured["url"] = aliexpress_url
            captured["category_id"] = category_id
            from app.integrations.autods import AutoDSDraft
            return AutoDSDraft(import_id="imp_1", ebay_draft_id="draft_1")

        async def sync_listing(self, listing_id, *, title, description):
            return None

    backend = AutoDSBackend(_SpyAutoDS())
    url = "https://de.aliexpress.com/item/x.html"
    draft = await backend.create_draft(
        sku="AE-123", aliexpress_url=url, title_seo="t", description="d",
        image_urls=[], category_id=None, price_eur=10.0, quantity=1,
    )
    assert draft.backend == "autods"
    assert draft.ebay_draft_id == "draft_1"
    assert captured["url"] == url          # echte URL, nicht "AE-123"
    assert captured["category_id"] == "0"  # None -> "0" Fallback


@pytest.mark.asyncio
async def test_native_backend_rolls_back_orphan_on_offer_failure():
    """Scheitert create_offer, muss das Inventory-Item wieder entfernt werden."""
    deleted = []

    class _FlakyEbay(MockEbayClient):
        async def create_offer(self, sku, *, price_eur, category_id, quantity,
                               merchant_location_key=None, listing_policies=None,
                               listing_description=None):
            raise RuntimeError("offer boom")

        async def delete_inventory_item(self, sku):
            deleted.append(sku)

    backend = NativeListingBackend(_FlakyEbay())
    with pytest.raises(RuntimeError):
        await backend.create_draft(
            sku="AE-9", title_seo="t", description="d", image_urls=[],
            category_id="1", price_eur=10.0, quantity=1,
        )
    assert deleted == ["AE-9"]   # Rollback erfolgt


class _FakeEbayAspects:
    def __init__(self, aspects):
        self._aspects = aspects

    async def get_required_aspects(self, category_id):
        return self._aspects


def test_variation_axis_mapping_renames_forbidden_axes():
    """Regression eBay 25002: 'Charakter ist kein zulaessiges Variantenmerkmal'."""
    import asyncio

    from app.services.golive_service import _rename_axes, _variation_safe_axis_map

    ebay = _FakeEbayAspects([
        {"name": "Marke", "required": True, "variation": False, "values": []},
        {"name": "Farbe", "required": False, "variation": True, "values": []},
        {"name": "Modell", "required": False, "variation": True, "values": []},
        {"name": "Größe", "required": False, "variation": True, "values": []},
    ])
    # 'Charakter' verboten -> erster freier Fallback (Modell); 'Farbe' erlaubt -> bleibt
    m = asyncio.run(_variation_safe_axis_map(ebay, "123", ["Charakter", "Farbe"]))
    assert m == {"Charakter": "Modell", "Farbe": "Farbe"}

    # Zwei verbotene Achsen -> zwei VERSCHIEDENE erlaubte Namen
    m2 = asyncio.run(_variation_safe_axis_map(ebay, "123", ["Charakter", "Edition"]))
    assert m2["Charakter"] != m2["Edition"]
    assert set(m2.values()) <= {"Farbe", "Modell", "Größe"}

    # Keine Taxonomy-Info -> alles unveraendert
    m3 = asyncio.run(_variation_safe_axis_map(_FakeEbayAspects([]), "123", ["Charakter"]))
    assert m3 == {"Charakter": "Charakter"}

    # _rename_axes benennt Namen + Options-Keys konsistent um
    names, variants = _rename_axes(
        ["Charakter"], [{"options": {"Charakter": "Goku"}, "image": "i"}], m)
    assert names == ["Modell"]
    assert variants[0]["options"] == {"Modell": "Goku"}
    assert variants[0]["image"] == "i"
