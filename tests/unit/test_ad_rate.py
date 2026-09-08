"""Tests: echte Promoted-Listings-Anzeigenrate je Listing lesen + speichern.

Deckt drei Ebenen ab:
* RealEbayClient.get_ad_rates: parst die eBay-getAds-Antwort korrekt zu
  {listingId: bruch} ("12.0" -> 0.12); nicht-numerische/fehlende bidPercentage
  werden uebersprungen; kein Scope/keine Kampagne -> {}.
* ad_rate_service.sync_ad_rates: schreibt listing.ad_rate_pct fuer gefundene
  Listings, laesst nicht gefundene unangetastet.
* ad_rate_service.set_ad_rate: setzt + committet.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import Settings
from app.integrations.ebay import RealEbayClient
from app.models import Listing
from app.retry import PersistentError
from app.services import ad_rate_service


# --------------------------------------------------------------------------
# Fakes fuer den HTTP-Layer des RealEbayClient (netzwerkfrei)
# --------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeHttpPages:
    """Liefert bei jedem GET die naechste vorbereitete Seite (Paginierung)."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = 0

    async def get(self, *a, **k):
        self.calls += 1
        page = self._pages.pop(0) if self._pages else {"ads": []}
        return _FakeResp(page)


def _client(**over) -> RealEbayClient:
    base = dict(
        use_mocks=False, ebay_client_id="cid", ebay_client_secret="sec",
        ebay_refresh_token="rt", ebay_marketplace_id="EBAY_DE",
    )
    base.update(over)
    return RealEbayClient(Settings(**base))


def _stub_campaign(monkeypatch, client, http):
    """ensure_ad_campaign / _marketing_headers / _http fuer parse-Tests stubben."""
    async def _camp(*a, **k):
        return "CAMP-1"

    async def _headers(*a, **k):
        return {}
    monkeypatch.setattr(client, "ensure_ad_campaign", _camp)
    monkeypatch.setattr(client, "_marketing_headers", _headers)
    monkeypatch.setattr(client, "_http", lambda: http)


# --------------------------------------------------------------------------
# get_ad_rates: Parsing
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_ad_rates_parses_bid_percentage_to_fraction(monkeypatch):
    c = _client()
    page = {"ads": [
        {"listingId": "111", "bidPercentage": "12.0"},
        {"listingId": "222", "bidPercentage": "9.5"},
    ], "total": 2}
    _stub_campaign(monkeypatch, c, _FakeHttpPages([page]))

    rates = await c.get_ad_rates()
    assert rates == {"111": 0.12, "222": 0.095}


@pytest.mark.asyncio
async def test_get_ad_rates_skips_missing_or_nonnumeric(monkeypatch):
    c = _client()
    page = {"ads": [
        {"listingId": "111", "bidPercentage": "10.0"},
        {"listingId": "222"},                       # fehlende bidPercentage
        {"listingId": "333", "bidPercentage": ""},  # leer
        {"listingId": "444", "bidPercentage": "n/a"},  # nicht numerisch
        {"bidPercentage": "5.0"},                   # ohne listingId
    ], "total": 5}
    _stub_campaign(monkeypatch, c, _FakeHttpPages([page]))

    rates = await c.get_ad_rates()
    assert rates == {"111": 0.10}   # nur das gueltige Ad


@pytest.mark.asyncio
async def test_get_ad_rates_filters_to_requested_ids(monkeypatch):
    c = _client()
    page = {"ads": [
        {"listingId": "111", "bidPercentage": "12.0"},
        {"listingId": "222", "bidPercentage": "8.0"},
        {"listingId": "333", "bidPercentage": "6.0"},
    ], "total": 3}
    _stub_campaign(monkeypatch, c, _FakeHttpPages([page]))

    rates = await c.get_ad_rates(["111", "333"])
    assert rates == {"111": 0.12, "333": 0.06}


@pytest.mark.asyncio
async def test_get_ad_rates_paginates_over_total(monkeypatch):
    c = _client()
    # total=3, aber 200er-Seiten -> hier zwei Seiten simulieren.
    p1 = {"ads": [{"listingId": "111", "bidPercentage": "12.0"}],
          "total": 3, "next": "?offset=1"}
    p2 = {"ads": [{"listingId": "222", "bidPercentage": "9.0"},
                  {"listingId": "333", "bidPercentage": "7.0"}],
          "total": 3}
    http = _FakeHttpPages([p1, p2])
    _stub_campaign(monkeypatch, c, http)

    rates = await c.get_ad_rates()
    assert rates == {"111": 0.12, "222": 0.09, "333": 0.07}
    assert http.calls == 2


@pytest.mark.asyncio
async def test_get_ad_rates_no_campaign_returns_empty(monkeypatch):
    c = _client()

    async def _boom(*a, **k):
        raise PersistentError("kein marketing scope")
    monkeypatch.setattr(c, "ensure_ad_campaign", _boom)

    assert await c.get_ad_rates(["111"]) == {}


@pytest.mark.asyncio
async def test_get_ad_rates_http_error_returns_empty(monkeypatch):
    c = _client()

    class _Boom:
        async def get(self, *a, **k):
            raise RuntimeError("network down")
    _stub_campaign(monkeypatch, c, _Boom())

    assert await c.get_ad_rates() == {}


# --------------------------------------------------------------------------
# Helpers fuer den Service (DB)
# --------------------------------------------------------------------------
def _mk_listing(db, *, item_id=None, status="active", ad_rate=None) -> Listing:
    lst = Listing(
        title_seo="Testartikel", description="x", listing_status=status,
        price_eur=Decimal("29.95"), ebay_item_id=item_id, ad_rate_pct=ad_rate,
    )
    db.add(lst)
    db.commit()
    return lst


class _FakeEbayRates:
    def __init__(self, rates):
        self._rates = rates
        self.requested = None

    async def get_ad_rates(self, listing_ids=None):
        self.requested = listing_ids
        if listing_ids:
            return {k: v for k, v in self._rates.items() if k in set(listing_ids)}
        return dict(self._rates)


# --------------------------------------------------------------------------
# sync_ad_rates
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sync_ad_rates_writes_found_and_keeps_unknown(monkeypatch, db):
    a = _mk_listing(db, item_id="111")                     # bekommt Rate
    b = _mk_listing(db, item_id="222", ad_rate=0.10)       # NICHT in Antwort -> unangetastet
    _mk_listing(db, item_id=None)                          # kein item_id -> ignoriert
    _mk_listing(db, item_id="999", status="draft")         # nicht aktiv -> ignoriert

    fake = _FakeEbayRates({"111": 0.15})
    monkeypatch.setattr(ad_rate_service, "_real_ebay", lambda: fake)

    result = await ad_rate_service.sync_ad_rates(db)
    assert result == {"updated": 1, "checked": 2}          # nur 111 + 222 aktiv/mit id

    db.refresh(a)
    db.refresh(b)
    assert a.ad_rate_pct == 0.15                           # geschrieben
    assert b.ad_rate_pct == 0.10                           # unveraendert (nicht auf None)
    # nur aktive Listings mit item_id wurden angefragt
    assert set(fake.requested) == {"111", "222"}


@pytest.mark.asyncio
async def test_sync_ad_rates_no_active_listings(monkeypatch, db):
    _mk_listing(db, item_id="111", status="draft")
    called = {"n": 0}

    class _Never:
        async def get_ad_rates(self, listing_ids=None):
            called["n"] += 1
            return {}
    monkeypatch.setattr(ad_rate_service, "_real_ebay", lambda: _Never())

    result = await ad_rate_service.sync_ad_rates(db)
    assert result == {"updated": 0, "checked": 0}
    assert called["n"] == 0                                # kein Netz-Call ohne Kandidaten


@pytest.mark.asyncio
async def test_sync_ad_rates_empty_response_updates_nothing(monkeypatch, db):
    a = _mk_listing(db, item_id="111", ad_rate=0.10)
    monkeypatch.setattr(ad_rate_service, "_real_ebay", lambda: _FakeEbayRates({}))

    result = await ad_rate_service.sync_ad_rates(db)
    assert result == {"updated": 0, "checked": 1}
    db.refresh(a)
    assert a.ad_rate_pct == 0.10                           # unangetastet


@pytest.mark.asyncio
async def test_sync_ad_rates_swallows_client_error(monkeypatch, db):
    _mk_listing(db, item_id="111")

    class _Boom:
        async def get_ad_rates(self, listing_ids=None):
            raise RuntimeError("eBay down")
    monkeypatch.setattr(ad_rate_service, "_real_ebay", lambda: _Boom())

    result = await ad_rate_service.sync_ad_rates(db)
    assert result["updated"] == 0 and result["checked"] == 1
    assert "error" in result


# --------------------------------------------------------------------------
# set_ad_rate
# --------------------------------------------------------------------------
def test_set_ad_rate_sets_and_commits(db):
    lst = _mk_listing(db, item_id="111")
    ad_rate_service.set_ad_rate(db, lst, 0.18)

    # frische Session -> beweist, dass committet wurde
    from app.database import SessionLocal
    other = SessionLocal()
    try:
        reloaded = other.get(Listing, lst.id)
        assert reloaded.ad_rate_pct == 0.18
    finally:
        other.close()
