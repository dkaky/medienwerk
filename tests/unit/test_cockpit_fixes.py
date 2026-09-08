"""Cockpit-Fixes 08.08.: eBay-Galerie-Cache im Detail + Auto-Varianten-Zuordnung."""
from __future__ import annotations

from app.models import Listing
from app.services import golive_service, supplier_service


def _listing(db, *, item_id="112233"):
    l = Listing(title_seo="T", description="d", listing_status="active",
                ebay_item_id=item_id)
    db.add(l)
    db.flush()
    return l


async def test_ebay_gallery_fetches_once_then_serves_cache(db, monkeypatch):
    calls = {"n": 0}

    async def fake_gallery(_ebay, _listing):
        calls["n"] += 1
        # Realistische EPS-URLs: der Cache gilt nur als frisch, wenn er nach echter
        # eBay-Galerie aussieht (Guard gegen vergiftete alicdn-Alt-Caches, 09.08.).
        return [{"url": "https://i.ebayimg.com/e1.jpg"},
                {"url": "https://i.ebayimg.com/e2.jpg"}]

    monkeypatch.setattr(golive_service, "ebay_listing_images", fake_gallery)
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: object())
    l = _listing(db)
    db.commit()

    r1 = await golive_service.get_ebay_gallery(db, listing_id=l.id)
    assert r1 == {"images": ["https://i.ebayimg.com/e1.jpg", "https://i.ebayimg.com/e2.jpg"],
                  "live": True, "cached": False}
    r2 = await golive_service.get_ebay_gallery(db, listing_id=l.id)
    assert r2["cached"] is True and r2["images"] == r1["images"]
    assert calls["n"] == 1, "zweiter Aufruf muss aus dem Cache kommen"


async def test_listing_images_prefer_live_gallery_over_inventory(db):
    """GetItem (echte Live-Galerie, i.ebayimg.com) schlaegt den Inventory-Pfad, der nur
    die beim Publish EINGEREICHTEN AliExpress-URLs kennt (Nutzer-Fund 09.08.)."""
    class E:
        async def get_item_pictures(self, item_id):
            return ["https://i.ebayimg.com/real1.jpg", "https://i.ebayimg.com/real2.jpg"]

        async def get_inventory_item(self, sku):
            raise AssertionError("Inventory darf nicht gefragt werden, wenn GetItem liefert")

    l = _listing(db)
    out = await golive_service.ebay_listing_images(E(), l)
    assert [c["url"] for c in out] == ["https://i.ebayimg.com/real1.jpg",
                                      "https://i.ebayimg.com/real2.jpg"]
    assert out[0]["is_main"] is True

    class E2:                      # GetItem leer -> Inventory-Fallback greift weiter
        async def get_item_pictures(self, item_id):
            return []

        async def get_inventory_item(self, sku):
            return {"product": {"imageUrls": ["https://ae01.alicdn.com/a.jpg"]}}

    l2 = _listing(db, item_id="445566")
    l2.ebay_sku = "AE-Z"
    out2 = await golive_service.ebay_listing_images(E2(), l2)
    assert [c["url"] for c in out2] == ["https://ae01.alicdn.com/a.jpg"]


async def test_ebay_gallery_error_serves_stale_cache_and_draft_is_empty(db, monkeypatch):
    async def boom(_ebay, _listing):
        raise RuntimeError("eBay down")

    monkeypatch.setattr(golive_service, "_real_ebay", lambda: object())
    l = _listing(db)
    l.ebay_gallery = {"urls": ["https://ebay/alt.jpg"], "fetched_at": "2020-01-01T00:00:00+00:00"}
    db.commit()

    monkeypatch.setattr(golive_service, "ebay_listing_images", boom)
    r = await golive_service.get_ebay_gallery(db, listing_id=l.id)
    assert r["images"] == ["https://ebay/alt.jpg"] and r.get("stale") is True, \
        "API-Stoerung -> alter Cache statt Fehler (Modal hat Quell-Fallback)"

    entwurf = _listing(db, item_id=None)
    db.commit()
    assert await golive_service.get_ebay_gallery(db, listing_id=entwurf.id) == \
        {"images": [], "live": False}


def test_auto_match_variants_learns_unique_matches_only(db):
    l = _listing(db)
    db.commit()
    old = [{"options": {"Farbe": "Rot"}}, {"options": {"Farbe": "Blau"}},
           {"options": {}}]  # ohne Optionen -> zaehlt nicht
    new = [{"attr": "14:1#Red;200007763:9#Polen", "id": "1", "options": {"Farbe": "Rot"}},
           {"attr": "14:2#Blue;200007763:9#Polen", "id": "2", "options": {"Farbe": "Blau"}}]

    r = supplier_service._auto_match_variants(l, "555", old, new)
    assert r == {"matched": 2, "total": 2}
    vmap = l.variant_map or {}
    assert "14:1#Red;200007763:9#Polen" in vmap.values()
    assert "14:2#Blue;200007763:9#Polen" in vmap.values()
    assert any(k.startswith("555|") for k in vmap), "Quellen-Namespace muss gesetzt sein"


def test_auto_match_variants_ambiguity_stays_unmatched(db):
    l = _listing(db)
    db.commit()
    old = [{"options": {"Farbe": "Rot"}}]
    new = [{"attr": "14:1#RedA", "id": "1", "options": {"Farbe": "Rot"}},
           {"attr": "14:2#RedB", "id": "2", "options": {"Farbe": "Rot"}}]

    r = supplier_service._auto_match_variants(l, "777", old, new)
    assert r["matched"] == 0, "Mehrdeutigkeit darf NIE geraten werden (falsche Ware)"
    assert not (l.variant_map or {})
