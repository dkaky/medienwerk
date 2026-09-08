"""Taegliche Store-Entdeckung (Nutzerauftrag 15.08.): Kandidatenwahl (EU zuerst),
Merkliste (nie doppelt), Fehlversuchs-Zaehler und prune-Schutz der 🏬-Ideen."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import ProductIdea
from app.services import product_research_service as research


def _idee(db, sid, *, name="Shop", ships=None, ali="x1"):
    db.add(ProductIdea(aliexpress_id=ali, store_id=sid, store_name=name,
                       ships_from=ships, status="new"))
    db.flush()


@pytest.fixture()
def seen_file(tmp_path, monkeypatch):
    f = tmp_path / "seen.json"
    monkeypatch.setattr(research, "_STORE_SEEN_FILE", str(f))
    return f


def _stub_harvest(monkeypatch, ids=("100500123",), fail_for=()):
    calls = []

    async def fake(sid, *, limit=25, **kw):
        calls.append(sid)
        if sid in fail_for:
            from app.integrations.aliexpress_store import StoreScrapeError
            raise StoreScrapeError("blockiert")
        return list(ids)

    from app.integrations import aliexpress_store
    monkeypatch.setattr(aliexpress_store, "fetch_store_product_ids", fake)
    return calls


def _stub_discover(monkeypatch):
    calls = []

    async def fake(db, **kw):
        calls.append(kw)
        return {"kept": 2, "scanned": 5}

    monkeypatch.setattr(research, "discover", fake)
    return calls


def test_eu_stores_zuerst_und_merkliste(db, seen_file, monkeypatch):
    _idee(db, "111", name="China-Shop", ships=None, ali="a1")
    _idee(db, "222", name="EU-Shop", ships=["Polen"], ali="a2")
    _idee(db, "333", name="EU-Shop2", ships=["Deutschland"], ali="a3")
    # Explizite Nur-China-Achse: truthy, aber KEIN EU-Lager -> darf nicht vorgezogen werden
    _idee(db, "444", name="China2", ships=["China"], ali="a4")
    harvest = _stub_harvest(monkeypatch)
    disc = _stub_discover(monkeypatch)

    r = asyncio.run(research.daily_store_discovery(db, stores_per_day=3, per_store=5))

    assert r["neu_verarbeitet"] == 3
    # EU-Lager-Stores kommen VOR den China-Stores dran
    assert set(harvest[:2]) == {"222", "333"}
    assert harvest[2] in ("111", "444") and len(harvest) == 3
    # Ideen laufen unter der 🏬-Nische durch die normale Pipeline
    assert disc[0]["id_import_label"].startswith("🏬 ")
    seen = json.loads(seen_file.read_text(encoding="utf-8"))
    assert set(seen) == set(harvest)
    assert all(v.get("date") for v in seen.values())


def test_zweiter_lauf_nimmt_nur_neue_stores(db, seen_file, monkeypatch):
    for i, sid in enumerate(["1", "2", "3", "4"]):
        _idee(db, sid, ali=f"b{i}")
    _stub_harvest(monkeypatch)
    _stub_discover(monkeypatch)

    asyncio.run(research.daily_store_discovery(db, stores_per_day=3, per_store=5))
    r2 = asyncio.run(research.daily_store_discovery(db, stores_per_day=3, per_store=5))

    assert r2["neu_verarbeitet"] == 1          # nur der vierte Store war noch uebrig
    assert len(json.loads(seen_file.read_text(encoding="utf-8"))) == 4


def test_fehlversuch_zaehlt_und_gibt_nach_drei_auf(db, seen_file, monkeypatch):
    _idee(db, "999", ali="c1")
    _stub_harvest(monkeypatch, fail_for=("999",))
    _stub_discover(monkeypatch)

    for _ in range(3):
        asyncio.run(research.daily_store_discovery(db, stores_per_day=3, per_store=5))
    seen = json.loads(seen_file.read_text(encoding="utf-8"))
    assert seen["999"]["fails"] == 3 and not seen["999"].get("date")

    r = asyncio.run(research.daily_store_discovery(db, stores_per_day=3, per_store=5))
    assert r["kandidaten"] == 0                # dauerhaft blockiert -> aufgegeben


def test_discover_id_import_erkennt_beide_id_formen(db):
    """Browser-Ernten liefern die 3256-Form, die DB fuehrt die 1005-Form derselben
    Ware (Offset 2^51) — der Dedup muss BEIDE Formen erkennen (Review-Fund 16.08.)."""
    offset = 2251799813685248
    alt = 1005009000000001
    db.add(ProductIdea(aliexpress_id=str(alt), status="new"))
    db.commit()

    r = asyncio.run(research.discover(db, product_ids=[str(alt + offset)], target=5))

    assert r["kept"] == 0                      # dieselbe Ware -> keine Duplikat-Idee
    ideen = db.scalars(select(ProductIdea)).all()
    assert len(ideen) == 1


def test_prune_verschont_store_ideen(db):
    alt = datetime.utcnow() - timedelta(days=9)
    a = ProductIdea(aliexpress_id="p1", niche="🏬 EU-Shop", status="new")
    b = ProductIdea(aliexpress_id="p2", niche="autopflege", status="new")
    db.add_all([a, b])
    db.flush()
    a.created_at = alt
    b.created_at = alt
    db.commit()

    r = research.prune_old_ideas(db, days=5)

    assert r["deleted"] == 1
    rest = [i.aliexpress_id for i in db.scalars(select(ProductIdea)).all()]
    assert rest == ["p1"]                      # 🏬-Idee bleibt IMMER stehen


def test_elektronik_filter_kennt_werkzeug_elektrik():
    """Nachtrag 16.08.: diese drei ECHTEN Store-Fund-Titel rutschten durch."""
    for titel in (
        "Professionelles Auto-Dellen-Reparatur-Set – PDR-Werkzeuge mit Klebepistole",
        "Professionelle PDR-Werkzeuge, Dellenentfernungs-LED-Lampe, Autokarosserie",
        "Mini-Holzdrehmaschine 12 V DC 2 A 24 W Mehrzweck-20000 U/min",
    ):
        assert research._is_electronic(titel) is True, titel
    # Die bewusst ERLAUBTE Deko-Nische darf NICHT getroffen werden
    assert research._is_electronic("LED Deko Lampe Wohnzimmer Ambiente") is False
    assert research._is_electronic("Mikrofaser Auto Detailing Set") is False
