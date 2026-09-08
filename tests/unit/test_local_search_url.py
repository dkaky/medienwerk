"""Lokal-Fokus Trend-Suche (Nutzerfund 10.08.): Web-Suche mit DE-Lager-Filter."""
from app.services.product_research_service import _local_search_url


def test_local_search_url_has_warehouse_filter():
    u = _local_search_url("hunde spielzeug")
    assert u.startswith("https://de.aliexpress.com/w/wholesale-hunde-spielzeug.html")
    assert "shipFromCountry=DE" in u
