"""Angebote bei eBay deaktivieren und loeschen (mit Attrappe statt echtem eBay)."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.studio import angebote, ebay_weg
from app.studio.models import PodListing, PodProduct, StudioDesign


class FakeEbay:
    def __init__(self):
        self.aufrufe: list[tuple] = []

    async def withdraw_offer_by_group(self, key):
        self.aufrufe.append(("gruppe_beenden", key))

    async def erstes_angebot_zu_sku(self, sku):
        self.aufrufe.append(("suche", sku))
        return {"offerId": "OFFER1"}

    async def withdraw_offer(self, offer_id):
        self.aufrufe.append(("beenden", offer_id))

    async def delete_inventory_item(self, sku):
        self.aufrufe.append(("artikel_loeschen", sku))

    async def delete_inventory_item_group(self, key):
        self.aufrufe.append(("gruppe_loeschen", key))


@pytest.fixture
def db():
    sitzung = SessionLocal()
    yield sitzung
    sitzung.close()


def _anlegen(db, produkt_key):
    d = StudioDesign(title="Beendentest", status="draft", source="test")
    db.add(d)
    db.commit()
    prod = PodProduct(design_id=d.id, produktart=produkt_key, title="t", status="active", provider="eigen")
    db.add(prod)
    db.commit()
    li = PodListing(product_id=prod.id, channel=ebay_weg.KANAL, external_id="123", status="active",
                    price_eur=14.9, quantity_available=5, url="https://www.ebay.de/itm/123")
    db.add(li)
    db.commit()
    return d, prod, li


def _aufraeumen(db, d, prod, li):
    db.delete(li)
    db.delete(prod)
    db.delete(d)
    db.commit()


def test_deaktivieren_beendet_die_gruppe_und_behaelt_die_artikel(db):
    d, prod, li = _anlegen(db, "tshirt")
    try:
        fake = FakeEbay()
        erg = asyncio.run(ebay_weg.beende(db, d, produkt_key="tshirt", ebay=fake, s=get_settings()))
        assert erg["status"] == "ended"
        assert fake.aufrufe == [("gruppe_beenden", ebay_weg.gruppe(d.id, ebay_weg.PRODUKTE["tshirt"]))]
        db.refresh(li)
        assert li.status == "ended"
        assert ebay_weg.aktives_angebot(db, d.id, "tshirt") is None
        assert [a["status"] for a in angebote.uebersicht(db, get_settings()) if a["design_id"] == d.id] == ["ended"]
    finally:
        _aufraeumen(db, d, prod, li)


def test_loeschen_entfernt_alle_varianten_und_die_gruppe(db):
    d, prod, li = _anlegen(db, "tshirt")
    try:
        fake = FakeEbay()
        s, p = get_settings(), ebay_weg.PRODUKTE["tshirt"]
        asyncio.run(ebay_weg.beende(db, d, produkt_key="tshirt", ebay=fake, s=s, loeschen=True))
        geloescht = [a[1] for a in fake.aufrufe if a[0] == "artikel_loeschen"]
        assert len(geloescht) == len(ebay_weg.farben(p)) * len(ebay_weg.groessen(p, s))
        assert ("gruppe_loeschen", ebay_weg.gruppe(d.id, p)) in fake.aufrufe
        db.refresh(li)
        assert li.status == "deleted"
        assert [a for a in angebote.uebersicht(db, s) if a["design_id"] == d.id] == []
    finally:
        _aufraeumen(db, d, prod, li)


def test_tasse_wird_ueber_das_offer_beendet(db):
    d, prod, li = _anlegen(db, "tasse")
    try:
        fake = FakeEbay()
        asyncio.run(ebay_weg.beende(db, d, produkt_key="tasse", ebay=fake, s=get_settings()))
        assert ("beenden", "OFFER1") in fake.aufrufe
    finally:
        _aufraeumen(db, d, prod, li)


def test_ohne_angebot_kommt_ein_klarer_fehler(db):
    d = StudioDesign(title="Leer", status="draft", source="test")
    db.add(d)
    db.commit()
    try:
        with pytest.raises(ebay_weg.EbayWegFehler):
            asyncio.run(ebay_weg.beende(db, d, produkt_key="tshirt", ebay=FakeEbay(), s=get_settings()))
    finally:
        db.delete(d)
        db.commit()
