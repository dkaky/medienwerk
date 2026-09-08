"""Datenbasierte Gebuehrenquote (Netto-Fix Orders) + Best-Guess-Variantenvorwahl."""
from __future__ import annotations

from decimal import Decimal

from app.models import Listing, Product, Sale
from app.services import analytics_service as an
from app.services import supplier_service as sup


# ---------------- empirische Gebuehrenquote je Kategorie ----------------
def _mk_sale(db, *, tid, vk, fee, cat):
    l = Listing(ebay_sku=f"AE-{tid}", title_seo="T", description="d",
                listing_status="active", category_name=cat, price_eur=Decimal(str(vk)))
    db.add(l); db.flush()
    s = Sale(ebay_transaction_id=tid, listing_id=l.id, status="delivered",
             price_eur=Decimal(str(vk)),
             fee_eur_actual=(Decimal(str(fee)) if fee is not None else None))
    db.add(s); db.commit()
    return s, l


def test_empirical_fee_rates_uses_real_median(db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "ebay_fixed_fee_eur", 0.45)
    # 6 Schmuck-Verkaeufe mit echter Gebuehr ~31 % + 0,45 fix
    for i in range(6):
        _mk_sale(db, tid=f"j{i}", vk=20.0, fee=0.45 + 20.0 * 0.31,
                 cat="Uhren & Schmuck:Modeschmuck:Ringe")
    # zu wenige Daten in einer anderen Kategorie -> nicht aufgenommen
    _mk_sale(db, tid="a1", vk=30.0, fee=0.45 + 30.0 * 0.10, cat="Auto & Motorrad:Teile")
    rates = an.empirical_fee_rates(db)
    assert abs(rates["Uhren & Schmuck"] - 0.31) < 0.005
    assert "Auto & Motorrad" not in rates          # < 5 Datenpunkte -> kein Eintrag


def test_order_economics_estimate_uses_empirical_rate(db, monkeypatch):
    """Sale OHNE echte Gebuehr: Schaetzung nutzt die empirische Kategorie-Quote (31 %),
    nicht die zu niedrige Pauschale (26 %) -> Netto NICHT zu hoch (User-Fund 1151-1153)."""
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "ebay_fixed_fee_eur", 0.45)
    for i in range(5):
        _mk_sale(db, tid=f"h{i}", vk=20.0, fee=0.45 + 20.0 * 0.31,
                 cat="Uhren & Schmuck:Modeschmuck:Ringe")
    s_new, l_new = _mk_sale(db, tid="new", vk=19.95, fee=None,
                            cat="Uhren & Schmuck:Modeschmuck:Ringe")
    rates = an.empirical_fee_rates(db)
    eco = an._order_economics(s_new, None, l_new, None, fee_pct=0.32, fixed_fee=0.45,
                              fee_rate_by_cat=rates)
    # 19,95 * 0,31 + 0,45 = 6,63  (nicht 19,95*0,26+0,45 = 5,64)
    assert abs(eco["fee"] - (19.95 * rates["Uhren & Schmuck"] + 0.45)) < 0.02
    assert eco["fee"] > 6.0            # deutlich hoeher als die alte Pauschal-Schaetzung
    assert eco["fee_is_actual"] is False


def test_order_economics_prefers_actual_fee(db):
    s, l = _mk_sale(db, tid="act", vk=20.0, fee=7.10, cat="Uhren & Schmuck:Ringe")
    eco = an._order_economics(s, None, l, None, fee_pct=0.32, fixed_fee=0.45,
                              fee_rate_by_cat={"Uhren & Schmuck": 0.31})
    assert eco["fee"] == 7.10 and eco["fee_is_actual"] is True


# ---------------- Best-Guess Variantenvorwahl (Ausweichquellen) ----------------
def test_best_guess_variant_idx_value_overlap():
    skus = [{"attr": "a", "options": {"Color": "Silver 45cm"}},
            {"attr": "b", "options": {"Color": "Gold 65cm"}},
            {"attr": "c", "options": {"Color": "Silver 65cm"}}]
    # eBay-Auswahl "65 cm" -> beste Ueberlappung (65 + cm) hat idx 1 oder 2; 'silver' zusaetzlich -> 2
    assert sup.best_guess_variant_idx({"Farbe": "Silber 65 cm"}, skus) == 2


def test_best_guess_variant_idx_weiss_eszett():
    # 'weiß' (haeufigste dt. Schreibweise) muss auf englische Alt-Quelle 'White' mappen.
    # Regression: der Tokenizer schnitt frueher am ß ab ({'wei'}) -> Synonym war tot.
    skus = [{"attr": "a", "options": {"Color": "Black"}},
            {"attr": "b", "options": {"Color": "White"}}]
    assert sup.best_guess_variant_idx({"Farbe": "weiß"}, skus) == 1


def test_best_guess_variant_idx_defaults_to_first_when_no_overlap():
    """Nutzerwunsch: bei JEDEM Sale eine Variante vorwählen. Keine Überlappung / keine Auswahl
    -> erste Variante als Default (der Nutzer gegencheckt), NICHT None. Nur ohne Kandidaten None."""
    skus = [{"attr": "a", "options": {"Color": "rot"}}, {"attr": "b", "options": {"Color": "blau"}}]
    assert sup.best_guess_variant_idx({"Farbe": "lila"}, skus) == 0
    assert sup.best_guess_variant_idx(None, skus) == 0
    assert sup.best_guess_variant_idx({}, skus) == 0
    assert sup.best_guess_variant_idx({"Farbe": "lila"}, []) is None   # keine Kandidaten


def test_best_guess_variant_idx_tie_picks_lowest_index():
    """Gleichstand (gleiche Überlappung) -> niedrigster Index statt None (immer vorwählen)."""
    skus = [{"attr": "a", "options": {"Color": "rot L"}}, {"attr": "b", "options": {"Color": "rot L"}}]
    assert sup.best_guess_variant_idx({"Farbe": "rot L"}, skus) == 0


def test_best_guess_variant_confidence():
    """P7-Review-Fix: idx immer gesetzt (Vorwahl), aber confident nur bei EINDEUTIGER Überlappung.
    Unsichere Vorwahl -> UI verlangt bestätigenden Klick (kein Auto-Order der falschen Variante)."""
    skus = [{"attr": "a", "options": {"C": "Silver 45cm"}}, {"attr": "b", "options": {"C": "Silver 65cm"}}]
    g = sup.best_guess_variant({"Farbe": "Silber 65 cm"}, skus)
    assert g["idx"] == 1 and g["confident"] is True            # klare, eindeutige Überlappung
    g0 = sup.best_guess_variant({"Farbe": "lila"}, skus)
    assert g0["idx"] == 0 and g0["confident"] is False          # keine Überlappung -> Default, unsicher
    tie = sup.best_guess_variant({"C": "Silver"},
                                 [{"attr": "a", "options": {"C": "Silver A"}},
                                  {"attr": "b", "options": {"C": "Silver B"}}])
    assert tie["idx"] == 0 and tie["confident"] is False        # Gleichstand -> vorgewählt, aber unsicher
    assert sup.best_guess_variant(None, [])["idx"] is None      # keine Kandidaten


def test_deterministic_variant_from_publish_sku(db):
    """KERN-Fix: unsere Publish-SKU 'AE-{id}-V{i}' löst die AliExpress-Variante EINDEUTIG auf
    (Position i), ohne Raten — wir haben das Listing selbst veröffentlicht. resolve_selection_
    against_skus nimmt den so gebackenen attr direkt (Hauptquelle), fällt sonst sauber zurück."""
    from decimal import Decimal
    from app.services import order_service as osvc
    p = Product(aliexpress_url="https://de.aliexpress.com/item/rng.html", aliexpress_id="rng",
                variants={"axes": {"Farbe": ["Rot", "Blau"]},
                          "skus": [{"attr": "14:1", "options": {"Farbe": "Rot"}, "id": 1, "price": 5, "stock": 9},
                                   {"attr": "14:2", "options": {"Farbe": "Blau"}, "id": 2, "price": 5, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-99", ebay_item_id="E99", title_seo="Ring",
                description="d", listing_status="active", price_eur=Decimal("14.95"))
    db.add(l); db.commit()
    assert osvc._attr_from_ebay_variation_sku(l, p, "AE-99-V2")["attr"] == "14:2"
    assert osvc._attr_from_ebay_variation_sku(l, p, "AE-99-V1")["attr"] == "14:1"
    assert osvc._attr_from_ebay_variation_sku(l, p, "FREMD-SKU") is None      # anderes Schema -> Fallback
    skus = p.variants["skus"]
    # gebackener attr existiert unveraendert in der (Haupt-)Quelle (Options-Snapshot passt) -> direkt
    assert osvc.resolve_selection_against_skus(
        {"Farbe": "Blau", "attr": "14:2", "options": {"Farbe": "Blau"}}, skus,
        listing=l, source_id="rng")["attr"] == "14:2"
    # gebackener attr NICHT in dieser (Ausweich-)Quelle -> Fallback auf Score-Matching (Rot -> 14:1)
    assert osvc.resolve_selection_against_skus({"Farbe": "Rot", "attr": "99:99"}, skus,
                                               listing=l, source_id="alt")["attr"] == "14:1"


def test_variant_consistent_with_buyer():
    """Konsistenzpruefung POSITIV: der Kandidat gilt nur, wenn ein Kaeuferwert ihn TRAEGT und kein
    anderer SKU besser passt. Fehlt/verschwindet das Vokabular (Score 0), NICHT trauen (Umstellung)."""
    from app.services import order_service as osvc
    skus = [{"attr": "14:1", "options": {"Farbe": "Rot"}},
            {"attr": "14:2", "options": {"Farbe": "Blau"}}]
    assert osvc._variant_consistent_with_buyer({"Farbe": "Rot"}, skus, "14:1") is True
    assert osvc._variant_consistent_with_buyer({"Farbe": "Rot"}, skus, "14:2") is False   # Blau widerlegt
    assert osvc._variant_consistent_with_buyer({"Farbe": "egal"}, skus, "14:2") is False   # 0-Signal -> NICHT trauen
    assert osvc._variant_consistent_with_buyer({}, skus, "14:2") is False                  # keine Werte -> NICHT trauen
    assert osvc._variant_consistent_with_buyer({"Farbe": "Rot"}, skus, "99:99") is False   # attr fehlt


def test_deterministic_variant_rejects_reordered_product(db):
    """GELD-SCHUTZ (Review-CRIT-1): wird product.variants nach dem Verkauf umgestellt (Hauptquellen-
    Swap / geloeschte Variante), zeigt Position V{i} auf FALSCHE Ware. Die Kaeuferwerte widerlegen
    den positionell decodierten Kandidaten -> KEIN Baken (Fallback statt falsche Bestellung)."""
    from decimal import Decimal
    from app.services import order_service as osvc
    # Publish-Reihenfolge WAR [Rot, Blau] -> V1=Rot. Jetzt umgestellt auf [Blau, Rot]:
    p = Product(aliexpress_url="https://de.aliexpress.com/item/x.html", aliexpress_id="x",
                variants={"axes": {"Farbe": ["Blau", "Rot"]},
                          "skus": [{"attr": "14:2", "options": {"Farbe": "Blau"}, "id": 2, "price": 5, "stock": 9},
                                   {"attr": "14:1", "options": {"Farbe": "Rot"}, "id": 1, "price": 5, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-77", ebay_item_id="E77", title_seo="Ring",
                description="d", listing_status="active", price_eur=Decimal("14.95"))
    db.add(l); db.commit()
    # Kaeufer kaufte "Rot" (V1 im alten Publish); Position 1 der UMGESTELLTEN Liste ist Blau
    # -> Widerspruch -> None (nicht die falsche blaue Variante baken).
    assert osvc._attr_from_ebay_variation_sku(l, p, "AE-77-V1", {"Farbe": "Rot"}) is None
    # Ohne Kaeuferwerte (None = kein Kaeuferkontext, Rueckwaerts-Kompat) reine Positions-Decodierung.
    assert osvc._attr_from_ebay_variation_sku(l, p, "AE-77-V1")["attr"] == "14:2"


def test_deterministic_variant_position_vs_contradiction(db):
    """Policy (Nutzer 15.07.): fuer UNSER Listing gilt die Publish-Position. Sie wird NUR verworfen,
    wenn die Kaeuferwerte eine ANDERE Variante STRIKT besser treffen (verschobene/getauschte
    Variante). Fehlt jedes Wert-Signal (nichtssagende Namen, #1161), gilt die Position."""
    from decimal import Decimal
    from app.services import order_service as osvc
    p = Product(aliexpress_url="https://de.aliexpress.com/item/d.html", aliexpress_id="d",
                variants={"axes": {"Farbe": ["Rot", "Gruen"]},
                          "skus": [{"attr": "14:1", "options": {"Farbe": "Rot"}, "id": 1, "price": 5, "stock": 9},
                                   {"attr": "14:3", "options": {"Farbe": "Gruen"}, "id": 3, "price": 5, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-88", ebay_item_id="E88", title_seo="Ring",
                description="d", listing_status="active", price_eur=Decimal("14.95"))
    db.add(l); db.commit()
    # (a) "Blau" trifft weder Rot noch Gruen -> kein Signal -> Position V1 (Rot) gilt
    assert osvc._attr_from_ebay_variation_sku(l, p, "AE-88-V1", {"Farbe": "Blau"})["attr"] == "14:1"
    # (b) "Gruen" trifft V2(Gruen) STRIKT besser als V1(Rot) -> Position V1 verworfen (None)
    assert osvc._attr_from_ebay_variation_sku(l, p, "AE-88-V1", {"Farbe": "Gruen"}) is None


def test_deterministic_breaks_value_tie(db):
    """NUTZERWUNSCH: der Kaeufer waehlte nur 'Silber' (Achse vergroebert), zwei AliExpress-Varianten
    teilen 'Silber'. Der Wert TRAEGT beide gleich (Gleichstand) -> die Publish-Position loest
    deterministisch auf (das ist genau der gewuenschte Auto-Vorwahl-Effekt)."""
    from decimal import Decimal
    from app.services import order_service as osvc
    p = Product(aliexpress_url="https://de.aliexpress.com/item/s.html", aliexpress_id="s",
                variants={"axes": {"Farbe": ["Silber 45cm", "Silber 65cm"]},
                          "skus": [{"attr": "s45", "options": {"Farbe": "Silber 45cm"}, "id": 1, "price": 5, "stock": 9},
                                   {"attr": "s65", "options": {"Farbe": "Silber 65cm"}, "id": 2, "price": 5, "stock": 9}]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-55", ebay_item_id="E55", title_seo="Kette",
                description="d", listing_status="active", price_eur=Decimal("14.95"))
    db.add(l); db.commit()
    # V2 == zweite Variante == Silber 65cm; "Silber" traegt beide -> Gleichstand -> Position entscheidet.
    assert osvc._attr_from_ebay_variation_sku(l, p, "AE-55-V2", {"Farbe": "Silber"})["attr"] == "s65"


def test_resolve_alt_source_attr_collision_falls_back(db):
    """GELD-SCHUTZ (Review-CRIT-2): der aus der Hauptquelle gebackene attr '14:2' (Snapshot Blau)
    existiert ZUFAELLIG auch in einer Ausweichquelle (gemeinsame attr-Taxonomie), meint dort aber
    GRUEN. Der Options-Snapshot weicht ab -> kein Direkt-Treffer -> Score-Matching auf die echte
    blaue Ware (77:1)."""
    from app.services import order_service as osvc
    alt_skus = [{"attr": "14:2", "options": {"Farbe": "Gruen"}, "id": 8},
                {"attr": "77:1", "options": {"Farbe": "Blau"}, "id": 9}]
    hit = osvc.resolve_selection_against_skus(
        {"Farbe": "Blau", "attr": "14:2", "options": {"Farbe": "Blau"}, "id": 2},
        alt_skus, source_id="alt")
    assert hit and hit["attr"] == "77:1"          # NICHT das kollidierende gruene 14:2


def test_resolve_variant_rejects_mutated_attr():
    """Fulfill-Pfad: ein nach dem Import umgestelltes Produkt haengt denselben attr an ANDERE Ware.
    Der Options-Snapshot (Blau) passt nicht mehr zum aktuellen a2 (Gruen) -> nicht blind bestellen;
    intakte Bakes bleiben unangetastet."""
    from app.models import Product
    from app.services import order_service as osvc
    # a2 traegt jetzt 'Gruen' (Umstellung); der Bake-Snapshot war 'Blau'.
    p = Product(aliexpress_url="u", aliexpress_id="z",
                variants={"skus": [{"attr": "a1", "options": {"Farbe": "Rot"}, "id": 1},
                                   {"attr": "a2", "options": {"Farbe": "Gruen"}, "id": 2}]})
    stale = {"Farbe": "Blau", "attr": "a2", "options": {"Farbe": "Blau"}, "id": 2}
    got = osvc._resolve_variant(p, stale)
    assert (got or {}).get("attr") != "a2"        # kein Blind-Order der falschen gruenen Ware
    # intakter Bake (Snapshot == aktuelle Optionswerte) bleibt so:
    ok = {"Farbe": "Gruen", "attr": "a2", "options": {"Farbe": "Gruen"}, "id": 2}
    assert osvc._resolve_variant(p, ok)["attr"] == "a2"
