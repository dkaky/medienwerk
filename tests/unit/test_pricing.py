"""Tests fuer die Pricing-Engine (Modell nach eigenem Kalkulator)."""
from __future__ import annotations

import pytest

from app.services import pricing


def test_effective_fee_pct_for_listing_uses_real_ad_rate():
    """Die Gebuehr nutzt die ECHTE Anzeigenrate des Listings (listing.ad_rate_pct), falls
    bekannt — sonst die Pauschale. Beide inkl. 19% MwSt."""
    from app.config import Settings
    s = Settings(ebay_ad_rate_pct=0.10, ebay_fee_vat_pct=0.19, use_mocks=True)

    class _L:
        category_name = "Uhren & Schmuck"   # 16% Provision
        ad_rate_pct = 0.20                   # echte Rate 20% (statt Pauschale 10%)

    class _LNone:
        category_name = "Uhren & Schmuck"
        ad_rate_pct = None                   # unbekannt -> Pauschale

    assert pricing.effective_fee_pct_for_listing(_L(), settings=s) == round((0.16 + 0.20) * 1.19, 4)
    assert pricing.effective_fee_pct_for_listing(_LNone(), settings=s) == round((0.16 + 0.10) * 1.19, 4)
    # None-Listing -> Default-Provision + Pauschale (kein Crash)
    assert pricing.effective_fee_pct_for_listing(None, settings=s) == pricing.effective_fee_pct(None, settings=s)


def test_effective_cost_flat_customs_not_percentage():
    """EK-Modell (Korrektur 14.07.): AliExpress berechnet eine FLACHE Einfuhrgebuehr
    (~3,57 EUR) je Order, KEINEN Prozentaufschlag -> teure Artikel explodieren nicht mehr."""
    from app.config import Settings
    s = Settings(aliexpress_tax_pct=0.0, customs_fee_eur=3.57,
                 aliexpress_free_shipping_threshold=10.0, aliexpress_shipping_fee_eur=1.99,
                 cny_to_eur_rate=1.0, shipping_cost_eur=0.0, use_mocks=True)
    # 5 EUR (unter Schwelle): + Versand 1,99 + Pauschalzoll 3,57 = 10,56
    assert pricing.effective_cost(5.0, settings=s) == 10.56
    # 50 EUR (ueber Schwelle): kein Versandaufschlag, NUR + 3,57 = 53,57 (nicht +16 wie bei 32 %)
    assert pricing.effective_cost(50.0, settings=s) == 53.57
    assert round(pricing.effective_cost(50.0, settings=s) - 50.0, 2) == 3.57   # fester Zuschlag
    # Bundle: Pauschalzoll faellt nur EINMAL je Sendung (nicht je Einheit)
    assert pricing.effective_cost_bundle(8.0, quantity=3, settings=s) == round(8 * 3 + 3.57, 2)


def test_matches_reference_calculator_example():
    """Der Referenz-Kalkulator PLUS die Marge-Untergrenze (Nutzerentscheidung 29.08.2026).

    Der Kalkulator rechnet "profit% 20" als AUFSCHLAG AUF DIE KOSTEN: 100 EUR Ware
    ergaben 150,95 EUR und 20 EUR Gewinn. Das sind aber nur 13,3 % MARGE - bei teurer
    Ware faellt der Aufschlag-Ansatz immer weiter unter die Zielmarge.

    Das Upload-Modell rechnete dagegen schon immer eine Marge vom Verkaufspreis. Beide
    hiessen "20 %" und meinten Verschiedenes; im Dashboard stand deshalb neben einem
    korrekten Preis dauerhaft ein widersprechender Vorschlag.

    Auf Nutzerwunsch gilt jetzt einheitlich die MARGE. Fuer teure Ware steigt der Preis
    dadurch (100 EUR Ware: 150,95 -> 167,95), fuer billige sinkt er (der Mindestgewinn
    ging von 8 auf 4 EUR). Der Kalkulator-Wert 150,95 ist damit ueberholt - er steht
    hier weiter als Beleg, WORAUS sich der neue Wert ergibt.
    """
    b = pricing.compute_price(100.0, fee_pct=0.20, fixed_fee_eur=0.45, profit_pct=0.20,
                              profit_eur=0.0, min_profit_eur=8.0, price_cents=0.95,
                              min_price_eur=0.0, max_price_eur=99999.0)
    assert b.profit_eur == 20.0                 # max(20, 0, 8) - unveraendert
    # Alter Kalkulator-Wert: (100 + 20 + 0,45) / (1 - 0,20)      = 150,56
    # Marge-Untergrenze:     (100 + 0,45) / (1 - 0,20 - 0,20)    = 167,42
    # Die zweite gewinnt, weil 20 % Marge NACH der 20 % Gebuehr uebrig bleiben muessen.
    assert b.price_eur == 167.42
    assert b.rounded_price_eur == 167.95        # aufgerundet auf ...,95
    assert b.margin_pct >= 0.20, "die Zielmarge muss jetzt wirklich getragen werden"


def test_profit_is_maximum_of_components():
    # niedrige Kosten -> Mindestgewinn (8) greift
    low = pricing.compute_price(10.0, fee_pct=0.12, fixed_fee_eur=0.45, profit_pct=0.20,
                                profit_eur=0.0, min_profit_eur=8.0, price_cents=0.95)
    assert low.profit_eur == 8.0                # max(2, 0, 8)
    # hohe Kosten -> Prozent-Gewinn greift
    high = pricing.compute_price(100.0, fee_pct=0.12, fixed_fee_eur=0.45, profit_pct=0.20,
                                 profit_eur=0.0, min_profit_eur=8.0, price_cents=0.95)
    assert high.profit_eur == 20.0
    # fixer Zusatzgewinn kann gewinnen
    flat = pricing.compute_price(10.0, profit_pct=0.0, profit_eur=15.0, min_profit_eur=8.0)
    assert flat.profit_eur == 15.0


def test_cents_rounding_ends_in_value():
    b = pricing.compute_price(37.0, price_cents=0.95)
    assert round((b.rounded_price_eur * 100) % 100) == 95
    b99 = pricing.compute_price(37.0, price_cents=0.99)
    assert round((b99.rounded_price_eur * 100) % 100) == 99


def test_round_up_to_cents_never_below_input():
    # AUFrunden: Ergebnis nie unter dem Eingabewert, Endung immer ...,95
    for p in [10.09, 10.259, 38.707, 9.96, 15.0, 0.10, 54.30, 20.95]:
        r = pricing.round_up_to_cents(p, 0.95)
        assert r >= p - 1e-9, f"{r} < {p}"
        assert round((r * 100) % 100) == 95


def test_price_floor_rounded_up_guarantees_target_margin():
    # Regression (Review 07/2026): price_floor + round_to_nearest_cents konnte UNTER die
    # Zielmarge runden (z.B. 19,3 %). round_up_to_cents muss die >=Zielmarge-Garantie halten,
    # weil die Marge monoton mit dem VK steigt (VK >= Boden -> Marge >= Zielmarge).
    from app.config import get_settings
    s = get_settings()
    fix, m, cents = s.ebay_fixed_fee_eur, s.target_margin_pct, (s.price_cents or 0.95)
    for cat in ["Uhren & Schmuck", "TV, Video & Audio", "Kleidung & Accessoires", None]:
        fee = pricing.effective_fee_pct(cat, settings=s)
        for ek in [3.0, 5.95, 8.08, 9.58, 20.59, 34.39, 44.99]:
            fl = pricing.price_floor(ek, min_margin_pct=m, category_name=cat, settings=s)
            if not fl:
                continue
            vk = pricing.round_up_to_cents(fl, cents)
            margin = (vk - vk * fee - fix - ek) / vk
            assert margin >= m - 1e-9, f"cat={cat} ek={ek} vk={vk} margin={margin:.4f} < {m}"


def test_price_covers_costs_and_yields_profit():
    b = pricing.compute_price(20.0)
    assert b.rounded_price_eur > 20.0
    # Nettogewinn auf gerundetem Preis bleibt positiv
    assert b.rounded_price_eur - b.cost_eur - round(b.rounded_price_eur * 0.12, 2) - b.fixed_fee_eur > 0


def test_max_price_clamp():
    b = pricing.compute_price(5000.0, min_price_eur=0.0, max_price_eur=99.0)
    assert b.rounded_price_eur == 99.0
    assert b.clamped == "max"


def test_price_from_cny_eur_bezug():
    # cny_to_eur_rate Default = 1.0 -> Wert ist bereits EUR
    b = pricing.price_from_cny(30.0)
    assert b.cost_eur >= 30.0
    assert b.rounded_price_eur > b.cost_eur


def test_higher_profit_pct_raises_price():
    low = pricing.compute_price(50.0, profit_pct=0.10, min_profit_eur=0.0)
    high = pricing.compute_price(50.0, profit_pct=0.60, min_profit_eur=0.0)
    assert high.rounded_price_eur > low.rounded_price_eur


def test_self_stock_uses_own_ek_and_stays_sellable(db):
    """P3: Eigenbestand-Variante rechnet mit EIGENEM EK, bleibt lieferbar solange qty>0
    (auch wenn der Lieferant 0 hat), qty=0 -> wieder ausverkauft, clear -> zurueck zur Quelle."""
    from decimal import Decimal
    from app.config import get_settings
    from app.models import Listing, Product
    from app.services import listing_match_service as lm, supplier_service as sup
    p = Product(aliexpress_url="https://de.aliexpress.com/item/ss.html", aliexpress_id="ss",
                variants={"axes": {"Farbe": ["Rot", "Blau"]},
                          "skus": [{"attr": "a0", "options": {"Farbe": "Rot"}, "price": "5", "stock": 0},
                                   {"attr": "a1", "options": {"Farbe": "Blau"}, "price": "5", "stock": 10}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-ss", ebay_item_id="123", title_seo="T",
                description="d", listing_status="active", price_eur=Decimal("19.95"))
    db.add(l); db.commit()
    s = get_settings()

    # Rot ist beim Lieferanten ausverkauft -> ohne Eigenbestand: oos, nicht verkaufbar
    rot0 = {r["attr"]: r for r in lm._variant_rows(l, p, s)}["a0"]
    assert rot0["oos"] is True and rot0["sellable"] is False and rot0["self_stock"] is False

    sup.set_self_stock(db, listing_id=l.id, sku_attr="a0", qty=5, cost_eur=2.0, price_eur=14.95)
    rot = {r["attr"]: r for r in lm._variant_rows(l, p, s)}["a0"]
    assert rot["self_stock"] is True and rot["self_qty"] == 5
    assert rot["ek_eur"] == 2.0                       # eigener EK, NICHT effective_cost(5)
    assert rot["oos"] is False and rot["sellable"] is True   # lieferbar trotz Lieferant 0

    sup.set_self_stock(db, listing_id=l.id, sku_attr="a0", qty=0, cost_eur=2.0)
    rot2 = {r["attr"]: r for r in lm._variant_rows(l, p, s)}["a0"]
    assert rot2["oos"] is True and rot2["sellable"] is False   # Eigenbestand leer -> ausverkauft

    sup.clear_self_stock(db, listing_id=l.id, sku_attr="a0")
    rot3 = {r["attr"]: r for r in lm._variant_rows(l, p, s)}["a0"]
    assert rot3["self_stock"] is False                # zurueck auf die Quelle


def test_self_stock_fulfill_ships_own_and_decrements(db):
    """P3b: verkaufte Eigenbestand-Variante wird als self_shipped erkannt (kein AliExpress-
    Kauf) + Menge -1. set_self_stock merged (reines Mengen-Update behaelt EK/Preis)."""
    from decimal import Decimal
    from app.models import Listing, Product, Sale
    from app.services import supplier_service as sup
    from app.services.order_service import _resolve_self_stock_attr
    p = Product(aliexpress_url="https://de.aliexpress.com/item/f.html", aliexpress_id="f",
                variants={"axes": {"Farbe": ["Rot", "Blau"]},
                          "skus": [{"attr": "a0", "options": {"Farbe": "Rot"}, "price": "5", "stock": 0},
                                   {"attr": "a1", "options": {"Farbe": "Blau"}, "price": "5", "stock": 5}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-f", title_seo="T", description="d",
                listing_status="active", price_eur=Decimal("19.95"))
    db.add(l); db.commit()
    sale_rot = Sale(ebay_transaction_id="tr", listing_id=l.id, status="pending",
                    price_eur=Decimal("19.95"), quantity=1, variant_selected={"Farbe": "Rot", "attr": "a0"})

    assert _resolve_self_stock_attr(sale_rot, l, p) is None          # noch kein Eigenbestand
    sup.set_self_stock(db, listing_id=l.id, sku_attr="a0", qty=3, cost_eur=2.0, price_eur=14.95)
    db.refresh(l)
    assert _resolve_self_stock_attr(sale_rot, l, p) == "a0"          # erkannt
    # MERGE: reines Mengen-Update behaelt EK/Preis
    sup.set_self_stock(db, listing_id=l.id, sku_attr="a0", qty=5)
    db.refresh(l)
    assert l.self_stock["a0"]["cost_eur"] == 2.0 and l.self_stock["a0"]["price_eur"] == 14.95
    # leer -> nicht mehr als Eigenbestand-Fulfillment erkannt
    sup.set_self_stock(db, listing_id=l.id, sku_attr="a0", qty=0)
    db.refresh(l)
    assert _resolve_self_stock_attr(sale_rot, l, p) is None
    # andere (Nicht-Eigenbestand-)Variante -> None
    sale_blau = Sale(ebay_transaction_id="tr2", listing_id=l.id, status="pending",
                     price_eur=Decimal("19.95"), quantity=1, variant_selected={"Farbe": "Blau", "attr": "a1"})
    assert _resolve_self_stock_attr(sale_blau, l, p) is None


def test_sold_out_center_surfaces_gone_listings(db):
    """P2: das Ausverkauf-Center listet ausverkaufte (Quelle aus) Listings, lieferbare nicht."""
    from decimal import Decimal
    from app.models import Listing, Product
    from app.services import listing_match_service as lm
    p = Product(aliexpress_url="https://de.aliexpress.com/item/soc.html", aliexpress_id="soc",
                price_cny=Decimal("5"))
    db.add(p); db.flush()
    gone = Listing(product_id=p.id, ebay_sku="AE-soc", ebay_item_id="E-soc", title_seo="Ausverkauft",
                   description="d", listing_status="active", price_eur=Decimal("14.95"),
                   supplier_in_stock=False)
    db.add(gone)
    p2 = Product(aliexpress_url="https://de.aliexpress.com/item/ok.html", aliexpress_id="ok",
                 price_cny=Decimal("5"))
    db.add(p2); db.flush()
    ok = Listing(product_id=p2.id, ebay_sku="AE-ok", title_seo="Lieferbar", description="d",
                 listing_status="active", price_eur=Decimal("14.95"), supplier_in_stock=True)
    db.add(ok); db.commit()

    res = lm.sold_out_center(db)
    ids = {it["listing_id"] for it in res["items"]}
    assert gone.id in ids and ok.id not in ids
    it = next(it for it in res["items"] if it["listing_id"] == gone.id)
    assert it["has_alt"] is False and res["summary"]["without_alt"] >= 1


def test_sold_out_center_catches_gone_multivariant_even_if_variant_stock_stale(db):
    """P5/R11 (Pikachu-Fix): ein MULTIVARIANTEN-Listing, dessen Quelle weg ist
    (monitor_status=out_of_stock), muss als GANZ-Listing-Eintrag erscheinen — auch wenn der
    zwischengespeicherte Varianten-Bestand nach dem 404 noch „lieferbar" zeigt (keine
    einzelne Variante gilt als oos). Vorher rutschte so ein Listing komplett durch."""
    from decimal import Decimal
    from app.models import Listing, Product
    from app.services import listing_match_service as lm
    p = Product(aliexpress_url="https://de.aliexpress.com/item/pika.html", aliexpress_id="pika",
                price_cny=Decimal("6"),
                variants={"axes": {"Farbe": ["Grau", "Bunt"]},
                          "skus": [{"attr": "1:1", "options": {"Farbe": "Grau"}, "id": 1, "price": 6.0, "stock": 9},
                                   {"attr": "1:2", "options": {"Farbe": "Bunt"}, "id": 2, "price": 8.0, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-pika", ebay_item_id="E-pika", title_seo="Pikachu Leinwand",
                description="d", listing_status="active", price_eur=Decimal("19.95"),
                supplier_in_stock=True, monitor_status="out_of_stock")   # Quelle 404, Varianten stale
    db.add(l); db.commit()

    res = lm.sold_out_center(db)
    it = next((it for it in res["items"] if it["listing_id"] == l.id), None)
    assert it is not None, "gone Multivariante MUSS im Center erscheinen (Pikachu-Fix)"
    assert it["kind"] == "listing" and it["state"] == "gone"
    assert res["summary"]["gone_listings"] >= 1


def test_sold_out_center_orphaned_yes_but_paused_no(db):
    """P5-Review-Fixes: (a) VERWAISTES Dropship-Listing (Produkt gelöscht) erscheint als tot;
    (b) ein PAUSIERTES Listing (sales_hold) erscheint NICHT — bewusst gestoppt, kein Handlungsfall."""
    from decimal import Decimal
    from app.models import Listing, Product
    from app.services import listing_match_service as lm
    # (a) verwaist: product_id zeigt auf ein nicht (mehr) existierendes Produkt
    orph = Listing(product_id=999999, ebay_sku="AE-orph", ebay_item_id="E-orph", title_seo="Verwaist",
                   description="d", listing_status="active", price_eur=Decimal("14.95"),
                   supplier_in_stock=True)
    db.add(orph)
    # (b) pausiert + monitor out_of_stock -> soll NICHT im Center auftauchen
    p = Product(aliexpress_url="https://de.aliexpress.com/item/hold.html", aliexpress_id="hold",
                price_cny=Decimal("5"))
    db.add(p); db.flush()
    held = Listing(product_id=p.id, ebay_sku="AE-hold", ebay_item_id="E-hold", title_seo="Pausiert",
                   description="d", listing_status="active", price_eur=Decimal("14.95"),
                   supplier_in_stock=False, monitor_status="out_of_stock", sales_hold=True)
    db.add(held); db.commit()

    res = lm.sold_out_center(db)
    ids = {it["listing_id"] for it in res["items"]}
    assert orph.id in ids, "verwaistes Dropship-Listing MUSS erscheinen"
    assert held.id not in ids, "pausiertes Listing darf NICHT im Center erscheinen"


def test_reprice_report_exposes_gone_flags(db):
    """P5: jede Report-Zeile trägt monitor_oos + fully_out, damit die Handlungsleiste den
    'ausverkauft ohne Alternative'-Fall (inkl. gone) ohne Live-Scrape erkennt."""
    from decimal import Decimal
    from app.models import Listing, Product
    from app.services import listing_match_service as lm
    p = Product(aliexpress_url="https://de.aliexpress.com/item/gf.html", aliexpress_id="gf",
                price_cny=Decimal("6"))
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-gf", ebay_item_id="E-gf", title_seo="Weg",
                description="d", listing_status="active", price_eur=Decimal("19.95"),
                supplier_in_stock=True, monitor_status="out_of_stock")
    db.add(l); db.commit()
    lm.rebuild_reprice_report(db)
    data = lm.reprice_report(db)
    row = next(r for r in data["rows"] if r["listing_id"] == l.id)
    assert row.get("monitor_oos") is True and row.get("fully_out") is True


def test_reprice_report_exposes_ranges(db):
    """P1: der Report liefert je Listing EK/Preis/Gewinn/Marge als Spanne (min–max),
    konsistent (min<=max) und bei Einzel-Listing = der Headline-Wert."""
    from decimal import Decimal
    from app.models import Listing, Product
    from app.services import listing_match_service as lm
    p = Product(aliexpress_url="https://de.aliexpress.com/item/rng.html", aliexpress_id="rng",
                price_cny=Decimal("8.0"))
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-rng", ebay_item_id="E-rng", title_seo="T",
                description="d", listing_status="active", price_eur=Decimal("24.95"),
                cost_eur=Decimal("9"), supplier_in_stock=True)
    db.add(l); db.commit()
    lm.rebuild_reprice_report(db)
    row = {r["listing_id"]: r for r in lm.reprice_report(db)["rows"]}[l.id]
    for k in ("ek_min_eur", "ek_max_eur", "price_min_eur", "price_max_eur",
              "profit_min_eur", "profit_max_eur", "margin_min", "margin_max"):
        assert k in row
    assert row["ek_min_eur"] <= row["ek_max_eur"]
    assert row["price_min_eur"] <= row["price_max_eur"]
    assert row["ek_min_eur"] == row["cost_eff_eur"]      # Einzel-Listing: Spanne = Headline


def test_model_signature_changes_with_constants():
    """P1: aendert sich eine Kalkulations-Konstante, aendert sich die Report-Signatur ->
    Auto-Rebuild. Gleiche Konstanten -> gleiche Signatur (stabil)."""
    from app.config import Settings
    from app.services import listing_match_service as lm
    base = Settings(customs_fee_eur=3.57, use_mocks=True)
    assert lm._model_signature(base) == lm._model_signature(Settings(customs_fee_eur=3.57, use_mocks=True))
    assert lm._model_signature(base) != lm._model_signature(Settings(customs_fee_eur=4.20, use_mocks=True))
    assert lm._model_signature(base) != lm._model_signature(Settings(ebay_fee_vat_pct=0.20, use_mocks=True))


def test_reprice_report_margin_threshold(db, monkeypatch):
    """Anhebung nur bei Marge < 20%; >= 20% = 'Preis passt' (Nutzerregel)."""
    from app.config import get_settings
    from app.models import Listing, Product
    from app.services import listing_match_service as lm
    # Schwellen-Logik isoliert: EK-Aufschlaege (Prozent + Pauschalzoll) neutralisieren.
    monkeypatch.setattr(get_settings(), "aliexpress_tax_pct", 0.0)
    monkeypatch.setattr(get_settings(), "customs_fee_eur", 0.0)

    def _mk(aid, price, cost):
        p = Product(aliexpress_url=f"https://de.aliexpress.com/item/{aid}.html",
                    aliexpress_id=aid, price_cny=cost,
                    variants={"skus": [{"attr": "a", "options": {"x": "y"}, "price": str(cost),
                                        "stock": 5, "image": "i"}]}, images=["i"])
        db.add(p); db.flush()
        l = Listing(product_id=p.id, ebay_sku="AE-"+aid, title_seo="T", description="d",
                    listing_status="active", price_eur=price, cost_eur=cost,
                    supplier_in_stock=True)
        db.add(l); db.commit()
        return l

    # hohe Marge (VK 30, EK ~5) -> >= 20% -> KEINE Anhebung
    hi = _mk("hi01", 30.0, 5.0)
    # niedrige Marge (VK 20, EK ~14) -> < 20% -> Anhebung
    lo = _mk("lo01", 20.0, 14.0)

    lm.rebuild_reprice_report(db)
    rows = {r["listing_id"]: r for r in lm.reprice_report(db)["rows"]}
    assert rows[hi.id]["needs_raise"] is False and rows[hi.id]["margin_now"] >= 0.20
    assert rows[lo.id]["needs_raise"] is True and rows[lo.id]["margin_now"] < 0.20


# ------------------------------- Kategorie-genaue Gebühren -------------------------------
def test_commission_and_effective_fee_by_category():
    from app.config import Settings
    from app.services import pricing
    s = Settings(ebay_ad_rate_pct=0.10, ebay_fee_pct=0.22, use_mocks=True)
    assert pricing.commission_pct("Uhren & Schmuck") == 0.16
    assert pricing.commission_pct("Uhren & Schmuck:Modeschmuck:Halsketten") == 0.16  # Top-Level
    assert pricing.commission_pct("Kleidung & Accessoires") == 0.12
    assert pricing.commission_pct("TV, Video & Audio") == 0.07
    assert pricing.commission_pct("Unbekannte Kategorie") == pricing._COMMISSION_DEFAULT
    assert pricing.commission_pct(None) == pricing._COMMISSION_DEFAULT
    # effektive Gebühr = (Provision + Anzeigenrate) × 1,19 MwSt (eBay besteuert die Gebühr)
    assert pricing.effective_fee_pct("Uhren & Schmuck", settings=s) == round(0.26 * 1.19, 4)  # 0.3094
    assert pricing.effective_fee_pct("TV, Video & Audio", settings=s) == round(0.17 * 1.19, 4)  # 0.2023
    assert pricing.ebay_fixed_fee(s) == round(0.45 * 1.19, 4)                                   # 0.5355


def test_profit_and_floor_are_category_aware():
    from app.config import Settings
    from app.services import pricing
    s = Settings(ebay_ad_rate_pct=0.10, ebay_fee_pct=0.22, ebay_fixed_fee_eur=0.45,
                 lowering_min_margin_pct=0.20, use_mocks=True)
    # Schmuck (26% Gebühr) frisst mehr Gewinn als Elektronik (17%) beim selben Preis/EK.
    pj = pricing.profit_at_price(25.0, 8.0, category_name="Uhren & Schmuck", settings=s)
    pe = pricing.profit_at_price(25.0, 8.0, category_name="TV, Video & Audio", settings=s)
    assert pe > pj
    # ohne Kategorie -> Default-Provision + Anzeigenrate, alles inkl. MwSt
    p0 = pricing.profit_at_price(25.0, 8.0, settings=s)
    exp_fee = pricing.effective_fee_pct(None, settings=s)
    assert p0 == round(25.0 - 25.0 * exp_fee - pricing.ebay_fixed_fee(s) - 8.0, 2)
    # Preis-Boden (20% Marge) liegt bei Schmuck höher als bei Elektronik
    assert pricing.price_floor(8.0, category_name="Uhren & Schmuck", settings=s) > \
           pricing.price_floor(8.0, category_name="TV, Video & Audio", settings=s)


# ------------------------------- Multi-Buy-Rabatt (#5) -------------------------------
def test_volume_pricing_recommends_high_margin_blocks_thin():
    from app.config import Settings
    from app.services import pricing
    s = Settings(ebay_ad_rate_pct=0.10, ebay_fixed_fee_eur=0.45, price_cents=0.95,
                 multibuy_min_margin_pct=0.25, multibuy_max_discount_pct=0.10,
                 multibuy_min_extra_profit_eur=3.0, lowering_min_margin_pct=0.20, use_mocks=True)
    # Schmuck: EK 4,49 / VK 16,95, Gebühr 26% -> hohe Marge -> empfohlen, Staffeln lohnen.
    fee_j = pricing.effective_fee_pct("Uhren & Schmuck", settings=s)   # 0.26
    plan = pricing.volume_pricing_plan(16.95, 4.49, fee_pct=fee_j, settings=s)
    assert plan["recommended"] is True
    assert plan["tiers"] and all(t["db_extra_eur"] >= 3.0 for t in plan["tiers"])
    assert all(t["margin_pct"] >= 0.20 for t in plan["tiers"])

    # Dünne Marge: EK 22 / VK 39,95, Gebühr 22% -> Ist-Marge < 25% -> NICHT empfohlen.
    fee_a = pricing.effective_fee_pct("Auto & Motorrad", settings=s)   # 0.22
    thin = pricing.volume_pricing_plan(39.95, 22.0, fee_pct=fee_a, settings=s)
    assert thin["recommended"] is False
    assert thin["tiers"] == []          # keine lohnende Staffel angeboten


# ------------------------------- Bündel-Kosten (#5 Teil 2) -------------------------------
def test_effective_cost_bundle_shipping_charged_once():
    from app.config import Settings
    from app.services import pricing
    s = Settings(aliexpress_free_shipping_threshold=10.0, aliexpress_shipping_fee_eur=1.99,
                 customs_fee_eur=0.0, shipping_cost_eur=0.0, cny_to_eur_rate=1.0,
                 aliexpress_tax_pct=0.0, use_mocks=True)   # Steuer isoliert (eigener Test unten)
    assert pricing.effective_cost(4.0, settings=s) == 5.99                    # 1 Stk unter 10 -> +1.99
    assert pricing.effective_cost_bundle(4.0, quantity=2, settings=s) == 9.99  # 8<10 -> Versand EINMAL
    assert pricing.effective_cost_bundle(6.0, quantity=2, settings=s) == 12.0  # 12>=10 -> KEIN Versand
    assert pricing.marginal_unit_cost(6.0, at_qty=2, settings=s) == 4.01       # 12 - 7.99


def test_aliexpress_tax_uplift_on_product_plus_shipping():
    """Versteckte DS-Steuer/Gebuehr: Aufschlag auf (Produkt+Versand) — ohne ihn kalkulierte
    der Upload-Preis systematisch zu knapp (Vorfall #1142: geschaetzt 8,88 €, real 11,72 €)."""
    from app.config import Settings
    from app.services import pricing
    s = Settings(aliexpress_free_shipping_threshold=10.0, aliexpress_shipping_fee_eur=1.99,
                 customs_fee_eur=0.0, shipping_cost_eur=0.0, cny_to_eur_rate=1.0,
                 aliexpress_tax_pct=0.32, use_mocks=True)
    # (4,00 + 1,99) * 1,32 = 7,91 — Steuer trifft Produkt UND Versand
    assert pricing.effective_cost(4.0, settings=s) == 7.91
    # der reale #1142-Fall: (6,89 + 1,99) * 1,32 = 11,72 (exakt die echte Belastung)
    assert pricing.effective_cost(6.89, settings=s, ship_override=1.99) == 11.72
    # Bundle: Steuer auf die ganze Sendung (Produkt*q + Versand einmal)
    assert pricing.effective_cost_bundle(4.0, quantity=2, settings=s) == round(9.99 * 1.32, 2)


# ------------------- Echte Lieferanten-Versandkosten (freight query) -------------------
def test_ship_override_replaces_flat_estimate():
    """ship_override = echte AliExpress-Versandkosten ersetzt die 1,99-€-Pauschale komplett:
    teurerer Versand hebt den EK, Gratisversand senkt ihn – unabhaengig von der 10-€-Schwelle."""
    from app.config import Settings
    from app.services import pricing
    s = Settings(aliexpress_free_shipping_threshold=10.0, aliexpress_shipping_fee_eur=1.99,
                 customs_fee_eur=0.0, shipping_cost_eur=0.0, cny_to_eur_rate=1.0,
                 aliexpress_tax_pct=0.0, use_mocks=True)   # Steuer isoliert (eigener Test oben)
    # unter der Schwelle: Pauschale 1,99 -> echter 3,29 hebt, echter 0,00 senkt
    assert pricing.effective_cost(4.0, settings=s) == 5.99                          # Pauschale
    assert pricing.effective_cost(4.0, settings=s, ship_override=3.29) == 7.29      # echt teurer
    assert pricing.effective_cost(4.0, settings=s, ship_override=0.0) == 4.0        # Gratisversand
    # UEBER der Schwelle nimmt die Pauschale 0 an – ein echter Versand > 0 deckt den blinden Fleck auf
    assert pricing.effective_cost(20.0, settings=s) == 20.0                         # Pauschale: „gratis"
    assert pricing.effective_cost(20.0, settings=s, ship_override=3.29) == 23.29    # echt: doch 3,29
    # Bundle: echter Versand faellt nur EINMAL je Sendung an
    assert pricing.effective_cost_bundle(4.0, quantity=3, settings=s, ship_override=3.29) == 15.29
    # Upload-Preis: echter (teurerer) Versand hebt EK und damit den VK-Boden
    be = pricing.upload_breakdown_from_cny(5.0, settings=s)
    br = pricing.upload_breakdown_from_cny(5.0, settings=s, ship_override=3.29)
    assert br.cost_eur > be.cost_eur and br.rounded_price_eur >= be.rounded_price_eur


def test_volume_plan_extra_unit_cheaper_with_bundle():
    from app.config import Settings
    from app.services import pricing
    s = Settings(ebay_ad_rate_pct=0.10, ebay_fixed_fee_eur=0.45, price_cents=0.95,
                 multibuy_min_margin_pct=0.25, multibuy_max_discount_pct=0.10,
                 multibuy_min_extra_profit_eur=3.0, lowering_min_margin_pct=0.20,
                 aliexpress_free_shipping_threshold=10.0, aliexpress_shipping_fee_eur=1.99,
                 cny_to_eur_rate=1.0, use_mocks=True)
    fee = pricing.effective_fee_pct("Uhren & Schmuck", settings=s)
    ek1 = pricing.effective_cost(6.0, settings=s)                 # 7.99
    ekx = pricing.marginal_unit_cost(6.0, at_qty=2, settings=s)   # 4.01 (Versand-Trick)
    with_trick = pricing.volume_pricing_plan(16.95, ek1, fee_pct=fee, ek_extra=ekx, settings=s)
    flat = pricing.volume_pricing_plan(16.95, ek1, fee_pct=fee, settings=s)
    assert with_trick["all_tiers"][0]["db_extra_eur"] > flat["all_tiers"][0]["db_extra_eur"]


# ------------------------- Upload-Preis: 25% Marge ODER min 4€ (09.07.) -------------------------
def test_upload_price_enforces_min_profit_floor():
    """Sehr guenstige Artikel: 25% Marge brächte < 4€ effektiv -> Preis wird angehoben,
    bis mindestens upload_min_profit_eur Gewinn bleibt (statt starrer 8€-Grenze)."""
    from app.config import Settings
    from app.services import pricing
    s = Settings(ebay_fee_pct=0.22, ebay_ad_rate_pct=0.10, ebay_fixed_fee_eur=0.45,
                 price_cents=0.95, upload_margin_pct=0.25, upload_min_profit_eur=4.0,
                 aliexpress_free_shipping_threshold=10.0, aliexpress_shipping_fee_eur=1.99,
                 customs_fee_eur=0.0, shipping_cost_eur=0.0, cny_to_eur_rate=1.0, use_mocks=True)
    b = pricing.upload_breakdown_from_cny(3.0, settings=s)   # EK effektiv 4,99 (3+1,99 Versand)
    assert b.profit_eur >= 4.0                               # 4€-Boden greift
    assert b.margin_pct >= 0.25                              # 25%-Boden bleibt ebenfalls gewahrt
    # Ohne den 4€-Boden wäre der Preis niedriger (reiner 25%-Marge-Preis):
    margin_only = pricing.round_up_to_cents(
        pricing.price_floor(4.99, min_margin_pct=0.25, settings=s), 0.95)
    assert b.rounded_price_eur > margin_only

    # Teurer Artikel: 25% Marge bringt schon deutlich > 4€ -> Marge-Boden dominiert (~25%).
    b2 = pricing.upload_breakdown_from_cny(40.0, settings=s)
    assert b2.profit_eur > 4.0
    assert 0.24 <= b2.margin_pct <= 0.30


def test_volume_discount_applies_to_order_total_and_fees_on_discounted():
    """Nutzer-Modell (09.07.): der Rabatt gilt auf den UMSATZ, die Gebühren auf den
    RABATTIERTEN Umsatz. Produkt ~10€, 3 Stück -> ~27€ (10% Rabatt), ~20% Gebühr auf die
    27€ -> ~21€ bleiben (vor EK). Das per-Stück-Modell muss genau das abbilden."""
    from app.config import Settings
    from app.services import pricing
    s = Settings(ebay_ad_rate_pct=0.10, ebay_fixed_fee_eur=0.45, price_cents=0.95,
                 multibuy_min_margin_pct=0.0, multibuy_max_discount_pct=0.10,
                 multibuy_min_extra_profit_eur=0.0, lowering_min_margin_pct=0.0, use_mocks=True)
    fee = 0.20
    plan = pricing.volume_pricing_plan(9.95, 3.0, fee_pct=fee, ek_extra=3.0, settings=s)
    t3 = next(t for t in plan["all_tiers"] if t["qty"] == 3)
    assert t3["discount_pct"] == 10.0
    assert t3["price_eur"] == 8.95                       # 10% auf 9,95 -> 8,95 (rabattierter Stückpreis)
    # Umsatz 3 Stück nach Gebühr (Gebühr auf den RABATTIERTEN Umsatz, nicht auf 30€):
    revenue_after_fee = round(3 * t3["price_eur"] * (1 - fee), 2)
    assert 21.0 <= revenue_after_fee <= 21.6              # ~21€ wie vom Nutzer gerechnet
    # ECHTER marginaler Zusatzgewinn: die db_extra je Staffel MÜSSEN sich (mit dem 1-Stück-Gewinn)
    # zum echten Gesamtgewinn bei 3 Stück aufaddieren (alle 3 zum 10%-Staffelpreis, Fix einmal,
    # ab 2. Stück marginale EK) — beweist, dass der tiefere Rabatt auf ALLE Stücke berücksichtigt ist.
    t2 = next(t for t in plan["all_tiers"] if t["qty"] == 2)
    real_total_3 = round(3 * t3["price_eur"] * (1 - fee) - pricing.ebay_fixed_fee(s) - (3.0 + 2 * 3.0), 2)
    cumul_3 = round(plan["profit_1_eur"] + t2["db_extra_eur"] + t3["db_extra_eur"], 2)
    assert cumul_3 == real_total_3
    # und der echte marginale 3.-Stück-Gewinn ist KLEINER als das alte Isoliert-Modell (Preis·(1-fee)-EK):
    assert t3["db_extra_eur"] < round(t3["price_eur"] * (1 - fee) - 3.0, 2)


def test_volume_marginal_profit_is_real_not_isolated():
    """Nutzer-Ziel (16.07.): der Zusatzgewinn je Stück ist der ECHTE marginale Gewinn — eBay
    rabattiert ALLE Stück der Bestellung, der tiefere Rabatt frisst auch die früheren Stücke.
    db_extra = Gewinn(N) − Gewinn(N−1), NICHT das isoliert gerechnete Zusatzstück."""
    from app.config import Settings
    from app.services import pricing
    s = Settings(ebay_ad_rate_pct=0.10, ebay_fixed_fee_eur=0.45, price_cents=0.95,
                 multibuy_min_margin_pct=0.0, multibuy_max_discount_pct=0.10,
                 multibuy_min_extra_profit_eur=0.0, lowering_min_margin_pct=0.0, use_mocks=True)
    fee = 0.309
    plan = pricing.volume_pricing_plan(24.95, 12.86, fee_pct=fee, ek_extra=6.00, settings=s)
    p1 = plan["profit_1_eur"]
    t2, t3 = plan["all_tiers"][0], plan["all_tiers"][1]
    fix = pricing.ebay_fixed_fee(s)
    prof2 = round(2 * t2["price_eur"] * (1 - fee) - fix - (12.86 + 6.00), 2)        # alle 2 zum 5%-Preis
    prof3 = round(3 * t3["price_eur"] * (1 - fee) - fix - (12.86 + 2 * 6.00), 2)    # alle 3 zum 10%-Preis
    assert t2["db_extra_eur"] == round(prof2 - p1, 2)      # echter marginaler 2.-Stück-Gewinn
    assert t3["db_extra_eur"] == round(prof3 - prof2, 2)   # echter marginaler 3.-Stück-Gewinn
    assert p1 < prof2 < prof3                              # Gewinn steigt real bei jedem Stück
    # der echte 3.-Stück-Gewinn ist strikt kleiner als das alte Isoliert-Modell (zu optimistisch):
    assert t3["db_extra_eur"] < round(t3["price_eur"] * (1 - fee) - 6.00, 2)


def test_marge_duenn_nur_wenn_beide_schwellen_verfehlt(db):
    """P6/#5: 'Marge dünn' (needs_raise) nur, wenn WEDER die 20%-Zielmarge NOCH der 4€-
    Mindestgewinn erreicht wird. Eine Variante mit >=4€ Gewinn bei <20% Marge ist NICHT dünn."""
    from decimal import Decimal
    from sqlalchemy.orm.attributes import flag_modified
    from app.config import get_settings
    from app.models import Listing, Product
    from app.services import listing_match_service as lm, pricing
    s = get_settings()
    p = Product(aliexpress_url="https://de.aliexpress.com/item/thin.html", aliexpress_id="thin",
                price_cny=Decimal("12"),
                variants={"axes": {"Farbe": ["A", "B"]},
                          "skus": [{"attr": "a0", "options": {"Farbe": "A"}, "price": 12.0, "stock": 9},
                                   {"attr": "a1", "options": {"Farbe": "B"}, "price": 12.0, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-thin", ebay_item_id="E-thin", title_seo="T",
                description="d", listing_status="active", price_eur=Decimal("30"))
    db.add(l); db.flush()
    fee = pricing.effective_fee_pct_for_listing(l, settings=s)
    fix = pricing.ebay_fixed_fee(s)
    ek = round(pricing.effective_cost(12.0, settings=s), 2)
    # VK so pinnen, dass a0 ~5€ Gewinn (Marge<20%) und a1 ~3€ Gewinn (Marge<20%) hat.
    l.variant_prices = {"a0": round((5.0 + fix + ek) / (1 - fee), 2),
                        "a1": round((3.0 + fix + ek) / (1 - fee), 2)}
    flag_modified(l, "variant_prices"); db.commit()

    rows = {r["attr"]: r for r in lm._variant_rows(l, p, s)}
    a0, a1 = rows["a0"], rows["a1"]
    # Kontrolle: beide unter Zielmarge, a0 >= 4€ Gewinn, a1 < 4€ Gewinn
    assert a0["margin"] < s.target_margin_pct and a1["margin"] < s.target_margin_pct
    assert a0["profit_eur"] >= s.upload_min_profit_eur and a1["profit_eur"] < s.upload_min_profit_eur
    # Kern: a0 trotz <20% NICHT dünn (>=4€), a1 dünn (weder 20% noch 4€)
    assert a0["needs_raise"] is False
    assert a1["needs_raise"] is True


def test_whole_listing_self_stock_row_and_fulfillment(db):
    """P7-3: ein Listing OHNE Quelle/Varianten, als Ganz-Listing-Eigenbestand markiert
    ("__listing__"), erscheint mit eigenem EK/Preis (raus aus 'Quelle prüfen'), und der
    Fulfillment-Guard versendet selbst (kein AliExpress-Kauf) egal welche Variante verkauft wird."""
    from decimal import Decimal
    from app.models import Listing
    from app.services import listing_match_service as lm, order_service as osvc
    l = Listing(ebay_sku="AE-wls", ebay_item_id="E-wls", title_seo="Selbst gelagert",
                description="d", listing_status="active", price_eur=Decimal("19.95"),
                self_stock={"__listing__": {"qty": 4, "cost_eur": 3.0, "price_eur": 19.95}})
    db.add(l); db.commit()
    lm.rebuild_reprice_report(db)
    data = lm.reprice_report(db)
    row = next(r for r in data["rows"] if r["listing_id"] == l.id)
    assert row.get("self_stock_listing") is True and row["self_qty"] == 4
    assert row["current_price_eur"] == 19.95 and row["cost_eff_eur"] == 3.0
    assert row["status"] == "matched" and row["profit_now_eur"] is not None
    assert row["monitor_oos"] is False  # nicht als "Quelle weg" fehlmarkiert

    class _Sale:
        variant_selected = {"Farbe": "irgendwas"}
    assert osvc._resolve_self_stock_attr(_Sale(), l, None) == "__listing__"
    # qty 0 -> nicht mehr selbst versenden
    l.self_stock = {"__listing__": {"qty": 0, "cost_eur": 3.0, "price_eur": 19.95}}
    assert osvc._resolve_self_stock_attr(_Sale(), l, None) is None


# --- Ein Preismodell statt zwei (Nutzerentscheidung 29.08.2026) ---------------
# Im Dashboard stand neben einem korrekt kalkulierten Preis von 18,95 EUR dauerhaft
# ein Vorschlag von 23,95 EUR - 33 % Marge statt 20 %. Ursache: compute_price rechnete
# einen AUFSCHLAG AUF DIE KOSTEN (Kosten*0,20) plus 8 EUR Mindestgewinn, das
# Upload-Modell dagegen eine MARGE VOM VERKAUFSPREIS (20 % von VK) plus 4 EUR. Beide
# hiessen "20 %" und meinten Verschiedenes.

@pytest.mark.parametrize("ware", ["6.99", "9.08", "13.99", "14.49", "17.19", "25.00", "49.00"])
def test_beide_preismodelle_sagen_dasselbe(ware):
    """Der Vorschlag im Dashboard muss zum kalkulierten Preis passen."""
    from decimal import Decimal

    from app.services import pricing

    alt = pricing.price_from_cny(Decimal(ware), local=True)
    neu = pricing.upload_breakdown_from_cny(Decimal(ware), local=True)

    assert alt.rounded_price_eur == neu.rounded_price_eur, (
        f"Ware {ware}: altes Modell {alt.rounded_price_eur}, "
        f"Upload-Modell {neu.rounded_price_eur}")


def test_zielmarge_wird_nie_unterschritten():
    """Der Aufschlag auf die Kosten allein trug die Marge bei teurer Ware nicht."""
    from app.config import get_settings
    from app.services import pricing

    s = get_settings()
    for kosten in (5.0, 20.0, 50.0, 120.0):
        b = pricing.compute_price(kosten)
        assert b.margin_pct >= s.target_margin_pct - 0.001, (
            f"EK {kosten}: nur {b.margin_pct:.3f} Marge")


def test_mindestgewinn_traegt_billige_ware():
    """Bei sehr billiger Ware braechten 20 % nur ein paar Cent - dann greift der Boden."""
    from app.config import get_settings
    from app.services import pricing

    s = get_settings()
    b = pricing.compute_price(3.00)

    assert b.profit_eur >= s.min_profit_eur - 0.01
    assert s.min_profit_eur == 4.0, "Nutzerregel: 20 %, mindestens 4 Euro"


def test_eigener_gebuehrensatz_wird_respektiert():
    """Printify rechnet mit eigener Gebuehr - der Marge-Boden muss denselben Satz nutzen.

    Waere der Boden ueber price_floor gerechnet, haette er seinen eigenen Satz ermittelt
    und dem uebergebenen widersprochen.
    """
    from app.services import pricing

    b = pricing.compute_price(10.0, fee_pct=0.05)
    # Bei nur 5 % Gebuehr traegt derselbe Preis eine hoehere Marge als bei 26 %.
    teuer = pricing.compute_price(10.0, fee_pct=0.30)

    assert b.rounded_price_eur < teuer.rounded_price_eur
    assert b.margin_pct >= 0.199


@pytest.mark.parametrize("ek", [3.0, 6.99, 9.08, 17.19, 25.0, 50.0, 100.0])
def test_alle_rechenwege_folgen_lesart_b(ek):
    """Bestaetigt vom Nutzer 29.08.2026: "muesste alles nach lesart B laufen".

    Lesart A war "20 % auf den Einkauf draufschlagen" - davon bleibt nach eBays
    26 % Gebuehr nichts uebrig, bei 9,08 EUR Ware sogar ein Verlust von 1,57 EUR.
    Lesart B ist "20 % vom Verkaufspreis muessen uebrig bleiben", mindestens aber
    4 EUR Gewinn.

    Geprueft werden ALLE drei Wege: der Kalkulator (compute_price), der bequeme
    Einstieg (price_from_cny) und der Upload-Weg. Sie muessen sich einig sein -
    genau das war vorher nicht so, und im Dashboard stand deshalb neben einem
    richtigen Preis dauerhaft ein widersprechender Vorschlag.
    """
    from decimal import Decimal

    from app.config import get_settings
    from app.services import pricing

    s = get_settings()
    wege = {
        "compute_price": pricing.compute_price(ek),
        "price_from_cny": pricing.price_from_cny(Decimal(str(ek)), local=True),
        "upload_breakdown": pricing.upload_breakdown_from_cny(Decimal(str(ek)), local=True),
    }

    preise = {n: b.rounded_price_eur for n, b in wege.items()}
    assert len(set(preise.values())) == 1, f"Die Wege sind sich uneinig: {preise}"

    for name, b in wege.items():
        assert b.margin_pct >= s.target_margin_pct - 0.002, (
            f"{name} bei EK {ek}: nur {b.margin_pct*100:.1f} % Marge")
        assert b.profit_eur >= s.min_profit_eur - 0.02, (
            f"{name} bei EK {ek}: nur {b.profit_eur:.2f} EUR Gewinn")


def test_lesart_a_waere_ein_verlustgeschaeft():
    """Warum die Umstellung noetig war - der Gegenbeweis in Zahlen."""
    from app.services import pricing

    ek = 9.08
    lesart_a = round(ek * 1.20, 2)          # "20 % auf den Einkauf"
    gewinn_a = pricing.profit_at_price(lesart_a, ek)

    assert gewinn_a < 0, (
        f"Lesart A ergaebe {lesart_a} EUR und damit {gewinn_a} EUR - Verlust")
    assert pricing.compute_price(ek).rounded_price_eur > lesart_a
