"""Entwuerfe und fehlgeschlagene Produkte im Dashboard loeschen."""
import pytest

from app.database import SessionLocal
from app.studio import angebote, ebay_weg
from app.studio.models import PodListing, PodOrder, PodProduct


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.close()


def _produkt(db, status):
    p = PodProduct(design_id=None, produktart="tshirt", title="Entwurfstest", status=status, provider="eigen")
    db.add(p)
    db.commit()
    return p


def test_fehler_und_entwurf_lassen_sich_loeschen(db):
    for status in ("fehler", "draft"):
        p = _produkt(db, status)
        pid = p.id
        assert angebote.loesche_entwurf(db, pid)["geloescht"] == pid
        assert db.get(PodProduct, pid) is None


def test_aktives_produkt_und_aktives_angebot_sind_geschuetzt(db):
    p = _produkt(db, "active")
    try:
        with pytest.raises(angebote.AngebotFehler):
            angebote.loesche_entwurf(db, p.id)
        p.status = "draft"
        li = PodListing(product_id=p.id, channel=ebay_weg.KANAL, status="active", price_eur=1.0)
        db.add(li)
        db.commit()
        with pytest.raises(angebote.AngebotFehler):
            angebote.loesche_entwurf(db, p.id)
    finally:
        db.query(PodListing).filter_by(product_id=p.id).delete()
        db.delete(p)
        db.commit()


def test_bestellung_verhindert_loeschen(db):
    p = _produkt(db, "draft")
    li = PodListing(product_id=p.id, channel=ebay_weg.KANAL, status="ended", price_eur=1.0)
    db.add(li)
    db.commit()
    o = PodOrder(listing_id=li.id, channel=ebay_weg.KANAL, status="new")
    db.add(o)
    db.commit()
    try:
        with pytest.raises(angebote.AngebotFehler):
            angebote.loesche_entwurf(db, p.id)
    finally:
        db.delete(o); db.delete(li); db.delete(p); db.commit()


def test_unbekannter_eintrag(db):
    with pytest.raises(angebote.AngebotFehler):
        angebote.loesche_entwurf(db, 987654)
