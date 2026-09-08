"""Lokal-Lager-Umbau: "Versand aus" (SKU-Achse 200007763) statt Lieferzeit-Naeherung.

Kern-Garantien:
1. Der Lagerort wird NIE zur Kauf-Option (keine eBay-Variante, keine Germanisierung).
2. SKUs, die sich nur im Lager unterscheiden, kollabieren auf EINE — EU bevorzugt —
   und Preis/Bestand rechnen mit der GEWAEHLTEN SKU (Regel 14: keine falsche Kalkulation).
3. discover(local_only=True) behaelt nur Produkte mit EU-Lager-Option.
"""
from __future__ import annotations

from decimal import Decimal

from app.integrations import aliexpress_api as api
from app.services import product_research_service as research


def _sku(sku_id, attr, price, color, ship, stock=10):
    props = [{"sku_property_id": 14, "sku_property_name": "Farbe",
              "sku_property_value": color}]
    if ship:
        props.append({"sku_property_id": 200007763, "sku_property_name": "Versand aus",
                      "sku_property_value": ship})
    return {"sku_id": sku_id, "sku_attr": attr, "offer_sale_price": price,
            "sku_available_stock": stock,
            "ae_sku_property_dtos": {"ae_sku_property_d_t_o": props}}


def _raw(skus):
    return {"aliexpress_ds_product_get_response": {"result": {
        "ae_item_base_info_dto": {"product_id": "555", "subject": "Testprodukt"},
        "ae_item_sku_info_dtos": {"ae_item_sku_info_d_t_o": skus},
    }}}


def test_ships_from_never_becomes_axis_and_eu_sku_wins():
    raw = _raw([
        _sku("1", "14:1#Rot;200007763:100#CHINA", "3.50", "Rot", "CHINA", stock=500),
        _sku("2", "14:1#Rot;200007763:200#Polen", "4.20", "Rot", "Polen", stock=40),
        _sku("3", "14:2#Blau;200007763:100#CHINA", "3.60", "Blau", "CHINA", stock=300),
    ])
    p = api.parse_product(raw)
    variants = p["variants"]
    # 1) Lagerort ist KEINE Achse und KEINE Option
    assert "Versand aus" not in variants["axes"]
    assert variants["axes"] == {"Farbe": ["Rot", "Blau"]}
    assert all("Versand aus" not in v["options"] for v in variants["skus"])
    # 2) Rot: China+Polen kollabiert auf EINE SKU — die EU-SKU gewinnt, attr bleibt roh
    rot = [v for v in variants["skus"] if v["options"].get("Farbe") == "Rot"]
    assert len(rot) == 1
    assert rot[0]["ship_from"] == "Polen"
    assert rot[0]["attr"] == "14:1#Rot;200007763:200#Polen"
    # 3) Preis/Bestand NACH der Dedup: Minimum ist der Blau-China-Preis (3.60),
    #    NICHT der verworfene Rot-China-Preis (3.50); Bestand ohne die verworfene SKU.
    assert p["price_cny"] == Decimal("3.60")
    assert sum(1 for v in variants["skus"]) == 2
    # 4) extract_ships_from liefert alle Werte der Roh-Antwort
    assert api.extract_ships_from(api._unwrap_result(raw)) == ["CHINA", "Polen"]


def test_has_eu_warehouse_matching():
    assert api.has_eu_warehouse(["Polen"]) is True
    assert api.has_eu_warehouse(["GERMANY"]) is True
    assert api.has_eu_warehouse([" frankreich "]) is True
    assert api.has_eu_warehouse(["CHINA"]) is False
    assert api.has_eu_warehouse(["United States"]) is False
    assert api.has_eu_warehouse([]) is False
    assert api.has_eu_warehouse(None) is False


def _mk_search(monkeypatch, enrich_by_id):
    monkeypatch.setattr(research, "_real_ae", lambda: object())

    async def fake_search(_ae, _kw, **_k):
        return [{"id": pid, "title": e.get("title"), "image": None, "score": 4.8,
                 "orders": 100, "price": 3.0, "url": ""}
                for pid, e in enrich_by_id.items()]

    async def fake_enrich(_ae, pid):
        return enrich_by_id[pid]

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(research, "_text_search", fake_search)
    monkeypatch.setattr(research, "_enrich", fake_enrich)
    monkeypatch.setattr(research.asyncio, "sleep", no_sleep)


def _e(title, ships_from):
    return {"rating": 4.8, "reviews": 120, "status": "onSelling", "sl_product": False,
            "delivery_days": 6, "ships_from": ships_from, "store_name": "S",
            "price_cny": 12.0, "images": [], "title": title, "category_id": "1"}


async def test_discover_local_only_keeps_only_eu_and_persists(db, monkeypatch):
    # Hub-Quelle aus (leerer String): dieser Test prueft bewusst den KEYWORD-Pfad
    # mit Achsen-Beweis (die Hub-Seite wuerde die Keyword-Nischen sonst ersetzen).
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "local_source_page_url", "")
    _mk_search(monkeypatch, {
        "111": _e("EU Produkt", ["CHINA", "Polen"]),
        "222": _e("China Produkt", ["CHINA"]),
        "333": _e("Ohne Angabe", []),
    })
    r = await research.discover(db, niches=["x"], target=10, local_only=True)
    assert r["kept"] == 1

    out = research.list_ideas(db)
    assert len(out["ideas"]) == 1
    idea = out["ideas"][0]
    assert idea["title"] == "EU Produkt"
    assert idea["ships_from"] == ["CHINA", "Polen"]
    assert idea["local_warehouse"] is True


async def test_discover_without_local_only_keeps_all(db, monkeypatch):
    _mk_search(monkeypatch, {
        "444": _e("EU Produkt", ["Frankreich"]),
        "555": _e("China Produkt", ["CHINA"]),
    })
    r = await research.discover(db, niches=["x"], target=10, local_only=False)
    assert r["kept"] == 2
    flags = {i["title"]: i["local_warehouse"] for i in research.list_ideas(db)["ideas"]}
    assert flags == {"EU Produkt": True, "China Produkt": False}
