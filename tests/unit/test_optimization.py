"""Listing-Optimierung: PROPOSE-ONLY (KI schlaegt Titel vor, Freigabe ist manuell).

Regression zum Vorfall 07/2026: der Optimierer ueberschrieb 115 echte Titel mit
KI-Chat-Text ("Ich bin bereit, einen Titel zu erstellen ...") und haette Listings
automatisch beendet. Diese Tests fixieren: (1) Chat-Text wird nie als Titel akzeptiert,
(2) es wird nichts automatisch angewandt/beendet, (3) Freigabe/Verwerfen funktioniert.
"""
from __future__ import annotations

from decimal import Decimal

from app.integrations.ebay import EbayAnalytics
from app.models import Listing
from app.services import optimization_service as opt


# ------------------------------- Titel-Validierung -------------------------------
def test_clean_suggested_title_accepts_clean():
    t = opt._clean_suggested_title("Bobby Car Kinderauto Rutschauto Premium Blitzversand", "Alt")
    assert t == "Bobby Car Kinderauto Rutschauto Premium Blitzversand"


def test_clean_suggested_title_rejects_chat_and_junk():
    current = "iPhone Huelle"
    # KI-Geschwaetz (genau der Vorfall)
    assert opt._clean_suggested_title("Ich bin bereit, einen optimierten Titel zu erstellen", current) is None
    assert opt._clean_suggested_title("Ich kann die Analyse nicht durchfuehren, da ...", current) is None
    assert opt._clean_suggested_title("Hier ist dein neuer Titel: Super Huelle", current) is None
    # mehrzeilig / leer / zu lang / Satzende
    assert opt._clean_suggested_title("Zeile eins\nZeile zwei", current) is None
    assert opt._clean_suggested_title("   ", current) is None
    assert opt._clean_suggested_title("x" * 90, current) is None
    assert opt._clean_suggested_title("Guter Titel aber mit Punkt.", current) is None
    # identisch zum aktuellen -> kein Vorschlag
    assert opt._clean_suggested_title("iPhone Huelle", "iPhone Huelle") is None


# ------------------------------- Fake-eBay fuer Analytics/Titel -------------------------------
class _Report(dict):
    """Sammel-Report-Ersatz: liefert fuer JEDES Listing die konfigurierten Klicks."""
    def __init__(self, clicks):
        super().__init__()
        self._c = clicks

    def get(self, key, default=None):
        return EbayAnalytics(listing_id=key, impressions=120, clicks=self._c)

    def __bool__(self):
        return True

    def __len__(self):
        return 1


class _FakeEbay:
    def __init__(self, clicks=0):
        self._clicks = clicks
        self.titles: dict[str, str] = {}

    async def get_listing_analytics(self, item_id, *, days=7):
        return EbayAnalytics(listing_id=item_id, impressions=120, clicks=self._clicks)

    async def get_all_listing_analytics(self, item_ids=None, *, days=30):
        return _Report(self._clicks)

    async def revise_item_title(self, item_id, title):
        self.titles[str(item_id)] = title

    async def search_competitor_titles(self, keywords, *, limit=12):
        return [f"{keywords} Konkurrent A", f"{keywords} Konkurrent B"][:limit]

    async def update_inventory(self, item_id, *, title=None, category_id=None,
                               price_eur=None, quantity=None):
        return None


def _active(db, title="Original Produkt Titel", item="900001"):
    l = Listing(title_seo=title, description="d", listing_status="active",
                ebay_item_id=item, price_eur=Decimal("19.90"))
    db.add(l)
    db.commit()
    return l


def _confirmed_product(db):
    """Produkt mit ECHTER (manueller/Nicht-Bildmatch-)Quelle -> source_is_confirmed True.
    Optimierungs-Buckets zeigen NUR verifizierte Produkte (Nutzerregel 09.07.: Quelle
    entfernt = nicht verifiziert = raus). Bucket-Tests brauchen daher eine bestaetigte
    Quelle am Listing, sonst greift der Verifizierungs-Filter."""
    from app.models import Product
    p = Product(aliexpress_url="https://de.aliexpress.com/item/verified.html", title_raw="Quelle")
    db.add(p)
    db.flush()
    return p.id


# ------------------------------- Propose-only Lauf -------------------------------
async def test_weekly_optimization_only_proposes(db, monkeypatch):
    """0-Klick-Listing bekommt einen VORSCHLAG – Titel bleibt unveraendert, nichts live."""
    fake = _FakeEbay(clicks=0)
    monkeypatch.setattr(opt, "_real_ebay", lambda: fake)
    l = _active(db)

    r = await opt.run_weekly_optimization(db, dry_run=False)

    db.refresh(l)
    assert r["suggestions_created"] == 1
    assert l.optimization_suggestion  # Vorschlag gespeichert
    assert l.optimization_status == "needs_review"
    assert l.title_seo == "Original Produkt Titel"   # NICHT angewandt
    assert l.listing_status == "active"              # NICHT beendet
    assert fake.titles == {}                          # kein eBay-Push


async def test_weekly_optimization_uses_market_research(db, monkeypatch):
    """Die Konkurrenz-Titel (Marktrecherche) landen im LLM-Aufruf."""
    fake = _FakeEbay(clicks=0)
    monkeypatch.setattr(opt, "_real_ebay", lambda: fake)
    seen = {}

    class _FakeLLM:
        async def suggest_title(self, *, current_title, competitor_titles, internal_titles=None):
            seen["competitors"] = list(competitor_titles)
            seen["internal"] = list(internal_titles or [])
            return "Neuer Markt-Optimierter Titel Keywords"

    monkeypatch.setattr(opt, "get_llm_client", lambda: _FakeLLM())
    _active(db, title="Bobby Car")

    await opt.run_weekly_optimization(db, dry_run=False)

    # Der Fake-eBay liefert "<kw> Konkurrent A/B" – die müssen beim LLM ankommen.
    assert seen.get("competitors") == ["Bobby Car Konkurrent A", "Bobby Car Konkurrent B"]


async def test_title_suggestion_feeds_internal_bestsellers(db, monkeypatch):
    """Eigene, gut verkaufte Listing-Titel (interne Winning Products) landen als Vorbild im
    LLM-Aufruf – Titel werden an bewiesenen Verkäufern ausgerichtet, nicht willkürlich."""
    fake = _FakeEbay(clicks=0)
    monkeypatch.setattr(opt, "_real_ebay", lambda: fake)
    seen = {}

    class _FakeLLM:
        async def suggest_title(self, *, current_title, competitor_titles, internal_titles=None):
            seen["internal"] = list(internal_titles or [])
            return "Optimierter Titel mit Bewährten Keywords"

    monkeypatch.setattr(opt, "get_llm_client", lambda: _FakeLLM())
    # Bestseller in gleicher Kategorie (viele Verkäufe) + der 0-Klick-Kandidat.
    seller = Listing(title_seo="Auto Waschset Bestseller 9-teilig", description="d",
                     listing_status="active", ebay_item_id="bs1", category_name="Auto & Motorrad",
                     sales_total=42, price_eur=Decimal("24.95"))
    cand = Listing(title_seo="Auto Reinigungsset", description="d", listing_status="active",
                   ebay_item_id="c1", category_name="Auto & Motorrad", sales_total=0,
                   clicks_week=0, price_eur=Decimal("19.95"))
    db.add_all([seller, cand]); db.commit()

    await opt.run_weekly_optimization(db, dry_run=False)

    assert "Auto Waschset Bestseller 9-teilig" in seen.get("internal", [])


async def test_weekly_optimization_skips_listings_with_clicks(db, monkeypatch):
    """Listing mit Klicks ist kein Kandidat -> kein Vorschlag."""
    fake = _FakeEbay(clicks=7)
    monkeypatch.setattr(opt, "_real_ebay", lambda: fake)
    l = _active(db)

    r = await opt.run_weekly_optimization(db, dry_run=False)

    db.refresh(l)
    assert r["suggestions_created"] == 0
    assert l.optimization_suggestion is None


async def test_weekly_dry_run_changes_nothing(db, monkeypatch):
    fake = _FakeEbay(clicks=0)
    monkeypatch.setattr(opt, "_real_ebay", lambda: fake)
    l = _active(db)

    r = await opt.run_weekly_optimization(db, dry_run=True)

    db.refresh(l)
    assert r["status"] == "dry_run"
    assert l.optimization_suggestion is None


# ------------------------------- Freigabe / Verwerfen -------------------------------
async def test_accept_suggestion_applies_title_to_ebay(db, monkeypatch):
    fake = _FakeEbay()
    monkeypatch.setattr(opt, "_real_ebay", lambda: fake)
    l = _active(db, item="900042")
    l.optimization_suggestion = "Neuer Starker eBay Titel mit Keywords"
    l.optimization_status = "needs_review"
    db.commit()

    r = await opt.accept_suggestion(db, listing_id=l.id)

    db.refresh(l)
    assert r["status"] == "optimized"
    assert l.title_seo == "Neuer Starker eBay Titel mit Keywords"
    assert l.optimization_suggestion is None
    assert fake.titles["900042"] == "Neuer Starker eBay Titel mit Keywords"  # auf eBay gepusht


async def test_accept_rejects_broken_suggestion_without_pushing(db, monkeypatch):
    """Ein (wie auch immer) kaputter Vorschlag wird NICHT auf eBay gesetzt."""
    import pytest
    fake = _FakeEbay()
    monkeypatch.setattr(opt, "_real_ebay", lambda: fake)
    l = _active(db)
    l.optimization_suggestion = "Ich bin bereit, einen Titel zu erstellen"
    db.commit()

    with pytest.raises(ValueError):
        await opt.accept_suggestion(db, listing_id=l.id)
    db.refresh(l)
    assert l.title_seo == "Original Produkt Titel"   # unveraendert
    assert fake.titles == {}                          # kein Push
    assert l.optimization_suggestion is None          # verworfen


def test_reject_suggestion_keeps_title(db):
    l = _active(db)
    l.optimization_suggestion = "Irgendein Vorschlag Titel"
    db.commit()

    r = opt.reject_suggestion(db, listing_id=l.id)

    db.refresh(l)
    assert r["status"] == "reviewed"
    assert l.optimization_suggestion is None
    assert l.title_seo == "Original Produkt Titel"


# ------------------------------- Kandidatenliste + Einzel-Vorschlag -------------------------------
def test_list_candidates_only_zero_click_sorted_by_impressions(db):
    a = Listing(title_seo="A viel gesehen", description="d", listing_status="active",
                ebay_item_id="a1", clicks_week=0, impressions_week=200)
    b = Listing(title_seo="B wenig gesehen", description="d", listing_status="active",
                ebay_item_id="b1", clicks_week=0, impressions_week=20)
    c = Listing(title_seo="C hat Klicks", description="d", listing_status="active",
                ebay_item_id="c1", clicks_week=4, impressions_week=99)
    db.add_all([a, b, c]); db.commit()

    r = opt.list_candidates(db)
    ids = [x["title"] for x in r["candidates"]]
    assert "C hat Klicks" not in ids            # hat Klicks -> kein Kandidat
    assert ids == ["A viel gesehen", "B wenig gesehen"]  # meiste Impressions zuerst
    assert isinstance(r["window_days"], int) and r["window_days"] >= 1


async def test_suggest_one_stores_suggestion(db, monkeypatch):
    fake = _FakeEbay(clicks=0)
    monkeypatch.setattr(opt, "_real_ebay", lambda: fake)
    l = _active(db, title="Bobby Car")

    r = await opt.suggest_one(db, listing_id=l.id)

    db.refresh(l)
    assert r["suggestion"]
    assert l.optimization_suggestion == r["suggestion"]
    assert l.optimization_status == "needs_review"
    assert l.title_seo == "Bobby Car"   # nur Vorschlag, Titel unveraendert


# ------------------------------- Kein Auto-Beenden -------------------------------
async def test_apply_stage_three_never_ends_listing(db, monkeypatch):
    """Regression: Stufe 3 darf ein Listing NICHT automatisch beenden."""
    fake = _FakeEbay()
    monkeypatch.setattr(opt, "_real_ebay", lambda: fake)
    l = _active(db)

    await opt.apply_stage(db, listing_id=l.id, stage=3, reason="zero_clicks_21_days")

    db.refresh(l)
    assert l.listing_status == "active"   # NICHT "ended"
    assert l.optimization_stage == 3


# ------------------------------- refresh_click_data: alle Listings, kein 200-Limit -------------------------------
async def test_refresh_click_data_queries_all_ids_and_zeroes_missing(db, monkeypatch):
    """Regression (Optimierungs-Tab 0/0-Bug): refresh muss ALLE aktiven Listings
    GEZIELT per ID abfragen (nicht den auf 200 Zeilen gedeckelten Sammel-Report, der
    das heute verkaufte Listing verschluckte). Vorhandene -> Werte uebernehmen
    (views=clicks), fehlende -> echt 0."""
    seen: dict = {}

    class _RefreshEbay:
        async def get_all_listing_analytics(self, item_ids=None, *, days=30):
            seen["item_ids"] = sorted(item_ids or [])
            return {"hit": EbayAnalytics(listing_id="hit", impressions=321, clicks=7)}

    monkeypatch.setattr(opt, "_real_ebay", lambda: _RefreshEbay())
    hit = Listing(title_seo="Hat Traffic + heute verkauft", description="d",
                  listing_status="active", ebay_item_id="hit",
                  clicks_week=0, impressions_week=0)
    miss = Listing(title_seo="Kein Traffic im Zeitraum", description="d",
                   listing_status="active", ebay_item_id="miss",
                   clicks_week=9, impressions_week=999)
    db.add_all([hit, miss]); db.commit()

    r = await opt.refresh_click_data(db)

    db.refresh(hit); db.refresh(miss)
    assert seen["item_ids"] == ["hit", "miss"]          # ALLE per ID abgefragt
    assert (hit.impressions_week, hit.clicks_week) == (321, 7)   # views -> clicks
    assert (miss.impressions_week, miss.clicks_week) == (0, 0)   # fehlt = echt 0
    assert r["error"] is None and r["refreshed"] == 1


# ------------------------------- 3 Funnel-Buckets -------------------------------
def test_optimization_buckets_route_by_funnel_stage(db):
    """Kandidaten landen im richtigen Bucket (Preis/Titel/Reaktivieren) und sind
    nach 'verschwendeter Aufmerksamkeit' sortiert; 0/0, frisch optimiert und frisch
    verkauft tauchen NICHT auf."""
    from datetime import datetime, timezone, timedelta
    from app.models import Sale
    now = datetime.now(timezone.utc)
    pid = _confirmed_product(db)

    def mk(title, item, impr, clk, sales_total=0, optimized_at=None, age_days=50):
        # age_days > opt_min_age_days(42, Nutzerwunsch 08.08.: 6 Wochen), aber < cleanup_min_age_days(60): qualifiziert fuer
        # Preis/Titel, faellt aber nicht ins Aufraeumen.
        return Listing(title_seo=title, description="d", listing_status="active",
                       ebay_item_id=item, impressions_week=impr, clicks_week=clk,
                       sales_total=sales_total, optimized_at=optimized_at,
                       listing_start_date=now - timedelta(days=age_days),
                       product_id=pid, price_eur=Decimal("19.90"))

    price_hi = mk("Preis viel Klicks", "p2", 500, 50)       # -> price, zuerst (gleiches Alter, mehr Klicks)
    price_lo = mk("Preis wenig Klicks", "p1", 100, 5)       # -> price
    title_l = mk("Titel", "t1", 80, 0)                       # -> title (impr>5, kein Klick)
    invisible = mk("Unsichtbar", "i1", 0, 0)                 # 0/0, <60 Tage -> kein Bucket
    hidden = mk("Frisch optimiert", "h1", 90, 9, optimized_at=now)   # -> ausgeblendet (Wirkzeit)
    react = mk("Kalt", "r1", 200, 20, sales_total=1)         # verkauft + kalt -> reactivate
    warm = mk("Frisch verkauft", "w1", 200, 20, sales_total=1)       # verkauft + frisch -> nirgends
    too_new = mk("Zu frisch", "n1", 300, 30, age_days=5)     # <21 Tage online -> NICHT optimieren
    db.add_all([price_hi, price_lo, title_l, invisible, hidden, react, warm, too_new])
    db.commit()
    db.add_all([
        Sale(ebay_transaction_id="tx-old", listing_id=react.id,
             status="delivered", sale_date=now - timedelta(days=120)),
        Sale(ebay_transaction_id="tx-new", listing_id=warm.id,
             status="delivered", sale_date=now - timedelta(days=2)),
    ])
    db.commit()

    r = opt.list_optimization_buckets(db)
    b = r["buckets"]
    assert [x["listing_id"] for x in b["price"]] == [price_hi.id, price_lo.id]
    assert [x["listing_id"] for x in b["title"]] == [title_l.id]
    assert [x["listing_id"] for x in b["reactivate"]] == [react.id]
    assert too_new.id not in {x["listing_id"] for lst in b.values() for x in lst}  # Mindestalter
    all_ids = {x["listing_id"] for lst in b.values() for x in lst}
    assert invisible.id not in all_ids     # 0 Impr/0 Klicks -> kein Bucket
    assert hidden.id not in all_ids        # frisch optimiert -> Wirkzeit
    assert warm.id not in all_ids          # frisch verkauft -> kein Handlungsbedarf
    assert r["counts"] == {"price": 2, "title": 1, "reactivate": 1, "cleanup": 0, "tracked": 0}
    assert b["price"][0]["ctr"] == round(50 / 500, 4)


# ------------------------------- Preissenkung (#4): 20%-Boden -------------------------------
def test_price_floor_keeps_20pct_margin_and_undercuts_8eur_rule():
    from app.config import Settings
    from app.services import pricing
    s = Settings(ebay_fee_pct=0.22, ebay_fixed_fee_eur=0.45, lowering_min_margin_pct=0.20,
                 min_profit_eur=8.0, use_mocks=True)
    floor = pricing.price_floor(8.0, settings=s)
    # Gebuehr inkl. 19% MwSt (eBay besteuert die gesamte Gebuehr)
    fee_pct = pricing.effective_fee_pct(None, settings=s)
    profit = floor - floor * fee_pct - pricing.ebay_fixed_fee(s) - 8.0
    assert round(profit / floor, 2) == 0.20          # bei floor genau 20% Marge, kein Verlust
    assert pricing.price_floor(None, settings=s) is None
    up = pricing.compute_price(8.0, settings=s).rounded_price_eur
    assert floor < up                                 # 8€-Zwang entfaellt -> Senk-Spielraum


async def test_compute_market_clamps_lowering_to_floor(db, monkeypatch):
    from decimal import Decimal
    from app.models import Listing, Product
    from app.services import optimization_service as opt
    p = Product(aliexpress_url="https://ae/1", title_raw="Kette", price_cny=Decimal("5"))
    db.add(p); db.flush()
    l = Listing(product_id=p.id, title_seo="Halskette Edelstahl Herren", description="d",
                price_eur=Decimal("25.95"), cost_eur=Decimal("8"))
    db.add(l); db.commit()

    class _LLM:
        async def analyze_market(self, **kw):
            key = kw["variants"][0]["key"]
            # KI will absurd tief senken (weit unter den Boden)
            return {"price_recommendations": [{"variant_key": key,
                    "recommended_price_eur": 9.0, "reasoning": "billiger als Markt"}],
                    "push_recommendations": [], "market_note": ""}
    monkeypatch.setattr(opt, "get_llm_client", lambda: _LLM())

    m = await opt._compute_market(db, l, [{"title": "Konk", "price_eur": 14.0, "url": "u"}])
    v = m["variants"][0]
    assert v["price_floor_eur"] is not None
    assert v["recommended_price_eur"] >= v["price_floor_eur"]   # NIE unter den 20%-Boden
    assert v.get("clamped_to_floor") is True
    assert v["is_lowering"] is True                             # Senkung wird erkannt


# ------------------------------- Lösch-Bucket (#3) -------------------------------
def test_cleanup_bucket_flags_dead_listings_only(db):
    """cleanup = nie verkauft + 0 Traffic + lange online + nicht 'behalten'.
    Junge, dismisste und Traffic-Listings NICHT."""
    from datetime import datetime, timezone, timedelta
    from app.services import optimization_service as opt
    now = datetime.now(timezone.utc)
    pid = _confirmed_product(db)

    def mk(title, item, *, days_online, clk=0, impr=0, dismissed=None):
        return Listing(title_seo=title, description="d", listing_status="active",
                       ebay_item_id=item, impressions_week=impr, clicks_week=clk,
                       sales_total=0, price_eur=Decimal("24.95"), product_id=pid,
                       listing_start_date=now - timedelta(days=days_online),
                       cleanup_dismissed_at=dismissed)
    dead = mk("Ladenhueter alt", "c1", days_online=90)              # -> cleanup
    young = mk("Neu, noch Chance", "c2", days_online=10)            # zu jung -> nichts
    kept = mk("Behalten", "c3", days_online=200, dismissed=now)     # dismissed -> nichts
    has_clicks = mk("Klicks aber tot", "c4", days_online=90, clk=7)  # -> price, nicht cleanup
    db.add_all([dead, young, kept, has_clicks]); db.commit()

    r = opt.list_optimization_buckets(db)
    cleanup_ids = {x["listing_id"] for x in r["buckets"]["cleanup"]}
    assert dead.id in cleanup_ids
    assert young.id not in cleanup_ids
    assert kept.id not in cleanup_ids
    assert has_clicks.id not in cleanup_ids
    assert has_clicks.id in {x["listing_id"] for x in r["buckets"]["price"]}
    assert r["counts"]["cleanup"] == 1
    # age_days wird mitgeliefert
    assert next(x for x in r["buckets"]["cleanup"] if x["listing_id"] == dead.id)["age_days"] >= 60


def test_dismiss_cleanup_toggles_flag(db):
    from app.services import optimization_service as opt
    l = Listing(title_seo="X", description="d", listing_status="active", price_eur=Decimal("9.95"))
    db.add(l); db.commit()
    opt.dismiss_cleanup(db, listing_id=l.id)
    db.refresh(l); assert l.cleanup_dismissed_at is not None
    opt.dismiss_cleanup(db, listing_id=l.id, undo=True)
    db.refresh(l); assert l.cleanup_dismissed_at is None


async def test_recommended_ad_rate_is_margin_based_not_fixed(db, monkeypatch):
    from decimal import Decimal
    from app.models import Listing, Product
    from app.services import optimization_service as opt
    p = Product(aliexpress_url="https://ae/2", title_raw="Ring", price_cny=Decimal("3"))
    db.add(p); db.flush()
    # Hohe Marge (EK 5, VK 20) -> Anzeigenrate soll bis zum Deckel (20%) gehen, NICHT 13%.
    hi = Listing(product_id=p.id, title_seo="Ring Silber", description="d",
                 price_eur=Decimal("20"), cost_eur=Decimal("5"))
    db.add(hi); db.commit()

    class _LLM:
        async def analyze_market(self, **kw):
            key = kw["variants"][0]["key"]
            return {"price_recommendations": [{"variant_key": key,
                    "recommended_price_eur": 20.0, "reasoning": "ok"}],
                    "push_recommendations": [], "market_note": ""}
    monkeypatch.setattr(opt, "get_llm_client", lambda: _LLM())
    m = await opt._compute_market(db, hi, [{"title": "K", "price_eur": 19.0, "url": "u"}])
    assert m["recommended_ad_rate_pct"] == 20.0          # Deckel bei hoher Marge, nicht 13
    assert m["ad_rate_margin_limited"] is False


async def test_activate_deactivate_volume_pricing(db, monkeypatch):
    from decimal import Decimal
    from app.models import Listing, Product
    from app.services import optimization_service as opt
    p = Product(aliexpress_url="https://ae/9", title_raw="Ring", price_cny=Decimal("4"))
    db.add(p); db.flush()
    l = Listing(product_id=p.id, title_seo="Ring Silber", description="d",
                ebay_item_id="8009", category_name="Uhren & Schmuck",
                price_eur=Decimal("20"), cost_eur=Decimal("5.99"), listing_status="active")
    db.add(l); db.commit()
    calls = {}
    class _FakeEbay:
        async def create_volume_pricing(self, item_id, *, tiers, name):
            calls["item_id"] = item_id; calls["tiers"] = list(tiers); return "PROMO-1"
        async def delete_volume_pricing(self, pid):
            calls["deleted"] = pid
    monkeypatch.setattr(opt, "_real_ebay", lambda: _FakeEbay())

    r = await opt.activate_volume_pricing(db, listing_id=l.id)
    db.refresh(l)
    assert r["promotion_id"] == "PROMO-1" and l.volume_promotion_id == "PROMO-1"
    assert calls["item_id"] == "8009" and calls["tiers"]

    r2 = await opt.deactivate_volume_pricing(db, listing_id=l.id)
    db.refresh(l)
    assert "entfernt" in r2["message"] and l.volume_promotion_id is None and calls["deleted"] == "PROMO-1"


def test_volume_pricing_blocks_unconfirmed_image_source(db):
    from decimal import Decimal
    from app.models import Listing, Product
    from app.services import optimization_service as opt
    p = Product(aliexpress_url="https://ae/x", title_raw="Kette", price_cny=Decimal("4"),
                alternatives={"matched": "image-auto", "confirmed": False})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, title_seo="Kette", description="d", ebay_item_id="8",
                category_name="Uhren & Schmuck", price_eur=Decimal("20"), cost_eur=Decimal("5.99"),
                listing_status="active")
    db.add(l); db.commit()
    r = opt.suggest_volume_pricing(db, listing_id=l.id)
    assert r["recommended"] is False and r["source_confirmed"] is False   # unsichere Quelle -> blockiert
    p.alternatives = {"matched": "image-auto", "confirmed": True}; db.commit()
    r2 = opt.suggest_volume_pricing(db, listing_id=l.id)
    assert r2["recommended"] is True and r2["source_confirmed"] is True    # manuell bestätigt -> Kandidat


async def test_auto_remove_unprofitable_multibuy(db, monkeypatch):
    """Automatik: aktive Mengenrabatte OHNE profitable Staffel werden entfernt; noch profitable
    bleiben; ohne gueltigen Plan (unbestaetigte Quelle) wird NICHT angefasst (fail-safe)."""
    from decimal import Decimal
    from app.models import Listing
    from app.services import optimization_service as opt

    def _mk(item):
        l = Listing(title_seo="V" + item, description="d", listing_status="active",
                    ebay_item_id=item, price_eur=Decimal("20.00"), cost_eur=Decimal("6.00"),
                    volume_promotion_id="promo-" + item)
        db.add(l); return l
    loss = _mk("loss"); okl = _mk("ok"); noplan = _mk("noplan")
    db.commit()

    def _fake_suggest(db, *, listing_id):
        it = db.get(Listing, listing_id).ebay_item_id
        if it == "loss":     # KEIN Tier bringt echten Mehrgewinn -> entfernen
            return {"recommended": False, "margin_1": 0.4, "reason": "kein Tier",
                    "all_tiers": [{"qty": 2, "ok": False}, {"qty": 3, "ok": False}]}
        if it == "ok":       # eine Staffel ist ok -> bleibt (Basis-Marge egal)
            return {"recommended": True, "margin_1": 0.5,
                    "all_tiers": [{"qty": 2, "ok": True}, {"qty": 3, "ok": False}]}
        return {"recommended": False, "source_confirmed": False, "all_tiers": []}   # kein Plan
    monkeypatch.setattr(opt, "suggest_volume_pricing", _fake_suggest)

    deleted = []
    class _FakeEbay:
        async def delete_volume_pricing(self, pid):
            deleted.append(pid)
    monkeypatch.setattr(opt, "_real_ebay", lambda: _FakeEbay())

    r = await opt.auto_remove_unprofitable_multibuy(db)
    db.refresh(loss); db.refresh(okl); db.refresh(noplan)
    assert r["enabled"] is True and r["removed"] == 1
    assert deleted == ["promo-loss"]
    assert loss.volume_promotion_id is None                 # unrentabel -> entfernt
    assert okl.volume_promotion_id == "promo-ok"            # noch rentabel -> bleibt
    assert noplan.volume_promotion_id == "promo-noplan"     # kein Plan -> nicht angefasst


async def test_auto_remove_multibuy_respects_toggle(db, monkeypatch):
    """Schalter aus -> Automatik macht nichts; ein manueller Klick (respect_toggle=False) laeuft."""
    from app.config import get_settings
    from app.services import optimization_service as opt
    monkeypatch.setattr(get_settings(), "multibuy_auto_remove", False)
    r = await opt.auto_remove_unprofitable_multibuy(db)
    assert r["enabled"] is False and r["removed"] == 0
    r2 = await opt.auto_remove_unprofitable_multibuy(db, respect_toggle=False)
    assert r2["enabled"] is True


# ------------------------- Optimieren: Feinschliff 08.07. -------------------------
def test_optimization_excludes_unconfirmed_image_match(db):
    """Unbestaetigte Auto-Bild-Matches gehoeren NICHT in die Optimier-Liste."""
    from datetime import datetime, timezone, timedelta
    from app.models import Product
    now = datetime.now(timezone.utc)
    p = Product(aliexpress_url="https://de.aliexpress.com/item/u.html", aliexpress_id="u",
                alternatives={"matched": "image-auto", "confirmed": False})
    db.add(p); db.flush()
    l = Listing(title_seo="Unbestätigt", description="d", listing_status="active",
                ebay_item_id="ux", impressions_week=200, clicks_week=20, product_id=p.id,
                listing_start_date=now - timedelta(days=50), price_eur=Decimal("19.90"))
    db.add(l); db.commit()
    b = opt.list_optimization_buckets(db)["buckets"]
    ids = {x["listing_id"] for lst in b.values() for x in lst}
    assert l.id not in ids
    # bestaetigt -> erscheint (Preis-Bucket)
    p.alternatives = {"matched": "image-auto", "confirmed": True}; db.commit()
    b2 = opt.list_optimization_buckets(db)["buckets"]
    assert l.id in {x["listing_id"] for x in b2["price"]}


def test_cleanup_includes_low_impressions_and_optimized_failures(db):
    """Aufraeumen strenger: <=5 Impr/0 Klicks aged -> cleanup; optimiert+trotzdem tot -> cleanup."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    pid = _confirmed_product(db)

    def mk(item, impr, clk, since_days, optimized_days=None):
        return Listing(title_seo="x", description="d", listing_status="active", ebay_item_id=item,
                       impressions_week=impr, clicks_week=clk, product_id=pid,
                       listing_start_date=now - timedelta(days=since_days),
                       optimized_at=(now - timedelta(days=optimized_days)) if optimized_days else None,
                       price_eur=Decimal("9.90"))

    aged_lowimpr = mk("c1", 4, 0, 70)                    # >60 Tage, <=5 Impr, 0 Klicks -> cleanup
    opt_failed = mk("c2", 3, 0, 30, optimized_days=40)   # 30 Tage, aber optimiert+tot -> cleanup
    still_young = mk("c3", 3, 0, 45)                      # ueber 6-Wochen-Schwelle, nicht optimiert -> Titel, KEIN cleanup
    db.add_all([aged_lowimpr, opt_failed, still_young]); db.commit()
    b = opt.list_optimization_buckets(db)["buckets"]
    cu = {x["listing_id"] for x in b["cleanup"]}
    assert aged_lowimpr.id in cu and opt_failed.id in cu
    assert still_young.id not in cu
    assert still_young.id in {x["listing_id"] for x in b["title"]}


def test_reactivate_offers_price_cut_when_still_profitable(db):
    """Ladenhueter (kalt) bekommen einen Preissenkungs-Vorschlag, der noch im Plus bleibt."""
    from datetime import datetime, timezone, timedelta
    from app.models import Sale
    now = datetime.now(timezone.utc)
    l = Listing(title_seo="Ladenhüter", description="d", listing_status="active", ebay_item_id="pc",
                sales_total=1, price_eur=Decimal("29.90"), cost_eur=Decimal("6.00"),
                product_id=_confirmed_product(db),
                listing_start_date=now - timedelta(days=90))
    db.add(l); db.flush()
    db.add(Sale(ebay_transaction_id="pc-old", listing_id=l.id, status="delivered",
                sale_date=now - timedelta(days=90)))
    db.commit()
    react = opt.list_optimization_buckets(db)["buckets"]["reactivate"]
    assert react and react[0]["listing_id"] == l.id
    pc = react[0]["price_cut"]
    assert pc is not None
    assert pc["suggested_price_eur"] < 29.90 and pc["suggested_profit_eur"] > 0


def test_optimization_needs_real_online_date(db):
    """Ohne echtes eBay-Online-Datum (listing_start_date) ist das Alter unbekannt ->
    nicht optimieren (verhindert, dass frisch gelistete Artikel mit altem DB-Datum
    durchrutschen – der HTC-NE61-Fall)."""
    l = Listing(title_seo="Ohne Startdatum", description="d", listing_status="active",
                ebay_item_id="nd", impressions_week=300, clicks_week=30,
                listing_start_date=None, price_eur=Decimal("19.90"))
    db.add(l); db.commit()
    b = opt.list_optimization_buckets(db)["buckets"]
    ids = {x["listing_id"] for lst in b.values() for x in lst}
    assert l.id not in ids


# ------------------------- Nachverfolgung / Snapshot (08.07.) -------------------------
def test_capture_optimization_snapshots_metrics(db):
    from datetime import datetime, timezone, timedelta
    l = Listing(title_seo="Opt", description="d", listing_status="active", ebay_item_id="o1",
                clicks_week=5, impressions_week=100, sales_total=2, price_eur=Decimal("20.00"))
    db.add(l); db.commit()
    opt._capture_optimization(l); db.commit()
    snap = db.get(Listing, l.id).opt_snapshot
    assert snap["clicks"] == 5 and snap["impressions"] == 100 and snap["sales"] == 2
    assert snap["price_eur"] == 20.0 and "at" in snap
    assert l.optimized_at is not None and l.optimization_status == "optimized"


def test_tracker_bucket_before_after(db):
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    l = Listing(title_seo="Verfolgt", description="d", listing_status="active", ebay_item_id="t1",
                clicks_week=10, impressions_week=120, sales_total=3, price_eur=Decimal("18.00"),
                optimized_at=now - timedelta(days=5),
                opt_snapshot={"clicks": 3, "impressions": 50, "sales": 1,
                              "price_eur": 24.0, "at": (now - timedelta(days=5)).isoformat()})
    db.add(l); db.commit()
    r = opt.list_optimization_buckets(db)
    tr = r["buckets"]["tracked"]
    assert r["counts"]["tracked"] == 1 and len(tr) == 1
    t = tr[0]
    assert t["before"] == {"clicks": 3, "impressions": 50, "sales": 1}
    assert t["after"] == {"clicks": 10, "impressions": 120, "sales": 3}
    assert t["delta"] == {"clicks": 7, "impressions": 70, "sales": 2}
    assert t["days_since"] == 5 and t["price_before_eur"] == 24.0


def test_volume_candidates_active_sorted_last(db, monkeypatch):
    # Zwei empfohlene Kandidaten: der bereits aktive muss NACH dem inaktiven kommen.
    def _mk(item, active):
        return Listing(title_seo="V"+item, description="d", listing_status="active",
                       ebay_item_id=item, price_eur=Decimal("30.00"), cost_eur=Decimal("6.00"),
                       volume_promotion_id=("promo-"+item if active else None))
    a = _mk("act", True); b = _mk("inact", False)
    db.add_all([a, b]); db.commit()
    # suggest_volume_pricing so stubben, dass beide empfohlen sind (gleicher Mehrgewinn).
    def _fake_suggest(db, *, listing_id):
        return {"recommended": True, "category_name": "X", "vk_eur": 30.0, "ek_eur": 6.0,
                "ek_extra_eur": 6.0, "margin_1": 0.5,
                "tiers": [{"qty": 2, "discount_pct": 5, "price_eur": 28.5, "db_extra_eur": 5.0}]}
    monkeypatch.setattr(opt, "suggest_volume_pricing", _fake_suggest)
    cands = opt.list_volume_pricing_candidates(db)["candidates"]
    ids = [c["listing_id"] for c in cands]
    assert ids == [b.id, a.id]           # inaktiv oben, aktiv unten
    assert cands[-1]["active"] is True


# ------------------------- Optimieren-Paket 09.07. -------------------------
def test_fix_faux_leather():
    f = opt._fix_faux_leather
    assert f("Damen Handtasche Echtes Leder Schwarz") == "Damen Handtasche Kunstleder Schwarz"
    assert f("Vintage Echtleder Geldbörse") == "Vintage Kunstleder Geldbörse"
    assert f("Elegante Ledertasche Damen") == "Elegante Kunstledertasche Damen"
    assert f("Tasche aus Kunstleder Premium") == "Tasche aus Kunstleder Premium"   # unangetastet
    assert f("Sneaker Weiß Größe 42") == "Sneaker Weiß Größe 42"                     # ohne Leder


def test_clean_title_defuses_leather():
    t = opt._clean_suggested_title("Elegante Damen Handtasche Echtes Leder Schwarz Groß", "Alt Titel")
    assert t is not None and "Kunstleder" in t and "Echt" not in t
    t2 = opt._clean_suggested_title("Schicke Ledertasche Damen Umhängetasche Shopper", "Alt")
    assert t2 is not None and t2.startswith("Schicke Kunstledertasche")


def test_price_for_mode_rounds_up_to_95():
    from app.config import Settings
    from app.services import golive_service as gl
    s = Settings(ebay_fee_pct=0.2, ebay_fixed_fee_eur=0.45, price_cents=0.95,
                 target_margin_pct=0.20, use_mocks=True)
    p = gl._price_for_mode(5.0, mode="margin", value=25, settings=s)
    assert round(p - int(p), 2) == 0.95     # endet auf ,95 (aufgerundet)


def test_price_for_mode_uses_vat_fee_without_category():
    """Regression: ohne Kategorie MUSS die MwSt-inkl. Gebuehr (effective_fee_pct(None))
    greifen, nicht das rohe ebay_fee_pct. Sonst war der Preis zu billig und die ECHTE
    Marge lag unter Ziel (der 14.07.-Bypass in golive_service:438)."""
    from app.config import Settings
    from app.services import golive_service as gl, pricing
    s = Settings(ebay_fee_pct=0.2, ebay_ad_rate_pct=0.10, ebay_fixed_fee_eur=0.45,
                 ebay_fee_vat_pct=0.19, price_cents=0.95, target_margin_pct=0.20, use_mocks=True)
    ek = 8.0
    p = gl._price_for_mode(ek, mode="margin", value=25, settings=s, category_name=None)
    # ECHTE (MwSt-inkl.) Marge zum vorgeschlagenen Preis trifft das 25%-Ziel (aufgerundet).
    true_profit = p - p * pricing.effective_fee_pct(None, settings=s) - pricing.ebay_fixed_fee(s) - ek
    assert true_profit / p >= 0.24     # alter Roh-Gebuehr-Preis ergaebe hier ~0.20 -> faellt durch


def test_volume_plan_exposes_profit_1():
    from app.config import Settings
    from app.services import pricing
    s = Settings(ebay_fee_pct=0.2, ebay_fixed_fee_eur=0.45, multibuy_min_margin_pct=0.25,
                 multibuy_max_discount_pct=0.10, multibuy_min_extra_profit_eur=1.0,
                 lowering_min_margin_pct=0.20, price_cents=0.95, use_mocks=True)
    plan = pricing.volume_pricing_plan(30.0, 6.0, fee_pct=0.2, settings=s)
    assert plan["profit_1_eur"] == 17.46     # 30*0.8 - 0.45*1.19 (Fix inkl. MwSt) - 6


async def test_activate_all_volume_aggregates(db, monkeypatch):
    monkeypatch.setattr(opt, "list_volume_pricing_candidates",
                        lambda db: {"candidates": [
                            {"listing_id": 1, "active": False},
                            {"listing_id": 2, "active": True},    # schon aktiv -> überspringen
                            {"listing_id": 3, "active": False}]})
    calls = []
    async def _fake_activate(db, *, listing_id):
        calls.append(listing_id)
        if listing_id == 3:
            raise ValueError("boom")
        return {"listing_id": listing_id}
    monkeypatch.setattr(opt, "activate_volume_pricing", _fake_activate)
    r = await opt.activate_all_volume_pricing(db)
    assert r["total"] == 2 and r["activated"] == 1 and r["failed"] == 1
    assert calls == [1, 3]     # nur inaktive; aktiver (2) übersprungen


def test_clean_title_rejects_leather_noop_vs_current():
    # Wird ein Vorschlag ERST nach der Kunstleder-Normalisierung identisch zum aktuellen
    # Titel, darf er nicht als (No-Op-)Vorschlag durchrutschen (Review-Fund 09.07.).
    cur = "Kunstleder Jacke Damen Umhängetasche Elegant"
    assert opt._clean_suggested_title("Echt Leder Jacke Damen Umhängetasche Elegant", cur) is None
    assert opt._clean_suggested_title("Neue Kunstleder Handtasche Damen Schwarz Elegant", cur) is not None


def test_dismiss_optimization_hides_and_undoes(db):
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    l = Listing(title_seo="Läuft so gut", description="d", listing_status="active", ebay_item_id="d1",
                impressions_week=80, clicks_week=0, sales_total=0, product_id=_confirmed_product(db),
                listing_start_date=now - timedelta(days=50), price_eur=Decimal("19.90"))
    db.add(l); db.commit()
    assert l.id in {x["listing_id"] for x in opt.list_optimization_buckets(db)["buckets"]["title"]}
    opt.dismiss_optimization(db, listing_id=l.id)      # „So lassen"
    b = opt.list_optimization_buckets(db)["buckets"]
    assert l.id not in {x["listing_id"] for lst in b.values() for x in lst}
    opt.dismiss_optimization(db, listing_id=l.id, undo=True)
    assert l.id in {x["listing_id"] for x in opt.list_optimization_buckets(db)["buckets"]["title"]}


def test_optimization_excludes_no_source_listing(db):
    """Wurde die (falsche) Quelle entfernt (product_id None), ist das Produkt nicht mehr
    verifiziert -> raus aus Preis-/Titel-/Reaktivieren (Nutzerregel 09.07.)."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    l = Listing(title_seo="Ohne Quelle Weekender", description="d", listing_status="active",
                ebay_item_id="ns1", impressions_week=80, clicks_week=0, sales_total=0,
                product_id=None, listing_start_date=now - timedelta(days=50),
                price_eur=Decimal("39.90"))
    db.add(l); db.commit()
    b = opt.list_optimization_buckets(db)["buckets"]
    ids = {x["listing_id"] for lst in b.values() for x in lst}
    assert l.id not in ids


# ------------------------- KI-Bildbewertung (Titelbild) 09.07. -------------------------
async def test_assess_images_annotates_and_picks_best(db, monkeypatch):
    """assess_images haengt Score/Grund an jedes Bild und markiert das beste Titelbild."""
    from app.models import Product
    p = Product(aliexpress_url="https://ae/x", title_raw="Weekender",
                images=["https://img/1.jpg", "https://img/2.jpg"])
    db.add(p); db.flush()
    l = Listing(title_seo="Weekender Reisetasche", description="d", listing_status="active",
                product_id=p.id, price_eur=Decimal("39.90"))   # kein ebay_item_id -> Quell-Bilder
    db.add(l); db.commit()

    class _LLM:
        async def assess_images(self, *, product_title, image_urls):
            assert image_urls == ["https://img/1.jpg", "https://img/2.jpg"]
            return {"assessments": [{"index": 0, "score": 4.0, "reason": "Text im Bild"},
                                    {"index": 1, "score": 9.0, "reason": "sauber & hell"}],
                    "best_index": 1, "note": "Bild 2 eignet sich besser."}
    monkeypatch.setattr(opt, "get_llm_client", lambda: _LLM())

    r = await opt.assess_images(db, listing_id=l.id)
    assert r["image_source"] == "quelle"
    assert r["best_index"] == 1
    assert [i["score"] for i in r["images"]] == [4.0, 9.0]
    assert r["images"][1]["is_best"] is True and r["images"][0]["is_best"] is False
    assert r["images"][1]["reason"] == "sauber & hell"
    assert r["note"] == "Bild 2 eignet sich besser."


async def test_assess_images_survives_llm_failure(db, monkeypatch):
    """Faellt die KI aus, bleibt der Aufruf heil: keine Scores, klarer Hinweis, best_index None."""
    from app.models import Product
    p = Product(aliexpress_url="https://ae/x", images=["https://img/1.jpg"])
    db.add(p); db.flush()
    l = Listing(title_seo="X", description="d", listing_status="active",
                product_id=p.id, price_eur=Decimal("9.90"))
    db.add(l); db.commit()

    class _Boom:
        async def assess_images(self, **kw):
            raise RuntimeError("api down")
    monkeypatch.setattr(opt, "get_llm_client", lambda: _Boom())

    r = await opt.assess_images(db, listing_id=l.id)
    assert r["best_index"] is None
    assert r["images"][0]["score"] is None and r["images"][0]["is_best"] is False
    assert "nicht verfügbar" in r["note"]


async def test_assess_images_no_images(db, monkeypatch):
    l = Listing(title_seo="Ohne Bild", description="d", listing_status="active",
                product_id=None, price_eur=Decimal("9.90"))
    db.add(l); db.commit()
    r = await opt.assess_images(db, listing_id=l.id)
    assert r["images"] == [] and r["best_index"] is None
    assert "Keine Bilder" in r["note"]


# ------------------------- Mengenrabatt: konservative EK + aktive sichtbar (09.07.) -------------------------
def _vol_listing(db, *, cost_eur, price_eur=19.95, ae_price="5", promo=None, item="v1"):
    from app.models import Product
    p = Product(aliexpress_url=f"https://de.aliexpress.com/item/vol-{item}.html", aliexpress_id="vol" + item,
                price_cny=Decimal(ae_price),
                variants={"axes": {"Farbe": ["Schwarz", "Blau"]},
                          "skus": [{"attr": f"a{i}", "options": {"Farbe": c}, "price": ae_price,
                                    "stock": 10} for i, c in enumerate(["Schwarz", "Blau"])]})
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku="AE-" + item, title_seo="Vol " + item, description="d",
                listing_status="active", ebay_item_id=item, price_eur=Decimal(str(price_eur)),
                cost_eur=Decimal(str(cost_eur)), category_name="Auto & Motorrad",
                volume_promotion_id=promo)
    db.add(l); db.commit()
    return l, p


def test_volume_pricing_worst_case_is_lowest_margin_variant(db, monkeypatch):
    """Der bindende Worst Case ist die Variante mit dem NIEDRIGSTEN Basis-Gewinn, NICHT die
    mit dem hoechsten EK — sonst versteckt eine billige Verlust-Variante den Schaden
    (Nutzer-Fund IRAN-Kette 09.08.: Verlust im Cockpit, Mengenrabatt trotzdem aktiv)."""
    from app.services import golive_service as gl
    l, _ = _vol_listing(db, cost_eur=4, item="wc1")
    monkeypatch.setattr(gl, "variant_price_rows", lambda listing, product, settings: [
        {"ek_eur": 8.0, "current_price_eur": 30.0, "ae_price_eur": None},   # profitabel
        {"ek_eur": 4.6, "current_price_eur": 4.5, "ae_price_eur": None},    # VERLUST
    ])
    r = opt.suggest_volume_pricing(db, listing_id=l.id)
    assert r["vk_eur"] == 4.5 and r["ek_eur"] == 4.6, \
        "die Verlust-Variante muss die Rechenbasis sein (alter Code nahm EK 8 / VK 30)"
    assert r["profit_1_eur"] is not None and r["profit_1_eur"] <= 0
    assert r["recommended"] is False


async def test_auto_remove_multibuy_on_base_loss(db, monkeypatch):
    """NEU 09.08. (IRAN-Kette): macht die BASIS Verlust, wird der aktive Rabatt entfernt —
    auch wenn die Staffeln fuer sich noch 'Mehrgewinn' zeigen. Ein Verlust-Listing darf
    keinen Kaufanreiz fuer MEHR Einheiten tragen."""
    l, _ = _vol_listing(db, cost_eur=6, promo="promo-base-loss", item="bl1")

    def _fake_suggest(db, *, listing_id):
        return {"recommended": False, "profit_1_eur": -0.42, "margin_1": -0.02,
                "all_tiers": [{"qty": 2, "ok": True}]}
    monkeypatch.setattr(opt, "suggest_volume_pricing", _fake_suggest)
    deleted = []

    class _FakeEbay:
        async def delete_volume_pricing(self, pid):
            deleted.append(pid)
    monkeypatch.setattr(opt, "_real_ebay", lambda: _FakeEbay())

    r = await opt.auto_remove_unprofitable_multibuy(db)
    db.refresh(l)
    assert r["removed"] == 1 and deleted == ["promo-base-loss"]
    assert l.volume_promotion_id is None


def test_volume_pricing_uses_conservative_cost(db):
    """cost_eur (real, evtl. höher) schlägt den optimistischen AE-Listenpreis: die EK-Basis
    für die Mengenrabatt-Marge ist die HÖHERE der beiden -> keine zu optimistische Empfehlung."""
    l, p = _vol_listing(db, cost_eur=12.49, ae_price="5")   # effective_cost(5)=6.99 << 12.49
    r = opt.suggest_volume_pricing(db, listing_id=l.id)
    assert r["ek_eur"] == 12.49            # konservativ: cost_eur, NICHT 6.99
    assert r["recommended"] is False       # ~13% Ist-Marge -> nicht empfohlen (statt fälschlich ja)


def test_volume_candidates_show_active_but_thin(db):
    """Ein aktiver Mengenrabatt bleibt in der Liste, auch wenn die Marge inzwischen zu dünn
    ist (sonst nicht mehr entfernbar) – mit Warnung. Ein inaktiver dünner erscheint NICHT."""
    active_thin, _ = _vol_listing(db, cost_eur=12.49, promo="promo-1@EBAY_DE", item="vt1")
    inactive_thin, _ = _vol_listing(db, cost_eur=12.49, promo=None, item="vt2")
    cands = opt.list_volume_pricing_candidates(db)["candidates"]
    by_id = {c["listing_id"]: c for c in cands}
    assert active_thin.id in by_id
    assert by_id[active_thin.id]["active"] is True
    assert by_id[active_thin.id]["recommended"] is False
    assert by_id[active_thin.id]["warn"]
    assert inactive_thin.id not in by_id       # inaktiv + dünn -> nicht gelistet


def test_volume_pricing_preserves_bundle_shipping_trick(db):
    """Im Normalfall (cost_eur == effektiver AE-EK) bleibt die ECHTE marginale EK erhalten
    (Bundle-Versand-Trick) – die konservative EK-Logik darf sie nicht wegbügeln. Weicht
    cost_eur ab, wird derselbe Spar-Betrag auf die höhere EK angewandt."""
    from app.services import pricing
    from app.config import get_settings
    s = get_settings()
    ae_full = round(pricing.effective_cost(5.0, settings=s), 2)
    marg = round(pricing.marginal_unit_cost(5.0, at_qty=2, settings=s), 2)
    assert marg < ae_full                      # Versand-Trick macht die Zusatz-Einheit billiger

    l, _ = _vol_listing(db, cost_eur=ae_full, ae_price="5", item="bt1")
    r = opt.suggest_volume_pricing(db, listing_id=l.id)
    assert r["ek_eur"] == ae_full
    assert r["ek_extra_eur"] == marg           # echte marginale EK erhalten (nicht auf ek gehoben)

    l2, _ = _vol_listing(db, cost_eur=12.49, ae_price="5", item="bt2")
    r2 = opt.suggest_volume_pricing(db, listing_id=l2.id)
    bundle_saving = round(ae_full - marg, 2)
    assert r2["ek_eur"] == 12.49
    assert r2["ek_extra_eur"] == round(12.49 - bundle_saving, 2)   # gleicher Spar-Betrag, höhere Basis
    assert r2["ek_extra_eur"] <= r2["ek_eur"]


# ---------------------- Massen-Beenden der Aufraeum-Kandidaten ----------------------
# Nutzerwunsch 02.08.: ein Knopf, der ALLE Ladenhueter (nie verkauft, 0 Klicks, wenig
# Impressionen) auf eBay beendet. Weil das unumkehrbar ist, sichern diese Tests die
# fail-closed-Pruefung ab, die unmittelbar VOR jedem Beenden laeuft.
def _cleanup_listing(db, *, sales_total=0, clicks=0, impressions=0, status="active"):
    from datetime import datetime, timedelta, timezone
    from app.models import Product
    p = Product(aliexpress_url=f"https://ae/c{id(db)}{clicks}{impressions}{sales_total}{status}",
                aliexpress_id=f"c{abs(hash((clicks, impressions, sales_total, status))) % 10**9}",
                price_cny=Decimal("5"))
    db.add(p); db.flush()
    l = Listing(product_id=p.id, ebay_sku=f"CL-{p.id}", ebay_item_id=f"IT{p.id}",
                title_seo="Ladenhueter Testartikel", description="d", listing_status=status,
                price_eur=Decimal("19.95"), cost_eur=Decimal("7"),
                sales_total=sales_total, clicks_week=clicks, impressions_week=impressions,
                listing_start_date=datetime.now(timezone.utc) - timedelta(days=120))
    db.add(l); db.commit()
    return l


def test_cleanup_valid_only_for_true_ladenhueter(db):
    ok_l = _cleanup_listing(db, sales_total=0, clicks=0, impressions=30)
    assert opt._cleanup_still_valid(db, ok_l.id)[0] is True


def test_cleanup_refuses_sold_clicked_visible_and_inactive(db):
    """Jede einzelne Abweichung verhindert das Beenden (fail-closed)."""
    sold = _cleanup_listing(db, sales_total=2, clicks=0, impressions=10)
    clicked = _cleanup_listing(db, sales_total=0, clicks=7, impressions=10)
    seen = _cleanup_listing(db, sales_total=0, clicks=0, impressions=9999)
    ended = _cleanup_listing(db, sales_total=0, clicks=0, impressions=10, status="ended")
    for l, wort in ((sold, "Verkauf"), (clicked, "Klicks"), (seen, "Impressionen"), (ended, "aktiv")):
        ok, grund = opt._cleanup_still_valid(db, l.id)
        assert ok is False and wort.lower() in grund.lower()
    assert opt._cleanup_still_valid(db, 99999999)[0] is False       # unbekannte ID


def test_cleanup_refuses_listing_with_sale_rows(db):
    """Auch ohne eBay-Zaehler: existiert lokal eine Verkaufszeile, wird NICHT beendet."""
    from app.models import Sale
    l = _cleanup_listing(db, sales_total=0, clicks=0, impressions=10)
    db.add(Sale(ebay_transaction_id=f"T-{l.id}", ebay_order_id=f"O-{l.id}",
                ebay_line_item_id=f"T-{l.id}", listing_id=l.id, buyer_name="Max",
                quantity=1, price_eur=Decimal("19.95"), status="delivered"))
    db.commit()
    ok, grund = opt._cleanup_still_valid(db, l.id)
    assert ok is False and "Verkaufszeilen" in grund


def test_end_cleanup_ids_can_only_narrow_never_widen(db, monkeypatch):
    """Eine mitgeschickte ID-Liste darf die serverseitige Auswahl nur EINSCHRAENKEN, nie erweitern.
    Schutz davor, dass ein manipulierter/veralteter Client fremde Listings beenden laesst."""
    import asyncio
    a = _cleanup_listing(db, sales_total=0, clicks=0, impressions=10)
    b = _cleanup_listing(db, sales_total=0, clicks=0, impressions=20)
    fremd = _cleanup_listing(db, sales_total=0, clicks=0, impressions=30)
    monkeypatch.setattr(opt, "cleanup_candidate_ids", lambda _db: [a.id, b.id])   # fremd NICHT dabei
    calls = []

    async def fake_end(_db, *, listing_id):
        calls.append(listing_id)
        return {"listing_id": listing_id, "detail": "beendet"}

    from app.services import golive_service
    monkeypatch.setattr(golive_service, "end_listing_live", fake_end)
    res = asyncio.run(opt.end_cleanup_candidates(db, listing_ids=[a.id, fremd.id]))
    assert calls == [a.id]                       # fremde ID ignoriert, b nicht angefasst
    assert res["n_beendet"] == 1


def test_end_cleanup_skips_invalid_and_survives_errors(db, monkeypatch):
    """Ungueltige werden uebersprungen; ein Fehler stoppt den Lauf nicht."""
    import asyncio
    ok1 = _cleanup_listing(db, sales_total=0, clicks=0, impressions=10)
    verkauft = _cleanup_listing(db, sales_total=3, clicks=0, impressions=10)
    ok2 = _cleanup_listing(db, sales_total=0, clicks=0, impressions=20)
    monkeypatch.setattr(opt, "cleanup_candidate_ids", lambda _db: [ok1.id, verkauft.id, ok2.id])

    async def fake_end(_db, *, listing_id):
        if listing_id == ok1.id:
            raise RuntimeError("eBay 500")
        return {"listing_id": listing_id, "detail": "beendet"}

    from app.services import golive_service
    monkeypatch.setattr(golive_service, "end_listing_live", fake_end)
    res = asyncio.run(opt.end_cleanup_candidates(db))
    assert res["n_beendet"] == 1                 # ok2 durchgelaufen
    assert res["n_fehlgeschlagen"] == 1          # ok1 Fehler, Lauf ging weiter
    assert res["n_uebersprungen"] == 1           # verkauft NIE beendet
    assert res["uebersprungen"][0]["listing_id"] == verkauft.id


def test_clean_suggested_title_entfernt_erfundene_mengen():
    """Zoro-Vorfall 19.08.: Vorschlag darf keine Set-/Mengen-Behauptung einfuehren,
    die der aktuelle Titel nicht traegt."""
    current = "One Piece Zoro Ohrringe Damen Herren Cosplay Gold Silber Anime"
    vorschlag = "One Piece Zoro Ohrringe 3er-Set Damen Herren Cosplay Gold Silber Anime"
    # Nach dem Entfernen bleibt exakt der aktuelle Titel -> Vorschlag zu Recht verworfen
    assert opt._clean_suggested_title(vorschlag, current) is None
    # Bei echtem Mehrwert bleibt der Vorschlag OHNE die erfundene Menge erhalten
    vorschlag_b = "One Piece Zoro Ohrringe 3er-Set Damen Herren Cosplay Gold Silber Manga"
    t = opt._clean_suggested_title(vorschlag_b, current)
    assert t is not None and "3er" not in t and "Set" not in t and "Manga" in t
    # traegt der AKTUELLE Titel die Menge, bleibt sie im Vorschlag erlaubt
    current2 = "Zoro Ohrringe 3er-Set Anime Cosplay Schmuck Fanartikel Geschenk"
    vorschlag2 = "Ohrringe Zoro 3er-Set Anime Cosplay Schmuck Fanartikel Geschenkidee"
    t2 = opt._clean_suggested_title(vorschlag2, current2)
    assert t2 is not None and "3er-Set" in t2
