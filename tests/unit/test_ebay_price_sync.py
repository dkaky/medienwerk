"""Live-eBay-Preis-Abgleich + Drift-Sichtbarkeit (Fund 17.07.).

Problem: das Cockpit rechnete Marge/Gewinn immer gegen den INTERN gespeicherten Preis.
Wich der echte eBay-Listing-Preis ab (z.B. nie/fehlgeschlagen gepusht: intern 29,95 €,
eBay 17,95 €), zeigte das Cockpit eine optimistische Scheinmarge, waehrend real Verlust lief.
Fix: echten eBay-Preis je Variante abgleichen (`sync_ebay_live_prices`) und die Marge im
Report gegen DIESEN Preis rechnen + Drift markieren."""
from __future__ import annotations

import asyncio
from decimal import Decimal

from app.models import Listing, Product
from app.services import ebay_import_service
from app.services.listing_match_service import _variant_rows


class _FakeEbayPrices:
    """GetItem-Ersatz: liefert je ItemID einen current_price + Variationen (SKU->StartPrice)."""

    def __init__(self, by_item):
        self._by_item = by_item
        self.calls = []

    async def get_item_price_info(self, item_id):
        self.calls.append(item_id)
        info = self._by_item.get(str(item_id))
        if isinstance(info, Exception):
            raise info
        return info


def _active_listing(db, *, item_id, sku, price):
    p = Product(aliexpress_url=f"https://ae/{item_id}", aliexpress_id=f"ae{item_id}")
    db.add(p)
    db.flush()
    l = Listing(product_id=p.id, ebay_sku=sku, ebay_item_id=item_id, title_seo="T",
                description="d", listing_status="active", price_eur=Decimal(str(price)),
                cost_eur=Decimal("7.00"))
    db.add(l)
    db.commit()
    return l


# ---------------------------------------------------------------- sync_ebay_live_prices


def test_sync_maps_variations_to_sku_prices(db):
    l = _active_listing(db, item_id="V1", sku="AE-V1", price=24.95)
    ebay = _FakeEbayPrices({"V1": {"item_id": "V1", "current_price": 24.95, "variations": [
        {"sku": "AE-V1-V1", "price": 17.95}, {"sku": "AE-V1-V2", "price": 19.95}]}})
    res = asyncio.run(ebay_import_service.sync_ebay_live_prices(db, ebay=ebay))
    db.refresh(l)
    assert l.ebay_live_prices == {"AE-V1-V1": 17.95, "AE-V1-V2": 19.95}
    assert l.ebay_price_synced_at is not None
    assert res["checked"] == 1 and res["updated"] == 1


def test_sync_stores_value_signature_keys(db):
    """Fund 17.07. (Beige Sonnenschutz-Netz): neben der eBay-SKU wird je Variation auch eine
    Merkmals-WERT-Signatur ("sig:…", klein/ohne Leerzeichen/sortiert) gespeichert, damit die
    Anzeige die richtige Variante auch dann findet, wenn die eBay-SKUs NICHT den Positions-SKUs
    entsprechen (importiertes Listing)."""
    l = _active_listing(db, item_id="SIG", sku="AE-SIG", price=24.95)
    ebay = _FakeEbayPrices({"SIG": {"item_id": "SIG", "current_price": 24.95, "variations": [
        {"sku": "EBAY-XYZ-1", "price": 22.95, "specifics": [("Größe", "3x4 m")]},
        {"sku": "EBAY-XYZ-2", "price": 76.95, "specifics": [("Größe", "6x8 m")]}]}})
    asyncio.run(ebay_import_service.sync_ebay_live_prices(db, ebay=ebay))
    db.refresh(l)
    assert l.ebay_live_prices["EBAY-XYZ-1"] == 22.95       # eBay-SKU-Key (falls je passend)
    assert l.ebay_live_prices["EBAY-XYZ-2"] == 76.95
    assert l.ebay_live_prices["sig:3x4m"] == 22.95         # Wert-Signatur je Variante
    assert l.ebay_live_prices["sig:6x8m"] == 76.95


def test_sync_signature_collision_keeps_minimum_price(db):
    """GELD-SCHUTZ: teilen sich zwei Variationen DIESELBE Merkmals-Wert-Signatur (z.B. permutierte
    2-Achsen), behaelt der sig:-Key den NIEDRIGSTEN Preis – eine mehrdeutige Signatur darf nie den
    hoeheren (Verlust versteckenden) Preis anzeigen. Die exakten eBay-SKU-Keys bleiben unberuehrt."""
    l = _active_listing(db, item_id="COL", sku="AE-COL", price=24.95)
    ebay = _FakeEbayPrices({"COL": {"item_id": "COL", "current_price": 24.95, "variations": [
        {"sku": "K-1", "price": 30.0, "specifics": [("Vorne", "Blau"), ("Hinten", "Rot")]},
        {"sku": "K-2", "price": 20.0, "specifics": [("Vorne", "Rot"), ("Hinten", "Blau")]}]}})
    asyncio.run(ebay_import_service.sync_ebay_live_prices(db, ebay=ebay))
    db.refresh(l)
    assert l.ebay_live_prices["sig:blau|rot"] == 20.0     # konservativ der niedrigere
    assert l.ebay_live_prices["K-1"] == 30.0              # exakte SKU-Keys unveraendert
    assert l.ebay_live_prices["K-2"] == 20.0


def test_sync_single_listing_uses_current_price_on_base_sku(db):
    """Einzel-Listing ohne Variationen -> Basis-SKU = item current_price."""
    l = _active_listing(db, item_id="S1", sku="AE-S1", price=29.95)
    ebay = _FakeEbayPrices({"S1": {"item_id": "S1", "current_price": 17.95, "variations": []}})
    asyncio.run(ebay_import_service.sync_ebay_live_prices(db, ebay=ebay))
    db.refresh(l)
    assert l.ebay_live_prices == {"AE-S1": 17.95}


def test_sync_counts_drift_when_internal_price_diverges(db):
    """Intern 29,95 € vs. echter eBay-Preis 17,95 € -> als Drift gezaehlt (Kern des Funds)."""
    _active_listing(db, item_id="D1", sku="AE-D1", price=29.95)
    ebay = _FakeEbayPrices({"D1": {"item_id": "D1", "current_price": 17.95, "variations": []}})
    res = asyncio.run(ebay_import_service.sync_ebay_live_prices(db, ebay=ebay))
    assert res["drift"] == 1


def test_sync_no_drift_when_prices_match(db):
    _active_listing(db, item_id="M1", sku="AE-M1", price=17.95)
    ebay = _FakeEbayPrices({"M1": {"item_id": "M1", "current_price": 17.95, "variations": []}})
    res = asyncio.run(ebay_import_service.sync_ebay_live_prices(db, ebay=ebay))
    assert res["drift"] == 0


def test_sync_best_effort_one_listing_error_does_not_stop_rest(db):
    """Ein eBay-Fehler bei EINEM Listing darf den Abgleich der uebrigen nicht kippen."""
    _active_listing(db, item_id="E1", sku="AE-E1", price=24.95)
    ok = _active_listing(db, item_id="E2", sku="AE-E2", price=24.95)
    ebay = _FakeEbayPrices({
        "E1": RuntimeError("GetItem 500"),
        "E2": {"item_id": "E2", "current_price": 21.95, "variations": []}})
    res = asyncio.run(ebay_import_service.sync_ebay_live_prices(db, ebay=ebay))
    db.refresh(ok)
    assert ok.ebay_live_prices == {"AE-E2": 21.95}   # zweites Listing normal abgeglichen
    assert res["checked"] == 1                        # nur E2 erfolgreich
    assert len(res["errors"]) == 1 and res["errors"][0]["listing_id"] == db.query(Listing).filter_by(ebay_item_id="E1").one().id


def test_sync_only_ids_restricts_to_given_listing(db):
    """Per-Listing-Sofortabgleich (💶 eBay-Preis): only_ids beschraenkt auf EIN Listing (1 GetItem)."""
    a = _active_listing(db, item_id="OA", sku="AE-OA", price=24.95)
    b = _active_listing(db, item_id="OB", sku="AE-OB", price=24.95)
    ebay = _FakeEbayPrices({"OA": {"item_id": "OA", "current_price": 17.95, "variations": []},
                            "OB": {"item_id": "OB", "current_price": 19.95, "variations": []}})
    res = asyncio.run(ebay_import_service.sync_ebay_live_prices(db, ebay=ebay, only_ids=[a.id]))
    db.refresh(a)
    db.refresh(b)
    assert a.ebay_live_prices == {"AE-OA": 17.95}   # nur A abgeglichen
    assert b.ebay_live_prices is None                # B unberuehrt
    assert ebay.calls == ["OA"] and res["checked"] == 1


def test_sync_empty_result_does_not_wipe_previous_price(db):
    """GELD-SCHUTZ (Review 17.07.): ein flaky/leeres GetItem-Ergebnis darf einen ZUVOR
    bestaetigten echten Preis NICHT auf None zuruecksetzen – sonst versteckt genau EIN
    schlechter Sync wieder einen realen Verlust (die Drift-Warnung verschwaende)."""
    l = _active_listing(db, item_id="W1", sku="AE-W1", price=29.95)
    good = _FakeEbayPrices({"W1": {"item_id": "W1", "current_price": 17.95, "variations": []}})
    asyncio.run(ebay_import_service.sync_ebay_live_prices(db, ebay=good))
    db.refresh(l)
    assert l.ebay_live_prices == {"AE-W1": 17.95}    # echter Preis erfasst
    synced_at = l.ebay_price_synced_at

    # Zweiter Lauf: GetItem liefert diesmal KEINEN Preis (blanke Variation, kein current_price)
    empty = _FakeEbayPrices({"W1": {"item_id": "W1", "current_price": None,
                                     "variations": [{"sku": "", "price": None}]}})
    res = asyncio.run(ebay_import_service.sync_ebay_live_prices(db, ebay=empty))
    db.refresh(l)
    assert l.ebay_live_prices == {"AE-W1": 17.95}    # UNVERAENDERT – nicht auf None geloescht
    assert l.ebay_price_synced_at == synced_at        # kein Schein-Update des Zeitstempels
    assert res["checked"] == 1 and res["updated"] == 0 and res["empty"] == 1


def test_sync_skips_non_active_and_missing_item_id(db):
    """Nur aktive Listings MIT ebay_item_id werden abgefragt."""
    p = Product(aliexpress_url="https://ae/na", aliexpress_id="aena")
    db.add(p)
    db.flush()
    db.add(Listing(product_id=p.id, ebay_sku="AE-NA", ebay_item_id=None, title_seo="T",
                   description="d", listing_status="active", price_eur=Decimal("24.95")))
    db.add(Listing(product_id=p.id, ebay_sku="AE-END", ebay_item_id="ZZ", title_seo="T",
                   description="d", listing_status="ended", price_eur=Decimal("24.95")))
    db.commit()
    ebay = _FakeEbayPrices({"ZZ": {"item_id": "ZZ", "current_price": 1.0, "variations": []}})
    res = asyncio.run(ebay_import_service.sync_ebay_live_prices(db, ebay=ebay))
    assert res["checked"] == 0 and ebay.calls == []   # weder ohne item_id noch "ended" abgefragt


# ---------------------------------------------------------------- Drift-Marge im Report


def _variant_product(db, *, ebay_live_prices=None):
    p = Product(
        aliexpress_url="https://de.aliexpress.com/item/pt-1.html", aliexpress_id="pt1",
        price_cny=Decimal("6.00"), supplier_id="store-a",
        variants={"axes": {"Farbe": ["Schwarz", "Rot"]},
                  "skus": [{"attr": "14:1", "options": {"Farbe": "Schwarz"}, "id": 1,
                            "price": 6.0, "stock": 9},
                           {"attr": "14:2", "options": {"Farbe": "Rot"}, "id": 2,
                            "price": 8.0, "stock": 9}]})
    db.add(p)
    db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-PT", ebay_item_id="PT", title_seo="T",
                description="d", listing_status="active", price_eur=Decimal("24.95"),
                cost_eur=Decimal("7.00"), ebay_live_prices=ebay_live_prices)
    db.add(l)
    db.commit()
    return p, l


def test_variant_rows_margin_on_real_ebay_price_and_drift_flag(db):
    """Weicht der echte eBay-Preis (12,95 €) vom internen (~19-22 €) ab, wird die Marge auf
    dem ECHTEN Preis gerechnet und die Variante als Drift markiert – keine Scheinmarge."""
    from app.config import get_settings
    p, l = _variant_product(db, ebay_live_prices={"AE-PT-V1": 12.95, "AE-PT-V2": 12.95})
    rows = _variant_rows(l, p, get_settings())
    assert rows, "Varianten-Report sollte Zeilen liefern"
    for r in rows:
        assert r["ebay_price_eur"] == 12.95
        assert r["price_drift"] is True
        # Marge/Gewinn auf dem echten (niedrigen) Preis: Gewinn = 12,95 − Gebuehr − Fix − EK.
        # Muss GERINGER sein als auf dem internen Preis (Scheinmarge waere hoeher).
        assert r["profit_eur"] < 12.95
        assert r["margin"] is not None


def test_variant_rows_conservative_margin_on_key_mismatch(db):
    """CRITICAL-Schutz: matchen die eBay-SKUs NICHT die Positions-SKUs (importiertes Listing), wird
    die Marge KONSERVATIV auf dem niedrigsten echten eBay-Preis gerechnet – nie optimistisch auf dem
    internen Preis (sonst gruene Marge, obwohl real Verlust). Gilt fuer JEDE Variante."""
    from app.config import get_settings
    p, l = _variant_product(db, ebay_live_prices={"WRONG-KEY-1": 12.95, "WRONG-KEY-2": 14.95})
    rows = _variant_rows(l, p, get_settings())
    assert rows
    for r in rows:
        assert r["ebay_price_eur"] == 12.95        # konservativ der niedrigste echte Preis
        assert r["price_drift"] is True
        # Konservativ auf 12,95 gerechnet deckt hier sogar einen VERLUST auf (EK 7 €, Preis 12,95)
        assert r["profit_eur"] < 0
        # Marge entspricht der auf 12,95 gerechneten (auf 4 Nachkommastellen gerundet), NICHT der internen
        assert abs(r["margin"] - (r["profit_eur"] / 12.95)) < 1e-3


def test_variant_rows_resolve_per_variant_price_via_signature(db):
    """Nutzer-Fund (Beige Sonnenschutz-Netz): eBay-SKUs != Positions-SKUs, aber die Merkmals-Werte
    matchen -> JEDE Variante zeigt IHREN echten eBay-Preis (Schwarz 22,95 / Rot 76,95), nicht fuer
    alle den niedrigsten. Genau das war der Bug (alle 22,95)."""
    from app.config import get_settings
    # eBay-SKUs bewusst FALSCH (importiert); nur die sig:-Keys treffen die Options-Werte.
    p, l = _variant_product(db, ebay_live_prices={
        "IMPORTED-A": 22.95, "IMPORTED-B": 76.95, "sig:schwarz": 22.95, "sig:rot": 76.95})
    rows = _variant_rows(l, p, get_settings())
    by_name = {r["name"]: r for r in rows}
    assert by_name["Schwarz"]["ebay_price_eur"] == 22.95
    assert by_name["Rot"]["ebay_price_eur"] == 76.95        # NICHT 22,95 (der Min-Fallback-Bug)
    # unterschiedliche Preise pro Variante -> Bug behoben
    assert {r["ebay_price_eur"] for r in rows} == {22.95, 76.95}


def test_variant_price_rows_resolve_per_variant_price_via_signature(db):
    """Preise-anpassen-Dialog: derselbe Signatur-Weg -> je Variante ihr echter eBay-Preis, auch
    wenn die eBay-SKUs nicht den Positions-SKUs entsprechen."""
    from app.config import get_settings
    from app.services import golive_service
    p, l = _variant_product(db, ebay_live_prices={
        "IMPORTED-A": 22.95, "IMPORTED-B": 76.95, "sig:schwarz": 22.95, "sig:rot": 76.95})
    rows = golive_service.variant_price_rows(l, p, get_settings())
    by_name = {r["name"]: r for r in rows}
    assert by_name["Schwarz"]["current_price_eur"] == 22.95
    assert by_name["Rot"]["current_price_eur"] == 76.95
    assert all(r["price_is_live"] for r in rows)


def _variant_product_extra_axis(db, *, ebay_live_prices=None):
    """Produkt mit einer KONSTANT-Achse ("Verpackung": nur 1 Wert), die `_usable_variants` NICHT
    als eBay-Achse publiziert, die aber in JEDEM SKU-options-Dict steht – so wie viele
    AliExpress-Scrapes. Publiziert/als Merkmal gespeichert wird nur die echte Achse "Farbe"."""
    p = Product(
        aliexpress_url="https://de.aliexpress.com/item/xa-1.html", aliexpress_id="xa1",
        price_cny=Decimal("6.00"), supplier_id="store-a",
        variants={"axes": {"Farbe": ["Schwarz", "Rot"], "Verpackung": ["Karton"]},
                  "skus": [{"attr": "14:1", "options": {"Farbe": "Schwarz", "Verpackung": "Karton"},
                            "id": 1, "price": 6.0, "stock": 9},
                           {"attr": "14:2", "options": {"Farbe": "Rot", "Verpackung": "Karton"},
                            "id": 2, "price": 8.0, "stock": 9}]})
    db.add(p)
    db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-XA", ebay_item_id="XA", title_seo="T",
                description="d", listing_status="active", price_eur=Decimal("24.95"),
                cost_eur=Decimal("7.00"), ebay_live_prices=ebay_live_prices)
    db.add(l)
    db.commit()
    return p, l


def test_variant_rows_signature_ignores_unpublished_constant_axis(db):
    """Review-Fund 17.07.: der rohe options-Dict hat eine zusaetzliche KONSTANT-Achse
    ("Verpackung"), die NICHT auf eBay publiziert wird. Die Signatur wird NUR aus den publizierten
    Achsen (Farbe) gebaut – sonst waere sie 'karton|schwarz' und wuerde die gespeicherte
    'sig:schwarz' verfehlen -> stiller Rueckfall auf den Min-Preis fuer ALLE (der alte Bug)."""
    from app.config import get_settings
    p, l = _variant_product_extra_axis(db, ebay_live_prices={
        "IMPORTED-A": 22.95, "IMPORTED-B": 76.95, "sig:schwarz": 22.95, "sig:rot": 76.95})
    rows = _variant_rows(l, p, get_settings())
    by_name = {r["name"]: r for r in rows}
    assert by_name["Schwarz"]["ebay_price_eur"] == 22.95
    assert by_name["Rot"]["ebay_price_eur"] == 76.95        # matcht trotz Extra-Achse
    assert {r["ebay_price_eur"] for r in rows} == {22.95, 76.95}


def test_variant_rows_no_live_price_is_unchanged(db):
    """Ohne Abgleich (ebay_live_prices=None) bleibt alles wie bisher: kein Drift, Marge intern."""
    from app.config import get_settings
    p, l = _variant_product(db, ebay_live_prices=None)
    rows = _variant_rows(l, p, get_settings())
    assert rows
    for r in rows:
        assert r["ebay_price_eur"] is None
        assert r["price_drift"] is False
        # Marge auf dem internen Preis gerechnet (current_price_eur == Kalkulationspreis)
        assert r["current_price_eur"] is not None


# ---------------------------------------------------------------- Drift-Helfer + Fallback


def test_price_drift_helpers():
    from app.services.listing_match_service import _price_drifts, _live_price_values

    class _L:
        ebay_live_prices = {"a": 17.95, "b": 19.95, "c": None}
    assert _live_price_values(_L()) == [17.95, 19.95]            # None gefiltert
    assert _live_price_values(type("X", (), {"ebay_live_prices": None})()) == []
    assert _price_drifts(29.95, 17.95) is True                   # >= 2 % / 0,50 €
    assert _price_drifts(18.00, 17.95) is False                  # < Schwelle
    assert _price_drifts(29.95, None) is False                   # kein Live-Preis -> kein Drift
    assert _price_drifts(None, 17.95) is False


def test_effective_ebay_price_helper():
    """EINHEITLICHE Preis-Quelle: exakter SKU-Treffer, sonst konservativ der niedrigste, sonst None."""
    from app.services.listing_match_service import effective_ebay_price

    class _L:
        ebay_live_prices = {"AE-X-V1": 17.95, "AE-X-V2": 22.95}
    assert effective_ebay_price(_L(), "AE-X-V1") == 17.95        # exakter Treffer
    assert effective_ebay_price(_L(), "UNBEKANNT") == 17.95      # kein Treffer -> konservativ min
    assert effective_ebay_price(_L(), None) == 17.95             # ohne sku -> konservativ min
    assert effective_ebay_price(type("N", (), {"ebay_live_prices": None})(), "x") is None   # nicht abgeglichen


def test_effective_ebay_price_by_value_signature():
    """Kern des Nutzer-Funds (Beige Sonnenschutz-Netz): die eBay-SKUs matchen die Positions-SKUs
    NICHT, aber die Merkmals-WERTE tun es -> jede Variante findet IHREN echten Preis (nicht den
    fuer alle gleichen niedrigsten Preis). SKU-Treffer hat weiter Vorrang; Signatur ist der 2. Weg."""
    from app.services.listing_match_service import effective_ebay_price

    class _L:
        ebay_live_prices = {"EBAY-XYZ-1": 22.95, "EBAY-XYZ-2": 76.95,
                            "sig:3x4m": 22.95, "sig:6x8m": 76.95}
    # SKU trifft nicht -> ueber die Options-Werte matchen (dict ODER Liste als options):
    assert effective_ebay_price(_L(), "AE-POS-V1", {"Größe": "3x4 m"}) == 22.95
    assert effective_ebay_price(_L(), "AE-POS-V2", {"Größe": "6x8 m"}) == 76.95   # NICHT 22,95!
    assert effective_ebay_price(_L(), "AE-POS-V2", ["6x8 m"]) == 76.95            # Liste erlaubt
    # exakter SKU-Treffer hat Vorrang vor der Signatur (auch wenn options mitgegeben werden):
    assert effective_ebay_price(_L(), "EBAY-XYZ-2", {"Größe": "3x4 m"}) == 76.95
    # unbekannte Optionen -> konservativ der niedrigste echte Preis
    assert effective_ebay_price(_L(), "AE-POS-V9", {"Größe": "99x99 m"}) == 22.95


def test_ebay_produkte_shows_live_price(db):
    """eBay-Produkte-Tab: der angezeigte Preis ist der ECHTE eBay-Preis (nicht der interne)."""
    from app.services import analytics_service
    p = Product(aliexpress_url="https://ae/kb", aliexpress_id="kb", price_cny=Decimal("30"))
    db.add(p)
    db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-KB", ebay_item_id="KB", title_seo="Kulturbeutel",
                description="d", listing_status="active", price_eur=Decimal("29.95"),
                cost_eur=Decimal("8.00"), ebay_live_prices={"AE-KB": 17.95})
    db.add(l)
    db.commit()
    row = {r["listing_id"]: r for r in analytics_service.list_ebay_products(db)["products"]}[l.id]
    assert row["price_eur"] == 17.95            # echter eBay-Preis, nicht 29,95 intern
    assert row["price_is_live"] is True


def test_listing_detail_shows_live_price_and_honest_profit(db):
    """Listing-Detail-Modal (Coverage-Audit-Fund 17.07.): Preis + Gewinn auf dem ECHTEN eBay-Preis,
    nicht dem internen – sonst zeigt es als einzige Flaeche eine optimistische Scheinmarge bei Drift."""
    from app.services import analytics_service
    p = Product(aliexpress_url="https://ae/dt", aliexpress_id="dt", price_cny=Decimal("30"))
    db.add(p)
    db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-DT", ebay_item_id="DT", title_seo="Detail",
                description="d", listing_status="active", price_eur=Decimal("29.95"),
                cost_eur=Decimal("8.00"), ebay_live_prices={"AE-DT": 17.95})
    db.add(l)
    db.commit()
    d = analytics_service.get_listing_detail(db, listing_id=l.id)
    assert d["price_eur"] == 17.95              # echter eBay-Preis, nicht 29,95 intern
    assert d["price_is_live"] is True
    assert d["internal_price_eur"] == 29.95     # interner Preis bleibt separat sichtbar
    # Gewinn auf 17,95 gerechnet (VK − Gebuehren − EK) < roher Rohertrag 17,95−8 = 9,95
    assert d["profit_eur"] is not None and d["profit_eur"] < 9.95


def test_listing_detail_falls_back_to_internal_before_sync(db):
    """Ohne Live-Abgleich (ebay_live_prices None) bleibt der interne Preis, price_is_live False."""
    from app.services import analytics_service
    p = Product(aliexpress_url="https://ae/dt2", aliexpress_id="dt2", price_cny=Decimal("30"))
    db.add(p)
    db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-DT2", ebay_item_id="DT2", title_seo="Detail2",
                description="d", listing_status="active", price_eur=Decimal("24.95"),
                cost_eur=Decimal("8.00"), ebay_live_prices=None)
    db.add(l)
    db.commit()
    d = analytics_service.get_listing_detail(db, listing_id=l.id)
    assert d["price_eur"] == 24.95
    assert d["price_is_live"] is False


def test_variant_price_rows_current_is_ebay_live(db):
    """Preise-anpassen-Dialog: „aktueller VK" je Variante ist der ECHTE eBay-Preis."""
    from app.config import get_settings
    from app.services import golive_service
    p, l = _variant_product(db, ebay_live_prices={"AE-PT-V1": 12.95, "AE-PT-V2": 14.95})
    rows = golive_service.variant_price_rows(l, p, get_settings())
    by_sku = {r["sku"]: r for r in rows}
    assert by_sku["AE-PT-V1"]["current_price_eur"] == 12.95      # exakter eBay-Preis der Variante
    assert by_sku["AE-PT-V2"]["current_price_eur"] == 14.95
    assert all(r["price_is_live"] for r in rows)


def test_report_listing_level_drift_with_mismatched_keys(db, monkeypatch, tmp_path):
    """Kern des Nutzer-Funds: auch wenn die gespeicherten eBay-SKUs NICHT den Positions-SKUs
    entsprechen (importiertes/AutoDS-Listing), zeigt das Cockpit die ECHTE eBay-Preis-Spanne
    + Drift auf Listing-Ebene – nicht mehr nur den internen Preis."""
    from app.services import listing_match_service
    monkeypatch.setattr(listing_match_service, "REPORT_FILE", str(tmp_path / "r.json"))
    p = Product(aliexpress_url="https://de.aliexpress.com/item/500.html", aliexpress_id="500",
                title_raw="Kulturbeutel", price_cny=Decimal("30.00"))
    db.add(p)
    db.flush()
    l = Listing(product_id=p.id, title_seo="Kulturbeutel zum Aufhängen", description="d",
                listing_status="active", price_eur=Decimal("29.95"),
                ebay_live_prices={"IMPORTED-SKU-XYZ": 17.95})   # Key matcht NICHT AE-{id}
    db.add(l)
    db.commit()

    listing_match_service.rebuild_reprice_report(db)
    row = {r["listing_id"]: r for r in listing_match_service.reprice_report(db)["rows"]}[l.id]
    assert row["ebay_price_min_eur"] == 17.95      # echter eBay-Preis trotz Key-Mismatch sichtbar
    assert row["price_drift"] is True              # intern 29,95 vs. eBay 17,95 -> Drift


async def test_scheduler_price_job_rebuilds_report_after_sync(monkeypatch):
    """GELD-/Sichtbarkeits-Schutz: nach dem Live-Preis-Abgleich MUSS der Cockpit-Report neu
    gerechnet werden – sonst zeigt das Cockpit weiter den alten (internen) Preis."""
    from app import scheduler
    from app.services import ebay_import_service, listing_match_service
    order = []

    async def _fake_sync(db):
        order.append("sync")
        return {"checked": 1, "updated": 1, "drift": 1, "errors": []}

    def _fake_rebuild(db):
        order.append("rebuild")

    monkeypatch.setattr(ebay_import_service, "sync_ebay_live_prices", _fake_sync)
    monkeypatch.setattr(listing_match_service, "rebuild_reprice_report", _fake_rebuild)
    await scheduler._ebay_price_sync_job()
    assert order == ["sync", "rebuild"]            # Reihenfolge: erst Preise, dann Report neu
