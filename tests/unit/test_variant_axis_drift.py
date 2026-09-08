"""Regression: Varianten-Achsen duerfen zwischen Inventory-Items und -Group nicht driften.

Bug (Listing 426 'Deutscher Adler'): _variation_safe_axis_map ist nicht deterministisch
(bei Taxonomy-Ausfall Identity-Map). Bei getrennten Operationen (Publish vs. spaeterem
_push_group_update) bekamen die ITEMS die umbenannten Achsen (Stil/Farbe), die GRUPPE aber
die Original-AE-Achsen (Adler-Design/Lieferumfang) -> aspectsImageVariesBy zeigte auf einen
Namen, den kein Item traegt -> eBay zeigte fuer alle Varianten dasselbe Bild.

Fix: Bei Live-Listings uebernimmt _push_group_update die live GETRAGENEN Achsennamen aus den
Inventory-Items (Fallback Gruppe) statt sie neu zu raten. repair_variant_axes repariert
bereits gedriftete Listings in-place (oder meldet 'Aktion erforderlich' bei eBay 25013).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.config import get_settings
from app.models import Listing, Product
from app.retry import PersistentError
from app.services import golive_service


# Die ITEMS tragen live die umbenannten Achsen 'Stil'/'Farbe' (nur die KEYS wurden beim
# Publish umbenannt, die WERTE bleiben die AE-Optionswerte -> Werte-Abgleich matcht sie).
_LIVE_ITEM_ASPECTS = {
    "AE-AD-V1": {"Stil": ["Style 1"], "Farbe": ["Mit Kette"], "Marke": ["Markenlos"]},
    "AE-AD-V2": {"Stil": ["Style 1"], "Farbe": ["Nur Anhänger"], "Marke": ["Markenlos"]},
    "AE-AD-V3": {"Stil": ["Style 2"], "Farbe": ["Mit Kette"], "Marke": ["Markenlos"]},
    "AE-AD-V4": {"Stil": ["Style 2"], "Farbe": ["Nur Anhänger"], "Marke": ["Markenlos"]},
}

_DRIFTED_GROUP = {
    "variesBy": {
        "specifications": [
            {"name": "Adler-Design", "values": ["Style 1", "Style 2"]},
            {"name": "Lieferumfang", "values": ["Mit Kette", "Nur Anhänger"]},
        ],
        "aspectsImageVariesBy": ["Adler-Design"],
    },
    "variantSKUs": ["AE-AD-V1", "AE-AD-V2", "AE-AD-V3", "AE-AD-V4"],
}

_CONSISTENT_GROUP = {
    "variesBy": {
        "specifications": [
            {"name": "Stil", "values": ["Style 1", "Style 2"]},
            {"name": "Farbe", "values": ["Mit Kette", "Nur Anhänger"]},
        ],
        "aspectsImageVariesBy": ["Stil"],
    },
    "variantSKUs": ["AE-AD-V1", "AE-AD-V2", "AE-AD-V3", "AE-AD-V4"],
}


def _adler(db, *, live=True):
    """Adler-Produkt: 2 Achsen (Adler-Design x Lieferumfang), Bild variiert nach Design."""
    p = Product(
        aliexpress_url="https://de.aliexpress.com/item/adler.html", aliexpress_id="ad1",
        price_cny=Decimal("12"), images=["https://i/main.jpg"],
        variants={"axes": {"Adler-Design": ["Style 1", "Style 2"],
                           "Lieferumfang": ["Mit Kette", "Nur Anhänger"]},
                  "skus": [
            {"attr": "a1", "options": {"Adler-Design": "Style 1", "Lieferumfang": "Mit Kette"},
             "price": 12.0, "stock": 9, "image": "https://i/s1.jpg"},
            {"attr": "a2", "options": {"Adler-Design": "Style 1", "Lieferumfang": "Nur Anhänger"},
             "price": 10.0, "stock": 9, "image": "https://i/s1.jpg"},
            {"attr": "a3", "options": {"Adler-Design": "Style 2", "Lieferumfang": "Mit Kette"},
             "price": 13.0, "stock": 9, "image": "https://i/s2.jpg"},
            {"attr": "a4", "options": {"Adler-Design": "Style 2", "Lieferumfang": "Nur Anhänger"},
             "price": 11.0, "stock": 9, "image": "https://i/s2.jpg"}]})
    db.add(p)
    db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-AD", title_seo="Adler", description="d",
                listing_status="active", price_eur=Decimal("24.95"), category_id="123",
                ebay_draft_id="AE-AD-GRP")
    if live:
        l.ebay_item_id = "IT-426"
    db.add(l)
    db.commit()
    return p, l


class _FakeEbay:
    """Minimaler eBay-Client: liefert Live-Item-Aspekte + (veraenderbare) Gruppe."""

    def __init__(self, *, item_aspects=None, group=None, taxonomy=None, publish_error=None):
        self._items = item_aspects if item_aspects is not None else _LIVE_ITEM_ASPECTS
        self._group = group
        self._taxonomy = taxonomy or []
        self._publish_error = publish_error
        self.created_group = None
        self.published = 0

    async def get_inventory_item(self, sku):
        asp = self._items.get(sku)
        return {"product": {"aspects": asp}} if asp else None

    async def get_inventory_item_group(self, gk):
        return self._group

    async def get_required_aspects(self, cat):
        return self._taxonomy

    async def build_aspects(self, cat, base):
        return {k: (v if isinstance(v, list) else [v]) for k, v in (base or {}).items()}

    async def create_inventory_item_group(self, gk, **kw):
        self.created_group = {"group_key": gk, **kw}
        self._group = {"variesBy": {"specifications": kw.get("specifications") or [],
                                    "aspectsImageVariesBy": kw.get("image_varies_by") or []},
                       "variantSKUs": kw.get("variant_skus") or []}

    async def publish_offer_by_inventory_item_group(self, gk):
        if self._publish_error:
            raise RuntimeError(self._publish_error)
        self.published += 1
        return "IT-426"


def _spec_names(kw) -> set:
    return {s["name"] for s in (kw.get("specifications") or [])}


# ------------------------------------------------------------------ _live_axis_map
def test_live_axis_map_reads_names_from_items(db):
    p, l = _adler(db)
    axis_names, variants = golive_service._usable_variants(p)
    m = asyncio.run(golive_service._live_axis_map(_FakeEbay(), l, axis_names, variants))
    # AE-Achsen werden ueber die Werte-Reihenfolge den live getragenen Namen zugeordnet.
    assert m == {"Adler-Design": "Stil", "Lieferumfang": "Farbe"}


def test_live_axis_map_group_fallback_when_items_unreadable(db):
    p, l = _adler(db)
    axis_names, variants = golive_service._usable_variants(p)
    # Items nicht lesbar -> Fallback auf die (konsistente) Gruppe.
    ebay = _FakeEbay(item_aspects={}, group=_CONSISTENT_GROUP)
    m = asyncio.run(golive_service._live_axis_map(ebay, l, axis_names, variants))
    assert m == {"Adler-Design": "Stil", "Lieferumfang": "Farbe"}


def test_live_axis_map_none_when_nothing_live(db):
    p, l = _adler(db)
    axis_names, variants = golive_service._usable_variants(p)
    ebay = _FakeEbay(item_aspects={}, group=None)
    assert asyncio.run(golive_service._live_axis_map(ebay, l, axis_names, variants)) is None


# ------------------------------------------------------------------ _push_group_update (Kern-Regression)
def test_push_group_update_uses_live_item_axes_despite_taxonomy_down(db):
    """DER 426-Bug: Taxonomy faellt beim Group-Update aus. Ohne Fix -> Identity-Map ->
    Gruppe bekommt 'Adler-Design'/'Lieferumfang' (driftet von Items 'Stil'/'Farbe').
    Mit Fix -> Gruppe uebernimmt die live getragenen Item-Achsen 'Stil'/'Farbe'."""
    p, l = _adler(db, live=True)
    ebay = _FakeEbay(taxonomy=[])   # Taxonomy nicht verfuegbar
    ok = asyncio.run(golive_service._push_group_update(ebay, l, db))
    assert ok is True
    kw = ebay.created_group
    assert _spec_names(kw) == {"Stil", "Farbe"}            # NICHT die Original-AE-Namen
    # Bild-Achse zeigt auf einen Namen, den die Items wirklich tragen.
    assert kw["image_varies_by"] == ["Stil"]


def test_push_group_update_draft_falls_back_to_axis_map(db):
    """Ohne Live-Item (Entwurf) bleibt der alte Pfad: _variation_safe_axis_map."""
    p, l = _adler(db, live=False)   # kein ebay_item_id
    ebay = _FakeEbay(taxonomy=[])   # Identity -> Original-Namen (self-consistent beim Publish)
    ok = asyncio.run(golive_service._push_group_update(ebay, l, db))
    assert ok is True
    assert _spec_names(ebay.created_group) == {"Adler-Design", "Lieferumfang"}


# ------------------------------------------------------------------ repair_variant_axes
def _patch_real_ebay(monkeypatch, ebay):
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: ebay)


def test_repair_inplace_resets_group_to_item_truth(db, monkeypatch):
    p, l = _adler(db, live=True)
    ebay = _FakeEbay(group={k: v for k, v in _DRIFTED_GROUP.items()})
    _patch_real_ebay(monkeypatch, ebay)
    r = asyncio.run(golive_service.repair_variant_axes(db, listing_id=l.id))
    assert r["repaired"] is True and r["drift"] is True and r["verified"] is True
    assert set(r["axes"]) == {"Stil", "Farbe"}
    assert r["image_varies_by"] == ["Stil"]
    assert ebay.published == 1                              # republish erfolgt
    assert _spec_names(ebay.created_group) == {"Stil", "Farbe"}


def test_repair_noop_when_already_consistent(db, monkeypatch):
    p, l = _adler(db, live=True)
    ebay = _FakeEbay(group={k: v for k, v in _CONSISTENT_GROUP.items()})
    _patch_real_ebay(monkeypatch, ebay)
    r = asyncio.run(golive_service.repair_variant_axes(db, listing_id=l.id))
    assert r["repaired"] is False and r["drift"] is False
    assert ebay.created_group is None and ebay.published == 0   # nichts angefasst


def test_repair_25013_proposes_recreate_without_ending(db, monkeypatch):
    """eBay 25013 (in-place nicht moeglich) -> nichts beenden, action_required melden."""
    p, l = _adler(db, live=True)
    ebay = _FakeEbay(group={k: v for k, v in _DRIFTED_GROUP.items()},
                     publish_error="Errors: 25013 group inconsistent")
    _patch_real_ebay(monkeypatch, ebay)
    r = asyncio.run(golive_service.repair_variant_axes(db, listing_id=l.id))
    assert r["repaired"] is False
    assert r["needs_recreate"] is True and r["action_required"] is True
    assert ebay.published == 0                              # kein erfolgreicher Publish
    # Listing bleibt aktiv – es wurde NICHTS automatisch beendet.
    db.refresh(l)
    assert l.listing_status == "active"


def test_repair_rejects_non_multi_listing(db, monkeypatch):
    p, l = _adler(db, live=True)
    l.ebay_draft_id = "offer_123"   # kein -GRP -> Einzel-Listing
    db.commit()
    _patch_real_ebay(monkeypatch, _FakeEbay())
    with pytest.raises(PersistentError, match="Multivarianten"):
        asyncio.run(golive_service.repair_variant_axes(db, listing_id=l.id))


# ------------------------------------------------------------------ variant_repair_report (read-only)
def _live_multi(db, *, sku, item_id, days_ago=0, stocks=(9, 9), grp=True):
    p = Product(aliexpress_url=f"https://de.aliexpress.com/item/{sku}.html", aliexpress_id=sku,
                price_cny=Decimal("6"), images=["https://i/m.jpg"],
                variants={"axes": {"Farbe": ["A", "B"]},
                          "skus": [
                    {"attr": "x1", "options": {"Farbe": "A"}, "price": 6.0,
                     "stock": stocks[0], "image": "https://i/a.jpg"},
                    {"attr": "x2", "options": {"Farbe": "B"}, "price": 6.0,
                     "stock": stocks[1], "image": "https://i/b.jpg"}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku=sku, title_seo=sku, description="d",
                listing_status="active", price_eur=Decimal("19.95"),
                ebay_item_id=item_id, ebay_draft_id=(f"{sku}-GRP" if grp else "offer_1"))
    db.add(l); db.flush()
    if days_ago:
        from datetime import datetime, timedelta, timezone
        l.created_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.commit()
    return l


def test_report_only_multi_live_and_scope(db):
    _live_multi(db, sku="AE-NEU", item_id="800000000001", days_ago=0)      # diese Woche
    _live_multi(db, sku="AE-ALT", item_id="800000000002", days_ago=30)     # alt
    _live_multi(db, sku="AE-SGL", item_id="800000000003", grp=False)       # Einzel-Listing
    draft = _live_multi(db, sku="AE-DRF", item_id="800000000004")
    draft.ebay_item_id = None; draft.listing_status = "draft"; db.commit()  # Entwurf

    allr = golive_service.variant_repair_report(db, scope="all")
    assert allr["total_multi_live"] == 2                 # nur die zwei Multi-Live
    assert allr["this_week"] == 1 and allr["older"] == 1
    assert {r["ebay_item_id"] for r in allr["listings"]} == {"800000000001", "800000000002"}
    assert allr["listings"][0]["this_week"] is True      # diese Woche oben

    wk = golive_service.variant_repair_report(db, scope="week")
    assert wk["total_multi_live"] == 1
    assert wk["listings"][0]["ebay_item_id"] == "800000000001"


def test_report_flags_stock_and_image_cleanup(db):
    _live_multi(db, sku="AE-OK", item_id="800000000010", stocks=(9, 9))
    _live_multi(db, sku="AE-OOS", item_id="800000000011", stocks=(9, 0))   # eine Variante aus
    r = golive_service.variant_repair_report(db, scope="all")
    by_id = {x["ebay_item_id"]: x for x in r["listings"]}
    assert by_id["800000000011"]["sold_out_variants"] == 1
    assert by_id["800000000011"]["needs_stock_fix"] is True
    assert by_id["800000000010"]["needs_stock_fix"] is False
    assert r["needs_stock_fix"] == 1
    # Bild-Aufraeumen betrifft ALLE Alt-Listings.
    assert r["needs_image_cleanup"] == r["total_multi_live"] == 2
    assert all(x["needs_image_cleanup"] for x in r["listings"])


# ------------------------------------------------------------------ bulk_repair_variants
def test_bulk_dry_run_changes_nothing(db, monkeypatch):
    _live_multi(db, sku="AE-W1", item_id="800000000021", days_ago=0)
    _live_multi(db, sku="AE-W2", item_id="800000000022", days_ago=0)
    _live_multi(db, sku="AE-OLD", item_id="800000000023", days_ago=30)

    called = []
    async def _boom(dbx, *, listing_id):
        called.append(listing_id)
        raise AssertionError("Reparatur darf im dry_run NICHT aufgerufen werden")
    monkeypatch.setattr(golive_service, "repair_variant_images", _boom)

    r = asyncio.run(golive_service.bulk_repair_variants(db, scope="week", dry_run=True))
    assert r["dry_run"] is True and r["count"] == 2   # nur die zwei dieser Woche
    assert called == []                               # nichts angefasst


def test_bulk_real_run_repairs_and_flags_partial(db, monkeypatch):
    _live_multi(db, sku="AE-A", item_id="800000000031", days_ago=0)
    b = _live_multi(db, sku="AE-B", item_id="800000000032", days_ago=0)

    async def _fake_repair(dbx, *, listing_id):
        if listing_id == b.id:  # einzelne SKU nach Retries noch offen -> partial
            return {"listing_id": listing_id, "item_id": "IT", "trimmed": 3, "failed": ["S9"]}
        return {"listing_id": listing_id, "item_id": "IT", "trimmed": 5, "failed": []}
    monkeypatch.setattr(golive_service, "repair_variant_images", _fake_repair)

    r = asyncio.run(golive_service.bulk_repair_variants(db, scope="week", dry_run=False))
    assert r["repaired"] == 1 and r["partial"] == 1 and r["errored"] == 0
    assert r["total_trimmed"] == 8 and r["total_failed_skus"] == 1
    by_id = {x["listing_id"]: x for x in r["results"]}
    assert by_id[b.id]["status"] == "partial" and by_id[b.id]["failed_skus"] == ["S9"]
    # Nichts wurde beendet – Listing bleibt aktiv.
    db.refresh(b)
    assert b.listing_status == "active"


def test_bulk_real_run_isolates_generic_failure(db, monkeypatch):
    a = _live_multi(db, sku="AE-C", item_id="800000000041", days_ago=0)
    _live_multi(db, sku="AE-D", item_id="800000000042", days_ago=0)

    async def _fake_repair(dbx, *, listing_id):
        if listing_id == a.id:
            raise RuntimeError("Netzwerk kaputt")
        return {"listing_id": listing_id, "item_id": "IT", "trimmed": 2, "failed": []}
    monkeypatch.setattr(golive_service, "repair_variant_images", _fake_repair)

    r = asyncio.run(golive_service.bulk_repair_variants(db, scope="week", dry_run=False))
    assert r["repaired"] == 1 and r["errored"] == 1 and r["partial"] == 0


def test_bulk_limit_restricts_batch(db, monkeypatch):
    for i in range(3):
        _live_multi(db, sku=f"AE-L{i}", item_id=f"8000000000{50+i}", days_ago=0)
    seen = []
    async def _fake_repair(dbx, *, listing_id):
        seen.append(listing_id)
        return {"listing_id": listing_id, "item_id": "IT", "trimmed": 1, "failed": []}
    monkeypatch.setattr(golive_service, "repair_variant_images", _fake_repair)

    r = asyncio.run(golive_service.bulk_repair_variants(db, scope="week", dry_run=False, limit=2))
    assert r["count"] == 2 and len(seen) == 2   # nur 2 trotz 3 Kandidaten


# ------------------------------------------------------------------ repair_variant_images
class _FakeImgEbay:
    """eBay-Client-Stub fuer die Bild-Reparatur: Items mit mehreren Bildern."""
    def __init__(self):
        self._imgs = {"S1": ["vimg1", "c1", "c2", "c3"], "S2": ["vimg2", "c1", "c2"]}
        self.updated = {}
        self.published = 0
    async def get_inventory_item_group(self, gk):
        return {"variantSKUs": ["S1", "S2"]}
    async def get_inventory_item(self, sku):
        return {"product": {"imageUrls": self._imgs.get(sku, [])}}
    async def update_inventory_item_fields(self, sku, *, title=None, description=None,
                                           aspects=None, image_urls=None):
        if image_urls is not None:
            self._imgs[sku] = list(image_urls)
            self.updated[sku] = list(image_urls)
    async def publish_offer_by_inventory_item_group(self, gk):
        self.published += 1
        return "IT-X"


def test_repair_variant_images_trims_to_one_in_place(db, monkeypatch):
    from app.models import Listing
    from app.services import golive_service
    l = Listing(title_seo="T", description="d", listing_status="active",
                ebay_item_id="800111", ebay_sku="AE-1-GRP", ebay_draft_id="AE-1-GRP")
    db.add(l); db.commit()
    fake = _FakeImgEbay()
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: fake)
    r = asyncio.run(golive_service.repair_variant_images(db, listing_id=l.id))
    # Beide Varianten auf GENAU 1 Bild (ihr eigenes) gekuerzt, Gruppe re-published.
    assert r["trimmed"] == 2 and r["item_id"] == "800111"
    assert fake.updated == {"S1": ["vimg1"], "S2": ["vimg2"]}
    assert fake.published == 1


def test_repair_variant_images_noop_when_already_one(db, monkeypatch):
    from app.models import Listing
    from app.services import golive_service
    l = Listing(title_seo="T", description="d", listing_status="active",
                ebay_item_id="800222", ebay_sku="AE-2-GRP", ebay_draft_id="AE-2-GRP")
    db.add(l); db.commit()
    fake = _FakeImgEbay(); fake._imgs = {"S1": ["v1"], "S2": ["v2"]}
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: fake)
    r = asyncio.run(golive_service.repair_variant_images(db, listing_id=l.id))
    assert r["trimmed"] == 0 and fake.published == 0


class _FlakyImgEbay(_FakeImgEbay):
    """update_inventory_item_fields scheitert die ersten `fail_times`-Aufrufe je SKU."""
    def __init__(self, fail_times=1, always_fail_sku=None):
        super().__init__()
        self._imgs = {"S1": ["v1", "a", "b"], "S2": ["v2", "a", "b"]}
        self._left = {}
        self._fail_times = fail_times
        self._always = always_fail_sku
    async def update_inventory_item_fields(self, sku, *, title=None, description=None,
                                           aspects=None, image_urls=None):
        from app.retry import TransientError
        if sku == self._always:
            raise TransientError("eBay 500: 25001")
        n = self._left.get(sku, self._fail_times)
        if n > 0:
            self._left[sku] = n - 1
            raise TransientError("eBay 500: 25001")
        await super().update_inventory_item_fields(sku, image_urls=image_urls)


def test_repair_variant_images_retries_flaky_25001(db, monkeypatch):
    from app.models import Listing
    from app.services import golive_service
    async def _nosleep(*a, **k): return None
    monkeypatch.setattr(golive_service.asyncio, "sleep", _nosleep)
    l = Listing(title_seo="T", description="d", listing_status="active",
                ebay_item_id="800333", ebay_sku="AE-3-GRP", ebay_draft_id="AE-3-GRP")
    db.add(l); db.commit()
    fake = _FlakyImgEbay(fail_times=2)  # 2x scheitern, 3. Versuch klappt
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: fake)
    r = asyncio.run(golive_service.repair_variant_images(db, listing_id=l.id))
    assert r["trimmed"] == 2 and r["failed"] == [] and fake.published == 1
    assert fake.updated == {"S1": ["v1"], "S2": ["v2"]}


def test_repair_variant_images_collects_persistent_failures(db, monkeypatch):
    from app.models import Listing
    from app.services import golive_service
    async def _nosleep(*a, **k): return None
    monkeypatch.setattr(golive_service.asyncio, "sleep", _nosleep)
    l = Listing(title_seo="T", description="d", listing_status="active",
                ebay_item_id="800444", ebay_sku="AE-4-GRP", ebay_draft_id="AE-4-GRP")
    db.add(l); db.commit()
    fake = _FlakyImgEbay(fail_times=0, always_fail_sku="S2")  # S1 ok, S2 dauerhaft kaputt
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: fake)
    r = asyncio.run(golive_service.repair_variant_images(db, listing_id=l.id))
    assert r["trimmed"] == 1 and r["failed"] == ["S2"]
    assert fake.published == 1  # S1-Erfolg wird trotzdem live gestellt
    assert fake.updated == {"S1": ["v1"]}
