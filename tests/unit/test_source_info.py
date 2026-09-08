"""source_info-Diagnose: SKU-Achsen (inkl. „Ships From") robust extrahieren, nie werfen."""
from __future__ import annotations

from app.services import product_research_service as research

_RAW = {"aliexpress_ds_product_get_response": {"result": {
    "logistics_info_dto": {"delivery_time": 7, "ship_to_country": "DE"},
    "ae_item_sku_info_dtos": {"ae_item_sku_info_d_t_o": [
        {"sku_attr": "200007763:201336100#CHINA",
         "ae_sku_property_dtos": {"ae_sku_property_d_t_o": [
             {"sku_property_id": 200007763, "sku_property_name": "Ships From",
              "sku_property_value": "CHINA"}]}},
        {"sku_attr": "200007763:201336103#GERMANY",
         "ae_sku_property_dtos": {"ae_sku_property_d_t_o":
             {"sku_property_id": 200007763, "sku_property_name": "Ships From",
              "sku_property_value": "GERMANY"}}},
    ]},
}}}


class _FakeAE:
    def __init__(self, raw=_RAW, freight=None, fail=False):
        self._raw, self._freight, self._fail = raw, freight, fail

    async def _call(self, method, business):  # noqa: ARG002
        if self._fail:
            raise RuntimeError("API down")
        return self._raw

    async def query_freight(self, *, product_id, sku_id=None, country=None, quantity=1):  # noqa: ARG002
        if self._freight is None:
            raise RuntimeError("freight down")
        return self._freight


async def test_source_info_extracts_ships_from_axis(monkeypatch):
    monkeypatch.setattr(research, "_real_ae",
                        lambda: _FakeAE(freight={"delivery_days": 5}))
    r = await research.source_info("1005001")
    assert r["logistics_info_dto"]["delivery_time"] == 7
    assert r["axes"] == {"Ships From [id=200007763]": ["CHINA", "GERMANY"]}
    assert r["skus"][0]["properties"][0]["value"] == "CHINA"
    assert r["freight"] == {"delivery_days": 5}


async def test_source_info_never_raises(monkeypatch):
    monkeypatch.setattr(research, "_real_ae", lambda: _FakeAE(fail=True))
    r = await research.source_info("42")
    assert "error" in r and "freight_error" in r
    assert r["aliexpress_id"] == "42"
