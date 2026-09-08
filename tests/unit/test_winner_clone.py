"""Winner-Cloner: Suchbegriffe aus den eigenen Bestsellern statt Zufalls-Nischen.

Hintergrund (Portfolio-Analyse 08/2026): 79 % der aktiven Listings werden geklickt,
aber nie gekauft — das Sourcing ist der Engpass, nicht die Sichtbarkeit.
"""
from __future__ import annotations

from decimal import Decimal

from app.models import Listing
from app.services import product_research_service as research


def _listing(db, *, title, sales_total=0, status="active", price=None):
    l = Listing(title_seo=title, description="d", listing_status=status,
                sales_total=sales_total,
                price_eur=Decimal(str(price)) if price is not None else None)
    db.add(l)
    db.flush()
    return l


def test_dna_lists_winners_sorted_and_skips_rest(db):
    _listing(db, title="Topseller Motocross Brille", sales_total=35, price=19.95)
    _listing(db, title="Solider Zweiter", sales_total=8, price=27.95)
    _listing(db, title="Einmalverkauf", sales_total=1)           # < min_sales -> raus
    _listing(db, title="Ladenhueter", sales_total=0)             # nie verkauft -> raus
    _listing(db, title="Beendeter Gewinner", sales_total=50, status="ended")  # inaktiv -> raus
    db.commit()

    dna = research.winner_dna_context(db)
    assert "Topseller Motocross Brille" in dna
    assert "Solider Zweiter" in dna
    assert "Einmalverkauf" not in dna
    assert "Ladenhueter" not in dna
    assert "Beendeter Gewinner" not in dna
    # Bestseller zuerst (Sortierung nach sales_total absteigend)
    assert dna.index("Topseller") < dna.index("Solider Zweiter")
    assert "19.95 EUR" in dna and "35 Verkäufe" in dna


def test_dna_empty_without_winners(db):
    _listing(db, title="Nur ein Verkauf", sales_total=1)
    db.commit()
    assert research.winner_dna_context(db) == ""


async def test_winner_clone_feeds_llm_keywords_into_discover(db, monkeypatch):
    _listing(db, title="Gewinner A", sales_total=12, price=24.95)
    db.commit()

    captured: dict = {}

    async def fake_discover(_db, **kwargs):
        captured.update(kwargs)
        return {"kept": 3, "scanned": 30}

    monkeypatch.setattr(research, "discover", fake_discover)
    res = await research.winner_clone(db, target=17, min_margin=0.3)

    # Der Mock-LLM liefert feste Keywords -> genau die muessen in discover ankommen
    assert captured["niches"], "discover wurde ohne Keywords aufgerufen"
    assert all(isinstance(k, str) and k for k in captured["niches"])
    assert captured["from_trend"] is True
    assert captured["target"] == 17
    assert captured["min_margin"] == 0.3
    assert res["kept"] == 3
    assert res["terms"], "die KI-Begriffe muessen im Ergebnis auftauchen"


async def test_winner_clone_without_winners_skips_discover(db, monkeypatch):
    called = False

    async def fake_discover(_db, **kwargs):  # noqa: ARG001
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(research, "discover", fake_discover)
    res = await research.winner_clone(db)

    assert called is False, "ohne Gewinner darf keine Suche starten (kostet API-Calls)"
    assert res["kept"] == 0
    assert "note" in res
