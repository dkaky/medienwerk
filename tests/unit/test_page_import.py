"""Seiten-Import: eine AliExpress-URL im Nischen-Feld wird geerntet statt gesucht.

Sicherheitskern: NUR aliexpress-Domains werden headless gerendert (SSRF-Schutz),
und die geernteten IDs laufen durch die NORMALE Pipeline (inkl. Lokal-Filter).
"""
from __future__ import annotations

from app.services import product_research_service as research


def test_url_detection_only_aliexpress():
    ok = research._is_aliexpress_page_url
    assert ok("https://www.aliexpress.com/ssr/300002243/YM37sZrQ4D") is True
    assert ok("https://de.aliexpress.com/w/wholesale-wasserschuhe.html?shipFromCountry=DE") is True
    assert ok("  HTTPS://best.aliexpress.com/kampagne  ") is True
    assert ok("wasserschuhe badeschuhe") is False
    assert ok("aliexpress.com/ohne-schema") is False
    assert ok("https://evil.com/aliexpress.fake/item/1") is False
    assert ok("https://evil.com/?q=aliexpress.com") is False
    assert ok("") is False
    assert ok(None) is False


def _e(title, ships_from):
    return {"rating": 4.8, "reviews": 120, "status": "onSelling", "sl_product": False,
            "delivery_days": 6, "ships_from": ships_from, "store_name": "S",
            "price_cny": 12.0, "images": [], "title": title, "category_id": "1"}


def _wire(monkeypatch, page_ids, enrich_by_id):
    from app.integrations import aliexpress_store as store
    monkeypatch.setattr(research, "_real_ae", lambda: object())

    async def fake_page_ids(url, *, limit, **_k):  # noqa: ARG001
        return page_ids[:limit]

    async def forbidden_text_search(*_a, **_k):
        raise AssertionError("Bei einer URL-Nische darf KEINE Textsuche laufen")

    async def fake_enrich(_ae, pid):
        return enrich_by_id[pid]

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(store, "fetch_page_product_ids", fake_page_ids)
    monkeypatch.setattr(research, "_text_search", forbidden_text_search)
    monkeypatch.setattr(research, "_enrich", fake_enrich)
    monkeypatch.setattr(research.asyncio, "sleep", no_sleep)


async def test_page_import_harvests_and_filters_local(db, monkeypatch):
    _wire(monkeypatch, ["111", "222", "333"], {
        "111": _e("EU Wasserschuh", ["Polen"]),
        "222": _e("China Wasserschuh", ["CHINA"]),
        # Ein-Lager-Produkt OHNE "Versand aus"-Achse: die Lokal-Seite ist der Beweis
        # (Nutzerfund 10.08. — sonst 0-Ausbeute beim Hub-Import).
        "333": _e("Ohne Achse Wasserschuh", []),
    })
    r = await research.discover(
        db, niches=["https://www.aliexpress.com/ssr/300002243/YM37sZrQ4D"],
        target=10, local_only=True)
    assert r["kept"] == 2

    ideas = research.list_ideas(db)["ideas"]
    assert len(ideas) == 2
    by_title = {i["title"]: i for i in ideas}
    assert set(by_title) == {"EU Wasserschuh", "Ohne Achse Wasserschuh"}
    assert all(i["niche"] == "Seiten-Import" for i in ideas)
    assert all(i["local_warehouse"] is True for i in ideas)


async def test_page_import_failure_does_not_crash_run(db, monkeypatch):
    from app.integrations import aliexpress_store as store

    async def boom(url, **_k):  # noqa: ARG001
        raise store.StoreScrapeError("blockiert")

    monkeypatch.setattr(research, "_real_ae", lambda: object())
    monkeypatch.setattr(store, "fetch_page_product_ids", boom)

    async def no_sleep(_s):
        return None
    monkeypatch.setattr(research.asyncio, "sleep", no_sleep)

    r = await research.discover(
        db, niches=["https://www.aliexpress.com/ssr/kaputt"], target=5)
    assert r["kept"] == 0


async def test_store_scan_pages_get_no_local_trust(db, monkeypatch):
    """Store-Seiten (trust_pages=False) sind KEIN Lokal-Beweis: ohne EU-Achse
    fliegt der Kandidat raus — nur die explizite Achse zaehlt."""
    _wire(monkeypatch, ["111", "333"], {
        "111": _e("EU Produkt", ["Polen"]),
        "333": _e("Ohne Achse", []),
    })
    r = await research.discover(
        db, niches=["https://www.aliexpress.com/store/12345"], target=10,
        local_only=True, use_hub=False, trust_pages=False,
        min_margin=0.0, min_rating=0.0)
    assert r["kept"] == 1
    assert r["local_skipped"] == 1


async def test_use_hub_false_keeps_only_given_page(db, monkeypatch):
    """use_hub=False: die konfigurierte Hub-Seite wird NICHT dazugemischt —
    der Store-Scan erntet ausschliesslich seine eigene Seite."""
    from app.config import get_settings
    from app.integrations import aliexpress_store as store
    monkeypatch.setattr(get_settings(), "local_source_page_url",
                        "https://www.aliexpress.com/ssr/300002243/HUBSEITE")
    monkeypatch.setattr(research, "_real_ae", lambda: object())
    geerntet = []

    async def fake_page_ids(url, *, limit, **_k):  # noqa: ARG001
        geerntet.append(url)
        return []

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(store, "fetch_page_product_ids", fake_page_ids)
    monkeypatch.setattr(research.asyncio, "sleep", no_sleep)
    await research.discover(
        db, niches=["https://www.aliexpress.com/store/12345"], target=5,
        local_only=True, use_hub=False)
    assert geerntet == ["https://www.aliexpress.com/store/12345"]
    # Gegenprobe: mit use_hub=True (Default) kommt die Hub-Seite dazu.
    geerntet.clear()
    await research.discover(
        db, niches=["https://www.aliexpress.com/store/12345"], target=5,
        local_only=True)
    assert "https://www.aliexpress.com/ssr/300002243/HUBSEITE" in geerntet


async def test_store_id_lands_on_idea(db, monkeypatch):
    """Die store_id aus dem Produkt-Detail wird auf der Idee gespeichert
    (Grundlage der Store-Pipeline)."""
    from app.models import ProductIdea
    from sqlalchemy import select
    e = _e("EU Wasserschuh", ["Polen"])
    e["store_id"] = "1102334455"
    _wire(monkeypatch, ["111"], {"111": e})
    r = await research.discover(
        db, niches=["https://www.aliexpress.com/ssr/300002243/X"], target=5,
        local_only=True, min_margin=0.0, min_rating=0.0)
    assert r["kept"] == 1
    idea = db.scalar(select(ProductIdea))
    assert idea.store_id == "1102334455"


async def test_trend_fallback_china_fills_quota(db, monkeypatch, tmp_path):
    """Nutzerwunsch 14.08.: liefert die Lokal-Runde zu wenig, fuellt eine zweite
    Runde mit normalen China-Produkten das Tagesziel auf (gleiche Keywords)."""
    calls = []

    async def fake_discover(_db, *, niches, target, local_only=False, **_k):
        calls.append({"target": target, "local_only": local_only, "niches": niches})
        return {"kept": 2 if local_only else target, "scanned": 10,
                "local_skipped": 0}

    class _FakeLLM:
        async def research_trends(self, *, context, max_terms):  # noqa: ARG002
            return [{"keyword": "gartenzwerg"}, {"keyword": "brotdose"}]

    from app import integrations as _integr
    monkeypatch.setattr(_integr, "get_llm_client", lambda: _FakeLLM())
    monkeypatch.setattr(research, "discover", fake_discover)
    monkeypatch.setattr(research, "_TRENDS_FILE", str(tmp_path / "trends.json"))
    r = await research.discover_trends(db, target=10, local_only=True,
                                       fallback_china=True)
    assert len(calls) == 2
    assert calls[0]["local_only"] is True and calls[0]["target"] == 10
    assert calls[1]["local_only"] is False and calls[1]["target"] == 8
    assert r["kept"] == 10 and r["china_kept"] == 8
    # Ohne fallback_china bleibt es bei EINER Runde (Bestandsverhalten).
    calls.clear()
    r2 = await research.discover_trends(db, target=10, local_only=True)
    assert len(calls) == 1 and r2["kept"] == 2


async def test_product_ids_import_skips_page_and_search(db, monkeypatch):
    """ID-Import (Store-Workaround 14.08.): extern geerntete IDs laufen ohne
    Seiten-Rendern/Textsuche durch die normale Pipeline, ohne Lokal-Vertrauen."""
    from app.integrations import aliexpress_store as store

    async def forbidden_page(*_a, **_k):
        raise AssertionError("ID-Import darf KEINE Seite rendern")

    async def forbidden_search(*_a, **_k):
        raise AssertionError("ID-Import darf KEINE Textsuche machen")

    async def fake_enrich(_ae, pid):
        return _e(f"Produkt {pid}", ["Polen"] if pid == "111" else [])

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(research, "_real_ae", lambda: object())
    monkeypatch.setattr(store, "fetch_page_product_ids", forbidden_page)
    monkeypatch.setattr(research, "_text_search", forbidden_search)
    monkeypatch.setattr(research, "_enrich", fake_enrich)
    monkeypatch.setattr(research.asyncio, "sleep", no_sleep)
    r = await research.discover(
        db, product_ids=["111", "222"], target=10, local_only=False,
        min_margin=0.0, min_rating=0.0)
    # local_only=False: beide bleiben (China-Kalkulation fuer 222, lokal fuer 111)
    assert r["kept"] == 2


async def test_unrated_zero_rating_passes_bad_rating_drops(db, monkeypatch):
    """Bewertung 0.0 mit 0 Rezensionen = unbewertet (passiert wie None);
    echte Schlecht-Bewertung (Rezensionen > 0) faellt weiter durch."""
    async def fake_enrich(_ae, pid):
        e = _e(f"Produkt {pid}", ["Polen"])
        if pid == "111":                 # brandneu: 0.0 ohne Rezensionen
            e["rating"] = 0.0
            e["reviews"] = 0
        else:                            # wirklich schlecht: 2.5 bei 30 Rezensionen
            e["rating"] = 2.5
            e["reviews"] = 30
        return e

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(research, "_real_ae", lambda: object())
    monkeypatch.setattr(research, "_enrich", fake_enrich)
    monkeypatch.setattr(research.asyncio, "sleep", no_sleep)
    r = await research.discover(
        db, product_ids=["111", "222"], target=10, local_only=False,
        min_margin=0.0, min_rating=4.0)
    assert r["kept"] == 1
    assert r["drops"]["rating"] == 1
