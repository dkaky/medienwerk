"""Re-Import eines bereits vorhandenen Produkts (Vorfall 21.07.):

Dieselbe Ware kommt oft über leicht andere URLs -> die reine URL-Duplikatprüfung verfehlte
sie und der spätere INSERT crashte am aliexpress_id-UNIQUE. Fix: Duplikat auch per ID
erkennen; existiert das Produkt NUR als Entwurf, wird es IN PLACE aktualisiert (korrigierte
Größen) statt zu duplizieren/crashen. Ein bereits VERÖFFENTLICHTES Produkt wird abgelehnt."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.integrations.aliexpress import MockAliExpressClient
from app.models import Listing, Product
from app.retry import PersistentError
from app.services import product_service

_SIZE_VARIANTS = {
    "axes": {"Größe": ["50 x 70 cm", "40 x 60 cm"]},
    "skus": [
        {"id": "1", "attr": "5:1#50x70cm", "price": "5.69", "stock": 5,
         "options": {"Größe": "50 x 70 cm"}},
        {"id": "2", "attr": "5:2#40x60cm", "price": "5.69", "stock": 5,
         "options": {"Größe": "40 x 60 cm"}},
    ],
}


class _FixedIdAE(MockAliExpressClient):
    """Liefert für JEDE URL DASSELBE Produkt (feste aliexpress_id) – simuliert 'gleiche Ware,
    andere URL'. Erbt query_freight/place_order vom Mock."""

    def __init__(self, fixed_id: str):
        self._fixed_id = fixed_id

    async def scrape_product(self, url: str):
        sp = await super().scrape_product(url)
        return replace(sp, aliexpress_id=self._fixed_id, variants=_SIZE_VARIANTS)


def _use_fixed_ae(monkeypatch, fixed_id="1005099998888"):
    monkeypatch.setattr(product_service, "get_aliexpress_client", lambda: _FixedIdAE(fixed_id))


def test_reimport_same_product_via_other_url_refreshes_draft_no_crash(db, monkeypatch):
    """Der eigentliche Vorfall: gleiche ID, andere URL -> KEIN aliexpress_id-UNIQUE-Crash,
    sondern der vorhandene Entwurf wird in place aktualisiert (genau 1 Produkt)."""
    _use_fixed_ae(monkeypatch)
    r1 = asyncio.run(product_service.upload_product(
        db, aliexpress_url="https://de.aliexpress.com/item/1005099998888.html", skip_autods=True))
    assert r1["status"] == "draft_created"

    # zweiter Import DERSELBEN Ware über eine ANDERE URL (Query-Param) -> darf NICHT crashen
    r2 = asyncio.run(product_service.upload_product(
        db, aliexpress_url="https://www.aliexpress.com/item/1005099998888.html?spm=a2g0o.x",
        skip_autods=True))
    assert r2["status"] == "draft_created"
    assert r2["product_id"] == r1["product_id"]          # SELBES Produkt, kein Duplikat
    assert any("bereits als Entwurf" in w for w in r2["warnings"])

    prods = db.scalars(select(Product)).all()
    assert len(prods) == 1                                # genau EIN Produkt (kein Duplikat)
    axis = (prods[0].variants or {}).get("axes", {}).get("Größe")
    assert axis == ["50 x 70 cm", "40 x 60 cm"]           # volle L×B (nach dem Scraper-Fix)
    listings = db.scalars(select(Listing).where(Listing.product_id == prods[0].id)).all()
    assert len(listings) == 1                             # kein Zweit-Entwurf


def test_reimport_refused_if_listing_goes_live_midflight(db, monkeypatch):
    """TOCTOU (Review-Fund): wird das Listing WÄHREND Scrape/LLM (parallele Session) live geschaltet,
    darf der Refresh die jetzt-veröffentlichte Zeile NICHT überschreiben -> Ablehnung vor dem Write."""
    _use_fixed_ae(monkeypatch)
    r1 = asyncio.run(product_service.upload_product(
        db, aliexpress_url="https://de.aliexpress.com/item/1005099998888.html", skip_autods=True))
    lid = r1["listing_id"]

    class _MidflightPublishAE(_FixedIdAE):
        async def scrape_product(self, url):            # simuliert: paralleler Publish mitten im Import
            sp = await super().scrape_product(url)
            live = db.get(Listing, lid)
            live.listing_status, live.ebay_item_id = "active", "999888"
            db.commit()
            return sp
    monkeypatch.setattr(product_service, "get_aliexpress_client",
                        lambda: _MidflightPublishAE("1005099998888"))

    with pytest.raises(PersistentError) as ei:
        asyncio.run(product_service.upload_product(
            db, aliexpress_url="https://www.aliexpress.com/item/1005099998888.html?x=2", skip_autods=True))
    assert "live" in str(ei.value).lower()


def test_concurrent_create_collision_returns_clean_error(db, monkeypatch):
    """Wettlauf-Neuanlage: Produkt mit dieser aliexpress_id existiert schon (paralleler Import),
    die Duplikatprüfung verfehlt es aber (andere URL, ID nicht aus URL ableitbar) -> INSERT kollidiert.
    Statt 500-Crash eine saubere PersistentError."""
    from decimal import Decimal
    fixed = "1005077766655"
    db.add(Product(aliexpress_url="https://de.aliexpress.com/item/OTHER111.html",
                   aliexpress_id=fixed, title_raw="x", price_cny=Decimal("1")))
    db.commit()
    monkeypatch.setattr(product_service, "get_aliexpress_client", lambda: _FixedIdAE(fixed))

    with pytest.raises(PersistentError) as ei:
        asyncio.run(product_service.upload_product(
            db, aliexpress_url="https://de.aliexpress.com/item/2223334445.html", skip_autods=True))
    assert "zeitgleich" in str(ei.value).lower() or "existiert" in str(ei.value).lower()
    # kein halbes Duplikat: weiterhin genau das eine vorbestehende Produkt
    assert len(db.scalars(select(Product)).all()) == 1


def test_reimport_published_product_is_refused(db, monkeypatch):
    """Ist das Produkt bereits auf eBay veröffentlicht (ebay_item_id gesetzt), wird der Re-Import
    ABGELEHNT (nichts stillschweigend am Live-Listing ändern)."""
    _use_fixed_ae(monkeypatch)
    r1 = asyncio.run(product_service.upload_product(
        db, aliexpress_url="https://de.aliexpress.com/item/1005099998888.html", skip_autods=True))
    listing = db.get(Listing, r1["listing_id"])
    listing.listing_status = "active"
    listing.ebay_item_id = "1122334455"                  # simuliert: auf eBay veröffentlicht
    db.commit()

    with pytest.raises(PersistentError) as ei:
        asyncio.run(product_service.upload_product(
            db, aliexpress_url="https://www.aliexpress.com/item/1005099998888.html?ref=2",
            skip_autods=True))
    assert "live" in str(ei.value).lower()
    assert isinstance(ei.value, product_service.DuplikatListing)  # Dashboard kann den Knopf zeigen
    assert ei.value.listing_id == listing.id
    assert len(db.scalars(select(Product)).all()) == 1   # nichts dupliziert
