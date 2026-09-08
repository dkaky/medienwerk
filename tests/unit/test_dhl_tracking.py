"""DHL Shipment-Tracking-Anbindung: echter Zustellstatus + gedrosselte Hochstufung (Fund 17.07.).

Deckt AliExpress' Luecke bei der deutschen DHL-Zustellung. Client parst den STRUKTURIERTEN
statusCode; die Hochstufung ist gegen das Free-Limit (250/Tag, 1/5s) gedrosselt."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.integrations import dhl as dhlmod
from app.integrations.dhl import DhlStatus, DhlTrackingClient
from app.models import OrderAliexpress, Sale
from app.services import order_service

_DHL = "00340434886281338687"          # 003… -> DHL
_HERMES = "H012345678901234567"         # H + Alnum -> Hermes (kein DHL)


@pytest.fixture(autouse=True)
def _reset_dhl_globals():
    """Prozessweite DHL-Drossel/Budget zwischen Tests zuruecksetzen (sonst 5s-Wartezeit-Bleeding)."""
    order_service._dhl_pace["next_ts"] = 0.0
    order_service._dhl_pace["lock"] = None
    order_service._dhl_budget["date"] = None
    order_service._dhl_budget["count"] = 0
    yield


# ---------------------------------------------------------------- Client-Parsing

def _patch_httpx(monkeypatch, *, status_code=200, payload=None):
    class _Resp:
        def __init__(self):
            self.status_code = status_code
        def raise_for_status(self):
            return None
        def json(self):
            return payload or {}

    class _Client:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, *a, **k):
            return _Resp()
    monkeypatch.setattr(dhlmod.httpx, "AsyncClient", _Client)


def test_dhl_client_parses_delivered(monkeypatch):
    _patch_httpx(monkeypatch, payload={"shipments": [{"status": {
        "statusCode": "delivered", "status": "Zugestellt", "timestamp": "2026-07-07T14:23:00Z"}}]})
    r = asyncio.run(dhlmod.DhlTrackingClient("k").get_status(_DHL))
    assert r.found and r.delivered and r.status_code == "delivered"
    assert r.delivered_at is not None and r.delivered_at.year == 2026


def test_dhl_client_transit_not_delivered(monkeypatch):
    _patch_httpx(monkeypatch, payload={"shipments": [{"status": {
        "statusCode": "transit", "status": "In Zustellung"}}]})
    r = asyncio.run(dhlmod.DhlTrackingClient("k").get_status(_DHL))
    assert r.found and not r.delivered and r.status_code == "transit"


def test_dhl_client_404_not_found(monkeypatch):
    _patch_httpx(monkeypatch, status_code=404)
    r = asyncio.run(dhlmod.DhlTrackingClient("k").get_status(_HERMES))
    assert not r.found and not r.delivered


def test_dhl_client_429_raises(monkeypatch):
    _patch_httpx(monkeypatch, status_code=429)
    with pytest.raises(dhlmod.DhlRateLimited):
        asyncio.run(dhlmod.DhlTrackingClient("k").get_status(_DHL))


# ---------------------------------------------------------------- Hochstufung

class _FakeDhl:
    def __init__(self, by_number):
        self.by_number = by_number
        self.calls = []

    async def get_status(self, tn):
        self.calls.append(tn)
        return self.by_number.get(tn) or DhlStatus(tn, found=False, delivered=False,
                                                   status_code=None, status_text=None, delivered_at=None)


def _tracking_sale(db, *, tx, tracking, days_ago=10, checked_at=None):
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    sale = Sale(ebay_transaction_id=tx, ebay_order_id=tx.split("-")[0], ebay_line_item_id="A",
                quantity=1, status="tracking", sale_date=when)
    db.add(sale)
    db.flush()
    db.add(OrderAliexpress(sale_id=sale.id, aliexpress_order_id=f"AE-{tx}", quantity=1,
                           status="shipped", tracking_number=tracking, delivery_checked_at=checked_at))
    db.commit()
    return sale


def _delivered(tn):
    return DhlStatus(tn, found=True, delivered=True, status_code="delivered",
                     status_text="Zugestellt", delivered_at=datetime.now(timezone.utc))


def test_dhl_promote_marks_delivered(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "dhl_api_key", "testkey")
    sale = _tracking_sale(db, tx="X1-A", tracking=_DHL)
    fake = _FakeDhl({_DHL: _delivered(_DHL)})
    r = asyncio.run(order_service.promote_delivered_from_dhl(db, dhl=fake))
    assert r["checked"] == 1 and r["promoted"] == 1
    db.refresh(sale)
    assert sale.status == "delivered"


def test_dhl_transit_sets_checked_at_no_promote(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "dhl_api_key", "testkey")
    sale = _tracking_sale(db, tx="X2-A", tracking=_DHL)
    transit = DhlStatus(_DHL, found=True, delivered=False, status_code="transit",
                        status_text="unterwegs", delivered_at=None)
    r = asyncio.run(order_service.promote_delivered_from_dhl(db, dhl=_FakeDhl({_DHL: transit})))
    assert r["promoted"] == 0
    db.refresh(sale)
    assert sale.status == "tracking"
    order = db.query(OrderAliexpress).filter_by(sale_id=sale.id).one()
    assert order.delivery_checked_at is not None      # gedrosselt: heute nicht nochmal fragen


def test_dhl_skips_non_dhl_number(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "dhl_api_key", "testkey")
    sale = _tracking_sale(db, tx="X3-A", tracking=_HERMES)   # Hermes -> DHL-API nicht fragen
    fake = _FakeDhl({})
    r = asyncio.run(order_service.promote_delivered_from_dhl(db, dhl=fake))
    assert fake.calls == [] and r["checked"] == 0
    db.refresh(sale)
    assert sale.status == "tracking"


def test_dhl_disabled_without_key(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "dhl_api_key", "")
    _tracking_sale(db, tx="X4-A", tracking=_DHL)
    r = asyncio.run(order_service.promote_delivered_from_dhl(db, dhl=_FakeDhl({_DHL: _delivered(_DHL)})))
    assert r.get("disabled") is True and r["promoted"] == 0


def test_dhl_recheck_throttle_skips_recent(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "dhl_api_key", "testkey")
    recent = datetime.now(timezone.utc) - timedelta(hours=2)   # < recheck_hours (20)
    sale = _tracking_sale(db, tx="X5-A", tracking=_DHL, checked_at=recent)
    fake = _FakeDhl({_DHL: _delivered(_DHL)})
    r = asyncio.run(order_service.promote_delivered_from_dhl(db, dhl=fake))
    assert fake.calls == [] and r["checked"] == 0       # erst nach ~1 Tag wieder fragen
    db.refresh(sale)
    assert sale.status == "tracking"


def test_dhl_skips_too_fresh(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "dhl_api_key", "testkey")
    sale = _tracking_sale(db, tx="X6-A", tracking=_DHL, days_ago=0)   # < min_age
    fake = _FakeDhl({_DHL: _delivered(_DHL)})
    r = asyncio.run(order_service.promote_delivered_from_dhl(db, dhl=fake))
    assert fake.calls == [] and r["checked"] == 0
    db.refresh(sale)
    assert sale.status == "tracking"


# ---------------------------------------------------------------- DHL-Key im Dashboard (DB)

def test_app_settings_get_set(db):
    from app.services.app_settings import effective_dhl_api_key, get_app_setting, set_app_setting
    assert get_app_setting(db, "dhl_api_key") is None
    set_app_setting(db, "dhl_api_key", "abc12345")
    assert get_app_setting(db, "dhl_api_key") == "abc12345"
    assert effective_dhl_api_key(db) == "abc12345"          # kein env -> DB-Wert


def test_effective_key_prefers_env(db, monkeypatch):
    from app.services.app_settings import effective_dhl_api_key, set_app_setting
    set_app_setting(db, "dhl_api_key", "dbkey123")
    monkeypatch.setattr(get_settings(), "dhl_api_key", "envkey123")
    assert effective_dhl_api_key(db) == "envkey123"         # env hat Vorrang vor DB


def test_dhl_key_endpoint_set_and_status(client):
    r = client.post("/api/v1/settings/dhl-key", json={"key": "yiKZMO9ZwyyG0Ccn"})
    assert r.status_code == 200 and r.json()["configured"] is True
    assert "…" in r.json()["masked"]                        # nie im Klartext
    assert client.get("/api/v1/settings/dhl-key").json()["configured"] is True


def test_dhl_key_endpoint_rejects_bad(client):
    assert client.post("/api/v1/settings/dhl-key", json={"key": "short"}).status_code == 400
    assert client.post("/api/v1/settings/dhl-key", json={"key": "hat leerzeichen 12"}).status_code == 400


def test_classify_probe_maps_http_status_to_verdict():
    C = DhlTrackingClient._classify_probe
    assert C(200)["ok"] is True                              # Key akzeptiert
    assert C(404)["ok"] is True                              # Nummer unbekannt, Auth trotzdem ok
    assert C(429)["ok"] is True                              # Limit/Takt – Key funktioniert
    assert C(401)["ok"] is False and "lehnt den Key ab" in C(401)["reason"]
    assert C(403)["ok"] is False
    assert C(500)["ok"] is False and "500" in C(500)["reason"]


async def test_probe_without_key_reports_not_configured():
    r = await DhlTrackingClient("").probe()
    assert r["ok"] is False and "Kein DHL" in r["reason"]


def test_dhl_key_test_endpoint_without_key(client):
    """Self-Test-Endpoint ohne konfigurierten Key -> ok=False, kein Crash, kein Key im Body."""
    r = client.get("/api/v1/settings/dhl-key/test")
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_dhl_promote_uses_db_key(db, monkeypatch):
    from app.services.app_settings import set_app_setting
    monkeypatch.setattr(get_settings(), "dhl_api_key", "")   # kein env-Key
    set_app_setting(db, "dhl_api_key", "dbkey12345")         # aber im Dashboard gesetzt
    sale = _tracking_sale(db, tx="Y1-A", tracking=_DHL)
    r = asyncio.run(order_service.promote_delivered_from_dhl(db, dhl=_FakeDhl({_DHL: _delivered(_DHL)})))
    assert r["promoted"] == 1
    db.refresh(sale)
    assert sale.status == "delivered"
