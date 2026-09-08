"""Lokale Kalkulation: EU-Lager-Produkte zahlen keinen Zoll und keine Versand-Schaetzung.

Nutzerregel 08.08.2026: "Der Preis der angezeigt wird, ist dann tatsaechlich auch der
Preis den man am Ende zahlt. Da kommt kein extra Zoll und kein extra Versand hinzu
(ausser da steht extra Versand)." ECHTE Frachtkosten (ship_override) und der eigene
Versandaufschlag bleiben. Regel 14: Finanzzahlen exakt, nie schaetzen.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.config import get_settings
from app.services import pricing
from app.services import product_research_service as research

# Explizite Rechen-Einstellungen: Artikel 5 EUR liegt UNTER der Gratis-Schwelle,
# damit die Versand-Schaetzung im China-Fall sicher greift.
S = SimpleNamespace(cny_to_eur_rate=1.0, usd_to_eur_rate=1.0,
                    aliexpress_free_shipping_threshold=10.0,
                    aliexpress_shipping_fee_eur=1.99,
                    aliexpress_tax_pct=0.0, customs_fee_eur=3.57,
                    shipping_cost_eur=0.5)


def test_effective_cost_local_drops_customs_and_shipping_estimate():
    china = pricing.effective_cost(5.0, settings=S)
    lokal = pricing.effective_cost(5.0, settings=S, local=True)
    assert china == round(5.0 + 1.99 + 3.57 + 0.5, 2)   # Schaetzung + Zoll + eigener Versand
    assert lokal == round(5.0 + 0.5, 2)                  # NUR Preis + eigener Versand


def test_effective_cost_local_keeps_real_freight():
    # "ausser da steht extra Versand": echte Frachtkosten werden addiert, Zoll nicht
    lokal = pricing.effective_cost(5.0, settings=S, local=True, ship_override=3.29)
    assert lokal == round(5.0 + 3.29 + 0.5, 2)


def test_effective_cost_bundle_local():
    china = pricing.effective_cost_bundle(5.0, quantity=3, settings=S)
    lokal = pricing.effective_cost_bundle(5.0, quantity=3, settings=S, local=True)
    # China: 15 ueber Schwelle -> keine Versand-Schaetzung, aber Zoll einmal
    assert china == round(15.0 + 3.57 + 3 * 0.5, 2)
    assert lokal == round(15.0 + 3 * 0.5, 2)


def test_upload_breakdown_local_cost_is_customs_lower():
    s = get_settings()
    china = pricing.upload_breakdown_from_cny(12.0, settings=s, ship_override=0.0)
    lokal = pricing.upload_breakdown_from_cny(12.0, settings=s, ship_override=0.0, local=True)
    assert round(float(china.cost_eur) - float(lokal.cost_eur), 2) == round(
        s.customs_fee_eur, 2), "lokaler EK muss exakt um den Pauschalzoll niedriger sein"


def _e(title, ships_from):
    return {"rating": 4.8, "reviews": 120, "status": "onSelling", "sl_product": False,
            "delivery_days": 6, "ships_from": ships_from, "store_name": "S",
            "price_cny": 12.0, "images": [], "title": title, "category_id": "1"}


async def test_discover_passes_local_flag_per_candidate(db, monkeypatch):
    captured: list[tuple[str, bool]] = []
    real_cost = pricing.effective_cost

    def spy_cost(price, **kw):
        if "local" in kw:
            captured.append(("cost", kw["local"]))
        return real_cost(price, **kw)

    real_br = pricing.upload_breakdown_from_cny

    def spy_br(price, **kw):
        captured.append(("br", kw.get("local", False)))
        return real_br(price, **kw)

    monkeypatch.setattr(research.pricing, "effective_cost", spy_cost)
    monkeypatch.setattr(research.pricing, "upload_breakdown_from_cny", spy_br)
    monkeypatch.setattr(research, "_real_ae", lambda: object())

    enrich_by_id = {"111": _e("EU Produkt", ["Polen"]),
                    "222": _e("China Produkt", ["CHINA"])}

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

    r = await research.discover(db, niches=["x"], target=10)
    assert r["kept"] == 2
    # Je Kandidat einmal cost + einmal br, mit dem richtigen Lokal-Flag
    assert ("cost", True) in captured and ("br", True) in captured, "EU-Produkt muss lokal rechnen"
    assert ("cost", False) in captured and ("br", False) in captured, "China-Produkt normal"
