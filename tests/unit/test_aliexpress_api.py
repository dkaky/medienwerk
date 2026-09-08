"""Tests fuer die AliExpress-Open-Platform-Protokollschicht + RealAliExpressClient."""
from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.integrations import aliexpress_api as api
from app.integrations.aliexpress import ProductNotFoundError, RealAliExpressClient

# --------------------------------------------------------------- Produkt-ID
@pytest.mark.parametrize("url,expected", [
    ("https://de.aliexpress.com/item/1005006789012345.html", "1005006789012345"),
    ("https://www.aliexpress.com/item/4000999888777.html?spm=a2g0o", "4000999888777"),
    ("https://aliexpress.com/i/1005001234567890.html", "1005001234567890"),
    ("https://de.aliexpress.com/p/x?productId=1005002223334445", "1005002223334445"),
    ("https://amazon.de/dp/B0xyz", None),
])
def test_extract_product_id(url, expected):
    assert api.extract_product_id(url) == expected


# --------------------------------------------------------------- Signatur
def test_sign_sha256_matches_hmac_spec():
    params = {"app_key": "K", "method": "m", "product_id": "1", "timestamp": "1700000000000"}
    base = "".join(f"{k}{params[k]}" for k in sorted(params))
    expected = hmac.new(b"SECRET", base.encode(), hashlib.sha256).hexdigest().upper()
    assert api.sign(params, "SECRET", "sha256") == expected
    assert len(expected) == 64


def test_sign_md5_wraps_secret():
    params = {"a": "1", "b": "2"}
    base = "a1b2"
    expected = hashlib.md5(f"SECRET{base}SECRET".encode()).hexdigest().upper()
    assert api.sign(params, "SECRET", "md5") == expected
    assert len(expected) == 32


def test_sign_ignores_sign_key_and_is_order_independent():
    a = api.sign({"b": "2", "a": "1", "sign": "X"}, "S", "sha256")
    b = api.sign({"a": "1", "b": "2"}, "S", "sha256")
    assert a == b


def test_signed_params_has_system_fields_and_valid_sign():
    p = api.signed_params("aliexpress.ds.product.get", {"product_id": "9"},
                          app_key="K", app_secret="S", sign_method="sha256",
                          timestamp="1700000000000")
    for key in ("app_key", "method", "format", "v", "sign_method", "timestamp", "sign", "product_id"):
        assert key in p
    # sign muss sich aus den uebrigen Feldern reproduzieren lassen
    recomputed = api.sign({k: v for k, v in p.items() if k != "sign"}, "S", "sha256")
    assert p["sign"] == recomputed


# --------------------------------------------------------------- Parser
_SAMPLE = {
    "aliexpress_ds_product_get_response": {
        "result": {
            "ae_item_base_info_dto": {
                "product_id": 1005006789012345,
                "subject": "Auto Detailing Set Mikrofaser",
                "detail": "<p>Beschreibung</p>",
                "category_id": 200001234,
            },
            "ae_multimedia_info_dto": {"image_urls": "https://img/1.jpg;https://img/2.jpg"},
            "ae_item_sku_info_dtos": {"ae_item_sku_info_d_t_o": [
                {"sku_id": "a1", "sku_attr": "14:200", "offer_sale_price": "12.50",
                 "sku_available_stock": "30"},
                {"sku_id": "a2", "sku_attr": "14:201", "offer_sale_price": "9.90",
                 "sku_available_stock": "0"},
            ]},
            "ae_store_info": {"store_id": 4400123, "store_evaluate_rate": "97.5"},
        }
    }
}


def test_parse_product_extracts_core_fields():
    p = api.parse_product(_SAMPLE)
    assert p["aliexpress_id"] == "1005006789012345"
    assert p["title_raw"].startswith("Auto Detailing")
    assert p["description_raw"] == "<p>Beschreibung</p>"
    assert p["price_cny"] == Decimal("9.90")            # Minimum der SKU-Preise
    assert p["images"] == ["https://img/1.jpg", "https://img/2.jpg"]
    assert p["supplier_id"] == "4400123"
    assert p["supplier_rating"] == Decimal("97.5")
    assert p["in_stock"] is True                         # 30 + 0 > 0
    assert len(p["variants"]["skus"]) == 2
    assert p["category_id"] == "200001234"


def test_parse_product_extracts_specs_and_variant_axes():
    data = {"aliexpress_ds_product_get_response": {"result": {
        "ae_item_base_info_dto": {"product_id": 1, "subject": "Kette"},
        "ae_item_properties": {"ae_item_property": [
            {"attr_name": "Metalltyp", "attr_value": "Edelstahl"},
            {"attr_name": "Anlass", "attr_value": "Jahrestag"},
            {"attr_name": "Material festlegen", "attr_value": "Keine"},   # Muell -> raus
            {"attr_name": "Choice", "attr_value": "yes"},                 # Muell -> raus
        ]},
        "ae_item_sku_info_dtos": {"ae_item_sku_info_d_t_o": [
            {"offer_sale_price": "6.09", "sku_available_stock": "10", "ae_sku_property_dtos":
                {"ae_sku_property_d_t_o": [
                    {"sku_property_name": "Metallfarbe", "sku_property_value": "Gold"},
                    {"sku_property_name": "Länge", "sku_property_value": "45 cm"}]}},
            {"offer_sale_price": "6.09", "sku_available_stock": "5", "ae_sku_property_dtos":
                {"ae_sku_property_d_t_o": [
                    {"sku_property_name": "Metallfarbe", "sku_property_value": "Silber"},
                    {"sku_property_name": "Länge", "sku_property_value": "60 cm"}]}},
        ]},
    }}}
    p = api.parse_product(data)
    names = [s["name"] for s in p["specs"]]
    assert "Metalltyp" in names and "Anlass" in names
    assert "Material festlegen" not in names and "Choice" not in names   # gefiltert
    axes = p["variants"]["axes"]
    assert axes["Metallfarbe"] == ["Gold", "Silber"]
    assert axes["Länge"] == ["45 cm", "60 cm"]


@pytest.mark.parametrize("raw,expected", [
    ("50x70cm", "50 x 70 cm"),        # Vorfall 21.07.: durfte NICHT '70 cm' werden
    ("40x60cm", "40 x 60 cm"),
    ("50x70 cm", "50 x 70 cm"),
    ("50×70cm", "50 x 70 cm"),        # Unicode-×
    ("40*55cm", "40 x 55 cm"),        # Stern-Trenner
    ("5.5x6.5cm", "5.5 x 6.5 cm"),    # Dezimal
    ("30X40cmNoframe", "30 x 40 cm"), # Gross-X + Textrest
    ("50x70", "50 x 70"),             # ohne Einheit -> beide Zahlen behalten
    ("45cm or 17.7 Inches", "45 cm"), # Einzelmaß mit Zoll-Umrechnung -> nur cm (unveraendert)
    ("45 cm", "45 cm"),
    ("Gold Color", "Gold"),
    ("Yoshi", "Yoshi"),
])
def test_clean_option_value_preserves_multidimensional_measurements(raw, expected):
    assert api._clean_option_value(raw) == expected


def test_explain_empty_product_surfaces_real_reason():
    """Statt pauschal 'nicht verfuegbar' den echten Grund/Beleg zeigen (self-diagnostizierend)."""
    # a) explizite AliExpress-Fehlermeldung
    msg = api.explain_empty_product("123", {"aliexpress_ds_product_get_response": {
        "rsp_code": "500", "rsp_msg": "system busy, try later"}})
    assert "system busy" in msg and "123" in msg
    # b) leeres result -> DS-Katalog/Lieferland-Hinweis + vorhandene Felder als Beleg
    msg2 = api.explain_empty_product("999", {"aliexpress_ds_product_get_response": {"result": {}}})
    assert "999" in msg2 and ("DS-Katalog" in msg2 or "Dropshipping" in msg2)
    assert "nicht (mehr) verfuegbar" not in msg2      # keine irrefuehrende Loeschungs-Behauptung


def test_parse_product_keeps_full_size_dimension_in_axis():
    """Roh 'LxBcm' Größen-Achse -> beide Kanten bleiben (nicht nur die Breite)."""
    data = {"aliexpress_ds_product_get_response": {"result": {
        "ae_item_base_info_dto": {"product_id": 1005012221830157, "subject": "Mario Poster"},
        "ae_item_sku_info_dtos": {"ae_item_sku_info_d_t_o": [
            {"offer_sale_price": "5.69", "sku_available_stock": "5", "ae_sku_property_dtos":
                {"ae_sku_property_d_t_o": [
                    {"sku_property_name": "Motiv", "sku_property_value": "Yoshi"},
                    {"sku_property_name": "Größe", "sku_property_value": "50x70cm"}]}},
            {"offer_sale_price": "5.69", "sku_available_stock": "5", "ae_sku_property_dtos":
                {"ae_sku_property_d_t_o": [
                    {"sku_property_name": "Motiv", "sku_property_value": "Mario"},
                    {"sku_property_name": "Größe", "sku_property_value": "40x60cm"}]}},
        ]},
    }}}
    p = api.parse_product(data)
    axes = p["variants"]["axes"]
    assert axes["Größe"] == ["50 x 70 cm", "40 x 60 cm"]      # volle L×B, nicht ['70 cm','60 cm']
    assert p["variants"]["skus"][0]["options"]["Größe"] == "50 x 70 cm"


def test_parse_product_out_of_stock_when_all_skus_zero():
    data = {"aliexpress_ds_product_get_response": {"result": {
        "ae_item_base_info_dto": {"product_id": 1, "subject": "x"},
        "ae_item_sku_info_dtos": {"ae_item_sku_info_d_t_o": [
            {"offer_sale_price": "5.00", "sku_available_stock": "0"}]},
    }}}
    p = api.parse_product(data)
    assert p["in_stock"] is False


def test_parse_product_missing_stock_fields_is_not_out_of_stock():
    """Vorfall 15.08.: AliExpress liess die Bestandsfelder komplett weg -> Produkte
    wurden faelschlich genullt. Ohne Bestandsangabe entscheidet der Produkt-Status."""
    data = {"aliexpress_ds_product_get_response": {"result": {
        "ae_item_base_info_dto": {"product_id": 1, "subject": "x",
                                  "product_status_type": "onSelling"},
        "ae_item_sku_info_dtos": {"ae_item_sku_info_d_t_o": [
            {"offer_sale_price": "5.00"},              # KEIN Bestandsfeld
            {"offer_sale_price": "6.00"}]},
    }}}
    assert api.parse_product(data)["in_stock"] is True


def test_parse_product_missing_stock_and_offline_status_is_out_of_stock():
    data = {"aliexpress_ds_product_get_response": {"result": {
        "ae_item_base_info_dto": {"product_id": 1, "subject": "x",
                                  "product_status_type": "offline"},
        "ae_item_sku_info_dtos": {"ae_item_sku_info_d_t_o": [
            {"offer_sale_price": "5.00"}]},
    }}}
    assert api.parse_product(data)["in_stock"] is False


def test_parse_product_missing_stock_without_status_stays_available():
    data = {"aliexpress_ds_product_get_response": {"result": {
        "ae_item_base_info_dto": {"product_id": 1, "subject": "x"},
        "ae_item_sku_info_dtos": {"ae_item_sku_info_d_t_o": [
            {"offer_sale_price": "5.00"}]},
    }}}
    assert api.parse_product(data)["in_stock"] is True


def test_parse_product_reported_zero_stock_still_wins_over_status():
    data = {"aliexpress_ds_product_get_response": {"result": {
        "ae_item_base_info_dto": {"product_id": 1, "subject": "x",
                                  "product_status_type": "onSelling"},
        "ae_item_sku_info_dtos": {"ae_item_sku_info_d_t_o": [
            {"offer_sale_price": "5.00", "sku_available_stock": "0"}]},
    }}}
    assert api.parse_product(data)["in_stock"] is False


def test_parse_tracking_defensive():
    data = {"aliexpress_ds_order_tracking_get_response": {"result": {
        "mail_no": "LP123456789DE", "logistics_service_name": "AliExpress Standard",
        "official_status": "in_transit"}}}
    t = api.parse_tracking(data)
    assert t["tracking_number"] == "LP123456789DE"
    assert t["carrier"] == "AliExpress Standard"
    assert t["status"] == "in_transit"


async def test_get_tracking_returns_none_when_not_yet_available(monkeypatch):
    """Kurz nach dem Kauf gibt es noch keine finale Nummer -> tracking.get meldet
    'TRACKING DATA NOT FOUND'. get_tracking gibt dann None zurueck und faellt NICHT
    auf die provisorische Cainiao-Nummer aus trade.ds.order.get zurueck."""
    client = _client()
    calls = []

    async def fake_call(method, business):
        calls.append(method)
        assert method == "aliexpress.ds.order.tracking.get"  # nur diese API!
        return {"aliexpress_ds_order_tracking_get_response": {"result": {
            "ret": False, "msg": "TRACKING DATA NOT FOUND", "code": "1001"}}}

    monkeypatch.setattr(client, "_call", fake_call)
    t = await client.get_tracking("3074700364032059")
    assert t.tracking_number is None
    assert calls == ["aliexpress.ds.order.tracking.get"]  # kein order.get-Fallback


# --------------------------------------------------------------- Client
def _settings(**over):
    base = dict(
        aliexpress_app_key="K", aliexpress_app_secret="S",
        aliexpress_api_base="https://api-sg.aliexpress.com/sync",
        aliexpress_sign_method="sha256", aliexpress_ship_to="DE",
        aliexpress_target_currency="CNY", aliexpress_target_language="de",
        aliexpress_access_token="TESTTOKEN", aliexpress_refresh_token="",
        aliexpress_token_file="./_does_not_exist_token.json",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _client(**over) -> RealAliExpressClient:
    return RealAliExpressClient(_settings(**over))


async def test_scrape_product_builds_scraped_product(monkeypatch):
    async def fake_call(client, **kwargs):
        assert kwargs["method"] == "aliexpress.ds.product.get"
        assert kwargs["business"]["product_id"] == "1005006789012345"
        return _SAMPLE
    monkeypatch.setattr(api, "call", fake_call)

    sp = await _client().scrape_product("https://de.aliexpress.com/item/1005006789012345.html")
    assert sp.aliexpress_id == "1005006789012345"
    assert sp.price_cny == Decimal("9.90")
    assert sp.in_stock is True
    assert len(sp.images) == 2


async def test_scrape_product_without_id_raises():
    with pytest.raises(ProductNotFoundError):
        await _client().scrape_product("https://amazon.de/x")


def test_real_client_requires_keys():
    from app.retry import PersistentError
    c = _client(aliexpress_app_key="", aliexpress_app_secret="")
    with pytest.raises(PersistentError):
        c._require_keys()


# --------------------------------------------------------------- Token / Auto-Refresh
def test_token_store_roundtrip(tmp_path):
    import time as _t
    p = str(tmp_path / "tok.json")
    api.save_token_store(p, access_token="A", refresh_token="R", expires_in=120)
    d = api.load_token_store(p)
    assert d["access_token"] == "A" and d["refresh_token"] == "R"
    assert d["expires_at"] > _t.time()
    assert api.load_token_store(str(tmp_path / "missing.json")) is None


def test_is_token_error_heuristic():
    assert api.is_token_error("InvalidAccessToken: token expired")
    assert api.is_token_error("illegal token")
    assert not api.is_token_error("MissingParameter access_token is mandatory")
    assert not api.is_token_error("IncompleteSignature")


async def test_refresh_on_token_error_then_retry(tmp_path, monkeypatch):
    import time as _t
    seen = []

    async def fake_call(client, **kw):
        seen.append((kw["method"], (kw["business"] or {}).get("access_token")))
        if kw["method"] == "/auth/token/refresh":
            return {"access_token": "NEW", "refresh_token": "RT2", "expires_in": 86400}
        if (kw["business"] or {}).get("access_token") == "NEW":
            return _SAMPLE
        raise api.AliExpressApiError("InvalidAccessToken token has expired")
    monkeypatch.setattr(api, "call", fake_call)

    c = _client(aliexpress_refresh_token="RT", aliexpress_token_file=str(tmp_path / "t.json"))
    c._token_expiry = _t.time() + 99999       # gueltig -> kein proaktiver Refresh
    sp = await c.scrape_product("https://de.aliexpress.com/item/1005006789012345.html")
    assert sp.aliexpress_id == "1005006789012345"
    assert any(m == "/auth/token/refresh" for m, _ in seen)   # Refresh wurde ausgeloest
    assert c._access_token == "NEW"
    # neuer Token wurde persistiert
    assert api.load_token_store(str(tmp_path / "t.json"))["access_token"] == "NEW"


async def test_proactive_refresh_when_expired(tmp_path, monkeypatch):
    import time as _t

    async def fake_call(client, **kw):
        if kw["method"] == "/auth/token/refresh":
            return {"access_token": "NEW", "refresh_token": "RT2", "expires_in": 86400}
        assert (kw["business"] or {}).get("access_token") == "NEW"   # nur frischer Token
        return _SAMPLE
    monkeypatch.setattr(api, "call", fake_call)

    c = _client(aliexpress_refresh_token="RT", aliexpress_token_file=str(tmp_path / "t.json"))
    c._token_expiry = _t.time() - 10          # abgelaufen -> proaktiver Refresh
    sp = await c.scrape_product("https://de.aliexpress.com/item/1005006789012345.html")
    assert sp.aliexpress_id == "1005006789012345"
    assert c._access_token == "NEW"


async def test_no_token_and_no_refresh_raises(tmp_path, monkeypatch):
    from app.retry import PersistentError

    async def fake_call(client, **kw):
        return _SAMPLE
    monkeypatch.setattr(api, "call", fake_call)

    c = _client(aliexpress_access_token="", aliexpress_refresh_token="",
                aliexpress_token_file=str(tmp_path / "none.json"))
    with pytest.raises(PersistentError):
        await c.scrape_product("https://de.aliexpress.com/item/1005006789012345.html")


def test_logistics_address_strips_special_chars():
    """AliExpress lehnt Vorwahl/Nummer mit Sonderzeichen ab (Fall SpongeBob/Sale 810)."""
    from app.integrations.aliexpress import RealAliExpressClient as R
    # 1) Keine Telefonnummer -> Vorwahl '49', Platzhalter-Nummer, KEIN '+'
    a = R._logistics_address("Eric Beer", {"country": "DE", "phone": None}, "DE")
    assert a["phone_country"] == "49"
    assert a["mobile_no"].isdigit() and "+" not in a["mobile_no"]
    # 2) Nummer mit Sonderzeichen + doppelter Vorwahl -> bereinigt
    b = R._logistics_address("X", {"country": "DE", "phone": "+49 1512-345 678"}, "DE")
    assert b["phone_country"] == "49"
    assert b["mobile_no"] == "1512345678"
    # 3) Nationale Nummer mit fuehrender 0
    c = R._logistics_address("X", {"country": "DE", "phone": "0151/2345678"}, "DE")
    assert c["mobile_no"] == "1512345678"


def test_logistics_address_strips_country_prefix_from_postal():
    """Bestellung 1256 (AT): Kaeufer-PLZ 'A-1010'/'AT 1010' -> Praefix weg, das
    Bundesland kommt trotzdem aus der PLZ. NL-Format bleibt unangetastet."""
    from app.integrations.aliexpress import RealAliExpressClient as R
    # Seit 12.08.: AT-Provinz geht direkt als ENGLISCHER Divisionslisten-Name raus.
    a = R._logistics_address("Max", {"country": "AT", "city": "Wien",
                                     "street": "Ring 1", "postal": "A-1010"}, "DE")
    assert a["zip"] == "1010" and a["province"] == "Vienna"
    b = R._logistics_address("Max", {"country": "AT", "city": "Graz",
                                     "street": "X 1", "postal": "AT 8010"}, "DE")
    assert b["zip"] == "8010" and b["province"] == "Styria"
    nl = R._logistics_address("Jan", {"country": "NL", "city": "Amsterdam",
                                      "street": "X 1", "postal": "1234 AB"}, "DE")
    assert nl["zip"] == "1234 AB"


def test_fallback_city_for_unknown_at_villages():
    """'Please enter a City' (Sale 1123/1256): Ersatz-Stadt aus der PLZ-Zone — nur AT,
    nie wenn die Stadt schon die Hauptstadt ist."""
    from app.integrations.aliexpress import RealAliExpressClient as R
    assert R._fallback_city("AT", "3443", "Sieghartskirchen") == "St. Pölten"
    assert R._fallback_city("AT", "6850", "Dornbirn") == "Bregenz"    # Vorarlberg 67-69
    assert R._fallback_city("AT", "9900", "Assling") == "Lienz"      # Osttirol 99xx
    assert R._fallback_city("AT", "1010", "Wien") is None             # schon Hauptstadt
    assert R._fallback_city("DE", "50667", "Koeln") is None           # nur AT
    assert R._fallback_city("AT", None, "X") is None


def test_city_english_fallback_known():
    """Sale 1256: 'Wien' lehnt die AT-Staedteliste ab — englischer Name als Retry."""
    from app.integrations.aliexpress import RealAliExpressClient as R
    assert R._CITY_EN.get("wien") == "Vienna"
    assert R._CITY_EN.get("hamburg") is None      # nur bekannte Uebersetzungsfaelle


async def test_order_create_expands_italian_province_code(monkeypatch):
    """Sale 1266: eBay liefert den IT-Provinzcode ('GE'), AliExpress will den vollen
    Namen ('Genova'). Retry-Kette: Code -> voller Name -> Hauptort als Stadt."""
    import json as _json
    import pytest
    from app.integrations.aliexpress import OrderRejectedError
    client = _client()
    tried = []

    async def fake_call(method, business):
        req = _json.loads(business["param_place_order_request4_open_api_d_t_o"])
        la = req["logistics_address"]
        tried.append((la.get("province"), la.get("city")))
        return {"aliexpress_ds_order_create_response": {"result": {
            "is_success": False, "error_msg": "Please select a State/Province/County",
            "error_code": "B_DROPSHIPPER_DELIVERY_ADDRESS_VALIDATE_FAIL"}}}

    monkeypatch.setattr(client, "_call", fake_call)
    with pytest.raises(OrderRejectedError):
        await client._order_create(
            product_items=[{"product_id": "1", "product_count": 1}],
            delivery_name="Mario",
            delivery_address={"country": "IT", "city": "Serra Riccò Genova",
                              "province": "GE", "postal": "16010",
                              "street": "Via Roma 1", "phone": "3331234567"})
    # Live-Erkenntnis Sale 1266: AliExpress' IT-Liste fuehrt REGIONEN, nicht Provinzen.
    assert tried[0] == ("GE", "Serra Riccò Genova")
    assert tried[1] == ("Liguria", "Serra Riccò Genova")
    assert tried[2] == ("Liguria", "Genova"), tried
    assert len(tried) == 3


async def test_order_create_at_division_list_fallback(monkeypatch):
    """Sale 1256/1274 (Aufloesung 12.08.): AliExpress' AT-Liste kennt nur ENGLISCHE
    Bundeslaender und BEZIRKE als 'Stadt' -> Staffel endet mit dem produktiv
    bewaehrten city='Other' (Ort in der Adresszeile) und 'Other'/'Other'."""
    import json as _json
    import pytest
    from app.integrations.aliexpress import OrderRejectedError
    client = _client()
    tried = []

    async def fake_call(method, business):
        req = _json.loads(business["param_place_order_request4_open_api_d_t_o"])
        la = req["logistics_address"]
        tried.append((la.get("province"), la.get("city"), la.get("address") or ""))
        return {"aliexpress_ds_order_create_response": {"result": {
            "is_success": False, "error_msg": "Please enter a City",
            "error_code": "B_DROPSHIPPER_DELIVERY_ADDRESS_VALIDATE_FAIL"}}}

    monkeypatch.setattr(client, "_call", fake_call)
    with pytest.raises(OrderRejectedError):
        await client._order_create(
            product_items=[{"product_id": "1", "product_count": 1}],
            delivery_name="Max",
            delivery_address={"country": "AT", "city": "Fügen",
                              "street": "Dorfstraße 1", "postal": "6263",
                              "phone": "06641234567"})
    pc = [(p, c) for p, c, _ in tried]
    # Erster Versuch ist SOFORT der Divisionslisten-konforme: EN-Bundesland +
    # echte Stadt (klappt direkt, wenn die Stadt ein Bezirksname ist).
    assert pc[0] == ("Tyrol", "Fügen")
    # Danach: EN-Bundesland + city='Other' (Ort in der Adresszeile), zuletzt
    # 'Other'/'Other'. Tote Varianten (Tirol, Innsbruck, Austria, leer) sind raus.
    assert pc == [("Tyrol", "Fügen"), ("Tyrol", "Other"), ("Other", "Other")]
    for p, c, address in tried:
        if c == "Other":
            assert "Fügen" in address, "Ort muss in der Adresszeile stehen"
    # province ist IMMER als Key dabei (MissingParameter-Live-Befund 12.08.).
    assert all(p is not None for p, _, _ in tried)


async def test_order_create_param_error_tries_next_candidate(monkeypatch):
    """Sale 1274 (12.08.): MissingParameter bei EINER Adress-Variante brach die
    ganze Kette ab -> jetzt laeuft die naechste Variante, am Ende kommt die
    Ablehnung MIT Versuchsliste. Geld-sicher: Parameter-Fehler = nicht bestellt."""
    import pytest
    from app.integrations.aliexpress import OrderRejectedError
    from app.retry import PersistentError
    client = _client()
    calls = []

    async def fake_call(method, business):
        calls.append(1)
        raise PersistentError("AliExpress-API-Fehler: MissingParameter province")

    monkeypatch.setattr(client, "_call", fake_call)
    with pytest.raises(OrderRejectedError) as exc:
        await client._order_create(
            product_items=[{"product_id": "1", "product_count": 1}],
            delivery_name="Max",
            delivery_address={"country": "AT", "city": "Graz", "province": "Steiermark",
                              "postal": "8010", "phone": "0676123"})
    assert len(calls) == 3, "alle Varianten muessen trotz Parameter-Fehler drankommen"
    assert "Adress-Versuche" in str(exc.value)


def test_de_ascii_transliteration():
    from app.integrations.aliexpress import RealAliExpressClient as R
    assert R._de_ascii("Fügen") == "Fuegen"
    assert R._de_ascii("Groß-Enzersdorf") == "Gross-Enzersdorf"
    assert R._de_ascii("Innsbruck") == "Innsbruck"


def test_validate_buyer_accepts_prefixed_postcode():
    """Die Vorpruefung darf eine Praefix-PLZ nicht mehr als 'ungueltig' blocken."""
    from types import SimpleNamespace
    from app.services.order_service import validate_buyer
    ok = SimpleNamespace(buyer_name="Max Muster", buyer_email=None,
                         delivery_address={"country": "AT", "postal": "A-1010"})
    assert validate_buyer(ok) == []
    bad = SimpleNamespace(buyer_name="Max Muster", buyer_email=None,
                          delivery_address={"country": "AT", "postal": "10105"})
    assert any("PLZ" in p for p in validate_buyer(bad))


# --------------------------------------------------------------- freight query
def test_parse_freight_picks_cheapest_tracked_option():
    """parse_freight -> guenstigste TRACKBARE Option; shipping_fee_cent ist bereits EUR."""
    resp = {"aliexpress_ds_freight_query_response": {"result": {"success": True,
        "delivery_options": {"delivery_option_d_t_o": [
            {"code": "CAINIAO_STANDARD", "free_shipping": False, "tracking": True,
             "max_delivery_days": 13, "company": "AliExpress-Standardversand",
             "shipping_fee_cent": "3.29"}]}}}}
    fr = api.parse_freight(resp)
    assert fr == {"fee_eur": 3.29, "free_shipping": False,
                  "carrier": "AliExpress-Standardversand", "delivery_days": 13, "tracking": True}


def test_parse_freight_prefers_tracked_over_free_untracked():
    """Eine gratis, aber untrackbare Option wird NICHT gewaehlt (wir brauchen Tracking fuer eBay)
    -> die guenstigste trackbare gewinnt, auch wenn sie etwas kostet (keine Kosten-Unterschaetzung)."""
    resp = {"x_response": {"result": {"delivery_options": {"delivery_option_d_t_o": [
        {"code": "A", "free_shipping": True, "tracking": False, "shipping_fee_cent": "0"},
        {"code": "B", "free_shipping": False, "tracking": True, "shipping_fee_cent": "2.50"}]}}}}
    fr = api.parse_freight(resp)
    assert fr["tracking"] is True and fr["fee_eur"] == 2.50


def test_parse_freight_handles_comma_decimal_and_free():
    resp = {"x_response": {"result": {"delivery_options": {"delivery_option_d_t_o": [
        {"code": "F", "free_shipping": True, "tracking": True, "shipping_fee_cent": "0"}]}}}}
    assert api.parse_freight(resp) == {"fee_eur": 0.0, "free_shipping": True,
                                       "carrier": "F", "delivery_days": None, "tracking": True}
    # keine/leere Optionen -> None (Kalkulation faellt auf Pauschale zurueck)
    assert api.parse_freight({"x_response": {"result": {"delivery_options": {}}}}) is None
    assert api.parse_freight({}) is None


# ---------------------- AT-Bundesland aus PLZ + Adress-Retry (Vorfall 1123) ----------------------
def test_derive_province_at_from_plz():
    R = RealAliExpressClient
    assert R._derive_province("AT", "3443") == "Niederösterreich"     # Fall Sale 1123
    assert R._derive_province("AT", "1010") == "Wien"
    assert R._derive_province("AT", "6850") == "Vorarlberg"           # 68xx -> Vorarlberg
    assert R._derive_province("AT", "9900") == "Tirol"                # Osttirol
    assert R._derive_province("AT", "8010") == "Steiermark"
    assert R._derive_province("AT", "3443", english=True) == "Lower Austria"
    assert R._derive_province("DE", "50667") is None                  # DE: kein Mapping (Stadt-Fallback ok)
    assert R._derive_province("AT", None) is None


def test_logistics_address_at_derives_bundesland_and_phone_43():
    """AT-Adresse ohne Bundesland: PLZ-Ableitung statt Stadt-Fallback (AliExpress lehnt
    unbekannte Provinzen bei AT ab) + Telefon-Vorwahl 43 statt stur 49."""
    from app.integrations.aliexpress import RealAliExpressClient as R
    a = R._logistics_address("Max Muster", {
        "country": "AT", "city": "Rappoltenkirchen", "street": "Bonnastraße 37",
        "postal": "3443", "phone": "06769283550"}, "DE")
    assert a["province"] == "Lower Austria"   # seit 12.08.: direkt EN-Listen-Name
    assert a["phone_country"] == "43"
    assert a["mobile_no"] == "6769283550"
    # DE unveraendert: echtes Bundesland aus der Adresse hat Vorrang, Vorwahl 49
    d = R._logistics_address("Max", {"country": "DE", "city": "Teuchern",
                                     "province": "Sachsen-Anhalt", "postal": "06682",
                                     "phone": "017692289082"}, "DE")
    assert d["province"] == "Sachsen-Anhalt" and d["phone_country"] == "49"


def test_logistics_address_titlecases_lowercase_city():
    """Komplett kleingeschriebene Stadt ('hohenems') -> Title-Case, sonst lehnt die
    AliExpress-Staedte-Validierung ab ('Please enter a City', Sale 1143). Gemischte
    Schreibweisen bleiben unangetastet."""
    from app.integrations.aliexpress import RealAliExpressClient as R
    a = R._logistics_address("Tugay U", {"country": "AT", "city": "hohenems",
                                         "street": "fohrenweg 8", "postal": "6845",
                                         "phone": "061395811"}, "DE")
    assert a["city"] == "Hohenems"
    assert a["province"] == "Vorarlberg"           # 68xx -> Vorarlberg (nicht Tirol)
    b = R._logistics_address("X", {"country": "AT", "city": "bad ischl",
                                   "postal": "4820"}, "DE")
    assert b["city"] == "Bad Ischl"
    c = R._logistics_address("X", {"country": "DE", "city": "St. Pölten",
                                   "postal": "12345"}, "DE")
    assert c["city"] == "St. Pölten"               # gemischt -> unangetastet


def test_logistics_address_phone_length_guard_and_lt_country():
    """AliExpress erlaubt 7-13 Ziffern (Sale 1147: 14-stelliger Tippfehler -> Fallback).
    Litauen (Sale 1144) bekommt Vorwahl 370 statt stur 49."""
    from app.integrations.aliexpress import RealAliExpressClient as R
    a = R._logistics_address("Mario K", {"country": "DE", "city": "Dierdorf",
                                         "postal": "56269",
                                         "phone": "026890123456789"}, "DE")  # 15 Ziffern roh
    assert 7 <= len(a["mobile_no"]) <= 13          # Fallback statt 14-stelliger Nummer
    assert a["mobile_no"] != "26890123456789"
    b = R._logistics_address("K Hohlweg", {"country": "LT", "city": "Marijampole",
                                           "postal": "68116", "phone": "65010226"}, "DE")
    assert b["phone_country"] == "370"
    assert b["mobile_no"] == "65010226"            # gueltige 8-stellige Nummer bleibt


def test_reject_message_translates_cryptic_codes():
    R = RealAliExpressClient
    m1 = R._reject_message({"error_code": "DELIVERY_METHOD_NOT_EXIST", "is_success": False})
    assert "abgelehnt" in m1 and "Bestand" in m1                      # Klartext-Hinweis
    m2 = R._reject_message({"error_code": "B_DROPSHIPPER_DELIVERY_ADDRESS_VALIDATE_FAIL"})
    assert "Lieferadresse" in m2
    m3 = R._reject_message({"error_code": "IRGENDWAS_NEUES"})
    assert m3.startswith("AliExpress-Bestellung abgelehnt")           # unbekannt -> roh
    m4 = R._reject_message({"error_code": "PRODUCT_NOT_EXIST", "is_success": False})
    assert "existiert" in m4 and "Ausweich-Quelle" in m4              # Klartext + Handlungshinweis


async def test_order_create_product_not_exist_raises_product_not_found(monkeypatch):
    """PRODUCT_NOT_EXIST bei der Bestellung = Quelle weg -> ProductNotFoundError (löst den Ausweich-
    Quellen-Fluss aus) statt generischer OrderRejectedError (totes 'needs_manual_review')."""
    from app.integrations.aliexpress import OrderRejectedError
    client = _client()

    async def fake_call(method, business):
        return {"aliexpress_ds_order_create_response": {"result": {
            "is_success": False, "error_code": "PRODUCT_NOT_EXIST"}}}
    monkeypatch.setattr(client, "_call", fake_call)

    with pytest.raises(ProductNotFoundError):
        await client._order_create(
            product_items=[{"product_id": "1", "product_count": 1}],
            delivery_name="Max Muster",
            delivery_address={"country": "DE", "city": "Berlin", "postal": "10115", "phone": "01512345"})


async def test_order_create_expired_token_is_rejected_not_uncertain(monkeypatch):
    """Vorfall 01.08. (Sales 1227/1228): abgelaufener Refresh-Token -> die Bestellung wurde NIE
    gesendet. Muss OrderRejectedError sein (Claim faellt zurueck, sauberer Retry moeglich), NICHT
    OrderUncertainError (das blockt den Verkauf und verlangt manuelle Pruefung im AE-Konto)."""
    from app.integrations.aliexpress import OrderRejectedError, OrderUncertainError
    client = _client()

    async def fail_token():
        raise api.AliExpressApiError(
            "{'code': '15', 'sub_code': 'IllegalRefreshToken', "
            "'sub_msg': 'The specified refresh token is invalid or expired'}")
    monkeypatch.setattr(client, "_ensure_token", fail_token)

    with pytest.raises(OrderRejectedError) as ei:
        await client._order_create(
            product_items=[{"product_id": "1", "product_count": 1}],
            delivery_name="Max Muster",
            delivery_address={"country": "DE", "city": "Berlin", "postal": "10115", "phone": "01512345"})
    msg = str(ei.value)
    assert "utorisier" in msg and "NICHTS" in msg          # handlungsleitend + geld-sicher
    assert not isinstance(ei.value, OrderUncertainError)


async def test_order_create_generic_reject_still_order_rejected(monkeypatch):
    """Eine NICHT-'Produkt-weg'-Ablehnung bleibt OrderRejectedError (kein Fehl-Routing in Alternativen)."""
    from app.integrations.aliexpress import OrderRejectedError
    client = _client()

    async def fake_call(method, business):
        return {"aliexpress_ds_order_create_response": {"result": {
            "is_success": False, "error_code": "SOME_OTHER_ERROR"}}}
    monkeypatch.setattr(client, "_call", fake_call)

    with pytest.raises(OrderRejectedError):
        await client._order_create(
            product_items=[{"product_id": "1", "product_count": 1}],
            delivery_name="Max Muster",
            delivery_address={"country": "DE", "city": "Berlin", "postal": "10115", "phone": "01512345"})


async def test_order_create_retries_address_validation_with_english_province(monkeypatch):
    """Abgelehnte Adresse (= sicher NICHT bestellt): seit 12.08. geht die Provinz
    SOFORT als englischer Listen-Name raus; der erste Retry ist die
    Divisions-Staffel (gleiches Bundesland, city='Other') — und ein Erfolg
    mittendrin beendet die Kette mit den Order-IDs."""
    import json as _json
    client = _client()
    provinces = []

    async def fake_call(method, business):
        assert method == "aliexpress.ds.order.create"
        req = _json.loads(business["param_place_order_request4_open_api_d_t_o"])
        provinces.append(req["logistics_address"]["province"])
        if len(provinces) == 1:
            return {"aliexpress_ds_order_create_response": {"result": {
                "is_success": False,
                "error_code": "B_DROPSHIPPER_DELIVERY_ADDRESS_VALIDATE_FAIL",
                "error_msg": "Please select a State/Province/County"}}}
        return {"aliexpress_ds_order_create_response": {"result": {
            "is_success": True, "order_id": "9990001"}}}

    monkeypatch.setattr(client, "_call", fake_call)
    ids = await client._order_create(
        product_items=[{"product_id": "1", "product_count": 1}],
        delivery_name="Max Muster",
        delivery_address={"country": "AT", "city": "Rappoltenkirchen",
                          "postal": "3443", "phone": "0676123"})
    assert ids == ["9990001"]
    assert provinces == ["Lower Austria", "Lower Austria"]


async def test_order_create_no_retry_when_address_had_own_province(monkeypatch):
    """Hat die Adresse ein ECHTES Bundesland, gibt es keine ABGELEITETEN Varianten
    (englisches Bundesland etc.). Seit Sale 1274 folgt fuer AT aber genau EIN
    Zusatzversuch OHNE Bundesland — die AT-Maske von AliExpress kennt keine
    Provinz-Ebene, ein gesetztes Bundesland kippt die Validierung."""
    import json as _json
    import pytest
    from app.integrations.aliexpress import OrderRejectedError
    client = _client()
    calls = []

    async def fake_call(method, business):
        calls.append(1)
        return {"aliexpress_ds_order_create_response": {"result": {
            "is_success": False,
            "error_code": "B_DROPSHIPPER_DELIVERY_ADDRESS_VALIDATE_FAIL"}}}

    monkeypatch.setattr(client, "_call", fake_call)
    with pytest.raises(OrderRejectedError, match="Lieferadresse"):
        await client._order_create(
            product_items=[{"product_id": "1", "product_count": 1}],
            delivery_name="Max",
            # Graz: eigenes Bundesland, kein englischer Namens-Fallback, Stadt =
            # Zonen-Hauptstadt -> Original + Divisions-Staffel (Styria/Other, Other/Other).
            delivery_address={"country": "AT", "city": "Graz", "province": "Steiermark",
                              "postal": "8010", "phone": "0676123"})
    assert len(calls) == 3


async def test_order_create_retries_with_english_city_name(monkeypatch):
    """Sale 1256, aktualisiert 12.08.: 'Vienna' als STADT steht gar nicht in der
    AT-Divisionsliste (dort sind es Bezirke) -> der englische Stadt-Retry ist fuer
    AT tot und wird uebersprungen; stattdessen direkt die 'Other'-Staffel.
    (Fuer CH bleibt der englische Stadtname-Retry aktiv.)"""
    import json as _json
    import pytest
    from app.integrations.aliexpress import OrderRejectedError
    client = _client()
    cities = []

    async def fake_call(method, business):
        req = _json.loads(business["param_place_order_request4_open_api_d_t_o"])
        cities.append(req["logistics_address"].get("city"))
        return {"aliexpress_ds_order_create_response": {"result": {
            "is_success": False, "error_msg": "Please enter a City",
            "error_code": "B_DROPSHIPPER_DELIVERY_ADDRESS_VALIDATE_FAIL"}}}

    monkeypatch.setattr(client, "_call", fake_call)
    with pytest.raises(OrderRejectedError):
        await client._order_create(
            product_items=[{"product_id": "1", "product_count": 1}],
            delivery_name="Max",
            delivery_address={"country": "AT", "city": "Wien", "province": "Wien",
                              "postal": "1030", "phone": "0676123"})
    assert cities[0] == "Wien", cities
    assert "Vienna" not in cities, "englischer Stadtname ist fuer AT tot (kein Bezirk)"
    # Seit 12.08. endet die AT-Kette mit der Divisions-Staffel (city='Other').
    assert cities[-2:] == ["Other", "Other"], cities


async def test_order_create_transient_error_never_retries_second_address(monkeypatch):
    """GELD-INVARIANTE: Timeout/Netzfehler = Ausgang UNKLAR -> NIE ein zweiter Geld-Call,
    auch wenn eine englische Adress-Variante bereitstuende (Doppelbestellungs-Schutz)."""
    from app.integrations.aliexpress import OrderUncertainError
    from app.retry import TransientError
    client = _client()
    calls = []

    async def fake_call(method, business):
        calls.append(1)
        raise TransientError("timeout")

    monkeypatch.setattr(client, "_call", fake_call)
    with pytest.raises(OrderUncertainError, match="unklar"):
        await client._order_create(
            product_items=[{"product_id": "1", "product_count": 1}],
            delivery_name="Max",
            delivery_address={"country": "AT", "city": "Rappoltenkirchen",
                              "postal": "3443", "phone": "0676123"})
    assert len(calls) == 1


async def test_order_create_missing_order_id_uncertain_single_call(monkeypatch):
    """GELD-INVARIANTE: Antwort ohne Order-ID -> Order KANN existieren -> OrderUncertainError
    nach genau EINEM Call, kein Adress-Retry."""
    from app.integrations.aliexpress import OrderUncertainError
    client = _client()
    calls = []

    async def fake_call(method, business):
        calls.append(1)
        return {"aliexpress_ds_order_create_response": {"result": {"is_success": True}}}

    monkeypatch.setattr(client, "_call", fake_call)
    with pytest.raises(OrderUncertainError, match="ohne Order-ID"):
        await client._order_create(
            product_items=[{"product_id": "1", "product_count": 1}],
            delivery_name="Max",
            delivery_address={"country": "AT", "city": "Rappoltenkirchen",
                              "postal": "3443", "phone": "0676123"})
    assert len(calls) == 1


async def test_order_create_non_address_reject_no_second_candidate_try(monkeypatch):
    """GELD-INVARIANTE: Ablehnung OHNE ADDRESS_VALIDATE (z.B. Bestand) -> sofort
    OrderRejectedError, KEIN zweiter Versuch trotz vorhandener englischer Adress-Variante."""
    from app.integrations.aliexpress import OrderRejectedError
    client = _client()
    calls = []

    async def fake_call(method, business):
        calls.append(1)
        return {"aliexpress_ds_order_create_response": {"result": {
            "is_success": False, "error_code": "INVENTORY_HOLD_ERROR"}}}

    monkeypatch.setattr(client, "_call", fake_call)
    with pytest.raises(OrderRejectedError, match="Bestand"):
        await client._order_create(
            product_items=[{"product_id": "1", "product_count": 1}],
            delivery_name="Max",
            delivery_address={"country": "AT", "city": "Rappoltenkirchen",
                              "postal": "3443", "phone": "0676123"})
    assert len(calls) == 1


def test_reject_message_finds_code_behind_250_chars():
    """Langer error_msg VOR dem error_code: der Hinweis darf nicht durch die
    250-Zeichen-Anzeige-Kuerzung verloren gehen (Code-Suche im Volltext)."""
    long = {"error_msg": "x" * 300, "error_code": "DELIVERY_METHOD_NOT_EXIST",
            "is_success": False}
    m = RealAliExpressClient._reject_message(long)
    assert "Bestand" in m                      # Hinweis trotz Kuerzung vorhanden
    assert len(m.split("\n")[0]) < 320         # Anzeige bleibt gekuerzt


def test_logistics_address_keeps_landline_with_cc_like_prefix():
    """AT-Festnetz '4352 12345' (Ortsvorwahl 4352 beginnt zufaellig mit '43') darf NICHT
    als Laendervorwahl gestrippt werden; echte Mobil-/Intl-Formate weiterhin schon."""
    from app.integrations.aliexpress import RealAliExpressClient as R
    a = R._logistics_address("Max", {"country": "AT", "city": "Gaming", "postal": "3292",
                                     "phone": "4352 12345"}, "DE")
    assert a["mobile_no"] == "435212345"       # unangetastet (kein Mobil-Muster nach '43')
    b = R._logistics_address("Max", {"country": "AT", "city": "Wien", "postal": "1010",
                                     "phone": "+43 676 9283550"}, "DE")
    assert b["mobile_no"] == "6769283550"      # explizit international -> gestrippt
    c = R._logistics_address("Max", {"country": "AT", "city": "Wien", "postal": "1010",
                                     "phone": "43676123456789"}, "DE")
    assert c["mobile_no"] == "676123456789"    # bare Mobil-Muster ('6' nach 43) -> gestrippt


async def test_order_create_token_ablauf_im_wiederholungspfad_ist_auch_abgelehnt(monkeypatch):
    """Zweiter Weg zum selben Ergebnis (gefunden 28.08.2026).

    Der Test darueber faelscht _ensure_token und deckt damit nur den VORAB-Pfad ab.
    Es gibt aber einen zweiten: laeuft der Access-Token erst waehrend des Aufrufs ab,
    faengt _call den Token-Fehler und erneuert MITTEN im except-Zweig. Scheitert die
    Erneuerung dort, verliess ihre Ausnahme _call roh - Geschwister-except-Zweige
    desselben try fangen nichts mehr, was in einem von ihnen geworfen wurde.

    Folge war "Ausgang unklar" statt "Es wurde NICHTS bestellt": Verkauf blockiert,
    Handpruefung im AliExpress-Konto verlangt - obwohl der erste Versuch nachweislich
    am Token abgelehnt wurde und damit sicher nichts ausgeloest hat.
    """
    from app.integrations.aliexpress import OrderRejectedError, OrderUncertainError
    client = _client()

    async def token_ok():
        return None
    monkeypatch.setattr(client, "_ensure_token", token_ok)

    async def raw_lehnt_wegen_token(method, business, is_auth):
        raise api.AliExpressApiError(
            "{'code': '15', 'sub_code': 'IllegalRefreshToken', "
            "'sub_msg': 'The specified refresh token is invalid or expired'}")
    monkeypatch.setattr(client, "_raw_call", raw_lehnt_wegen_token)

    async def erneuerung_scheitert(force=False):
        raise api.AliExpressApiError(
            "{'code': '15', 'sub_code': 'IllegalRefreshToken'}")
    monkeypatch.setattr(client, "_refresh_access_token", erneuerung_scheitert)
    client._refresh_token = "irgendwas"          # sonst wird gar nicht erst erneuert

    with pytest.raises(OrderRejectedError) as ei:
        await client._order_create(
            product_items=[{"product_id": "1", "product_count": 1}],
            delivery_name="Max Muster",
            delivery_address={"country": "DE", "city": "Berlin", "postal": "10115",
                              "phone": "01512345"})

    msg = str(ei.value)
    assert "utorisier" in msg and "NICHTS" in msg
    assert not isinstance(ei.value, OrderUncertainError)


async def test_order_create_401_bei_der_erneuerung_ist_auch_abgelehnt(monkeypatch):
    """Der Token-Endpunkt antwortet mit 401 als httpx-Fehler, nicht als AliExpressApiError.

    Eine Sperre nur fuer api.AliExpressApiError haette diesen Weg offen gelassen -
    bei einem Token-Endpunkt ist 401 mindestens so wahrscheinlich wie eine
    Gateway-Fehlermeldung.
    """
    import httpx

    from app.integrations.aliexpress import OrderRejectedError, OrderUncertainError
    client = _client()

    async def token_ok():
        return None
    monkeypatch.setattr(client, "_ensure_token", token_ok)

    async def raw_lehnt_wegen_token(method, business, is_auth):
        raise api.AliExpressApiError("{'sub_code': 'IllegalRefreshToken'}")
    monkeypatch.setattr(client, "_raw_call", raw_lehnt_wegen_token)

    async def erneuerung_401(force=False):
        raise httpx.HTTPStatusError(
            "401", request=httpx.Request("POST", "https://api-sg.aliexpress.com/rest"),
            response=httpx.Response(401))
    monkeypatch.setattr(client, "_refresh_access_token", erneuerung_401)
    client._refresh_token = "irgendwas"

    with pytest.raises(OrderRejectedError) as ei:
        await client._order_create(
            product_items=[{"product_id": "1", "product_count": 1}],
            delivery_name="Max Muster",
            delivery_address={"country": "DE", "city": "Berlin", "postal": "10115",
                              "phone": "01512345"})

    assert not isinstance(ei.value, OrderUncertainError)


async def test_echter_aussetzer_bei_der_erneuerung_bleibt_unklar(monkeypatch):
    """Gegenprobe: ein Netz-Aussetzer darf NICHT als "sicher nichts bestellt" gelten.

    Die Sperre reicht PersistentError und TransientError unveraendert weiter, genau
    wie der Vorab-Pfad. Sonst wuerde sie einen echten Zweifelsfall wegdefinieren -
    und das waere die gefaehrlichere Richtung.
    """
    from app.retry import TransientError
    client = _client()

    async def token_ok():
        return None
    monkeypatch.setattr(client, "_ensure_token", token_ok)

    async def raw_lehnt_wegen_token(method, business, is_auth):
        raise api.AliExpressApiError("{'sub_code': 'IllegalRefreshToken'}")
    monkeypatch.setattr(client, "_raw_call", raw_lehnt_wegen_token)

    async def erneuerung_haengt(force=False):
        raise TransientError("Zeitueberschreitung")
    monkeypatch.setattr(client, "_refresh_access_token", erneuerung_haengt)
    client._refresh_token = "irgendwas"

    with pytest.raises(Exception) as ei:
        await client._call("/x/y", {})

    assert isinstance(ei.value, TransientError), "Aussetzer bleibt Aussetzer"
