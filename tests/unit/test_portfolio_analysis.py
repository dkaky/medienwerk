"""Portfolio-Analyse: Konzentration, Trichter, Kohorten, Preisbaender (nur lesen).

Grundlage der Wachstums-Entscheidungen — die Zahlen muessen exakt stimmen
(Regel 14: Finanzzahlen nie schaetzen, Stornos und unverknuepfte Verkaeufe
werden ausgewiesen statt still mitgezaehlt).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.models import Listing, Sale
from app.services.analytics_service import get_portfolio_analysis

_TX = iter(range(1, 10_000))


def _listing(db, *, title="L", status="active", price=None, cost=None,
             sales_total=0, views_30d=None, impressions=0, clicks=0,
             started_days_ago=None):
    l = Listing(title_seo=title, description="d", listing_status=status,
                price_eur=Decimal(str(price)) if price is not None else None,
                cost_eur=Decimal(str(cost)) if cost is not None else None,
                sales_total=sales_total, views_30d=views_30d,
                impressions_week=impressions, clicks_week=clicks)
    if started_days_ago is not None:
        l.listing_start_date = datetime.now(timezone.utc) - timedelta(days=started_days_ago)
    db.add(l)
    db.flush()
    return l


def _sale(db, *, listing=None, price, status="paid"):
    s = Sale(ebay_transaction_id=f"tx-{next(_TX)}",
             listing_id=listing.id if listing else None,
             price_eur=Decimal(str(price)), status=status,
             sale_date=datetime.now(timezone.utc))
    db.add(s)
    db.flush()
    return s


def test_concentration_counts_listings_for_revenue_shares(db):
    big = _listing(db, title="Big", price=30, sales_total=8)
    mid = _listing(db, title="Mid", price=20, sales_total=2)
    tiny = _listing(db, title="Tiny", price=10, sales_total=1)
    for _ in range(8):
        _sale(db, listing=big, price=100)   # 800 EUR
    _sale(db, listing=mid, price=100)       # 100 EUR
    _sale(db, listing=tiny, price=50)       # 50 EUR
    db.commit()

    r = get_portfolio_analysis(db)
    assert r["basis"]["revenue_linked_eur"] == 950.0
    assert r["concentration"]["listings_with_sale"] == 3
    # 800 von 950 = 84 % -> EIN Listing deckt schon 50 % und 80 %
    assert r["concentration"]["n_for_50pct"] == 1
    assert r["concentration"]["n_for_80pct"] == 1
    # 95 % von 950 = 902,50 -> Big+Mid (900) reicht NICHT, es braucht alle drei
    assert r["concentration"]["n_for_95pct"] == 3
    assert r["concentration"]["top"][0]["title"] == "Big"
    assert r["concentration"]["top"][0]["revenue_eur"] == 800.0


def test_cancelled_and_unlinked_sales_excluded_but_reported(db):
    l = _listing(db, title="A", sales_total=1)
    _sale(db, listing=l, price=40)
    _sale(db, listing=l, price=99, status="refunded")    # zaehlt NICHT
    _sale(db, listing=l, price=99, status="cancelled")   # zaehlt NICHT
    _sale(db, listing=None, price=25)                    # unverknuepft -> separat
    db.commit()

    r = get_portfolio_analysis(db)
    assert r["basis"]["revenue_linked_eur"] == 40.0
    assert r["basis"]["sales_unlinked"] == 1
    assert r["basis"]["revenue_unlinked_eur"] == 25.0


def test_funnel_buckets(db):
    _listing(db, title="Winner", sales_total=3, views_30d=50, clicks=2)
    _listing(db, title="Clicked", sales_total=0, views_30d=12, impressions=100, clicks=4)
    _listing(db, title="ShownOnly", sales_total=0, views_30d=0, impressions=80, clicks=0)
    _listing(db, title="Invisible", sales_total=0, views_30d=0, impressions=0, clicks=0)
    _listing(db, title="NoStats", sales_total=0, views_30d=None, impressions=0, clicks=0)
    _listing(db, title="Draft", status="draft")  # zaehlt gar nicht mit
    db.commit()

    f = get_portfolio_analysis(db)["funnel"]
    assert f["winner"] == 1
    assert f["clicked_no_sale"] == 1
    assert f["shown_no_click"] == 1
    # Ohne Statistik ist "unsichtbar" nicht von "nie geladen" unterscheidbar ->
    # beide landen im Unsichtbar-Topf, stats_missing weist die Unschaerfe aus.
    assert f["invisible"] == 2
    assert f["stats_missing"] == 1


def test_cohorts_new_vs_old(db):
    _listing(db, title="Neu1", started_days_ago=7, sales_total=1)
    _listing(db, title="Neu2", started_days_ago=20, sales_total=0)
    _listing(db, title="Alt1", started_days_ago=120, sales_total=5)
    db.commit()

    co = get_portfolio_analysis(db)["cohorts"]
    assert co["new"] == {"listings": 2, "sold_any": 1, "sales_total": 1}
    assert co["old"] == {"listings": 1, "sold_any": 1, "sales_total": 5}


def test_price_bands_and_winner_profile(db):
    w1 = _listing(db, title="W1", price=27.95, cost=17.86, sales_total=4)
    w2 = _listing(db, title="W2", price=39.95, cost=20.00, sales_total=3)
    _listing(db, title="Einmal", price=12.00, sales_total=1)  # <3 -> kein Gewinner-Profil
    _sale(db, listing=w1, price=27.95)
    _sale(db, listing=w2, price=39.95)
    _sale(db, listing=w2, price=85.00)
    db.commit()

    r = get_portfolio_analysis(db)
    bands = {(b["from_eur"], b["to_eur"]): b["sales"] for b in r["price_bands"]}
    assert bands[(25, 35)] == 1
    assert bands[(35, 50)] == 1
    assert bands[(80, None)] == 1

    w = r["winner_profile"]
    assert w["count"] == 2
    assert w["price_median_eur"] == round((27.95 + 39.95) / 2, 2)
    assert w["margin_median_eur"] == round(((27.95 - 17.86) + (39.95 - 20.0)) / 2, 2)
