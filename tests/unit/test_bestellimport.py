"""eBay-Verkaeufe kommen in die eigene Bestellliste - nur lesend, ohne Doppelte."""
from __future__ import annotations

import pytest

from app.studio import bestellimport
from app.studio.models import PodListing, PodOrder, PodProduct


def _bestellung(nummer="27-1", status="NOT_STARTED", bezahlt="PAID", summe="14.90", sku="MW-12-tshirt-White-M",
                artikel="110000000001", storno="NONE_REQUESTED"):
    return {
        "orderId": nummer, "creationDate": "2026-09-20T09:15:00.000Z",
        "orderFulfillmentStatus": status, "orderPaymentStatus": bezahlt,
        "pricingSummary": {"total": {"value": summe, "currency": "EUR"}},
        "cancelStatus": {"cancelState": storno},
        "lineItems": [{"legacyItemId": artikel, "sku": sku, "title": "T-Shirt Erst Kaffee", "quantity": 1}],
    }


def test_neue_bestellung_wird_angelegt(db):
    r = bestellimport.uebernehme(db, [_bestellung()])
    assert r == {"gelesen": 1, "neu": 1, "aktualisiert": 0}
    o = db.query(PodOrder).one()
    assert (o.channel, o.external_id, o.status, o.sale_total_eur) == ("ebay", "27-1", "new", 14.90)
    assert o.ordered_at is not None and "T-Shirt Erst Kaffee" in o.note
    assert o.fulfillment_cost_eur is None                   # unbekannt bleibt leer


def test_zweiter_lauf_verdoppelt_nichts_und_frischt_den_status_auf(db):
    bestellimport.uebernehme(db, [_bestellung(status="NOT_STARTED")])
    r = bestellimport.uebernehme(db, [_bestellung(status="FULFILLED")])
    assert r["neu"] == 0 and r["aktualisiert"] == 1
    assert db.query(PodOrder).count() == 1
    assert db.query(PodOrder).one().status == "shipped"


def test_stornierte_und_unbezahlte_werden_richtig_markiert(db):
    bestellimport.uebernehme(db, [_bestellung("a", storno="CANCELED"), _bestellung("b", bezahlt="PENDING")])
    stati = {o.external_id: o.status for o in db.query(PodOrder).all()}
    assert stati == {"a": "cancelled", "b": "pending"}


def test_zuordnung_zum_eigenen_angebot_ueber_artikelnummer_und_sku(db):
    produkt = PodProduct(title="T-Shirt", design_id=12, produktart="tshirt", status="active")
    db.add(produkt)
    db.flush()
    angebot = PodListing(product_id=produkt.id, channel="ebay", external_id="110000000001", status="active")
    db.add(angebot)
    db.commit()
    bestellimport.uebernehme(db, [_bestellung("x", artikel="110000000001", sku="")])
    bestellimport.uebernehme(db, [_bestellung("y", artikel="", sku="MW-12-tshirt-Black-L")])
    zeilen = {o.external_id: o.listing_id for o in db.query(PodOrder).all()}
    assert zeilen == {"x": angebot.id, "y": angebot.id}


def test_kinder_shirt_sku_wird_erkannt():
    assert bestellimport._SKU.match("MW-7-kids_tshirt-Red-116").group(2) == "kids_tshirt"


class _Ebay:
    def __init__(self):
        self.aufrufe = 0

    async def list_all_orders(self, *, since, max_orders=500):
        self.aufrufe += 1
        return [_bestellung()]


@pytest.mark.asyncio
async def test_abgleich_liest_und_fragt_nicht_zu_oft_nach(db):
    bestellimport._letzter_lauf = 0.0
    ebay = _Ebay()
    erst = await bestellimport.gleiche_ab(db, ebay)
    assert erst["neu"] == 1 and ebay.aufrufe == 1
    zweit = await bestellimport.gleiche_ab(db, ebay)
    assert zweit.get("uebersprungen") is True and ebay.aufrufe == 1        # Drosselung
    dritt = await bestellimport.gleiche_ab(db, ebay, erzwingen=True)
    assert ebay.aufrufe == 2 and dritt["neu"] == 0
    bestellimport._letzter_lauf = 0.0


def test_umsatz_zaehlt_keine_stornos(db):
    from app import pod_router

    bestellimport.uebernehme(db, [_bestellung("a", summe="14.90"), _bestellung("b", summe="34.90", storno="CANCELED"),
                                  _bestellung("c", summe="11.90", bezahlt="PENDING")])
    k = pod_router.dashboard(db)["kpis"]
    assert k["revenue_eur"] == 14.90 and k["open_orders"] == 1


# --------------------------------------------------------------------------
# Produkt in der Bestellung und Download der Motivdatei
# --------------------------------------------------------------------------
def test_position_liest_produkt_farbe_groesse_und_motiv_aus_der_sku():
    p = bestellimport._position({"sku": "MW-12-tshirt-White-M", "title": "T-Shirt Kaffee", "quantity": 2})
    assert p["design_id"] == 12 and p["produktart"] == "tshirt"
    assert p["farbe"] == "Weiß" and p["farbcode"] == "White" and p["groesse"] == "M" and p["menge"] == 2
    k = bestellimport._position({"sku": "MW-7-kids_tshirt-BottleGreen-116", "title": "x"})
    assert k["produktart"] == "kids_tshirt" and k["farbe"] == "Flaschengrün" and k["groesse"] == "116"
    assert bestellimport._position({"sku": "", "title": "fremd"})["design_id"] is None


def _motiv(db, tmp_path, monkeypatch):
    from PIL import Image

    from app.config import get_settings
    from app.studio.models import StudioDesign

    monkeypatch.setattr(get_settings(), "studio_image_dir", str(tmp_path))
    Image.new("RGBA", (20, 10), (255, 255, 255, 255)).save(tmp_path / "kaffee.png")
    d = StudioDesign(title="Kaffee Charakter", status="ready", image_url="/studio/bilder/kaffee.png")
    db.add(d)
    db.commit()
    return d


def test_dashboard_nennt_produkt_und_gibt_downloadlink(db, tmp_path, monkeypatch):
    from app import pod_router

    d = _motiv(db, tmp_path, monkeypatch)
    bestellimport.uebernehme(db, [_bestellung(sku=f"MW-{d.id}-tshirt-Black-L")])
    o = pod_router.dashboard(db)["orders"][0]
    pos = o["positionen"][0]
    assert pos["produkt"] == "T-Shirt" and pos["farbe"] == "Schwarz" and pos["groesse"] == "L"
    assert pos["downloads"][0]["name"] == "Vorderseite"
    assert pos["downloads"][0]["url"] == f"/api/v1/pod/orders/{o['id']}/motiv?position=0&seite=vorne&ebene=0"


def test_download_liefert_die_motivdatei(db, tmp_path, monkeypatch):
    from app import pod_router

    d = _motiv(db, tmp_path, monkeypatch)
    bestellimport.uebernehme(db, [_bestellung(sku=f"MW-{d.id}-tshirt-Black-L")])
    o = db.query(PodOrder).one()
    antwort = pod_router.order_motiv(o.id, 0, "vorne", 0, db)
    assert str(antwort.path).endswith("kaffee.png")
    assert 'kaffee-charakter-vorne.png' in antwort.headers["content-disposition"]


def test_download_fuers_weisse_shirt_hat_schwarze_schrift(db, tmp_path, monkeypatch):
    from PIL import Image

    from app import pod_router

    d = _motiv(db, tmp_path, monkeypatch)
    bestellimport.uebernehme(db, [_bestellung(sku=f"MW-{d.id}-tshirt-White-M")])
    o = db.query(PodOrder).one()
    antwort = pod_router.order_motiv(o.id, 0, "vorne", 0, db)
    assert "schwarzschrift" in str(antwort.path)
    assert Image.open(antwort.path).convert("RGBA").getpixel((5, 5))[:3] == (17, 17, 17)
    assert "schwarze-schrift" in antwort.headers["content-disposition"]


def test_download_ohne_motiv_im_studio_ist_ein_klarer_404(db):
    from fastapi import HTTPException

    from app import pod_router

    bestellimport.uebernehme(db, [_bestellung(sku="fremd-123")])
    o = db.query(PodOrder).one()
    with pytest.raises(HTTPException) as fehler:
        pod_router.order_motiv(o.id, 0, "vorne", 0, db)
    assert fehler.value.status_code == 404
