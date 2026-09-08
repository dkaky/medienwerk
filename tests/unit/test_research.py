"""Tests für die Produkt-Research-Filter (ohne Netz – _text_search/_enrich gemockt)."""
from __future__ import annotations

import asyncio

from app.models import ProductIdea
from app.services import product_research_service as r


async def _noop(*a, **k):
    return None


def test_text_search_uses_relevance_sort_by_default():
    """RELEVANZ statt Bestseller: ohne sortBy liefert die API die Beste-Ergebnisse-
    Reihenfolge. 'orders,desc' hatte thematisch falsche Hochvolumen-Treffer hochgespuelt
    (Bug 'kinesio tapes'). Bestseller-Sortierung bleibt per sort= moeglich."""
    captured: dict = {}

    class FakeAE:
        async def _call(self, method, params):
            captured["method"] = method
            captured["params"] = dict(params)
            return {"resp": {"data": {"products": {"selection_search_product": []}}}}

    ae = FakeAE()
    asyncio.run(r._text_search(ae, "kinesio tapes"))
    assert captured["method"] == "aliexpress.ds.text.search"
    assert "sortBy" not in captured["params"]            # Default = Relevanz
    assert captured["params"]["keyWord"] == "kinesio tapes"
    asyncio.run(r._text_search(ae, "x", sort="orders,desc"))
    assert captured["params"]["sortBy"] == "orders,desc"  # explizit weiterhin moeglich


def test_discover_applies_hard_filters(db, monkeypatch):
    """Kriterien ab 10.07.: ALLE Preise/Kategorien, Bedingung = Marge >= 25 %, dazu
    Rating > 4 und Lieferzeit <= 10. Der starre VK-/Gewinn-Zwang (20 €/8 €) ist weg."""
    candidates = [
        {"id": "1", "title": "Guter Kandidat", "image": "i", "score": 4.6, "orders": 5000, "price": 6.0, "url": "u1"},
        {"id": "2", "title": "Schlechtes Rating", "image": "i", "score": 3.5, "orders": 10, "price": 6.0, "url": "u2"},
        {"id": "3", "title": "Zu langsam", "image": "i", "score": 4.8, "orders": 100, "price": 6.0, "url": "u3"},
        {"id": "4", "title": "Sehr billig", "image": "i", "score": 4.9, "orders": 100, "price": 1.0, "url": "u4"},
    ]
    enrich = {
        "1": {"rating": 4.7, "reviews": 100, "status": "onSelling", "sl_product": True,
              "delivery_days": 7, "store_name": "S", "price_cny": 6.0, "images": ["i"],
              "title": "Guter Kandidat", "category_id": "10"},
        "3": {"rating": 4.8, "reviews": 10, "status": "onSelling", "sl_product": False,
              "delivery_days": 20, "store_name": "S", "price_cny": 6.0, "images": ["i"],
              "title": "Zu langsam", "category_id": "10"},
        "4": {"rating": 4.9, "reviews": 10, "status": "onSelling", "sl_product": False,
              "delivery_days": 5, "store_name": "S", "price_cny": 1.0, "images": ["i"],
              "title": "Sehr billig", "category_id": "10"},
    }

    async def fake_search(ae, kw, **k):
        return candidates

    async def fake_enrich(ae, pid):
        return enrich[pid]

    monkeypatch.setattr(r, "_real_ae", lambda: object())
    monkeypatch.setattr(r, "_text_search", fake_search)
    monkeypatch.setattr(r, "_enrich", fake_enrich)
    monkeypatch.setattr(r.asyncio, "sleep", _noop)

    res = asyncio.run(r.discover(db, niches=["test"], target=100))
    # Kandidat 1 (normal) UND 4 (sehr billig) bleiben – beide mit >=25 % Marge kalkulierbar.
    # 2 fliegt am Rating-Vorfilter raus, 3 an der Lieferzeit.
    assert res["kept"] == 2
    ideas = db.query(ProductIdea).all()
    assert sorted(i.aliexpress_id for i in ideas) == ["1", "4"]
    for idea in ideas:
        assert idea.margin_pct >= 0.20 - 1e-9          # Bedingung erfüllt (Nutzerregel 02.08.: 20 %)
        assert idea.rating >= 4.0 and idea.delivery_days <= 10


def test_strip_forbidden_blocks_removes_warranty_and_service_hours():
    from app.integrations.llm import strip_forbidden_blocks
    text = ("🔧 Tolles Produkt\nBeschreibung hier.\n\n"
            "✅ 12 Monate Garantie & Authentizität\nKundenservice Mo–Sa 9–18 Uhr (GMT+7).\n\n"
            "📦 Lieferumfang\n1x Artikel")
    w: list[str] = []
    out = strip_forbidden_blocks(text, w)
    assert "Garantie" not in out and "GMT" not in out
    assert "Tolles Produkt" in out and "Lieferumfang" in out
    assert w  # Warnung erzeugt
    # Footer/normale Texte bleiben unangetastet
    ok = "📩 Fragen? Unser Kundenservice hilft jederzeit gerne."
    assert strip_forbidden_blocks(ok, []) == ok


def test_list_and_set_status(db, monkeypatch):
    db.add(ProductIdea(aliexpress_id="X1", title="A", price_eur=25, profit_eur=9, status="new"))
    db.commit()
    lst = r.list_ideas(db, status="new")
    assert lst["count"] == 1 and lst["total"] == 1
    idea_id = lst["ideas"][0]["id"]
    r.set_status(db, idea_id=idea_id, status="kept")
    assert r.list_ideas(db, status="kept")["count"] == 1
    assert r.list_ideas(db, status="new")["count"] == 0


def test_discover_max_cost_filter(db, monkeypatch):
    """EK-Spanne: Kandidaten oberhalb max_cost werden aussortiert."""
    candidates = [
        {"id": "10", "title": "Guenstig", "image": "i", "score": 4.6, "orders": 500, "price": 6.0, "url": "u"},
        {"id": "11", "title": "Teuer", "image": "i", "score": 4.6, "orders": 500, "price": 40.0, "url": "u"},
    ]
    enrich = {
        pid: {"rating": 4.7, "reviews": 100, "status": "onSelling", "sl_product": True,
              "delivery_days": 7, "store_name": "S", "price_cny": p, "images": ["i"],
              "title": t, "category_id": "10"}
        for pid, p, t in (("10", 6.0, "Guenstig"), ("11", 40.0, "Teuer"))
    }

    async def fake_search(ae, kw, **k):
        return candidates

    async def fake_enrich(ae, pid):
        return enrich[pid]

    from app.config import get_settings
    # EK-Spannen-Logik isoliert: EK-Aufschlaege (Prozent + Pauschalzoll) neutralisieren.
    monkeypatch.setattr(get_settings(), "aliexpress_tax_pct", 0.0)
    monkeypatch.setattr(get_settings(), "customs_fee_eur", 0.0)
    monkeypatch.setattr(r, "_real_ae", lambda: object())
    monkeypatch.setattr(r, "_text_search", fake_search)
    monkeypatch.setattr(r, "_enrich", fake_enrich)
    monkeypatch.setattr(r.asyncio, "sleep", _noop)

    res = asyncio.run(r.discover(db, niches=["test"], target=10, max_cost=10.0))
    ideas = db.query(ProductIdea).all()
    assert res["kept"] == 1 and len(ideas) == 1
    assert ideas[0].aliexpress_id == "10"


def test_discover_customs_cap_skips_over_140(db, monkeypatch):
    """HARTER Zoll-Deckel (max_source_cost_eur=140): Artikel mit EK > 140 € werden IMMER
    aussortiert – auch OHNE gesetzte EK-Spanne. Schutz vor der 150-€-Zollanmeldung."""
    candidates = [
        {"id": "ok", "title": "Bezahlbar", "image": "i", "score": 4.7, "orders": 500, "price": 50.0, "url": "u"},
        {"id": "zoll", "title": "Zu teuer fuer Zoll", "image": "i", "score": 4.7, "orders": 500, "price": 200.0, "url": "u"},
    ]
    enrich = {
        pid: {"rating": 4.7, "reviews": 100, "status": "onSelling", "sl_product": True,
              "delivery_days": 7, "store_name": "S", "price_cny": p, "images": ["i"],
              "title": t, "category_id": "10"}
        for pid, p, t in (("ok", 50.0, "Bezahlbar"), ("zoll", 200.0, "Zu teuer fuer Zoll"))
    }
    _mk_disc(monkeypatch, candidates, enrich)
    # KEINE EK-Spanne gesetzt -> allein der Zoll-Deckel greift.
    res = asyncio.run(r.discover(db, niches=["test"], target=10))
    ideas = db.query(ProductIdea).all()
    assert res["kept"] == 1 and [i.aliexpress_id for i in ideas] == ["ok"]
    assert float(ideas[0].cost_eur) <= 140.0


def test_list_ideas_newest_first_default(db):
    """Neueste Funde stehen oben (Default), 'profit' liefert die alte Reihenfolge."""
    db.add(ProductIdea(aliexpress_id="A", title="Alt, hoher Gewinn", price_eur=30, profit_eur=15, status="new"))
    db.commit()
    db.add(ProductIdea(aliexpress_id="B", title="Neu, kleiner Gewinn", price_eur=25, profit_eur=9, status="new"))
    db.commit()
    newest = [i["aliexpress_id"] for i in r.list_ideas(db)["ideas"]]
    assert newest == ["B", "A"]
    by_profit = [i["aliexpress_id"] for i in r.list_ideas(db, sort="profit")["ideas"]]
    assert by_profit == ["A", "B"]


def test_discover_endpoint_parses_user_criteria(client, monkeypatch):
    """Router: Komma-String -> Nischen-Liste, EK-Spanne wird durchgereicht + sortiert."""
    captured = {}

    async def fake_discover(db, **kw):
        captured.update(kw)
        return {"kept": 0, "scanned": 0, "target": kw.get("target"), "niches": 0}

    from app.routers import research as research_router
    monkeypatch.setattr(research_router.research, "discover", fake_discover)

    resp = client.post("/api/v1/research/discover", json={
        "target": "15", "niches": "camping gadget, edelstahl schmuck ,",
        "min_cost": "12,50", "max_cost": 5,
    })
    assert resp.status_code == 202
    body = resp.json()
    assert body["target"] == 15 and body["niches"] == 2
    # min > max -> wird getauscht
    assert body["min_cost"] == 5.0 and body["max_cost"] == 12.5
    assert captured["niches"] == ["camping gadget", "edelstahl schmuck"]
    assert captured["min_cost"] == 5.0 and captured["max_cost"] == 12.5


def test_discover_endpoint_base_criteria(client, monkeypatch):
    """Basis-Kriterien: gesetzt -> durchgereicht; leer/fehlend -> bewaehrte Defaults."""
    captured = {}

    async def fake_discover(db, **kw):
        captured.update(kw)
        return {"kept": 0, "scanned": 0, "target": kw.get("target"), "niches": 0}

    from app.routers import research as research_router
    monkeypatch.setattr(research_router.research, "discover", fake_discover)

    resp = client.post("/api/v1/research/discover", json={
        "target": 10, "min_profit": "10,5", "min_rating": "", "max_delivery": 15,
    })
    assert resp.status_code == 202
    assert captured["min_profit"] == 10.5
    assert captured["min_rating"] == 4.0      # leer -> Default
    assert captured["max_delivery"] == 15
    assert captured["min_price"] == 0.0       # fehlt -> Default (kein VK-Zwang mehr)
    assert captured["min_margin"] == 0.20     # Default-Marge-Bedingung (20 % ODER mind. 4 EUR)
    # min_profit=0 ist erlaubt (Filter aus), Rating wird auf 5 gedeckelt
    captured.clear()
    resp = client.post("/api/v1/research/discover", json={"min_profit": 0, "min_rating": 9})
    assert resp.status_code == 202
    assert captured["min_profit"] == 0.0 and captured["min_rating"] == 5.0


def test_create_from_idea_custom_profit(db, monkeypatch):
    """Individueller Gewinn: Listing bekommt min_profit_eur + niedrigerer Preis."""
    import asyncio
    from app.models import Listing, Product, ProductIdea
    from app.services import product_research_service as prs

    idea = ProductIdea(aliexpress_id="CP1", aliexpress_url="https://de.aliexpress.com/item/cp1.html",
                       title="Test", price_eur=25, profit_eur=8, status="new")
    db.add(idea); db.commit()

    async def fake_upload(db_, *, aliexpress_url, skip_autods=False):
        p = Product(aliexpress_url=aliexpress_url, aliexpress_id="CP1", price_cny=5.0)
        db_.add(p); db_.flush()
        l = Listing(product_id=p.id, ebay_sku="AE-CP1", title_seo="T", description="d",
                    listing_status="draft", price_eur=20.0, cost_eur=5.0)
        db_.add(l); db_.commit()
        return {"listing_id": l.id}

    async def fake_publish(db_, *, listing_id, draft_only=False):
        return {"status": "draft_created", "ebay_item_id": None}

    from app.services import golive_service, product_service
    monkeypatch.setattr(product_service, "upload_product", fake_upload)
    monkeypatch.setattr(golive_service, "publish_listing_live", fake_publish)

    r = asyncio.run(prs.create_from_idea(db, idea_id=idea.id, publish=False, min_profit_eur=4.0))
    listing = db.get(Listing, r["listing_id"])
    assert float(listing.min_profit_eur) == 4.0
    # Preis mit 4€ Gewinn < Preis mit Standard 8€
    from app.services import pricing
    from app.config import get_settings
    p8 = pricing.price_from_cny(5.0, settings=get_settings(), min_profit_eur=8.0).rounded_price_eur
    assert float(listing.price_eur) < p8


def test_discover_trends_researches_and_searches(db, monkeypatch, tmp_path):
    """Trend-Recherche: KI-Keywords -> discover() + Trend-Datei geschrieben."""
    import asyncio
    import json
    from app.services import product_research_service as prs

    called = {}
    async def fake_discover(db_, *, niches, **kw):
        called["niches"] = list(niches)
        return {"kept": 3, "scanned": 8, "target": kw.get("target"), "niches": len(niches)}

    monkeypatch.setattr(prs, "discover", fake_discover)
    monkeypatch.setattr(prs, "_TRENDS_FILE", str(tmp_path / "trends.json"))

    r = asyncio.run(prs.discover_trends(db, target=10))
    # MockLLMClient.research_trends liefert feste Beispiel-Trends
    assert len(r["trends"]) >= 1 and r["kept"] == 3
    assert called["niches"]                      # discover mit Trend-Keywords aufgerufen
    saved = json.loads((tmp_path / "trends.json").read_text(encoding="utf-8"))
    assert saved["terms"] and saved["generated_at"]
    # last_trends liest die Datei
    assert prs.last_trends()["terms"] == saved["terms"]


def test_run_lock_shared_and_blocks_second_start(db, monkeypatch):
    """Geteilter Lauf-Lock: zweiter Start prallt ab; Scheduler nutzt DENSELBEN Lock."""
    from app.routers import research as rr
    from app.services import product_research_service as prs

    prs.release_run()   # sauberer Start
    assert prs.try_acquire_run() is True
    assert prs.is_running() is True
    # zweiter manueller Start prallt ab (Lock schon gehalten)
    resp = client_post_discover(monkeypatch)
    assert resp["status"] == "running"
    prs.release_run()
    assert prs.is_running() is False


def client_post_discover(monkeypatch):
    """Hilfsfunktion: /discover mit gehaltenem Lock -> muss 'running' liefern."""
    from app.routers import research as rr

    class _BG:
        def add_task(self, *a, **k):
            raise AssertionError("darf nicht eingereiht werden, wenn Lock gehalten")
    return rr.discover(_BG(), body={}, db=None)


def test_discover_handler_acquires_lock_before_add_task(db, monkeypatch):
    """Der Lock wird VOR add_task gesetzt (kein Race-Fenster)."""
    from app.routers import research as rr
    from app.services import product_research_service as prs

    prs.release_run()
    order = []

    class _BG:
        def add_task(self, *a, **k):
            order.append(("add_task", prs.is_running()))

    r = rr.discover(_BG(), body={"target": 5}, db=None)
    assert r["status"] == "started"
    # bei add_task war der Lock bereits aktiv
    assert order == [("add_task", True)]
    prs.release_run()   # Cleanup (Task lief hier nie)


def test_list_ideas_niche_filter(db):
    """Nische-Filter: nur Ideen des Trend-Begriffs (Klick auf Trend-Chip)."""
    db.add(ProductIdea(aliexpress_id="T1", title="WM Fanpaket", price_eur=25, profit_eur=9,
                       niche="WM 2026 Fanpaket Deutschland", status="new"))
    db.add(ProductIdea(aliexpress_id="X1", title="Camping", price_eur=25, profit_eur=9,
                       niche="camping gadget", status="new"))
    db.commit()
    only = r.list_ideas(db, niche="WM 2026 Fanpaket Deutschland")
    assert only["count"] == 1 and only["ideas"][0]["aliexpress_id"] == "T1"
    assert r.list_ideas(db)["count"] == 2   # ohne Filter alle


# ------------------------- Elektronik-Deemphasis (10.07.) -------------------------
def test_is_electronic_helper():
    """Konservative Elektronik-Erkennung: klare Elektronik True, Zubehör/Deko False."""
    assert r._is_electronic("Bluetooth Kopfhörer Sport") is True
    assert r._is_electronic("Powerbank 20000mAh USB") is True
    assert r._is_electronic("LED Strip RGB Controller App") is True
    assert r._is_electronic("Edelstahl Halskette Herren") is False
    assert r._is_electronic("Handyhülle Silikon Case") is False       # Zubehör, nicht elektrisch
    assert r._is_electronic("LED Deko Lampe Tischdeko") is False      # Deko-Lampe bleibt
    assert r._is_electronic("") is False


def test_is_electronic_strikt_19_08():
    """Reale Durchrutscher vom 19.08. muessen erkannt werden (Nutzer: 'Bitte strikt
    keine elektroartikel mehr importieren') — Zubehoer/Schmuck bleibt erlaubt."""
    elektro = [
        "HiBREW Kaffeemaschine Cafetera 20-Bar Inox Halbautomatisch",
        "Hibrew 20bar halbautomat ische Espresso maschine Temperatur einstellbar",
        "10L Heißluftfritteuse 1800W, Ölfrei, Touchscreen",
        "SONOFF SNZB-02DR2 Zigbee Temperatur- & Feuchtigkeitssensor LCD",
        "Handgehaltener Dampfreiniger mit Hoher Temperatur",
        "2026 Neuestes VCDS HEX V2 OBD2 Scanner-Kabel Diagnosegerät",
        "WLtoys 2,4GHz RC-Auto 1:16 Offroad-Monstertruck",
        "Neon-Lichterkette flexible LED-Lichterkette mit App/Fernbedienung",
        "Anker SOLIX Smarter Zähler Smart Meter 3 Phasen",
        "Seesii Mini-Punktschweißgerät tragbar",
        "EAFC Auto Polierer Handheld Drahtlose Polierer",
        "Intelligenter Deckenventilator mit LED-Licht",
        "2025 Frauen Uhr Silikon Band Quarzuhr Sport Armbanduhr",
        "Schmuckbox mit Beleuchtung Geschenkverpackung",
        "Unbekanntes Geraet 1800W Profi",              # Muster: Watt-Angabe
        "Mini Modul 12V Auto",                        # Muster: Volt-Angabe
    ]
    for titel in elektro:
        assert r._is_electronic(titel) is True, titel
    kein_elektro = [
        "Magnetischer Fensterreiniger doppelseitig Glas",
        "Kabelorganisator Magnetische Kabelclips Kabelhalter",
        "Kompressions-Verpackungswürfel Netz-Reisetasche Gepäck-Organizer",
        "Uhrenarmband Leder mit Holzkiste Schmetterlingsverschluss",
        "20G Flache Ohrstecker Hypoallergen für Knorpel Helix",
        "Anker-Armband Edelstahl Herren maritim",     # Schmuck-Anker bleibt erlaubt
        "500ML Edelstahl Kaffee-Thermoskanne auslaufsicher",
        "Magnetischer Gewürzständer für Kühlschrank mit Haken",
        "Pfannenset für Induktion beschichtet",
        "Lavendel Massageöl Körperpflege 100ml",
        "Kleid Damen Sommer V-Ausschnitt Gr. 38",     # kein Volt-Fehltreffer
    ]
    for titel in kein_elektro:
        assert r._is_electronic(titel) is False, titel


def test_is_electronic_zubehoer_nennt_geraet_nur_als_bezug():
    """Nintendo-Fall 19.08. ('Hülle für Switch OLED' brachte 0 Ideen): Zubehoer,
    das sein Geraet nur BENENNT, ist kein Elektroartikel — Geraete selbst und
    harte Elektro-Woerter bleiben strikt geblockt."""
    zubehoer = [
        "Anime Schutzhülle für Nintendo Switch OLED Hardcase",
        "Aufbewahrungstasche für Nintendo Switch-Konsole, Tragetasche Switch OLED",
        "Hülle für Nintendo Switch mit Ladestation Ausschnitt",
        "Armband für Samsung Galaxy Smartwatch 20mm Silikon",
        "Laptop Tasche 15 Zoll wasserdicht gepolstert",
        "Silikonhülle für TV-Fernbedienung stoßfest",
        "Kopfhörer Hülle für Earbuds mit Karabiner",
    ]
    for titel in zubehoer:
        assert r._is_electronic(titel) is False, titel
    weiterhin_elektro = [
        "OLED Monitor 27 Zoll Gaming",                 # Geraet selbst
        "Tablet 10 Zoll Android 14",
        "Hülle mit 5000mAh Akku für iPhone",           # Zubehoer MIT Akku = Elektro
        "Ladestation 3-in-1 für iPhone und Watch",     # kein Zubehoer-Wort
        "Bluetooth Kopfhörer Sport in-ear",            # hartes Elektro-Wort
    ]
    for titel in weiterhin_elektro:
        assert r._is_electronic(titel) is True, titel


def _mk_disc(monkeypatch, candidates, enrich):
    async def fake_search(ae, kw, **k):
        return candidates

    async def fake_enrich(ae, pid):
        return enrich[pid]

    monkeypatch.setattr(r, "_real_ae", lambda: object())
    monkeypatch.setattr(r, "_text_search", fake_search)
    monkeypatch.setattr(r, "_enrich", fake_enrich)
    monkeypatch.setattr(r.asyncio, "sleep", _noop)


def test_discover_skips_electronics(db, monkeypatch):
    """avoid_electronics=True: klar elektronische Kandidaten fliegen raus (früh am Suchtitel),
    Nicht-Elektronik bleibt."""
    candidates = [
        {"id": "e1", "title": "Bluetooth Kopfhörer Sport", "image": "i", "score": 4.7, "orders": 900, "price": 12.0, "url": "u"},
        {"id": "n1", "title": "Edelstahl Küchenreibe Set", "image": "i", "score": 4.7, "orders": 900, "price": 12.0, "url": "u"},
    ]
    enrich = {  # e1 wird schon am Suchtitel aussortiert -> enrich nie nötig
        "n1": {"rating": 4.7, "reviews": 50, "status": "onSelling", "sl_product": True,
               "delivery_days": 7, "store_name": "S", "price_cny": 12.0, "images": ["i"],
               "title": "Edelstahl Küchenreibe Set", "category_id": "10"},
    }
    _mk_disc(monkeypatch, candidates, enrich)
    res = asyncio.run(r.discover(db, niches=["test"], target=10, avoid_electronics=True))
    assert [i.aliexpress_id for i in db.query(ProductIdea).all()] == ["n1"]
    assert res["electronics_skipped"] == 1


def test_discover_electronics_filter_can_be_disabled(db, monkeypatch):
    """avoid_electronics=False: Elektronik bleibt, wenn sie die harten Kriterien erfüllt."""
    candidates = [
        {"id": "e2", "title": "Bluetooth Lautsprecher Mini", "image": "i", "score": 4.7, "orders": 900, "price": 12.0, "url": "u"},
    ]
    enrich = {
        "e2": {"rating": 4.7, "reviews": 50, "status": "onSelling", "sl_product": True,
               "delivery_days": 7, "store_name": "S", "price_cny": 12.0, "images": ["i"],
               "title": "Bluetooth Lautsprecher Mini", "category_id": "10"},
    }
    _mk_disc(monkeypatch, candidates, enrich)
    res = asyncio.run(r.discover(db, niches=["test"], target=10, avoid_electronics=False))
    assert res["kept"] == 1 and res["electronics_skipped"] == 0
    assert db.query(ProductIdea).count() == 1


def test_discover_prefilters_cost_band_before_enrich(db, monkeypatch):
    """EK-Spanne: klar zu billige/teure Treffer werden schon am Such-Preis übersprungen,
    OHNE den teuren Detail-Call (spart Zeit/Quota bei einer 15–40€-EK-Suche)."""
    candidates = [
        {"id": "cheap", "title": "Billiger Krimskrams", "image": "i", "score": 4.7, "orders": 900, "price": 3.0, "url": "u"},
        {"id": "band", "title": "Edelstahl Werkzeugkoffer Set", "image": "i", "score": 4.7, "orders": 900, "price": 25.0, "url": "u"},
    ]
    enriched = []

    async def fake_search(ae, kw, **k):
        return candidates

    async def fake_enrich(ae, pid):
        enriched.append(pid)
        if pid == "cheap":
            raise AssertionError("zu billiger Kandidat darf nicht angereichert werden")
        return {"rating": 4.7, "reviews": 50, "status": "onSelling", "sl_product": True,
                "delivery_days": 7, "store_name": "S", "price_cny": 25.0, "images": ["i"],
                "title": "Edelstahl Werkzeugkoffer Set", "category_id": "10"}

    monkeypatch.setattr(r, "_real_ae", lambda: object())
    monkeypatch.setattr(r, "_text_search", fake_search)
    monkeypatch.setattr(r, "_enrich", fake_enrich)
    monkeypatch.setattr(r.asyncio, "sleep", _noop)

    res = asyncio.run(r.discover(db, niches=["test"], target=10, min_cost=15.0, max_cost=40.0))
    assert enriched == ["band"]                     # 'cheap' wurde NIE angereichert
    assert [i.aliexpress_id for i in db.query(ProductIdea).all()] == ["band"]
    idea = db.query(ProductIdea).first()
    assert 15.0 <= float(idea.cost_eur) <= 40.0     # EK im gewünschten Band


# ------------------------- Hintergrund-Import (Entwurf-Bug 10.07.) -------------------------
def test_begin_import_marks_importing_and_is_idempotent(db, monkeypatch):
    """Klick reiht im Hintergrund ein + markiert 'importing' SOFORT; ein zweiter Klick
    während 'importing'/'imported' startet KEINEN zweiten Lauf (keine Doppel-Anlage)."""
    calls = []
    monkeypatch.setattr(r, "_schedule_import", lambda *a, **k: calls.append(a))
    idea = ProductIdea(aliexpress_id="BI1", aliexpress_url="https://de.aliexpress.com/item/bi1.html",
                       title="T", status="new")
    db.add(idea); db.commit()
    res = r.begin_import(db, idea_id=idea.id, publish=False)
    assert res["status"] == "importing" and not res.get("already")
    db.refresh(idea); assert idea.status == "importing"
    assert len(calls) == 1
    res2 = r.begin_import(db, idea_id=idea.id, publish=False)
    assert res2["already"] is True and len(calls) == 1     # kein zweiter Lauf


async def test_run_import_records_failure_not_lost(db, monkeypatch):
    """Scheitert der Import, wird der Fehler am Datensatz vermerkt (status=import_failed
    + Klartext) – nichts verschwindet stillschweigend, der Nutzer kann erneut anklicken."""
    idea = ProductIdea(aliexpress_id="RI1", aliexpress_url="https://de.aliexpress.com/item/ri1.html",
                       title="T", status="importing")
    db.add(idea); db.commit()

    async def boom(db_, *, idea_id, publish=False, min_profit_eur=None):
        raise RuntimeError("scrape kaputt")

    monkeypatch.setattr(r, "create_from_idea", boom)
    await r._run_import(idea.id, False, None)
    db.expire_all(); db.refresh(idea)
    assert idea.status == "import_failed"
    assert "scrape kaputt" in (idea.import_error or "")


async def test_run_import_success_marks_imported(db, monkeypatch):
    idea = ProductIdea(aliexpress_id="RI2", aliexpress_url="https://de.aliexpress.com/item/ri2.html",
                       title="T", status="importing")
    db.add(idea); db.commit()

    async def ok(db_, *, idea_id, publish=False, min_profit_eur=None):
        it = db_.get(ProductIdea, idea_id); it.status = "imported"; it.import_error = None; db_.commit()
        return {"listing_id": 1, "status": "draft"}

    monkeypatch.setattr(r, "create_from_idea", ok)
    await r._run_import(idea.id, False, None)
    db.expire_all(); db.refresh(idea)
    assert idea.status == "imported"


def test_list_ideas_new_filter_includes_in_progress(db):
    """'offen' zeigt auch laufende UND fehlgeschlagene Importe (sonst verschwinden angeklickte
    Ideen bzw. Fehler unsichtbar); erledigte NICHT."""
    db.add_all([
        ProductIdea(aliexpress_id="s_new", title="neu", status="new"),
        ProductIdea(aliexpress_id="s_imp", title="läuft", status="importing"),
        ProductIdea(aliexpress_id="s_fail", title="fehler", status="import_failed", import_error="x"),
        ProductIdea(aliexpress_id="s_done", title="fertig", status="imported"),
    ]); db.commit()
    ids = {x["aliexpress_id"] for x in r.list_ideas(db, status="new")["ideas"]}
    assert ids == {"s_new", "s_imp", "s_fail"}


def test_create_from_idea_draft_is_local_only(db, monkeypatch):
    """Entwurf (publish=False) macht KEINEN eBay-Call – nur ein lokaler Draft-Listing
    (Nutzerwunsch 10.07.: online erst bei „Live"). Verhindert die minutenlangen eBay-
    Transaktionen, die parallele Anlagen aussperrten."""
    import asyncio
    from app.models import Listing, Product, ProductIdea
    from app.services import product_research_service as prs, golive_service, product_service

    idea = ProductIdea(aliexpress_id="DL1", aliexpress_url="https://de.aliexpress.com/item/dl1.html",
                       title="T", status="importing")
    db.add(idea); db.commit()

    async def fake_upload(db_, *, aliexpress_url, skip_autods=False):
        p = Product(aliexpress_url=aliexpress_url, aliexpress_id="DL1", price_cny=5.0)
        db_.add(p); db_.flush()
        l = Listing(product_id=p.id, ebay_sku="AE-DL1", title_seo="T", description="d",
                    listing_status="draft", price_eur=20.0, cost_eur=5.0)
        db_.add(l); db_.commit()
        return {"listing_id": l.id}

    called = {"publish": False}

    async def fake_publish(*a, **k):
        called["publish"] = True
        return {"status": "draft"}

    monkeypatch.setattr(product_service, "upload_product", fake_upload)
    monkeypatch.setattr(golive_service, "publish_listing_live", fake_publish)

    res = asyncio.run(prs.create_from_idea(db, idea_id=idea.id, publish=False))
    assert called["publish"] is False              # KEIN eBay-Call für den Entwurf
    assert res.get("draft_only") is True
    db.expire_all(); db.refresh(idea)
    assert idea.status == "imported"
    listing = db.get(Listing, res["listing_id"])
    assert listing.listing_status == "draft"       # nur lokaler Entwurf


async def test_run_import_retries_transient_lock(db, monkeypatch):
    """Voruebergehende DB-Sperre -> Retry statt Fehlschlag (nichts geht verloren)."""
    idea = ProductIdea(aliexpress_id="RT1", aliexpress_url="https://de.aliexpress.com/item/rt1.html",
                       title="T", status="importing")
    db.add(idea); db.commit()
    attempts = {"n": 0}

    async def flaky(db_, *, idea_id, publish=False, min_profit_eur=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("(sqlite3.OperationalError) database is locked")
        it = db_.get(ProductIdea, idea_id); it.status = "imported"; db_.commit()

    monkeypatch.setattr(r, "create_from_idea", flaky)
    monkeypatch.setattr(r.asyncio, "sleep", _noop)
    await r._run_import(idea.id, False, None)
    assert attempts["n"] == 2
    db.expire_all(); db.refresh(idea)
    assert idea.status == "imported"


# ---------------- Hängende Importe: Zombie-Recovery (Vorfall 11.07.) ----------------
def _mk_importing(db, aeid):
    idea = ProductIdea(aliexpress_id=aeid, aliexpress_url=f"https://de.aliexpress.com/item/{aeid}.html",
                       title="X", status="importing")
    db.add(idea); db.commit()
    return idea.id


def _age_updated_at(db, idea_id, **delta):
    """updated_at per Roh-SQL zurückdatieren (umgeht onupdate=func.now())."""
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import text
    old = datetime.now(timezone.utc) - timedelta(**delta)
    db.execute(text("UPDATE product_ideas SET updated_at=:t WHERE id=:i"), {"t": old, "i": idea_id})
    db.commit(); db.expire_all()


def test_begin_import_blocks_fresh_but_reruns_stale_zombie(db, monkeypatch):
    scheduled = []
    monkeypatch.setattr(r, "_schedule_import", lambda idea_id, publish, mp: scheduled.append(idea_id))
    iid = _mk_importing(db, "II1")
    r._importing_ids.discard(iid)
    # frisch importing -> blockt (kein Doppelstart)
    assert r.begin_import(db, idea_id=iid)["already"] is True and scheduled == []
    # 40 min alt + kein lebender Task hier -> Zombie -> neu einreihen
    _age_updated_at(db, iid, minutes=40)
    res = r.begin_import(db, idea_id=iid)
    assert res["already"] is False and scheduled == [iid]


def test_begin_import_force_reruns_but_never_when_running(db, monkeypatch):
    scheduled = []
    monkeypatch.setattr(r, "_schedule_import", lambda idea_id, publish, mp: scheduled.append(idea_id))
    iid = _mk_importing(db, "II2")
    r._importing_ids.discard(iid)
    # force -> auch bei frischem Status neu starten (manuelles „↻ erneut")
    assert r.begin_import(db, idea_id=iid, force=True)["already"] is False
    assert scheduled == [iid]
    # aber: läuft NACHWEISLICH in diesem Prozess -> nie doppelt, auch mit force
    scheduled.clear(); r._importing_ids.add(iid)
    try:
        assert r.begin_import(db, idea_id=iid, force=True)["already"] is True
        assert scheduled == []
    finally:
        r._importing_ids.discard(iid)


def test_recover_stuck_imports_requeues_recent_fails_hard_stuck(db, monkeypatch):
    scheduled = []
    monkeypatch.setattr(r, "_schedule_import", lambda idea_id, publish, mp: scheduled.append((idea_id, publish)))
    recent = _mk_importing(db, "RS1")     # frisch-ish, Entwurf -> requeue publish=False
    old = _mk_importing(db, "RS2")        # uralt -> import_failed
    _age_updated_at(db, old, hours=8)     # > HARD_STUCK_HOURS
    r._importing_ids.discard(recent); r._importing_ids.discard(old)
    out = r.recover_stuck_imports()
    assert out == {"requeued": 1, "failed": 1}
    assert (recent, False) in scheduled            # Entwurf-Absicht -> publish=False
    db.expire_all()
    assert db.get(ProductIdea, old).status == "import_failed"
    assert db.get(ProductIdea, recent).status == "importing"


def test_recover_stuck_imports_restores_live_intent(db, monkeypatch):
    """Ein per „🚀 Live" gestarteter, dann durch Neustart verwaister Import wird als LIVE
    wiederhergestellt – nicht stillschweigend zum Entwurf degradiert (Review-Fund 11.07.)."""
    scheduled = []
    monkeypatch.setattr(r, "_schedule_import", lambda idea_id, publish, mp: scheduled.append((idea_id, publish)))
    idea = ProductIdea(aliexpress_id="RSL", aliexpress_url="https://de.aliexpress.com/item/rsl.html",
                       title="Live gewollt", status="importing", publish_requested=True)
    db.add(idea); db.commit()
    r._importing_ids.discard(idea.id)
    out = r.recover_stuck_imports()
    assert out["requeued"] == 1
    assert (idea.id, True) in scheduled            # Live-Absicht bleibt erhalten


def test_begin_import_persists_publish_intent(db, monkeypatch):
    monkeypatch.setattr(r, "_schedule_import", lambda *a, **k: None)
    idea = ProductIdea(aliexpress_id="PI", aliexpress_url="https://de.aliexpress.com/item/pi.html",
                       title="X", status="new")
    db.add(idea); db.commit()
    r._importing_ids.discard(idea.id)
    r.begin_import(db, idea_id=idea.id, publish=True)
    db.expire_all()
    assert db.get(ProductIdea, idea.id).publish_requested is True
