"""Startklar-Pruefung: rot blockiert, gelb warnt, gruen geht durch.

Der Anlass ist ein konkreter Irrtum: der Trockenlauf in
``publish-all-drafts?dry_run=true`` meldete "bereit: 30" fuer dreissig
Entwuerfe, obwohl er ausser dem Preis nichts geprueft hatte. Diese Tests halten
fest, was die neue Pruefung stattdessen sieht.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.models import Listing, Product
from app.services import startklar


_naechste_id = iter(range(1000, 9999))


def _produkt(db, *, bilder=None) -> Product:
    # Jedes Produkt braucht eine eigene aliexpress_id - die Spalte ist eindeutig,
    # und ein Test legt bewusst zwei Produkte an.
    kennung = str(next(_naechste_id))
    p = Product(
        aliexpress_url=f"https://example.invalid/item/{kennung}",
        aliexpress_id=kennung,
        title_raw="T-Shirt",
        images=bilder if bilder is not None else ["https://example.invalid/a.jpg"],
    )
    db.add(p)
    db.commit()
    return p


def _listing(db, product, **felder) -> Listing:
    vorgabe = dict(
        product_id=product.id,
        title_seo="T-Shirt Kurzarm Baumwolle Unisex Schwarz",
        description="Ein Shirt.",
        listing_status="draft",
        price_eur=Decimal("27.95"),
        cost_eur=Decimal("9.00"),
        quantity_available=20,
        item_specifics={"Marke": "Markenlos", "Material": "Baumwolle"},
        category_id="15687",
        category_name="Kleidung & Accessoires",
    )
    vorgabe.update(felder)
    l = Listing(**vorgabe)
    db.add(l)
    db.commit()
    return l


# --- gruen -----------------------------------------------------------------

def test_vollstaendiger_entwurf_ist_gruen(db):
    l = _listing(db, _produkt(db))
    z = startklar.pruefe_listing(l, db.get(Product, l.product_id))
    assert z.ampel == startklar.GRUEN
    assert z.befunde == []


def test_gruener_entwurf_liefert_gewinn_nach_gebuehren(db):
    """Die Marge muss die eBay-Gebuehren enthalten, sonst ist sie geschoent."""
    l = _listing(db, _produkt(db))
    z = startklar.pruefe_listing(l, db.get(Product, l.product_id))
    # VK 27,95 minus EK 9,00 waeren naiv 67 % - nach Provision und Fixgebuehr
    # bleibt deutlich weniger. Genau das ist der Sinn der Zahl.
    assert z.gewinn_eur is not None
    assert z.gewinn_eur < 27.95 - 9.00
    assert z.marge_pct is not None and z.marge_pct < 67


# --- rot: der Live-Gang wuerde scheitern -----------------------------------

def test_ohne_bilder_ist_rot(db):
    """``publish_listing_live`` bricht bei fehlenden Bildern selbst ab."""
    l = _listing(db, _produkt(db, bilder=[]))
    z = startklar.pruefe_listing(l, db.get(Product, l.product_id))
    assert z.ampel == startklar.ROT
    assert any(b.punkt == "bilder" for b in z.befunde)


def test_ohne_preis_ist_rot(db):
    l = _listing(db, _produkt(db), price_eur=None)
    z = startklar.pruefe_listing(l, db.get(Product, l.product_id))
    assert z.ampel == startklar.ROT
    assert any(b.punkt == "preis" for b in z.befunde)


def test_menge_null_ist_rot(db):
    l = _listing(db, _produkt(db), quantity_available=0)
    z = startklar.pruefe_listing(l, db.get(Product, l.product_id))
    assert z.ampel == startklar.ROT
    assert any(b.punkt == "menge" for b in z.befunde)


def test_schon_live_ist_rot(db):
    l = _listing(db, _produkt(db), ebay_item_id="v1|123|0")
    z = startklar.pruefe_listing(l, db.get(Product, l.product_id))
    assert z.ampel == startklar.ROT
    assert any(b.punkt == "schon_live" for b in z.befunde)


# --- gelb: geht durch, will aber angesehen werden ---------------------------

def test_fehlende_kosten_erfinden_keine_marge(db):
    """Eiserne Regel 3: fehlt der EK, bleibt die Marge leer statt 100 %."""
    l = _listing(db, _produkt(db), cost_eur=None)
    z = startklar.pruefe_listing(l, db.get(Product, l.product_id))
    assert z.ampel == startklar.GELB
    assert z.marge_pct is None
    assert z.gewinn_eur is None
    assert any(b.punkt == "kosten" for b in z.befunde)


def test_duenne_marge_ist_gelb(db):
    l = _listing(db, _produkt(db), price_eur=Decimal("12.00"),
                 cost_eur=Decimal("9.00"))
    z = startklar.pruefe_listing(l, db.get(Product, l.product_id))
    assert z.ampel == startklar.GELB
    assert any(b.punkt in ("marge", "gewinn") for b in z.befunde)


def test_fehlende_marke_ist_gelb(db):
    l = _listing(db, _produkt(db), item_specifics={"Material": "Baumwolle"})
    z = startklar.pruefe_listing(l, db.get(Product, l.product_id))
    assert z.ampel == startklar.GELB
    assert any(b.punkt == "marke" for b in z.befunde)


def test_aufdruck_als_marke_an_bekleidung_ist_gelb(db):
    """Regel vom 28.08.2026: die T-Shirts sind alle markenlos."""
    l = _listing(db, _produkt(db),
                 title_seo="T-Shirt Kurzarm Baumwolle Fluffy Cat Damen",
                 item_specifics={"Marke": "Fluffy Cat"})
    z = startklar.pruefe_listing(l, db.get(Product, l.product_id))
    assert z.ampel == startklar.GELB
    assert any(b.punkt == "marke" for b in z.befunde)


def test_fehlende_kategorie_ist_gelb_nicht_rot(db):
    """Die Kategorie wird beim Anlegen live erfragt - sie blockiert nicht."""
    l = _listing(db, _produkt(db), category_id=None, category_name=None)
    z = startklar.pruefe_listing(l, db.get(Product, l.product_id))
    assert z.ampel == startklar.GELB
    assert any(b.punkt == "kategorie" for b in z.befunde)


def test_zu_langer_titel_ist_gelb(db):
    l = _listing(db, _produkt(db), title_seo="T-Shirt " + "sehr lang " * 12)
    z = startklar.pruefe_listing(l, db.get(Product, l.product_id))
    assert z.ampel == startklar.GELB
    assert any(b.punkt == "titel" for b in z.befunde)


# --- Sammelbericht ----------------------------------------------------------

def test_bericht_zaehlt_die_ampeln(db):
    p_ok = _produkt(db)
    p_leer = _produkt(db, bilder=[])
    _listing(db, p_ok)                                   # gruen
    _listing(db, p_ok, item_specifics={"Material": "X"})  # gelb (Marke)
    _listing(db, p_leer)                                  # rot (Bilder)
    b = startklar.bericht(db)
    assert b["geprueft"] == 3
    assert b["startklar"] == 1
    assert b["mit_hinweis"] == 1
    assert b["blockiert"] == 1


def test_bericht_uebergeht_was_kein_entwurf_ist(db):
    p = _produkt(db)
    _listing(db, p)
    _listing(db, p, listing_status="active", ebay_item_id="v1|9|0")
    assert startklar.bericht(db)["geprueft"] == 1
    assert startklar.bericht(db, nur_entwuerfe=False)["geprueft"] == 2


def test_bericht_ruft_ebay_nicht_an(db, monkeypatch):
    """Strukturelle Sperre: die Pruefung darf nie ans Netz gehen.

    Sonst kostet ein blosser Blick auf die Liste Zeit und API-Kontingent - und
    bei ausgefallenem eBay waere die Pruefung gar nicht mehr benutzbar.
    """
    import app.integrations.ebay as ebay_modul

    def _verboten(*a, **k):
        raise AssertionError("Die Startklar-Pruefung hat eBay angerufen.")

    monkeypatch.setattr(ebay_modul.EbayClient, "__init__", _verboten)
    _listing(db, _produkt(db))
    assert startklar.bericht(db)["geprueft"] == 1


# --- Endpunkt ---------------------------------------------------------------

def test_endpunkt_liefert_den_bericht(client, db):
    _listing(db, _produkt(db))
    r = client.get("/api/v1/products/startklar")
    assert r.status_code == 200
    daten = r.json()
    assert daten["geprueft"] == 1
    assert daten["startklar"] == 1
    assert daten["listings"][0]["ampel"] == "gruen"


def test_endpunkt_kollidiert_nicht_mit_listing_id(client, db):
    """/startklar darf nicht als listing_id gelesen werden."""
    r = client.get("/api/v1/products/startklar")
    assert r.status_code == 200
