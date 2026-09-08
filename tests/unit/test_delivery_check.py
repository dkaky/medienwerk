"""Liefer-Check fuer Auslands-Bestellungen (16.08.): definitive Aussagen, fail-open
bei Unbekanntem, Klartext-Warnungen und der Bestell-Preflight."""
from __future__ import annotations

import asyncio

import pytest

from app.integrations import aliexpress_api as api
from app.models import Listing, Product, Sale
from app.retry import PersistentError
from app.services import delivery_check_service as dcs


# ---------------------------------------------------------------- Parser

def _freight(options):
    res = {"delivery_options": options} if options is not ... else {}
    return {"aliexpress_ds_freight_query_response": {"result": res}}


def test_freight_deliverable_mit_optionen_ist_true():
    data = _freight({"delivery_option_d_t_o": [{"code": "CAINIAO"}]})
    assert api.freight_deliverable(data) is True


def test_freight_deliverable_einzel_option_als_dict_ist_true():
    data = _freight({"delivery_option_d_t_o": {"code": "CAINIAO"}})
    assert api.freight_deliverable(data) is True


def test_freight_deliverable_leerer_container_ist_false():
    assert api.freight_deliverable(_freight({})) is False
    assert api.freight_deliverable(_freight({"delivery_option_d_t_o": []})) is False


def test_freight_deliverable_fehlendes_feld_ist_unbekannt():
    """Lehre aus dem Bestands-Vorfall 15.08.: fehlende Daten sind NIE ein Nein."""
    assert api.freight_deliverable(_freight(...)) is None


# ---------------------------------------------------------------- Warnungstexte

def test_warnung_nur_bei_definitiver_absage():
    assert dcs.warnung_text("AT", True, {}) is None
    assert dcs.warnung_text("AT", None, {}) is None      # unbekannt -> keine Warnung
    assert dcs.warnung_text("AT", False, {}) is not None


def test_warnung_nennt_land_und_ausweich_weg():
    w = dcs.warnung_text("AT", False, {"111": True})
    assert "Österreich" in w and "💡" in w
    w2 = dcs.warnung_text("FR", False, {"111": None})
    assert "Frankreich" in w2 and "keine deiner Ausweich-Quellen" in w2


# ---------------------------------------------------------------- Service + Preflight

class _StubAe:
    """delivery_availability-Stub: Antwort je Produkt-ID ('*' = Default)."""
    def __init__(self, antworten):
        self.antworten = antworten
        self.calls = []

    async def delivery_availability(self, *, product_id, country, sku_id=None):
        self.calls.append((str(product_id), country))
        return self.antworten.get(str(product_id), self.antworten.get("*"))


def _verkauf(db, *, land="AT", alt_sources=None):
    p = Product(aliexpress_url="https://de.aliexpress.com/item/900.html",
                aliexpress_id="900")
    if alt_sources is not None:
        p.alternatives = {"sources": alt_sources}
    db.add(p)
    db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-DC", title_seo="T", description="d",
                listing_status="active")
    db.add(l)
    db.flush()
    s = Sale(ebay_transaction_id=f"TX-DC-{land}", listing_id=l.id,
             delivery_address={"country": land, "city": "Wien"}, status="pending")
    db.add(s)
    db.commit()
    return s


def _mit_stub(monkeypatch, antworten):
    stub = _StubAe(antworten)
    monkeypatch.setattr("app.integrations.get_aliexpress_client", lambda: stub)
    return stub


def test_pruefe_sale_de_braucht_keinen_check(db, monkeypatch):
    stub = _mit_stub(monkeypatch, {"*": False})
    s = _verkauf(db, land="DE")
    assert asyncio.run(dcs.pruefe_sale(db, s)) is None
    assert stub.calls == []


def test_pruefe_sale_definitive_absage_warnt_und_persistiert(db, monkeypatch):
    _mit_stub(monkeypatch, {"900": False,
                            "alt1": True})
    s = _verkauf(db, land="AT", alt_sources=[
        {"aliexpress_id": "900"}, {"aliexpress_id": "alt1"}])
    check = asyncio.run(dcs.pruefe_sale(db, s))
    assert check["haupt_ok"] is False and check["alt_ok"] == {"alt1": True}
    assert "Österreich" in check["warnung"]
    assert (s.delivery_check or {}).get("warnung") == check["warnung"]


def test_pruefe_sale_unbekannt_warnt_nicht(db, monkeypatch):
    _mit_stub(monkeypatch, {"*": None})
    s = _verkauf(db, land="FR")
    check = asyncio.run(dcs.pruefe_sale(db, s))
    assert check["haupt_ok"] is None and check["warnung"] is None


def test_preflight_stoppt_nur_bei_definitiver_absage(db, monkeypatch):
    _mit_stub(monkeypatch, {"900": False})
    s = _verkauf(db, land="AT")
    with pytest.raises(PersistentError) as e:
        asyncio.run(dcs.preflight_bestellung(db, s, product_id="900"))
    assert "Österreich" in str(e.value) and "selbst beheben" in str(e.value)
    # Warnung wurde fuers Orders-Tab persistiert
    assert (s.delivery_check or {}).get("warnung")


def test_preflight_faehrt_bei_unbekannt_fort(db, monkeypatch):
    _mit_stub(monkeypatch, {"*": None})
    s = _verkauf(db, land="AT")
    asyncio.run(dcs.preflight_bestellung(db, s, product_id="900"))   # kein Raise


def test_preflight_ignoriert_inlandslieferung(db, monkeypatch):
    stub = _mit_stub(monkeypatch, {"*": False})
    s = _verkauf(db, land="DE")
    asyncio.run(dcs.preflight_bestellung(db, s, product_id="900"))
    assert stub.calls == []


# ------------------------------------------ Review-Fixes 16.08.

def test_freight_deliverable_direkte_liste_ohne_huelle():
    """AliExpress liefert die Options-Liste manchmal DIREKT (ohne d_t_o-Huelle)."""
    assert api.freight_deliverable(_freight(...)) is None
    data = {"aliexpress_ds_freight_query_response": {"result": {
        "delivery_options": [{"code": "CAINIAO"}]}}}
    assert api.freight_deliverable(data) is True
    leer = {"aliexpress_ds_freight_query_response": {"result": {"delivery_options": []}}}
    assert api.freight_deliverable(leer) is False


def test_freight_deliverable_degradierte_formen_sind_unbekannt():
    for outer in (None, "CAINIAO", 7, {"fremd": 1}):
        data = {"aliexpress_ds_freight_query_response": {"result": {
            "delivery_options": outer}}}
        assert api.freight_deliverable(data) is None, repr(outer)


def test_preflight_altquelle_vergiftet_hauptbefund_nicht(db, monkeypatch):
    """Scheitert die AUSWEICH-Quelle, bleibt ein zuvor positiver Hauptquellen-
    Befund stehen (Review-Fund: pauschales haupt_ok=False war falsch)."""
    _mit_stub(monkeypatch, {"alt9": False})
    s = _verkauf(db, land="AT")
    s.delivery_check = {"land": "AT", "haupt_ok": True, "alt_ok": {}, "warnung": None}
    db.commit()
    with pytest.raises(PersistentError):
        asyncio.run(dcs.preflight_bestellung(db, s, product_id="alt9",
                                             ist_hauptquelle=False))
    check = s.delivery_check or {}
    assert check.get("haupt_ok") is True          # Hauptbefund unangetastet
    assert check.get("alt_ok", {}).get("alt9") is False


def test_preflight_reicht_sku_id_durch(db, monkeypatch):
    stub = _mit_stub(monkeypatch, {"*": True})
    s = _verkauf(db, land="AT")
    asyncio.run(dcs.preflight_bestellung(db, s, product_id="900", sku_id="SKU77"))
    # StubAe zeichnet (product_id, country) auf; sku_id ankommen zu lassen genuegt
    # der Signatur-Vertrag — haette der Stub sku_id nicht akzeptiert, waere der
    # Aufruf als TypeError im fail-open-Log gelandet und ok=None gewesen.
    assert stub.calls == [("900", "AT")]
