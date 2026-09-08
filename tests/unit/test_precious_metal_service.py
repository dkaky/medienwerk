"""Scan/Backfill: 925/Echtsilber/Sterlingsilber -> „versilbert" in bestehenden Listings."""
from __future__ import annotations

import asyncio
from decimal import Decimal

from app.models import Listing, Product
from app.services import precious_metal_service as pms
from app.spec_filter import strip_forbidden_specs


def _listing(db, title, *, specs=None, desc="Edelstahl Schmuck", status="active", item_id=None):
    p = Product(aliexpress_url=f"https://ae/{abs(hash(title)) % 10**9}",
                aliexpress_id=str(abs(hash(title)) % 10**9), price_cny=Decimal("5"))
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku=f"S-{p.id}", title_seo=title, description=desc,
                listing_status=status, price_eur=Decimal("19.95"), cost_eur=Decimal("7"),
                item_specifics=specs, ebay_item_id=item_id)
    db.add(l); db.commit()
    return l


def test_strip_forbidden_specs_material_925_to_versilbert():
    out = strip_forbidden_specs({"Material": "925 Silber", "Herkunftsland": "China", "Farbe": "Silber"})
    assert out["Material"] == "versilbert"         # 925 -> versilbert
    assert "Herkunftsland" not in out              # China weiterhin entfernt
    assert out["Farbe"] == "Silber"                # bloße Farbe „Silber" bleibt


def test_scan_flags_only_forbidden_not_plain_silber(db):
    _listing(db, "925 Sterling Silber Halskette", specs={"Material": "925 Silber"},
             desc="Echtsilber Kette.")
    _listing(db, "Silber Armband Edelstahl Damen", specs={"Material": "Silber"},   # nur „Silber" -> OK
             desc="Silber Armband.")
    d = pms.scan_precious_metal_listings(db)
    assert d["count"] == 1
    r = d["listings"][0]
    assert "versilbert" in r["proposed_title"].lower() and "925" not in r["proposed_title"]
    assert r["proposed_material"] == "versilbert"


def test_apply_fixes_draft_price_untouched(db):
    l = _listing(db, "Echtsilber Ring 925er Damen", specs={"Material": "925 Sterling Silber"},
                 desc="Aus echtem Silber.", status="draft")
    res = asyncio.run(pms.apply_precious_metal_fix(db, listing_ids=[l.id]))
    assert res["n_applied"] == 1 and res["n_failed"] == 0
    db.refresh(l)
    assert "925" not in l.title_seo and "versilbert" in l.title_seo.lower()
    assert l.item_specifics["Material"] == "versilbert"
    assert "925" not in l.description
    assert l.price_eur == Decimal("19.95")


def test_apply_idempotent_second_run_skips(db):
    l = _listing(db, "925 Sterling Silber Kette", specs={"Material": "Silber"},
                 desc="Aus echtem Silber.", status="draft")
    r1 = asyncio.run(pms.apply_precious_metal_fix(db, listing_ids=[l.id]))
    assert r1["n_applied"] == 1
    db.refresh(l)
    t1 = l.title_seo
    r2 = asyncio.run(pms.apply_precious_metal_fix(db, listing_ids=[l.id]))
    assert l.id in r2["skipped"] and r2["n_applied"] == 0
    db.refresh(l)
    assert l.title_seo == t1


def test_regenerate_affected_rewrites_seo_and_clears_claims(db, monkeypatch):
    from app import integrations

    class _LLM:
        async def revise_listing(self, *, instruction, current_title, current_description,
                                 current_specifics, current_category=None):
            return {"title_seo": "Halskette Pferd Anhänger Damen versilbert Geschenk Schmuck",
                    "description": "Elegante versilberte Halskette mit Pferd-Anhänger.\n\nMaße: 45 cm",
                    "item_specifics": {"Material": "versilberter Edelstahl"}, "warnings": []}
    monkeypatch.setattr(integrations, "get_llm_client", lambda: _LLM())

    l = _listing(db, "versilbert Halskette Sterling Damen", specs={"Material": "925 Silber"},
                 desc="Stempel: 925 Sterling. Schön.", status="draft")
    res = asyncio.run(pms.regenerate_affected(db, limit=3, include_drafts=True))
    assert res["n_done"] == 1 and res["n_failed"] == 0
    db.refresh(l)
    assert not l.title_seo.lower().startswith("versilbert")      # SEO: Produkt-Keyword zuerst
    assert "sterling" not in l.title_seo.lower()
    assert "925" not in (l.description or "") and "stempel" not in (l.description or "").lower()


def test_update_listing_live_refuses_stub_description(db):
    """Vorfall 30.07.: Platzhalter '(von eBay importiert)' darf NIE als Beschreibung gepusht
    werden – er würde die echte Live-Beschreibung zerstören. Stub wird ignoriert (nur Titel etc.)."""
    import asyncio as _a
    from app.services.golive_service import update_listing_live
    l = _listing(db, "Testartikel Kette Damen", desc="Echte Beschreibung, lang genug für den Check.",
                 status="draft")
    res = _a.run(update_listing_live(db, listing_id=l.id, title="Neuer Titel Kette Damen",
                                     description="(von eBay importiert)"))
    db.refresh(l)
    assert l.title_seo == "Neuer Titel Kette Damen"
    assert l.description == "Echte Beschreibung, lang genug für den Check."   # Stub NICHT übernommen
    assert "Beschreibung" not in (res.get("changed") or [])


def test_publish_base_sku_strips_grp_suffix():
    """SKU-Falle: ebay_sku kann vom Import auf den Gruppen-Key '…-GRP' verbogen sein – die echten
    Items heißen {base}-V{i}. Basis kommt zuverlässig aus ebay_draft_id ({base}-GRP)."""
    from types import SimpleNamespace
    from app.services.golive_service import _publish_base_sku
    l = SimpleNamespace(id=1, ebay_sku="AE-100-GRP", ebay_draft_id="AE-100-GRP")
    assert _publish_base_sku(l) == "AE-100"
    l2 = SimpleNamespace(id=2, ebay_sku="AE-200", ebay_draft_id="AE-200-GRP")
    assert _publish_base_sku(l2) == "AE-200"
    l3 = SimpleNamespace(id=3, ebay_sku="AE-300", ebay_draft_id=None)
    assert _publish_base_sku(l3) == "AE-300"


def test_count_affected(db):
    _listing(db, "925 Silber Kette", status="active")
    _listing(db, "versilbert Halskette Damen", status="active")   # Ersetzer-Schaden (Titel beginnt so)
    _listing(db, "Silber Armband Edelstahl", status="active")     # nur „Silber" -> nicht betroffen
    assert pms.count_affected(db) == 2


def test_sweep_and_fix_corrects_all_affected(db):
    _listing(db, "925 Silber Kette", specs={"Material": "925"}, status="draft")
    _listing(db, "Sterlingsilber Ring", status="draft")
    _listing(db, "Silber Armband Edelstahl", status="draft")   # nur „Silber" -> nicht betroffen
    res = asyncio.run(pms.sweep_and_fix(db))
    assert res["found"] == 2 and res["n_applied"] == 2
    assert asyncio.run(pms.sweep_and_fix(db))["found"] == 0     # idempotent
