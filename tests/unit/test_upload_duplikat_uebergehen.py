"""Ein beendeter Artikel darf den erneuten Upload nicht sperren (Vorfall 21.08.).

Wajjahat hatte ein Listing auf eBay geloescht und wollte den Artikel neu anlegen.
Das Programm meldete „existiert bereits" — er fragte aber nur, ob IRGENDEIN
Listing eine eBay-Artikelnummer hat. Die bleibt fuer immer an der Zeile stehen,
auch nach dem Beenden. Der Artikel war damit dauerhaft gesperrt.

Zwei Antworten darauf, beide hier abgesichert:
1. Nur ein Listing, das nach unserem Stand noch AKTIV ist, sperrt.
2. Weil unser Stand veraltet sein kann, gibt es den bewussten Uebergeh-Knopf.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.integrations.aliexpress import MockAliExpressClient
from app.models import Listing, Product
from app.services import product_service

_URL1 = "https://de.aliexpress.com/item/1005099998888.html"
_URL2 = "https://www.aliexpress.com/item/1005099998888.html?spm=a2g0o.x"


class _FesteId(MockAliExpressClient):
    """Jede URL liefert dieselbe Ware – so wie AliExpress-Spiegel-URLs es tun."""

    async def scrape_product(self, url: str):
        return replace(await super().scrape_product(url), aliexpress_id="1005099998888")


@pytest.fixture(autouse=True)
def _ae(monkeypatch):
    monkeypatch.setattr(product_service, "get_aliexpress_client", lambda: _FesteId())


def _erstanlage(db):
    return asyncio.run(product_service.upload_product(db, aliexpress_url=_URL1, skip_autods=True))


def _setze(db, listing_id, *, status, item_id="1122334455"):
    l = db.get(Listing, listing_id)
    l.listing_status, l.ebay_item_id = status, item_id
    db.commit()
    return l


# ------------------------------------------------------------- Sperre nur bei aktiv
@pytest.mark.parametrize("status", ["ended", "deleted", "completed", "error"])
def test_beendetes_listing_sperrt_nicht_mehr(db, status):
    r1 = _erstanlage(db)
    _setze(db, r1["listing_id"], status=status)

    r2 = asyncio.run(product_service.upload_product(db, aliexpress_url=_URL2, skip_autods=True))

    assert r2["listing_id"] != r1["listing_id"], "es muss ein NEUER Entwurf entstehen"
    assert r2["product_id"] == r1["product_id"]          # dieselbe Ware, kein Zweit-Produkt
    assert len(db.scalars(select(Product)).all()) == 1


def test_aktives_listing_sperrt_weiterhin(db):
    """Der Schutz vor doppelt-live bleibt – nur eben zutreffend."""
    r1 = _erstanlage(db)
    _setze(db, r1["listing_id"], status="active")

    with pytest.raises(product_service.DuplikatListing) as ei:
        asyncio.run(product_service.upload_product(db, aliexpress_url=_URL2, skip_autods=True))

    assert ei.value.listing_id == r1["listing_id"]
    assert ei.value.listing_status == "active"


def test_das_alte_listing_bleibt_unberuehrt(db):
    """Ein beendetes Listing ist Historie – die Buchhaltung haengt daran."""
    r1 = _erstanlage(db)
    _setze(db, r1["listing_id"], status="ended", item_id="777666555")

    asyncio.run(product_service.upload_product(db, aliexpress_url=_URL2, skip_autods=True))

    alt = db.get(Listing, r1["listing_id"])
    assert alt.listing_status == "ended"
    assert alt.ebay_item_id == "777666555"


# -------------------------------------------------------------- bewusstes Uebergehen
def test_knopf_geht_ueber_die_sperre(db):
    r1 = _erstanlage(db)
    _setze(db, r1["listing_id"], status="active")

    r2 = asyncio.run(product_service.upload_product(
        db, aliexpress_url=_URL2, skip_autods=True, ignoriere_duplikat=True))

    assert r2["listing_id"] != r1["listing_id"]
    assert db.get(Listing, r1["listing_id"]).listing_status == "active"   # live bleibt live


def test_ohne_knopf_bleibt_es_gesperrt(db):
    """Der Standardweg darf sich NICHT geaendert haben – sonst waere der Schutz weg."""
    r1 = _erstanlage(db)
    _setze(db, r1["listing_id"], status="active")

    with pytest.raises(product_service.DuplikatListing):
        asyncio.run(product_service.upload_product(db, aliexpress_url=_URL2, skip_autods=True))


# ------------------------------------------------- der neue Entwurf muss brauchbar sein
def test_neuer_entwurf_bekommt_eine_draft_id(db, monkeypatch):
    """Sonst haette der Knopf einen Entwurf erzeugt, der sich nie live stellen laesst.

    Frueher hing das Anlegen des eBay-Entwurfs daran, ob das PRODUKT schon bekannt
    ist – nicht daran, ob es einen Entwurf zum Wiederverwenden gibt.
    """
    r1 = _erstanlage(db)
    _setze(db, r1["listing_id"], status="ended")

    r2 = asyncio.run(product_service.upload_product(db, aliexpress_url=_URL2))

    assert db.get(Listing, r2["listing_id"]).ebay_draft_id, "Entwurf ohne Draft-ID ist nicht live zu stellen"


def test_vorhandener_entwurf_wird_weiter_wiederverwendet(db):
    """Kein Zweit-Entwurf, wenn schon einer da ist – das bisherige Verhalten."""
    r1 = _erstanlage(db)

    r2 = asyncio.run(product_service.upload_product(db, aliexpress_url=_URL2, skip_autods=True))

    assert r2["listing_id"] == r1["listing_id"]
    assert len(db.scalars(select(Listing)).all()) == 1


# ------------------------------------------------ die Form, auf die das Dashboard baut
def test_api_meldet_die_sperre_maschinenlesbar(db, client):
    """Ohne diese Felder kann das Dashboard keinen Knopf anbieten, sondern nur eine
    Sackgasse melden – genau das war das Problem."""
    r1 = _erstanlage(db)
    _setze(db, r1["listing_id"], status="active")

    r = client.post("/api/v1/products/upload", json={"aliexpress_url": _URL2, "skip_autods": True})

    assert r.status_code == 409                      # eigener Status, nicht der Sammel-400
    d = r.json()["detail"]
    assert d["code"] == "duplikat"
    assert d["listing_id"] == r1["listing_id"]
    assert d["listing_status"] == "active"
    assert "live" in d["message"].lower()            # Klartext fuer den Toast


def test_api_laesst_das_uebergehen_durch(db, client):
    r1 = _erstanlage(db)
    _setze(db, r1["listing_id"], status="active")

    r = client.post("/api/v1/products/upload",
                    json={"aliexpress_url": _URL2, "skip_autods": True,
                          "ignoriere_duplikat": True})

    assert r.status_code == 201
    assert r.json()["listing_id"] != r1["listing_id"]
