"""Quellen-Slots (supplier_service): geteilte Produkte, Atomaritaet, Bestands-Signale.

Deckt die im adversarialen Review bestaetigten Defekte ab, damit sie nicht
zurueckkommen: geteilte Produkte werden nie mutiert, Primary-Entfernung ist
atomar (Scrape vor Commit), Transient-Fehler gelten nicht als ausverkauft,
Verdachts-Flag nur fuer Bildsuche-Matches.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.integrations.aliexpress import (OutOfStockError, ProductNotFoundError,
                                         ScrapedProduct)
from app.models import Listing, Product
from app.retry import PersistentError
from app.services import supplier_service


def _scraped(pid: str, price: str = "10.00", in_stock: bool = True,
             variants: dict | None = None) -> ScrapedProduct:
    return ScrapedProduct(
        aliexpress_id=pid, title_raw=f"Produkt {pid}", description_raw="",
        price_cny=Decimal(price), images=[f"https://img/{pid}.jpg"],
        variants=variants or {}, supplier_id="s1", supplier_rating=Decimal("4.8"),
        in_stock=in_stock,
    )


def _ae_stub(monkeypatch, mapping: dict):
    """_real_ae() ersetzen: scrape_product liefert je Produkt-ID aus `mapping`
    (ScrapedProduct ODER Exception)."""
    async def scrape(url):
        pid = url.rstrip(".html").rsplit("/", 1)[-1]
        val = mapping[pid]
        if isinstance(val, Exception):
            raise val
        return val
    stub = MagicMock()
    stub.scrape_product = AsyncMock(side_effect=scrape)
    monkeypatch.setattr(supplier_service, "_real_ae", lambda: stub)
    return stub


def _mk_listing(db, *, product: Product | None = None, price="29.95") -> Listing:
    listing = Listing(title_seo="Testartikel HTC NE70", description="x",
                      listing_status="active", price_eur=Decimal(price),
                      product_id=product.id if product else None)
    db.add(listing)
    db.commit()
    return listing


def _mk_product(db, pid="111111", price="6.00") -> Product:
    p = Product(aliexpress_url=f"https://de.aliexpress.com/item/{pid}.html",
                aliexpress_id=pid, title_raw=f"P{pid}", price_cny=Decimal(price))
    db.add(p)
    db.commit()
    return p


@pytest.mark.anyio
async def test_add_source_manual_setzt_slot_und_confirmed(db, monkeypatch):
    p = _mk_product(db, "111111")
    l = _mk_listing(db, product=p)
    _ae_stub(monkeypatch, {"222222": _scraped("222222", "8.00")})

    r = await supplier_service.add_source(
        db, listing_id=l.id, url="https://de.aliexpress.com/item/222222.html")

    assert [s["aliexpress_id"] for s in r["sources"]] == ["111111", "222222"]
    assert r["avg_ek_eur"] is not None
    alt = db.get(Product, p.id).alternatives
    assert alt["confirmed"] is True


@pytest.mark.anyio
async def test_primary_wechsel_auf_geteiltem_produkt_koppelt_nur_ein_listing_ab(db, monkeypatch):
    p = _mk_product(db, "111111")
    l1 = _mk_listing(db, product=p)
    l2 = _mk_listing(db, product=p)
    # L1 hat einen manuell gepflegten Zusatz-Slot auf dem geteilten Produkt
    _ae_stub(monkeypatch, {"333333": _scraped("333333", "7.00"),
                           "999999": _scraped("999999", "5.00")})
    await supplier_service.add_source(
        db, listing_id=l1.id, url="https://de.aliexpress.com/item/333333.html")

    await supplier_service.add_source(
        db, listing_id=l1.id, url="https://de.aliexpress.com/item/999999.html", primary=True)

    db.expire_all()
    l1, l2, p = db.get(Listing, l1.id), db.get(Listing, l2.id), db.get(Product, p.id)
    # L2 + geteiltes Produkt unangetastet
    assert l2.product_id == p.id
    assert p.aliexpress_id == "111111"
    assert [s["aliexpress_id"] for s in p.alternatives["sources"]] == ["111111", "333333"]
    # L1 haengt am neuen Produkt, Ausweich-Slots + confirmed wurden mitgenommen
    np = db.get(Product, l1.product_id)
    assert np.id != p.id and np.aliexpress_id == "999999"
    ids = [s["aliexpress_id"] for s in np.alternatives["sources"]]
    assert ids[0] == "999999" and "333333" in ids and "111111" in ids
    assert np.alternatives.get("confirmed") is True


@pytest.mark.anyio
async def test_remove_primary_promotes_source_owned_by_another_product_no_integrityerror(db, monkeypatch):
    """Fund 20.07.: Hauptquelle entfernen befoerdert den naechsten Slot – dessen aliexpress_id
    gehoert aber SCHON einem ANDEREN Produkt (UNIQUE). Statt IntegrityError (ID ueberschreiben)
    wird das Listing sauber an das VORHANDENE Produkt umgehaengt."""
    pA = _mk_product(db, "111111")
    pB = _mk_product(db, "222222", "8.00")     # anderes Produkt besitzt 222222 bereits
    l = _mk_listing(db, product=pA)
    _ae_stub(monkeypatch, {"222222": _scraped("222222", "8.00")})
    await supplier_service.add_source(          # 222222 als Zusatz-Slot (primary=False)
        db, listing_id=l.id, url="https://de.aliexpress.com/item/222222.html")

    r = await supplier_service.remove_source(db, listing_id=l.id, aliexpress_id="111111")

    db.expire_all()
    l = db.get(Listing, l.id)
    assert l.product_id == pB.id                # an das VORHANDENE Produkt umgehaengt
    assert db.get(Product, pB.id).aliexpress_id == "222222"   # ID unveraendert (kein Overwrite)
    assert [s["aliexpress_id"] for s in r["sources"]][0] == "222222"


@pytest.mark.anyio
async def test_remove_primary_befoerdert_naechsten_slot_und_entfernt_alten(db, monkeypatch):
    p = _mk_product(db, "111111")
    l = _mk_listing(db, product=p)
    _ae_stub(monkeypatch, {"222222": _scraped("222222", "8.00")})
    await supplier_service.add_source(
        db, listing_id=l.id, url="https://de.aliexpress.com/item/222222.html")

    r = await supplier_service.remove_source(db, listing_id=l.id, aliexpress_id="111111")

    db.expire_all()
    p = db.get(Product, db.get(Listing, l.id).product_id)
    ids = [s["aliexpress_id"] for s in p.alternatives["sources"]]
    assert ids == ["222222"]                      # alter Primary wirklich raus
    assert p.aliexpress_id == "222222"            # Produktfelder zeigen auf neue Quelle
    assert [s["aliexpress_id"] for s in r["sources"]] == ["222222"]


@pytest.mark.anyio
async def test_remove_primary_scrape_fehler_laesst_alles_unveraendert(db, monkeypatch):
    p = _mk_product(db, "111111")
    l = _mk_listing(db, product=p)
    _ae_stub(monkeypatch, {"222222": _scraped("222222", "8.00")})
    await supplier_service.add_source(
        db, listing_id=l.id, url="https://de.aliexpress.com/item/222222.html")
    # Zweitquelle stirbt: Nach-Scrape beim Befoerdern schlaegt fehl
    _ae_stub(monkeypatch, {"222222": ProductNotFoundError("weg")})

    with pytest.raises(Exception):
        await supplier_service.remove_source(db, listing_id=l.id, aliexpress_id="111111")

    db.expire_all()
    p = db.get(Product, p.id)
    ids = [s["aliexpress_id"] for s in p.alternatives["sources"]]
    assert ids == ["111111", "222222"]            # kein halber Zustand
    assert p.aliexpress_id == "111111"


@pytest.mark.anyio
async def test_remove_letzte_quelle_trennt_listing_geteiltes_produkt_bleibt(db, monkeypatch):
    p = _mk_product(db, "111111")
    l1 = _mk_listing(db, product=p)
    l2 = _mk_listing(db, product=p)

    r = await supplier_service.remove_source(db, listing_id=l1.id, aliexpress_id="111111")

    db.expire_all()
    assert r["unlinked"] is True
    assert db.get(Listing, l1.id).product_id is None
    p = db.get(Product, p.id)
    assert db.get(Listing, l2.id).product_id == p.id
    assert [s["aliexpress_id"] for s in p.alternatives["sources"]] == ["111111"]


@pytest.mark.anyio
async def test_refresh_transient_fehler_ist_nicht_ausverkauft(db, monkeypatch):
    p = _mk_product(db, "111111")
    l = _mk_listing(db, product=p)
    supplier_service.ensure_primary_slot(p)
    db.commit()
    _ae_stub(monkeypatch, {"111111": RuntimeError("429 rate limit")})
    monkeypatch.setattr(supplier_service, "_RATE_S", 0)

    await supplier_service.refresh_sources(db, listing_id=l.id)

    db.expire_all()
    p = db.get(Product, p.id)
    assert p.alternatives["sources"][0]["in_stock"] is True     # unveraendert
    assert db.get(Listing, l.id).supplier_in_stock is True


@pytest.mark.anyio
async def test_refresh_oos_fehler_markiert_ausverkauft(db, monkeypatch):
    p = _mk_product(db, "111111")
    l = _mk_listing(db, product=p)
    supplier_service.ensure_primary_slot(p)
    db.commit()
    _ae_stub(monkeypatch, {"111111": OutOfStockError("leer")})
    monkeypatch.setattr(supplier_service, "_RATE_S", 0)

    await supplier_service.refresh_sources(db, listing_id=l.id)

    db.expire_all()
    assert db.get(Product, p.id).alternatives["sources"][0]["in_stock"] is False
    assert db.get(Listing, l.id).supplier_in_stock is False


def test_report_verdacht_nur_bei_bildsuche_matches(db, monkeypatch, tmp_path):
    from app.services import listing_match_service
    # NICHT die echte Report-Datei ueberschreiben
    monkeypatch.setattr(listing_match_service, "REPORT_FILE",
                        str(tmp_path / "reprice_report.json"))
    # Bild-Match mit absurder Differenz -> Verdacht, kein action_required
    p1 = _mk_product(db, "111111", price="50.00")
    p1.alternatives = {"matched": "image-auto"}
    l1 = _mk_listing(db, product=p1, price="9.95")
    # Eigener Upload (kein matched-Flag) mit gleicher Differenz -> KEIN Verdacht,
    # dafuer action_required (Verlustverkauf)
    p2 = _mk_product(db, "222222", price="50.00")
    l2 = _mk_listing(db, product=p2, price="9.95")
    db.commit()

    listing_match_service.rebuild_reprice_report(db)
    rows = {r["listing_id"]: r for r in listing_match_service.reprice_report(db)["rows"]}

    assert rows[l1.id]["match_verdacht"] is True
    assert rows[l1.id]["action_required"] is False
    assert rows[l2.id]["match_verdacht"] is False
    assert rows[l2.id]["action_required"] is True


@pytest.mark.anyio
async def test_failover_schaltet_auf_lieferbare_ausweichquelle(db, monkeypatch):
    p = _mk_product(db, "111111", price="6.00")
    l = _mk_listing(db, product=p)
    _ae_stub(monkeypatch, {"222222": _scraped("222222", "8.00")})
    await supplier_service.add_source(
        db, listing_id=l.id, url="https://de.aliexpress.com/item/222222.html")
    # Hauptquelle stirbt, Slot 2 ist lieferbar
    _ae_stub(monkeypatch, {"111111": ProductNotFoundError("weg"),
                           "222222": _scraped("222222", "8.00")})

    switched, scraped = await supplier_service.failover_primary(
        db, listing=db.get(Listing, l.id), product=db.get(Product, p.id))

    db.expire_all()
    p = db.get(Product, p.id)
    assert switched is True and scraped.aliexpress_id == "222222"
    assert p.aliexpress_id == "222222"                       # Produktfelder umgestellt
    ids = [(s["aliexpress_id"], s["in_stock"]) for s in p.alternatives["sources"]]
    assert ids[0] == ("222222", True)
    assert ("111111", False) in ids                          # toter Primary bleibt markiert
    l2 = db.get(Listing, l.id)
    # Restock-Zweig im Monitoring braucht das False-Flag (setzt Menge ATOMAR mit Push)
    # -> failover selbst darf supplier_in_stock nicht anfassen (hier war es True).
    # Preis-Pushes stoppen bis zum Review (Varianten-SKUs passen evtl. nicht mehr).
    assert l2.auto_reprice is False


@pytest.mark.anyio
async def test_failover_ueberspringt_geteilte_produkte(db, monkeypatch):
    p = _mk_product(db, "111111")
    l1 = _mk_listing(db, product=p)
    _mk_listing(db, product=p)   # zweites Listing teilt das Produkt
    _ae_stub(monkeypatch, {"222222": _scraped("222222", "8.00")})
    await supplier_service.add_source(
        db, listing_id=l1.id, url="https://de.aliexpress.com/item/222222.html")

    switched, scraped = await supplier_service.failover_primary(
        db, listing=db.get(Listing, l1.id), product=db.get(Product, p.id))

    assert switched is False and scraped is None
    assert db.get(Product, p.id).aliexpress_id == "111111"   # geteiltes Produkt unangetastet


@pytest.mark.anyio
async def test_failover_ohne_lieferbare_alternative_tut_nichts(db, monkeypatch):
    p = _mk_product(db, "111111")
    l = _mk_listing(db, product=p)
    supplier_service.ensure_primary_slot(p)
    db.commit()

    switched, scraped = await supplier_service.failover_primary(
        db, listing=l, product=p)

    assert switched is False and scraped is None
    assert db.get(Product, p.id).aliexpress_id == "111111"


def test_confirm_match_setzt_flag(db):
    p = _mk_product(db, "111111")
    p.alternatives = {"matched": "image-auto"}
    l = _mk_listing(db, product=p)
    db.commit()

    r = supplier_service.confirm_match(db, listing_id=l.id)

    db.expire_all()
    assert r["confirmed"] is True
    alt = db.get(Product, p.id).alternatives
    assert alt["confirmed"] is True and alt.get("verified_at")


def test_shipping_fields_zuschlag_erkennung(db):
    from app.config import get_settings
    from app.services.listing_match_service import _shipping_fields
    s = get_settings()
    frei = _mk_listing(db)
    frei.shipping_policy_id = "291968613015"
    frei.shipping_policy_name = "Kostenloser Versand 7 Tage Bearbeitung"
    zuschlag = _mk_listing(db)
    zuschlag.shipping_policy_id = "282125020015"
    zuschlag.shipping_policy_name = "Versand 7 Tage bearbeitung"
    unbekannt = _mk_listing(db)

    assert _shipping_fields(frei, s)["shipping_surcharge"] is False
    assert _shipping_fields(zuschlag, s)["shipping_surcharge"] is True
    assert _shipping_fields(unbekannt, s)["shipping_surcharge"] is False


@pytest.mark.anyio
async def test_preis_update_fehler_laesst_db_unveraendert(db, monkeypatch):
    """KRITISCH: schlaegt der eBay-Push fehl, darf die DB NICHT den neuen Preis zeigen."""
    from app.services import golive_service
    l = _mk_listing(db, price="20.00")
    l.ebay_item_id = "389000000001"
    db.commit()
    ebay = MagicMock()
    ebay._first_offer_for_sku = AsyncMock(return_value=None)          # importiert: kein Offer
    ebay.get_item_price_info = AsyncMock(return_value={               # EINZEL-Listing (eine Variation)
        "item_id": l.ebay_item_id, "current_price": 20.0, "variations": []})
    ebay.revise_item_price_smart = AsyncMock(side_effect=PersistentError("21916736"))
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: ebay)

    with pytest.raises(Exception):
        await golive_service.update_listing_live(db, listing_id=l.id, price_eur=42.95)

    db.expire_all()
    assert float(db.get(Listing, l.id).price_eur) == 20.00   # unveraendert!


@pytest.mark.anyio
async def test_preis_update_ohne_readback_verifikation_gilt_als_fehler(db, monkeypatch):
    """Push meldet Erfolg, aber eBay zeigt weiter den alten Preis -> Fehler + DB alt."""
    from app.services import golive_service
    l = _mk_listing(db, price="20.00")
    l.ebay_item_id = "389000000002"
    db.commit()
    ebay = MagicMock()
    ebay._first_offer_for_sku = AsyncMock(return_value=None)
    ebay.get_item_price_info = AsyncMock(return_value={               # EINZEL-Listing (eine Variation)
        "item_id": l.ebay_item_id, "current_price": 20.0, "variations": []})
    ebay.revise_item_price_smart = AsyncMock(return_value={"variations": 0})
    ebay.verify_item_price = AsyncMock(return_value=False)            # Read-back schlaegt fehl
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: ebay)

    with pytest.raises(Exception):
        await golive_service.update_listing_live(db, listing_id=l.id, price_eur=42.95)

    db.expire_all()
    assert float(db.get(Listing, l.id).price_eur) == 20.00


@pytest.mark.anyio
async def test_preis_update_erfolg_erst_nach_verifikation(db, monkeypatch):
    from app.services import golive_service
    l = _mk_listing(db, price="20.00")
    l.ebay_item_id = "389000000003"
    db.commit()
    ebay = MagicMock()
    ebay._first_offer_for_sku = AsyncMock(return_value=None)
    ebay.get_item_price_info = AsyncMock(return_value={               # EINZEL-Listing (eine Variation)
        "item_id": l.ebay_item_id, "current_price": 20.0, "variations": []})
    ebay.revise_item_price_smart = AsyncMock(return_value={"variations": 4})
    ebay.verify_item_price = AsyncMock(return_value=True)
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: ebay)

    r = await golive_service.update_listing_live(db, listing_id=l.id, price_eur=42.95)

    db.expire_all()
    assert r["pushed_to_ebay"] is True and r["price_verified"] is True
    assert float(db.get(Listing, l.id).price_eur) == 42.95
    ebay.verify_item_price.assert_awaited_once()


# --------- Multivarianten-Schutz beim Listing-EINZELPREIS (Fund 18.07., /{id}/edit) ---------
# update_listing_live setzte einen Listing-Einzelpreis (z.B. „🔻 Senken“) auf ALLE Variationen
# flach – dieselbe Klasse Bug wie im Reprice-Dialog (376a167), nur ueber die andere Preis-Route.


@pytest.mark.anyio
async def test_edit_einzelpreis_auf_native_gruppe_verweigert(db, monkeypatch):
    """FIX: Ein Listing-Einzelpreis darf ein NATIVES Multivarianten-Listing (Inventar-Gruppe,
    ``-GRP``) NICHT flach setzen. Es wird verweigert (Verweis auf „Preise anpassen“), NICHTS
    auf eBay gepusht, die DB bleibt unveraendert."""
    from app.services import golive_service
    l = _mk_listing(db, price="20.00")
    l.ebay_item_id = "389000000010"
    l.ebay_draft_id = "GRP-XYZ-GRP"
    db.commit()
    ebay = MagicMock()
    ebay.get_inventory_item_group = AsyncMock(return_value={"variantSKUs": ["A", "B", "C"]})
    ebay._first_offer_for_sku = AsyncMock(return_value={"offerId": "O1"})
    ebay.bulk_update_price = AsyncMock(side_effect=AssertionError("NIE flach pushen!"))
    ebay.revise_item_price_smart = AsyncMock(side_effect=AssertionError("NIE flach pushen!"))
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: ebay)

    with pytest.raises(Exception) as ei:
        await golive_service.update_listing_live(db, listing_id=l.id, price_eur=42.95)
    assert "Varianten" in str(ei.value) or "Preise anpassen" in str(ei.value)
    ebay.bulk_update_price.assert_not_awaited()
    ebay.revise_item_price_smart.assert_not_awaited()
    db.expire_all()
    assert float(db.get(Listing, l.id).price_eur) == 20.00


@pytest.mark.anyio
async def test_edit_einzelpreis_auf_klassische_multivariante_verweigert(db, monkeypatch):
    """FIX (Kern des Fundes): ein klassisch/importiertes Multivarianten-Listing (kein Inventory-
    Offer, aber GetItem zeigt >= 2 Variationen) wird beim Listing-Einzelpreis NICHT via
    revise_item_price_smart flach gesetzt, sondern verweigert. DB unveraendert."""
    from app.services import golive_service
    l = _mk_listing(db, price="17.95")
    l.ebay_item_id = "389000000011"   # kein -GRP -> klassisch/importiert
    db.commit()
    ebay = MagicMock()
    ebay._first_offer_for_sku = AsyncMock(return_value=None)   # kein Inventory-Offer
    ebay.get_item_price_info = AsyncMock(return_value={"item_id": l.ebay_item_id,
        "current_price": 17.95, "variations": [
            {"sku": "A", "price": 17.95, "quantity": "5", "specifics": [("Farbe", "Rot")]},
            {"sku": "B", "price": 21.95, "quantity": "3", "specifics": [("Farbe", "Blau")]}]})
    ebay.revise_item_price_smart = AsyncMock(side_effect=AssertionError("NIE flach setzen!"))
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: ebay)

    with pytest.raises(Exception):
        await golive_service.update_listing_live(db, listing_id=l.id, price_eur=42.95)
    ebay.revise_item_price_smart.assert_not_awaited()
    db.expire_all()
    assert float(db.get(Listing, l.id).price_eur) == 17.95


@pytest.mark.anyio
async def test_edit_einzelpreis_fallback_produktvarianten_wenn_getitem_ausfaellt(db, monkeypatch):
    """FALLBACK-SCHUTZ: faellt GetItem aus, aber das PRODUKT kennt >= 2 echte Varianten, wird der
    Listing-Einzelpreis sicherheitshalber verweigert (nicht blind flach gesetzt)."""
    from app.models import Product
    from app.services import golive_service
    p = Product(aliexpress_url="https://ae/mv", aliexpress_id="mv", price_cny=Decimal("6"),
                variants={"axes": {"Farbe": ["Rot", "Blau"]},
                          "skus": [{"attr": "14:1", "options": {"Farbe": "Rot"}, "id": 1, "price": 6, "stock": 9},
                                   {"attr": "14:2", "options": {"Farbe": "Blau"}, "id": 2, "price": 6, "stock": 9}]})
    db.add(p); db.flush()
    l = _mk_listing(db, product=p, price="20.00")
    l.ebay_item_id = "389000000012"
    db.commit()
    ebay = MagicMock()
    ebay._first_offer_for_sku = AsyncMock(return_value=None)
    ebay.get_item_price_info = AsyncMock(side_effect=RuntimeError("GetItem 500"))
    ebay.revise_item_price_smart = AsyncMock(side_effect=AssertionError("NIE flach setzen!"))
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: ebay)

    with pytest.raises(Exception):
        await golive_service.update_listing_live(db, listing_id=l.id, price_eur=42.95)
    ebay.revise_item_price_smart.assert_not_awaited()
    db.expire_all()
    assert float(db.get(Listing, l.id).price_eur) == 20.00


@pytest.mark.anyio
async def test_edit_einzelpreis_unklar_wird_failclosed_verweigert(db, monkeypatch):
    """FAIL-CLOSED: ist das Listing live, aber GetItem NICHT erreichbar UND kein Produkt-
    Variantenhinweis vorhanden, wird NICHT geraten – der Push wird verweigert (kein Blind-
    Flatten), die DB bleibt unveraendert."""
    from app.services import golive_service
    l = _mk_listing(db, price="20.00")   # kein Produkt
    l.ebay_item_id = "389000000013"
    db.commit()
    ebay = MagicMock()
    ebay._first_offer_for_sku = AsyncMock(return_value=None)
    ebay.get_item_price_info = AsyncMock(side_effect=RuntimeError("GetItem 500"))
    ebay.revise_item_price_smart = AsyncMock(side_effect=AssertionError("NIE flach setzen!"))
    ebay.bulk_update_price = AsyncMock(side_effect=AssertionError("NIE flach setzen!"))
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: ebay)

    with pytest.raises(Exception):
        await golive_service.update_listing_live(db, listing_id=l.id, price_eur=42.95)
    ebay.revise_item_price_smart.assert_not_awaited()
    ebay.bulk_update_price.assert_not_awaited()
    db.expire_all()
    assert float(db.get(Listing, l.id).price_eur) == 20.00


@pytest.mark.anyio
async def test_edit_einzelpreis_echte_einzelvariante_mit_offer_geht_durch(db, monkeypatch):
    """NICHT-REGRESSION: ein echtes EINZEL-Listing (eine Variation, mit Inventory-Offer) darf
    seinen Preis weiterhin per bulk_update_price setzen – der Multivarianten-Schutz greift NUR
    bei >= 2 Variationen."""
    from app.services import golive_service
    l = _mk_listing(db, price="20.00")
    l.ebay_item_id = "389000000020"
    db.commit()
    ebay = MagicMock()
    ebay._first_offer_for_sku = AsyncMock(return_value={"offerId": "OFF-1"})
    ebay.bulk_update_price = AsyncMock(return_value=None)
    ebay.get_item_price_info = AsyncMock(return_value={"item_id": l.ebay_item_id,
        "current_price": 20.0, "variations": []})   # eine Variation -> kein Multi
    ebay.verify_item_price = AsyncMock(return_value=True)
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: ebay)

    r = await golive_service.update_listing_live(db, listing_id=l.id, price_eur=24.95)
    ebay.bulk_update_price.assert_awaited_once()
    db.expire_all()
    assert r["price_verified"] is True
    assert float(db.get(Listing, l.id).price_eur) == 24.95


@pytest.mark.anyio
async def test_edit_einzelpreis_produkt_multi_sperrt_auch_bei_getitem_unterreport(db, monkeypatch):
    """HAERTUNG (Review-Fund): meldet GetItem fuer ein Listing, dessen PRODUKT >= 2 echte Varianten
    kennt, faelschlich < 2 Variationen, wird der Einzelpreis TROTZDEM verweigert. Sonst koennte
    revise_item_price_smart mit seinem eigenen GetItem doch noch die volle Staffel flach setzen."""
    from app.models import Product
    from app.services import golive_service
    p = Product(aliexpress_url="https://ae/ur", aliexpress_id="ur", price_cny=Decimal("6"),
                variants={"axes": {"Farbe": ["Rot", "Blau"]},
                          "skus": [{"attr": "14:1", "options": {"Farbe": "Rot"}, "id": 1, "price": 6, "stock": 9},
                                   {"attr": "14:2", "options": {"Farbe": "Blau"}, "id": 2, "price": 6, "stock": 9}]})
    db.add(p); db.flush()
    l = _mk_listing(db, product=p, price="20.00")
    l.ebay_item_id = "389000000030"
    db.commit()
    ebay = MagicMock()
    ebay._first_offer_for_sku = AsyncMock(return_value=None)
    ebay.get_item_price_info = AsyncMock(return_value={"item_id": l.ebay_item_id,
        "current_price": 20.0, "variations": [   # eBay meldet nur EINE Variation (Under-Report)
            {"sku": "ONLY", "price": 20.0, "quantity": "5", "specifics": [("Farbe", "Rot")]}]})
    ebay.revise_item_price_smart = AsyncMock(side_effect=AssertionError("NIE flach setzen!"))
    monkeypatch.setattr(golive_service, "_real_ebay", lambda: ebay)

    with pytest.raises(Exception):
        await golive_service.update_listing_live(db, listing_id=l.id, price_eur=42.95)
    ebay.revise_item_price_smart.assert_not_awaited()
    db.expire_all()
    assert float(db.get(Listing, l.id).price_eur) == 20.00


def test_already_ended_erkennung_streng():
    from app.services.golive_service import _already_ended_error
    assert _already_ended_error("Error 1047: The auction has already been closed.")
    assert _already_ended_error("Der Artikel wurde bereits beendet.")
    # Lose Woerter duerfen NICHT matchen (echte Fehler nicht verschlucken)
    assert not _already_ended_error("This listing cannot be ended right now.")
    assert not _already_ended_error("Item 800271104711 kann nicht beendet werden: aktiv.")


def test_find_source_candidates_ranks_and_excludes_own(db, monkeypatch):
    """KI-Quellenfinder: Bildsuche + Ranking, eigene Quelle raus, nach Konfidenz sortiert."""
    import asyncio as _asyncio
    from app.models import Listing, Product
    from app.services import supplier_service as ss

    p = Product(aliexpress_url="https://de.aliexpress.com/item/own.html", aliexpress_id="OWN1")
    db.add(p); db.flush()
    listing = Listing(product_id=p.id, ebay_sku="AE-S1", title_seo="Motocross Brille MX",
                      description="d", listing_status="active", image_url="http://img/x.jpg")
    db.add(listing); db.commit()

    class _AE:
        def _http(self):
            class _C:
                async def get(self, *a, **k):
                    class _R: content = b"imgbytes"
                    return _R()
            return _C()
        async def image_search(self, img, *, page_size=8):
            return [
                {"aliexpress_id": "OWN1", "url": "u0", "title": "eigene", "image": "i0", "price_eur": 5.0},
                {"aliexpress_id": "C1", "url": "u1", "title": "MX Brille schwarz", "image": "i1", "price_eur": 6.0},
                {"aliexpress_id": "C2", "url": "u2", "title": "irgendwas anderes", "image": "i2", "price_eur": 7.0},
            ]

    class _LLM:
        async def rank_source_candidates(self, *, ebay_title, candidates):
            score = {"C1": 0.9, "C2": 0.2}
            return [{"aliexpress_id": c["aliexpress_id"],
                     "confidence": score.get(c["aliexpress_id"], 0.0), "reason": "x"}
                    for c in candidates]

    monkeypatch.setattr(ss, "_real_ae", lambda: _AE())
    monkeypatch.setattr("app.integrations.get_llm_client", lambda: _LLM())
    r = _asyncio.run(ss.find_source_candidates(db, listing_id=listing.id))
    ids = [c["aliexpress_id"] for c in r["candidates"]]
    assert "OWN1" not in ids                 # eigene Quelle ausgeschlossen
    assert ids == ["C1", "C2"]               # nach KI-Konfidenz sortiert
    assert r["candidates"][0]["ki_confidence"] == 0.9
    assert r["candidates"][0]["ek_eur"] is not None and r["candidates"][0]["vk_eur"] is not None


def test_find_source_candidates_no_image(db):
    from app.models import Listing
    from app.services import supplier_service as ss
    listing = Listing(ebay_sku="AE-NOIMG", title_seo="X", description="d",
                      listing_status="active", image_url=None)
    db.add(listing); db.commit()
    import asyncio as _asyncio
    r = _asyncio.run(ss.find_source_candidates(db, listing_id=listing.id))
    assert r["candidates"] == [] and "Bild" in r["note"]


def test_remove_source_by_index_removes_exactly_one_even_with_dup_ids(db):
    """Bugfix: identische/leere aliexpress_id -> nur der geklickte Slot (Index) geht weg,
    NICHT alle mit derselben ID."""
    import asyncio
    p = Product(aliexpress_url="https://de.aliexpress.com/item/rmv.html", aliexpress_id="P-RMV",
                alternatives={"sources": [
                    {"aliexpress_id": "", "url": "u1", "title": "Slot A", "price_eur": 5.0},
                    {"aliexpress_id": "", "url": "u2", "title": "Slot B", "price_eur": 6.0},
                    {"aliexpress_id": "", "url": "u3", "title": "Slot C", "price_eur": 7.0}]})
    db.add(p); db.flush()
    listing = Listing(product_id=p.id, ebay_sku="AE-RMV", title_seo="T", description="d",
                      listing_status="active")
    db.add(listing); db.commit()

    # Slot in der Mitte (Index 1) entfernen -> A und C bleiben, nur B weg
    r = asyncio.run(supplier_service.remove_source(db, listing_id=listing.id, slot_index=1))
    urls = [s["url"] for s in r["sources"]]
    assert urls == ["u1", "u3"]          # GENAU ein Slot entfernt (früher: alle 3 weg)
    db.refresh(p)
    assert len(p.alternatives["sources"]) == 2


def test_remove_alternative_item_by_index(db):
    p = Product(aliexpress_url="https://de.aliexpress.com/item/alt.html", aliexpress_id="P-ALT",
                alternatives={"items": [
                    {"aliexpress_id": "", "url": "a1", "title": "Vorschlag 1"},
                    {"aliexpress_id": "", "url": "a2", "title": "Vorschlag 2"}]})
    db.add(p); db.flush()
    listing = Listing(product_id=p.id, ebay_sku="AE-ALT", title_seo="T", description="d",
                      listing_status="active")
    db.add(listing); db.commit()

    r = supplier_service.remove_alternative_item(db, listing_id=listing.id, item_index=0)
    assert [i["url"] for i in r["items"]] == ["a2"]   # nur Vorschlag 1 weg
