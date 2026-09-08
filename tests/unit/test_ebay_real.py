"""Unit-Tests fuer RealEbayClient (netzwerkfrei).

Geprueft wird die deterministische Logik: OAuth-Token-Cache, Fehler-Mapping,
Analytics-/Order-Parsing, die Account-Deletion-Challenge und die MODERNE
Notification-Signatur-Verifikation (ECDSA/SHA1) mit selbst erzeugtem Schluessel.
Echte eBay-API-Aufrufe brauchen Credentials und werden nicht getestet.
"""
from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import Settings
from app.integrations.ebay import RealEbayClient
from app.retry import PersistentError, RateLimitError, TransientError


def _client(**over) -> RealEbayClient:
    base = dict(
        use_mocks=False, ebay_client_id="cid", ebay_client_secret="sec",
        ebay_refresh_token="rt", ebay_marketplace_id="EBAY_DE",
    )
    base.update(over)
    return RealEbayClient(Settings(**base))


# ----------------------------------------------------------------- OAuth/Setup
def test_host_switches_to_sandbox():
    assert "sandbox" in _client(ebay_use_sandbox=True)._token_url
    assert "sandbox" not in _client(ebay_use_sandbox=False)._token_url


def test_basic_auth_header():
    c = _client()
    expected = "Basic " + base64.b64encode(b"cid:sec").decode()
    assert c._basic_auth() == expected


def test_marketplace_currency_and_language():
    c = _client(ebay_marketplace_id="EBAY_DE")
    assert c._currency == "EUR"
    assert c._content_language == "de-DE"


@pytest.mark.asyncio
async def test_user_token_uses_cache_without_network():
    c = _client()
    c._user_token = "cached-token"
    c._user_token_expiry = time.monotonic() + 100  # noch gueltig
    assert await c._get_user_token() == "cached-token"  # kein HTTP-Call


@pytest.mark.asyncio
async def test_user_token_missing_refresh_is_persistent():
    c = _client(ebay_refresh_token="")
    c._user_token_expiry = 0  # abgelaufen
    with pytest.raises(PersistentError):
        await c._get_user_token()


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeHttp:
    def __init__(self, data):
        self._data = data
        self.calls = 0

    async def post(self, *a, **k):
        self.calls += 1
        return _FakeResp(self._data)


def _active_xml(total_pages, item_ids):
    items = "".join(
        f"<Item><ItemID>{i}</ItemID><Title>T{i}</Title><SKU>S{i}</SKU>"
        f"<SellingStatus><CurrentPrice currencyID='EUR'>19.95</CurrentPrice></SellingStatus>"
        f"<Quantity>5</Quantity></Item>" for i in item_ids)
    return (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<GetMyeBaySellingResponse xmlns='urn:ebay:apis:eBLBaseComponents'>"
        "<Ack>Success</Ack><ActiveList>"
        f"<PaginationResult><TotalNumberOfPages>{total_pages}</TotalNumberOfPages></PaginationResult>"
        f"<ItemArray>{items}</ItemArray></ActiveList></GetMyeBaySellingResponse>")


class _PagedHttp:
    """Liefert je angefragter PageNumber die passende ActiveList-Seite zurueck."""
    def __init__(self, pages):
        self.pages = pages                 # {page: (total_pages, [item_ids])}
        self.requested: list[int] = []

    async def post(self, url, headers=None, content=None):
        import re
        body = content.decode() if isinstance(content, (bytes, bytearray)) else content
        page = int(re.search(r"<PageNumber>(\d+)</PageNumber>", body).group(1))
        self.requested.append(page)
        total_pages, ids = self.pages[page]

        class _R:
            def __init__(self, t): self.text = t
            def raise_for_status(self): return None
        return _R(_active_xml(total_pages, ids))


@pytest.mark.asyncio
async def test_get_active_listings_walks_all_pages(monkeypatch):
    """KERN-FIX (615->500): die Paginierung MUSS bis TotalNumberOfPages laufen, nicht bei
    einem festen max_items stoppen – sonst fehlen die Listings auf den letzten Seiten."""
    c = _client()

    async def _tok(): return "tok"
    monkeypatch.setattr(c, "_get_user_token", _tok)
    http = _PagedHttp({1: (3, [101, 102]), 2: (3, [201, 202]), 3: (3, [301])})
    monkeypatch.setattr(c, "_http", lambda: http)

    out = await c.get_active_listings()
    assert http.requested == [1, 2, 3]                     # ALLE Seiten geholt
    assert [r["item_id"] for r in out] == ["101", "102", "201", "202", "301"]
    assert c._last_active_fetch_complete is True            # bis zur letzten Seite -> vollstaendig


@pytest.mark.asyncio
async def test_get_active_listings_safety_ceiling_truncates(monkeypatch):
    """max_items bleibt eine Sicherheits-Obergrenze: bei Erreichen wird sauber gekappt UND als
    unvollstaendig markiert (damit der Import-Reconcile pausiert)."""
    c = _client()

    async def _tok(): return "tok"
    monkeypatch.setattr(c, "_get_user_token", _tok)
    http = _PagedHttp({1: (3, [1, 2]), 2: (3, [3, 4]), 3: (3, [5])})
    monkeypatch.setattr(c, "_http", lambda: http)

    out = await c.get_active_listings(max_items=3)
    assert len(out) == 3                                    # an der Obergrenze gekappt
    assert http.requested == [1, 2]                        # danach keine weitere Seite mehr
    assert c._last_active_fetch_complete is False           # gekappt -> NICHT vollstaendig


@pytest.mark.asyncio
async def test_get_active_listings_empty_intermediate_page_is_incomplete(monkeypatch):
    """Leere NICHT-letzte Seite (transienter eBay-Blip): Fetch gilt als unvollstaendig, damit der
    Reconcile keine live Listings faelschlich beendet."""
    c = _client()

    async def _tok(): return "tok"
    monkeypatch.setattr(c, "_get_user_token", _tok)
    http = _PagedHttp({1: (4, [11, 12]), 2: (4, [])})       # Seite 2 leer, obwohl total=4
    monkeypatch.setattr(c, "_http", lambda: http)

    out = await c.get_active_listings()
    assert [r["item_id"] for r in out] == ["11", "12"]
    assert http.requested == [1, 2]                        # bei der leeren Zwischenseite gestoppt
    assert c._last_active_fetch_complete is False           # unvollstaendig -> Reconcile pausiert


@pytest.mark.asyncio
async def test_get_active_listings_missing_pagination_on_page2_is_incomplete(monkeypatch):
    """Residual-Fund: Seite 1 meldet total=4, Seite 2 kommt OHNE PaginationResult (->_parse-Default 1)
    UND leer zurueck. total_pages muss STICKY bei 4 bleiben, sonst wuerde `page>=total_pages` faelschlich
    True und der unvollstaendige Fetch als komplett gelten (-> Reconcile wuerde live Listings beenden)."""
    c = _client()

    async def _tok(): return "tok"
    monkeypatch.setattr(c, "_get_user_token", _tok)

    class _Degenerate:
        def __init__(self): self.requested = []

        async def post(self, url, headers=None, content=None):
            import re
            body = content.decode() if isinstance(content, (bytes, bytearray)) else content
            page = int(re.search(r"<PageNumber>(\d+)</PageNumber>", body).group(1))
            self.requested.append(page)
            if page == 1:
                text = _active_xml(4, [11, 12])            # Seite 1: total=4, 2 Items
            else:
                # Seite 2: gueltig, Ack=Success, aber KEIN PaginationResult + leeres ItemArray
                text = ("<?xml version='1.0' encoding='utf-8'?>"
                        "<GetMyeBaySellingResponse xmlns='urn:ebay:apis:eBLBaseComponents'>"
                        "<Ack>Success</Ack><ActiveList><ItemArray></ItemArray></ActiveList>"
                        "</GetMyeBaySellingResponse>")

            class _R:
                def __init__(self, t): self.text = t
                def raise_for_status(self): return None
            return _R(text)

    http = _Degenerate()
    monkeypatch.setattr(c, "_http", lambda: http)

    out = await c.get_active_listings()
    assert [r["item_id"] for r in out] == ["11", "12"]     # nur Seite 1
    assert c._last_active_fetch_complete is False           # sticky total_pages=4 -> NICHT als komplett gewertet


@pytest.mark.asyncio
async def test_get_active_listings_missing_pagination_on_page1_is_incomplete(monkeypatch):
    """Residual-Fund (Seite 1): degenerierte SEITE-1-Antwort (echte Items, aber KEIN
    PaginationResult) hat keinen frueheren Anker fuer Stickiness. Sie darf NICHT als vollstaendig
    gelten (sonst wuerde `page(1)>=total_pages(1)` True und der Reconcile live Listings beenden)."""
    c = _client()

    async def _tok(): return "tok"
    monkeypatch.setattr(c, "_get_user_token", _tok)

    class _Degenerate1:
        def __init__(self): self.requested = []

        async def post(self, url, headers=None, content=None):
            import re
            body = content.decode() if isinstance(content, (bytes, bytearray)) else content
            self.requested.append(int(re.search(r"<PageNumber>(\d+)</PageNumber>", body).group(1)))
            # Seite 1: Ack=Success, ActiveList + echte Items, aber KEIN PaginationResult
            text = ("<?xml version='1.0' encoding='utf-8'?>"
                    "<GetMyeBaySellingResponse xmlns='urn:ebay:apis:eBLBaseComponents'>"
                    "<Ack>Success</Ack><ActiveList><ItemArray>"
                    "<Item><ItemID>77</ItemID><Title>T77</Title><SKU>S77</SKU>"
                    "<SellingStatus><CurrentPrice currencyID='EUR'>9.95</CurrentPrice></SellingStatus>"
                    "<Quantity>3</Quantity></Item>"
                    "</ItemArray></ActiveList></GetMyeBaySellingResponse>")

            class _R:
                def __init__(self, t): self.text = t
                def raise_for_status(self): return None
            return _R(text)

    http = _Degenerate1()
    monkeypatch.setattr(c, "_http", lambda: http)

    out = await c.get_active_listings()
    assert [r["item_id"] for r in out] == ["77"]            # Items der Seite werden mitgenommen
    assert http.requested == [1]                            # keine weitere Seite
    assert c._last_active_fetch_complete is False           # fehlendes PaginationResult -> unvollstaendig


@pytest.mark.asyncio
async def test_bulk_update_price_chunks_over_25(monkeypatch):
    """eBay bulkUpdatePriceQuantity erlaubt max. 25 SKUs/Request (errorId 25712).

    Multivarianten-Listings (z.B. 72 Poster-Varianten) muessen gechunkt werden,
    sonst schlaegt der komplette Mengen-Push (u.a. OOS -> Menge 0) fehl."""
    c = _client()

    async def _fake_headers(*a, **k):
        return {}
    monkeypatch.setattr(c, "_auth_headers", _fake_headers)
    batches: list[int] = []

    class _Cap:
        async def post(self, url, headers=None, json=None):
            batches.append(len(json["requests"]))
            return _FakeResp({})
    monkeypatch.setattr(c, "_http", lambda: _Cap())

    updates = [{"sku": f"S{i}", "offer_id": f"O{i}", "quantity": 0} for i in range(72)]
    await c.bulk_update_price(updates)
    assert batches == [25, 25, 22]        # kein Batch > 25
    assert sum(batches) == 72             # alle 72 abgedeckt


@pytest.mark.asyncio
async def test_user_token_success_caches(monkeypatch):
    c = _client()
    c._user_token_expiry = 0
    fake = _FakeHttp({"access_token": "AT-123", "expires_in": 7200})
    monkeypatch.setattr(c, "_http", lambda: fake)
    assert await c._get_user_token() == "AT-123"
    assert c._user_token == "AT-123" and c._user_token_expiry > time.monotonic()
    # zweiter Aufruf nutzt Cache -> kein weiterer POST
    assert await c._get_user_token() == "AT-123"
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_user_token_missing_access_token_is_persistent(monkeypatch):
    """200er ohne access_token wird auf PersistentError gemappt, kein roher KeyError."""
    c = _client()
    c._user_token_expiry = 0
    monkeypatch.setattr(c, "_http", lambda: _FakeHttp({"error": "invalid_grant"}))
    with pytest.raises(PersistentError):
        await c._get_user_token()


def test_production_mock_guard():
    from app.integrations import _ensure_safe_mock_usage

    with pytest.raises(RuntimeError):
        _ensure_safe_mock_usage(Settings(app_env="production", use_mocks=True))
    # Erlaubte Kombinationen werfen nicht. mock_ebay MUSS hier ausdruecklich auf
    # None: Settings liest sonst die lokale .env mit, und dort steht seit dem
    # Probebetrieb MOCK_EBAY=true. Der Einzelschalter hat Vorrang vor use_mocks -
    # die Attrappe waere also an, der Waechter schluege an, und der Test fiele
    # ueber die Konfiguration des Entwicklers statt ueber das Programm.
    _ensure_safe_mock_usage(Settings(app_env="production", use_mocks=False, mock_ebay=None))
    _ensure_safe_mock_usage(Settings(app_env="development", use_mocks=True))

    # Und der Einzelschalter selbst: Attrappe in Produktion bleibt verboten, auch
    # wenn use_mocks aus ist. Genau diese Lage stellt die .env gerade her.
    with pytest.raises(RuntimeError):
        _ensure_safe_mock_usage(Settings(app_env="production", use_mocks=False, mock_ebay=True))


# ----------------------------------------------------------------- Fehler-Mapping
def _status_error(code: int, headers=None) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://api.ebay.com/x")
    resp = httpx.Response(code, headers=headers or {}, request=req, text="boom")
    return httpx.HTTPStatusError("e", request=req, response=resp)


def test_translate_rate_limit():
    mapped = _client()._translate(_status_error(429, {"retry-after": "12"}))
    assert isinstance(mapped, RateLimitError) and mapped.retry_after == 12.0


def test_translate_server_error_transient():
    assert isinstance(_client()._translate(_status_error(503)), TransientError)


def test_translate_client_error_persistent():
    assert isinstance(_client()._translate(_status_error(400)), PersistentError)


def test_translate_network_error_transient():
    err = httpx.ConnectError("down")
    assert isinstance(_client()._translate(err), TransientError)


# ----------------------------------------------------------------- Parsing
def test_parse_traffic_report_views_as_clicks():
    payload = {
        "header": {"metrics": [
            {"key": "LISTING_IMPRESSION_TOTAL"},
            {"key": "LISTING_VIEWS_TOTAL"},
            {"key": "CLICK_THROUGH_RATE"},
        ]},
        "records": [{"metricValues": [{"value": "120"}, {"value": "7"}, {"value": "5.8"}]}],
    }
    a = RealEbayClient._parse_traffic_report(payload, "12345")
    assert a.impressions == 120
    assert a.clicks == 7  # LISTING_VIEWS_TOTAL als Naeherung
    assert a.listing_id == "12345"


def test_parse_traffic_report_empty_records():
    a = RealEbayClient._parse_traffic_report({"header": {"metrics": []}, "records": []}, "9")
    assert a.impressions == 0 and a.clicks == 0


def test_parse_order():
    payload = {
        "orderId": "06-12345-67890",
        "salesRecordReference": "SR-77",
        "pricingSummary": {"total": {"value": "29.99", "currency": "EUR"}},
    }
    o = RealEbayClient._parse_order(payload)
    assert o.ebay_order_id == "06-12345-67890"
    assert o.transaction_id == "SR-77"
    assert o.invoice_amount == 29.99
    assert o.currency == "EUR"


def test_sanitize_offer_drops_readonly():
    offer = {"offerId": "1", "listing": {"listingId": "x"}, "status": "PUBLISHED",
             "sku": "s", "marketplaceId": "EBAY_DE", "format": "FIXED_PRICE",
             "categoryId": "111", "availableQuantity": 5}
    patch = RealEbayClient._sanitize_offer(offer)
    assert "offerId" not in patch and "listing" not in patch and "status" not in patch
    assert patch["categoryId"] == "111" and patch["marketplaceId"] == "EBAY_DE"


# ----------------------------------------------------------------- Account-Deletion
def test_account_deletion_challenge_response_known_vector():
    import hashlib

    code, token, url = "abc123", "my-verification-token-1234567890", "https://x.example/ebay"
    expected = hashlib.sha256((code + token + url).encode()).hexdigest()
    assert RealEbayClient.account_deletion_challenge_response(code, token, url) == expected
    assert len(expected) == 64  # HEX, nicht Base64


# ----------------------------------------------------------------- Signatur (ECDSA/SHA1)
def _build_signed(body: bytes, kid: str = "kid-1"):
    priv = ec.generate_private_key(ec.SECP256R1())
    der = priv.sign(body, ec.ECDSA(hashes.SHA1()))
    pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    header = base64.b64encode(json.dumps(
        {"alg": "ECDSA", "kid": kid, "signature": base64.b64encode(der).decode(), "digest": "SHA1"}
    ).encode()).decode()
    return header, pem


def test_verify_signature_valid(monkeypatch):
    c = _client()
    body = b'{"notificationId":"n1","data":{"x":1}}'
    header, pem = _build_signed(body)
    monkeypatch.setattr(c, "_fetch_public_key_pem", lambda kid: pem)
    assert c.verify_ipn_signature(body, header) is True


def test_verify_signature_tampered_body(monkeypatch):
    c = _client()
    body = b'{"notificationId":"n1"}'
    header, pem = _build_signed(body)
    monkeypatch.setattr(c, "_fetch_public_key_pem", lambda kid: pem)
    assert c.verify_ipn_signature(b'{"notificationId":"TAMPERED"}', header) is False


def test_verify_signature_empty_or_malformed():
    c = _client()
    assert c.verify_ipn_signature(b"x", "") is False
    assert c.verify_ipn_signature(b"x", "not-base64-json!!") is False


def test_decode_signature_header_roundtrip():
    header, _ = _build_signed(b"abc", kid="kid-XYZ")
    decoded = RealEbayClient._decode_signature_header(header)
    assert decoded is not None and decoded[0] == "kid-XYZ"
    assert isinstance(decoded[1], bytes) and len(decoded[1]) > 0
